# Investment Plan Enhancement Guide

[English](investment-plan-guide.md) | [简体中文](investment-plan-guide.zh-CN.md)

> Status: Drawdown Add Plans, before-close actions, Manual Add Estimates,
> Position Sync reconciliation, enhanced fixed DCA position estimates, and
> position-linked Price-Gain Reminders are implemented. Generic allocation
> rebalancing is deferred.

This enhancement adds Drawdown Add Plans, informational long-term trend context,
low-maintenance DCA position estimates, Position Syncs, and cost-based
Price-Gain Reminders to the existing local bot. Everything remains a reminder:
the bot never places an order, reads a sales-platform account, or provides
financial advice.

## Recommended end-to-end flow

Initial setup is intentionally short:

1. Add the Drawdown Add Plan, inspect the resolved Reference ETF and domestic
   feeder fund, then confirm the pair.
2. Add the enhanced fixed DCA rule with the feeder-fund code, actual discounted
   subscription fee, and holiday policy.
3. Run one Position Sync using the current units and average unit cost shown by
   the sales platform.
4. Optionally add user-chosen Price-Gain thresholds with `auto` cost. The bot
   never creates default gain thresholds.

Steps 2 and 3 may be completed later. Market drawdown reminders still work, but
the plan displays `SETUP_REQUIRED` and cannot estimate added units until both the
fee and initial position exist.

Normal operation is:

1. On a scheduled DCA date, the bot assumes the configured fixed deduction will
   run. No weekly success acknowledgement is needed; the user acts only if it
   failed or was not executed.
2. At `14:50`, the bot checks the Reference ETF's realtime price. A reached open
   tier produces a provisional pre-alert so the user can subscribe before the
   fund cutoff. The bot never submits the subscription.
3. If the user subscribed, they record exactly which tiers they used. The bot
   asks for confirmation and, when necessary, which side of the `15:00` cutoff
   the real submission occurred.
4. At `17:10`, confirmed ETF closing data records every newly reached market
   tier. A tier that was already confirmed but not added remains **pending** and
   can appear in a later reminder while the drawdown is still below its level;
   a tier the user marked added or skipped is not offered again in that cycle.
5. At `08:30` on later calendar mornings, the bot waits for the exact dated
   feeder-fund unit NAV. It quietly applies routine DCA estimates, sends one
   completion notice for a manual drawdown addition, and evaluates any enabled
   Price-Gain rule.
6. The user checks the concise current state with `/plans`, uses `/check` for
   detailed drawdown measurements, and occasionally runs `/sync_position` to
   replace estimates with the platform's actual units and average cost.

If required market data, the trading calendar, or exact-date fund NAV is
unavailable, affected work stays pending and the bot sends an aggregated data
notice. It does not guess from stale data or treat missing data as “no alert.”

## Drawdown Add Plans

A Drawdown Add Plan associates planned additional amounts with drawdown tiers.
Normal DCA remains separate: drawdown state never pauses, changes, or creates a
scheduled contribution. Existing reminder-only DCA rules remain compatible;
the optional fixed-schedule flow described below adds position estimation for a
specified Investment Feeder Fund.

Each plan has two assets:

- a **Reference ETF**, whose closes and realtime quote supply market conditions;
- an **Investment Feeder Fund**, which is the open-ended ETF feeder fund the user
  actually owns.

V1 supports domestic A-share ETF feeder funds such as the user's A500,
ChiNext, STAR 50, and STAR 100 sleeves. QDII and other funds with a different
valuation calendar are intentionally excluded because the domestic ETF calendar
cannot determine their subscription date safely.

V1 does not separately configure the official index. Telegram uses:

```text
/add_drawdown_plan <reference_etf_symbol> <feeder_fund_symbol> <name> <tiers> [lookback:<calendar_days>]
```

The command fixes the first symbol as `cn_etf` and the second as `cn_open_fund`,
so the user does not enter asset-type tokens. A name containing spaces must be
quoted. `lookback` is optional and defaults to `365`; SMA stays internal at 250
observations with a 20-observation slope window. A plan accepts at most 50 tiers
so Telegram can always render the tier action buttons; normal plans typically
need only a small handful. Before saving, the bot also renders the largest
possible pre-alert and confirmed reminder and rejects a name/tier combination
that would exceed Telegram's 4,096-character message limit.

Conceptual example:

```text
/add_drawdown_plan <ETF代码> <联接基金代码> 中证A500 15:5000,20:10000,25:15000,30:20000,35:10000
```

An override is appended only when required:

```text
/add_drawdown_plan <ETF代码> <联接基金代码> 中证A500 15:5000,20:10000 lookback:500
```

The command does not save immediately. It resolves the exact ETF and feeder-fund
codes to provider names where possible, then shows a read-only confirmation:

```text
参考 ETF：<provider name>（<code>）
实际持有：<provider name>（<code>）
回撤档位：...

[✅ 确认配对并创建]
[❌ 取消]
```

Matching codes and names do not prove the economic relationship between the two
products; the user confirms that pairing. A provider code/type mismatch is a
hard error. If names or market data are unavailable, the stronger button says
**I verified in my platform; create anyway** and the response identifies every
unverified field, including whether the fund uses the domestic A-share valuation
calendar. No automatic name heuristic guesses that a fund is a feeder.

The preview also shows every incremental tier, the maximum one-cycle capital
commitment (the sum of all tiers), the latest validated drawdown, the amount
currently reached, the data date, and whether market data is confirmed or
realtime. For example, a confirmed -26% previews that the -15%, -20%, and -25%
tiers may total ¥30,000. Missing market data makes the current-drawdown portion
unavailable, but the maximum configured total remains visible and explicit
pairing confirmation is still possible.

Only confirmation saves the enabled rule. It creates no Tier Record or alert
event. The first scheduled plan evaluation may initialize a Drawdown Cycle from
confirmed history so a newly created plan can produce a same-day `14:50`
pre-alert. Only a confirmed-close evaluation records market-confirmed tiers.
Repeated confirmation is idempotent, and only one enabled Drawdown Add Plan may
use either Reference ETF or Investment Feeder Fund. V1 keeps the mapping
one-to-one so duplicate plans cannot send two reminders or update the same
position twice.

The confirmation draft is intentionally short-lived and held only by the single
local bot process. If it expires or the process restarts, the button asks the
user to rerun `/add_drawdown_plan`; no partial rule exists to recover.

Plan creation does not require the feeder fund's fee or initial Position Sync.
Drawdown evaluation remains usable because it depends only on the Reference ETF.
The response derives and displays one of two readiness states:

```text
READY

SETUP_REQUIRED
手续费：未配置
初始持仓：未同步
行情提醒继续；自动持仓估算暂不可用
```

`SETUP_REQUIRED` is a visible readiness state, not a disabled rule. `/plans` and
every affected reminder repeat the missing setup until it is resolved.

If market data is unavailable during creation, explicit manual pairing
confirmation may still save the plan, but the response says that the market
preview is unavailable. No amount is guessed.

User-entered percentages are converted to fractions internally. Tiers must be in
ascending drawdown order, may not repeat a drawdown, and require percentages
between 0% and 100% and positive amounts. Drawdown Add Plans always use the
Reference ETF's normalized forward-adjusted (`qfq`) closing prices; there is no
configurable price field.

Each amount belongs only to its tier; it does not include amounts from earlier
tiers. Market facts and user actions are separate: a `close_confirmed` Tier
Record means only that the ETF closed through the level, while `Manual Add
Confirmation` means that you say you actually subscribed. A confirmed but
unadded tier is **pending** and may be reminded again; it is not silently
treated as purchased.

Example using `15:5000,20:10000,25:15000,30:20000`:

```text
Day 1: drawdown reaches -15%
New amount: -15% = ¥5,000
If you do not add it, -15% becomes pending.

Day 2: drawdown falls directly to -30%
Actionable amounts: pending -15% + new -20% + -25% + -30%
Total: ¥5,000 + ¥10,000 + ¥15,000 + ¥20,000 = ¥50,000
If -15% was already added or skipped, only the three new tiers total ¥45,000.

Day 3: drawdown recovers to -20%
The pending -15% tier is not actionable while the market is above -15%.
If it later falls below -15% in the same cycle, it becomes actionable again.
```

If the total actionable amount would be too much for one gap event, configure
smaller incremental amounts per tier. The evaluator does not silently cap, spread,
or reinterpret the user's plan.

### Cycle behavior

The first scheduled evaluation finds the highest valid Reference ETF close in
the inclusive calendar window ending on the latest confirmed close. A 365-day
window therefore contains that date plus the preceding 364 calendar dates. It
locks the most recent equal high as the Recent Peak for the Drawdown Cycle, and
each tier's market fact is recorded once in that cycle. Pending user reminders
are separate and can repeat on later market dates until the user adds, skips, or
a new cycle begins.

The peak does not expire or fall simply because 365 days pass. A new cycle begins
when a confirmed close exceeds the locked peak, or first returns to the peak
after an intervening below-peak close. Repeated equal closes at the peak without
an intervening decline do not create empty new cycles. This prevents a long
decline from looking smaller merely because its original high left a rolling
window.

Forward-adjusted history may be recalculated after an ETF distribution. The bot
therefore keeps the peak date stable but refreshes that date's `qfq` value on the
current basis before calculating drawdown. Distribution adjustment changes
neither the cycle identity nor its recorded tiers.

