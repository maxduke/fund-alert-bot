# Investment Plan Enhancement Implementation

> Status: accepted implementation plan; implementation has not started.

This document turns the decisions in the
[Investment Plan Enhancement Guide](investment-plan-guide.md) into a minimal
implementation plan for the existing service. Domain definitions live in
[`CONTEXT.md`](../CONTEXT.md), and the non-obvious decisions are recorded in
[`docs/adr`](adr/).

## Scope

Add:

- Drawdown Add Plans that bind one Reference ETF to one Investment Feeder Fund;
- confirmed-close tier state and aggregated reminders;
- Reference-ETF realtime before-close pre-alerts;
- informational SMA context;
- Position Snapshots plus low-maintenance estimates from scheduled fixed DCA;
- cost-based Price-Gain Reminders using feeder-fund NAV;
- read-only plan and position status.

Keep existing reminder-only Normal DCA, simple drawdown, notification adapters,
and stored price-gain rules compatible. Do not add trading, brokerage access, technical
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
- the existing after-close and before-close scheduler jobs call the Drawdown Add
  Plan evaluator;
- one additional in-process APScheduler job coordinates next-morning feeder-fund
  NAV work without adding a worker or service;
- Telegram, Bark, ntfy, and webhook dispatch remain unchanged.

Use the rule type `drawdown_plan`; do not add a second general-purpose rule
repository. Generic allocation tracking and rebalancing are deferred from v1.

## Drawdown Plan Parameters

Each plan binds a `cn_etf` Reference ETF to a `cn_open_fund` Investment Feeder
Fund. The Reference ETF supplies every drawdown and trend input. The feeder fund
identity links the reminder to the actual position and later supplies NAV for
position and Price-Gain calculations. The command is:

```text
/add_drawdown_plan <reference_etf_symbol> <feeder_fund_symbol> <name> <tiers> [lookback:<calendar_days>]
```

Do not accept asset-type arguments: persist the rule's existing asset type and
symbol fields as `cn_etf` and the Reference ETF symbol, and store the Investment
Feeder Fund symbol in params. Use shell-style quoting for a display name with
spaces. Require six-digit v1 symbols for both instruments. V1 accepts only
domestic A-share ETF feeder pairs; QDII and other non-CN valuation calendars are
out of scope. Reject a known non-domestic fund; when metadata is unavailable,
the stronger confirmation must explicitly include the user's statement that the
fund follows the domestic A-share valuation calendar.

Store:

```json
{
  "investment_fund_symbol": "<six-digit fund code>",
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

- a six-digit Reference ETF symbol and distinct six-digit Investment Feeder Fund
  symbol;
- a non-empty display name;
- positive `lookback_days`;
- between 1 and 50 tiers in strictly ascending drawdown order, keeping Telegram
  actions and messages within channel limits;
- both fully rendered all-tier pre-alert and confirmed-close messages within
  Telegram's 4,096-character limit;
- unique finite drawdowns strictly between zero and one;
- positive finite amounts;
- `sma_window >= 2`;
- `sma_slope_window >= 1`.

Use the existing numeric convention: integral inputs remain integers and other
finite values remain floats. No new money or decimal abstraction is needed for
reminder-only RMB amounts.

Parse tiers from the required comma-separated `<percent>:<amount>` token. Parse
only the optional trailing `lookback:<positive integer>` token; reject unknown,
duplicate, or misplaced options. Apply `lookback_days=365`, `sma_window=250`,
and `sma_slope_window=20` when absent rather than exposing SMA arguments in the
Telegram command.

Display `sum(tier.amount)` as the maximum one-cycle capital commitment in every
creation preview. This is arithmetic validation of the user's plan, not an
investment recommendation or an automatic cap.

## Market-Data Contract

Drawdown Add Plans request Reference ETF daily history from
`fund_etf_hist_em(..., adjust="qfq")`. Do not use the provider's current
unadjusted Sina fallback unless it can later guarantee and report equivalent
forward-adjusted semantics. This restriction applies only to the new plan;
existing rules retain their current compatibility behavior.

The implementation PR pins a tested AKShare version (or adds an equivalent
reproducible dependency lock) instead of allowing an unbounded provider upgrade.
Adapter tests assert the exact endpoint arguments and normalized fields so an API
shape change fails in CI rather than silently changing reminder calculations.

The plan data request and per-run cache key must distinguish the `qfq` basis from
existing unadjusted history. Normalized results must retain enough source and
price-basis metadata for logging and validation. Before evaluation:

- require the requested symbol and `qfq` basis;
- normalize dates and daily granularity;
- sort ascending and keep the last duplicate for a date;
- coerce closes, then reject confirmed history if any retained close is missing,
  non-finite, or non-positive;
- require the expected confirmed trading date after close;
- require a positive finite current-session ETF quote with evidence of trading
  before close (`volume > 0` or `amount > 0`);
- require the quote's previous close to match the latest confirmed `qfq` close
  within an explicit price tolerance;
- fail the evaluation on missing, stale, suspended, or ambiguous data.

The Reference ETF realtime price comes from a bounded per-symbol Eastmoney quote
request and is never inserted into confirmed history. Forward adjustment keeps
the current price unchanged, so it shares the current-price basis of the `qfq`
series. The Investment Feeder Fund uses `fund_open_fund_info_em` with
`indicator="单位净值走势"`; only unit NAV is used for position and Price-Gain
calculations, and its published date must travel with the value.

Both accepted realtime responses must supply a parseable source quote timestamp.
Persist it as timezone-aware `quote_time`; never replace a missing source date
with the Bot's current date. Validate the AKShare Sina trading-calendar payload
before either phase: the requested date must lie
within the returned calendar's minimum and maximum coverage. A date beyond its
maximum is calendar unavailability, not a closed market; new plan state remains
unchanged and the phase-level notice is eligible. Existing legacy weekday
fallback behavior remains unchanged.

Do not label `fund_open_fund_daily_em` as a provider fallback because it shares
the Eastmoney failure domain. Do not use Sina fund-scale/ETF data or Xueqiu basic
metadata as feeder-fund unit NAV. V1 has one acceptable exact-date NAV provider;
after its configured retries fail, leave every affected calculation pending and
include it in the aggregated next-morning Data Availability Notice. Do not add a
per-fund-company scraper, manual NAV command, or generic provider framework.

If the bounded per-symbol Eastmoney realtime request is unavailable, make one
bounded per-symbol Sina request for a Fallback Pre-Alert only. Validate the
exchange-prefixed symbol, source quote time, positive finite latest price,
non-zero trading activity, and previous close against the last confirmed `qfq`
snapshot for the expected prior trading date. Do not use `fund_etf_hist_sina`
for peak, SMA, or close confirmation.

Collect provider outcomes across every plan before dispatch. If no acceptable
source remains for a plan, add it to the phase's affected list. Reserve at most
one `data_unavailable` event with key
`data_unavailable:<phase>:<trading_date>`, containing all affected plan IDs and
display names, source outcomes, check time, and last reliable data dates. Do not
include raw exceptions or secrets. Reserving this operational event never creates
a cycle or Tier Record.

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
- the distance to the active cycle's locked peak date;
- the history needed to examine closes since the last persisted cycle
  evaluation.

With the defaults, trend slope requires 270 valid closes even though the
drawdown search window is 365 calendar days. Define that window with inclusive
endpoints: `start = evaluation_date - (lookback_days - 1) days`. Fetching extra
data does not expand the drawdown search window. If history is shorter than
`lookback_days`, use all valid since-listing closes, record and display the
shorter coverage, and keep SMA fields unavailable until their observation
requirements are met.

The evaluator receives normalized frames and contains no AKShare-specific code.

## Drawdown Cycle State

Add only the state that cannot be represented safely by notification history.

### `drawdown_cycles`

Store:

- primary key;
- `rule_id`;
- locked peak date;
- initial peak price for audit and latest refreshed `qfq` peak price;
- last evaluated closing date;
- optional end date;
- creation/update timestamps.

Allow one active cycle per rule. Its database ID and locked peak date—not a
time-varying forward-adjusted price string—identify its Tier Records.

### `drawdown_tier_records`

Store:

- primary key;
- cycle ID;
- canonical tier key plus numeric drawdown and amount;
- record source (`close_confirmed` or `user_marked_added`);
- confirmation data date or user-action date;
- aggregate alert-event ID;
- creation timestamp.

Enforce one row per cycle and canonical tier key. The canonical string avoids
using a binary float as the deduplication identity.

On first evaluation, lock the most recent occurrence of the maximum valid `qfq`
close in the configured calendar lookback. On later runs:

- refresh the adjusted price for the locked peak date on the current `qfq`
  basis without changing the cycle identity;
- lookback expiry never changes the peak, cycle, or Tier Records;
- a confirmed close above the refreshed peak starts a new cycle;
- an equal close starts a new cycle only when at least one confirmed close since
  the locked peak was below it, using an explicit float tolerance for equality;
- repeated equal closes without an intervening decline do not create new cycles;
- when recovering after downtime, scan closing history to find the latest cycle
  boundary, but create close-confirmed tier records only from the latest closing
  snapshot;
- never replay drawdowns that crossed and recovered entirely while offline.

When one close crosses several untriggered tiers, insert every Tier Record and one
`alert_events` row in one transaction. The alert payload contains
the individual tiers, total incremental amount, current and peak data, trend
context, source, and data date.

## Before-Close Evaluation

Run once at the existing `BEFORE_CLOSE_CHECK_TIME=14:50`; do not add an interval
poller. The result is a current-session estimate near the close, not a confirmed
closing value. Persist `quote_time` in the payload and state that a crossing
after this check is handled only by the after-close evaluation.

For every plan's `cn_etf` Reference ETF:

1. Fetch confirmed `qfq` closing history through the plan-specific price basis,
   without inserting the realtime row.
2. Initialize a missing Drawdown Cycle from that confirmed history, or refresh
   the active cycle's locked peak value, then calculate drawdown, SMA, and slope.
   Cycle initialization alone inserts no Tier Record or alert event.
3. Fetch a positive finite quote for the exact ETF symbol and require evidence
   of current-session trading.
   If Eastmoney fails, apply the stricter Sina Fallback Pre-Alert validation.
4. Calculate estimated drawdown and price-to-SMA distance from the realtime
   price.
5. Aggregate unconfirmed crossed tiers into at most one pre-alert per plan and
   trading date.
6. Include Telegram actions for all tiers, partial selection, and no action,
   plus the exact `/mark_added <plan_id> <tier_percentages>` fallback and a
   prominent warning that this records only a completed user action.
7. Do not insert Tier Records automatically; the only allowed state mutation is
   initialization of a missing cycle from confirmed history.

Telegram button callbacks and `/mark_added` share one handler. Validate the
authorized user and chat, plan, active cycle, eligible alert event, and each
named tier. Require a second confirmation for all-tier and partial-tier button
actions. The callback payload carries only a stable event/action identity; load
amounts and eligible tiers from SQLite rather than trusting Telegram data.

The confirmation displays the selected tiers and configured gross total, then
requires **actual amount matches** or **amount differs; sync later**. Only the
matching branch may create a Manual Add Estimate. The differing branch inserts
the selected Tier Records, sets the Position Sync requirement, and stores no
guessed amount, units, fee calculation, or pending estimate.

Require the final Manual Add Confirmation on the alert's Asia/Shanghai market
date. A later callback or `/mark_added` returns an expiry message and a
`/sync_position` instruction; it does not accept a guessed or backdated
subscription date. This keeps the no-date command safe and avoids a transaction
entry workflow.

In one transaction, insert any missing `user_marked_added` Tier Records for a
pre-alert, retain any existing close-confirmed Tier Records, and create one
Manual Add Estimate for the selected tiers. The estimate snapshots their gross
total, shared Fund Subscription Fee, and action timestamp for later dated-NAV
processing. Unique cycle-and-tier action identity makes commands, repeated
callbacks, and restart idempotent. The handler does not place or verify an
order. Selecting no action creates no Tier Record or position estimate.

Create the Manual Add Estimate only for a `READY` plan. For `SETUP_REQUIRED`,
render a distinct record-without-estimate action and confirmation. It inserts the
selected Tier Records and sets `position_sync_required_since`, but changes no
units, cost, or pending estimate. The reply requires a current post-purchase
Position Sync. This branch remains idempotent and never consumes an unselected
tier.

If the final confirmation occurs before the configured cutoff, default the
effective date to the current confirmed fund open day. At or after the cutoff,
require the user to choose whether the real subscription was submitted before
or after cutoff; do not use the callback timestamp as execution evidence. The
after-cutoff choice resolves to the next confirmed fund open day. In every case,
require unit NAV for the resolved exact date before applying the estimate.

The action reply states that the Manual Add Estimate is recorded and waiting for
dated NAV. When the next-morning job applies it, atomically update the Position
Estimate and reserve one `manual_add_settled` alert event keyed by the manual-add
occurrence ID. Its payload contains plan/fund identity, selected tiers, gross
amount, fee snapshot, action/effective dates, exact dated unit NAV and source,
estimated added units, new average cost, and estimated label. Never describe it
as platform-confirmed execution.

An occurrence in `reconciled_by_sync` cannot apply or reserve a settlement
notice. Once position application commits, delivery failure never rolls it back;
the existing delivery retry path retries only the event. Repeated NAV jobs or
restart return the same applied state and event identity.

Represent actions in the stored alert payload so Telegram can render inline
buttons while Bark, ntfy, and webhook messages keep the textual command
fallback. Add only the existing library's callback-query handler; do not create
a second Telegram application or notification framework.

The `cn_open_fund` Investment Feeder Fund does not need a realtime quote. A
pre-alert event expires at close and is never selected for later retry.

Existing simple drawdown rules retain their current before-close semantics.

After all plans are attempted, send one before-close Data Availability Notice if
both Eastmoney and Sina failed validation for any plan. Continue normal handling
for plans with valid data. The notice says that no tier decision was made for the
affected plans and that the user should check their own platform.

## Next-Morning Fund NAV Job

Register one daily feeder-fund job with default
`FUND_NAV_PROCESS_TIME=08:30` Asia/Shanghai; keep it
separate from the `17:10` Reference ETF confirmed-close job. Run it on every
calendar day, including weekends and holidays, because a prior open day's late
NAV may still need processing.

For each pending contribution occurrence, resolve its exact effective fund open
day from the validated calendar and configured holiday/cutoff policy. Process it
only after that day has completed and a matching dated unit NAV exists. For each
position-linked Price-Gain Rule, evaluate only a new NAV date for the latest
completed confirmed open day. Persist last evaluated NAV date so weekend,
holiday, restart, and repeated job runs are idempotent.

No new NAV is expected for a closed date. Calendar unavailability leaves the
affected work pending. A missing NAV creates a Data Availability Notice only
when the exact confirmed completed open day should already have published one.
Aggregate affected fund symbols and expected NAV dates into one event keyed by
the processing date; never emit one notice per occurrence. Retain pending work
and retry on the next run without applying stale NAV.

## Initial Plan Evaluation

Before storing, the command creates a short-lived in-memory confirmation draft
scoped to the authorized user and chat. Resolve exact provider codes, instrument
types, and names where possible, then show both instruments, rule configuration,
Plan Readiness, and the latest read-only drawdown preview. A code/type mismatch
is a hard error. Provider or market-data unavailability lists unverified fields
and requires the stronger explicit manual-verification callback; it never
guesses a feeder relationship.

Only a valid callback stores the enabled rule. Use an opaque short callback
token, an explicit expiry, and one-process memory rather than adding a draft
table. An expired or restart-lost token returns a rerun instruction. Store the
created rule ID with the live draft so a repeated callback is idempotent. Before
insert, reject an existing enabled rule using either the same Reference ETF or
the same Investment Feeder Fund. Confirmation creates no cycle, Tier Record, or
alert event.

Do not require feeder-fund position setup for plan creation. Derive `READY` only
when the shared Fund Subscription Fee and an initial Position Snapshot—including
valid `0, 0`—both exist; otherwise derive `SETUP_REQUIRED` and list every missing
item. This readiness affects only position updates, never Reference ETF
evaluation, tier confirmation, or reminders. Do not persist a second status that
can drift from the underlying setup.

The first scheduled plan evaluation may initialize the cycle from confirmed
history without recording tiers. This allows a first-day before-close pre-alert.
The first successful confirmed-close scheduled evaluation records every open
tier still reached by that close in one aggregated reminder; it does not replay
crossings that recovered before the plan existed.

## Status Reads

Separate state calculation from state mutation so the same evaluator output can
serve commands safely.

- `/plans` groups known Investment Feeder Funds from Drawdown Add Plans, enhanced
  DCA rules, Position Snapshots, and position-linked Price-Gain Rules. It shows a
  concise DCA, drawdown/next-tier, position-accuracy, and Price-Gain summary; it
  never expands notification history. Position accuracy includes the last sync
  date and count of applied estimates since that sync.
- `/check` calculates and displays detailed Drawdown Add Plan and position
  status without reserving alerts or changing stored state.
- Scheduled checks explicitly request state mutation and notification
  reservation.

For each plan, expose current price and date, Recent Peak and date, drawdown,
available trend fields, current-cycle triggered tiers, next tier, and distance to
that tier. If all tiers are triggered, say so rather than inventing another
level. Render tier state as `open`, `reminded_unrecorded`, or `add_recorded` by
combining Tier Records with Manual Add Confirmations; never use a generic check
mark that could be mistaken for execution. A realtime crossed tier is “pending
close,” not triggered.

### Persistent market-data cache

Store normalized confirmed daily rows in SQLite keyed by symbol, asset type,
price basis, and date. Store exact feeder-fund unit NAVs separately by fund and
NAV date. Do not store raw provider payloads. The first evaluation backfills the
required drawdown and trend range; later after-close evaluations refresh the
full required QFQ history window (unadjusted history uses a small overlap) and
upsert it. Before-close evaluation reads the last confirmed rows and still fetches one realtime quote per ETF. The plan-status
portions of `/plans` and `/check` use local confirmed rows by default;
`/plans refresh` is the explicit provider-refresh escape hatch. A cached row is never used as an
official close unless its date matches the scheduled confirmed date, and every
status includes the data date.

For a known feeder fund without an `auto` Price-Gain rule, include a Telegram
**Set gain thresholds** callback. It returns a command template with trusted fund
identity and an explicit threshold placeholder; it neither supplies percentages
nor creates a rule. Other channels render the text template. `/plans`, its
callback, and `/check` are read-only until the user submits a separate valid
`/add_profit` command.

Also display the derived `READY` or `SETUP_REQUIRED` state. Persist one per-fund
`position_sync_required_since` marker only when a user records a manual addition
that cannot be estimated because setup is incomplete. Any later successful
Position Sync explicitly replaces the current platform position and clears that
marker; do not reconstruct or apply the earlier purchase afterward.

## Scheduled DCA position estimates

Keep the existing three-argument `/add_dca <name> <weekday> <amount>` command
compatible as a reminder-only rule. Add the enhanced form:

```text
/add_dca <fund_symbol> <name> <weekday> <gross_amount> <fee> [holiday:next|holiday:skip]
```

Distinguish the legacy three-argument form from the enhanced five- or six-
argument form. Persist an enhanced rule with `asset_type="cn_open_fund"`, the
six-digit feeder-fund code in the rule symbol, and normalized `weekday`, positive
gross `amount`, and `holiday_policy` in params. Use the existing name field and
shell-style quoted-name parsing. Reject unknown, duplicate, or misplaced
optional tokens. Existing legacy rules retain `asset_type="dca"` and their
current storage shape.

Reject an enhanced rule when available metadata identifies a QDII or other
non-domestic valuation calendar; do not apply CN holiday semantics to it.

Reject a second enabled enhanced rule with the same feeder-fund symbol and
weekday. Different weekdays remain independently configurable.

The enhanced rule stores an Investment Feeder Fund symbol and gross amount. Its
fee argument initializes or validates the shared fee setting for that fund,
using exactly one form: `rate:<percent>%` or `fixed:<RMB>`. Store a percentage as
its fraction after validating a finite non-negative value; validate a finite
non-negative fixed fee and require it to be lower than an occurrence's gross
amount before calculation. Do not attempt to discover platform discounts or
model management, custody, or sales-service fees already reflected in NAV.

The same fund-level fee is authoritative for fixed DCA and manual drawdown
additions. A confirmed `/mark_added` creates a Manual Add Estimate only when the
plan is `READY`. Reject an `/add_dca` fee that conflicts with an existing setting
and direct the user to `/set_fund_fee <fund_symbol> <fee>`. A fee update affects
only later occurrences: copy the current fee form and value into each occurrence
when it is created, so pending or applied history is not reinterpreted.

Allow `/set_dca_amount <rule_id> <new_amount>` for an enabled DCA rule. Update
only `rules.params_json.amount` while retaining the rule ID. Never rewrite a
row in `scheduled_dca_occurrences`: each occurrence is the authoritative
snapshot of its due-date gross amount and fee. If an occurrence already exists
when the command runs, reminder reconstruction and later unit estimation must
use that occurrence's stored amount rather than the rule's new amount. Reject a
new amount that is non-finite, non-positive, or not greater than the fund's
fixed subscription fee. Reminder-only DCA rules use the same command but have no
position estimate.

Store the feeder fund's sales-platform subscription cutoff beside its shared
fee, using `15:00` for this initial configuration and allowing an explicit
`/set_fund_cutoff <fund_symbol> <HH:MM>` correction. A change applies only to
future manual confirmations; snapshot the cutoff decision and effective date in
each Manual Add Estimate.

Accept `holiday:next` and `holiday:skip`, defaulting to `holiday:next` for an
enhanced rule. Keep the original due date in the occurrence identity. With
`next`, resolve the first confirmed fund open day on or after the due date and
require a feeder-fund unit NAV whose date matches it exactly. With `skip`, mark a
non-open due date skipped without changing the position. If the calendar or
expected NAV is unavailable, leave the occurrence pending and emit the existing
aggregated data notice; do not fall back to weekday assumptions or stale NAV.

The command response repeats fund code, normalized weekday, gross amount, shared
fee, holiday policy, `15:00` cutoff, and derived Plan Readiness so the user can
spot an incorrect configuration immediately.

Store one Position Snapshot per Investment Feeder Fund with an update timestamp
and whether later DCA changes make it estimated. Require either positive units
and positive average unit cost or the exact fully closed pair `0, 0`; reject
mixed zero and positive values. `/sync_position` replaces units and average cost
with the user-supplied sales-platform values and clears the estimated marker. Its
command is `/sync_position <fund_symbol> <units> <average_unit_cost>`.

Before the first Position Snapshot, create scheduled occurrences and reminders
but leave every estimate pending without changing units or cost. Require an
explicit Position Sync, including `0, 0` for a genuinely empty position, then
reconcile the displayed pending occurrences through the same all-included or
none-included flow. Never infer that a missing snapshot means zero holdings.

Before mutation, query all pending scheduled DCA and Manual Add Estimates for
that feeder fund and return a sync preview listing source, due/action date, and
gross amount. With no pending items, apply the validated snapshot directly. With
pending items, require `all included`, `none included`, `partial`, or cancel:

- `all included` atomically replaces the position and marks the displayed pending
  occurrences `reconciled_by_sync` so they can never apply later;
- `none included` atomically replaces the position but leaves every item pending;
- `partial` cancels without mutation and tells the user to sync again after every
  displayed item settles;
- cancel mutates nothing.

Store a stable sync-preview identity and reject a callback whose pending set no
longer matches, rather than applying a stale decision. Repeated callbacks return
the committed result. Clear `position_sync_required_since` only when the
corresponding unestimated manual additions are all included. The none-included
choice explicitly means that no displayed item is represented by the snapshot.
If the snapshot contains only some displayed items, require cancellation and a
later sync; do not add partial-reconciliation mutation in v1.

Reuse each durable DCA reminder occurrence as the identity for at most one
Scheduled DCA Estimate. Creating the occurrence records only an assumption from
the fixed schedule, not verified execution. Keep the state needed to distinguish
`pending`, `skipped`, `applied`, and `reconciled_by_sync`, resume a delayed NAV
calculation, and prevent duplicate application. This is not a general purchase
ledger.

The reminder includes only a **Deduction failed / not executed** button and
`/dca_skip <rule_id> <due_date>` fallback; normal execution requires no user
action. The schedule creates the occurrence before dispatch, so notification
failure never controls position-estimate state. A skip is idempotent and may
change only a pending occurrence. An applied occurrence returns a Position Sync
instruction rather than implementing estimated-purchase reversal logic.

For a non-skipped occurrence, select its confirmed effective open day and
require the feeder-fund unit NAV for that exact date, then calculate:

```text
rate:  net_amount = gross_amount / (1 + fee_rate)
fixed: net_amount = gross_amount - fixed_fee
estimated_added_units = net_amount / unit_nav
```

Add estimated units to the prior units and the gross amount to the prior total
cost, then derive average unit cost. Apply the occurrence once in a SQLite
transaction, retaining the due date, NAV date, and estimated status across
restart. If the NAV is missing or stale, leave the occurrence pending and do not
guess. Position value is `units * latest published unit NAV`; never substitute
the Reference ETF price or cumulative NAV.

Do not reserve an alert event for a successfully applied scheduled DCA estimate.
Expose its last due date, effective NAV date, gross amount, estimated added
units, and applied status through `/plans` and structured logs. This quiet path
does not suppress a DCA Exception, Data Availability Notice, Position Sync
requirement, or Price-Gain Reminder produced by the same run.

Also expose `last_synced_at` and the count of applied estimates since that sync.
Do not add another scheduled reminder: the user syncs after a redemption,
distribution/reinvestment, unrecorded manual purchase, fee discrepancy, or
visible mismatch with the sales platform.

The calculated units remain an estimate until `/sync_position`: the bot has no
sales-platform execution or registrar confirmation. A platform may handle a
holiday or failed deduction differently; every estimate therefore shows both
the scheduled due date and selected NAV date.

Extend the existing `/add_profit` cost token with `auto` only for
`cn_open_fund`. Resolve `auto` from the fund's current positive-unit Position
Snapshot or Position Estimate at evaluation time and calculate
`unit_nav / average_unit_cost - 1`. Carry the position accuracy label and NAV
date into the result and message. Missing position, zero units, invalid cost, or
missing/stale NAV fails that evaluation cleanly. Numeric-cost rules remain
unchanged and require no migration.

Store thresholds on the individual rule and never create a global Price-Gain
threshold setting. Validate positive finite, unique, strictly ascending user
percentages. Permit at most one enabled `auto` rule per feeder-fund symbol to
avoid overlapping position-linked reminders. Do not migrate, disable, or delete
legacy numeric-cost rules.

Do not seed a threshold list or implicitly create an `auto` rule from a Drawdown
Add Plan, DCA rule, Position Snapshot, example, or deployment configuration. The
user creates it explicitly through `/add_profit` during real use. A feeder fund
without such a rule still tracks its position but performs no position-linked
gain evaluation.

After storing an `auto` rule, perform a read-only preview using the latest
validated position and dated unit NAV. Return all currently reached thresholds
and the exact-or-estimated gain label without reserving an alert or mutating
threshold state. Data unavailability leaves the saved rule intact and reports
that preview is unavailable.

On the first successful scheduled evaluation, atomically record every currently
reached open threshold and one aggregate alert event. Later evaluations use the
same aggregation for several newly reached thresholds. Preserve individual
canonical thresholds in the payload and deduplication records. Evaluate only the
latest valid snapshot; do not replay a threshold crossed and recovered before
the rule existed or during unavailable data.

Do not use the changing numeric average cost as alert identity: routine DCA
would otherwise reset deduplication every week.

Persist a Position Cycle identity per feeder-fund position. Positive-to-positive
updates, including partial-redemption Position Syncs, preserve it. A transition
to exact `0, 0` closes it. The next zero-to-positive Position Sync or estimated
contribution creates a new cycle. Build position-linked alert keys from rule ID,
Position Cycle ID, and canonical threshold, so each threshold alerts once per
cycle. Reminder delivery never changes position or cycle state.

Add Price-Gain actions to the stored alert payload. Telegram renders partial
redemption, full close, and no-action buttons; other channels render the
`/sync_position` fallback. Partial redemption returns a prefilled command but
still requires user-supplied current units and average unit cost. Full close
requires a second confirmation and then routes through the same Position Sync
validation with exact `0, 0`. Choosing no action mutates no state. Do not
calculate or recommend redemption units, amount, or percentage.

## Notification Recovery

Keep confirmed business state separate from delivery state:

- close-confirmed Tier Records remain confirmed if delivery fails;
- pending and failed aggregate drawdown events are selected again on a later
  scheduled run or startup until at least one enabled channel succeeds;
- pending and failed Manual Add Settlement Notices retry without reapplying the
  position estimate;
- pending and failed Position-Linked Price-Gain Reminders retry without
  reopening recorded thresholds;
- pending and failed simple drawdown, fixed-cost Price-Gain, and DCA reminders
  retry during the next morning NAV process or application startup;
- the one-time delivery-state migration derives its boundary from the first
  attempted event in that SQLite database, preserves later pending and all
  failed events, and replaces ambiguous older rows with one `/check` notice
  rather than replaying stale reminders;
- when no attempted event exists, treat all unattempted pre-migration standard
  rows as ambiguous and use that same notice instead of guessing;
- re-reserving a failed event preserves its prior attempt timestamp so a second
  crash remains distinguishable from ambiguous migration history;
- a recovered DCA action requires the event's persisted fund symbol to match
  the current occurrence, preventing deleted SQLite rule IDs from being reused;
- expired pre-alerts are not retried;
- Data Availability Notices are deduplicated by phase and trading date;
- current delivery semantics remain unchanged: success on any enabled channel
  marks the event delivered.

Do not introduce a queue or worker. A small SQLite query before scheduled
dispatch is sufficient.

## Rule lifecycle and logging

For `drawdown_plan`, enhanced DCA, and position-linked Price-Gain rules, route
`/del` to a soft disable rather than deleting dependent state. Disabling stops
future evaluations or occurrences but leaves already-created pending
contributions eligible for settlement or Position Sync reconciliation. Keep
legacy deletion behavior unchanged.

Use the existing logging stack with structured key-value fields; add no logging
dependency. Each plan evaluation logs `rule_id`, `symbol`, `evaluation_date`,
source, basis, `quote_time`, latest and peak prices, drawdown, newly crossed
tiers, SMA context, cycle/tier reservation result, and notification result.
Position work logs fund symbol, occurrence identity and state transition,
effective NAV date, NAV source, whether position application committed, and
notification result. Never log tokens, webhook URLs, or notification secrets.

## PR Plan

### PR 1 — Trend calculations

- Add the pure SMA, distance, and slope functions in `rules/drawdown_plan.py`.
- Add focused tests for exact values, ordering, duplicates, NaN handling,
  positive/negative slope, and partial history.
- No database, scheduler, or Telegram changes.

### PR 2 — Drawdown Plan evaluator and persistence

- Add plan validation and pure evaluation results.
- Add the `qfq` ETF-history request, distinct cache basis, source/date
  validation, and fail-closed provider tests.
- Add Sina fallback-quote normalization and continuity tests without allowing it
  into confirmed history.
- Validate trading-calendar coverage so an out-of-range date is unavailable,
  not silently classified as a holiday.
- Add cycle and tier-record tables and atomic reservation helpers.
- Add confirmed-close aggregation, latest-snapshot downtime behavior, payloads,
  formatting, and retry selection.
- Add focused evaluator/storage tests, including restart behavior.

### PR 3 — Fund settings and position foundation

- Add the minimal fund settings and Position Snapshot tables.
- Add `/set_fund_fee`, `/set_fund_cutoff`, and basic `/sync_position` with exact
  `0, 0` close semantics.
- Add exact-date feeder-NAV normalization without scheduling or gain rules.
- Show exact/estimated status, last sync date, and latest dated position value.
- Add focused validation, migration, and restart tests.

### PR 4 — Drawdown Plan commands, scheduling, and manual additions

Deliver this phase as two reviewable changes without changing its final scope:

- **PR 4A:** pairing-confirmed `/add_drawdown_plan`, confirmed-close scheduling,
  notification recovery, and read-only `/plans`/`/check` status;
- **PR 4B:** before-close pre-alerts, `/mark_added` and Telegram actions, cutoff
  choices, pending Manual Add Estimates, Position Sync reconciliation, and the
  exact-date feeder-NAV settlement job.

- Add `/add_drawdown_plan`, `/mark_added`, and `/plans` using the fixed command
  shape, optional `lookback`, and internal SMA defaults.
- Preview identities, readiness, current tiers, and maximum one-cycle total;
  save only after explicit pairing confirmation.
- Add Telegram actions, same-date Manual Add Confirmation, command fallback,
  exact-amount confirmation, pending Manual Add Estimates, cutoff handling, and
  Position Sync reconciliation.
- Integrate pre-alert and confirmed-close paths into existing scheduler jobs,
  initializing a missing cycle from confirmed history when needed.
- Add the configurable daily feeder-NAV job for exact-date manual-add settlement
  and its independently retryable completion notice.
- Extend `/check` and `/plans` with read-only state and aggregate data failures by
  phase.
- Add command, callback, freshness, holiday, idempotency, atomicity, delivery,
  and restart tests.

### PR 5 — Enhanced DCA position estimates

- Preserve the legacy DCA command and add the exact enhanced command with fund,
  fee, and holiday policy.
- Reject duplicate enabled rules for the same fund and weekday.
- Create one occurrence per due date, default to assumed execution, and add only
  the failure/skip action.
- Extend the feeder-NAV job to apply each non-skipped exact-date estimate once;
  successful routine application remains silent.
- Extend Position Sync reconciliation and `/plans` for DCA occurrences.
- Add fee, calendar, holiday, stale-NAV, skip, sync, and restart tests.

### PR 6 — Position-linked Price-Gain Reminders

- Accept `auto` cost only for `cn_open_fund`, preserving numeric-cost rules.
- Keep thresholds user-defined per fund, permit one enabled `auto` rule, and
  create no defaults or implicit rule.
- Add Position Cycle deduplication, creation preview, multi-threshold
  aggregation, and exact/estimated labeling.
- Extend the feeder-NAV job and `/plans`; add partial-sync, twice-confirmed full
  close, and no-action Telegram conveniences without choosing a redemption.
- Add threshold, position-cycle, missing/stale-NAV, synchronization, delivery,
  and restart tests.

Every PR must preserve legacy behavior and must not add allocation alerts or a
transaction ledger.

## Acceptance Checklist

- Existing tests and legacy rule behavior remain compatible.
- Tier amounts are incremental and multi-tier gaps produce one totalled reminder.
- A multi-tier total includes only open tiers newly reached in that evaluation;
  recorded earlier tiers and upward recovery crossings contribute nothing.
- A tier triggers once per stable Drawdown Cycle.
- A 365-day peak window uses deterministic inclusive endpoints: the latest
  confirmed date plus the preceding 364 calendar dates.
- The peak date remains locked through the cycle; lookback expiry never lowers
  it. An equal-price full recovery after a decline starts a new cycle, while
  repeated equal closes with no intervening decline do not.
- Reference ETF history is `qfq`; the peak value is refreshed on that basis so
  a distribution does not create a false drawdown.
- The tested AKShare interface version is reproducible, and adapter tests pin the
  endpoint parameters and normalized contract.
- Plan creation previews current reached tiers, while only the first successful
  confirmed-close evaluation records them.
- Plan creation displays the maximum sum of all incremental tiers.
- `/add_drawdown_plan` saves no rule before pairing confirmation; expired or
  restart-lost drafts leave no partial database state.
- Enabled Drawdown Add Plans form a one-to-one Reference ETF/Investment Feeder
  Fund mapping; duplicate use of either side is rejected.
- Incomplete feeder-fund setup never blocks a Drawdown Add Plan or its market
  reminders; it visibly disables only automatic position estimation.
- `/add_drawdown_plan` distinguishes Reference ETF and Investment Feeder Fund by
  argument position and never asks the user for redundant asset-type tokens.
- V1 rejects non-domestic/QDII feeder pairs rather than applying the CN calendar
  to them.
- Realtime ETF price, confirmed ETF history, and feeder-fund unit NAV remain
  distinct, date-labeled data streams.
- A trading-calendar result must cover the requested date; an out-of-range date
  is unavailable, not a holiday.
- A source with unknown or inconsistent adjustment semantics fails closed rather
  than falling back silently.
- Sina may create a clearly labeled Fallback Pre-Alert but never a confirmed Tier
  Record, peak, or MA value.
- Total source failure creates one phase-level notice without changing plan,
  cycle, tier, or position state.
- Realtime pre-alerts use realtime price and never consume confirmed tiers.
- Drawdown Add Plans run one realtime estimate at `14:50`; it displays the Bot
  fetch time, never invents an exchange quote time, and never claims to cover a
  later intraday crossing.
- Closing confirmation, Tier Records, and aggregate event are atomic.
- Every pre-alert prominently tells a user who subscribes to run the exact
  `/mark_added` fallback; unmarked pre-alert tiers remain open.
- Telegram callbacks and `/mark_added` record only explicitly selected tiers,
  are idempotent, and never place or verify an order.
- A Manual Add Estimate is created only when the user confirms that actual gross
  amount equals the selected tiers' configured total; a mismatch requires sync.
- A Manual Add Confirmation from a later date expires into Position Sync rather
  than guessing or accepting a backdated subscription date.
- A confirmed manual add creates one fee-aware, dated pending Position Estimate;
  choosing no action changes no purchase or position state.
- Applying a ready Manual Add Estimate updates the position once and reserves one
  separately retryable, clearly estimated completion notice.
- If fee or initial position is missing, a separately confirmed action may
  record selected tiers without estimating the position and must leave a visible
  Position Sync requirement.
- Manual confirmations before the configured cutoff default to the current open
  day; at or after cutoff the user must explicitly identify which side of the
  cutoff the actual submission occurred.
- SMA context is partial when necessary and never changes a tier decision.
- Status commands do not mutate plan or position state.
- Tier status distinguishes an emitted reminder from a user-recorded addition;
  neither is described as a platform-confirmed purchase.
- The `/plans` Price-Gain setup button supplies no thresholds and creates no rule
  until the user explicitly submits `/add_profit`.
- Every market-driven message includes its data date and source context.
- Missing or stale inputs cannot create an incorrect reminder.
- Position Syncs remain manual and apply only to Investment Feeder Funds.
- A fixed DCA occurrence may update only the explicitly estimated position; it
  never claims that platform execution was verified.
- Before the first Position Sync, DCA occurrences remain pending and missing
  position is never interpreted as an empty account.
- A pending occurrence can be skipped, and each non-skipped occurrence is
  applied at most once.
- Normal fixed DCA requires no acknowledgement; notification delivery never
  creates, duplicates, or proves its scheduled occurrence.
- Duplicate enhanced DCA rules for the same fund and weekday are rejected.
- Successfully applying a routine DCA estimate creates no second notification;
  exceptions and independent reminder conditions remain visible.
- Holiday handling is configured once per enhanced DCA rule; `next` preserves
  the original occurrence identity and `skip` creates no position estimate.
- Missing calendar or exact-date NAV data leaves an occurrence pending instead
  of using weekday assumptions or stale values.
- Fee-aware DCA units are labeled estimated until the next Position Sync.
- A fund-fee change affects only future occurrences and never recalculates
  historical position estimates.
- A DCA amount change retains the rule ID, affects only occurrences created
  later, and never rewrites an existing occurrence, reminder, or estimate.
- A position-linked Price-Gain Reminder uses feeder-fund unit NAV and the
  current exact-or-estimated average unit cost, never Reference ETF price.
- Routine estimated cost changes do not create duplicate gain-threshold alerts.
- The daily `08:30` feeder-NAV job runs through holidays but expects data only
  for a confirmed completed open day; closed dates are not data failures.
- Pending estimates and gain checks require exact-date NAV, survive restart, and
  never advance from a stale value.
- Feeder-fund NAV has no falsely labeled Sina or same-domain fallback; provider
  failure leaves all dependent work pending and visible.
- Position Sync explicitly reconciles all displayed pending contributions as
  included or not included; stale or duplicate confirmation cannot double-count.
- `/plans` shows the last Position Sync and number of later applied estimates.
- Position-linked gain thresholds alert once per Position Cycle; only a fully
  closed `0, 0` position followed by a new positive position opens them again.
- Several position-linked thresholds first observed in one valid evaluation
  create one reminder while retaining separate threshold records.
- Price-Gain buttons only help record a partial Position Sync, a twice-confirmed
  full close, or no action; they never choose or execute a redemption.
- Reference ETF prices never masquerade as feeder-fund NAV or exact profit.
- State and eligible notifications recover across process restart.
- Disabling a stateful plan stops future work without deleting an already-pending
  contribution or its reconciliation state.
- Structured logs contain evaluation and state-transition fields but no secrets.
- Notification dispatch continues through the existing adapters.
- Ruff and pytest pass in every implementation PR.
