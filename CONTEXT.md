# Investment Reminder Context

This context describes the personal investment reminders owned by
`fund-alert-bot`. Reminders report user-defined conditions; they are not
financial advice and never place trades.

## Reminder boundaries

**Investment Reminder**:
A notification that a user-defined investment condition has been observed.
_Avoid_: Signal, recommendation, trade instruction

**Data Availability Notice**:
An operational notification that one or more plans could not be evaluated from
acceptable current data and no investment condition was inferred.
_Avoid_: Investment Reminder, inferred no-trigger result

## Assets and market context

**Reference ETF**:
The exchange-traded fund whose adjusted closes and realtime quote describe the
market condition for a Drawdown Add Plan.
_Avoid_: Official index, Investment Feeder Fund, cost basis

**Investment Feeder Fund**:
The open-ended ETF feeder fund the user actually owns and whose unit NAV values
the position and Price-Gain Reminder.
_Avoid_: Reference ETF, official index, realtime NAV

**Feeder-Fund Unit NAV**:
The dated unit net asset value published for an Investment Feeder Fund and used
for position estimates and Price-Gain Reminders.
_Avoid_: Reference ETF price, cumulative NAV, fund scale

**Long-Term Trend Context**:
Informational price-versus-moving-average and moving-average-slope measurements
that never control a tier, amount, or sale.
_Avoid_: Buy filter, sell signal, timing rule

## Recurring contributions

**Normal DCA**:
A recurring contribution reminder whose configured schedule and amount remain
independent of drawdown and Price-Gain Reminders.
_Avoid_: Automatic investment, drawdown buy

**Scheduled DCA Estimate**:
One expected fixed-schedule contribution whose fee and dated unit NAV may update
a Position Estimate once; it is not proof of execution.
_Avoid_: Broker confirmation, automatic purchase, confirmed shares

**DCA Exception**:
The user's report that one expected contribution was not executed and must not
update the Position Estimate.
_Avoid_: Normal weekly acknowledgement, inferred failure

**DCA Holiday Policy**:
The configured choice to defer or skip an expected contribution whose due date
is not a fund open day.
_Avoid_: Weekday fallback, duplicate occurrence

**Fund Subscription Fee**:
The Investment Feeder Fund's actual front-end subscription fee after the user's
sales-platform discount, shared by recurring and manual additions.
_Avoid_: Published list rate, recurring NAV-borne expenses

**Fund Subscription Cutoff**:
The sales-platform boundary between a same-open-day manual subscription and the
next-open-day case.
_Avoid_: Exchange close, button time as execution evidence

## Drawdown additions

**Drawdown Add Plan**:
A user-defined relationship between one Reference ETF, one Investment Feeder
Fund, and incremental Drawdown Tiers.
_Avoid_: Trading strategy, ETF-price-only rule

**Drawdown Tier**:
One drawdown level and its independent additional amount within a Drawdown Add
Plan.
_Avoid_: Cumulative threshold, cumulative amount definition

**Recent Peak**:
The adjusted Reference ETF closing high whose date remains fixed while its
Drawdown Cycle is active.
_Avoid_: Cost basis, rolling peak, all-time high

**Drawdown Cycle**:
The period in which each Drawdown Tier's market fact may become confirmed once,
ending only when a confirmed close reaches or exceeds the Recent Peak after being
below it. Pending user reminders are separate and may repeat within the cycle.
_Avoid_: Rolling-window reset, partial recovery reset

**Initial Plan Evaluation**:
The first confirmed-close evaluation that records all currently reached open
tiers after a Drawdown Add Plan is created.
_Avoid_: Silent baseline, historical replay, command-time trigger

**Drawdown Pre-Alert**:
A provisional reminder that a realtime Reference ETF price has reached an
actionable Drawdown Tier, whether newly reached or previously confirmed but
still pending, without making a new market fact officially due.
_Avoid_: Confirmed trigger, official alert

