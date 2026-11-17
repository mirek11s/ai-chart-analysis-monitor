# ai-chart-analysis-monitor project instructions

Jarvis remains the assistant name and `jarvis` remains the CLI command.

Use [the shared Jarvis skill](skills/jarvis-charts/SKILL.md) for chart analysis, browser access, monitoring, and interpreting outputs. Read it before operating Jarvis. Claude uses the same instructions.

Keep responses short. Report actual artifact paths, data mode, and worker status. Use the local `.venv` interpreter when available. Implementations live in `src/jarvis`; tests run with `python -m pytest -q`. Run `ruff check src tests` after changes.

Preserve read-only market access, explicit existing-session authorization, traceable level calculation, and transactional alert deduplication. Do not initialize Git or create a GitHub repository.
