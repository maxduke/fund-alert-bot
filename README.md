# fund-alert-bot

[English](README.md) | [简体中文](README.zh-CN.md)

Lightweight personal investment alert bot for personal portfolio reminders.

This project runs alongside `maxduke/rsi6_monitor_bot`. The existing RSI6 bot remains responsible for RSI6 alerts. `fund-alert-bot` focuses on a smaller set of non-trading reminders:

- drawdown from recent high alerts
- DCA reminders
- price-gain reminders
- multi-channel notifications

This repository is intentionally not a web platform, not an RSI implementation, and not an automatic trading system.

## Current Status

The service includes environment-based configuration, SQLite storage,
drawdown-from-high rule evaluation, DCA reminder
evaluation, price-gain reminder evaluation, Telegram commands, scheduled
market and DCA checks, multi-channel notification dispatch with delivery state,
market data normalization, tests, Ruff configuration, and Docker packaging.

Implemented Telegram commands:

- `/start`
- `/help`
- `/add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>`
- `/add_profit <asset_type> <symbol> <name> <cost|auto> <thresholds>`
- `/add_dca <name> <weekday> <amount>`
- `/add_dca <fund_symbol> <name> <weekday> <gross_amount> <fee> [holiday:next|holiday:skip]`
- `/set_dca_amount <rule_id> <new_amount>`
- `/dca_skip <rule_id> <due_date>`
- `/set_fund_fee <fund_symbol> <rate:<percent>%|fixed:<RMB>>`
- `/set_fund_cutoff <fund_symbol> <HH:MM>`
- `/sync_position <fund_symbol> <units> <average_unit_cost>`
- `/add_drawdown_plan <reference_etf> <feeder_fund> <name> <tiers> [lookback:<days>]`
- `/mark_added <plan_id> <tier_percentages>`
- `/plans [refresh]`
- `/list`
- `/del <id>`
- `/check`
- `/test_notify`

Supported drawdown `asset_type` values are `cn_index`, `cn_etf`, `cn_stock`,
and `cn_open_fund`. Thresholds are entered as percentages, for example
`10,15,20`. `/check` runs enabled drawdown rules immediately and includes the
current drawdown percentage for each checked drawdown rule. APScheduler also
runs a before-close realtime drawdown check Monday-Friday so reminders can arrive
before the same-day close, then runs the after-close market reminder check. Both
scheduled market jobs skip official CN market holidays when AKShare's Sina
trade-date calendar is available. If that calendar cannot be loaded, the
scheduled check falls back to weekday behavior.

Price-Gain reminders are added with `/add_profit`, for example
`/add_profit cn_etf 159915 ChiNext-ETF 1.85 25,40` or
`/add_profit cn_open_fund 110026 Example-Fund auto 20,30`. Numeric cost is a
fixed personal cost basis; `auto` reads the feeder fund's tracked average cost
from `/sync_position`. Thresholds are user-chosen percentages and must be unique
and ascending in `auto` mode. `/check` uses
the latest normalized `close` value from the market data provider as the current
price for legacy numeric-cost rules. The daily `08:30` fund-NAV job evaluates
`auto` rules only from the exact previous confirmed trading day's feeder-fund
unit NAV. Reached tiers are aggregated and alert once per continuous positive
position cycle; changing average cost does not reopen them. A full `0 0` sync
closes the cycle and a later positive position starts a new one. Reminder buttons
can show sync instructions or, after a second confirmation, record a zero
position. They never redeem funds or place orders.

DCA reminder-only rules are added with `/add_dca`, for example `/add_dca 创业板 周四
1000` or `/add_dca 创业板 Thursday 1000`. Supported weekdays are 周一 through
周日 and Monday through Sunday; rules store normalized weekday codes such as
`THU`. `/check` also checks whether DCA reminders are due today. Scheduled DCA
checks run daily and send at most one reminder per rule per date.

For a fixed weekly feeder-fund plan that also estimates units after the exact
dated NAV is published, use:

```text
/add_dca 110026 "A500 feeder" 周四 2000 rate:0.12% holiday:next
```

