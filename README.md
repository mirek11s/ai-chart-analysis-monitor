# ai-chart-analysis-monitor

Jarvis is the assistant and `jarvis` is the CLI for this project.

Python MVP: real Binance spot OHLCV → reproducible support/resistance → precisely scaled PNG + Markdown → persistent SQLite monitor → local alert. Runs without a paid API or an open AI conversation. No trading endpoints.

## Install

Python 3.11+; tested here with Python 3.13. Run from this directory.

```powershell
cd C:\Users\Mirek\Desktop\Code\ai-chart-analysis-monitor
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[browser,dev]"
# Optional browser; storing binaries here keeps the installation self-contained.
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) 'data\browsers'
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\Activate.ps1
jarvis doctor
```

Activation is optional: `.\.venv\Scripts\python.exe -m jarvis ...` always works. On Unix: `python3 -m venv .venv`, `.venv/bin/pip install -e '.[browser,dev]'`, `.venv/bin/python -m playwright install chromium`, then `source .venv/bin/activate`. For analysis without browser tools, install `-e .` only. Do not copy virtual environments between directories; recreate them after moving the project. `requirements-lock.txt` records the verified Python 3.13 Windows environment; install it before `pip install -e . --no-deps` to reproduce those versions.

`JARVIS_HOME` or global `jarvis --home <path>` selects the data/configuration root. Editable installations default to this project directory regardless of current working directory; regular installations default to `~/.jarvis`. `config/settings.toml` contains defaults; optional `config/local.toml` overrides them. `.env.example` documents environment variables; `.env` is not loaded automatically. Local data, profiles, outputs, and private configuration are ignored by the supplied `.gitignore`. No Git repository is initialized.

## Analyze

```powershell
jarvis analyze --symbol BTC/USDC --exchange binance --timeframe 1d
jarvis analyze --symbol BTC/USDC --timeframe 4h --browser owned
jarvis latest --symbol BTC/USDC --timeframe 4h
jarvis compare --symbol BTC/USDC --timeframe 4h
```

CLI stdout is JSON; diagnostic errors use stderr and exit code 2. `--json` before the command is accepted but unnecessary. The Python entry point is `jarvis.analysis.service.analyze(Settings.load(), pair='BTC/USDC', interval='1d')`.

Each run creates a unique directory under `outputs/`, named with its UTC timestamp, pair, timeframe, and short ID:

```text
outputs/2026-11-17_123000_BTC-USDC_1d_<id>/
  chart_original.png    # browser screenshot if captured; otherwise raw OHLCV chart
  chart_annotated.png   # exact OHLCV candlestick chart with detected zones
  analysis.md          # concise interpretation, sources, limitations
  levels.json          # identity, quote, parameters, pivot evidence, checksum
  candles.csv          # complete input data used by the algorithm
```

Output names above are illustrative; inspect the actual `output_dir`. `levels.json` is written last; incomplete runs do not appear in `latest`. The original browser image is never used to estimate line positions. The annotated image uses Matplotlib's actual price coordinates, with a linear axis and translucent cluster zones. Label leaders preserve exact line positions while avoiding text overlap. `browser.verified_ui` indicates whether visible symbol, exchange, and timeframe were verified; the browser image is a reference only.

The default algorithm uses 500 **completed** candles, a strict pivot window of 3 candles on each side, clusters spanning at most 0.3% of their lowest price, and at least 2 pivot touches separated by more than the window. Level = median pivot price. The nearest valid level plus the strongest remaining zones are selected, then ordered by distance. Each pivot is traceable to its CSV row and UTC timestamp. Right-edge pivots need future confirmation and are excluded. Flat candles and insufficient repeated evidence produce no level. Supports must be at/below the observed quote; resistance at/above it.

Tune with `--limit 999 --window 3 --cluster-pct 0.3 --min-touches 2`. Smaller intervals may need a narrower cluster percentage. Maximum history is 999 candles; no historical pagination in this MVP. Missing/duplicate/malformed candles, wrong market identity, stale data, and unsupported timeframes cause errors. Available intervals: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w. Monthly candles are unsupported.

