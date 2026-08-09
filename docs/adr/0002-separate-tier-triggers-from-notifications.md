# Separate tier triggers from notifications

Each confirmed Drawdown Tier is stored as its own durable trigger record, while
all tiers confirmed by one evaluation share one aggregated notification event.
The trigger records and notification event are committed in one SQLite
transaction so per-tier deduplication survives restarts without producing one
message per tier or hiding business state inside notification JSON. A delivery
failure never removes confirmed tier records; the pending or failed notification
is retried after later scheduled runs or process restart until at least one
enabled channel succeeds.
