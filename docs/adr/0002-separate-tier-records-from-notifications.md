# Separate tier records from notifications

Each confirmed Drawdown Tier is stored as its own durable market-fact Tier
Record with source `close_confirmed` or `user_marked_added`. User reminder
preferences such as one-day snooze and cycle skip are stored separately; they
never delete or rewrite the market fact. Tiers confirmed by one closing
evaluation share one aggregated notification event and commit with their Tier
Records in one SQLite transaction, so deduplication survives restart without
hiding business state in notification JSON. Delivery failure never removes a
Tier Record and retries only the event.
