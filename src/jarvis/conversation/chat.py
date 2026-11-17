from __future__ import annotations

import json
import re

from jarvis.analysis.service import analyze, compare, latest, load_report, write_json
from jarvis.models import JarvisError, symbol, timeframe
from jarvis.monitoring.store import Store

HELP = (
    "Try: 'Analyze BTC/USDC on the daily', 'Find BTC supports on 4H', "
    "'Monitor the nearest one on the 1-minute chart', 'Show all active monitors', "
    "'Stop monitoring BTC', 'Show the latest chart', 'What changed?'. "
    "Use Codex/Claude with SKILL.md for unrestricted natural language. Type quit to exit."
)


def parse_timeframe(message: str) -> str | None:
    match = re.search(
        r"\b(\d+)\s*[- ]?\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)\b", message, re.I
    )
    if match:
        unit = match.group(2).lower()[0]
        return timeframe(match.group(1) + unit)
    for word, interval in (("daily", "1d"), ("hourly", "1h"), ("weekly", "1w")):
        if re.search(rf"\b{word}\b", message, re.I):
            return interval
    return None


def parse_symbol(message: str) -> str | None:
    pair = re.search(r"\b([A-Z0-9]{2,15})\s*[/\-]\s*([A-Z]{2,8})\b", message, re.I)
    if pair and pair.group(1)[0].isalpha():
        return symbol(pair.group(1) + "/" + pair.group(2))
    asset = re.search(r"\b(BTC|ETH|SOL|XRP|BNB|ADA|DOGE|AVAX|LINK|DOT)\b", message, re.I)
    return symbol(asset.group(1) + "/USDC") if asset else None


class Chat:
    def __init__(
        self,
        settings,
        *,
        simulation=False,
        browser="none",
        allow_existing=False,
        cdp_url=None,
        tab=None,
        headed=False,
    ):
        self.settings, self.simulation = settings, simulation
        self.browser_options = {
            "browser": browser,
            "allow_existing": allow_existing,
            "cdp_url": cdp_url,
            "tab": tab,
            "headed": headed,
        }
        self.path = settings.home / "data" / ("chat-simulation.json" if simulation else "chat.json")
        try:
            self.context = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.context = {}

    def respond(self, message: str) -> dict:
        from jarvis.cli import start_monitor

        text = message.lower().strip()
        pair = (
            parse_symbol(message)
            or self.context.get("symbol")
            or self.settings.defaults.get("symbol", "BTC/USDC")
        )
        interval = (
            parse_timeframe(message)
            or self.context.get("timeframe")
            or self.settings.defaults.get("timeframe", "1d")
        )
        store = Store(self.settings.db)
        if re.search(r"\b(help|commands)\b", text):
            return {"message": HELP}
        if re.search(r"\b(stop|cancel|remove)\b", text):
            job_match = re.search(r"\b([a-f0-9]{12})\b", text)
            if job_match:
                jobs = [store.stop(job_match.group(1))]
            elif parse_symbol(message):
                jobs = [
                    store.stop(job["id"])
                    for job in store.list(active=True)
                    if job["symbol"] == pair and job["simulation"] == self.simulation
                ]
            else:
                raise JarvisError("Specify the monitor ID or asset to stop.")
            return {"message": f"Stopped {len(jobs)} monitor(s).", "jobs": jobs}
        if re.search(r"\b(list|show)\b", text) and re.search(
            r"\b(monitors|monitoring|jobs)\b", text
        ):
            return {
                "jobs": [
                    job
                    for job in store.list(active="active" in text)
                    if job["simulation"] == self.simulation
                ],
                "worker": store.worker_status(),
            }
        if "changed" in text or "compare" in text:
            return compare(self.settings, pair=pair, interval=interval)
        if "latest" in text and re.search(r"\b(chart|image|analysis)\b", text):
            report = latest(self.settings, pair=pair, interval=interval, simulation=self.simulation)
            return {
                "image": report["output_dir"] + "/chart_annotated.png",
                "analysis": report["output_dir"] + "/analysis.md",
            }
        monitor_request = bool(re.search(r"\b(monitor|watch|alert)\b", text))
        analysis_request = bool(
            re.search(r"\b(analyze|analyse|find|draw|inspect|open|use)\b", text)
        )
        if not (monitor_request or analysis_request):
            return {"message": "Request not recognized. " + HELP}
        options = dict(self.browser_options)
        existing = any(
            phrase in text for phrase in ("currently open", "existing", "this chart", "current tab")
        )
        if existing:
            if not options["allow_existing"]:
                raise JarvisError(
                    "Existing browser access is disabled. Restart chat with --allow-existing after enabling CDP."
                )
            options["browser"] = "existing"
        elif "tradingview" in text or re.search(r"\bopen\b", text):
            if options["browser"] == "none":
                options["browser"] = "owned"
        report = None
        if monitor_request and not analysis_request and self.context.get("analysis"):
            try:
                previous = load_report(self.context["analysis"])
                if (
                    previous["symbol"] == pair
                    and previous["timeframe"] == interval
                    and previous["simulation"] == self.simulation
                ):
                    report = previous
            except JarvisError:
                pass
        if report is None:
            # An explicit new timeframe always gets new analysis before level selection.
            report = analyze(
                self.settings,
                pair=None if existing and not parse_symbol(message) else pair,
                interval=parse_timeframe(message) if existing else interval,
                simulation=self.simulation,
                **options,
            )
        self.context.update(
            {
                "symbol": report["symbol"],
                "timeframe": report["timeframe"],
                "analysis": report["output_dir"],
            }
        )
        write_json(self.path, self.context)
        result = {
            "message": f"Found {len(report['supports'])} support zones and {len(report['resistances'])} resistance zones.",
            "image": report["output_dir"] + "/chart_annotated.png",
            "analysis": report["output_dir"] + "/analysis.md",
        }
        if monitor_request:
            side = "resistance" if "resistance" in text else "support"
            result["monitor"] = start_monitor(self.settings, report=report, side=side)
            job = result["monitor"]["job"]
            result["message"] = (
                f"Monitor {job['id']}: {job['symbol']} {job['timeframe']} {side} {job['level']:g}."
            )
            if not job["simulation"] and not result["monitor"]["worker"]["running"]:
                result["message"] += " Worker is not running; start it before expecting alerts."
        return result

    def run(self, voice=None):
        print("Jarvis local chat. " + HELP)
        while True:
            try:
                message = voice.listen() if voice else input("You> ")
                if message.strip().lower() in {"quit", "exit"}:
                    return
                response = self.respond(message)
                print("Jarvis> " + json.dumps(response, indent=2))
                if voice:
                    voice.speak(response.get("message", json.dumps(response)))
            except (EOFError, KeyboardInterrupt):
                print()
                return
            except JarvisError as exc:
                print(f"Jarvis> {exc}")