Market data uses Binance's public market-only host and verifies exchange metadata; no authentication or market substitution. REST requests are spaced, transient errors receive bounded retries, and HTTP 418/429 honor `Retry-After`. Regional unavailability is an error, never an automatic simulation fallback. See [Binance market-only endpoints](https://developers.binance.com/docs/binance-spot-api-docs/faqs/market_data_only) and [market-data API](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints).

## Monitor

```powershell
# Complete vertical slice; starts a detached worker automatically.
jarvis analyze --symbol BTC/USDC --timeframe 1m --monitor

# Or select a level from a saved analysis without entering its price.
jarvis monitor start --from-analysis latest --poll-seconds 30 --tolerance-pct 0.05
jarvis monitor start --from-analysis '<output-directory>' --side resistance --level-index 1
jarvis monitor list --active
jarvis monitor events --job-id <job-id>
jarvis monitor stop <job-id>
jarvis monitor stop --symbol BTC/USDC
```

`--from-analysis` preserves the source pair, timeframe, and level evidence. A different timeframe requires new analysis. Use fresh analysis when starting a monitor; jobs store a fixed level and never recompute it automatically. Manual levels are also supported: `jarvis monitor start --symbol BTC/USDC --timeframe 1m --level <your-level>`. A manually entered level is labeled by its absent analysis provenance; it is not claimed to be detected support.

Support condition: `price <= level * (1 + tolerance_pct / 100)`. Resistance: `price >= level * (1 - tolerance_pct / 100)`. Tolerance is a **percentage**, not quote-currency units. The example default 0.05 means 0.05%. First observation already beyond the threshold produces an alert. Gaps across the threshold also trigger on the next sample. Sampling can miss crossings between polls; no tick-history backfill is attempted.

An alert disarms the job. It re-arms only after recovery beyond twice its tolerance. Another alert requires both re-arming and elapsed cooldown (`--cooldown-seconds`, default 300). Remaining beyond the threshold never produces repeated alerts just because cooldown expires. The transition and event insertion share a SQLite transaction, protecting against concurrency and restart duplicates. Non-increasing or stale quote timestamps are rejected/ignored.

Live polling minimum: 5 seconds, default 30. A shared feed cache limits same-cycle duplicate price requests. Failures persist in the job, retry with backoff, and do not stop other jobs. SQLite WAL stores jobs, armed state, check times, failures, worker heartbeat, and the alert outbox.

Alerts write `data/events/<event-id>.json` and terminal messages. Detached-worker terminal output goes to `data/worker.log`; it does not pop up a desktop notification. Use `Get-Content .\data\worker.log -Wait` to follow it. The event outbox retries failed delivery across restarts. Local files are idempotent by event ID. External sinks are **at least once**: a crash after a side effect but before acknowledgement can repeat delivery, so future integrations must use `event['id']` as an idempotency key.

## Independent worker and restart recovery

```powershell
jarvis worker status
jarvis worker start     # detached; hidden on Windows; log in data/worker.log
jarvis worker stop      # cooperative stop after current operation
jarvis worker run       # foreground; useful for debugging or a service manager
jarvis worker tick      # one cycle, only when no worker holds the lock
```

Live `monitor start` starts the worker unless `--no-worker` is supplied. Check the returned `worker.running`; startup failure leaves the saved job ID visible with a recovery command. `worker stop` preserves active jobs, which resume at the next start. `monitor stop` disables that job persistently. An OS file lock allows one worker per data directory and releases automatically after crashes. Heartbeats run separately from network/browser checks.

The machine must be awake and connected. A detached process survives closing the chat/terminal; it cannot survive shutdown. Jobs resume from SQLite when the worker next starts. To start at Windows sign-in, explicitly run:

```powershell
.\scripts\install-startup.ps1
# Undo only this scheduled task:
Unregister-ScheduledTask -TaskName 'JarvisChartAgent' -Confirm:$false
```