Run `/sync_position 110026 <units> <average_cost>` once before relying on the
estimate. `holiday:next` defers a holiday occurrence to the next confirmed open
day; `holiday:skip` records it as skipped. If the platform does not execute a
scheduled deduction, tap the Telegram failure button or run
`/dca_skip <rule_id> <YYYY-MM-DD>` before NAV processing. The bot assumes a
configured deduction occurred, but cannot verify the platform. Check `/plans`
and periodically run `/sync_position` after any mismatch. No order is placed.
When several enhanced fixed-DCA rules are due on the same date, their delivery
is combined into one notification. Each occurrence, estimate, and failure
button remains independent; `holiday:skip` items are shown but excluded from
the displayed total. Failed delivery retries use the same grouping.
At rule creation the default AKShare provider makes one per-symbol Xueqiu
metadata request to verify the declared fund type; QDII/overseas funds are
rejected, and an unavailable metadata response creates no rule. This avoids a
bulk Eastmoney fund-list request and never adds traffic to recurring checks.

Use `/set_dca_amount 12 500` to change only future occurrences of DCA rule 12.
Already-created occurrences keep their original amount, rule ID, and history.

### Feeder-fund setup and position sync

Use the six-digit code of the ETF feeder fund you actually own. Set its known
sales-platform subscription fee once, for example:

```text
/set_fund_fee 110026 rate:0.15%
/set_fund_fee 110026 fixed:1.50
```

`rate:0%` is valid when the share class has no front-end subscription fee. The
default subscription cutoff is `15:00`; correct it only when your platform uses
a different cutoff:

```text
/set_fund_cutoff 110026 15:00
```

Copy the current units and average unit cost shown by the sales platform:

```text
/sync_position 110026 12345.67 1.2345
```

Both values must be positive. Use the exact pair `/sync_position 110026 0 0`
only when the position is fully closed. The command replaces the Bot's current
snapshot and labels it `exact`; it does not import transactions or contact a
broker. For a positive position, the response fetches the feeder fund's latest
published unit NAV once and shows both its date and the resulting position
value. It never substitutes the Reference ETF's realtime price.

Remember to run `/sync_position` again after any redemption, distribution or
reinvestment, unrecorded purchase, fee mismatch, or visible difference from the
sales platform. The Bot cannot discover those account changes itself.

After the initial sync, add position-linked thresholds only if wanted:

```text
/add_profit cn_open_fund 110026 "A500 feeder" auto 20,30
```

The creation preview is read-only and consumes no threshold. If a Price-Gain
reminder arrives after a partial redemption, wait for the platform's exact units
and cost, then run `/sync_position` again. Do not forget this step: the Bot has
no brokerage connection and cannot detect the changed holding itself.
Creation performs one per-symbol Xueqiu metadata check and rejects QDII/overseas
funds before saving because auto-cost scheduling uses the domestic CN calendar.

### Drawdown Add Plans

A Drawdown Add Plan watches a listed ETF as the market reference while keeping
the ETF feeder fund you actually own as a separate position identity:

```text
/add_drawdown_plan 510300 000001 "Core index" 15:5000,20:10000,25:15000
```

An optional `lookback:<days>` token may appear only at the end. The default is
365 calendar days; MA250 and its 20-session slope are always informational.
Every tier amount is incremental, so the example's maximum one-cycle total is
¥30,000. The Bot shows that total before saving.

The command first produces a read-only preview and saves nothing. Check both
six-digit codes, the tiers, the setup state, and any available market-data
preview. Press **Confirm pair + domestic calendar** only when the two codes are
the intended ETF/feeder pair and the fund follows the domestic A-share valuation
calendar. The confirmation expires after 10 minutes and is tied to the current
Telegram user and chat.

Confirmed-close checks use only the Reference ETF's forward-adjusted (`qfq`)
daily history. The feeder fund's NAV is never substituted for ETF drawdown, and
the ETF realtime price is never used as exact position value. Several newly
reached tiers produce one aggregated reminder; each tier is remembered within
its peak cycle. A reminder does not mean that a purchase happened.

Use `/plans` for a concise overview and `/check` for detailed plan state. Both
are read-only for Drawdown Add Plans: they do not consume a tier or create an
alert. `/plans` reads the latest confirmed ETF history and feeder-fund NAV
stored in SQLite, so opening the overview normally does not create paid market
data requests. Use `/plans refresh` only when you explicitly want to refresh
the cached data; it may call the provider.

When the cached confirmed close has already reached an unrecorded tier, the
overview labels it **reached, awaiting official close confirmation**. A
before-close realtime estimate never consumes that tier; the after-close job
must confirm it from the ETF close.

