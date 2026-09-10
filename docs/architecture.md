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

`runtime_status.py` keeps one bounded outcome record per scheduled task in
SQLite app metadata, including startup executions. It distinguishes complete
success, missing-data or delivery problems, skips, failures and interrupted
runs; a partial run never overwrites the last complete success time. `/status`
reads these records and local cache/delivery/estimate counts without fetching
market data or evaluating rules. The runtime heartbeat remains a separate
process-liveness check, not evidence that evaluations succeeded.

DCA catch-up excludes dates before each rule's creation date in the configured
timezone. Pre-creation pending occurrences left by older releases remain
available for explicit reconciliation but cannot update a position estimate.

### Notifications

Responsible for formatting and sending messages through configured notification channels.

Telegram should use `python-telegram-bot`. Other channels can use small adapters backed by `requests` when needed.

Notification modules should receive already-evaluated alert events. They should not fetch market data or decide whether an alert is due.

### Telegram Commands

`command_args.py` owns argument parsing, usage strings, immutable parsed command
types, and conversion to rule parameters. It reuses pure rule validation helpers
but does not load the Telegram command shell or storage, access a provider, or
write state. Parser tests live in `tests/test_command_args.py`.

`commands.py` owns Telegram registration, authorization, replies, confirmation
drafts, and orchestration of existing services. Its original parsing imports are
re-exported for compatibility. Integration and interaction tests remain in
`tests/test_commands.py`; moving parsers does not change callback lifecycles or
transaction boundaries. Further extraction should follow one responsibility
at a time rather than replacing the command system with a new framework.

`plan_views.py` renders the supplied plan and position snapshots for `/plans`
and the plan section of `/check`. It owns their tier-state, date, trend, and
position text, including the existing language-dependent labels. It does not
fetch data, evaluate or persist alerts, or load the command shell or storage.
Status-result types are imported only for type checking. Existing formatter
imports from `commands.py` and the existing command logger category remain
compatible. Provider-backed creation previews and Telegram callbacks stay in
the command shell. Direct view tests verify presentation and unchanged inputs;
the command integration tests continue to cover authorization and side effects.

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
