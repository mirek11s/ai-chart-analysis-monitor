from __future__ import annotations

from statistics import median

from jarvis.models import Candle, JarvisError, positive, utc_iso

ALGORITHM = "confirmed-pivot-clusters-v1"


def detect_levels(
    candles: list[Candle],
    current_price: float,
    *,
    window: int = 3,
    cluster_pct: float = 0.3,
    min_touches: int = 2,
    max_levels: int = 5,
) -> dict:
    """Strict local extrema; bounded-width clusters; separated, confirmed pivots.

    Cluster width <= cluster_pct percent of its smallest price. Every touch is
    a pivot separated by > window bars. The median pivot price is the level.
    Unconfirmed right-edge pivots and single touches never become levels.
    """
    positive(current_price, "current price")
    positive(cluster_pct, "cluster percentage")
    if window < 1 or min_touches < 2 or max_levels < 1 or cluster_pct > 10:
        raise JarvisError("Require window >= 1, touches >= 2, levels >= 1, cluster_pct <= 10.")
    if len(candles) < 2 * window + 1:
        raise JarvisError("Too few completed candles for this pivot window.")
    for candle in candles:
        candle.validate()
    result = {
        "supports": [],
        "resistances": [],
        "algorithm": ALGORITHM,
        "parameters": {
            "window": window,
            "cluster_pct": cluster_pct,
            "min_touches": min_touches,
            "max_levels": max_levels,
        },
    }
    for side, field in (("supports", "low"), ("resistances", "high")):
        values = [getattr(c, field) for c in candles]
        pivots = []
        for i in range(window, len(candles) - window):
            neighbors = values[i - window : i] + values[i + 1 : i + window + 1]
            extreme = (
                values[i] < min(neighbors) if side == "supports" else values[i] > max(neighbors)
            )
            if extreme:
                pivots.append((values[i], i))
        clusters: list[list[tuple[float, int]]] = []
        for price, index in sorted(pivots):
            if not clusters or price - clusters[-1][0][0] > clusters[-1][0][0] * cluster_pct / 100:
                clusters.append([])
            clusters[-1].append((price, index))
        levels = []
        for cluster in clusters:
            touches = []
            for price, index in sorted(cluster, key=lambda item: item[1]):
                if not touches or index - touches[-1][1] > window:
                    touches.append((price, index))
            if len(touches) < min_touches:
                continue
            price = median(p for p, _ in touches)
            if (side == "supports" and price > current_price) or (
                side == "resistances" and price < current_price
            ):
                continue
            levels.append(
                {
                    "price": price,
                    "zone_low": min(p for p, _ in touches),
                    "zone_high": max(p for p, _ in touches),
                    "touches": len(touches),
                    "pivot_indices": [i for _, i in touches],
                    "pivot_prices": [p for p, _ in touches],
                    "pivot_times": [utc_iso(candles[i].timestamp / 1000) for _, i in touches],
                    "last_touch": utc_iso(candles[touches[-1][1]].timestamp / 1000),
                    "distance_pct": abs(price - current_price) / current_price * 100,
                }
            )
        # Include the nearest validated level, then the strongest remaining zones.
        nearest = sorted(levels, key=lambda level: level["distance_pct"])
        chosen = nearest[:1]
        chosen += sorted(nearest[1:], key=lambda level: (-level["touches"], level["distance_pct"]))[
            : max_levels - 1
        ]
        result[side] = sorted(chosen, key=lambda level: level["distance_pct"])
    return result
