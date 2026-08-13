# Fail closed on ambiguous market data

Drawdown Add Plans use AKShare's Eastmoney ETF daily-history endpoint with
`adjust="qfq"`. Before close, the market-data provider requests only the exact
ETF symbol from Eastmoney with a hard timeout and briefly suppresses all other
Eastmoney requests after a failure. A bounded Sina per-symbol request is the
realtime fallback. Both realtime adapters must expose the source's quote time;
the Bot request time is not market-data freshness evidence.

Forward adjustment keeps the current price unchanged while removing artificial
distribution gaps, while unadjusted Sina history never confirms a plan. Sina
realtime may provide only a validated, source-labeled pre-alert; calendar
coverage, symbol, basis, continuity, activity, value, and dates must all validate.
Ambiguous ETF data or unavailable exact-date feeder NAV leaves state pending and
produces an aggregated data notice instead of an investment reminder.
