from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol

from jarvis.analysis.service import write_json


class AlertSink(Protocol):
    """Implement send(event); use event['id'] as the idempotency key."""

    def send(self, event: dict) -> None: ...


class FileSink:
    def __init__(self, directory: Path):
        self.directory = directory

    def send(self, event: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{event['id']}.json"
        if not path.exists():
            write_json(path, event)


class TerminalSink:
    def send(self, event: dict) -> None:
        prefix = "SIMULATION " if event["simulation"] else ""
        print(
            f"{prefix}ALERT {event['symbol']} {event['side']} {event['level']:g}: "
            f"price={event['price']:g} at {event['observed_at']} event={event['id']}",
            file=sys.stderr,
            flush=True,
        )


def dispatch_pending(store, sinks: list[AlertSink]) -> int:
    delivered = 0
    for event in store.events(pending=True):
        try:
            for sink in sinks:
                sink.send(event)
            store.delivered(event["id"])
            delivered += 1
        except Exception as exc:
            store.delivered(event["id"], f"{type(exc).__name__}: {exc}")
    return delivered