After downtime, the bot recovers the latest cycle from closing history but does
not replay reminders for drawdowns that crossed and recovered entirely while it
was offline. An untriggered tier still crossed by the latest close is handled
normally.

### Before and after close

Before close, the bot sends a provisional Drawdown Pre-Alert when the realtime
price reaches any actionable tier: a newly reached tier or a previously
confirmed-but-pending tier. It aggregates them, sends at most once per plan per
trading date, and does not consume a confirmed market fact.

The Drawdown Add Plan reuses the existing single before-close scheduler time,
`14:50` Asia/Shanghai. That quote is closer to the close than an earlier check,
but it remains a realtime estimate—not a closing price or feeder-fund NAV. A tier
first crossed after the check cannot produce another same-day pre-alert; the
after-close job evaluates it from confirmed closing data. Because the quote
interfaces do not provide a trustworthy exchange timestamp, the message shows
the Bot's fetch time as **fetched at**, never as the exchange's quote time. This
leaves the user roughly ten minutes before the configured `15:00`
fund-subscription cutoff.

Telegram pre-alerts and confirmed reminders include inline buttons. A typical
multi-tier message shows:

```text
[✅ 已加仓]
[⏰ 今天不投，之后提醒]
[⏭ 本周期跳过]
```

`✅ 已加仓` starts the existing all-or-selected-tier manual-add confirmation;
only tiers the user actually submitted should be selected. `⏰ 今天不投，之后提醒`
stores a one-market-date snooze without recording an addition. `⏭ 本周期跳过`
asks for confirmation and then lets the user skip all or selected tiers for the
current drawdown cycle. A pending tier remains eligible on later market dates
until it is added or skipped. A confirmed action edits the original Telegram
message to show what was recorded, and duplicate callbacks are idempotent.

Buttons are the normal Telegram workflow. Bark, ntfy, webhook, and button
failure use the command fallback printed in the same reminder:

```text
/mark_added <plan_id> 15,20

Only use this after you actually subscribe. It records your statement and never
places an order.
```

The buttons and `/mark_added` accept only tiers listed in the latest eligible
pre-alert or confirmed reminder for that plan. They record exactly the selected
percentages. For a pre-alert, those tiers are not offered again in the same
cycle; for an already confirmed tier, the existing Tier Record remains intact.
Choosing **Not added yet** creates no purchase or position state.

Automatic position estimation accepts only a Manual Add Confirmation completed
on the reminder's local market date. A later button or command is rejected rather
than guessing when the subscription happened; if the user invested later or
forgot to record it, they use `/sync_position` after the platform shows the
settled units and average cost. This deliberately favors correct cost over a
convenient but ambiguous backdated estimate.

A confirmed Manual Add Confirmation also creates one pending Manual Add
Estimate containing the sum of the selected tier amounts, the shared Fund
Subscription Fee, and the user-action timestamp. After its applicable feeder-
fund unit NAV is published, the bot applies the estimated units and gross cost
once and labels the Position Estimate accordingly. It never claims that the
sales platform confirmed those units. This estimate exists only after the user
confirms that the real gross amount exactly matches the configured sum; a
different amount follows the Position Sync path instead.

The button or command immediately replies **Recorded; waiting for dated NAV**.
Unlike routine DCA, this infrequent manual addition sends one completion notice
after the estimate is applied:

```text
✅ 加仓估算已更新

金额：¥10,000
申购日期：2026-XX-XX
净值及日期：X / 2026-XX-XX
预计新增份额：X
最新平均成本：X
状态：预计，尚未与平台同步
```

The notice includes the plan/fund, selected tiers, fee, source, and data date but
contains no trade instruction. Position application and notification delivery
are separate: the estimate applies once even if every channel fails, while the
completion notice remains independently retryable. An occurrence already
`reconciled_by_sync` creates neither a later estimate nor this notice.

This user's fund subscription cutoff is `15:00`. A confirmation received before
that time defaults to the current confirmed fund open day. At or after `15:00`,
the bot does not infer the application date from the button timestamp because
the user may have subscribed earlier and recorded it later. It asks:

```text
[15:00前已提交 — 使用当日净值]
[15:00后才提交 — 使用下一开放日净值]
[取消]
```

The selected effective date remains pending until a unit NAV with that exact
date is published. Both the submission choice and NAV date are displayed in the
result. A button tap records the user's statement; it is not platform evidence.

That automatic estimate path is available only when the plan is `READY`. Under
`SETUP_REQUIRED`, the action button instead says **Record add; position sync
required** and requires confirmation. It records the selected tiers so they are
not offered again, changes no units or cost, and marks the feeder fund as needing
a Position Sync. The response instructs the user to copy the current post-
purchase units and average cost from the platform. A later `/sync_position`
replaces the position and clears that warning; the bot never back-calculates the
unready purchase.

