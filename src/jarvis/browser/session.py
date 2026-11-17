from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from jarvis.models import INTERVALS, JarvisError, symbol, timeframe


def tv_interval(interval: str) -> str:
    interval = timeframe(interval)
    return interval.upper() if interval.endswith(("d", "w")) else str(INTERVALS[interval] // 60)


def from_tv_interval(value: str) -> str:
    if value.isdigit():
        minutes = int(value)
        return timeframe(
            f"{minutes // 60}h" if minutes >= 60 and minutes % 60 == 0 else f"{minutes}m"
        )
    return timeframe(value if value[0].isdigit() else "1" + value)


def chart_url(pair: str, exchange: str, interval: str) -> str:
    if exchange.lower() != "binance":
        raise JarvisError("Browser chart matching currently supports Binance spot only.")
    return "https://www.tradingview.com/chart/?" + urlencode(
        {
            "symbol": f"{exchange.upper()}:{symbol(pair).replace('/', '')}",
            "interval": tv_interval(interval),
        }
    )


def identity_from_url(url: str) -> dict:
    parsed = urlparse(url)
    if parsed.hostname not in {
        "tradingview.com",
        "www.tradingview.com",
    } or not parsed.path.startswith("/chart/"):
        raise JarvisError("Select a TradingView chart tab.")
    query = parse_qs(parsed.query)
    raw = query.get("symbol", [""])[0].upper()
    match = re.fullmatch(r"([A-Z0-9]+):([A-Z0-9]+)", raw)
    if not match:
        raise JarvisError(
            "Tab URL does not identify exchange and spot symbol. Supply --symbol and --exchange explicitly."
        )
    exchange, ticker = match.groups()
    pair = None
    for quote in ("USDC", "USDT", "FDUSD", "TUSD", "BUSD", "BTC", "ETH", "EUR", "USD", "BNB"):
        if ticker.endswith(quote) and len(ticker) > len(quote):
            pair = symbol(ticker[: -len(quote)] + "/" + quote)
            break
    if not pair:
        raise JarvisError("Cannot identify quote currency. Supply an explicit spot pair.")
    raw_interval = query.get("interval", [None])[0]
    return {
        "symbol": pair,
        "exchange": exchange.lower(),
        "timeframe": from_tv_interval(raw_interval) if raw_interval else None,
    }


def validate_cdp_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "ws"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
    ):
        raise JarvisError(
            "CDP must use an unauthenticated loopback URL, e.g. http://127.0.0.1:9222."
        )


