# Architecture

`fund-alert-bot` is planned as a small Python service that periodically evaluates personal reminder rules and sends notifications. It does not host a web app and does not trade.

## Runtime Shape

The bot should run as a single Python process:

1. Load configuration from environment variables.
2. Open a local SQLite database.
3. Register APScheduler jobs.
4. Fetch fund or market data through AKShare-backed providers.
5. Evaluate supported reminder rules.
6. Persist alert state.
7. Send notifications through configured channels.

Docker and Docker Compose should package this same process for repeatable local deployment.

## Planned Modules

### Configuration

Responsible for reading environment variables, validating required settings, and exposing typed settings to the rest of the app.

Configuration must not contain real secrets in source control. `.env.example` should contain placeholders only.

### Data Providers

Responsible for retrieving fund and market data from AKShare and normalizing it into pandas data frames or simple internal records.

Provider code should not evaluate alert rules. It should only fetch, normalize, and report data availability errors.

### Storage

Responsible for SQLite schema management and persistence of:

- watched instruments
- alert configuration
- recent high snapshots
- reminder schedules
- notification history
- delivery status

Storage code should hide raw SQL from alert evaluation and notification modules where practical.

### Alert Evaluation

Responsible for deciding whether a reminder should be emitted.

Initial supported evaluator families:

- drawdown from recent high
- DCA reminder due
- profit-taking threshold reminder

RSI and RSI6 evaluators are explicitly out of scope.

### Scheduler

Responsible for registering APScheduler jobs, running checks on configured intervals, and handling job-level logging.

The scheduler should coordinate modules without owning business rules.

### Notifications

Responsible for formatting and sending messages through configured notification channels.

Telegram should use `python-telegram-bot`. Other channels can use small adapters backed by `requests` when needed.

Notification modules should receive already-evaluated alert events. They should not fetch market data or decide whether an alert is due.

### App Entry Point

Responsible for startup, dependency wiring, graceful shutdown, and process-level logging.

The entry point should stay thin. Business behavior belongs in the modules above.

## Data Flow

```text
Environment -> Configuration -> Scheduler
                              -> Data Providers -> Alert Evaluation
                              -> Storage
                              -> Notifications
```

Alert evaluation may read prior state from storage and write updated state after each run.

## Explicit Non-Goals

- No RSI or RSI6 alerts.
- No web UI.
- No HTTP API server.
- No automatic trading.
- No brokerage account integration.
- No PostgreSQL, Redis, Celery, Django, or FastAPI.
- No changes to `maxduke/rsi6_monitor_bot`.
