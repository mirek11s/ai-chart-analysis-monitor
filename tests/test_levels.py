from dataclasses import replace

import pytest

from jarvis.analysis.levels import detect_levels
from jarvis.models import JarvisError


def test_repeated_support_has_exact_source_evidence(candles):
    result = detect_levels(candles, 104, window=2, cluster_pct=0.3)
    support = result["supports"][0]
    assert support["price"] == pytest.approx(100.1)
    assert support["touches"] == 3
    assert support["pivot_indices"] == [2, 8, 14]
    assert support["pivot_prices"] == [candles[i].low for i in [2, 8, 14]]
    assert 20 not in support["pivot_indices"]  # no future confirmation at the right edge


def test_single_touch_and_flat_data_do_not_invent_support(candles):
    assert detect_levels(candles[:7], 104, window=2)["supports"] == []
    flat = [replace(c, open=100, high=101, low=99, close=100) for c in candles]
    result = detect_levels(flat, 100, window=2)
    assert result["supports"] == result["resistances"] == []


def test_levels_are_on_correct_side_of_price(candles):
    result = detect_levels(candles, 99, window=2)
    assert result["supports"] == []
    assert all(level["price"] >= 99 for level in result["resistances"])


def test_cluster_width_does_not_chain(candles):
    changed = list(candles)
    changed[8] = replace(changed[8], low=100.29)
    changed[14] = replace(changed[14], low=100.58)
    support = detect_levels(changed, 104, window=2, cluster_pct=0.3)["supports"][0]
    assert support["pivot_indices"] == [2, 8]
    assert support["touches"] == 2


def test_detection_is_reproducible(candles):
    assert detect_levels(candles, 104) == detect_levels(candles, 104)


@pytest.mark.parametrize(
    "options", [{"window": 0}, {"min_touches": 1}, {"cluster_pct": float("nan")}, {"max_levels": 0}]
)
def test_invalid_parameters(candles, options):
    with pytest.raises(JarvisError):
        detect_levels(candles, 104, **options)
