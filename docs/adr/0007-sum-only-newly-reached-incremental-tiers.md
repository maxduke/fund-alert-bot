# Sum only newly reached incremental tiers

Every Drawdown Tier defines its own incremental amount, not a cumulative amount.
When one evaluation reaches several previously open tiers, the bot sends one
reminder listing and summing only those tiers, excluding current-cycle Tier
Records so a deeper fall never adds an earlier tier twice. Creation displays the
maximum one-cycle sum so an unaffordable gap is corrected in configuration
rather than silently capped.
