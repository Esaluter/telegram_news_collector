"""Local JSON archive and durable Google synchronization queue.

The persistence and deduplication functions retain the behavior of
``group_news_storage.py`` from the source project.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from news_record import message_key, target_hashtags


def load_news(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as file:
        records = json.load(file)
    if not isinstance(records, list):
        raise ValueError("group_news.json должен содержать список")
    return records


def save_json_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".group_news_", suffix=".json", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(records, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def is_duplicate(records: list[dict[str, Any]], message: Any) -> bool:
    """Reject only repeated delivery of the same Telegram message."""
    for record in records:
        existing = record.get("telegram", {})
        chat = existing.get("chat") or {}
        if (
            chat.get("id") == message.chat_id
            and existing.get("message_id") == message.message_id
        ):
            return True
    return False


def classify_group_message(
    records: list[dict[str, Any]], message: Any, group_id: int
) -> dict[str, str]:
    if message.chat_id != group_id:
        return {"status": "skipped", "reason": "unexpected_chat"}
    if is_duplicate(records, message):
        return {"status": "duplicate", "reason": "duplicate_chat_message_id"}
    return {"status": "new", "reason": "new_message"}


def make_record(message: Any) -> dict[str, Any]:
    telegram = message.to_dict()
    has_target_tag = bool(target_hashtags(telegram))
    now = datetime.now(timezone.utc).isoformat()
    return {
        "telegram": telegram,
        "processing": {
            "status": "new",
            "processed_at": None,
            "error": None,
        },
        "collected_at": now,
        "google_sync": {
            "all_news": "pending",
            "izhs_corp": "pending" if has_target_tag else "not_required",
            "last_error": None,
            "updated_at": now,
        },
    }


def _sync_state(record: dict[str, Any]) -> dict[str, Any]:
    state = record.setdefault("google_sync", {})
    state.setdefault("all_news", "pending")
    tags = target_hashtags(record.get("telegram") or {})
    state.setdefault("izhs_corp", "pending" if tags else "not_required")
    state.setdefault("last_error", None)
    state.setdefault("updated_at", None)
    return state


def record_is_pending(record: dict[str, Any]) -> bool:
    state = _sync_state(record)
    return state["all_news"] != "synced" or state["izhs_corp"] not in {
        "synced",
        "not_required",
    }


class NewsStorage:
    def __init__(self, path: Path, group_id: int):
        self.path = path
        self.group_id = group_id
        self._lock = asyncio.Lock()

    async def add_message(self, message: Any) -> dict[str, str]:
        initial = classify_group_message([], message, self.group_id)
        if initial["status"] == "skipped":
            return initial

        async with self._lock:
            records = await asyncio.to_thread(load_news, self.path)
            classification = classify_group_message(records, message, self.group_id)
            if classification["status"] != "new":
                return classification
            records.append(make_record(message))
            await asyncio.to_thread(save_json_atomic, self.path, records)
            return {**classification, "record_key": message_key(message.to_dict())}

    async def pending(self, limit: int) -> list[dict[str, Any]]:
        async with self._lock:
            records = await asyncio.to_thread(load_news, self.path)
            return [record for record in records if record_is_pending(record)][:limit]

    async def pending_count(self) -> int:
        async with self._lock:
            records = await asyncio.to_thread(load_news, self.path)
            return sum(record_is_pending(record) for record in records)

    async def mark_results(
        self, results: list[dict[str, Any]], error: str | None = None
    ) -> None:
        result_by_key = {
            item.get("record_key"): item for item in results if item.get("record_key")
        }
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            records = await asyncio.to_thread(load_news, self.path)
            changed = False
            for record in records:
                key = message_key(record.get("telegram") or {})
                result = result_by_key.get(key)
                if result is None:
                    continue
                state = _sync_state(record)
                for local_name, remote_name in (
                    ("all_news", "all_news"),
                    ("izhs_corp", "izhs_corp"),
                ):
                    remote_status = result.get(remote_name)
                    if remote_status in {"inserted", "exists", "not_required"}:
                        new_status = (
                            "not_required"
                            if remote_status == "not_required"
                            else "synced"
                        )
                        if state.get(local_name) != new_status:
                            state[local_name] = new_status
                            changed = True
                state["last_error"] = result.get("error") or error
                state["updated_at"] = now
                changed = True
            if changed:
                await asyncio.to_thread(save_json_atomic, self.path, records)

    async def mark_batch_error(
        self, records: list[dict[str, Any]], error: str
    ) -> None:
        results = [
            {"record_key": message_key(record.get("telegram") or {}), "error": error}
            for record in records
        ]
        await self.mark_results(results, error=error)

