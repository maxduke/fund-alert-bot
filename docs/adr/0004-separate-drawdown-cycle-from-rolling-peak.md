# Separate drawdown cycle identity from the rolling peak

A Drawdown Cycle has its own persisted identity and Cycle Anchor; Tier Trigger
Records reference that identity. The rolling Recent Peak remains the reference
for current drawdown calculations and display, but a change caused only by
lookback expiry does not create a cycle or re-arm tiers. A new cycle is persisted
only after a closing observation reaches the then-current Recent Peak.
