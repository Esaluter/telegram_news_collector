"""Standalone Telegram news collector entry point for Bothost."""

from __future__ import annotations

import asyncio
import html
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from apps_script_client import AppsScriptClient, AppsScriptError
from news_record import build_sheet_record
from storage import NewsStorage


logger = logging.getLogger("telegram_news_collector")
STATUS_INTERVAL_SECONDS = 3 * 60 * 60


@dataclass
class PeriodCounters:
    received: int = 0
    new: int = 0
    duplicates: int = 0
    google_written: int = 0
    skipped: int = 0
    errors: int = 0

    def snapshot_and_reset(self) -> dict[str, int]:
        snapshot = {
            "received": self.received,
            "new": self.new,
            "duplicates": self.duplicates,
            "google_written": self.google_written,
            "skipped": self.skipped,
            "errors": self.errors,
        }
        self.received = 0
        self.new = 0
        self.duplicates = 0
        self.google_written = 0
        self.skipped = 0
        self.errors = 0
        return snapshot


def format_status_report(
    counters: dict[str, int], pending_count: int, google_sheet_url: str | None = None
) -> str:
    report = (
        "📊 Контрольный отчёт Telegram-сборщика\n"
        "Отчётный период: последние 3 часа\n\n"
        f"Получено сообщений: {counters['received']}\n"
        f"Новых: {counters['new']}\n"
        f"Дубликатов: {counters['duplicates']}\n"
        f"Записано в Google: {counters['google_written']}\n"
        f"Пропущено: {counters['skipped']}\n"
        f"Ошибок: {counters['errors']}\n"
        f"Ожидают отправки: {pending_count}"
    )
    if google_sheet_url:
        safe_url = html.escape(google_sheet_url, quote=True)
        report += f'\n\n<a href="{safe_url}">📊 Открыть Google Таблицу</a>'
    return report


@dataclass(frozen=True)
class Settings:
    bot_token: str
    group_id: int
    apps_script_url: str
    apps_script_secret: str
    archive_path: Path
    batch_size: int
    retry_seconds: int
    request_timeout_seconds: float
    status_chat_id: int | None
    google_sheet_url: str | None

    @classmethod
    def from_environment(cls) -> "Settings":
        required = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
            "GROUP_ID": os.getenv("GROUP_ID"),
            "GOOGLE_APPS_SCRIPT_URL": os.getenv("GOOGLE_APPS_SCRIPT_URL"),
            "GOOGLE_APPS_SCRIPT_SECRET": os.getenv("GOOGLE_APPS_SCRIPT_SECRET"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("Missing environment variables: " + ", ".join(missing))
        batch_size = int(os.getenv("SYNC_BATCH_SIZE", "50"))
        if not 1 <= batch_size <= 100:
            raise ValueError("SYNC_BATCH_SIZE must be between 1 and 100")
        group_id = int(str(required["GROUP_ID"]))
        raw_status_chat_id = os.getenv("STATUS_CHAT_ID", "").strip()
        status_chat_id = int(raw_status_chat_id) if raw_status_chat_id else None
        if status_chat_id == group_id:
            raise ValueError("STATUS_CHAT_ID must differ from GROUP_ID")
        return cls(
            bot_token=str(required["TELEGRAM_BOT_TOKEN"]),
            group_id=group_id,
            apps_script_url=str(required["GOOGLE_APPS_SCRIPT_URL"]),
            apps_script_secret=str(required["GOOGLE_APPS_SCRIPT_SECRET"]),
            archive_path=Path(os.getenv("ARCHIVE_PATH", "data/group_news.json")),
            batch_size=batch_size,
            retry_seconds=max(10, int(os.getenv("SYNC_RETRY_SECONDS", "60"))),
            request_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
            status_chat_id=status_chat_id,
            google_sheet_url=os.getenv("GOOGLE_SHEET_URL", "").strip() or None,
        )


class CollectorRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = NewsStorage(settings.archive_path, settings.group_id)
        self.client = AppsScriptClient(
            settings.apps_script_url,
            settings.apps_script_secret,
            settings.request_timeout_seconds,
        )
        self.sync_lock = asyncio.Lock()
        self.retry_task: asyncio.Task | None = None
        self.status_task: asyncio.Task | None = None
        self.bot = None
        self.counters = PeriodCounters()
        self._google_warning_active: str | None = None

    def record_delivery(self, status: str) -> None:
        self.counters.received += 1
        if status == "new":
            self.counters.new += 1
        elif status == "duplicate":
            self.counters.duplicates += 1
        else:
            self.counters.skipped += 1

    async def send_control_message(self, text: str) -> bool:
        if self.settings.status_chat_id is None or self.bot is None:
            return False
        try:
            await self.bot.send_message(
                chat_id=self.settings.status_chat_id,
                text=text,
                parse_mode="HTML",
            )
            return True
        except Exception as exc:
            logger.warning(
                "Control message delivery failed: %s", type(exc).__name__
            )
            return False

    async def notify_google_error(self, exc: Exception) -> None:
        signature = type(exc).__name__
        if self._google_warning_active == signature:
            return
        self._google_warning_active = signature
        await self.send_control_message(
            "⚠️ Ошибка синхронизации с Google. "
            f"Тип: {signature}. "
            "Записи сохранены локально, повторная отправка будет выполнена автоматически."
        )

    def note_google_success(self) -> None:
        self._google_warning_active = None

    async def send_periodic_report(self) -> None:
        try:
            pending_count = await self.storage.pending_count()
        except Exception as exc:
            self.counters.errors += 1
            pending_count = -1
            logger.warning("Pending queue count failed: %s", type(exc).__name__)
        snapshot = self.counters.snapshot_and_reset()
        if pending_count < 0:
            report = format_status_report(
                snapshot, 0, self.settings.google_sheet_url
            ).replace(
                "Ожидают отправки: 0", "Ожидают отправки: не удалось определить"
            )
        else:
            report = format_status_report(
                snapshot, pending_count, self.settings.google_sheet_url
            )
        await self.send_control_message(report)

    async def sync_pending(self) -> int:
        async with self.sync_lock:
            pending = await self.storage.pending(self.settings.batch_size)
            if not pending:
                return 0
            rows = [build_sheet_record(record) for record in pending]
            try:
                results = await asyncio.to_thread(self.client.sync, rows)
            except AppsScriptError as exc:
                await self.storage.mark_batch_error(pending, str(exc))
                self.counters.errors += 1
                await self.notify_google_error(exc)
                logger.warning(
                    "Google synchronization deferred: %s", type(exc).__name__
                )
                return 0
            await self.storage.mark_results(results)
            synced = sum(
                item.get("all_news") in {"inserted", "exists"}
                and item.get("izhs_corp")
                in {"inserted", "exists", "not_required"}
                for item in results
            )
            failed = any(
                item.get("error")
                or item.get("all_news") == "error"
                or item.get("izhs_corp") == "error"
                for item in results
            )
            if failed:
                self.counters.errors += 1
                await self.notify_google_error(AppsScriptError("partial_sheet_write"))
            else:
                self.note_google_success()
            self.counters.google_written += synced
            logger.info("Google synchronization completed: %s/%s", synced, len(rows))
            return synced

    async def retry_loop(self) -> None:
        while True:
            try:
                await self.sync_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.counters.errors += 1
                await self.notify_google_error(exc)
                logger.exception("Unexpected synchronization error")
            await asyncio.sleep(self.settings.retry_seconds)

    async def status_loop(self) -> None:
        while True:
            await asyncio.sleep(STATUS_INTERVAL_SECONDS)
            try:
                await self.send_periodic_report()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Periodic status failed: %s", type(exc).__name__)


def _runtime(application: Application) -> CollectorRuntime:
    return application.bot_data["collector_runtime"]


async def group_news_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context.application)
    try:
        result = await runtime.storage.add_message(message)
    except Exception as exc:
        runtime.counters.received += 1
        runtime.counters.errors += 1
        logger.exception("Telegram message persistence failed: %s", type(exc).__name__)
        return
    runtime.record_delivery(result["status"])
    logger.info("Telegram update: status=%s reason=%s", result["status"], result["reason"])
    if result["status"] in {"new", "duplicate"}:
        try:
            await runtime.sync_pending()
        except Exception as exc:
            runtime.counters.errors += 1
            await runtime.notify_google_error(exc)
            logger.exception("Immediate synchronization failed: %s", type(exc).__name__)


