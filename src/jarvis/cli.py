from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jarvis.analysis.service import analyze, compare, latest, load_report
from jarvis.browser import BrowserSession
from jarvis.config import Settings
from jarvis.models import JarvisError, symbol, timeframe
from jarvis.monitoring.store import Store
from jarvis.monitoring.worker import Worker, WorkerLock, simulate, start_worker


def browser_args(parser, *, mode: bool = True):
    if mode:
        parser.add_argument("--browser", choices=["none", "owned", "existing"], default="none")
    parser.add_argument(
        "--allow-existing",
        action="store_true",
        help="Authorize this operation to reuse the CDP session.",
    )
    parser.add_argument("--cdp-url")
    parser.add_argument("--tab", type=int, help="Zero-based TradingView chart tab index.")
    parser.add_argument("--headed", action="store_true")


def analysis_args(parser):
    parser.add_argument("--symbol")
    parser.add_argument("--exchange")
    parser.add_argument("--timeframe")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--window", type=int, default=3)
    parser.add_argument("--cluster-pct", type=float, default=0.3)
    parser.add_argument("--min-touches", type=int, default=2)
    parser.add_argument("--simulate", action="store_true")
    browser_args(parser)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="jarvis", description="Local chart analysis and monitoring; no trading."
    )
    root.add_argument(
        "--home",
        type=Path,
        help="Jarvis project/data root; defaults to JARVIS_HOME or project root.",
    )
    root.add_argument(
        "--json",
        action="store_true",
        help="Structured output (already the default, except interactive chat).",
    )
    commands = root.add_subparsers(dest="command", required=True)
    analyze_parser = commands.add_parser(
        "analyze", help="Calculate levels and save chart, data, and analysis."
    )
    analysis_args(analyze_parser)
    analyze_parser.add_argument(
        "--monitor", action="store_true", help="Monitor the nearest detected support; start worker."
    )

    monitor = commands.add_parser("monitor").add_subparsers(dest="operation", required=True)
    start = monitor.add_parser("start")
    start.add_argument("--from-analysis", help="Analysis directory, levels.json, or latest.")
    start.add_argument("--symbol")
    start.add_argument("--exchange")
    start.add_argument("--timeframe")
    start.add_argument("--level", type=float)
    start.add_argument("--side", choices=["support", "resistance"], default="support")
    start.add_argument(
        "--level-index", type=int, default=1, help="One-based distance-ranked detected level."
    )
    start.add_argument("--tolerance-pct", type=float)
    start.add_argument("--poll-seconds", type=float)
    start.add_argument("--cooldown-seconds", type=float)
    start.add_argument("--simulate", action="store_true")
    start.add_argument(
        "--no-worker", action="store_true", help="Save job only; start worker separately."
    )
    start.add_argument("--browser-revisit", choices=["none", "owned", "existing"], default="none")
    start.add_argument("--revisit-seconds", type=float)
    browser_args(start, mode=False)
    monitor.add_parser("list").add_argument("--active", action="store_true")
    stop = monitor.add_parser("stop")
    stop.add_argument("job_id", nargs="?")
    stop.add_argument("--symbol")
    events = monitor.add_parser("events")
    events.add_argument("--job-id")
    events.add_argument("--pending", action="store_true")
    sim = monitor.add_parser("simulate")
    sim.add_argument("job_id")
    sim.add_argument("--prices", nargs="+", type=float, required=True)
    sim.add_argument("--step-seconds", type=float, default=30)

    worker = commands.add_parser("worker").add_subparsers(dest="operation", required=True)
    for name in ("start", "run", "stop", "status", "tick"):
        worker.add_parser(name)
    for name in ("latest", "compare"):
        lookup = commands.add_parser(name)
        lookup.add_argument("--symbol")
        lookup.add_argument("--timeframe")
    chat = commands.add_parser("chat")
    chat.add_argument("--message", help="One request using persistent conversation context.")
    chat.add_argument("--simulate", action="store_true")
    browser_args(chat)
    browser = commands.add_parser("browser").add_subparsers(dest="operation", required=True)
    inspect = browser.add_parser("inspect")
    browser_args(inspect, mode=False)
    opening = browser.add_parser(
        "open", help="Open an agent-owned visible browser until Enter is pressed."
    )
    opening.add_argument("--symbol", default="BTC/USDC")
    opening.add_argument("--exchange", default="binance")
    opening.add_argument("--timeframe", default="1d")
    commands.add_parser("doctor")
    return root