class BrowserSession:
    """Small website adapter. Never reads cookies, storage, passwords, or credentials."""

    def __init__(
        self,
        settings,
        mode: str,
        *,
        allow_existing: bool = False,
        cdp_url: str | None = None,
        tab: int | None = None,
        headed: bool = False,
    ):
        if mode not in {"owned", "existing"}:
            raise JarvisError("Browser mode must be owned or existing.")
        if mode == "existing" and not allow_existing:
            raise JarvisError(
                "Existing-session access requires explicit --allow-existing authorization."
            )
        self.settings, self.mode, self.tab, self.headed = settings, mode, tab, headed
        self.cdp_url = cdp_url or settings.cdp_url
        if mode == "existing":
            validate_cdp_url(self.cdp_url)
        self.playwright = self.context = self.browser = self.page = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise JarvisError(
                'Browser extra missing: pip install -e ".[browser]"; python -m playwright install chromium'
            ) from exc
        try:
            local_browsers = self.settings.home / "data" / "browsers"
            if local_browsers.exists():
                os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(local_browsers))
            self.playwright = sync_playwright().start()
            if self.mode == "existing":
                self.browser = self.playwright.chromium.connect_over_cdp(
                    self.cdp_url, timeout=10000
                )
                pages = [
                    p
                    for c in self.browser.contexts
                    for p in c.pages
                    if urlparse(p.url).hostname in {"www.tradingview.com", "tradingview.com"}
                    and urlparse(p.url).path.startswith("/chart/")
                ]
                if not pages:
                    raise JarvisError(
                        "No TradingView chart tab in the authorized browser. Open one first."
                    )
                if self.tab is None and len(pages) > 1:
                    raise JarvisError(
                        f"Found {len(pages)} chart tabs; select --tab 0..{len(pages) - 1}."
                    )
                index = self.tab if self.tab is not None else 0
                if not 0 <= index < len(pages):
                    raise JarvisError("Chart tab index out of range.")
                self.page = pages[index]
            else:
                profile = self.settings.home / "data" / "browser-profile"
                self.context = self.playwright.chromium.launch_persistent_context(
                    str(profile),
                    headless=not self.headed,
                    viewport={"width": 1440, "height": 1000},
                )
                self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(5000)
            return self
        except JarvisError:
            self.__exit__(None, None, None)
            raise
        except Exception as exc:
            self.__exit__(None, None, None)
            raise JarvisError(
                f"Browser connection failed ({type(exc).__name__}). Check browser installation, CDP, or profile lock."
            ) from exc

    def __exit__(self, *_):
        # Cleanup must not replace a successful market-data fallback with a browser error.
        # Stop the client transport; never close the user's remote browser.
        for resource, method in ((self.context, "close"), (self.playwright, "stop")):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    pass

    def identify(self) -> dict:
        visible = self._visible_symbol()
        visible_exchange = self._visible_exchange()
        interval = self._visible_interval()
        try:
            identity = identity_from_url(self.page.url)
        except JarvisError:
            if not (visible and visible_exchange and interval):
                raise
            parsed = urlparse(self.page.url)
            if parsed.hostname not in {"www.tradingview.com", "tradingview.com"}:
                raise JarvisError("Select a TradingView chart tab.") from None
            # A saved layout may omit symbol/interval query parameters entirely.
            query = urlencode(
                {
                    "symbol": f"{visible_exchange.upper()}:{visible}",
                    "interval": tv_interval(interval),
                }
            )
            identity = identity_from_url("https://www.tradingview.com/chart/?" + query)
        if visible and visible != identity["symbol"].replace("/", ""):
            raise JarvisError(
                "Visible chart symbol disagrees with URL. Supply --symbol to navigate explicitly."
            )
        if visible_exchange and visible_exchange != identity["exchange"]:
            raise JarvisError(
                "Visible chart exchange disagrees with URL. Supply an explicit market."
            )
        # Saved layouts may have stale interval URLs. Read the toolbar if available.
        if interval:
            identity["timeframe"] = interval
        identity["identity_source"] = "TradingView URL and visible toolbar when available"
        return identity

    def _visible_symbol(self) -> str | None:
        controls = self.page.locator(
            '[data-name="header-toolbar-symbol-search"]:visible, button[title="Symbol search"]:visible'
        )
        for control in controls.all():
            text = control.inner_text().strip().upper()
            match = re.fullmatch(r"(?:[A-Z]+:)?([A-Z0-9]+(?:\.P)?)", text)
            if match:
                return match.group(1)
        return None

    def _visible_exchange(self) -> str | None:
        controls = self.page.get_by_role("button", name="Change symbol", exact=True)
        if controls.count() > 1:
            raise JarvisError("Multiple chart panes are ambiguous. Select a single-chart layout.")
        if controls.count():
            title = controls.first.evaluate("e => e.parentElement.parentElement.innerText")
            candidate = title.strip().splitlines()[-1].strip()
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,20}", candidate):
                return candidate.lower()
        return None

    def _visible_interval(self) -> str | None:
        controls = self.page.locator('[data-name="header-toolbar-intervals"]:visible')
        for control in controls.all():
            text = control.inner_text().strip()
            try:
                return from_tv_interval(text)
            except (JarvisError, IndexError):
                pass
        controls = self.page.get_by_role(
            "button", name=re.compile(r"^\d+ (?:minute|hour|day|week)s?$", re.I)
        )
        for control in controls.all():
            label = control.get_attribute("aria-label") or ""
            match = re.fullmatch(r"(\d+) (minute|hour|day|week)s?", label, re.I)
            if match:
                return timeframe(match.group(1) + match.group(2)[0].lower())
        return None

    def navigate(self, pair: str, exchange: str, interval: str) -> None:
        try:
            self.page.goto(
                chart_url(pair, exchange, interval), wait_until="domcontentloaded", timeout=30000
            )
            self.page.locator("canvas").first.wait_for(state="visible", timeout=15000)
            self.page.wait_for_timeout(1500)
        except Exception as exc:
            raise JarvisError(
                "TradingView chart unavailable or blocked; use the generated OHLCV chart."
            ) from exc

    def capture(self, path: Path, pair: str, exchange: str, interval: str) -> dict:
        identity = identity_from_url(self.page.url)
        if any(
            identity[key] != value
            for key, value in (("symbol", pair), ("exchange", exchange), ("timeframe", interval))
        ):
            raise JarvisError("TradingView URL no longer matches the requested market/timeframe.")
        visible_symbol, visible_interval = self._visible_symbol(), self._visible_interval()
        visible_exchange = self._visible_exchange()
        if visible_symbol and visible_symbol != pair.replace("/", ""):
            raise JarvisError("Visible TradingView symbol mismatch; screenshot not accepted.")
        if visible_interval and visible_interval != interval:
            raise JarvisError("Visible TradingView timeframe mismatch; screenshot not accepted.")
        if visible_exchange and visible_exchange != exchange:
            raise JarvisError("Visible TradingView exchange mismatch; screenshot not accepted.")
        self.page.screenshot(path=str(path), full_page=False, timeout=15000)
        return {
            "status": "captured",
            "mode": self.mode,
            "url": self.page.url,
            "visible_symbol": visible_symbol,
            "visible_timeframe": visible_interval,
            "visible_exchange": visible_exchange,
            "verified_ui": bool(visible_symbol and visible_interval and visible_exchange),
            "limitation": "Unannotated browser reference. Annotated chart is independently rendered from Binance OHLCV.",
        }
