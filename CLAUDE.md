# CLAUDE.md — Elder Triple Screen Scanner (Hyperliquid)

## What this project is
A **read-only daily analysis tool** that watches a *small* set of Hyperliquid-tradable
perps and evaluates them with Alexander Elder's **Triple Screen** + **Impulse** method
(*The New Trading for a Living*). It produces, once per day, a signals table and
interactive charts so the operator can make a discretionary entry decision and place
orders **manually**.

> This is an analysis/education tool, **not** an auto-trader and **not** financial advice.

## Hard constraints (never violate)
- **No order execution, no signing, no private keys.** Use only Hyperliquid's *public*
  `info` endpoint (`candleSnapshot`, `meta`, and `clearinghouseState` — the last is a
  read-only account lookup of *open positions* for a **public** address, like a block
  explorer; it takes no key and signs nothing). The codebase must contain no private key,
  no exchange API key, no `exchange`/`order`/`signing` code path.
- **No auto-trading loop.** Output is informational only; a human decides and executes.
- **Don't invent indicators.** "Less is more": implement only the indicators listed in
  the Strategy Spec. Adding more indicators is a regression, not a feature.
- Keep secrets out of the repo. Config (equity, watchlist, risk %) lives in `.env` /
  `config.toml`, never hard-coded.

## Strategy Spec (canonical — do not drift)
Timeframes follow Elder's "factor of ~5", adapted to a 24/7 daily swing trader:
- **First screen (the tide)** = **weekly** (`1w`). Decide strategic bias: bull / bear / neutral; tiny EMA13 slopes are treated as flat/no-trend.
- **Second screen (the wave)** = **daily** (`1d`). Counter-trend oscillator finds entries
  *against* the short-term wave but *with* the weekly tide.
- **Third screen (entry)** = entry technique on the daily, with optional true lower-timeframe timing on `4h`.

