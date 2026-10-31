"""Boundary tests runnable without pytest: python -m unittest tests.test_morning."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from src.morning import BEIJING, collect_morning, morning_window


class MorningTests(unittest.TestCase):
    def test_delayed_run_keeps_scheduled_cutoff_and_local_subject(self):
        window = morning_window(datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc))
        self.assertEqual(window.subject, "11月1日早间甜点")
        self.assertEqual(window.start, datetime(2026, 10, 31, tzinfo=timezone.utc))
        self.assertEqual(window.end, datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(window.end - window.start, timedelta(hours=24))

    def test_before_eight_uses_last_scheduled_issue_across_year_boundary(self):
        window = morning_window(datetime(2027, 1, 1, 7, 59, tzinfo=BEIJING))
        self.assertEqual(window.subject, "12月31日早间甜点")
        self.assertEqual(window.end.astimezone(BEIJING).date(), date(2026, 12, 31))

    def test_exact_eight_and_foreign_timezone_resolve_same_issue(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.assertEqual(morning_window(now), morning_window(now.astimezone(timezone(timedelta(hours=-7)))))
        self.assertEqual(morning_window(now).subject, "11月1日早间甜点")

    def test_explicit_issue_supports_leap_day(self):
        window = morning_window(issue_date=date(2028, 3, 1))
        self.assertEqual(window.start.astimezone(BEIJING).date(), date(2028, 2, 29))

    def test_rejects_naive_clock(self):
        with self.assertRaises(ValueError):
            morning_window(datetime(2026, 11, 1))

    def test_filters_publication_time_instead_of_fetch_time_before_dedup(self):
        window = morning_window(issue_date=date(2026, 11, 1))
        def item(key, published):
            return SimpleNamespace(id=key, published_at=published, source_type=SimpleNamespace(value="rss"), title=key, url="https://example.com/" + key, author=None, content="story", profile="tech-news")
        accepted = [item("start", window.start), item("last", (window.end - timedelta(microseconds=1)).astimezone(timezone(timedelta(hours=-7))))]
        excluded = [item("old", window.start - timedelta(seconds=1)), item("next", window.end), item("future", window.end + timedelta(hours=1)), item("naive", datetime(2026, 11, 1))]
        engine = SimpleNamespace(fetch_all_sources=AsyncMock(return_value=accepted + excluded), last_fetch_report=None, merge_cross_source_duplicates=Mock(side_effect=lambda items: items))
        payload = asyncio.run(collect_morning(engine, window))
        engine.fetch_all_sources.assert_awaited_once_with(window.start)
        engine.merge_cross_source_duplicates.assert_called_once_with(accepted)
        self.assertEqual([item["id"] for item in payload["items"]], ["last", "start"])
        self.assertEqual(payload["excluded"], {"outside_window": 3, "unknown_timezone": 1})

    def test_all_source_failures_are_not_reported_as_empty_news(self):
        report = SimpleNamespace(all_failed=True, failure_message=lambda: "all sources failed")
        engine = SimpleNamespace(fetch_all_sources=AsyncMock(return_value=[]), last_fetch_report=report)
        with self.assertRaisesRegex(RuntimeError, "all sources failed"):
            asyncio.run(collect_morning(engine, morning_window(issue_date=date(2026, 11, 1))))

    def test_empty_collection_requires_source_verification(self):
        engine = SimpleNamespace(fetch_all_sources=AsyncMock(return_value=[]), last_fetch_report=None)
        with self.assertRaisesRegex(RuntimeError, "verify source availability"):
            asyncio.run(collect_morning(engine, morning_window(issue_date=date(2026, 11, 1))))


if __name__ == "__main__":
    unittest.main()


def test_collector_cli_uses_updated_native_adapter(monkeypatch, tmp_path):
    import json
    from src import morning

    window = morning_window(issue_date=date(2026, 11, 1))
    item = SimpleNamespace(
        id="rss:test", published_at=window.start + timedelta(hours=1),
        source_type=SimpleNamespace(value="rss"), title="Test", url="https://example.com/test",
        author=None, content="verified", profile="tech-news",
    )
    engine = SimpleNamespace(
        fetch_all_sources=AsyncMock(return_value=[item]), last_fetch_report=None,
        merge_cross_source_duplicates=lambda items: items,
    )
    monkeypatch.setattr("src.orchestrator.HorizonOrchestrator", lambda config, storage: engine)
    monkeypatch.setattr("sys.argv", ["morning", "--date", "2026-11-01", "--sources", "rss", "--output-dir", str(tmp_path)])
    morning.main()
    result = json.loads((tmp_path / "2026-11-01.json").read_text())
    assert result["subject"] == "11月1日早间甜点"
    assert result["item_count"] == 1
    engine.fetch_all_sources.assert_awaited_once_with(window.start)
