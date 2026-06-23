# Elder Triple Screen Scanner — Hyperliquid

A **read-only daily analysis tool** that watches a small set of Hyperliquid-tradable
perps and evaluates them with Alexander Elder's **Triple Screen** + **Impulse** method
(*The New Trading for a Living*). Once a day it produces a signals table and interactive
charts so the operator can make a **discretionary** entry decision and place orders
**manually**.

> **Disclaimer:** this is an analysis/education tool, **not financial advice** and
> **not an auto-trader**. It places no orders, holds no keys, and only ever calls
> Hyperliquid's *public* `info` endpoint.

## How it works

- **First screen (tide):** weekly chart — strategic bias from the slope of the weekly EMA13, with tiny slopes treated as **flat/no-trend** so ranges do not become false signals.
- **Second screen (wave):** daily chart — the 2-EMA Force Index looks for pullbacks
  *against* the daily wave but *with* the weekly tide.
- **Third screen (entry):** buy-stop 1 tick above the prior day's high (longs) /
  sell-stop 1 tick below the prior day's low (shorts), plus an alternative limit at the
  projected EMA13 offset by the average pullback penetration. Optionally enable a true
  lower-timeframe third screen (`4h` by default) to time the stop-entry from the latest
  completed 4h high/low and veto entries against the 4h Impulse.
- **Impulse censorship (applied last):** any **red** Impulse (weekly or daily) forbids
  longs; any **green** forbids shorts.
- **Best-trade ranking:** every validated setup gets a 0–100 quality score blending
  Elder's selection criteria — reward:risk (dominant; 2:1 floor, 3:1 = full credit),
  Impulse agreement across both screens, weekly-tide strength, and daily pullback depth.
  The single highest-scoring setup that clears the 2:1 floor is flagged as the **best
  trade** (`★`); the table is sorted best-first. The 6% guard suppresses any pick.
- **Divergences:** recent bullish/bearish divergences between price and MACD-Histogram /
  13-EMA Force Index are surfaced as Elder warnings.
- **Risk:** 2% Rule (Iron Triangle sizing, default 1% risk per trade, hard cap 2%) and
  the 6% monthly guard that blocks all new entries once monthly losses + open risk reach
  6% of the month-start equity. When a public address is configured, open risk is
  calculated automatically from each held position's current Elder stop; the manual
  `open_trade_risk` field is only extra risk for positions the scanner cannot see.
- **Journal:** each `run.py` refresh can append a compact JSONL entry with the top
  pick, signal levels/reasons, open-position verdicts, stops and open risk.
- **Trade management (open positions):** for trades you already hold, Elder's exit tools
  give a daily verdict — **hold**, **take profits**, or **exit** — from the same weekly +
  daily screens (see below).

## Managing open trades

The Triple Screen says *when to enter*; this answers the operator's other daily
question — *"I already hold this, do I hold, take profits, or get out?"* If you set a
**public** wallet address in `config.toml` (`[positions].address`) the tool reads your
**open positions** from Hyperliquid's public `clearinghouseState` info endpoint — a
read-only account lookup, like a block explorer. **No private key, no signing, no
order** is ever involved, and you can leave the field empty to disable the feature.

For each open position it applies only Elder's own exit logic (no new indicators):

- **Impulse used for exits.** While long you may keep holding as long as the Impulse is
  **green**; once *neither* the weekly nor the daily Impulse is green any more (both have
  gone **blue**) the prohibition against selling is lifted → **permission to take
  profits** (only flagged while in profit). A **red** Impulse goes further — momentum has
  reversed → **exit**. Mirror image for shorts.
- **Premise invalidated.** If the **weekly tide flips** against the position, the reason
  you took the trade is gone → **exit** ("the trade no longer earns its risk").
- **Profit target.** When price reaches the **weekly value zone** (EMA13–EMA26) or, if it
  already trades beyond value, the weekly channel → **take profits**.
- **Trailing stop (SafeZone).** A suggested stop tucked behind the recent daily extreme by
  the average EMA penetration, ratcheted to at least break-even once the trade is in profit.

Verdict precedence is **exit > take profits > hold**. The result appears as an "Open
positions" table at the top of the dashboard and as a panel on the held asset's card, and
is printed by `run.py`. As everywhere, it is **informational only** — you decide and place
any order manually.

