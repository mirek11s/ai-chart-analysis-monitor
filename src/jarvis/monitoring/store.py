from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from jarvis.browser.session import validate_cdp_url
from jarvis.models import JarvisError, Quote, positive, symbol, timeframe, utc_iso
from jarvis.monitoring.conditions import reached, recovered

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, config TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',
 created_at REAL NOT NULL, next_check REAL NOT NULL, next_revisit REAL NOT NULL,
 armed INTEGER NOT NULL DEFAULT 1, last_alert REAL, last_price REAL, last_observed REAL,
 failures INTEGER NOT NULL DEFAULT 0, last_error TEXT, browser_error TEXT
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), created_at REAL NOT NULL,
 payload TEXT NOT NULL, delivered_at REAL, delivery_error TEXT
);
CREATE INDEX IF NOT EXISTS due_jobs ON jobs(status, next_check);
CREATE TABLE IF NOT EXISTS worker (
 singleton INTEGER PRIMARY KEY CHECK(singleton = 1), pid INTEGER,
 heartbeat REAL, stop_requested INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO worker(singleton) VALUES(1);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _job(row) -> dict:
        result = dict(row)
        result.update(json.loads(result.pop("config")))
        result["armed"] = bool(result["armed"])
        return result

    def create(
        self,
        *,
        pair: str,
        exchange: str,
        interval: str,
        level: float,
        side: str = "support",
        tolerance_pct: float = 0.05,
        poll_seconds: float = 30,
        cooldown_seconds: float = 300,
        simulation: bool = False,
        provenance: dict | None = None,
        browser_mode: str = "none",
        allow_existing: bool = False,
        cdp_url: str = "http://127.0.0.1:9222",
        tab: int | None = None,
        revisit_seconds: float = 300,
    ) -> dict:
        pair, interval = symbol(pair), timeframe(interval)
        level = positive(level, "level")
        positive(tolerance_pct, "tolerance percentage", zero=True)
        positive(cooldown_seconds, "cooldown", zero=True)
        positive(poll_seconds, "poll interval")
        positive(revisit_seconds, "revisit interval")
        if poll_seconds < (1 if simulation else 5) or tolerance_pct > 10:
            raise JarvisError(
                "Live polling minimum is 5s (simulation 1s); tolerance must be <= 10%."
            )
        if exchange != "binance" or side not in {"support", "resistance"}:
            raise JarvisError("Use Binance spot and side support or resistance.")
        if browser_mode not in {"none", "owned", "existing"}:
            raise JarvisError("Invalid browser revisit mode.")
        if simulation and browser_mode != "none":
            raise JarvisError("Simulation jobs cannot revisit live charts.")
        if browser_mode == "existing":
            if not allow_existing:
                raise JarvisError("Persistent browser revisits require explicit --allow-existing.")
            validate_cdp_url(cdp_url)
        if browser_mode != "none" and revisit_seconds < 30:
            raise JarvisError("Browser revisits must be at least 30s apart.")
        config = {
            "symbol": pair,
            "exchange": exchange,
            "timeframe": interval,
            "level": level,
            "side": side,
            "tolerance_pct": tolerance_pct,
            "poll_seconds": poll_seconds,
            "cooldown_seconds": cooldown_seconds,
            "simulation": simulation,
            "provenance": provenance,
            "browser_mode": browser_mode,
            "allow_existing": allow_existing,
            "cdp_url": cdp_url,
            "tab": tab,
            "revisit_seconds": revisit_seconds,
        }
        job_id, now = uuid4().hex[:12], time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,config,created_at,next_check,next_revisit) VALUES(?,?,?,?,?)",
                (job_id, json.dumps(config, allow_nan=False), now, now, now),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise JarvisError(f"Unknown monitor: {job_id}")
            return self._job(row)

    def list(self, active: bool = False) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM jobs"
                + (" WHERE status='ACTIVE'" if active else "")
                + " ORDER BY created_at DESC"
            ).fetchall()
            return [self._job(row) for row in rows]

    def stop(self, job_id: str) -> dict:
        self.get(job_id)
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='STOPPED' WHERE id=?", (job_id,))
        return self.get(job_id)

    def due(self, now: float) -> list[dict]:
        with self.connect() as db:
            return [
                self._job(row)
                for row in db.execute(
                    "SELECT * FROM jobs WHERE status='ACTIVE' AND next_check<=? ORDER BY next_check",
                    (now,),
                )
            ]

    def observe(self, job_id: str, quote: Quote, now: float | None = None) -> dict | None:
        quote.validate()
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise JarvisError(f"Unknown monitor: {job_id}")
            job = self._job(row)
            if job["status"] != "ACTIVE":
                return None
            if abs(now - quote.observed_at) > 120:
                raise JarvisError("Stale or future quote rejected.")
            if job["simulation"] != quote.source.startswith("SIMULATION:"):
                raise JarvisError("Quote mode does not match monitor mode.")
            if job["last_observed"] is not None and quote.observed_at <= job["last_observed"]:
                db.execute(
                    "UPDATE jobs SET next_check=? WHERE id=?", (now + job["poll_seconds"], job_id)
                )
                return None
            armed, last_alert, event = job["armed"], job["last_alert"], None
            if not armed and recovered(job, quote.price):
                armed = True
            if (
                armed
                and reached(job, quote.price)
                and (last_alert is None or now - last_alert >= job["cooldown_seconds"])
            ):
                event_id = uuid4().hex
                event = {
                    "id": event_id,
                    "job_id": job_id,
                    "created_at": utc_iso(now),
                    "observed_at": utc_iso(quote.observed_at),
                    "symbol": job["symbol"],
                    "exchange": job["exchange"],
                    "timeframe": job["timeframe"],
                    "level": job["level"],
                    "price": quote.price,
                    "side": job["side"],
                    "tolerance_pct": job["tolerance_pct"],
                    "simulation": job["simulation"],
                    "source": quote.source,
                    "condition": "price <= level + tolerance"
                    if job["side"] == "support"
                    else "price >= level - tolerance",
                }
                db.execute(
                    "INSERT INTO events(id,job_id,created_at,payload) VALUES(?,?,?,?)",
                    (event_id, job_id, now, json.dumps(event, allow_nan=False)),
                )
                armed, last_alert = False, now
            db.execute(
                """UPDATE jobs SET armed=?,last_alert=?,last_price=?,last_observed=?,
                          next_check=?,failures=0,last_error=NULL WHERE id=?""",
                (
                    armed,
                    last_alert,
                    quote.price,
                    quote.observed_at,
                    now + job["poll_seconds"],
                    job_id,
                ),
            )
            return event

    def failure(self, job_id: str, error: str, *, retry_after: float = 0) -> None:
        job = self.get(job_id)
        delay = max(retry_after, min(3600, job["poll_seconds"] * 2 ** min(job["failures"] + 1, 7)))
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET failures=failures+1,last_error=?,next_check=? WHERE id=?",
                (error, time.time() + delay, job_id),
            )

    def revisited(self, job_id: str, error: str | None) -> None:
        job = self.get(job_id)
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET next_revisit=?,browser_error=? WHERE id=?",
                (time.time() + job["revisit_seconds"], error, job_id),
            )

    def events(self, *, pending: bool = False, job_id: str | None = None) -> list[dict]:
        clauses, params = [], []
        if pending:
            clauses.append("delivered_at IS NULL")
        if job_id:
            clauses.append("job_id=?")
            params.append(job_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events"
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY created_at",
                params,
            )
            return [
                {
                    **json.loads(row["payload"]),
                    "delivered_at": row["delivered_at"],
                    "delivery_error": row["delivery_error"],
                }
                for row in rows
            ]

    def delivered(self, event_id: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE events SET delivered_at=?,delivery_error=? WHERE id=?",
                (None if error else time.time(), error, event_id),
            )

    def worker_update(self, pid: int | None = None, *, stopping: bool = False, reset: bool = False):
        with self.connect() as db:
            if stopping:
                db.execute("UPDATE worker SET stop_requested=1 WHERE singleton=1")
            else:
                db.execute(
                    "UPDATE worker SET pid=?,heartbeat=?"
                    + (",stop_requested=0" if reset else "")
                    + " WHERE singleton=1",
                    (pid, time.time()),
                )

    def worker_status(self) -> dict:
        with self.connect() as db:
            state = dict(db.execute("SELECT * FROM worker WHERE singleton=1").fetchone())
            state["running"] = bool(
                state["pid"] and state["heartbeat"] and time.time() - state["heartbeat"] < 90
            )
            return state
