from __future__ import annotations

import json
import math
import random
import time
from http.client import HTTPException
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jarvis.models import INTERVALS, Candle, JarvisError, Quote, symbol, timeframe


class RateLimited(JarvisError):
    def __init__(self, seconds: float):
        self.seconds = max(1, seconds)
        super().__init__(f"Exchange rate limit: retry after {self.seconds:g} seconds.")


class MarketFeed(Protocol):
    source: str
    simulation: bool

    def candles(self, pair: str, interval: str, limit: int) -> list[Candle]: ...
    def quote(self, pair: str) -> Quote: ...


def validate_candles(candles: list[Candle], interval: str) -> None:
    if not candles:
        raise JarvisError("No completed candles available.")
    step = INTERVALS[timeframe(interval)] * 1000
    for index, candle in enumerate(candles):
        candle.validate()
        if candle.close_time != candle.timestamp + step - 1:
            raise JarvisError("Candle duration does not match requested timeframe.")
        if index and candle.timestamp - candles[index - 1].timestamp != step:
            raise JarvisError("Missing, duplicate, or unordered candles; analysis aborted.")


class BinanceFeed:
    """Public spot market data only. No credentials or signed/trading endpoints."""

    source = "https://data-api.binance.vision/api/v3"
    simulation = False

    def __init__(self, timeout: float = 10):
        self.timeout = timeout
        self._last_call = 0.0
        self._pairs: set[str] = set()
        self._blocked_until = 0.0

    def _get(self, route: str, **params):
        if time.monotonic() < self._blocked_until:
            raise RateLimited(self._blocked_until - time.monotonic())
        for attempt in range(3):
            time.sleep(max(0, 0.25 - (time.monotonic() - self._last_call)))
            self._last_call = time.monotonic()
            request = Request(
                f"{self.source}/{route}?{urlencode(params)}", headers={"User-Agent": "Jarvis/0.1"}
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except HTTPError as exc:
                if exc.code in (418, 429):
                    try:
                        delay = float(exc.headers.get("Retry-After", "60"))
                        if not math.isfinite(delay):
                            delay = 60
                    except ValueError:
                        delay = 60
                    self._blocked_until = time.monotonic() + max(1, delay)
                    raise RateLimited(delay) from exc
                if exc.code < 500 or attempt == 2:
                    raise JarvisError(
                        f"Binance HTTP {exc.code}; verify pair and regional availability. No alternate market substituted."
                    ) from exc
            except (URLError, TimeoutError, OSError, ValueError, HTTPException) as exc:
                if attempt == 2:
                    raise JarvisError(
                        f"Binance market data unavailable: {type(exc).__name__}."
                    ) from exc
            time.sleep(0.5 * (2**attempt))
        raise JarvisError("Market request failed.")

    def _validate_pair(self, pair: str) -> str:
        pair = symbol(pair)
        api_symbol = pair.replace("/", "")
        if pair not in self._pairs:
            payload = self._get("exchangeInfo", symbol=api_symbol)
            matches = payload.get("symbols", []) if isinstance(payload, dict) else []
            if (
                not isinstance(matches, list)
                or len(matches) != 1
                or not isinstance(matches[0], dict)
            ):
                raise JarvisError(f"Binance spot pair unavailable: {pair}")
            info = matches[0]
            if (
                f"{info.get('baseAsset')}/{info.get('quoteAsset')}" != pair
                or info.get("status") != "TRADING"
                or not info.get("isSpotTradingAllowed", False)
            ):
                raise JarvisError(f"Pair is not an active matching Binance spot market: {pair}")
            self._pairs.add(pair)
        return api_symbol

    def candles(self, pair: str, interval: str, limit: int = 500) -> list[Candle]:
        interval = timeframe(interval)
        if not 20 <= limit <= 999:
            raise JarvisError("Candle limit must be between 20 and 999.")
        api_symbol = self._validate_pair(pair)
        try:
            server_ms = int(self._get("time")["serverTime"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise JarvisError("Malformed exchange server timestamp.") from exc
        rows = self._get("klines", symbol=api_symbol, interval=interval, limit=limit + 1)
        if not isinstance(rows, list) or any(
            not isinstance(row, list) or len(row) < 7 for row in rows
        ):
            raise JarvisError("Malformed exchange OHLCV response.")
        try:
            candles = [
                Candle(int(r[0]), *(float(v) for v in r[1:6]), int(r[6]))
                for r in rows
                if int(r[6]) < server_ms
            ][-limit:]
        except (TypeError, ValueError, OverflowError) as exc:
            raise JarvisError("Malformed exchange candle values.") from exc
        validate_candles(candles, interval)
        if server_ms - candles[-1].close_time > 2 * INTERVALS[interval] * 1000:
            raise JarvisError("Exchange returned stale candles.")
        return candles

    def quote(self, pair: str) -> Quote:
        data = self._get("ticker/24hr", symbol=self._validate_pair(pair), type="MINI")
        try:
            quote = Quote(
                float(data["lastPrice"]),
                int(data["closeTime"]) / 1000,
                self.source + "/ticker/24hr",
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise JarvisError("Malformed exchange quote response.") from exc
        quote.validate()
        if abs(time.time() - quote.observed_at) > 120:
            raise JarvisError("Stale quote or incorrect system clock (>120s skew).")
        return quote


class SimulationFeed:
    """Deterministic synthetic prices. Never a fallback for a live request."""

    source = "SIMULATION: deterministic synthetic OHLCV (seed 42)"
    simulation = True

    def __init__(self):
        self.price = 100.0

    def candles(self, pair: str, interval: str, limit: int = 500) -> list[Candle]:
        symbol(pair)
        step = INTERVALS[timeframe(interval)] * 1000
        if not 20 <= limit <= 999:
            raise JarvisError("Candle limit must be between 20 and 999.")
        end = int(time.time() * 1000) // step * step
        rng = random.Random(42)
        candles = []
        previous = 100.0
        for i in range(limit):
            close = 100 + 5 * math.sin(i * 2 * math.pi / 28) + 1.5 * math.sin(i / 57)
            close += rng.uniform(-0.25, 0.25)
            low = min(previous, close) - rng.uniform(0.2, 0.8)
            high = max(previous, close) + rng.uniform(0.2, 0.8)
            start = end - (limit - i) * step
            candles.append(
                Candle(start, previous, high, low, close, rng.uniform(10, 100), start + step - 1)
            )
            previous = close
        self.price = previous
        validate_candles(candles, interval)
        return candles

    def quote(self, pair: str) -> Quote:
        symbol(pair)
        return Quote(self.price, time.time(), self.source)


def make_feed(exchange: str, simulation: bool = False) -> MarketFeed:
    if exchange.lower() != "binance":
        raise JarvisError(
            "MVP supports Binance spot only. Add a MarketFeed adapter for other exchanges."
        )
    return SimulationFeed() if simulation else BinanceFeed()
