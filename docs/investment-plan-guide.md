# Investment Plan Enhancement Guide

> Status: agreed design; the commands and behavior in this guide are not yet
> implemented.

This enhancement adds Drawdown Buy Plans, informational long-term trend context,
and allocation reminders to the existing local bot. Everything remains a
reminder: the bot never places an order, changes a holding, or provides financial
advice.

## Drawdown Buy Plans

A Drawdown Buy Plan associates incremental additional amounts with drawdown
tiers. Normal DCA remains separate and continues unchanged.

Add a plan with:

```text
/add_drawdown_plan <asset_type> <symbol> <name> <lookback_days> <tiers>
```

Example:

```text
/add_drawdown_plan cn_index 000510 中证A500 365 15:5000,20:10000,25:15000,30:20000,35:10000
```

User-entered percentages are converted to fractions internally. Tiers must be in
ascending drawdown order, may not repeat a drawdown, and require percentages
between 0% and 100% and positive amounts. Drawdown Buy Plans always use normalized
closing prices; there is no configurable price field.

If one close crosses several new tiers, the bot confirms each tier separately but
sends one reminder containing their incremental amounts and total. A triggered
tier means its condition was confirmed, not that the user completed a purchase.

### Cycle behavior

Each plan has a Drawdown Cycle in which each tier may trigger once. A new cycle
begins after a closing price reaches the then-current Recent Peak, including an
equal-price full recovery. A peak merely leaving the rolling lookback does not
reset tiers. The current rolling peak remains the reference for drawdown
calculation and display.

After downtime, the bot recovers the latest cycle from closing history but does
not replay reminders for drawdowns that crossed and recovered entirely while it
was offline. An untriggered tier still crossed by the latest close is handled
normally.

### Before and after close

Before close, the bot sends a provisional Drawdown Pre-Alert only when the
realtime price actually reaches an unconfirmed tier. It aggregates crossed tiers,
sends at most once per plan per trading date, and does not consume the confirmed
tier.

The realtime price supplies the estimated drawdown and price-to-SMA distance.
The Recent Peak, SMA, and SMA slope use confirmed closes. After close, the day's
closing data may create durable Tier Trigger Records and one aggregated reminder.
Existing simple drawdown rules retain their current realtime behavior.

Open-ended funds (`cn_open_fund`) have no realtime quote source, so they do not
receive Drawdown Pre-Alerts. Their tiers are confirmed only from a same-date
published NAV; an older NAV is never presented as today's realtime estimate.

A failed Drawdown Pre-Alert is not retried after market close or on a later
restart because the estimate has expired. The after-close evaluation creates the
durable reminder if closing data confirms the tier.

### Trend context

The default context uses a 250-observation SMA and a 20-observation slope:

```text
sma_window = 250
sma_slope_window = 20
```

With fewer than 250 valid closes, SMA, distance, and slope are unavailable. With
250 through 269 valid closes, SMA and distance are shown but slope is unavailable.
With at least 270, all fields are shown. Trend context never changes a tier or its
amount.

### Status commands

`/plans` shows concise Drawdown Buy Plan status and the next tier. It is not an
alias for `/list`, which continues to list all rule configuration.

`/check` shows detailed plan measurements, current-cycle tier status, and the next
tier. Drawdown Buy Plan status is read-only: running `/check` never confirms a new
plan tier. Existing legacy rule behavior remains unchanged.

## Holdings

Holdings are maintained manually; there is no brokerage synchronization.

Add a market-priced holding:

```text
/add_holding <group> <bucket> <asset_type> <symbol> <name> <units>
```

Example:

```text
/add_holding cn_index_core growth cn_etf 159915 创业板ETF 10000
```

Market Holdings support `cn_etf`, `cn_stock`, and `cn_open_fund`. A `cn_index`
instrument may be observed by a Drawdown Buy Plan but cannot be a Holding because
an index level is not an investable market value; configure the actual ETF or
fund owned instead.

Add a manually valued holding:

```text
/add_manual_holding <group> <bucket> <key> <name> <value>
```

Example:

```text
/add_manual_holding cn_index_core defensive cash 现金 100000
```

A Market Holding requires positive units and no manual value. A Manual Holding
requires a non-negative manual value, no units, and records when the value was
updated. The two valuation methods cannot be mixed.

All Drawdown Tier amounts, DCA amounts, Market Holding values, and Manual Holding
values are RMB in v1. Foreign-currency holdings stay outside allocation groups;
the bot performs no exchange-rate conversion.

Maintain holdings with:

```text
/set_holding <id> <units_or_value>
/holdings [group]
/del_holding <id>
```

`/set_holding` updates units for a Market Holding and value for a Manual Holding.
To correct group, bucket, asset identity, or name metadata in v1, delete and
recreate the holding.

## Allocation Rules

An Allocation Rule watches one bucket as a percentage of its whole Portfolio
Group:

```text
/add_allocation <group> <bucket> <target_pct> <warning_pct> <rebalance_pct>
```

Example:

```text
/add_allocation cn_index_core growth 40 45 50
```

Percentages must satisfy:

```text
0 < target < warning < rebalance < 100
```

For the example rule:

- growth at or below 45% is `NORMAL`;
- growth above 45% and at or below 50% is `WARNING`;
- growth above 50% is `REBALANCE`.

Being above the target alone does not leave `NORMAL`. In `REBALANCE`, the amount
above target is:

```text
watched_bucket_value - group_total * target
```

The reminder reports how far the watched bucket is above target but does not
claim another bucket is underweight, choose a destination, or choose which asset
to sell. It also does not change or silence any DCA reminder.

### Valuation and state

Every enabled holding must be valued before the group allocation is calculated.
Scheduled after-close evaluation requires same-date market prices or NAVs; a
missing or older value skips the whole group. Manual values remain usable with
their update dates.

Allocation transitions are evaluated only by the after-close scheduled market
job. There are no realtime allocation alerts. Holding commands only update
holding data, and `/check` only displays allocation state; neither sends or
records an allocation transition.

The first `NORMAL` state is stored silently. An initial `WARNING` or `REBALANCE`
state sends one reminder. Afterwards, every state change—including direct jumps
and recoveries—sends one reminder, while an unchanged state remains silent. This
is transition suppression, not threshold hysteresis.

Each successful evaluation commits its Allocation Snapshot, Allocation State,
and any transition notification event in one SQLite transaction so a restart
cannot lose or duplicate a transition.

An undelivered allocation reminder is retried only while its reported state is
current. A newer state supersedes the old notification. `/check` may show the
current evaluation; an older stored Allocation Snapshot is display-only and is
always labeled with its date.

## Delivery and persistence

Confirmed tier state and notification delivery state are separate. A failed
notification does not undo a confirmed tier. Pending or failed drawdown reminders
are retried after later scheduled runs or restart until at least one enabled
channel succeeds.

All market-driven alerts include their market-data date. Missing, stale, or
insufficient data is reported without creating an incorrect reminder.

## Price-Gain Reminders

The existing command and stored rule type remain unchanged:

```text
/add_profit <asset_type> <symbol> <name> <cost> <thresholds>
```

Human-facing text calls this a Price-Gain Reminder, not a profit-taking
instruction. It reports gain from the configured cost basis and points users to
allocation-based rebalancing for strategic allocation decisions. Existing rules
need no migration.
