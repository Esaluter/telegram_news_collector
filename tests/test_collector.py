from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

