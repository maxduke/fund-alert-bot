# Confirm drawdown-plan tiers at market close

Drawdown Add Plans send provisional pre-alerts for realtime before-close tier
crossings but make market-confirmed tiers due only from closing-price data, while
legacy simple drawdown rules retain their existing behavior. A user may suppress
acted-on pre-alert tiers only through a same-date Manual Add Confirmation; the
Bot never places or verifies the subscription. Ready plans create a pending
fee-and-dated-NAV estimate only when the user confirms the actual gross amount
matches the selected tiers; otherwise they require Position Sync.
