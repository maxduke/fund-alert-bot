# Confirm drawdown-plan tiers at market close

Drawdown Buy Plans send provisional pre-alerts for realtime before-close tier
crossings but make tiers due only from closing-price data. Existing simple
drawdown rules retain their current realtime-trigger behavior for backward
compatibility; changing those rules could silently alter reminders users already
depend on. A pre-alert's drawdown and price-to-SMA distance use the realtime
price; its Recent Peak, SMA, and SMA slope use confirmed closes only. A pre-alert
expires at market close and is never retried after close or on a later restart;
the confirmed-close evaluation creates a durable reminder when appropriate.
