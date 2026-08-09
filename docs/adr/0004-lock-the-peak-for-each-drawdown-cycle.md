# Lock the peak for each drawdown cycle

The first scheduled plan evaluation with valid confirmed history initializes a
Drawdown Cycle from the highest Reference ETF `qfq` close in the configured
calendar lookback, choosing the most recent date on an equal high. That peak
remains
the drawdown reference until a later confirmed close exceeds it, or first
returns to it after an intervening below-peak close. Repeated equal closes at the
peak without an intervening decline do not create empty cycles; lookback expiry
never lowers the peak or re-arms tiers, and time-varying forward adjustment is
handled by identifying the cycle by database ID and peak date while refreshing
that date's `qfq` value.
