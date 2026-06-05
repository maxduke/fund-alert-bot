# fund-alert-bot

Lightweight personal investment alert bot for personal portfolio reminders.

This project runs alongside `maxduke/rsi6_monitor_bot`. The existing RSI6 bot remains responsible for RSI6 alerts. `fund-alert-bot` focuses on a smaller set of non-trading reminders:

- drawdown from recent high alerts
- DCA reminders
- profit-taking reminders
- multi-channel notifications

This repository is intentionally not a web platform, not an RSI implementation, and not an automatic trading system.

## Current Status

The project is in documentation-only scaffold mode. There is no business logic yet.

The first implementation PRs should add the Python package skeleton, tooling, tests, and configuration loading before any alert rules are built.

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
docker compose up --build
```

## Project Documents

- `AGENTS.md`: contributor and coding-agent guardrails
- `docs/architecture.md`: planned module responsibilities
- `docs/roadmap.md`: PR-sized implementation phases
- `.env.example`: placeholder-only configuration template

## Scope Boundaries

`fund-alert-bot` may read market/fund data, calculate supported personal reminder conditions, store alert state in SQLite, schedule checks, and send notifications.

`fund-alert-bot` must not place trades, submit orders, rebalance accounts, provide financial advice, implement RSI or RSI6 alerts, modify `rsi6_monitor_bot`, or expose a public/private web application.
