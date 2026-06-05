# fund-alert-bot

Lightweight personal investment alert bot for personal portfolio reminders.

This project runs alongside `maxduke/rsi6_monitor_bot`. The existing RSI6 bot remains responsible for RSI6 alerts. `fund-alert-bot` focuses on a smaller set of non-trading reminders:

- drawdown from recent high alerts
- DCA reminders
- profit-taking reminders
- multi-channel notifications

This repository is intentionally not a web platform, not an RSI implementation, and not an automatic trading system.

## Current Status

The project has a Python package skeleton, environment-based configuration,
SQLite storage helpers, drawdown-from-high rule evaluation, Telegram commands,
weekday after-close drawdown scheduling, multi-channel notification dispatch,
market data normalization, tests, Ruff configuration, and Docker packaging.

Implemented Telegram commands:

- `/add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>`
- `/list`
- `/del <id>`
- `/check`
- `/test_notify`

Supported drawdown `asset_type` values are `cn_index`, `cn_etf`, `cn_stock`,
and `cn_open_fund`. Thresholds are entered as percentages, for example
`10,15,20`. `/check` runs enabled drawdown rules immediately. APScheduler also
runs the same drawdown evaluation Monday-Friday after CN market close. Telegram
remains the command channel and default notification channel; optional Bark,
ntfy, and webhook channels can be enabled with environment variables.

Default scheduler configuration:

- `TZ=Asia/Shanghai`
- `AFTER_CLOSE_CHECK_TIME=17:10`
- `BARK_ENABLED=false`
- `NTFY_ENABLED=false`
- `WEBHOOK_ENABLED=false`

Optional notification channel configuration:

- `BARK_SERVER_URL`
- `BARK_DEVICE_KEY`
- `NTFY_SERVER_URL`
- `NTFY_TOPIC`
- `WEBHOOK_URL`

DCA reminders, profit-taking reminders, realtime quotes, and RSI/RSI6 alerts are
not implemented here.

## Planned Stack

- Python 3.12
- python-telegram-bot
- SQLite
- AKShare
- pandas
- APScheduler
- requests
- pytest
- ruff
- Docker
- Docker Compose

Do not add Django, FastAPI, PostgreSQL, Redis, Celery, RSI indicators, a web UI, or automatic trading features.

## Local Development

After the project skeleton is added in a future PR, the expected local flow will be:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Configuration should be created from `.env.example`:

```powershell
Copy-Item .env.example .env
```

Do not commit `.env` or real secrets.

Once tooling exists, use:

```powershell
ruff check .
pytest
```

Docker builds are validated by GitHub Actions on Linux. Local Docker is optional,
especially on Windows workstations.

## GitHub Actions

- `CI`: installs the project with dev dependencies, then runs Ruff and pytest on
  Python 3.12.
- `Docker Build`: builds the Docker image on Ubuntu for pull requests and pushes
  `ghcr.io/maxduke/fund-alert-bot` on non-PR runs.

## Project Documents

- `AGENTS.md`: contributor and coding-agent guardrails
- `docs/architecture.md`: planned module responsibilities
- `docs/roadmap.md`: PR-sized implementation phases
- `.env.example`: placeholder-only configuration template

## Scope Boundaries

`fund-alert-bot` may read market/fund data, calculate supported personal reminder conditions, store alert state in SQLite, schedule checks, and send notifications.

`fund-alert-bot` must not place trades, submit orders, rebalance accounts, provide financial advice, implement RSI or RSI6 alerts, modify `rsi6_monitor_bot`, or expose a public/private web application.
