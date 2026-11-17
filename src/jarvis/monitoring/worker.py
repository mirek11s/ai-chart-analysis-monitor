from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from jarvis.alerts import FileSink, TerminalSink, dispatch_pending
from jarvis.analysis.service import write_json
from jarvis.browser import BrowserSession
from jarvis.market_data import make_feed
from jarvis.market_data.feed import RateLimited
from jarvis.models import JarvisError, Quote, positive
from jarvis.monitoring.store import Store


class WorkerLock:
    """OS lock released on process exit, including crashes; one worker per home."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, 2)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise JarvisError("A worker already owns this Jarvis data directory.") from exc
        return self

    def __exit__(self, *_):
        if self.handle:
            self.handle.close()


class Worker:
    def __init__(self, settings, *, feed_factory=make_feed, sinks=None):
        self.settings = settings
        self.store = Store(settings.db)
        self.feed_factory = feed_factory
        self.feeds = {}
        self.sinks = (
            sinks
            if sinks is not None
            else [FileSink(settings.home / "data" / "events"), TerminalSink()]
        )
        self.stop_event = threading.Event()

    def tick(self) -> dict:
        checked = triggered = 0
        quotes = {}
        for job in self.store.due(time.time()):
            if self.stop_event.is_set():
                break
            if job["simulation"]:
                continue  # Synthetic observations require explicit monitor simulate.
            event = None
            try:
                key = (job["exchange"], job["symbol"])
                if key not in quotes:
                    if job["exchange"] not in self.feeds:
                        self.feeds[job["exchange"]] = self.feed_factory(job["exchange"])
                    quotes[key] = self.feeds[job["exchange"]].quote(job["symbol"])
                event = self.store.observe(job["id"], quotes[key])
                checked += 1
                triggered += int(event is not None)
            except Exception as exc:
                self.store.failure(
                    job["id"],
                    str(exc),
                    retry_after=exc.seconds if isinstance(exc, RateLimited) else 0,
                )
            # Price alerts are committed independently of browser availability.
            if job["browser_mode"] != "none" and (event or job["next_revisit"] <= time.time()):
                self.revisit(job, event)
        return {
            "checked": checked,
            "triggered": triggered,
            "delivered": dispatch_pending(self.store, self.sinks),
        }

    def revisit(self, job: dict, event: dict | None) -> None:
        if self.store.get(job["id"])["status"] != "ACTIVE":
            return
        directory = self.settings.home / "data" / "revisits" / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        name = event["id"] if event else f"{int(time.time())}_{uuid4().hex[:6]}"
        error = None
        metadata = {"job_id": job["id"], "event_id": event["id"] if event else None}
        try:
            with BrowserSession(
                self.settings,
                job["browser_mode"],
                allow_existing=job["allow_existing"],
                cdp_url=job["cdp_url"],
                tab=job["tab"],
            ) as browser:
                browser.navigate(job["symbol"], job["exchange"], job["timeframe"])
                metadata.update(
                    browser.capture(
                        directory / f"{name}.png", job["symbol"], job["exchange"], job["timeframe"]
                    )
                )
        except Exception as exc:
            error = str(exc)
            metadata.update({"status": "unavailable", "error": error})
        write_json(directory / f"{name}.json", metadata)
        self.store.revisited(job["id"], error)

    def run(self) -> None:
        with WorkerLock(self.settings.home / "data" / "worker.lock"):
            self.store.worker_update(os.getpid(), reset=True)

            def heartbeat():
                while not self.stop_event.wait(2):
                    try:
                        if self.store.worker_status()["stop_requested"]:
                            self.stop_event.set()
                        else:
                            self.store.worker_update(os.getpid())
                    except Exception as exc:
                        print(f"Heartbeat error: {exc}", file=sys.stderr, flush=True)

            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            previous = {}
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous[sig] = signal.signal(sig, lambda *_: self.stop_event.set())
            try:
                while not self.stop_event.is_set():
                    try:
                        self.tick()
                    except Exception as exc:
                        print(
                            f"Worker error: {type(exc).__name__}: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                    self.stop_event.wait(1)
            finally:
                self.stop_event.set()
                thread.join(timeout=5)
                self.store.worker_update(None)
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def start_worker(settings) -> dict:
    store = Store(settings.db)
    state = store.worker_status()
    if state["running"] and not state["stop_requested"]:
        return state
    log_path = settings.home / "data" / "worker.log"
    env = os.environ.copy()
    env["JARVIS_HOME"] = str(settings.home)
    env["PYTHONUNBUFFERED"] = "1"
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "jarvis", "--home", str(settings.home), "worker", "run"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=settings.home,
            env=env,
            **options,
        )
    for _ in range(50):
        time.sleep(0.1)
        state = store.worker_status()
        if state["running"] and not state["stop_requested"]:
            return {**state, "log": str(log_path)}
        if process.poll() is not None:
            break
    raise JarvisError(
        f"Worker did not start. Inspect {log_path}; run jarvis worker run to diagnose."
    )


def simulate(settings, job_id: str, prices: list[float], step_seconds: float = 30) -> dict:
    store = Store(settings.db)
    job = store.get(job_id)
    if not job["simulation"]:
        raise JarvisError(
            "Simulation cannot modify a live monitor. Create a separate monitor with --simulate."
        )
    if job["status"] != "ACTIVE":
        raise JarvisError("Cannot simulate a stopped monitor.")
    positive(step_seconds, "simulation step")
    if not prices:
        raise JarvisError("Supply at least one simulated price.")
    for price in prices:
        positive(price, "simulated price")
    now = max(time.time(), (job["last_observed"] or 0) + step_seconds)
    created = []
    for i, price in enumerate(prices):
        observed = now + i * step_seconds
        event = store.observe(
            job_id,
            Quote(price, observed, "SIMULATION: explicit price sequence; virtual clock"),
            now=observed,
        )
        if event:
            created.append(event)
    dispatch_pending(store, [FileSink(settings.home / "data" / "events"), TerminalSink()])
    return {
        "simulation": True,
        "observations": len(prices),
        "events_created": len(created),
        "events": created,
    }
