def reached(job: dict, price: float) -> bool:
    tolerance = job["level"] * job["tolerance_pct"] / 100
    return (
        price <= job["level"] + tolerance
        if job["side"] == "support"
        else price >= job["level"] - tolerance
    )


def recovered(job: dict, price: float) -> bool:
    # Hysteresis: move twice the tolerance beyond the level to re-arm.
    margin = job["level"] * job["tolerance_pct"] * 2 / 100
    return (
        price > job["level"] + margin if job["side"] == "support" else price < job["level"] - margin
    )