async def post_init(application: Application) -> None:
    runtime = _runtime(application)
    runtime.bot = application.bot
    runtime.counters = PeriodCounters()
    await runtime.send_control_message(
        "✅ Telegram News Collector запущен. "
        "Контрольный отчёт будет отправляться каждые 3 часа."
    )
    if runtime.settings.status_chat_id is not None:
        runtime.status_task = asyncio.create_task(
            runtime.status_loop(), name="collector_status_report"
        )
    try:
        repair = await asyncio.to_thread(runtime.client.repair)
        runtime.note_google_success()
        logger.info(
            "Google sheet repair completed: all_news=%s izhs_corp=%s",
            repair.get("all_news_repaired", 0),
            repair.get("izhs_corp_repaired", 0),
        )
    except AppsScriptError as exc:
        runtime.counters.errors += 1
        await runtime.notify_google_error(exc)
        logger.warning("Google sheet repair deferred: %s", type(exc).__name__)
    await runtime.sync_pending()
    runtime.retry_task = asyncio.create_task(runtime.retry_loop(), name="google_sync_retry")


async def post_shutdown(application: Application) -> None:
    runtime = _runtime(application)
    tasks = [task for task in (runtime.retry_task, runtime.status_task) if task]
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


def build_application(settings: Settings) -> Application:
    runtime = CollectorRuntime(settings)
    application = (
        Application.builder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["collector_runtime"] = runtime
    application.add_handler(
        MessageHandler(
            filters.Chat(chat_id=settings.group_id)
            & (filters.TEXT | filters.CAPTION)
            & ~filters.COMMAND,
            group_news_handler,
        )
    )
    return application


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_environment()
    settings.archive_path.parent.mkdir(parents=True, exist_ok=True)
    application = build_application(settings)
    logger.info("Collector started for the configured group")
    application.run_polling(
        allowed_updates=["message", "channel_post", "edited_channel_post"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()