The realtime price supplies the estimated drawdown and price-to-SMA distance.
The Recent Peak, SMA, and SMA slope use confirmed `qfq` closes. Since forward
adjustment leaves the current price unchanged, the realtime traded price is
comparable with that history. After close, the day's closing data may create
durable Tier Records and one aggregated reminder.
Existing simple ETF drawdown rules use the same timestamped quote path. For
legacy index or stock snapshots whose AKShare result has no source date, the bot
uses dated history instead of labeling the snapshot as today's data.

The Investment Feeder Fund does not need a realtime NAV. Before-close estimates
come from the Reference ETF and are explicitly labeled as estimates; position
value and Price-Gain Reminders use the feeder fund's latest published NAV with
its data date.

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

### Market-data correctness

The three data streams are intentionally separate:

| Purpose | Instrument and value | Required date/basis |
| --- | --- | --- |
| Peak, drawdown, MA250 | Reference ETF daily close | confirmed `qfq` history |
| Before-close estimate | Reference ETF latest traded price | current trading session |
| Position and price gain | Investment Feeder Fund unit NAV | latest published NAV date |

V1 obtains exact-date feeder-fund unit NAV from
`fund_open_fund_info_em(..., indicator="单位净值走势")`. AKShare's other open-fund
NAV view, `fund_open_fund_daily_em`, is also backed by Eastmoney and is not an
independent outage fallback. The documented Sina open-fund interface supplies
fund scale rather than dated unit NAV, while the Xueqiu basic-information
interface supplies metadata rather than a NAV series. None can replace the
required feeder-fund value.

Therefore an Eastmoney feeder-NAV failure leaves scheduled and manual position
estimates plus Position-Linked Price-Gain Rules pending. The next-morning job
sends one aggregated notice with fund codes, expected NAV dates, and last
reliable dates, then retries on its next run or startup. It never substitutes
Reference ETF price, cumulative NAV, a same-provider “fallback” label, or an old
unit NAV.

Per-fund-company website adapters and manual NAV entry are deferred. The former
have no single normalized contract across the user's future fund choices; the
latter can silently corrupt cost if a date or value is mistyped. If observed
outages make waiting materially unacceptable, manual exact-date NAV entry is the
first smaller option to reconsider, with confirmation and audit—not a generic
provider framework.

For default MA250 plus its 20-session slope, the evaluator needs at least 270
valid ETF closes. It requests a safe calendar range large enough for both those
observations and the locked peak date; the drawdown lookback remains 365 days
even when more history is fetched for calculation.

Before evaluating, the bot normalizes dates, sorts rows, and keeps the last
duplicate for a date. Pure trend calculations ignore invalid observations, but
confirmed-close plan evaluation rejects the entire snapshot if any retained
close is missing, non-finite, or non-positive; it cannot safely assume a missing
row was not the recent high. A before-close quote must match the exact ETF symbol
and show current-session trading. An after-close reminder requires the expected
confirmed trading date. The bot does not use a stale row and label it as current.