At `14:50`, a plan uses the Reference ETF's current realtime price for a
provisional pre-alert. It does not consume a tier by itself. If you actually
submit the feeder-fund subscription, use the Telegram button or the printed
fallback command, for example:

```text
/mark_added 1 15,20
```

Confirm only the tiers and configured gross amount you really submitted. Before
the fund cutoff, the bot waits for that market date's exact published fund NAV;
at or after the cutoff it asks whether the real submission occurred before or
after the cutoff. A matching, fully configured action updates an explicitly
estimated position once the exact dated NAV becomes available. A different
amount or incomplete fund setup records the tiers but requires a later
`/sync_position`. These actions record your statement only: the bot does not
place or verify an order.

The daily `08:30` NAV job runs on calendar days because a trading day's fund NAV
may be published later. It requests exact-dated NAV while an estimate is pending,
and also evaluates enabled `auto` Price-Gain rules for positive positions from
the latest completed trading day's NAV. Missing settlement data remains pending
and produces a data availability notice. If `/sync_position` sees pending
additions, Telegram asks whether the platform snapshot already includes them
before replacing the position.

Telegram remains the command channel and default notification channel; optional
Bark, ntfy, and webhook channels can be enabled with environment variables.
User-facing Telegram replies, buttons, and every notification channel use one
global language. Set `BOT_LANGUAGE=zh-CN` (default) or `BOT_LANGUAGE=en`, then
restart the service. Commands such as `/check` keep their English Telegram names.
The bot publishes this command menu at startup, so typing `/` shows the available
commands with localized descriptions.

Default scheduler configuration:

- `TZ=Asia/Shanghai`
- `BOT_LANGUAGE=zh-CN`
- `AFTER_CLOSE_CHECK_TIME=17:10`
- `BEFORE_CLOSE_CHECK_TIME=14:50`
- `DCA_REMINDER_TIME=09:30`
- `FUND_NAV_PROCESS_TIME=08:30`
- `AKSHARE_RETRIES=3`
- `AKSHARE_RETRY_DELAY_SECONDS=0.5`
- `AKSHARE_LATEST_LOOKBACK_DAYS=45`
- `BARK_ENABLED=false`
- `NTFY_ENABLED=false`
- `WEBHOOK_ENABLED=false`

Optional notification channel configuration:

- `BARK_SERVER_URL`
- `BARK_DEVICE_KEY`
- `NTFY_SERVER_URL`
- `NTFY_TOPIC`
- `WEBHOOK_URL`

### Optional paid Eastmoney proxy