The installer creates a current-user logon task with restart-on-failure. It is provided but not run automatically. It starts a hidden runner and writes `data/worker-scheduled.log`. On Linux, use `config/jarvis.service.example` as a user systemd service after replacing its absolute paths. Starting a service is optional. Heartbeat status means process liveness; individual job errors and `last_observed` show data health. Recovery detects the current price, not crossings that happened while the worker was offline.

## Browser access

**Owned browser:** `jarvis analyze --browser owned --symbol BTC/USDC --timeframe 1d`. Uses an isolated persistent profile in `data/browser-profile`, headless by default. `--headed` displays it. `jarvis browser open` opens the visible owned browser until Enter; sign in manually if desired. Close it before another operation uses the same profile. Browser binaries installed in `data/browsers` are discovered automatically unless `PLAYWRIGHT_BROWSERS_PATH` overrides them.

**Existing browser:** ordinary Chrome sessions cannot be attached arbitrarily. Launch a dedicated debugging session with a separate profile, then open/login to TradingView yourself. Chrome 136+ requires a non-default user data directory for remote debugging. See [Chrome debugging changes](https://developer.chrome.com/blog/remote-debugging-port) and [Playwright CDP](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp).

```powershell
# Explicitly launch this yourself when you want the debugging browser visible.
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 `
  "--user-data-dir=$env:LOCALAPPDATA\JarvisChromeDebug" `
  'https://www.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDC&interval=1'

jarvis browser inspect --allow-existing
jarvis analyze --browser existing --allow-existing --timeframe 1m
```

The explicit `--allow-existing` flag authorizes the requested operation. Jarvis accepts loopback CDP endpoints only; keep the port local. It never reads or exports passwords, cookies, or session storage. It does not close your connected browser. Multiple TradingView chart tabs require `--tab 0` (or another chart-tab index). Browser identification uses explicit exchange/symbol URL metadata and visible toolbar values when available; missing identity requires explicit `--symbol BTC/USDC --exchange binance`. An explicit timeframe navigates the selected tab to that timeframe. Ambiguous saved-layout metadata is a limitation, not guessed market identity.

For periodic revisits and an extra screenshot on alerts:

```powershell
jarvis monitor start --from-analysis latest --browser-revisit owned --revisit-seconds 300
# Existing-session permission persists only in this explicitly authorized job:
jarvis monitor start --from-analysis latest --browser-revisit existing --allow-existing --revisit-seconds 300
```

Revisits save image/metadata pairs in `data/revisits/<job-id>/`. Errors persist as `browser_error`; price monitoring and events continue. Browser revisits run sequentially and may delay other polls while a page loads. Use market-only monitoring when timing matters. No website restriction bypass or forced authentication is attempted. TradingView DOM changes, consent screens, login walls, and disconnections can cause browser failure; the OHLCV analysis remains available when the market is explicit.

## Conversation and coding agents

Open this directory in Codex or Claude Code and ask:

```text
Jarvis, analyze BTC on the daily.
Monitor the nearest one on the 1-minute chart.
Show all active monitors.
What changed since the previous analysis?
Stop monitoring BTC.
```

`AGENTS.md` and `CLAUDE.md` route both agents to one [shared skill](skills/jarvis-charts/SKILL.md). It teaches CLI selection, context handling, output interpretation, browser authorization, and monitoring semantics. To reuse from another project, point that project's agent instructions at the shared skill and invoke this installed CLI with `--home <Jarvis-directory>`. No global agent configuration was modified.

`jarvis chat` provides local text interaction with persistent pair/timeframe/analysis context. It uses a deterministic parser for documented intents, not an embedded LLM. Unknown requests return examples; they are never executed as code. Bare BTC means BTC/USDC. `jarvis chat --message 'Analyze BTC on the daily'` handles one message and retains context. `jarvis chat --simulate` keeps separate simulation context. Existing-tab access requires starting chat with `--allow-existing`; it is not enabled just because the text mentions a browser. A timeframe-changing follow-up performs new analysis before creating the monitor.

## Offline demonstration and tests

```powershell
jarvis analyze --symbol BTC/USDC --timeframe 1m --simulate
$created = jarvis monitor start --from-analysis latest --simulate | ConvertFrom-Json
$level = $created.job.level
jarvis monitor simulate $created.job.id --prices ($level * 1.01) $level ($level * 0.99)
jarvis monitor events --job-id $created.job.id
jarvis monitor stop $created.job.id
python -m pytest -q
ruff check src tests
```

Synthetic OHLCV and events are conspicuously labeled SIMULATION. Explicit simulation sequences advance a virtual clock (`--step-seconds`, default 30); they cannot update a live monitor. The background worker skips simulation jobs. `--simulate` on monitor creation can test a real detected level without fabricating a live event. Simulation is never an automatic fallback for exchange failure.

Tests cover pivot evidence and confirmation, bounded clustering, threshold boundaries, hysteresis, cooldown, persistence, concurrent deduplication, outbox retry, stopped jobs, stale quotes, rate-limit backoff, browser failures, simulation isolation, OHLCV checksums, CLI errors, and multi-turn context.

Two additional Chromium/CDP tests are opt-in: set `JARVIS_BROWSER_TESTS=1` and run `python -m pytest -q tests/test_browser_integration.py`. They launch temporary profiles and controlled chart markup, verify navigation/identity/screenshots, and verify that CDP disconnection leaves the test browser alive. They never attach to your personal browser.

## Structure and extension points

```text
src/jarvis/
  cli.py, config.py, models.py
  market_data/    MarketFeed protocol; Binance and synthetic adapters
  analysis/       deterministic levels, provenance, reports, comparison
  charts/         Matplotlib candlesticks and exact price annotations
  browser/        isolated Playwright and explicitly authorized CDP
  monitoring/     SQLite state machine, worker, simulation
  alerts/         AlertSink protocol; terminal and idempotent files
  conversation/   local intent router, persisted context, VoiceIO protocol
skills/jarvis-charts/SKILL.md
tests/, scripts/, config/, outputs/, data/
```

To add an exchange, implement `MarketFeed.candles/quote`, normalize and validate identity/timestamps/OHLCV, and register it in `make_feed`; extend store/browser identity validation deliberately. To add another site, implement a browser adapter with identify/navigate/capture methods; do not mix UI observations with market evidence. Add an `AlertSink.send(event)` implementation and pass it to `Worker(settings, sinks=[...])`; preserve event-ID idempotency and explicit user configuration. Future webhooks/custom Python callbacks should be explicitly configured adapters, never shell commands derived from an LLM or a webpage. `VoiceIO.listen/speak` can wrap local STT/TTS and is accepted by `Chat.run(voice=...)`.

Not implemented: other exchanges/futures, automatic order placement, indicator interpretation by an LLM, DOM-independent saved-layout discovery, drawing inside TradingView, tick-perfect crossing detection, embedded STT/TTS, desktop/Telegram/email/phone/webhook/script integrations, job editing, automatic level recalculation, multi-user access, and database schema migration tooling. Core modules and CLI are the supported extension surface.

## Verified demonstration — 2026-11-17

- Live Binance BTC/USDC: 500 completed daily candles and 500 completed one-minute candles; TradingView symbol, exchange, and timeframe verified in both screenshots.
- [Daily annotated chart](outputs/2026-11-17_173054_BTC-USDC_1d_e1fe56/chart_annotated.png) and [analysis](outputs/2026-11-17_173054_BTC-USDC_1d_e1fe56/analysis.md).
- [One-minute annotated chart](outputs/2026-11-17_173105_BTC-USDC_1m_04f8f7/chart_annotated.png) and [analysis](outputs/2026-11-17_173105_BTC-USDC_1m_04f8f7/analysis.md).
- Detached live polling and browser revisit succeeded. The same saved job resumed with a new worker process after restart.
- An explicit simulation using the detected one-minute support produced one event; repeated below-threshold observations produced zero duplicates. [Event](data/events/89741db570eb405c948a82f9ac0db271.json).
- Demonstration jobs are stopped; worker stopped; no startup task installed. [Validation record](data/demo-validation.json).
- Automated suite: 64 tests, including two opt-in Chromium/CDP tests. Ruff checks and skill validation pass.
