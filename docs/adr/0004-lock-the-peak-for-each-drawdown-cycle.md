# Keep a stable anchor for each drawdown cycle

The first scheduled plan evaluation with valid confirmed history initializes a
Drawdown Cycle from the highest Reference ETF `qfq` close in the configured
calendar lookback, choosing the most recent date on an equal high. Its date is
the immutable cycle anchor. The current peak continues to follow genuine
confirmed closing highs and remains the drawdown reference.

A new cycle begins only when a future confirmed genuine new high reaches the
plan's configured margin above the cycle anchor. Equal highs never re-arm a
cycle. Lookback expiry does not lower either reference or re-arm tiers.
Time-varying forward adjustment is handled by storing the anchor and current
peak dates and refreshing both dates' `qfq` values before evaluation.