The existing AKShare Sina trading calendar remains the calendar source, but a
payload is valid only when its returned date range covers the date being checked.
A date beyond the calendar's maximum is **calendar unavailable**, not a market
holiday. New investment-plan workflows then keep state unchanged and notify the
user; the legacy rules retain their existing weekday fallback. AKShare documents
the calendar as a finite-date dataset, so this coverage check is mandatory:
[AKShare trading-calendar documentation](https://akshare.akfamily.xyz/data/tool/tool.html).

The primary history request uses AKShare's Eastmoney ETF endpoint with
`adjust="qfq"`. The current unadjusted Sina fallback is not acceptable for a
Drawdown Add Plan because mixing price bases can create a false drawdown. If an
equivalent adjusted source is unavailable, that evaluation fails cleanly and is
logged instead of sending a reminder. Existing simple rules keep their existing
provider behavior.

Before close, Eastmoney realtime is primary. The provider requests only the
exact ETF symbol, applies an eight-second network timeout, and opens a brief
global Eastmoney cooldown after failure. Its exact symbol, source quote time,
positive trading activity, and previous close must be consistent with the latest
validated `qfq` close. If it is unavailable or inconsistent, the bot makes one
bounded per-symbol Sina request for a Fallback Pre-Alert with the same checks.
Sina failure cooldowns are isolated per symbol, so one temporary failure does
not suppress the other plans. If both sources fail, the data notice includes
both failure reasons. The message identifies the source and source quote time,
asks the user to verify their own platform, and never confirms a tier
automatically.

Sina daily history has no adjustment option, so it cannot provide the cycle
peak, MA250, or after-close confirmation. A prolonged Eastmoney outage delays
official confirmation instead of silently switching to an unadjusted series.

If no acceptable source remains, the bot sends one aggregated Data Availability
Notice per trading date and check phase—not one message per plan. Before close,
this requires both Eastmoney and Sina realtime data to fail validation. After
close, it means the expected Eastmoney `qfq` close is unavailable; Sina does not
confirm closing tiers.

Example:

```text
⚠️ Investment-plan data unavailable

Phase: before close
Affected: A500, ChiNext, STAR 50
Eastmoney: unavailable
Sina: unavailable or invalid
Last reliable data: 2026-XX-XX close

The bot could not determine whether an add tier was reached.
No tier was confirmed or consumed.
Please check your own fund or market platform today.
This is a data notice, not an add or price-gain reminder.
```

If Sina succeeds, no availability notice is sent; any resulting pre-alert names
Sina as its fallback source. A partial outage lists only plans that could not be
evaluated. Provider errors and stack traces remain in logs and are never exposed
in the user message.

AKShare documents that forward adjustment keeps current price unchanged while
adjusting older prices for distributions, and that the ETF daily-history endpoint
is updated after close: [AKShare ETF history documentation](https://akshare.akfamily.xyz/data/fund/fund_public.html#id16).
It also documents that open-fund unit NAV can update later in the evening, so the
NAV's own date is always displayed and validated: [AKShare open-fund documentation](https://akshare.akfamily.xyz/data/fund/fund_public.html).

If the Reference ETF has less than 365 days of history, drawdown uses all valid
history since listing and clearly labels the shorter coverage. Fewer than 250
valid closes makes MA250 unavailable but does not invalidate an otherwise
calculable drawdown. Missing, stale, suspended, or malformed data emits no
investment reminder.

### Status commands

`/plans` shows one concise overview per Investment Feeder Fund already known from
a Drawdown Add Plan, enhanced DCA rule, Position Snapshot, or Position-Linked
Price-Gain Rule. It summarizes configured DCA, current drawdown and next tier,
position accuracy, and Price-Gain status without detailed history. It is not an
alias for `/list`, which continues to list raw rule configuration. Plain
`/plans` reads the persisted normalized ETF history and feeder-fund NAV from
SQLite, so it does not refresh paid market data on every view. Use
`/plans refresh` when a deliberate provider refresh is wanted; that command can
consume provider quota.

If the latest confirmed close is already beyond an unrecorded tier, `/plans`
shows **Reached, awaiting official close confirmation**. It also shows
confirmed-but-unadded tiers as **pending**. This is read-only: `/plans` and
`/check` never consume the official tier or claim that a purchase occurred.

If a known feeder fund has no position-linked Price-Gain rule, Telegram shows:

```text
止盈提醒：未配置
[➕ 设置止盈档位]
```

The button returns a ready-to-complete command containing the fund identity:

```text
/add_profit cn_open_fund <fund_symbol> <name> auto <请输入档位>
```

It does not invent percentages or create a rule until the user replaces the
placeholder and submits a valid command. Non-Telegram status output prints the
same command template without a button.

The overview also derives plan readiness from the existence of a Fund
Subscription Fee and initial Position Snapshot. It shows `SETUP_REQUIRED` and
the missing items until both exist, plus a persistent **Position Sync required**
warning after any manual addition that could not be estimated. Position status
also shows the last Position Sync date and how many later contribution estimates
have been applied, so “estimated” never hides how far it has drifted from the
platform baseline.

`/check` shows detailed plan measurements, current-cycle tier status, and the next
tier. Drawdown Add Plan status is read-only: running `/check` never confirms a new
plan tier. It distinguishes **untriggered**, **triggered pending**, **added**, and
**skipped for this cycle**; a check mark never implies that the Bot or platform
completed a purchase. Existing legacy rule behavior remains unchanged.

## Low-maintenance DCA position tracking

An enhanced DCA rule identifies the Investment Feeder Fund and gross recurring
amount. Its fee argument initializes or validates the fund's shared actual
subscription-fee setting after any sales-platform discount:

```text
/add_dca <fund_symbol> <name> <weekday> <gross_amount> <fee> [holiday:next|holiday:skip]

Percentage fee: rate:0.12%
Fixed RMB fee:  fixed:1
Default holiday policy: holiday:next
Alternative:            holiday:skip
```

Example:

```text
/add_dca <基金代码> 中证A500联接 周四 2000 rate:0.12%
```

The enhanced form is weekly: it requires a six-digit feeder-fund code, non-empty
name, supported weekday, positive gross RMB amount, and one fee token. A name
containing spaces must be quoted. The optional holiday token must be last; an
unknown or duplicate option is rejected. Omitting it means `holiday:next`, and
the fund cutoff uses its configured `15:00` value. A known QDII or other
non-domestic-calendar fund is rejected.

Only one enabled enhanced DCA rule may use the same feeder fund and weekday.
This prevents an accidental repeated command from creating two assumed
deductions; intentionally investing on two weekdays uses two distinct rules.

Only one fee mode is allowed. A percentage must be finite and at least zero; a
fixed fee must be finite and at least zero. A fixed fee is also checked against
each future gross amount before calculation. A C share class with no front-end
subscription fee uses `rate:0%`; recurring sales service charges already
reflected in published NAV are not deducted again.

The fee belongs to the Investment Feeder Fund because this user's fixed DCA and
manual drawdown additions have the same actual fee. Both flows reference the
same setting, and a confirmed `/mark_added` creates a Manual Add Estimate when
the plan is `READY`. If the fund already has a different configured fee,
`/add_dca` fails rather than silently changing every future estimate. The
explicit update command is:

```text
/set_fund_fee <fund_symbol> <fee>
```

Each scheduled occurrence copies the current fee, so a later change affects only
future estimates and never rewrites a historical cost calculation. Existing
`/add_dca <name> <weekday> <amount>` rules continue as reminder-only rules and do
not estimate a position.

Change a DCA rule's future weekly amount without replacing its rule ID:

```text
/set_dca_amount <rule_id> <new_amount>
```

For example, `/set_dca_amount 12 500` changes rule 12 to ¥500. An occurrence
already created for a due date keeps its original gross amount, fee snapshot,
reminder text, and later position estimate. Only occurrences created after the
command use ¥500. The command never changes a drawdown-plan tier or submits an
order.

The Investment Feeder Fund settings also hold the sales-platform subscription
cutoff, using `15:00` for this configuration. It can be corrected once without
changing historical occurrences:

```text
/set_fund_cutoff <fund_symbol> <HH:MM>
```

The holiday policy is configured once:

- `holiday:next` is the default. A due date that is not a confirmed fund open
  day remains pending until the next confirmed open day and uses that day's
  published unit NAV.
- `holiday:skip` creates no position estimate for a due date that is not a
  confirmed fund open day.

The original due date remains the occurrence identity even when it moves to a
later NAV date, so a holiday cannot duplicate a weekly estimate. If the trading
calendar is unavailable or the expected dated NAV is missing, the occurrence
stays pending and the bot reports a data problem instead of treating a weekday
or old NAV as current.

### Next-morning NAV processing

The existing `17:10` after-close job remains responsible for confirmed Reference
ETF closes and Drawdown Tiers. Feeder-fund work runs separately every calendar
day at `08:30` Asia/Shanghai, after the normal evening NAV publication window:

- settle pending scheduled DCA and Manual Add Estimates whose effective fund
  open day has finished;
- evaluate Position-Linked Price-Gain Rules once for each new unit-NAV date;
- leave future or unresolved holiday occurrences pending.

Running on weekends and holidays is intentional because Friday's or the last
open day's NAV may need processing the next calendar morning. The job does not
expect a new NAV merely because it ran. It first resolves the latest completed
confirmed fund open day and requires a unit NAV with that exact date. If that
date was already processed, it exits without a reminder.

For `holiday:next`, a holiday occurrence waits through the entire closure, uses
the next confirmed open day, and is calculated at `08:30` on the following
calendar day at the earliest. For `holiday:skip`, it never reaches NAV
calculation. A manual subscription submitted after its `15:00` cutoff follows
the same next-open-day rule.

An unavailable trading calendar leaves affected occurrences pending. A missing
NAV is treated as a data problem only after a NAV is expected for a confirmed
completed open day; a normal holiday with no expected NAV is not an error. One
notice aggregates affected funds and expected NAV dates. No stale NAV advances a
position, triggers a gain threshold, or clears pending work.

The user first supplies one accurate Position Snapshot: current units and
average unit cost from the sales platform. Because this is a fixed automatic
investment plan, each scheduled date creates one pending Scheduled DCA Estimate;
the user does not confirm it every week. The reminder says that this is an
assumption based on the configured schedule, not verified execution.

Until that first Position Snapshot exists, scheduled reminders continue but no
occurrence may change units or cost. Occurrences stay pending and the bot asks
for Position Sync. The reconciliation prompt then lets the user state whether
the platform snapshot already includes all displayed pending contributions. A
new investor must still explicitly sync `0 0`; the bot never assumes an empty
account.

The normal Telegram reminder requires no action:

```text
本周固定定投：¥2,000
Bot 将按计划等待净值并估算份额，无需操作。

[⚠️ 本次扣款失败／未执行]
```

Enhanced fixed-DCA occurrences due on the same date are delivered as one
summary across Telegram, Bark, ntfy, and webhook. The summary total excludes
configured `holiday:skip` occurrences. Persistence stays per occurrence, and
Telegram keeps one named failure button per pending fund, so one failed
deduction never changes another fund's estimate.

There is no success button. The scheduled occurrence exists independently of
notification delivery, so a delayed or failed message does not create a second
occurrence or change the fixed schedule. Bark, ntfy, webhook, and Telegram
fallback text include the exact skip command instead of an interactive button.

If the platform reports that the automatic deduction failed or did not execute,
the user taps **Skip this occurrence** or runs:

```text
/dca_skip <rule_id> <due_date>
```

A skip received before calculation prevents that occurrence from changing the
estimate. If the discrepancy is discovered after application, `/sync_position`
provides the simple correction instead of implementing purchase reversal logic.
Clicking an old skip button after application therefore explains that the
estimate was already applied and returns the Position Sync command; it never
silently subtracts units or cost.

After the applicable unit NAV is published, the bot estimates the added units:

```text
percentage fee:
net amount = gross amount / (1 + fee rate)

fixed fee:
net amount = gross amount - fixed fee

estimated added units = net amount / subscription-date unit NAV
```

A successfully applied scheduled DCA estimate is quiet. It updates the persisted
Position Estimate and `/plans` fields for last due date, effective NAV date,
gross amount, estimated added units, and status, but creates no second success
notification. The original DCA reminder is sufficient. Deduction exceptions,
missing NAV/calendar data, Position Sync requirements, and any separately
reached Price-Gain threshold still notify normally.

Under `holiday:next`, the bot finds the first confirmed fund open day on or
after the scheduled date, waits for an Investment Feeder Fund unit NAV with that
exact date, then uses it as the estimated subscription date. This handles
weekends and market holidays without assuming that trading days equal calendar
days. The Position Estimate adds the calculated units and gross cash outlay to
the prior position, then derives the new average unit cost. It records the due
date, NAV date, fee mode, holiday policy, and estimated status.

It does not claim that value is the registrar-confirmed share amount: a sales
platform may skip a holiday rather than defer it, apply a different effective
date, reject the deduction, or use a different rounding rule.

The user does not enter weekly units or confirmations. An occasional
Position Sync supplies the two values displayed by the sales platform:

```text
/sync_position <fund_symbol> <units> <average_unit_cost>
```

Positive units require a positive average unit cost. The exact pair `0 0` records
a fully closed position; mixed zero and positive values are invalid. A Position
Sync replaces the estimate with the platform's current confirmed units and
average cost. This creates a new accurate baseline without importing a brokerage
history or building a general transaction ledger. Position value always uses
the feeder fund's latest published unit NAV, never the Reference ETF's realtime
price.

`/plans` shows the last sync date and the number of estimates applied since it.
The user should sync after any redemption, dividend reinvestment or cash payout,
manual purchase not recorded through the Bot, fee mismatch, or visible platform
difference. No extra scheduled “please sync” reminder is added in v1.

Before applying a Position Sync, the bot lists every pending scheduled DCA or
Manual Add Estimate with source, due/action date, and gross amount. If any exist,
Telegram requires one of these confirmations:

```text
[同步数据已包含以上交易]
[同步数据一笔都未包含以上交易]
[同步数据只包含部分（取消）]
[取消]
```

**Already included** writes the user-supplied position and marks those pending
occurrences `reconciled_by_sync`, so their later NAV cannot add units again.
**None included** means no displayed item is represented by the snapshot. It
writes the position but leaves every occurrence pending to apply after its exact
dated NAV arrives. **Partially included** cancels without changing anything; run
`/sync_position` again after every displayed item settles. The position
replacement and occurrence decisions happen atomically and repeated callbacks
return the stored outcome.

A `SETUP_REQUIRED` manual addition marked **Position Sync required** is cleared
only when the user confirms that the new platform snapshot includes it. Choosing
none included leaves that warning visible. If the snapshot contains only some
displayed items, choose the partial option; the bot changes nothing, and the user
synchronizes again after the platform has settled them rather than guessing.

## Delivery and persistence

Confirmed tier state and notification delivery state are separate. A failed
notification does not undo a confirmed tier. Pending or failed drawdown reminders
are retried after later scheduled runs or restart until at least one enabled
channel succeeds.

Simple drawdown, fixed-cost Price-Gain, and DCA reminders also keep their
delivery state in SQLite. An undelivered reminder is retried by the next morning
NAV process or application startup; a recovered DCA message only keeps its
failure button while that scheduled occurrence is still pending.

On the first upgrade that enables this recovery, SQLite uses the database's
first recorded delivery attempt to separate older ambiguous history. The bot
does not replay those possibly stale reminders individually; it sends one
recovery notice asking you to run `/check`. Later pending and failed reminders
continue through normal retry handling.

If the database has no recorded delivery attempt at all, there is no reliable
automatic boundary. The bot uses the same single recovery notice instead of
guessing that an old reminder is current. This is a one-time upgrade safeguard.

All market-driven alerts include their market-data date. Missing, stale, or
insufficient data is reported without creating an incorrect reminder.

For the new stateful Drawdown Add Plan and enhanced DCA rule, `/del` stops future
evaluations or scheduled occurrences but retains existing cycle and contribution
state. A contribution already recorded as pending still settles or is reconciled
through Position Sync; deleting a rule never silently discards it or applies it
twice. Legacy rule deletion keeps its current behavior.

## Price-Gain Reminders

Keep the existing command and stored rule type, and allow `auto` in the cost
position for an Investment Feeder Fund:

```text
/add_profit <asset_type> <symbol> <name> <cost|auto> <thresholds>

Position-linked example:
/add_profit cn_open_fund <fund_symbol> <name> auto 20,30
```

`20,30` is syntax illustration only, not an initial configuration or suggested
investment threshold. Creating a Drawdown Add Plan, DCA rule, or Position
Snapshot never creates a Price-Gain rule. When the bot is in real use, the user
manually chooses and enters thresholds through Telegram. Until then, position
tracking continues normally and no Price-Gain Reminder exists for that fund.

Each Investment Feeder Fund configures its thresholds independently. A500,
ChiNext, STAR 50, and STAR 100 do not inherit a global list, although the user
may deliberately enter the same percentages. Values are never hard-coded. To
avoid overlapping reminders, only one enabled `auto` Price-Gain rule is allowed
per feeder fund; existing legacy numeric-cost rules remain untouched.

Creating an `auto` rule performs a read-only preview from the latest validated
Position Snapshot or Position Estimate and dated feeder-fund unit NAV. If gain
is already `35%` for thresholds `20,30,40`, the response previews `20%` and
`30%`; it does not mark or notify them inside the command. Missing or stale input
does not block rule creation but makes the preview explicitly unavailable.

The first successful scheduled evaluation in the current Position Cycle records
all still-reached thresholds and sends one aggregated reminder. The same
aggregation applies when one later NAV moves through several new thresholds at
once. The payload retains every threshold separately, but the user receives one
message. A threshold crossed and recovered before the rule existed or while the
bot lacked valid data is not reconstructed from historical NAV.

Human-facing text calls this a Price-Gain Reminder, not a profit-taking
instruction. For an Investment Feeder Fund, it reports gain from the latest
published feeder-fund NAV and, with `auto`, reads the current average unit cost
from its Position Snapshot or Position Estimate:

```text
gain = current unit NAV / current average unit cost - 1
```

When DCA or manual additions have been estimated since the latest Position Sync,
the message labels the cost and gain as estimates. Missing position, zero units,
invalid cost, missing NAV, or stale NAV produces no Price-Gain Reminder and
reports the unavailable state cleanly. The Reference ETF's realtime price never
creates an exact feeder-fund profit calculation.

Existing numeric-cost rules retain their current behavior and need no migration.
Changing an automatically maintained average cost must not by itself duplicate
an already-triggered threshold.

Position-linked thresholds deduplicate within one Position Cycle. Partial
redemption and any positive-unit Position Sync preserve that cycle and its
triggered thresholds. `/sync_position <fund_symbol> 0 0` closes it. A later
transition from zero to positive units—by Position Sync, scheduled DCA estimate,
or confirmed manual addition—starts a new cycle with all thresholds open again.

A Price-Gain Reminder prominently tells the user to run `/sync_position` after
any redemption. The bot cannot know changed units or a full exit from reminder
delivery alone, and it never assumes that a reminder caused a sale.

The reminder reports the reached gain threshold, current dated feeder-fund unit
NAV, current average cost, exact-or-estimated label, and position value. It does
not choose a redemption amount or percentage. Telegram shows:

```text
[✅ 已部分赎回并同步持仓]
[✅ 已全部清仓]
[⏭ 暂不处理]
```

**Partially redeemed** displays a ready-to-complete
`/sync_position <fund_symbol> <units> <average_unit_cost>` command; the user must
copy the two current values from the sales platform because the bot cannot infer
them. **Fully closed** shows `0 0` and requires a second confirmation before
closing the Position Cycle. **No action** changes no position state. None of the
buttons submits a redemption, and the already-delivered gain threshold is not
reopened merely because the user chooses no action.