def start_monitor(
    settings,
    *,
    report: dict | None = None,
    pair: str | None = None,
    exchange: str | None = None,
    interval: str | None = None,
    level: float | None = None,
    side: str = "support",
    level_index: int = 1,
    simulation: bool = False,
    no_worker: bool = False,
    **options,
) -> dict:
    provenance = None
    if report:
        if level is not None:
            raise JarvisError("Use either --level or --from-analysis.")
        for provided, actual in (
            (pair, report["symbol"]),
            (exchange, report["exchange"]),
            (interval, report["timeframe"]),
        ):
            if provided is not None and provided != actual:
                raise JarvisError(
                    "Monitor identity differs from its analysis. Analyze the requested timeframe/market first."
                )
        pair, exchange, interval = report["symbol"], report["exchange"], report["timeframe"]
        levels = report["supports" if side == "support" else "resistances"]
        if not 1 <= level_index <= len(levels):
            raise JarvisError(f"No detected {side} at index {level_index}. No monitor created.")
        selected = levels[level_index - 1]
        level = selected["price"]
        simulation = simulation or report["simulation"]
        provenance = {
            "analysis": str(Path(report["output_dir"]) / "levels.json"),
            "observed_at": report["observed_at"],
            "candles_sha256": report["candles_sha256"],
            "algorithm": report["algorithm"],
            "selected_level": selected,
            "analysis_simulation": report["simulation"],
        }
    if level is None:
        raise JarvisError("Provide --from-analysis or --level.")
    for key, fallback in (
        ("poll_seconds", 30),
        ("cooldown_seconds", 300),
        ("tolerance_pct", 0.05),
        ("revisit_seconds", 300),
    ):
        if options.get(key) is None:
            options[key] = settings.defaults.get(key, fallback)
    options["cdp_url"] = options.get("cdp_url") or settings.cdp_url
    job = Store(settings.db).create(
        pair=pair or settings.defaults.get("symbol", "BTC/USDC"),
        exchange=exchange or settings.defaults.get("exchange", "binance"),
        interval=interval or settings.defaults.get("timeframe", "1m"),
        level=level,
        side=side,
        simulation=simulation,
        provenance=provenance,
        **options,
    )
    result = {
        "job": job,
        "worker": {
            "running": False,
            "reason": "simulation requires explicit observations"
            if simulation
            else "not started by request",
        },
    }
    if not simulation and not no_worker:
        try:
            result["worker"] = start_worker(settings)
        except JarvisError as exc:
            # Report persisted job even if process launch fails; avoid hidden duplicate creation on retry.
            result["worker"] = {
                "running": False,
                "error": str(exc),
                "recovery": "jarvis worker start",
            }
    return result


