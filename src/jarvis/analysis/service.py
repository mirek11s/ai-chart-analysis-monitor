from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from jarvis.analysis.levels import detect_levels
from jarvis.browser import BrowserSession
from jarvis.charts.render import render_chart
from jarvis.market_data import make_feed
from jarvis.models import JarvisError, symbol, timeframe, utc_iso


def write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_report(path: str | Path) -> dict:
    location = Path(path).expanduser().resolve()
    if location.is_dir():
        location /= "levels.json"
    try:
        report = json.loads(location.read_text(encoding="utf-8"))
        if report["schema_version"] != 1:
            raise JarvisError("Unsupported analysis schema.")
        candles_file = location.parent / "candles.csv"
        if hashlib.sha256(candles_file.read_bytes()).hexdigest() != report["candles_sha256"]:
            raise JarvisError("Saved OHLCV checksum mismatch; regenerate analysis.")
        report["output_dir"] = str(location.parent)
        return report
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise JarvisError(f"Cannot load a complete analysis from {location}.") from exc


def reports(
    settings,
    *,
    pair: str | None = None,
    interval: str | None = None,
    simulation: bool | None = None,
) -> list[dict]:
    found = []
    for path in (settings.home / "outputs").glob("*/levels.json"):
        try:
            report = load_report(path)
            if (
                (pair is None or report["symbol"] == pair)
                and (interval is None or report["timeframe"] == interval)
                and (simulation is None or report["simulation"] == simulation)
            ):
                found.append(report)
        except JarvisError:
            continue
    return sorted(found, key=lambda report: report["created_at"], reverse=True)


def latest(settings, **filters) -> dict:
    found = reports(settings, **filters)
    if not found:
        raise JarvisError("No matching analysis. Run jarvis analyze first.")
    return found[0]


def compare(settings, *, pair: str | None = None, interval: str | None = None) -> dict:
    newest = latest(settings, pair=pair, interval=interval)
    found = reports(
        settings,
        pair=newest["symbol"],
        interval=newest["timeframe"],
        simulation=newest["simulation"],
    )
    if len(found) < 2:
        raise JarvisError("Two analyses of the same symbol, timeframe, and data mode are required.")
    current, previous = found[:2]
    return {
        "symbol": current["symbol"],
        "timeframe": current["timeframe"],
        "simulation": current["simulation"],
        "previous": previous["created_at"],
        "current": current["created_at"],
        "price_change_pct": (current["current_price"] / previous["current_price"] - 1) * 100,
        "previous_supports": [level["price"] for level in previous["supports"]],
        "current_supports": [level["price"] for level in current["supports"]],
        "previous_resistances": [level["price"] for level in previous["resistances"]],
        "current_resistances": [level["price"] for level in current["resistances"]],
    }


