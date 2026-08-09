# Investment Plan Enhancement Implementation

> Status: agreed design; implementation has not started.

This document turns the decisions in the
[Investment Plan Enhancement Guide](investment-plan-guide.md) into a minimal
implementation plan for the existing service. Domain definitions live in
[`CONTEXT.md`](../CONTEXT.md), and the non-obvious decisions are recorded in
[`docs/adr`](adr/).

## Scope

Add:

- Drawdown Buy Plans with incremental tiers;
- confirmed-close tier state and aggregated reminders;
- realtime before-close pre-alerts;
- informational SMA context;
- manually maintained Holdings;
- one-bucket Allocation Rules and transition reminders;
- read-only plan and allocation status.

Keep existing Normal DCA, simple drawdown, notification adapters, and stored
price-gain rules compatible. Do not add trading, brokerage access, technical
indicators other than informational SMA context, a web service, or new
infrastructure.

## Reuse the Existing Shape

The implementation should extend the current paths instead of creating a second
framework:

- `rules` continues to hold type-specific JSON rule configuration;
- `alert_events` continues to hold messages and delivery state;
- `checks.py` continues to coordinate providers, rule evaluation, storage, and
  notification reservation;
- the per-run market-data cache continues to combine requests for the same
  instrument;
- the existing after-close and before-close scheduler jobs call the new
  evaluator;
- Telegram, Bark, ntfy, and webhook dispatch remain unchanged.

Use rule types `drawdown_plan` and `allocation`. An allocation rule reuses the
existing non-market rule convention already used by DCA: its group and bucket
identify the row, while its percentages live in `params_json`. Do not add a
second general-purpose rule repository.

## Drawdown Plan Parameters

Store:

```json
{
  "lookback_days": 365,
  "tiers": [
    {"drawdown": 0.15, "amount": 5000},
    {"drawdown": 0.20, "amount": 10000}
  ],
  "sma_window": 250,
  "sma_slope_window": 20
}
```

The rule always uses normalized `close`; omit `price_field`. Validate at both the
Telegram boundary and evaluator boundary:

- positive `lookback_days`;
- non-empty tiers in strictly ascending drawdown order;
- unique finite drawdowns strictly between zero and one;
- positive finite amounts;
- `sma_window >= 2`;
- `sma_slope_window >= 1`.

Use the existing numeric convention: integral inputs remain integers and other
finite values remain floats. No new money or decimal abstraction is needed for
reminder-only RMB amounts.

## Trend Calculations

Add the three requested pure calculations in `rules/drawdown_plan.py` and reuse
one private close-cleaning path:

- normalize dates;
- sort ascending;
- keep the last duplicate for a date;
- coerce prices and discard non-finite closes;
- calculate today's SMA from the last `sma_window` valid observations;
- calculate slope against the SMA ending `sma_slope_window` observations earlier.

Return partial context instead of failing drawdown evaluation:

- fewer than `sma_window` valid closes: all SMA fields unavailable;
- enough for today's SMA but not the earlier SMA: SMA and distance available,
  slope unavailable;
- sufficient data for both: all fields available.

`above_sma` means strictly above; equality is not above. Missing trend context
never changes crossed tiers or amounts.

Fetch enough calendar history for the largest rule requirement. A simple safe
range is the maximum of:

- `lookback_days`;
- twice `sma_window + sma_slope_window` calendar days;
- the history needed to examine closes since the last persisted cycle
  evaluation, including their lookback context.

The evaluator receives normalized frames and contains no AKShare-specific code.

## Drawdown Cycle State

Add only the state that cannot be represented safely by notification history.

### `drawdown_cycles`

Store:

- primary key;
- `rule_id`;
- anchor date and closing price;
- last evaluated closing date;
- optional end date;
- creation/update timestamps.

Allow one active cycle per rule. The stable cycle key—not the rolling peak
identity—is used by tier records.

### `drawdown_tier_triggers`

Store:

- primary key;
- cycle ID;
- canonical tier key plus numeric drawdown and amount;
- confirmation data date;
- aggregate alert-event ID;
- creation timestamp.

Enforce one row per cycle and canonical tier key. The canonical string avoids
using a binary float as the deduplication identity.

On first evaluation, anchor the cycle to the most recent occurrence of the
rolling maximum close. On later runs:

- lookback expiry may change the calculation's Recent Peak but not the cycle;
- a confirmed close equal to the then-current Recent Peak starts a new cycle;
- when recovering after downtime, scan closing history to find the latest cycle
  boundary, but create tier triggers only from the latest closing snapshot;
- never replay drawdowns that crossed and recovered entirely while offline.

When one close crosses several untriggered tiers, insert every Tier Trigger
Record and one `alert_events` row in one transaction. The alert payload contains
the individual tiers, total incremental amount, current and peak data, trend
context, source, and data date.

## Before-Close Evaluation

For `cn_index`, `cn_etf`, and `cn_stock` plans:

1. Fetch confirmed closing history without inserting the realtime row.
2. Calculate Recent Peak, SMA, and slope from those closes.
3. Fetch a current realtime quote dated for the trading date.
4. Calculate estimated drawdown and price-to-SMA distance from the realtime
   price.
5. Aggregate unconfirmed crossed tiers into at most one pre-alert per plan and
   trading date.
6. Do not insert Tier Trigger Records or change cycle state.

Skip pre-alerts for `cn_open_fund` because the provider has no realtime NAV. A
pre-alert event expires at close and is never selected for later retry.

Existing simple drawdown rules retain their current before-close semantics.

## Status Reads

Separate state calculation from state mutation so the same evaluator output can
serve commands safely.

- `/plans` lists concise Drawdown Buy Plan status only.
- `/check` calculates and displays detailed Drawdown Buy Plan and allocation
  status without reserving alerts or changing stored state.
