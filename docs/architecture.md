# Architecture

`fund-alert-bot` is a small Python service that periodically evaluates personal reminder rules and sends notifications. It does not host a web app and does not trade.

## Runtime Shape

The bot runs as a single Python process:

1. Load configuration from environment variables.
2. Open a local SQLite database.
3. Register APScheduler jobs.
4. Fetch fund or market data through AKShare-backed providers.
5. Evaluate supported reminder rules.
6. Persist alert state.
7. Send notifications through configured channels.

Docker and Docker Compose should package this same process for repeatable local deployment.

## Modules

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

SQLite history is bounded by a conservative retention pass at startup and
after the daily NAV process. Terminal history is normally retained for 400
days; enabled-rule windows, active-cycle peaks, latest fund NAVs, pending work,
and still-relevant deduplication state are preserved even when older.

### Alert Evaluation

Responsible for deciding whether a reminder should be emitted.

Supported evaluator families:

- drawdown from recent high
- DCA reminder due
- price-gain threshold reminder

RSI and RSI6 evaluators are explicitly out of scope.

### Scheduler

Responsible for registering APScheduler jobs, running checks on configured intervals, and handling job-level logging.

After-close market checks use the CN market calendar to skip official holidays,
with weekday fallback for ordinary reminder checks when AKShare calendar data is
unavailable. Reminder-only DCA rules remain personal weekday reminders. Enhanced
fund DCA settlement depends on a confirmed market calendar and keeps an
occurrence pending when calendar coverage is unavailable instead of guessing a
fund valuation date.

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
Notification dispatch records whether each reserved alert was delivered,
failed, or is still pending for each concrete channel target. Claims use a
short SQLite lease so overlapping jobs do not send the same target twice;
retries address only unfinished targets, and the aggregate event is complete
only when every frozen target succeeds.

## Explicit Non-Goals

- No RSI or RSI6 alerts.
- No web UI.
- No HTTP API server.
- No automatic trading.
- No brokerage account integration.
- No PostgreSQL, Redis, Celery, Django, or FastAPI.
- No changes to `maxduke/rsi6_monitor_bot`.
