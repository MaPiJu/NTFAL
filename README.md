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

- **First screen (tide):** weekly chart — strategic bias from the slope of the weekly EMA13.
- **Second screen (wave):** daily chart — the 2-EMA Force Index looks for pullbacks
  *against* the daily wave but *with* the weekly tide.
- **Third screen (entry):** buy-stop 1 tick above the prior day's high (longs) /
  sell-stop 1 tick below the prior day's low (shorts), plus an alternative limit at the
  projected EMA13 offset by the average pullback penetration.
- **Impulse censorship (applied last):** any **red** Impulse (weekly or daily) forbids
  longs; any **green** forbids shorts.
- **Risk:** 2% Rule (Iron Triangle sizing, default 1% risk per trade, hard cap 2%) and
  the 6% monthly guard that blocks all new entries once monthly losses + open risk reach
  6% of the month-start equity.

Indicators are exactly the ones in the spec — EMA13/EMA26, MACD-Histogram(12,26,9),
2-EMA Force Index (EMA-13 FI shown for context), Impulse color. Nothing else.

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

The dashboard shows the signals table (with a clear banner when the 6% guard is active)
and, per asset, weekly + daily TradingView Lightweight Charts with Impulse-colored
candles, EMA13/EMA26 overlays, and MACD-Histogram / Force Index panes.

## Configuration

Edit `config.toml`:

- `scanner.watchlist` — explicit coins, e.g. `["BTC", "ETH", "SOL", "HYPE"]`, or `["*"]`
  to scan **every** tradable Hyperliquid perp (delisted assets are excluded; the refresh
  fires two requests per asset, so the full universe takes a few minutes and assets too
  new to have two completed weekly/daily bars are listed as skipped)
- `risk.equity` — account equity used for sizing
- `risk.risk_pct` — risk per trade (default `0.01` = 1%; hard-capped at 2%)
- `risk.equity_at_month_start`, `risk.month_realized_losses`, `risk.open_trade_risk` —
  manual bookkeeping inputs for the 6% Rule (the tool is read-only and does not track
  your trades)

`EQUITY` and `RISK_PCT` can also be overridden via environment variables / `.env`
(see `.env.example`). No secrets are needed anywhere.

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
