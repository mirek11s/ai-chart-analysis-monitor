# Using Jarvis

## 1. Ask in this chat

This project chat has local file and command access. Type this request, or dictate it:

> Jarvis, analyze Binance BTC/USDC on the daily chart. Open your own TradingView browser, find support and resistance, and show me the annotated chart and short analysis.

Then send a follow-up:

> Analyze the same pair on the 1-minute chart and monitor its nearest detected support. Check every 30 seconds. Confirm the worker is running.

Changing timeframe requires a fresh analysis. If no support qualifies, Jarvis reports that instead of inventing one.

Other requests:

- "Show my active monitors."
- "Show the latest BTC/USDC chart."
- "What changed since the previous daily analysis?"
- "Stop monitoring BTC/USDC."

## 2. Use the microphone

For **dictation**, press the microphone, speak the request, review the transcription, and send it. It becomes a normal text request in this project chat. [OpenAI dictation guide](https://help.openai.com/en/articles/12168547-voice-dictation-faq).

If the app offers **Start voice chat** for this task, you can use that for a spoken conversation. Availability depends on the app, account, and rollout; dictation is a separate feature. [OpenAI voice guide](https://learn.chatgpt.com/docs/features/voice).

Jarvis itself has no built-in microphone listener, wake word, or spoken alerts. Microphone input comes from the chat app. You do not need a separate terminal when this chat can run the project commands.

## 3. Alternatively, use Codex in a terminal

Open PowerShell:

```powershell
cd C:\Users\Mirek\Desktop\Code\ai-chart-analysis-monitor
codex
```

Then enter the same natural-language request from section 1. The project has `AGENTS.md` and a shared skill describing how to operate Jarvis. Codex CLI runs from your project directory. [Codex CLI guide](https://developers.openai.com/codex/cli/).

Codex interprets your request and runs Jarvis tools. Opening Codex alone does not start a monitor.

## 4. Run Jarvis directly

In PowerShell, enter the project directory first:

```powershell
cd C:\Users\Mirek\Desktop\Code\ai-chart-analysis-monitor
```

Analyze a daily chart:

```powershell
.\.venv\Scripts\python.exe -m jarvis analyze --symbol BTC/USDC --timeframe 1d --browser owned
```

Analyze one-minute candles and start monitoring the nearest qualifying support:

```powershell
.\.venv\Scripts\python.exe -m jarvis analyze --symbol BTC/USDC --timeframe 1m --monitor
```

These commands print JSON with output paths and monitor/worker details. `--browser owned` uses Jarvis's isolated browser. Without it, analysis still works from market data and generates its own chart.

For the basic local text interface:

```powershell
.\.venv\Scripts\python.exe -m jarvis chat
```

Type `Analyze BTC on the daily`, then `Monitor the nearest one on the 1-minute chart`. Type `quit` to exit. This interface recognizes a limited set of intents; Codex or Claude handles broader language.

## 5. Find results and alerts

- Charts and reports: `outputs/<run>/chart_annotated.png` and `analysis.md`.
- Source candles and level evidence: `candles.csv` and `levels.json` in the same folder.
- Alerts: `data/events/`; detached worker messages: `data/worker.log`.

To watch the alert log:

```powershell
Get-Content .\data\worker.log -Wait
```

Alerts currently do not push messages into this chat, your phone, or desktop notifications. The worker continues after the conversation closes, but your computer must stay awake and online. After a reboot, run `jarvis worker start` through the environment below, or configure the optional startup task in [README.md](README.md#independent-worker-and-restart-recovery).

## 6. Check or stop monitoring

```powershell
.\.venv\Scripts\python.exe -m jarvis monitor list --active
.\.venv\Scripts\python.exe -m jarvis worker status
.\.venv\Scripts\python.exe -m jarvis monitor stop --symbol BTC/USDC
```

Worker controls:

```powershell
.\.venv\Scripts\python.exe -m jarvis worker stop
.\.venv\Scripts\python.exe -m jarvis worker start
```

Stopping the worker pauses polling but preserves active jobs. Stopping a monitor disables that job. Live monitor creation normally starts the worker automatically; verify `worker.running` in the result.

## 7. Existing Chrome tabs and configuration

For the simplest start, use the owned browser. Using your existing TradingView tab requires Chrome debugging setup and explicit session permission. Follow [browser setup](README.md#browser-access), then ask:

> Jarvis, use my TradingView tab in the debugging browser. I authorize access to that session. Analyze BTC/USDC on the 1-minute chart.

The installed project automatically detects its home directory and `data/browsers`. No environment variables are needed for this setup. `.env` is not automatically loaded. Binance spot is the supported market; Jarvis does not execute trades.
