# Estimate DCA units until a position sync

An enhanced DCA rule stores its Investment Feeder Fund and gross recurring
amount, and each occurrence snapshots the shared fund-level subscription fee so
later changes affect only future estimates. Fixed schedules assume execution
unless the user skips an occurrence, then apply one fee- and exact-date-NAV-based
Position Estimate without a weekly success acknowledgement. Position Sync
periodically replaces estimates and atomically reconciles pending work, avoiding
brokerage access, reversal logic, and a general transaction ledger.
