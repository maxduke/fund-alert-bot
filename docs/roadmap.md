# Roadmap

Implementation should proceed in small PRs. Each PR should keep the hard scope boundaries from `AGENTS.md` intact.

## PR 0: Documentation Scaffold

- Add `AGENTS.md`.
- Expand `README.md`.
- Add `docs/architecture.md`.
- Add `docs/roadmap.md`.
- Add `.env.example`.
- Do not add business logic.

## PR 1: Project Skeleton and Tooling

- Add Python package structure.
- Add `pyproject.toml`.
- Configure Python 3.12.
- Configure `pytest`.
- Configure `ruff`.
- Add empty module tests or smoke tests.
- Do not add alert rules yet.

## PR 2: Configuration and Logging

- Add environment-based settings loader.
- Validate required settings.
- Add structured logging conventions.
- Keep `.env.example` placeholder-only.

## PR 3: SQLite Storage Foundation

- Add SQLite connection handling.
- Add schema initialization.
- Add repositories for watched instruments, alert state, and notification history.
- Add storage tests using temporary databases.

## PR 4: Notification Adapters

- Add notification event model.
- Add Telegram adapter with `python-telegram-bot`.
- Add optional generic webhook adapter with `requests` if needed.
- Add notification formatting tests.
- Do not add trading actions.

## PR 5: Data Provider Layer

- Add AKShare provider interfaces.
- Normalize provider output with pandas.
- Add provider error handling.
- Add tests with mocked provider responses.
- Do not implement RSI indicators.

## PR 6: Drawdown Alerts

- Add recent-high tracking.
- Add drawdown threshold evaluation.
- Persist emitted alert state to prevent duplicate spam.
- Add tests for threshold crossing and recovery behavior.

## PR 7: DCA Reminders

- Add DCA reminder schedules.
- Add due-reminder evaluation.
- Persist acknowledgement or last-sent state.
- Add tests for recurring reminder behavior.

## PR 8: Profit-Taking Reminders

- Add profit threshold configuration.
- Add profit-taking reminder evaluation.
- Persist emitted alert state.
- Add tests for threshold and duplicate suppression behavior.

## PR 9: Scheduler Integration

- Wire APScheduler jobs to providers, evaluators, storage, and notifications.
- Add graceful startup and shutdown behavior.
- Add integration tests around job wiring where practical.

## PR 10: Docker Runtime

- Add Dockerfile.
- Add Docker Compose configuration.
- Document local container usage.
- Keep the service as a single lightweight process.

## PR 11: Hardening and Operations

- Add retry and timeout behavior where needed.
- Improve logs for failed data fetches and notification delivery.
- Add backup guidance for SQLite.
- Review docs for scope creep.

## Deferred Unless Explicitly Approved

- Additional notification channels.
- More fund data providers.
- Import/export helpers.
- CLI convenience commands.

These deferred items must still avoid RSI, web platform behavior, and automatic trading.
