# Separate tier records from notifications

Each Drawdown Tier suppressed for a cycle is stored as its own durable Tier
Record with source `close_confirmed` or `user_marked_added`. Tiers confirmed by
one closing evaluation share one aggregated notification event and commit with
their Tier Records in one SQLite transaction, so deduplication survives restart
without hiding business state in notification JSON. Delivery failure never
removes a Tier Record and retries only the event.
