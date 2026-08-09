# Supersede stale allocation notifications

An undelivered allocation notification is retried only while its reported
Allocation State remains current. A later state change supersedes the old
notification and creates the newest transition reminder instead; unlike a
confirmed Drawdown Tier, an old allocation state is not actionable context once
the allocation has changed.