Eastmoney-backed AKShare endpoints can be rate-limited. The image includes the
pinned [`akshare-proxy-patch==0.5.0`](https://github.com/HelloYie/akshare-proxy-patch),
but it is disabled by default.
Enable it only after purchasing a token from the proxy service:

```dotenv
AKSHARE_PROXY_ENABLED=true
AKSHARE_PROXY_AUTH_TOKEN=replace-with-your-token
AKSHARE_PROXY_RETRY=1
AKSHARE_HISTORY_CACHE_TTL_SECONDS=300
```

The patch is installed at process startup, before the first lazy AKShare import.
It hooks only the Eastmoney domains used by this bot; Sina fallback and Xueqiu
metadata remain direct requests. Concurrent `fast` pagination is deliberately
disabled because it can multiply paid requests. Keep the retry value low and
never commit or log the token. When enabled, the proxy patch is the only retry
layer for paid Eastmoney requests; the provider invokes each Eastmoney AKShare
operation once. `AKSHARE_RETRIES` remains the retry budget for other data
sources. History, realtime quotes, and feeder-NAV data also use short
in-process caches. Confirmed normalized ETF history and exact feeder-fund NAVs
are additionally stored in SQLite. After the first backfill, the after-close
job refreshes the full required QFQ history window (unadjusted history uses a
small overlap), while `/plans` reads local rows. A status refresh never treats
the current session as a confirmed close.
Restart after changing these variables and verify the startup log; a proxy
failure still fails closed rather than inventing market data.

The patch's optional `fast` mode is intentionally disabled. It parallelizes
generic paginated fund-flow/rank endpoints, while this bot uses bounded ETF,
index, stock, and feeder-NAV calls that do not need that pagination. Enabling it
could create concurrent paid requests without improving this bot's reminder jobs.

Realtime ETF quotes are used only for before-close drawdown estimates. Each
Reference ETF uses one bounded, per-symbol Eastmoney request; a failure opens a
brief global Eastmoney cooldown so the remaining plans do not repeat requests.
CN index and stock latest lookups also use one bounded, per-symbol request rather
than AKShare's full-market paginated spot endpoints, which avoids their internal
three-attempt page retry loop.
For ETF quotes, Sina is the bounded per-symbol fallback, and both sources must
supply their own quote timestamp. Confirmed `qfq` history still fails closed if Eastmoney is
unavailable. Exact feeder-fund NAV has no independent Sina equivalent, so it
stays pending rather than guessing. RSI and RSI6 alerts are not implemented
here. The bot does not poll realtime endpoints: it performs one scheduled
before-close pass, skips already completed plan/date work, and requests fund NAV
only for pending dated estimates or eligible `auto` Price-Gain evaluations.

## Technology Stack

- Python 3.12
- python-telegram-bot
- SQLite
- AKShare
- pandas
- APScheduler
- requests
- pytest
- ruff
- Docker
- Docker Compose

Do not add Django, FastAPI, PostgreSQL, Redis, Celery, RSI indicators, a web UI, or automatic trading features.

## Local Development

The local development flow is:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Configuration should be created from `.env.example`:

```powershell
Copy-Item .env.example .env
```

Before using Docker Compose on Linux, set `BOT_UID` and `BOT_GID` in `.env` to
the output of `id -u` and `id -g`; this lets the non-root container write the
host-owned `data` directory. Docker Desktop users can use `1000` for both. The
Compose file stops with a clear error when either value is empty. On a new
Linux checkout, create the bind source as the same non-root account before the
first start:

```bash
(
set -eu
BOT_OWNER_UID="$(id -u)"
BOT_OWNER_GID="$(id -g)"
if [ "$BOT_OWNER_UID" -eq 0 ] || [ "$BOT_OWNER_GID" -eq 0 ]; then
  echo "Use a non-root account." >&2
  exit 1
fi
mkdir -p data
printf 'BOT_UID=%s\nBOT_GID=%s\n' "$BOT_OWNER_UID" "$BOT_OWNER_GID"
)
```

Compose will not create a missing `data` directory as root.

When upgrading an existing Linux Compose checkout, set those values first,
then migrate existing SQLite ownership while the bot is stopped:

```bash
(
set -eu
BOT_OWNER_UID="$(id -u)"
BOT_OWNER_GID="$(id -g)"
if [ "$BOT_OWNER_UID" -eq 0 ] || [ "$BOT_OWNER_GID" -eq 0 ]; then
  echo "Use a non-root account." >&2
  exit 1
fi
docker compose stop
sudo chown -R "$BOT_OWNER_UID:$BOT_OWNER_GID" data
docker compose up -d
)
```

Do not use `0` for either setting. New checkouts with no `data` directory do
not need the ownership-migration commands, but must create `data` before the
first `docker compose up` as shown above.

Do not commit `.env` or real secrets.

Run:

```powershell
ruff check .
pytest
```

Docker builds are validated by GitHub Actions on Linux. Local Docker is optional,
especially on Windows workstations.

## GitHub Actions

- `CI`: installs the project with dev dependencies, then runs Ruff and pytest on
  Python 3.12.
- `Docker Build`: builds the Docker image on Ubuntu for pull requests and pushes
  `ghcr.io/maxduke/fund-alert-bot` on non-PR runs.

## Project Documents

- `AGENTS.md`: contributor and coding-agent guardrails
- [`docs/deployment.md`](docs/deployment.md): VPS deployment and SQLite backup guide ([简体中文](docs/deployment.zh-CN.md))
- [`docs/architecture.md`](docs/architecture.md): current module responsibilities
- [`docs/investment-plan-guide.md`](docs/investment-plan-guide.md): Drawdown Add Plan, DCA, position, and Price-Gain behavior ([简体中文](docs/investment-plan-guide.zh-CN.md))
- [`docs/investment-plan-implementation.md`](docs/investment-plan-implementation.md): accepted technical design and acceptance checks
- [`docs/roadmap.md`](docs/roadmap.md): historical implementation phases
- `.env.example`: placeholder-only configuration template

## Scope Boundaries

`fund-alert-bot` may read market/fund data, calculate supported personal reminder conditions, store alert state in SQLite, schedule checks, and send notifications.

`fund-alert-bot` must not place trades, submit orders, rebalance accounts, provide financial advice, implement RSI or RSI6 alerts, modify `rsi6_monitor_bot`, or expose a public/private web application.