def analyze(
    settings,
    *,
    pair: str | None = None,
    exchange: str | None = None,
    interval: str | None = None,
    browser: str = "none",
    allow_existing: bool = False,
    cdp_url: str | None = None,
    tab: int | None = None,
    headed: bool = False,
    simulation: bool = False,
    limit: int = 500,
    window: int = 3,
    cluster_pct: float = 0.3,
    min_touches: int = 2,
    feed=None,
) -> dict:
    browser_info = {"status": "not_requested", "mode": browser}
    browser_session = None
    if simulation and browser != "none":
        raise JarvisError("Simulation cannot use a live browser chart. Use --browser none.")
    if browser == "existing" and not allow_existing:
        raise JarvisError("Existing-session access requires --allow-existing.")
    try:
        if browser != "none":
            try:
                browser_session = BrowserSession(
                    settings,
                    browser,
                    allow_existing=allow_existing,
                    cdp_url=cdp_url,
                    tab=tab,
                    headed=headed,
                ).__enter__()
            except JarvisError as exc:
                if browser == "existing" and pair is None:
                    raise JarvisError(f"Cannot identify existing chart: {exc}") from exc
                browser_info = {"status": "unavailable", "mode": browser, "error": str(exc)}
        if browser_session and browser == "existing" and pair is None:
            identity = browser_session.identify()
            pair = identity["symbol"]
            if exchange and exchange.lower() != identity["exchange"]:
                raise JarvisError("Requested exchange disagrees with the existing chart.")
            exchange = identity["exchange"]
            interval = interval or identity["timeframe"]
            if interval is None:
                raise JarvisError("Existing chart timeframe is ambiguous; supply --timeframe.")
        pair = symbol(pair or settings.defaults.get("symbol", "BTC/USDC"))
        exchange = (exchange or settings.defaults.get("exchange", "binance")).lower()
        interval = timeframe(interval or settings.defaults.get("timeframe", "1d"))
        market = feed or make_feed(exchange, simulation)
        if bool(market.simulation) != simulation:
            raise JarvisError("Feed mode does not match requested simulation mode.")
        candles = market.candles(pair, interval, limit)
        quote = market.quote(pair)
        quote.validate()
        detected = detect_levels(
            candles, quote.price, window=window, cluster_pct=cluster_pct, min_touches=min_touches
        )
        created = datetime.now(timezone.utc)
        folder = f"{created:%Y-%m-%d_%H%M%S}_{pair.replace('/', '-')}_{interval}_{uuid4().hex[:6]}"
        output = settings.home / "outputs" / folder
        output.mkdir(parents=True)
        candles_file = output / "candles.csv"
        with candles_file.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(asdict(candles[0])))
            writer.writeheader()
            writer.writerows(asdict(candle) for candle in candles)
        report = {
            "schema_version": 1,
            "symbol": pair,
            "exchange": exchange,
            "timeframe": interval,
            "created_at": created.isoformat(),
            "observed_at": utc_iso(quote.observed_at),
            "current_price": quote.price,
            "source": market.source,
            "quote_source": quote.source,
            "simulation": simulation,
            "candle_count": len(candles),
            "first_candle": utc_iso(candles[0].timestamp / 1000),
            "last_closed_candle": utc_iso(candles[-1].close_time / 1000),
            "candles_sha256": hashlib.sha256(candles_file.read_bytes()).hexdigest(),
            "output_dir": str(output),
            "annotation": "OHLCV-rendered; linear price axis; exact price coordinates",
            **detected,
        }
        if browser_session:
            try:
                browser_session.navigate(pair, exchange, interval)
                browser_info = browser_session.capture(
                    output / "chart_original.png", pair, exchange, interval
                )
            except Exception as exc:
                browser_info = {"status": "unavailable", "mode": browser, "error": str(exc)}
        report["browser"] = browser_info
        if browser_info["status"] != "captured":
            render_chart(candles, report, output / "chart_original.png", annotated=False)
            report["original_image_kind"] = "unannotated OHLCV rendering"
        else:
            report["original_image_kind"] = (
                "TradingView browser screenshot; see browser verification metadata"
            )
        render_chart(candles, report, output / "chart_annotated.png", annotated=True)
        (output / "analysis.md").write_text(markdown_report(report), encoding="utf-8")
        # levels.json is the commit marker: readers ignore incomplete output directories.
        write_json(output / "levels.json", report)
        return report
    finally:
        if browser_session:
            browser_session.__exit__(None, None, None)


def markdown_report(report: dict) -> str:
    lines = [
        f"# {'SIMULATION — ' if report['simulation'] else ''}{report['symbol']} · {report['exchange']} spot · {report['timeframe']}",
        "",
        f"Observed: {report['observed_at']} (UTC). Price: **{report['current_price']:,.8g}**.",
        "",
        "## Supports",
        "",
    ]
    for i, level in enumerate(report["supports"], 1):
        lines.append(
            f"- S{i}: **{level['price']:,.8g}**; zone {level['zone_low']:,.8g}–{level['zone_high']:,.8g}; "
            f"{level['touches']} separated pivot lows; {level['distance_pct']:.2f}% below price; last touch {level['last_touch']}."
        )
    if not report["supports"]:
        lines.append("No support met the minimum repeated-touch rule in this sample.")
    lines += ["", "## Resistances", ""]
    for i, level in enumerate(report["resistances"], 1):
        lines.append(
            f"- R{i}: **{level['price']:,.8g}**; {level['touches']} separated pivot highs; {level['distance_pct']:.2f}% above price."
        )
    if not report["resistances"]:
        lines.append("No resistance met the minimum repeated-touch rule in this sample.")
    interpretation = (
        "Nearest support is a historical reaction zone; a revisit can hold or break."
        if report["supports"]
        else "This sample provides no validated support to monitor; expand history or choose another timeframe."
    )
    lines += [
        "",
        interpretation,
        "",
        "## Method and provenance",
        "",
        f"- {report['algorithm']}: {json.dumps(report['parameters'], sort_keys=True)}.",
        "- Strict confirmed pivots; bounded price clusters; median pivot price; minimum two independent touches.",
        f"- Source: {report['source']}. Quote: {report['quote_source']}.",
        f"- {report['candle_count']} completed candles, {report['first_candle']} through {report['last_closed_candle']}.",
        f"- Raw data: `candles.csv`; SHA-256 `{report['candles_sha256']}`. Pivot indices/times/prices: `levels.json`.",
        f"- Original image: {report['original_image_kind']}.",
        "- Annotated image: independently rendered OHLCV, linear scale. No estimated screenshot coordinates.",
        "- Finite history, delayed pivot confirmation, and polling gaps limit coverage. No prediction or trade execution.",
    ]
    if report["browser"]["status"] == "unavailable":
        lines.append(f"- Browser fallback: {report['browser']['error']}")
    if report["simulation"]:
        lines.append("- SYNTHETIC DATA. Prices and levels are for software testing only.")
    return "\n".join(lines) + "\n"
