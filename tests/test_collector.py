from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps_script_client import AppsScriptError
from collector import (
    CollectorRuntime,
    PeriodCounters,
    Settings,
    configure_logging,
    format_status_report,
)
from import_history import prepare_rows, send_batches
from news_record import build_sheet_record, get_news_links, target_hashtags
from storage import NewsStorage, load_news


GROUP_ID = -1001234567890


class FakeMessage:
    def __init__(self, message_id: int, text: str, chat_id: int = GROUP_ID):
        self.chat_id = chat_id
        self.message_id = message_id
        self._data = {
            "message_id": message_id,
            "date": 1760000000,
            "chat": {"id": chat_id, "title": "Рабочая группа"},
            "from": {"id": 999, "first_name": "Имя", "username": "private"},
            "text": text,
            "entities": [{"type": "text_link", "url": "https://EXAMPLE.org/post/"}],
        }

    def to_dict(self):
        return dict(self._data)


class FakeAppsScript:
    """In-memory implementation of the Apps Script idempotency contract."""

    def __init__(self):
        self.all_news = {}
        self.tagged = {}
        self.fail_tagged_once = False

    def sync(self, rows):
        results = []
        for row in rows:
            key = row["record_key"]
            all_status = "exists" if key in self.all_news else "inserted"
            self.all_news.setdefault(key, {**row, "manual_comment": ""})
            if not row["hashtags"]:
                tagged_status = "not_required"
            elif key in self.tagged:
                tagged_status = "exists"
            elif self.fail_tagged_once:
                self.fail_tagged_once = False
                tagged_status = "error"
            else:
                self.tagged[key] = {**row, "manual_comment": ""}
                tagged_status = "inserted"
            result = {
                "record_key": key,
                "all_news": all_status,
                "izhs_corp": tagged_status,
            }
            if tagged_status == "error":
                result["error"] = "simulated_tagged_failure"
            results.append(result)
        return results

    def repair(self):
        for key, row in list(self.tagged.items()):
            self.all_news.setdefault(key, {**row, "manual_comment": ""})
        for key, row in list(self.all_news.items()):
            if row["hashtags"]:
                self.tagged.setdefault(key, {**row, "manual_comment": ""})
        return {"ok": True}


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    async def send_message(self, chat_id, text, parse_mode=None):
        if self.fail:
            raise RuntimeError("simulated_control_delivery_failure")
        self.messages.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )


def runtime_settings(
    status_chat_id=-1003732301710,
    google_sheet_url="https://docs.google.com/spreadsheets/d/test/edit",
):
    return Settings(
        bot_token="test-token",
        group_id=GROUP_ID,
        apps_script_url="https://example.invalid/exec",
        apps_script_secret="test-secret",
        archive_path=Path("unused.json"),
        batch_size=50,
        retry_seconds=60,
        request_timeout_seconds=20,
        status_chat_id=status_chat_id,
        google_sheet_url=google_sheet_url,
    )


