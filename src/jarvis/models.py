from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

INTERVALS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}


class JarvisError(Exception):
    """An actionable user-facing error."""


def utc_iso(epoch: float | None = None) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        if epoch is not None
        else datetime.now(timezone.utc).isoformat()
    )


def timeframe(value: str) -> str:
    if value == "1M":
        raise JarvisError("Monthly candles are not supported; use 1m for one minute.")
    normalized = value.lower()
    if normalized not in INTERVALS:
        raise JarvisError(f"Unsupported timeframe {value!r}. Choose: {', '.join(INTERVALS)}")
    return normalized


def symbol(value: str) -> str:
    value = value.strip().upper().replace("-", "/")
    if not re.fullmatch(r"[A-Z0-9]{2,20}/[A-Z0-9]{2,10}", value):
        raise JarvisError("Use an explicit spot pair, for example BTC/USDC.")
    return value


def positive(value: float, name: str, *, zero: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or (value < 0 if zero else value <= 0):
        raise JarvisError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}.")
    return value


@dataclass(frozen=True)
class Candle:
    timestamp: int  # UTC milliseconds, opening time
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int  # UTC milliseconds, inclusive

    def validate(self) -> None:
        for name in ("open", "high", "low", "close"):
            positive(getattr(self, name), name)
        positive(self.volume, "volume", zero=True)
        if self.timestamp < 0 or self.close_time < self.timestamp:
            raise JarvisError("Invalid candle timestamp.")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise JarvisError("Invalid OHLC ordering.")


@dataclass(frozen=True)
class Quote:
    price: float
    observed_at: float
    source: str

    def validate(self) -> None:
        positive(self.price, "price")
        positive(self.observed_at, "quote timestamp")
