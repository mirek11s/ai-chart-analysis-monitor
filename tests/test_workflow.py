import json
from pathlib import Path

import pytest

from jarvis.analysis.service import analyze, compare, latest, load_report
from jarvis.cli import main, start_monitor
from jarvis.conversation.chat import Chat, parse_symbol, parse_timeframe
from jarvis.models import JarvisError
from jarvis.monitoring.store import Store
from jarvis.monitoring.worker import simulate


def test_complete_analysis_monitor_alert_stop(settings):
    report = analyze(settings, pair="BTC/USDC", interval="1m", simulation=True, limit=200)
    output = Path(report["output_dir"])
    for name in ("chart_original.png", "chart_annotated.png"):
        assert (output / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert "SIMULATION" in (output / "analysis.md").read_text(encoding="utf-8")
    assert load_report(output)["supports"]
    job = start_monitor(settings, report=report)["job"]
    assert job["level"] == report["supports"][0]["price"]
    result = simulate(settings, job["id"], [job["level"] * 1.05, job["level"], job["level"] * 0.99])
    assert result["events_created"] == 1
    store = Store(settings.db)
    assert len(store.list()) == 1
    assert len(list((settings.home / "data" / "events").glob("*.json"))) == 1
    store.stop(job["id"])
    assert store.list(active=True) == []


def test_candle_checksum_guards_artifact(settings):
    report = analyze(settings, simulation=True, limit=100)
    (Path(report["output_dir"]) / "candles.csv").write_text("corrupted", encoding="utf-8")
    with pytest.raises(JarvisError, match="checksum"):
        load_report(report["output_dir"])


def test_new_timeframe_reanalyzed_in_multiturn_chat(settings):
    chat = Chat(settings, simulation=True)
    chat.respond("Jarvis, analyze BTC on the daily.")
    restarted = Chat(settings, simulation=True)
    result = restarted.respond("Monitor the nearest one on the 1-minute chart.")
    job = result["monitor"]["job"]
    assert job["timeframe"] == "1m"
    source = load_report(job["provenance"]["analysis"])
    assert source["timeframe"] == "1m"
    assert job["level"] == source["supports"][0]["price"]
    assert restarted.respond("Show me all active monitors")["jobs"][0]["id"] == job["id"]
    assert restarted.respond("Stop monitoring BTC")["jobs"][0]["status"] == "STOPPED"


def test_chat_does_not_authorize_existing_browser(settings):
    with pytest.raises(JarvisError):
        Chat(settings).respond("Use my currently open TradingView chart")


@pytest.mark.parametrize(
    "text,expected",
    [("daily", "1d"), ("on the 4H", "4h"), ("1-minute chart", "1m"), ("30 minutes", "30m")],
)
def test_conversational_timeframe(text, expected):
    assert parse_timeframe(text) == expected


def test_asset_parsing_ignores_timeframe():
    assert parse_symbol("Monitor the nearest one on the 1-minute chart") is None
    assert parse_symbol("Analyze BTC/USDC on 4H") == "BTC/USDC"


def test_monitor_identity_mismatch_is_rejected(settings):
    report = analyze(settings, simulation=True, limit=100)
    with pytest.raises(JarvisError, match="differs"):
        start_monitor(settings, report=report, interval="1m")


def test_json_cli_and_error_exit(settings, capsys):
    assert main(["--home", str(settings.home), "monitor", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"] == []
    assert main(["--home", str(settings.home), "monitor", "start", "--level", "nan"]) == 2
    assert "finite" in json.loads(capsys.readouterr().err)["error"]


def test_compare_only_matches_same_market_and_mode(settings):
    analyze(settings, pair="BTC/USDC", interval="1m", simulation=True, limit=100)
    analyze(settings, pair="BTC/USDC", interval="1m", simulation=True, limit=120)
    result = compare(settings, pair="BTC/USDC", interval="1m")
    assert result["simulation"]
    assert latest(settings, pair="BTC/USDC")["candle_count"] == 120
