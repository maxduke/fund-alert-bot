# Do not backfill recovered drawdowns after downtime

After downtime, closing history is used to recover the latest Drawdown Cycle,
but close-confirmed Tier Records and reminders are created only from the latest
available closing snapshot. Drawdowns that crossed and recovered entirely while
the bot was offline are not replayed because a delayed “buy now” reminder would
no longer describe the current market condition.
