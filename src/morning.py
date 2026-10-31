"""Collect a fixed Beijing morning window for the scheduled Gmail digest.

Run from the repository with `.venv/bin/python -m src.morning`.
The caller summarizes the resulting JSON and sends it through connected Gmail;
this command never sends messages or invokes a paid model API.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from email.utils import parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

from ._file_utils import _atomic_write_text


BEIJING = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class MorningWindow:
    start: datetime
    end: datetime

    @property
    def subject(self) -> str:
        local = self.end.astimezone(BEIJING)
        return f"{local.month}月{local.day}日早间甜点"

    def metadata(self) -> dict:
        return {
            "issue_date": self.end.astimezone(BEIJING).date().isoformat(),
            "subject": self.subject,
            "timezone": "Asia/Shanghai",
            "start_inclusive": self.start.isoformat(),
            "end_exclusive": self.end.isoformat(),
            "start_beijing": self.start.astimezone(BEIJING).isoformat(),
            "end_beijing": self.end.astimezone(BEIJING).isoformat(),
        }


def morning_window(now: datetime | None = None, issue_date: date | None = None) -> MorningWindow:
    """Latest scheduled 08:00, anchored before fetching, regardless of host TZ."""
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    local = now.astimezone(BEIJING)
    end_local = datetime.combine(issue_date or local.date(), time(8), BEIJING)
    if issue_date is None and local < end_local:
        end_local -= timedelta(days=1)
    end = end_local.astimezone(timezone.utc)
    return MorningWindow(start=end - timedelta(hours=24), end=end)


def filter_morning_items(raw: list, window: MorningWindow) -> tuple[list, dict]:
    """Filter by timezone-aware publication timestamps, before URL deduplication."""
    eligible = []
    excluded = {"outside_window": 0, "unknown_timezone": 0}
    for item in raw:
        published = item.published_at
        if published.utcoffset() is None:
            excluded["unknown_timezone"] += 1
        elif window.start <= published.astimezone(timezone.utc) < window.end:
            eligible.append(item)
        else:
            excluded["outside_window"] += 1
    return eligible, excluded


def validate_morning_config(config, subscribers: list) -> None:
    """Fail before fetching/model calls when unattended delivery is not ready."""
    from .env import is_placeholder

    missing = []
    if is_placeholder(os.getenv(config.ai.api_key_env)):
        missing.append(config.ai.api_key_env)
    email = config.email
    if email is None or not email.enabled:
        missing.append("email.enabled")
    else:
        if is_placeholder(os.getenv(email.password_env)):
            missing.append(email.password_env)
        if not valid_email(email.email_address):
            missing.append("GMAIL_ADDRESS / email.email_address")
    if not subscribers or any(not valid_email(address) for address in subscribers):
        missing.append("data/subscribers.json (valid recipient addresses)")
    if missing:
        raise ValueError("Morning configuration missing: " + ", ".join(missing))


def valid_email(address) -> bool:
    return (
        isinstance(address, str) and "\r" not in address and "\n" not in address
        and parseaddr(address)[1] == address and "@" in address
        and not address.startswith("${") and not address.startswith("xxx@")
    )


async def collect_morning(orchestrator, window: MorningWindow) -> dict:
    """Apply a strict half-open publication window before cross-source merging."""
    raw = await orchestrator.fetch_all_sources(window.start)
    report = orchestrator.last_fetch_report
    if report is not None and report.all_failed:
        raise RuntimeError(report.failure_message())
    if not raw:
        raise RuntimeError("No dated items collected; verify source availability before preparing a digest.")
    eligible, excluded = filter_morning_items(raw, window)
    merged = sorted(
        orchestrator.merge_cross_source_duplicates(eligible),
        key=lambda item: item.published_at, reverse=True,
    )
    return {
        **window.metadata(),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "raw_count": len(raw),
        "item_count": len(merged),
        "excluded": excluded,
        "fetch_report": report.to_dict() if report is not None else None,
        "items": [
            {
                "id": item.id,
                "source": item.source_type.value,
                "title": item.title,
                "url": str(item.url),
                "published_at": item.published_at.astimezone(timezone.utc).isoformat(),
                "published_beijing": item.published_at.astimezone(BEIJING).isoformat(),
                "author": item.author,
                "content": (item.content or "")[:16000],
                "profile": item.profile,
            }
            for item in merged
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="Beijing issue date YYYY-MM-DD")
    parser.add_argument("--window-only", action="store_true", help="Show dates without fetching or writing")
    parser.add_argument("--sources", nargs="+", help="Optionally restrict source types for diagnostics")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/morning", help="Directory for collected JSON")
    args = parser.parse_args()
    window = morning_window(issue_date=args.date)
    if args.window_only:
        print(json.dumps(window.metadata(), ensure_ascii=False, indent=2))
        return

    from .mcp.horizon_adapter import (
        apply_source_filter, load_config, load_environment, make_storage,
    )
    from .orchestrator import HorizonOrchestrator

    load_environment(ROOT)
    config_path = ROOT / "data/config.json"
    config = load_config(config_path)
    if args.sources:
        config, _, unknown = apply_source_filter(config, args.sources)
        if unknown:
            parser.error(f"Unknown source types: {', '.join(unknown)}")
    # Profiles are project-relative even when invoked from another working directory.
    config.processing.profiles_dir = str(ROOT / config.processing.profiles_dir)
    orchestrator = HorizonOrchestrator(config, make_storage(config_path))
    payload = asyncio.run(collect_morning(orchestrator, window))
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / f"{payload['issue_date']}.json"
    _atomic_write_text(artifact, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({**window.metadata(), "item_count": payload["item_count"], "artifact": str(artifact)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