Two PnL columns are shown for each position: **Elder** (computed from the last *completed*
daily close — the same basis as the verdict) and **live** (the exchange mark price /
`unrealizedPnl`, which matches what Hyperliquid shows in real time). The verdict and the
"in profit" gate always use the Elder/close value, so they don't flicker with intraday
noise; the live column is there to reconcile with your exchange screen.

Indicators are exactly the ones in the spec — EMA13/EMA26, MACD-Histogram(12,26,9),
2-EMA Force Index (EMA-13 FI shown for context), Impulse color. Divergence warnings reuse
MACD-Histogram and Force Index; no extra indicators are introduced.

## Install

Python 3.11+ required.

```sh
# with uv (recommended)
uv sync

# or with a plain venv
python -m venv .venv && source .venv/bin/activate
pip install httpx pandas pyarrow fastapi uvicorn jinja2 python-dotenv apscheduler
pip install pytest ruff black   # dev tools
```

## Run the daily refresh

```sh
uv run python run.py
```

This validates the watchlist against the perp universe, incrementally refreshes weekly +
daily candles into `cache/*.parquet`, computes signals, writes `cache/snapshot.json`,
and prints the signals table.

## Open the dashboard

```sh
uv run python run.py --serve          # refresh, then serve
# then open http://127.0.0.1:8000
```

Optional once-a-day automatic refresh (manual run is the default workflow):

```sh
uv run python run.py --serve --schedule 06:00   # re-refresh daily at 06:00 UTC
```

The dashboard shows the signals table (with a clear banner when the 6% guard is active,
plus a green banner naming the best trade) and, per asset, weekly + daily TradingView
Lightweight Charts with Impulse-colored candles, EMA13/EMA26 overlays, MACD-Histogram /
Force Index panes, and a legend explaining every series. The table adds a **Current
price** column (latest completed daily close) and a **Score** column, is sorted
best-trade-first, and highlights the top pick. The header stays visible while scrolling,
and a filter (on by default) hides "stand aside" assets from both the table and the
chart cards — charts for hidden assets are only rendered if you reveal them.

## Configuration

Edit `config.toml`:

- `scanner.watchlist` — explicit coins, e.g. `["BTC", "ETH", "xyz:GOLD"]`, and/or
  wildcards: `"*"` scans **every** tradable native (crypto) Hyperliquid perp and
  `"xyz:*"` scans the whole HIP-3 `xyz` builder dex — the **tradfi** universe
  (stocks, indices, gold, oil, forex…). The default is `["*", "xyz:*"]`. Delisted
  assets are excluded; the refresh fires two requests per asset, so full universes
  take a few minutes and assets too new to have two completed weekly/daily bars are
  listed as skipped.
- `scanner.use_third_screen` / `scanner.third_screen_interval` — optional lower-timeframe
  entry timing, disabled by default and set to `4h` when enabled.
- `risk.equity` — account equity used for sizing
- `risk.risk_pct` — risk per trade (default `0.01` = 1%; hard-capped at 2%)
- `risk.equity_at_month_start`, `risk.month_realized_losses`, `risk.open_trade_risk` —
  bookkeeping inputs for the 6% Rule. `open_trade_risk` is now an optional manual
  add-on for trades not visible from the configured public address; visible
  Hyperliquid positions are risked automatically from their Elder trailing stop.
- `[strategy]` — tune the Elder scanner thresholds and ranking weights without editing
  code: flat weekly slope cutoff, EMA-penetration/channel/divergence lookbacks,
  minimum R:R, “excellent” R:R, weekly-tide strength scale, Force Index pullback
  scale, and score weights.
- `[journal]` — set `enabled` and `path` for the append-only JSONL scan journal.

- `positions.address` — **public** wallet address (0x…) used to read your open positions
  for trade management. Read-only: a public address only, never a private key; nothing is
  signed and no order is placed. Empty disables trade management.

`EQUITY`, `RISK_PCT`, and `HL_ADDRESS` can also be overridden via environment variables /
`.env` (see `.env.example`). No secrets are needed anywhere.

## Development

```sh
uv run pytest        # tests use recorded fixtures — no network
uv run ruff check .
uv run black .
```

## Attribution

The dashboard uses the [TradingView Lightweight Charts™](https://www.tradingview.com/lightweight-charts/)
library (Apache-2.0). Charts powered by TradingView — Copyright © TradingView, Inc.
<https://www.tradingview.com/>. The attribution logo on the charts is enabled as
required by the library's license.
