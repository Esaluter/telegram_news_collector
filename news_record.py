"""Conversion of stored Telegram updates to the safe Google Sheets schema.

The hashtag and link rules are copied from the working monitoring project.
Only the compact row returned by ``build_sheet_record`` leaves the host.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit


TARGET_HASHTAG_RE = re.compile(
    r"(?<![0-9a-zа-яё_])#(ижс|корп)(?![0-9a-zа-яё_])",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
MAX_CELL_LENGTH = 32767


def target_hashtags(message: dict[str, Any]) -> list[str]:
    """Return standalone #ижс/#корп tags using the existing project rule."""
    text = "\n".join(str(message.get(key) or "") for key in ("text", "caption"))
    return list(
        dict.fromkeys(
            f"#{match.casefold()}" for match in TARGET_HASHTAG_RE.findall(text)
        )
    )


def get_news_links(message: dict[str, Any]) -> list[str]:
    """Collect and normalize links using the existing collector rules."""
    urls: list[str] = []
    text = message.get("text") or message.get("caption") or ""
    urls.extend(URL_RE.findall(str(text)))

    for entity in (message.get("entities") or []) + (
        message.get("caption_entities") or []
    ):
        if isinstance(entity, dict) and entity.get("url"):
            urls.append(str(entity["url"]))

    preview = message.get("link_preview_options") or {}
    if isinstance(preview, dict) and preview.get("url"):
        urls.append(str(preview["url"]))

    normalized: list[str] = []
    for raw_url in urls:
        url = raw_url.rstrip('.,;!?)]}"')
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            continue
        value = urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip("/"),
                parts.query,
                "",
            )
        )
        if value not in normalized:
            normalized.append(value)
    return normalized


def message_key(message: dict[str, Any]) -> str:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if chat_id is None or message_id is None:
        raise ValueError("Telegram message has no chat.id or message_id")
    return f"{chat_id}:{message_id}"


def _telegram_date(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return ""


def _sender_name(message: dict[str, Any]) -> str:
    sender = message.get("from") or {}
    return " ".join(
        value
        for value in (sender.get("first_name"), sender.get("last_name"))
        if value
    )


def _forward_source(message: dict[str, Any]) -> str:
    origin = message.get("forward_origin") or {}
    return str(
        origin.get("sender_user_name")
        or (origin.get("chat") or {}).get("title")
        or message.get("forward_sender_name")
        or ""
    )


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)[:MAX_CELL_LENGTH]


def build_sheet_record(record: dict[str, Any]) -> dict[str, Any]:
    """Build the allowlisted payload sent to Apps Script.

    Sender IDs, usernames, replies, photo identifiers, and the full Telegram
    object intentionally stay only in the local JSON archive.
    """
    message = record.get("telegram") or {}
    chat = message.get("chat") or {}
    tags = target_hashtags(message)
    text = message.get("text") or message.get("caption") or ""
    return {
        "record_key": message_key(message),
        "message_date": _telegram_date(message.get("date")),
        "group_title": _cell_text(chat.get("title")),
        "sender_name": _cell_text(_sender_name(message)),
        "forward_source": _cell_text(_forward_source(message)),
        "text": _cell_text(text),
        "links": get_news_links(message),
        "hashtags": tags,
        "message_id": message.get("message_id"),
        "chat_id": chat.get("id"),
        "collected_at": _cell_text(record.get("collected_at")),
    }


def needs_tagged_sheet(record: dict[str, Any]) -> bool:
    return bool(build_sheet_record(record)["hashtags"])

