import time
from dataclasses import replace
from urllib.error import HTTPError

import pytest

from jarvis.browser.session import BrowserSession, chart_url, identity_from_url, validate_cdp_url
from jarvis.market_data.feed import BinanceFeed, RateLimited, SimulationFeed, validate_candles
from jarvis.models import JarvisError


def test_url_roundtrip_and_timeframes():
    for interval in ("1m", "4h", "1d", "1w"):
        result = identity_from_url(chart_url("BTC/USDC", "binance", interval))
        assert result == {"symbol": "BTC/USDC", "exchange": "binance", "timeframe": interval}


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/chart/?symbol=BINANCE:BTCUSDC",
        "https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT.P",
        "https://www.tradingview.com/chart/abc/",
    ],
)
def test_ambiguous_or_wrong_browser_identity_rejected(url):
    with pytest.raises(JarvisError):
        identity_from_url(url)


def test_browser_requires_explicit_authorization(settings):
    with pytest.raises(JarvisError, match="authorization"):
        BrowserSession(settings, "existing")


@pytest.mark.parametrize(
    "url", ["http://remote.test:9222", "http://user:pass@localhost:9222", "file:///tmp/test"]
)
def test_cdp_loopback_only(url):
    with pytest.raises(JarvisError):
        validate_cdp_url(url)


def test_candle_gaps_and_duration_rejected(candles):
    validate_candles(candles, "1m")
    with pytest.raises(JarvisError):
        validate_candles(candles[:5] + candles[6:], "1m")
    with pytest.raises(JarvisError):
        validate_candles([replace(candles[0], close_time=candles[0].close_time + 1)], "1m")


def test_closed_candles_only_and_exchange_symbol_validation(monkeypatch):
    feed = BinanceFeed()
    step = 60_000
    current_start = int(time.time() * 1000) // step * step
    rows = [
        [
            current_start + (i - 20) * step,
            "100",
            "102",
            "99",
            "101",
            "10",
            current_start + (i - 19) * step - 1,
        ]
        for i in range(21)
    ]

    def get(route, **params):
        if route == "exchangeInfo":
            assert params["symbol"] == "BTCUSDC"
            return {
                "symbols": [
                    {
                        "baseAsset": "BTC",
                        "quoteAsset": "USDC",
                        "status": "TRADING",
                        "isSpotTradingAllowed": True,
                    }
                ]
            }
        if route == "time":
            return {"serverTime": current_start + 10_000}
        assert params["interval"] == "1m"
        return rows

    monkeypatch.setattr(feed, "_get", get)
    candles = feed.candles("BTC/USDC", "1m", 20)
    assert len(candles) == 20
    assert candles[-1].close_time == current_start - 1


def test_exchange_does_not_silently_substitute_pair(monkeypatch):
    feed = BinanceFeed()
    monkeypatch.setattr(
        feed,
        "_get",
        lambda *args, **kwargs: {
            "symbols": [
                {
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "isSpotTradingAllowed": True,
                }
            ]
        },
    )
    with pytest.raises(JarvisError):
        feed.candles("BTC/USDC", "1m", 20)


def test_rate_limit_blocks_followup_requests(monkeypatch):
    calls = []

    def limited(*args, **kwargs):
        calls.append(1)
        raise HTTPError(
            "https://data-api.binance.vision", 429, "rate limit", {"Retry-After": "60"}, None
        )

    monkeypatch.setattr("jarvis.market_data.feed.urlopen", limited)
    feed = BinanceFeed()
    with pytest.raises(RateLimited):
        feed._get("time")
    with pytest.raises(RateLimited):
        feed._get("time")
    assert len(calls) == 1


def test_synthetic_data_is_labelled():
    feed = SimulationFeed()
    candles = feed.candles("BTC/USDC", "1m", 200)
    assert feed.simulation
    assert feed.source.startswith("SIMULATION:")
    validate_candles(candles, "1m")


@pytest.mark.parametrize(
    "payload", [None, [], {"message": "maintenance"}, {"lastPrice": "bad", "closeTime": 1}]
)
def test_malformed_quote_is_actionable_error(monkeypatch, payload):
    feed = BinanceFeed()
    feed._pairs.add("BTC/USDC")
    monkeypatch.setattr(feed, "_get", lambda *args, **kwargs: payload)
    with pytest.raises(JarvisError, match="Malformed"):
        feed.quote("BTC/USDC")


def test_malformed_candle_payload_is_actionable_error(monkeypatch):
    feed = BinanceFeed()
    feed._pairs.add("BTC/USDC")
    monkeypatch.setattr(
        feed, "_get", lambda route, **kwargs: {"serverTime": 1} if route == "time" else [[1, 2]]
    )
    with pytest.raises(JarvisError, match="Malformed"):
        feed.candles("BTC/USDC", "1m", 20)


def test_browser_cleanup_tolerates_disconnection(settings):
    class Gone:
        def close(self):
            raise RuntimeError("disconnected")

        def stop(self):
            raise RuntimeError("disconnected")

    session = BrowserSession(settings, "owned")
    session.context = session.playwright = Gone()
    session.__exit__(None, None, None)