- Scheduled checks explicitly request state mutation and notification
  reservation.

For each plan, expose current price and date, Recent Peak and date, drawdown,
available trend fields, current-cycle triggered tiers, next tier, and distance to
that tier. If all tiers are triggered, say so rather than inventing another
level. A realtime crossed tier is “pending close,” not triggered.

## Holdings

Add a `holdings` table with:

- ID;
- portfolio group and bucket;
- asset type, symbol/key, and name;
- nullable units;
- nullable manual value and its update timestamp;
- enabled flag;
- creation/update timestamps.

Validate exactly one valuation path:

- Market Holding: asset type is `cn_etf`, `cn_stock`, or `cn_open_fund`, units are
  positive, and manual value is null;
- Manual Holding: asset type is `manual`, units are null, and manual value is
  finite and non-negative.

`cn_index` is not a valid Holding. All values are RMB; do not add currency or FX
configuration. Holdings are changed only through the documented commands and are
never synchronized from a broker.

## Allocation Evaluation

Store allocation rule params:

```json
{
  "portfolio_group": "cn_index_core",
  "bucket": "growth",
  "target": 0.40,
  "warning": 0.45,
  "rebalance": 0.50
}
```

Validate `0 < target < warning < rebalance < 1`.

For every enabled Holding in the group:

- Market Holding value is units times its same-date confirmed close or NAV;
- Manual Holding value is its stored RMB value and carries its manual update
  date;
- one missing, non-finite, or stale Market Holding skips the whole group;
- a non-positive group total skips evaluation cleanly.

For a complete group:

```text
allocation = watched_bucket_value / group_total

NORMAL: allocation <= warning
WARNING: warning < allocation <= rebalance
REBALANCE: allocation > rebalance

trim_amount = watched_bucket_value - group_total * target
```

Calculate `trim_amount` only for `REBALANCE`. The message identifies the watched
bucket but no asset to sell and no destination bucket.

### Allocation state storage

Store only the latest complete Allocation Snapshot and state per allocation rule;
historical allocation reporting is not required. Include evaluation date, group
total, watched value, allocation, state, input data dates, and current transition
event ID.

In one SQLite transaction:

1. supersede an undelivered event for a state that is no longer current;
2. upsert the latest complete snapshot and state;
3. reserve a new transition event when required.

The first `NORMAL` state is silent. The first `WARNING` or `REBALANCE` state and
every later state change produce a reminder. An unchanged state produces none.
An undelivered event is retryable only while its state remains current.

Run this evaluator only in the after-close scheduled job. Holding commands and
`/check` never record transitions. Allocation state never changes DCA rules.

## Notification Recovery

Keep confirmed business state separate from delivery state:

- confirmed Tier Trigger Records remain confirmed if delivery fails;
- pending and failed aggregate drawdown events are selected again on a later
  scheduled run or startup until at least one enabled channel succeeds;
- expired pre-alerts are not retried;
- allocation events are retried only while their state is current;
- current delivery semantics remain unchanged: success on any enabled channel
  marks the event delivered.

Do not introduce a queue or worker. A small SQLite query before scheduled
dispatch is sufficient.

## PR Plan

### PR 1 — Trend calculations

- Add the pure SMA, distance, and slope functions in `rules/drawdown_plan.py`.
- Add focused tests for exact values, ordering, duplicates, NaN handling,
  positive/negative slope, and partial history.
- No database, scheduler, or Telegram changes.

### PR 2 — Drawdown Plan evaluator and persistence

- Add plan validation and pure evaluation results.
- Add cycle and tier-trigger tables and atomic reservation helpers.
- Add confirmed-close aggregation, latest-snapshot downtime behavior, payloads,
  formatting, and retry selection.
- Add focused evaluator/storage tests, including restart behavior.

### PR 3 — Drawdown Plan commands and scheduling

- Add `/add_drawdown_plan` and `/plans`.
- Extend `/check` with read-only detailed plan state.
- Integrate pre-alert and confirmed-close paths into existing scheduler jobs.
- Add command, scheduler, freshness, and notification tests.

### PR 4 — Holdings

- Add the Holdings table and documented holding commands.
- Add market/manual valuation and complete-group validation.
- Add storage, command, provider-error, and stale-data tests.
- Do not add allocation alerts.

### PR 5 — Allocation reminders

- Add `/add_allocation` using the existing rules repository.
- Add allocation calculation, latest state storage, transition reservation,
  superseding, retry, message formatting, and after-close scheduling.
- Extend `/check` with read-only allocation status.
- Change profit-rule human-facing text to Price-Gain Reminder.
- Add boundary, initial-state, direct-transition, recovery, restart, and delivery
  tests.

## Acceptance Checklist

- Existing tests and legacy rule behavior remain compatible.
- Tier amounts are incremental and multi-tier gaps produce one totalled reminder.
- A tier triggers once per stable Drawdown Cycle.
- Rolling peak expiry alone never re-arms tiers; equal-price full recovery does.
- Realtime pre-alerts use realtime price and never consume confirmed tiers.
- Closing confirmation, tier records, and aggregate event are atomic.
- SMA context is partial when necessary and never changes a tier decision.
- Status commands do not mutate new plan or allocation state.
- Every market-driven message includes its data date and source context.
- Missing or stale inputs cannot create a drawdown or allocation reminder.
- Holdings remain manual and RMB-only; indexes cannot be Holdings.
- Allocation evaluates one watched bucket, alerts on every state change, and
  never controls DCA or selects trades.
- State and eligible notifications recover across process restart.
- Notification dispatch continues through the existing adapters.
- Ruff and pytest pass in every implementation PR.
