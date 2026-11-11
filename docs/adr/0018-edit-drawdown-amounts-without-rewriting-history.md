# Edit drawdown amounts without rewriting history

`/set_plan_amounts <plan_id> <percent:amount,...>` edits only amounts of existing
tiers, including a subset. It never changes thresholds, tier count, ETF/fund
identity, cycle allocation, market facts, purchase estimates or position state.

Confirmation is a local-only preview scoped to the requesting Telegram user and
chat for ten minutes and the same China market date. It displays configured and
current-cycle amounts, state, future total and both pending totals. Confirmation
rechecks the complete plan/cycle/action/reminder/fee state inside the transaction;
any intervening change requires a new preview.

- Untriggered tiers use the new amount in this cycle and later cycles.
- Pending tiers can retain this cycle's amount or explicitly change it after
  the user confirms that the selected subscriptions have not been submitted.
- Added tiers, including those waiting for NAV, keep their recorded amounts.
- Skipped tiers remain skipped. Their changed amounts start next cycle.
- Changing a purchased tier never creates a purchase for the difference.

An append-only SQLite amount-change table stores dated current-cycle amounts and
future configured amounts. The original tier fact and alert payload remain
intact. Current reminders and status resolve current-cycle amounts; a newly
confirmed cycle uses the latest configuration. Historical additions resolve the
amount as of the declared subscription date (the last confirmed edit on that
date), still require an eligible close-confirmed fact, and never create NAV or
position estimates. A user who already subscribed must record it before editing.

Old action buttons and confirmation drafts expire after any amount edit, even
when an amount is later changed back. `/mark_added` creates a fresh confirmation
using the current amount and the existing same-day market eligibility. Unfinished
deliveries of old plan alerts are cancelled and their claims revoked; already
sent or in-flight network messages cannot be recalled. Source events remain
available for historical provenance, and late delivery callbacks cannot revive
cancelled retries. Updates and their audit rows commit atomically.
