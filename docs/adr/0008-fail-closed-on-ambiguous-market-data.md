# Fail closed on ambiguous market data

Drawdown Add Plans use AKShare's Eastmoney ETF daily-history endpoint with
`adjust="qfq"` and the Eastmoney ETF realtime endpoint's latest traded price;
forward adjustment keeps the current price unchanged while removing artificial
distribution gaps, while unadjusted Sina history never confirms a plan.
Sina realtime may provide only a validated, source-labeled pre-alert; calendar
coverage, symbol, basis, continuity, activity, value, and dates must all validate.
Ambiguous ETF data or unavailable exact-date feeder NAV leaves state pending and
produces an aggregated data notice instead of an investment reminder.
