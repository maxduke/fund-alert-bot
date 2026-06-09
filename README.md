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
SQLite storage helpers, drawdown-from-high rule evaluation, DCA reminder
evaluation, profit-taking reminder evaluation, Telegram commands, scheduled
market and DCA checks, multi-channel notification dispatch with delivery state,
market data normalization, tests, Ruff configuration, and Docker packaging.

Implemented Telegram commands:

- `/add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>`
- `/add_profit <asset_type> <symbol> <name> <cost> <thresholds>`
- `/add_dca <name> <weekday> <amount>`
- `/list`
- `/del <id>`
- `/check`
- `/test_notify`

Supported drawdown `asset_type` values are `cn_index`, `cn_etf`, `cn_stock`,
and `cn_open_fund`. Thresholds are entered as percentages, for example
`10,15,20`. `/check` runs enabled drawdown rules immediately and includes the
current drawdown percentage for each checked drawdown rule. APScheduler also
runs a before-close realtime drawdown check Monday-Friday so reminders can arrive
before the same-day close, then runs the after-close market reminder check. Both
scheduled market jobs skip official CN market holidays when AKShare's Sina
trade-date calendar is available. If that calendar cannot be loaded, the
scheduled check falls back to weekday behavior.

Profit-taking reminders are added with `/add_profit`, for example
`/add_profit cn_etf 159915 ChiNext-ETF 1.85 25,40` or
`/add_profit cn_open_fund 110026 Example-Fund 1.234 25,40`. The cost is the
personal cost basis, and thresholds are entered as percentages. `/check` uses
the latest normalized `close` value from the market data provider as the current
price; for `cn_open_fund`, that `close` value is the latest unit NAV. Each
configured threshold sends at most one reminder per cost basis. Profit reminders
are notifications only and do not calculate position size, redeem funds, place
orders, or connect to a broker. APScheduler evaluates profit reminders in the
same after-close scheduled market reminder check as drawdown rules.

DCA reminders are added with `/add_dca`, for example `/add_dca 创业板 周四
1000` or `/add_dca 创业板 Thursday 1000`. Supported weekdays are 周一 through
周日 and Monday through Sunday; rules store normalized weekday codes such as
`THU`. `/check` also checks whether DCA reminders are due today. Scheduled DCA
checks run daily and send at most one reminder per rule per date. DCA reminders
do not fetch market data and do not trade.

Telegram remains the command channel and default notification channel; optional
Bark, ntfy, and webhook channels can be enabled with environment variables.

Default scheduler configuration:

- `TZ=Asia/Shanghai`
- `AFTER_CLOSE_CHECK_TIME=17:10`
- `BEFORE_CLOSE_CHECK_TIME=14:50`
- `DCA_REMINDER_TIME=09:30`
- `AKSHARE_RETRIES=3`
- `AKSHARE_RETRY_DELAY_SECONDS=0.5`
- `AKSHARE_LATEST_LOOKBACK_DAYS=45`
- `BARK_ENABLED=false`
- `NTFY_ENABLED=false`
- `WEBHOOK_ENABLED=false`

Optional notification channel configuration:

- `BARK_SERVER_URL`
- `BARK_DEVICE_KEY`
- `NTFY_SERVER_URL`
- `NTFY_TOPIC`
- `WEBHOOK_URL`

Realtime quotes and RSI/RSI6 alerts are not implemented here.

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
