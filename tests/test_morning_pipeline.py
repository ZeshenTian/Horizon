import asyncio
from datetime import date, timedelta
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from rich.console import Console

from src.ai.enricher import EnrichmentBatchResult
from src.models import Config, ContentItem, SourceType, ClassificationResult, ProcessingResult, ContentAnalysis
from src.morning import morning_window, validate_morning_config
from src.orchestrator import HorizonOrchestrator
from src.storage.manager import StorageManager


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MINIMAX_API_KEY", "test-model-key")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "test-mail-password")
    root = Path(__file__).resolve().parents[1]
    config = Config.model_validate({
        "ai": {"provider": "minimax", "model": "MiniMax-M3.1-Flash", "api_key_env": "MINIMAX_API_KEY", "languages": ["zh"]},
        "sources": {},
        "processing": {"profiles_dir": str(root / "profiles")},
        "email": {"enabled": True, "imap_enabled": False, "smtp_server": "smtp.gmail.com", "imap_server": "imap.gmail.com", "email_address": "reader@gmail.com", "password_env": "GMAIL_APP_PASSWORD"},
    })
    storage = StorageManager(str(tmp_path / "data"))
    storage.add_subscriber("reader@gmail.com")
    result = HorizonOrchestrator(config, storage, console=Console(file=StringIO()))
    result.email_manager = Mock()
    window = morning_window(issue_date=date(2026, 11, 1))
    monkeypatch.setattr("src.orchestrator.morning_window", lambda: window)

    def item(key, published):
        return ContentItem(id=key, source_type=SourceType.RSS, title=key, url=f"https://example.com/{key}", published_at=published, profile="tech-news", processing=ProcessingResult(classification=ClassificationResult(profile="tech-news", method="source_override"), analysis=ContentAnalysis(score=8, reason="fact", summary="verified update")))

    result.fetch_all_sources = AsyncMock(return_value=[
        item("inside", window.start + timedelta(hours=12)),
        item("old", window.start - timedelta(seconds=1)),
        item("after-cutoff", window.end),
    ])
    result.analyze_items = AsyncMock(side_effect=lambda items: items)
    result.select_digest_items = AsyncMock(side_effect=lambda items: SimpleNamespace(items=items))
    result.enrich_items = AsyncMock(return_value=EnrichmentBatchResult(succeeded_ids=["inside"]))
    return result, window


def test_morning_pipeline_uses_exact_window_local_dates_and_skips_repeat(engine):
    result, window = engine
    asyncio.run(result.run(morning=True))
    result.fetch_all_sources.assert_awaited_once_with(window.start)
    analyzed = result.analyze_items.await_args.args[0]
    assert [item.id for item in analyzed] == ["inside"]
    call = result.email_manager.send_daily_summary.call_args
    body, subject, recipients = call.args
    assert subject == "11月1日早间甜点"
    assert "2026-10-31 08:00" in body and "2026-11-01 08:00" in body
    assert "10月31日 20:00" in body
    assert recipients == ["reader@gmail.com"]
    assert call.kwargs == {"raise_on_error": True}
    receipt = result.storage.data_dir / "morning/2026-11-01.sent.json"
    assert json.loads(receipt.read_text())["status"] == "sent"
    asyncio.run(result.run(morning=True))
    assert result.fetch_all_sources.await_count == 1
    assert result.email_manager.send_daily_summary.call_count == 1


def test_delivery_failure_does_not_write_success_receipt(engine):
    result, _ = engine
    result.email_manager.send_daily_summary.side_effect = RuntimeError("SMTP unavailable")
    with pytest.raises(RuntimeError, match="SMTP unavailable"):
        asyncio.run(result.run(morning=True))
    assert not (result.storage.data_dir / "morning/2026-11-01.sent.json").exists()


def test_pinned_issue_date_is_used_instead_of_later_clock(engine, monkeypatch):
    result, _ = engine
    monkeypatch.setattr("src.orchestrator.morning_window", morning_window)
    asyncio.run(result.run(morning=True, morning_date=date(2026, 11, 1)))
    assert result.email_manager.send_daily_summary.call_args.args[1] == "11月1日早间甜点"


def test_all_analysis_failures_do_not_send_false_empty_digest(engine):
    result, _ = engine
    for item in result.fetch_all_sources.return_value:
        item.processing.analysis.score = None
    with pytest.raises(RuntimeError, match="All AI analyses failed"):
        asyncio.run(result.run(morning=True))
    result.email_manager.send_daily_summary.assert_not_called()


def test_preflight_reports_missing_credential_name_without_value(engine, monkeypatch):
    result, _ = engine
    monkeypatch.setenv("MINIMAX_API_KEY", "xxx")
    with pytest.raises(ValueError, match="MINIMAX_API_KEY"):
        validate_morning_config(result.config, result.storage.load_subscribers())