def execute(args, settings) -> dict | None:
    if args.command == "analyze":
        report = analyze(
            settings,
            pair=args.symbol,
            exchange=args.exchange,
            interval=args.timeframe,
            browser=args.browser,
            allow_existing=args.allow_existing,
            cdp_url=args.cdp_url,
            tab=args.tab,
            headed=args.headed,
            simulation=args.simulate,
            limit=args.limit,
            window=args.window,
            cluster_pct=args.cluster_pct,
            min_touches=args.min_touches,
        )
        if args.monitor:
            report["monitor"] = start_monitor(settings, report=report)
        return report
    if args.command == "monitor":
        store = Store(settings.db)
        if args.operation == "list":
            return {"jobs": store.list(active=args.active), "worker": store.worker_status()}
        if args.operation == "events":
            return {"events": store.events(pending=args.pending, job_id=args.job_id)}
        if args.operation == "stop":
            if bool(args.job_id) == bool(args.symbol):
                raise JarvisError("Specify one job ID or --symbol.")
            if args.job_id:
                return {"job": store.stop(args.job_id)}
            selected = [
                job for job in store.list(active=True) if job["symbol"] == symbol(args.symbol)
            ]
            return {"stopped": [store.stop(job["id"]) for job in selected]}
        if args.operation == "simulate":
            return simulate(settings, args.job_id, args.prices, args.step_seconds)
        report = None
        if args.from_analysis:
            report = (
                latest(settings)
                if args.from_analysis == "latest"
                else load_report(args.from_analysis)
            )
        return start_monitor(
            settings,
            report=report,
            pair=args.symbol,
            exchange=args.exchange,
            interval=args.timeframe,
            level=args.level,
            side=args.side,
            level_index=args.level_index,
            simulation=args.simulate,
            no_worker=args.no_worker,
            tolerance_pct=args.tolerance_pct,
            poll_seconds=args.poll_seconds,
            cooldown_seconds=args.cooldown_seconds,
            browser_mode=args.browser_revisit,
            allow_existing=args.allow_existing,
            cdp_url=args.cdp_url,
            tab=args.tab,
            revisit_seconds=args.revisit_seconds,
        )
    if args.command == "worker":
        store = Store(settings.db)
        if args.operation == "start":
            return start_worker(settings)
        if args.operation == "status":
            return store.worker_status()
        if args.operation == "stop":
            store.worker_update(stopping=True)
            return {
                "stop_requested": True,
                "note": "Worker exits after its current bounded operation.",
            }
        worker = Worker(settings)
        if args.operation == "tick":
            with WorkerLock(settings.home / "data" / "worker.lock"):
                return worker.tick()
        worker.run()
        return {"status": "stopped"}
    if args.command in {"latest", "compare"}:
        action = latest if args.command == "latest" else compare
        return action(
            settings,
            pair=symbol(args.symbol) if args.symbol else None,
            interval=timeframe(args.timeframe) if args.timeframe else None,
        )
    if args.command == "chat":
        from jarvis.conversation.chat import Chat

        chat = Chat(
            settings,
            simulation=args.simulate,
            browser=args.browser,
            allow_existing=args.allow_existing,
            cdp_url=args.cdp_url,
            tab=args.tab,
            headed=args.headed,
        )
        if args.message:
            return chat.respond(args.message)
        chat.run()
        return None
    if args.command == "browser":
        if args.operation == "inspect":
            with BrowserSession(
                settings,
                "existing",
                allow_existing=args.allow_existing,
                cdp_url=args.cdp_url,
                tab=args.tab,
            ) as browser:
                return browser.identify()
        with BrowserSession(settings, "owned", headed=True) as browser:
            browser.navigate(args.symbol, args.exchange, args.timeframe)
            print(
                "Agent-owned browser open. Sign in manually if needed. Press Enter to close.",
                file=sys.stderr,
            )
            input()
        return {"status": "closed", "profile": str(settings.home / "data" / "browser-profile")}
    if args.command == "doctor":
        import importlib.util

        return {
            "python": sys.version.split()[0],
            "home": str(settings.home),
            "matplotlib": importlib.util.find_spec("matplotlib") is not None,
            "playwright": importlib.util.find_spec("playwright") is not None,
            "worker": Store(settings.db).worker_status(),
            "exchange": "binance spot (public, no API key)",
            "browser_access": "existing sessions require --allow-existing",
        }
    raise JarvisError("Unknown command.")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(args, Settings.load(args.home))
        if result is not None:
            print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (JarvisError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
