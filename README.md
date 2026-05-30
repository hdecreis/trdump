# trdump

Three small tools wrapped around
[traderepublic-sync](https://pypi.org/project/traderepublic-sync/):

- **`trdump fetch`** — dump every dataset the library can pull into a
  local `json/` directory.
- **`trdump export`** — turn that dump into CSV / Excel / JSON files
  using YAML+Jinja2 column-mapping templates.
- **`trdump monitor`** — full-screen live ticker shell: persistent
  table of subscribed tickers on top, scrolling log in the middle,
  command prompt pinned to the bottom.

> Unofficial. Uses Trade Republic's undocumented WebSocket API; can
> break at any time. Read [libtrsync's caveats](https://github.com/hdecreis/libtrsync)
> before relying on it.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium   # one-off, for the WAF token
```

## Credentials

On first run, `trdump` prompts for your phone number and PIN and writes
them to `~/.config/trdump/config.json` (`chmod 600`). The cached WAF +
session tokens land next to it as `session.json` and are reused until
they expire.

Override the directory via `TRDUMP_CONFIG_DIR=/some/path`.

## probe

One-shot WebSocket probe — sends a single `sub` frame and prints the
first response payload as pretty JSON. Useful for exploring undocumented
or V2 endpoints before wiring them into `fetch` / `monitor`.

```bash
trdump probe accountPairs
trdump probe compactPortfolio --account 1                       # auto-fills secAccNo
trdump probe compactPortfolioByTypeV2 --account 1 --protocol 34
trdump probe portfolioStatus --protocol 34
trdump probe ticker '{"id": "US0378331005.NSY"}'
```

`--account N` (1-indexed against `accountPairs`) fills `secAccNo`;
`--cash-account N` fills `accountNumber`. `--timeout` defaults to 10 s.

`--protocol N` sets the version sent in the WS `connect` frame.
**libtrsync hardcodes `31`** in most paths; TR's V2 subscriptions
(`compactPortfolioByTypeV2`, `portfolioStatus`, …) reject `31` with
`Unknown topic type: <name>.31` and only work on `34`. The probe opens
its own raw WS so it can pick the version independently of libtrsync.

## fetch

```bash
trdump fetch                  # writes ./json/*.json
trdump fetch -o /tmp/tr       # custom output dir
trdump fetch --code 123456    # skip the 2FA prompt
```

Produces:

| File | Content |
|---|---|
| `accounts.json` | Named accounts (cash + securities) |
| `account_pairs.json` | Raw `accountPairs` response |
| `assets.json` | Current positions with live prices |
| `cash_balance.json` | Available cash per currency |
| `transactions_raw.json` | Raw timeline items + parsed details |
| `transactions_dual.json` | Dual-legged (PURCHASE / SELL / DIVIDEND / …) view |

## export

Default templates live under `src/trdump/templates/` and are exposed by
name:

```bash
trdump export --list                          # transactions, assets, accounts
trdump export transactions --format csv       # → transactions.csv
trdump export assets --format xlsx -o out/portfolio.xlsx
trdump export ./my-template.yaml --format json
```

A template is a YAML doc:

```yaml
source: transactions_dual            # which <name>.json under ./json/
filter: "{{ item.transaction_type }}"   # optional; falsy = drop row
columns:
  - header: Date
    value: "{{ item.date }}"
  - header: Amount
    value: "{{ item.credit_amount or item.debit_amount }}"
```

Same template feeds CSV, Excel, and JSON writers. For non-CSV outputs,
rendered values are coerced back to `int` / `float` / `bool` / `None`
when they look numeric, so XLSX cells stay sortable.

## monitor

```bash
trdump monitor                                # empty shell
trdump monitor US0378331005 bitcoin           # pre-subscribed to Apple + BTC
```

Layout:

```
┌─ Accounts ────────────────────────────────────────────────────────────┐
│  ▼ 1. Trade Republic CTO   12 pos  val  15,234.50 EUR  P/L ▲ +812.00  │
│       Cash            EUR                       1,000.00 EUR           │
│   (s) Apple Inc       US0378331005  qty 12.5  @ 200.25  = 2,503.12    │
│   (b) févr. 2035      US91282CMM00  …                                 │
│   (p) Apollo          PE0001        …                                 │
│  ▶ 2. Trade Republic PEA    8 pos  val   8,123.00 EUR  P/L ▲ +123.45  │
└───────────────────────────────────────────────────────────────────────┘
┌─ Watched tickers ─────────────────────────────────────────────────────┐
│   Asset           ISIN          Last      Ccy    Δ     Δ%             │
│   Apple Inc       US0378331005  200.25    USD  ▲ +0.50  (+0.26%)      │
└───────────────────────────────────────────────────────────────────────┘
┌─ Log ─────────────────────────────────────────────────────────────────┐
│ [16:23:01] Apple Inc  US0378331005  200.25 USD  ▲ +0.50 (+0.26%)      │
└───────────────────────────────────────────────────────────────────────┘
 Ready.
> add tesla
```

Commands:

| Command | Effect |
|---|---|
| `add <ISIN \| query>` | Subscribe to a watched ticker. Non-ISIN strings go through `neonSearch` |
| `remove <ISIN \| query \| all>` | Unsubscribe by exact ISIN, by case-insensitive substring of any watched name/ISIN, or wipe everything |
| `list` | List current watched subscriptions in the log |
| `format compact\|verbose` | Switch the event log between one-line and multi-line ticks |
| `expand <N\|all>` | Open the per-position breakdown under account #N (1-indexed, as shown) |
| `collapse <N\|all>` | Close it |
| `help` | Reminder |
| `quit` / `exit` / `q` / `Ctrl-C` | Exit |

**Resilience.** The monitor opens its WebSocket with `auto_reconnect=True`
and wires both refresh hooks on the client:

- Transient WS drops (wifi blip, server hiccup) → libtrsync reconnects
  with exponential backoff and replays every live subscription
  (account-level `compactPortfolio` / `availableCash` and per-position
  `ticker`). You'll see `WebSocket reconnected; subscriptions replayed.`
  in the log.
- `WafExpired` from any frame → the monitor re-runs `acquire_waf_token`
  via Playwright in a thread (the sync Playwright API can't be invoked
  from the asyncio loop), persists the refreshed state back to
  `~/.config/trdump/session.json`, and the session continues. Briefly
  flashes `Refreshing WAF token…` in the status bar.
- `SessionExpired` (the multi-hour `tr_session` cookie has rotated) →
  logged + the monitor exits. Re-auth needs 2FA, which the full-screen
  UI can't drive; restart `trdump monitor` to log in again.

On startup the monitor discovers your TR accounts (`accountPairs`) and
subscribes per account to:

- **`compactPortfolioByTypeV2`** — quantities + EUR cost basis, with
  positions already grouped by category (`stocksAndETFs` / `cryptos` /
  `bonds` / `fixedSavings` / `privateMarkets`, plus any new category TR
  adds) and inline `name` / `instrumentType` / `status`.
- `privateMarketsPositions` (best-effort) — richer PE data
  (`instrumentName`, `pendingAmounts`, `bonusInfo`).
- `availableCash` — live cash balance,
- `ticker` for every held ISIN — live last-price drives the value / P&L
  totals.

`compactPortfolioByTypeV2` requires WebSocket protocol `34`.
`traderepublic-sync >= 0.4` opens every handshake with `connect 34`
natively, and `pyproject.toml` pins that floor so older versions can't
silently downgrade us into a state where V2 subs are silently dropped.

**Categories.** The account row's headline (Value / Today / All-time /
Pos) aggregates **every** category — stocks, cryptos, bonds, fixedSavings,
and Private Markets — matching the TR website's portfolio total. The
expanded view is a single flat list (no per-category subtotals); each row
is tagged with a one-letter category prefix: `(s)` stocks & ETFs, `(b)`
bonds, `(i)` fixedSavings/interest, `(c)` crypto, `(p)` private markets.
Rows group by category, then sort by name.

**Name enrichment.** `compactPortfolioByTypeV2` only gives bonds,
fixedSavings, and PE a generic name (a bond shows as its maturity,
"févr. 2035"; PE as "Private Equity"). For those categories the monitor
makes a best-effort `instrument` lookup and upgrades the label to the real
issuer/fund plus currency and maturity, e.g. `US TREASURY N/B (USD 2035)`.
If the lookup is slow or rejected, the generic name stays.

**Bonds and FX.** A bond's live price is quoted as a percentage of par
(e.g. `101.63`) in its own currency (often USD), while its cost basis is in
EUR. TR's WebSocket doesn't stream an FX rate, so to value a foreign-
currency bond in EUR the monitor fetches the daily EUR reference rate from
the ECB (`fx.py`, standard library only) and computes
`value = netSize × price/100 ÷ EURUSD`. Until a bond's currency is resolved
(via the `instrument` lookup) or if the FX fetch fails, the bond is shown at
its EUR cost basis and counted among the unpriced positions (the `*` on the
account `Pos` cell). Bond/ETF *funds* (e.g. an iShares iBonds UCITS ETF) are
already priced in EUR per share and need no conversion.

**Partial pricing.** If a position hasn't received its first ticker frame
(or its subscription failed — see the log for `ticker sub failed for …`),
the headline falls back to `avg_buy_in` for that position so the row never
goes blank. The `Pos` cell then shows `N*` to signal that one or more
cells in the breakdown will still read `—`. P&L stays correct: a position
valued at its cost basis contributes 0 to unrealized gain.

The watched-tickers panel is independent from the accounts panel: you can
follow ISINs you don't hold without affecting account totals.

### Accounts panel — what each column means

One unified table covers three row types: **account**, **cash**, and
**position**. The header is the same across all three. Expanded accounts
are rendered in **bold**. The `Value` column **flashes green / red for
~0.5 s** after the underlying number changes (per-position on a price
tick, per-account on aggregate change, cash on `availableCash` update).

| Column | Account row | Cash row (first under each expanded account) | Position row |
|---|---|---|---|
| `Asset` | `▶ N. <product label>` | `Cash` | `(x) ` category tag + instrument name |
| `ISIN` | — | account currency (e.g. `EUR`) | ISIN |
| `Qty` | held-positions count | — | `netSize` |
| `Last` | — | — | `ticker.last.price` |
| `Value` | Σ `qty × last_price` + ccy | cash balance + ccy | `Quantity × Last` |
| `Today Δ` | Σ `qty × (last − prev_close)` | — | `Quantity × (Last − prev_close)` |
| `Today %` | qty-weighted day return | — | `(Last − prev_close) / prev_close × 100` |
| `All-time Δ` | `Value − Cost` | — | `Quantity × (Last − Avg buy)` |
| `All-time %` | `(Value − Cost) / Cost × 100` | — | `(Last − Avg buy) / Avg buy × 100` |

The four P/L columns mirror the 2×2 grid TR's app shows on a position
page — **Today / All-time × Absolute / Percent** — and now use the same
columns at every level so values align vertically. `All-time` is
unrealized only: it does **not** include past realized sells, and does
**not** offset `Cost` by dividends received.

### Watched tickers panel — what each column means

| Column | Formula | Notes |
|---|---|---|
| `Last` | live `ticker.last.price` | |
| `Ccy` | quote currency | |
| `Day Δ` | `Last − previous-session close` | TR's `ticker.pre.price` |
| `Day %` | same, as % of prev close | matches TR app's "Aujourd'hui" |
| `Updated` | local clock | wall time at the most recent tick |

### What the monitor does **not** compute

By design the monitor only consumes live TR WebSocket subscriptions —
`compactPortfolio`, `availableCash`, and `ticker`. That gives you a live
**unrealized** snapshot of currently-held shares. The following require
the full transaction history (`json/transactions_dual.json`, produced by
`trdump fetch`) and are out of scope for the live shell:

- **Lifetime / realized P&L** — needs all past `PURCHASE`, `SELL`,
  `DIVIDEND`, `FEE`, `TAX` legs.
- **Dividend-adjusted cost basis** — `averageBuyIn` from
  `compactPortfolio` is just the purchase price; reducing it by net
  dividends received needs the timeline.
- **Annualised IRR (XIRR)** — solvable from
  `transactions_dual.json` cashflows + current `Value` as the terminal
  credit. Tractable as a separate `trdump` subcommand or as an extra
  account-row column populated once at startup.
- **TWR (time-weighted return)** — requires a daily portfolio NAV
  series across the holding period. TR's WebSocket doesn't expose
  historical prices for arbitrary dates, so this would need an external
  price source.
