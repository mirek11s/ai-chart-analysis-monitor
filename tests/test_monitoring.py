import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from jarvis.alerts import FileSink, dispatch_pending
from jarvis.models import JarvisError, Quote
from jarvis.monitoring.conditions import reached, recovered
from jarvis.monitoring.store import Store
from jarvis.monitoring.worker import Worker, WorkerLock, simulate


def new_job(store, **kwargs):
    options = dict(
        pair="BTC/USDC",
        exchange="binance",
        interval="1m",
        level=100,
        tolerance_pct=1,
        cooldown_seconds=10,
    )
    options.update(kwargs)
    return store.create(**options)


def observe(store, job, price, now):
    prefix = "SIMULATION:" if job["simulation"] else "live test feed"
    return store.observe(job["id"], Quote(price, now, prefix), now=now)


@pytest.mark.parametrize(
    "side,price,hit",
    [
        ("support", 101, True),
        ("support", 101.01, False),
        ("support", 90, True),
        ("resistance", 99, True),
        ("resistance", 98.9, False),
        ("resistance", 110, True),
    ],
)
def test_thresholds(side, price, hit):
    assert reached({"side": side, "level": 100, "tolerance_pct": 1}, price) == hit


def test_hysteresis():
    job = {"side": "support", "level": 100, "tolerance_pct": 1}
    assert not recovered(job, 102)
    assert recovered(job, 102.01)


def test_restart_persistence_latch_cooldown_and_rearm(store):
    job = new_job(store)
    t = time.time()
    assert observe(store, job, 100, t)
    restarted = Store(store.path)
    assert observe(restarted, job, 99, t + 1) is None
    assert observe(restarted, job, 103, t + 2) is None
    assert observe(restarted, job, 100, t + 3) is None  # recovery occurred but cooldown remains
    assert observe(restarted, job, 100, t + 11)
    assert observe(restarted, job, 100, t + 1000) is None  # staying below does not repeat
    assert len(restarted.events()) == 2


def test_atomic_dedup_under_concurrent_observations(store):
    job = new_job(store)
    t = time.time()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: observe(Store(store.path), job, 100, t), range(8)))
    assert sum(result is not None for result in results) == 1
    assert len(store.events()) == 1


def test_stopped_jobs_and_out_of_order_quotes(store):
    job = new_job(store)
    t = time.time()
    observe(store, job, 105, t)
    assert observe(store, job, 99, t - 1) is None
    store.stop(job["id"])
    assert observe(store, job, 99, t + 1) is None
    assert store.events() == []


def test_stale_and_nonfinite_quotes_rejected(store):
    job = new_job(store)
    t = time.time()
    with pytest.raises(JarvisError):
        store.observe(job["id"], Quote(99, t - 121, "test"), now=t)
    with pytest.raises(JarvisError):
        observe(store, job, float("nan"), t)
    assert store.events() == []


def test_outbox_retries_and_file_idempotency(store, tmp_path):
    job = new_job(store)
    event = observe(store, job, 100, time.time())
    files = FileSink(tmp_path / "events")

    class FailingSink:
        def send(self, event):
            raise OSError("offline")

    assert dispatch_pending(store, [files, FailingSink()]) == 0
    assert len(store.events(pending=True)) == 1
    restarted = Store(store.path)
    assert dispatch_pending(restarted, [files]) == 1
    assert dispatch_pending(restarted, [files]) == 0
    assert [path.name for path in (tmp_path / "events").glob("*.json")] == [event["id"] + ".json"]


def test_simulation_is_separate_from_live(settings, store):
    live = new_job(store)
    with pytest.raises(JarvisError):
        simulate(settings, live["id"], [99])
    simulated = new_job(store, simulation=True)
    result = simulate(settings, simulated["id"], [105, 100, 99, 99])
    assert result["events_created"] == 1
    assert store.get(live["id"])["last_price"] is None
    assert result["events"][0]["simulation"]


def test_worker_price_poll_and_restart(settings, store):
    job = new_job(store)

    class Feed:
        def quote(self, pair):
            return Quote(100, time.time(), "live test feed")

    worker = Worker(settings, feed_factory=lambda _: Feed(), sinks=[])
    assert worker.tick()["triggered"] == 1
    assert Worker(settings, feed_factory=lambda _: Feed(), sinks=[]).tick()["triggered"] == 0
    assert not store.get(job["id"])["armed"]


def test_worker_failures_backoff_without_stopping_job(settings, store):
    job = new_job(store)

    class Feed:
        def quote(self, pair):
            raise JarvisError("API offline")

    result = Worker(settings, feed_factory=lambda _: Feed(), sinks=[]).tick()
    saved = store.get(job["id"])
    assert result["triggered"] == 0
    assert saved["failures"] == 1
    assert saved["next_check"] > time.time() + 30
    assert saved["status"] == "ACTIVE"


def test_worker_lock_releases_after_exit(tmp_path):
    path = tmp_path / "worker.lock"
    with WorkerLock(path):
        with pytest.raises(JarvisError):
            with WorkerLock(path):
                pass
    with WorkerLock(path):
        pass


@pytest.mark.parametrize(
    "options",
    [
        {"level": float("inf")},
        {"poll_seconds": 0},
        {"tolerance_pct": -1},
        {"cooldown_seconds": -1},
        {"browser_mode": "existing"},
    ],
)
def test_job_validation(store, options):
    with pytest.raises(JarvisError):
        new_job(store, **options)


def test_browser_failure_does_not_prevent_alert(settings, store, monkeypatch):
    job = new_job(store, browser_mode="owned")

    class Feed:
        def quote(self, pair):
            return Quote(100, time.time(), "live test feed")

    class Disconnected:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise JarvisError("browser disconnected")

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("jarvis.monitoring.worker.BrowserSession", Disconnected)
    assert Worker(settings, feed_factory=lambda _: Feed(), sinks=[]).tick()["triggered"] == 1
    assert store.get(job["id"])["browser_error"] == "browser disconnected"
