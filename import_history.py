"""Idempotent batch import of the existing group_news.json archive."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from apps_script_client import AppsScriptClient
from news_record import build_sheet_record


def load_archive(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        records = json.load(file)
    if not isinstance(records, list):
        raise ValueError("Archive must contain a JSON list")
    return records


def batches(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def prepare_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    keys: set[str] = set()
    for record in records:
        row = build_sheet_record(record)
        if row["record_key"] in keys:
            continue
        keys.add(row["record_key"])
        rows.append(row)
    return rows


def send_batches(
    rows: list[dict[str, Any]], client: AppsScriptClient, batch_size: int
) -> dict[str, int]:
    totals = {"records": 0, "all_news": 0, "izhs_corp": 0, "errors": 0}
    for batch in batches(rows, batch_size):
        results = client.sync(batch)
        if any(item.get("error") for item in results):
            # A repeated request is safe and repairs a one-sheet partial write.
            results = client.sync(batch)
        totals["records"] += len(results)
        for result in results:
            totals["all_news"] += result.get("all_news") in {"inserted", "exists"}
            totals["izhs_corp"] += result.get("izhs_corp") in {
                "inserted",
                "exists",
                "not_required",
            }
            totals["errors"] += bool(result.get("error"))
    client.repair()
    if totals["errors"]:
        raise RuntimeError(
            f"Import left {totals['errors']} records unsynchronized; rerun is safe"
        )
    return totals


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument(
        "--send",
        action="store_true",
        help="Perform Google requests. Without this flag only validation is run.",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    if not 1 <= args.batch_size <= 100:
        raise ValueError("--batch-size must be between 1 and 100")
    records = load_archive(args.archive)
    rows = prepare_rows(records)
    tagged = sum(bool(row["hashtags"]) for row in rows)
    print(f"Archive records: {len(records)}")
    print(f"Unique valid rows: {len(rows)}")
    print(f"Rows for ИЖС-КОРП: {tagged}")
    print(f"Planned batches: {(len(rows) + args.batch_size - 1) // args.batch_size}")
    if not args.send:
        print("Dry run only. No network requests were made.")
        return

    url = os.getenv("GOOGLE_APPS_SCRIPT_URL")
    secret = os.getenv("GOOGLE_APPS_SCRIPT_SECRET")
    if not url or not secret:
        raise RuntimeError(
            "GOOGLE_APPS_SCRIPT_URL and GOOGLE_APPS_SCRIPT_SECRET are required"
        )
    client = AppsScriptClient(url, secret)
    totals = send_batches(rows, client, args.batch_size)
    print(
        "Import completed: "
        f"records={totals['records']} all_news_ok={totals['all_news']} "
        f"izhs_corp_ok={totals['izhs_corp']} errors={totals['errors']}"
    )


if __name__ == "__main__":
    main()

