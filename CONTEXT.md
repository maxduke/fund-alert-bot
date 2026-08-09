# Investment Reminder Context

This context describes the personal investment reminders owned by
`fund-alert-bot`. Reminders report conditions defined by the user; they are not
financial advice and never place trades.

## Language

**Investment Reminder**:
A notification that a user-defined investment condition has been observed.
_Avoid_: Signal, recommendation, trade instruction

**Normal DCA**:
A recurring reminder to contribute a configured amount on a configured schedule,
independent of market drawdown and allocation state. Other reminders never
silently enable, disable, or change it.
_Avoid_: Automatic investment, drawdown buy

**Drawdown Buy Plan**:
A set of incremental additional amounts associated with drawdown tiers for one
market asset.
_Avoid_: Trading strategy, cumulative buy schedule

**Drawdown Tier**:
One drawdown level and its incremental additional amount within a Drawdown Buy
Plan.
_Avoid_: Cumulative threshold

**Recent Peak**:
The highest valid closing price within a configured calendar lookback period,
using the most recent observation when that price occurs more than once.
_Avoid_: Cost basis, all-time high

**Drawdown Cycle**:
The span in which each Drawdown Tier may become due once. A new cycle begins
when a closing observation reaches the current Recent Peak, including an
equal-price full recovery; an older peak merely leaving the lookback period does
not begin a new cycle, and later rolling-window peak changes do not reset its
tiers.
_Avoid_: Recent Peak identity, rolling-window change, recovery without a recent high

**Cycle Anchor**:
The closing observation that begins a Drawdown Cycle and gives the cycle its
stable identity while its Recent Peak reference may later change.
_Avoid_: Current rolling peak

**Drawdown Pre-Alert**:
A provisional reminder that a realtime price has reached a Drawdown Tier before
the market close. It aggregates unconfirmed crossed tiers, is sent at most once
per plan per trading date, and does not make a tier due.
_Avoid_: Confirmed trigger, official alert

**Confirmed Drawdown Trigger**:
A Drawdown Tier becoming due from closing-price data and being recorded for its
Drawdown Cycle.
_Avoid_: Intraday crossing, pre-alert

**Tier Trigger Record**:
The durable record that one Drawdown Tier became due within one Drawdown Cycle.
It does not mean the user completed a purchase, and several Tier Trigger Records
may share one notification.
_Avoid_: Notification message, completed purchase, cumulative tier

**Aggregated Drawdown Reminder**:
One Investment Reminder containing all Drawdown Tiers newly confirmed in the same
evaluation and the sum of their incremental amounts, not an outstanding or
completed investment total.
_Avoid_: One reminder per tier, cumulative tier definition

**Plan Status Check**:
A read-only view of a Drawdown Buy Plan's current measurements, triggered tiers,
and next tier. Viewing it never makes a tier due.
_Avoid_: Manual evaluation, trigger command

**Long-Term Trend Context**:
Informational price-versus-moving-average and moving-average-slope measurements
shown with a Drawdown Buy Plan reminder. Available measurements remain usable
when another trend measurement lacks sufficient history, and none controls tier
confirmation or amount.
_Avoid_: Buy filter, sell signal, timing rule

**Price-Gain Reminder**:
A reminder that price has risen by a configured amount from the user's configured
cost basis.
_Avoid_: Profit-taking instruction, sell signal

**Holding**:
A manually maintained Market Holding or Manual Holding assigned to one allocation
bucket.
_Avoid_: Brokerage position, synchronized position

**Market Holding**:
A Holding in an investable market asset, valued from a user-maintained unit
quantity and the latest acceptable market price or net asset value.
_Avoid_: Market index, manually valued asset, synchronized position

**Manual Holding**:
A Holding valued from one user-maintained amount with a recorded update date and
no market-data lookup.
_Avoid_: Market holding, live cash balance

**Portfolio Group**:
An independent set of holdings whose allocation is measured together.
_Avoid_: Entire portfolio, brokerage account

**Complete Group Valuation**:
A valuation in which every enabled Holding in a Portfolio Group has an acceptable
same-date market value or a dated manual value. Allocation is not calculated
from partial or mixed-date market values.
_Avoid_: Best-effort allocation, partial total, mixed-date valuation

**Allocation Value**:
An RMB market value or RMB manual value included in a Complete Group Valuation.
_Avoid_: Foreign-currency value, converted value

**Allocation Snapshot**:
A Complete Group Valuation and its resulting bucket percentages for one
evaluation date. An older snapshot may be displayed with its date but cannot
create a new Allocation State Transition.
_Avoid_: Live brokerage balance, undated allocation

**Allocation Bucket**:
A named share of a Portfolio Group with a target allocation.
_Avoid_: Asset class hierarchy

**Allocation Rule**:
A rule that compares one watched Allocation Bucket with the total value of its
Portfolio Group using configured percentages ordered as
`0 < target < warning < rebalance < 100%`. Being above target alone does not
leave the `NORMAL` state.
_Avoid_: Full bucket target map, portfolio hierarchy

**Allocation State**:
The current `NORMAL`, `WARNING`, or `REBALANCE` classification of a monitored
Allocation Bucket under its configured boundaries.
_Avoid_: Trading decision

**Initial Allocation State**:
The first complete Allocation State observed for an Allocation Rule. An initial
`NORMAL` state is recorded silently; an initial `WARNING` or `REBALANCE` state
produces one reminder.
_Avoid_: State transition

**Allocation State Transition**:
A change from one Allocation State to any other, including direct jumps and
recoveries. Transition-only reminders suppress repetition while the state is
unchanged but do not use separate recovery boundaries.
_Avoid_: Hysteresis

**Allocation Rebalancing Reminder**:
A reminder that an Allocation Bucket has entered a different Allocation State.
It reports the watched bucket without choosing a destination for any transfer,
and becomes obsolete if the bucket leaves the state described by the reminder.
_Avoid_: Rebalance order, automatic sale
