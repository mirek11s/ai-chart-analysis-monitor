"""Opt-in real Chromium/CDP smoke tests, against controlled local chart markup.

Run with JARVIS_BROWSER_TESTS=1 and Playwright's Chromium installed.
No existing personal Chrome session is accessed; each test launches its own profile.
"""

import os
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from jarvis.browser import BrowserSession
from jarvis.browser.session import chart_url

pytestmark = pytest.mark.skipif(
    os.environ.get("JARVIS_BROWSER_TESTS") != "1", reason="opt-in Chromium tests"
)

HTML = """<!DOCTYPE html><html><body>
<button title="Symbol search">BTCUSDC</button>
<div><div><button aria-label="Change symbol">Bitcoin / USD Coin</button></div><div>Binance</div></div>
<button aria-label="1 minute">1m</button><canvas width="800" height="500"></canvas>
<script>const c=document.querySelector('canvas').getContext('2d');c.fillStyle='green';c.fillRect(10,10,100,100);</script>
</body></html>"""


def test_owned_browser_navigation_identity_capture(settings, tmp_path):
    with BrowserSession(settings, "owned") as browser:
        browser.context.route(
            "https://www.tradingview.com/**",
            lambda route: route.fulfill(body=HTML, content_type="text/html"),
        )
        browser.navigate("BTC/USDC", "binance", "1m")
        assert browser.identify()["symbol"] == "BTC/USDC"
        destination = tmp_path / "capture.png"
        metadata = browser.capture(destination, "BTC/USDC", "binance", "1m")
        assert metadata["verified_ui"]
        assert metadata["visible_exchange"] == "binance"
        assert destination.read_bytes().startswith(b"\x89PNG")
        browser.page.evaluate("history.replaceState({}, '', '/chart/saved-layout/')")
        assert browser.identify()["exchange"] == "binance"


def test_cdp_attach_detaches_without_closing_owned_test_browser(settings, tmp_path):
    from playwright.sync_api import sync_playwright

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with sync_playwright() as playwright:
        executable = playwright.chromium.executable_path
    arguments = [
        executable,
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={tmp_path / 'cdp-profile'}",
        "about:blank",
    ]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    process = subprocess.Popen(
        arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options
    )
    endpoint = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urlopen(endpoint + "/json/version", timeout=1):
                    break
            except (URLError, OSError):
                time.sleep(0.1)
        else:
            pytest.fail("Test Chromium CDP endpoint did not start")
        with sync_playwright() as playwright:
            remote = playwright.chromium.connect_over_cdp(endpoint)
            context = remote.contexts[0]
            context.route(
                "https://www.tradingview.com/**",
                lambda route: route.fulfill(body=HTML, content_type="text/html"),
            )
            context.pages[0].goto(chart_url("BTC/USDC", "binance", "1m"))
            # Remove the route before disconnecting the bootstrap client.
            context.unroute_all()
        with BrowserSession(settings, "existing", allow_existing=True, cdp_url=endpoint) as browser:
            assert browser.identify()["timeframe"] == "1m"
            assert browser.capture(tmp_path / "existing.png", "BTC/USDC", "binance", "1m")[
                "verified_ui"
            ]
        assert process.poll() is None
        with urlopen(endpoint + "/json/version", timeout=2) as response:
            assert response.status == 200
    finally:
        process.terminate()
        process.wait(timeout=10)
