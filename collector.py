"""Standalone Telegram news collector entry point for Bothost."""

from __future__ import annotations

import asyncio
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
        return cls(
            bot_token=str(required["TELEGRAM_BOT_TOKEN"]),
            group_id=int(str(required["GROUP_ID"])),
            apps_script_url=str(required["GOOGLE_APPS_SCRIPT_URL"]),
            apps_script_secret=str(required["GOOGLE_APPS_SCRIPT_SECRET"]),
            archive_path=Path(os.getenv("ARCHIVE_PATH", "data/group_news.json")),
            batch_size=batch_size,
            retry_seconds=max(10, int(os.getenv("SYNC_RETRY_SECONDS", "60"))),
            request_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
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
                logger.warning("Google synchronization deferred: %s", exc)
                return 0
            await self.storage.mark_results(results)
            synced = sum(
                item.get("all_news") in {"inserted", "exists"}
                and item.get("izhs_corp")
                in {"inserted", "exists", "not_required"}
                for item in results
            )
            logger.info("Google synchronization completed: %s/%s", synced, len(rows))
            return synced

    async def retry_loop(self) -> None:
        while True:
            try:
                await self.sync_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected synchronization error")
            await asyncio.sleep(self.settings.retry_seconds)


def _runtime(application: Application) -> CollectorRuntime:
    return application.bot_data["collector_runtime"]


async def group_news_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    runtime = _runtime(context.application)
    result = await runtime.storage.add_message(message)
    logger.info("Telegram update: status=%s reason=%s", result["status"], result["reason"])
    if result["status"] in {"new", "duplicate"}:
        await runtime.sync_pending()


async def post_init(application: Application) -> None:
    runtime = _runtime(application)
    try:
        repair = await asyncio.to_thread(runtime.client.repair)
        logger.info(
            "Google sheet repair completed: all_news=%s izhs_corp=%s",
            repair.get("all_news_repaired", 0),
            repair.get("izhs_corp_repaired", 0),
        )
    except AppsScriptError as exc:
        logger.warning("Google sheet repair deferred: %s", exc)
    await runtime.sync_pending()
    runtime.retry_task = asyncio.create_task(runtime.retry_loop(), name="google_sync_retry")


async def post_shutdown(application: Application) -> None:
    runtime = _runtime(application)
    task = runtime.retry_task
    if task is None or task.done():
        return
    task.cancel()
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