class TelegramRulesTests(unittest.TestCase):
    def test_standalone_hashtags_keep_existing_rules(self):
        self.assertEqual(["#ижс", "#корп"], target_hashtags({"text": "#ИЖС и #КоРп"}))
        self.assertEqual([], target_hashtags({"text": "#ижс123 #корпоратив"}))

    def test_message_is_normalized_without_full_private_object(self):
        message = FakeMessage(7, "Новость #ИЖС https://Example.com/a/")
        record = {"telegram": message.to_dict(), "collected_at": "2026-01-01T00:00:00+00:00"}
        row = build_sheet_record(record)
        self.assertEqual(f"{GROUP_ID}:7", row["record_key"])
        self.assertEqual(["#ижс"], row["hashtags"])
        self.assertEqual(
            ["https://example.com/a", "https://example.org/post"],
            get_news_links(record["telegram"]),
        )
        self.assertNotIn("telegram", row)
        self.assertNotIn("username", row)
        self.assertNotIn("sender_id", row)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_filter_and_chat_message_deduplication(self):
        with tempfile.TemporaryDirectory() as root:
            storage = NewsStorage(Path(root) / "group_news.json", GROUP_ID)
            saved = await storage.add_message(FakeMessage(1, "Новость"))
            duplicate = await storage.add_message(FakeMessage(1, "Повтор"))
            skipped = await storage.add_message(FakeMessage(2, "Чужая", chat_id=-1))
            self.assertEqual("new", saved["status"])
            self.assertEqual("duplicate", duplicate["status"])
            self.assertEqual("skipped", skipped["status"])
            self.assertEqual(1, len(load_news(storage.path)))
            self.assertEqual(1, await storage.pending_count())

    async def test_partial_sheet_failure_is_retried_without_overwriting_manual_data(self):
        with tempfile.TemporaryDirectory() as root:
            storage = NewsStorage(Path(root) / "group_news.json", GROUP_ID)
            await storage.add_message(FakeMessage(10, "Новость #корп"))
            remote = FakeAppsScript()
            remote.fail_tagged_once = True

            pending = await storage.pending(10)
            rows = [build_sheet_record(record) for record in pending]
            first = remote.sync(rows)
            await storage.mark_results(first)
            self.assertEqual(1, len(await storage.pending(10)))

            remote.all_news[rows[0]["record_key"]]["manual_comment"] = "Не менять"
            second = remote.sync(rows)
            await storage.mark_results(second)
            self.assertEqual([], await storage.pending(10))
            self.assertEqual(
                "Не менять", remote.all_news[rows[0]["record_key"]]["manual_comment"]
            )
            self.assertEqual(1, len(remote.all_news))
            self.assertEqual(1, len(remote.tagged))


class HistoricalImportTests(unittest.TestCase):
    def test_batch_import_is_idempotent_and_writes_both_sheets(self):
        records = []
        for message_id, text in ((1, "Обычная"), (2, "Новость #ижс"), (3, "#КОРП")):
            records.append(
                {
                    "telegram": FakeMessage(message_id, text).to_dict(),
                    "collected_at": "2026-01-01T00:00:00+00:00",
                }
            )
        rows = prepare_rows(records + [records[0]])
        self.assertEqual(3, len(rows))
        remote = FakeAppsScript()
        first = send_batches(rows, remote, batch_size=2)
        second = send_batches(rows, remote, batch_size=2)
        self.assertEqual(3, first["records"])
        self.assertEqual(3, second["records"])
        self.assertEqual(3, len(remote.all_news))
        self.assertEqual(2, len(remote.tagged))