**Fallback Pre-Alert**:
A Drawdown Pre-Alert from a validated secondary quote source with limited
metadata that can never confirm a tier.
_Avoid_: Confirmed close, silent source substitution

**Confirmed Drawdown Trigger**:
A Drawdown Tier becoming due from confirmed closing data in its Drawdown Cycle.
_Avoid_: Intraday crossing, pre-alert

**Tier Record**:
The durable market fact that one Drawdown Tier was confirmed by a close (or was
temporarily recorded from a user's manual-add statement). It is not proof that
the user bought and is not itself the user's reminder preference.
_Avoid_: Notification message, broker execution, cumulative tier

**Pending Drawdown Tier**:
A tier whose market condition has been confirmed in the active Drawdown Cycle,
but for which no Manual Add Confirmation or cycle skip exists. It is actionable
again on a later market date whenever the current drawdown still reaches it.
_Avoid_: New market crossing, completed purchase

**Tier Snooze**:
The user's choice to suppress a pending tier for one market date only. The
market fact remains stored and the snooze expires automatically on the next
market date.
_Avoid_: Tier skip, added state, deleted trigger

**Tier Skip**:
The user's choice to suppress one tier for the remainder of the active Drawdown
Cycle without claiming that an investment occurred. A new cycle reopens it.
_Avoid_: Manual addition, deleted market fact

**Aggregated Drawdown Reminder**:
One Investment Reminder containing all currently actionable newly reached and
pending Drawdown Tiers and the sum of their incremental amounts.
_Avoid_: One reminder per tier, completed investment total

**Manual Add Confirmation**:
The user's explicit statement that they acted on selected Drawdown Tiers; it
never places or proves an order.
_Avoid_: Automatic execution, reminder delivery, close confirmation

**Manual Add Estimate**:
A fee- and NAV-based Position Estimate for the sum of only the Drawdown Tiers
selected in a Manual Add Confirmation.
_Avoid_: Unselected tier, verified transaction, arbitrary extra purchase

**Manual Add Settlement Notice**:
A notification that a Manual Add Estimate was applied without claiming that the
sales platform confirmed execution.
_Avoid_: Trade confirmation, duplicate position application

**Plan Readiness**:
The derived indication that an Investment Feeder Fund has the fee and baseline
position required for automatic position estimation.
_Avoid_: Rule enabled state, inferred fee, guessed initial position

## Position and price gain

**Position Snapshot**:
A user-supplied baseline containing current units and average unit cost for one
Investment Feeder Fund.
_Avoid_: Holding hierarchy, brokerage synchronization, inferred execution

**Position Estimate**:
The latest position after applying estimated contributions, labeled as such
because the bot cannot observe sales-platform confirmation or rounding.
_Avoid_: Confirmed shares, Reference ETF valuation, broker position

**Position Sync**:
The user's replacement of a Position Estimate with current confirmed units and
average cost from the sales platform.
_Avoid_: Weekly transaction entry, automatic account import, trade history

**Position Reconciliation**:
The user's declaration of whether a Position Sync already includes the displayed
pending contributions.
_Avoid_: Silent inclusion, later double application

**Position Cycle**:
One continuous period in which an Investment Feeder Fund has positive units and
each position-linked gain threshold may alert once.
_Avoid_: Average-cost identity, weekly reset, reminder-implied redemption

**Price-Gain Reminder**:
A reminder that Feeder-Fund Unit NAV has risen by a configured amount from a
fixed cost or the position's current average unit cost.
_Avoid_: Profit-taking instruction, sell signal

**Position-Linked Price-Gain Rule**:
A user-created set of gain thresholds for one Investment Feeder Fund whose
trigger history is independent of every other fund.
_Avoid_: Global threshold list, hard-coded percentage, cross-fund state

**Initial Price-Gain Evaluation**:
The first successful evaluation that records all currently reached open
thresholds for a Position-Linked Price-Gain Rule.
_Avoid_: Command-time alert, one message per threshold, historical replay
