# Confirm the ETF and feeder-fund pair before saving

`/add_drawdown_plan` first resolves and displays the exact Reference ETF and
Investment Feeder Fund codes and provider names, configuration, readiness, and
available drawdown preview. The user confirms the economic pairing; matching
names are not proof, code/type mismatch is a hard error, and unavailable metadata
requires a stronger manual-verification confirmation. Only confirmation saves
a one-to-one ETF/fund mapping; its short-lived in-memory draft leaves no partial
rule after expiry or restart.
