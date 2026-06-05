# AGENTS.md

Guidance for coding agents and contributors working in this repository.

## Mission

Build a lightweight personal investment alert bot for:

- drawdown from recent high alerts
- DCA reminders
- profit-taking reminders
- multi-channel notifications

The bot should remain small, local-first, and maintainable.

## Hard Scope Limits

These limits are intentional and should be enforced in reviews:

- Do not implement RSI, RSI6, or any RSI-derived alert logic.
- Do not copy functionality from `maxduke/rsi6_monitor_bot`.
- Do not modify `maxduke/rsi6_monitor_bot` or any sibling repository.
- Do not build a web platform, web UI, dashboard, admin panel, API server, or hosted service.
- Do not add Django, FastAPI, PostgreSQL, Redis, or Celery.
- Do not implement automatic trading, brokerage integrations, order placement, or portfolio rebalancing.
- Do not turn notifications into financial advice; alerts are reminders only.
- Do not add business logic before the project skeleton, config, storage, tests, and docs are ready.

If a requested change pushes against these boundaries, pause and document the smaller in-scope version instead.

## Approved Technology Direction

Use the planned stack unless a future decision document changes it:

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

Prefer simple modules, explicit configuration, and boring operational behavior.

## Repository Discipline

- Keep changes scoped to this repository.
- Preserve existing user changes in the working tree.
- Add focused tests with each implementation PR.
- Keep secrets out of git.
- Use `.env.example` for placeholder configuration only.
- Document behavior before broadening alert rules or notification channels.

## PR Workflow

Use this finish process for repository changes:

1. Review the diff and confirm the scope is correct.
2. Run available tests and checks.
3. Commit the change.
4. Push the branch.
5. Open a PR.
6. After approval or explicit merge request, use squash merge.
7. Delete the remote branch.
8. Sync local `main`.
9. Delete the local feature branch.

Prefer:

```powershell
gh pr merge <PR_NUMBER> --squash --delete-branch
git switch main
git pull --ff-only origin main
git branch -d <branch>
```

If `git branch -d` refuses after a verified squash merge, use `git branch -D <branch>`.

## Alert Ownership

This project owns:

- recent-high drawdown checks
- recurring DCA reminder checks
- profit-taking reminder checks
- notification dispatch and delivery state for those reminders

`maxduke/rsi6_monitor_bot` owns RSI6 alerts. That separation should remain visible in documentation, code names, tests, and configuration.

## Design Principles

- Local-first: SQLite and environment-based configuration are enough.
- Small runtime: APScheduler jobs inside a single Python service are enough.
- Observable: logging and explicit notification results should be easy to inspect.
- Testable: isolate data providers, alert evaluators, storage, scheduling, and notification dispatch.
- Conservative: never add infrastructure because it might be useful later.
