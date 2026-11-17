import pytest

from jarvis.config import Settings
from jarvis.models import Candle
from jarvis.monitoring.store import Store


@pytest.fixture
def settings(tmp_path):
    return Settings.load(tmp_path)


@pytest.fixture
def store(settings):
    return Store(settings.db)


@pytest.fixture
def candles():
    lows = [
        104,
        103,
        100,
        103,
        104,
        105,
        104,
        103,
        100.1,
        103,
        104,
        105,
        104,
        103,
        100.2,
        103,
        104,
        105,
        104,
        103,
        99,
    ]
    start = 1_700_000_000_000
    return [
        Candle(start + i * 60000, low + 1, low + 2, low, low + 1.5, 10, start + (i + 1) * 60000 - 1)
        for i, low in enumerate(lows)
    ]