class SettingsTests(unittest.TestCase):
    def test_status_chat_is_optional(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "token",
            "GROUP_ID": str(GROUP_ID),
            "GOOGLE_APPS_SCRIPT_URL": "https://example.invalid/exec",
            "GOOGLE_APPS_SCRIPT_SECRET": "secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()
            self.assertIsNone(settings.status_chat_id)
            self.assertIsNone(settings.google_sheet_url)

    def test_status_chat_cannot_be_news_group(self):
        environment = {
            "TELEGRAM_BOT_TOKEN": "token",
            "GROUP_ID": str(GROUP_ID),
            "GOOGLE_APPS_SCRIPT_URL": "https://example.invalid/exec",
            "GOOGLE_APPS_SCRIPT_SECRET": "secret",
            "STATUS_CHAT_ID": str(GROUP_ID),
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "must differ"):
                Settings.from_environment()


class LoggingTests(unittest.TestCase):
    def test_http_client_info_logs_are_suppressed(self):
        httpx_logger = logging.getLogger("httpx")
        httpcore_logger = logging.getLogger("httpcore")
        application_logger = logging.getLogger("telegram_news_collector")
        original_levels = (
            httpx_logger.level,
            httpcore_logger.level,
            application_logger.level,
        )
        try:
            httpx_logger.setLevel(logging.NOTSET)
            httpcore_logger.setLevel(logging.NOTSET)
            application_logger.setLevel(logging.NOTSET)
            with patch.dict(os.environ, {"LOG_LEVEL": "INFO"}), patch(
                "collector.logging.basicConfig"
            ) as basic_config:
                configure_logging()

            basic_config.assert_called_once_with(
                level="INFO",
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            )
            self.assertEqual(logging.WARNING, httpx_logger.level)
            self.assertEqual(logging.WARNING, httpcore_logger.level)
            self.assertEqual(logging.NOTSET, application_logger.level)
        finally:
            httpx_logger.setLevel(original_levels[0])
            httpcore_logger.setLevel(original_levels[1])
            application_logger.setLevel(original_levels[2])


class StatusNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_period_counters_reset_after_snapshot(self):
        counters = PeriodCounters(
            received=5, new=3, duplicates=1, google_written=2, skipped=2, errors=1
        )
        snapshot = counters.snapshot_and_reset()
        self.assertEqual(5, snapshot["received"])
        self.assertEqual(2, snapshot["google_written"])
        self.assertEqual(PeriodCounters(), counters)
        report = format_status_report(snapshot, 4)
        self.assertIn("Получено сообщений: 5", report)
        self.assertIn("Ожидают отправки: 4", report)
        self.assertNotIn("Открыть Google Таблицу", report)

    async def test_report_includes_optional_clickable_google_sheet_link(self):
        report = format_status_report(
            PeriodCounters().snapshot_and_reset(),
            0,
            "https://docs.google.com/spreadsheets/d/test/edit?usp=sharing&x=1",
        )
        self.assertIn(
            '<a href="https://docs.google.com/spreadsheets/d/test/edit?usp=sharing&amp;x=1">'
            "📊 Открыть Google Таблицу</a>",
            report,
        )

    async def test_delivery_counters_keep_categories_separate(self):
        runtime = CollectorRuntime(runtime_settings())
        runtime.record_delivery("new")
        runtime.record_delivery("duplicate")
        runtime.record_delivery("skipped")
        self.assertEqual(3, runtime.counters.received)
        self.assertEqual(1, runtime.counters.new)
        self.assertEqual(1, runtime.counters.duplicates)
        self.assertEqual(1, runtime.counters.skipped)

    async def test_notifications_are_disabled_without_status_chat(self):
        runtime = CollectorRuntime(runtime_settings(status_chat_id=None))
        runtime.bot = FakeBot()
        self.assertFalse(await runtime.send_control_message("test"))
        self.assertEqual([], runtime.bot.messages)

    async def test_control_delivery_failure_does_not_escape(self):
        runtime = CollectorRuntime(runtime_settings())
        runtime.bot = FakeBot(fail=True)
        self.assertFalse(await runtime.send_control_message("test"))

    async def test_identical_google_warning_is_suppressed_until_recovery(self):
        runtime = CollectorRuntime(runtime_settings())
        runtime.bot = FakeBot()
        error = AppsScriptError("sensitive detail must not be sent")
        await runtime.notify_google_error(error)
        await runtime.notify_google_error(error)
        self.assertEqual(1, len(runtime.bot.messages))
        self.assertNotIn("sensitive detail", runtime.bot.messages[0]["text"])
        runtime.note_google_success()
        await runtime.notify_google_error(error)
        self.assertEqual(2, len(runtime.bot.messages))

    async def test_periodic_report_uses_current_queue_and_resets_period(self):
        runtime = CollectorRuntime(runtime_settings())
        runtime.bot = FakeBot()

        class QueueStorage:
            async def pending_count(self):
                return 7

        runtime.storage = QueueStorage()
        runtime.counters.received = 2
        runtime.counters.new = 2
        await runtime.send_periodic_report()
        self.assertEqual(1, len(runtime.bot.messages))
        self.assertIn("Получено сообщений: 2", runtime.bot.messages[0]["text"])
        self.assertIn("Ожидают отправки: 7", runtime.bot.messages[0]["text"])
        self.assertIn("📊 Открыть Google Таблицу", runtime.bot.messages[0]["text"])
        self.assertEqual("HTML", runtime.bot.messages[0]["parse_mode"])
        self.assertEqual(PeriodCounters(), runtime.counters)


if __name__ == "__main__":
    unittest.main()