Indicators and canonical parameters (per the book's figures/text):
- `EMA_fast = 13`, `EMA_slow = 26` (exponential).
- `MACD-Histogram` = MACD(12, 26, 9) histogram. Only the **slope** (last bar vs previous
  bar) matters for Impulse, regardless of sign.
- `Force Index` = EMA(2) of `(close - prev_close) * volume`. Also expose EMA(13) FI for context.
- **Impulse** (per bar, computed on weekly *and* daily):
  - EMA13 rising **and** MACD-Hist rising → **green** (bullish).
  - EMA13 falling **and** MACD-Hist falling → **red** (bearish).
  - mixed → **blue** (neutral).

Triple Screen decision logic:
| Weekly trend | Daily 2-EMA Force Index | Action       | Entry order                              |
|--------------|-------------------------|--------------|------------------------------------------|
| Up           | dips **below** 0        | **Go long**  | buy-stop 1 tick above prior day high, or limit at `EMA13 − avg downside penetration` |
| Up           | rising / above 0        | Stand aside  | none (chasing) |
| Down         | rises **above** 0       | **Go short** | sell-stop 1 tick below prior day low, or limit at `EMA13 + avg upside penetration` |
| Down         | falling / below 0       | Stand aside  | none |

**Second-screen caveat (Elder, p.158):** take the daily Force Index signal only while
FI(2) is **not** also printing a *new multi-week low* (longs) / *high* (shorts) — a fresh
extreme means the move is accelerating, not a pullback, so stand aside.

**Value-zone filter is directional (no chasing):** enter on a pullback *to* value, never
chasing. A long is vetoed only when the daily close is extended **above** the EMA13–EMA26
value zone; a short only when extended **below** it. A pullback extended the *other* way is
an Elder bargain (its falling-knife guard is the new-extreme caveat above), so it is **not**
vetoed by the value zone.

**Impulse censorship overlay (applied last):** if weekly **or** daily Impulse is **red**,
longs are forbidden; if weekly **or** daily Impulse is **green**, shorts are forbidden.
The Impulse system says what *not* to do — it filters the table above.

## Trade-management spec (open positions — exits)
The Triple Screen decides *entries*; managing an already-open position uses Elder's own
exit tools only — **no new indicators**. Per held position, produce a verdict
`hold | take_profits | exit` from the same weekly+daily bars. Precedence
**exit > take_profits > hold**:
- **EXIT** if the **weekly tide flips** against the position (the strategic premise is
  dead), or if **either** Impulse turns the *adverse* color (red for a long, green for a
  short — momentum reversed).
- **TAKE_PROFITS** if price reaches the profit target (weekly value zone EMA13–EMA26, or
  the weekly channel when price already trades beyond value), **or** when *neither* screen
  still shows the favorable Impulse color (both blue) **and** the trade is in profit —
  Elder's "permission to take profits" once the green/red is gone.
- **HOLD** otherwise; always surface a **SafeZone trailing-stop** suggestion (behind the
  recent daily extreme by the average EMA penetration, ratcheted to ≥ break-even in profit).
Output is informational only; a human exits manually.

Divergence warnings reuse Elder indicators only: recent price/indicator disagreement on MACD-Histogram or 13-EMA Force Index is surfaced in the signal reasons/dashboard, without introducing new indicators. A divergence counts only when the indicator **crosses its zero line between the two extremes** (Elder's "absolute must", p.103) — no crossover, no divergence.

"Average penetration": over the last ~4–6 weeks, measure how far pullbacks pierce below
(uptrend) / above (downtrend) the fast EMA; average those penetrations; project tomorrow's
EMA (`today_EMA + (today_EMA − yesterday_EMA)`) and offset by that average to set the limit.

## Risk module (the two pillars)
- **2% Rule:** `max_risk_per_trade = equity * risk_pct` with `risk_pct` default **1%**,
  hard cap **2%**. Position size = `floor(max_risk_per_trade / abs(entry - stop))`
  ("Iron Triangle"). Never silently exceed the cap.
- **6% Rule:** if `month_realized_losses + sum(open_trade_risk) >= 0.06 * equity_at_month_start`,
  block all new-entry suggestions for the rest of the month (flag clearly in the UI).
- **Targets:** profit target on the **weekly** value zone (between EMA13 and EMA26) or a
  weekly channel; **stop** on the **daily**. Reward:risk target ≥ **2:1**; flag setups below it.

## Architecture
- `data/hyperliquid.py` — public `info` client (`httpx`); `candleSnapshot` per coin/interval;
  validate watchlist against the perp `meta` universe; `clearinghouseState` open positions
  for a public address (read-only); cache OHLCV to parquet/SQLite; parse string OHLCV fields
  to float; respect the 5000-candle limit.
- `indicators/` — pure functions on pandas DataFrames (EMA, MACD-Hist, Force Index, Impulse color).
- `strategy/triple_screen.py` — combines screens → per-asset `Signal` (action, reason,
  weekly/daily impulse, suggested entry/stop/target, reward:risk).
- `strategy/trade_management.py` — Elder exit logic for **open positions** → per-position
  `TradeManagement` (verdict hold/take_profits/exit, reasons, target, SafeZone trailing stop).
- `risk/sizing.py` — 2% Iron Triangle + 6% monthly guard.
- `app/` — **FastAPI** server rendering a dashboard: a daily **signals table** at top +
  one card per asset with **weekly and daily charts** using **TradingView Lightweight
  Charts v5** (candles colored by Impulse, EMA13/EMA26 overlays, MACD-Hist + Force Index panes).
- `run.py` — one command does a full daily refresh + (re)build the dashboard data.
  Optional `apscheduler` job for a once-a-day refresh; manual run is the default.

## Tech & conventions
- Python 3.11+, fully type-hinted; `ruff` + `black`; `pytest`.
- Unit tests must not hit the network — use recorded fixtures for indicator/strategy tests.
- Lightweight Charts is Apache-2.0 but **requires TradingView attribution**: include the
  attribution notice / `attributionLogo` per its license in the dashboard.
- Config via `config.toml` (watchlist, intervals, equity, risk_pct) + `.env` for anything sensitive.
- Watchlist in `config.toml`: explicit coins (e.g. BTC, ETH, or tradfi perps like
  `xyz:GOLD`), `"*"` to scan the **entire** tradable native (crypto) perp universe, and
  `"<dex>:*"` to scan a whole HIP-3 builder dex — `"xyz:*"` is the tradfi universe
  (stocks, indices, gold, oil, forex…). Current default: `["*", "xyz:*"]`; delisted
  assets excluded.

## Definition of done (per phase)
1. Data layer fetches+caches weekly & daily candles for the watchlist; tests on fixtures pass.
2. Indicators match hand-computed reference values on a known fixture (golden test).
3. Triple Screen + Impulse produce the correct action on crafted up/down/range fixtures.
4. Risk module returns correct size and correctly trips the 6% guard.
5. Dashboard renders the signals table + per-asset weekly/daily Impulse-colored charts.
