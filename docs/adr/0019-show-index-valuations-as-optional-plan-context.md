# Show index valuations as optional plan context

Valuation is optional information in drawdown reminders and `/plans`/`/check`.
It never changes tiers, amounts, DCA, purchase records, positions or cycle state.
Existing plans make no extra valuation requests until explicitly configured.

`/set_plan_index <plan_id> <index_code|off>` is the user's explicit declaration
that the index matches the already-confirmed feeder-fund/ETF pair. The bot does
not infer tracking relationships from names or substitute a similar index.
Before saving a code, fetch its official valuation file and verify the returned
index identity. The response displays the fund, ETF, index code and index name.
`off` removes the association without requiring the source to be available.

Start with the public CSI indicator XLS source already verified for A500 and
STAR 50. Display P/E1 (total shares), P/E2 (calculation shares), D/P1 and D/P2
with source and data date. Do not relabel these as an unverified TTM metric.
This source does not provide PB or a multi-year history, so both PB and historical
percentiles remain explicitly unavailable. Other exact index codes may be bound
only when this same source verifies their identity; unsupported indexes are not
silently mapped to another index.

Use one bounded request with an eight-second network timeout and a two-MiB
response limit. Validate column identities, dates, index codes and numeric values.
Missing/non-positive PE and invalid dividend yields are unavailable metrics, never
zero-filled valuation signals. A zero dividend yield is valid.

A small SQLite cache stores the source's recent observations and attempt times
per index, surviving restart and sharing requests across plans. Successful
requests are cached for one hour, failures for five minutes; `/plans refresh`
explicitly refreshes. Preserve a validated prior snapshot on fetch failure,
label it as cached with refresh failure, and always show its date. Select only
observations on or before the price date (the confirmed date for prealerts).
Older observations are explicitly labelled. The cache does not compute signals
or maintain valuation cycles.

Fetch outside the plan write transaction and only when rendering a due reminder
or reading status. Valuation failure never blocks a drawdown reminder. Store the
displayed context in the existing event message/payload so delivery retries reuse
the original snapshot without a new valuation request or historical rewrite.
