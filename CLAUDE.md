# CLAUDE.md — Elder Triple Screen Scanner (Hyperliquid)

## What this project is
A **read-only analysis tool** that watches a *small* set of perps tradable on
Hyperliquid and evaluates them with Alexander Elder's **Triple Screen** +
**Impulse** method (*The New Trading for a Living*), on **several timeframe
horizons at once** (swing, scalp, minutes). It produces a signals table per
horizon plus interactive charts, so the operator can make a discretionary entry
decision and place orders **manually**.

> This is an analysis/education tool, **not** an auto-trader and **not** financial advice.

## Hard constraints (never violate)
- **No order execution, no signing, no private keys.** Use only *public, keyless,
  read-only* endpoints on Hyperliquid's `info` endpoint: `candleSnapshot`, `meta`, and
  `clearinghouseState` (the last is a read-only account lookup of *open positions* for
  a **public** address, like a block explorer; it takes no key and signs nothing). The
  codebase must contain no private key, no exchange API key, no `exchange`/`order`/
  `signing` code path.
- **No auto-trading loop.** Output is informational only; a human decides and executes.
- **Don't invent indicators.** "Less is more": implement only the indicators listed in
  the Strategy Spec. Adding more indicators is a regression, not a feature. Data-quality
  checks (bar counts, volume) are *not* indicators — they describe the inputs, never
  change an action.
- **Don't scan the whole universe by default.** The watchlist is a short, explicit list;
  the `"*"` / `"<dex>:*"` wildcards still exist but are not the default.
- Keep secrets out of the repo. Config (equity, watchlist, risk %, horizons) lives in
  `.env` / `config.toml`, never hard-coded.

## Strategy Spec (canonical — do not drift)

Page references (`p.N`) are the **printed** page numbers of *The New Trading for a Living*
(`The New Trading for a Living.pdf` at the repo root: PDF page index = printed page + 16).
The compliance audit lives in `docs/AUDIT.md`.

### The three screens are roles, not fixed intervals
Elder's "factor of ~5" is applied to a **role**, so one implementation serves every
horizon. Per horizon:
- **tide** (first screen) — strategic bias: bull / bear / neutral from the slope of the
  tide EMA13; tiny slopes are treated as flat/no-trend.
- **wave** (second screen) — counter-trend oscillator that finds entries *against* the
  short-term wave but *with* the tide.
- **entry** (third screen) — entry *technique*: the stop-entry is timed off the latest
  completed bar of a lower timeframe.

### Horizons (config.toml `[[scanner.horizons]]`, current default)
| name    | tide | wave | entry | refresh    |
|---------|------|------|-------|------------|
| `swing` | `1w` | `1d` | `4h`  | once a day |
| `scalp` | `4h` | `1h` | `15m` | hourly     |
| `micro` | `1h` | `15m`| `5m`  | every 5 min|

Every horizon runs the same code and produces its own signals table, its own charts and
its own "best trade" pick; horizons are never ranked against each other. Horizons are
data, not code — adding or retuning one is a config edit.

### Indicators and canonical parameters (per the book's figures/text)
- `EMA_fast = 13`, `EMA_slow = 26` (exponential).
- `MACD-Histogram` = MACD(12, 26, 9) histogram. Only the **slope** (last bar vs previous
  bar) matters for Impulse, regardless of sign.
- `Force Index` = EMA(2) of `(close - prev_close) * volume`. Also expose EMA(13) FI for context.
- **Impulse** (per bar, computed on the tide *and* wave screens):
  - EMA13 rising **and** MACD-Hist rising → **green** (bullish).
  - EMA13 falling **and** MACD-Hist falling → **red** (bearish).
  - mixed → **blue** (neutral).

### Triple Screen decision logic
| Tide | Wave 2-EMA Force Index | Action       | Entry order                              |
|------|------------------------|--------------|------------------------------------------|
| Up   | dips **below** 0       | **Go long**  | buy-stop 1 tick above the prior bar's high, or limit at `EMA13 − avg downside penetration` |
| Up   | rising / above 0       | Stand aside  | none (chasing) |
| Down | rises **above** 0      | **Go short** | sell-stop 1 tick below the prior bar's low, or limit at `EMA13 + avg upside penetration` |
| Down | falling / below 0      | Stand aside  | none |

**Second-screen caveat (Elder, p.158):** take the wave Force Index signal only while
FI(2) is **not** also printing a *new multi-period low* (longs) / *high* (shorts) — a
fresh extreme means the move is accelerating, not a pullback, so stand aside.

**Chasing veto is the wave channel, and directional (Elder, p.168):** "never buy above the
upper channel line or sell short below the lower channel line". A long is vetoed only when
the wave close is **above** the upper line of the **wave** channel (the same EMA26 envelope
as the tide target, fit on wave bars); a short only when it is **below** the lower line. A
pullback extended the *other* way is an Elder bargain (its falling-knife guard is the
new-extreme caveat above), so it is **not** vetoed. The wave close's position versus the
EMA13–EMA26 value zone (`in_value` / `near_value` / `extended` beyond the channel) is shown
as context only.

**Impulse censorship overlay (applied last):** if the **tide** or the **wave** Impulse is
**red**, longs are forbidden; if either is **green**, shorts are forbidden. The Impulse
system says what *not* to do — it filters the table above.

> **Censorship is scoped to screens 1 and 2 only.** The third screen must **never** veto
> a trade. The Triple Screen buys *into* a pullback, so the entry timeframe prints red
> exactly when a long setup is valid (and green when a short is) — censoring on it would
> cancel the very setups the second screen just found. Its color is surfaced as context
> for the operator and nothing more. This is guarded by a regression test.

## Trade-management spec (open positions — exits)
The Triple Screen decides *entries*; managing an already-open position uses Elder's own
exit tools only — **no new indicators**. Positions are judged on the horizon named by
`scanner.positions_horizon` (default `swing`): a held position is strategic, so it is
managed on the same chain that would have entered it. Per held position, produce a verdict
`hold | take_profits | exit` from that horizon's tide+wave bars. Precedence
**exit > take_profits > hold**:
- **EXIT** if the **tide flips** against the position (the strategic premise is dead), or
  if **either** Impulse turns the *adverse* color (red for a long, green for a short —
  momentum reversed).
- **TAKE_PROFITS** if price reaches the profit target (tide value zone EMA13–EMA26, or
  the tide channel when price already trades beyond value), **or** when *neither* screen
  still shows the favorable Impulse color (both blue) **and** the trade is in profit —
  Elder's "permission to take profits" once the green/red is gone.
- **HOLD** otherwise; always surface a **SafeZone trailing-stop** suggestion (behind the
  recent wave extreme by the average adverse bar noise × a factor — **2 for longs, 3 for
  shorts** per Elder, since shorting near highs is noisier — ratcheted to ≥ break-even in profit).
Output is informational only; a human exits manually.

Divergence warnings reuse Elder indicators only: recent price/indicator disagreement on
MACD-Histogram or 13-EMA Force Index is surfaced in the signal reasons/dashboard, without
introducing new indicators. A divergence counts only when the indicator **crosses its zero
line between the two extremes** (Elder's "absolute must", p.87) — no crossover, no
divergence — and only when the two extremes sit ~20–40 bars apart (Elder/Lovvorn, p.88).

"Average penetration": over the last ~4–6 weeks *of wave bars*, measure how far pullbacks
pierce below (uptrend) / above (downtrend) the fast EMA — **one value per pullback**, its
deepest bar (a run of consecutive piercing bars is one pullback; Fig. 39.3, p.159–160: A–D);
average those penetrations; project the next bar's EMA (`today_EMA + (today_EMA −
yesterday_EMA)`) and offset by that average to set the limit.

**Every lookback is a count of bars on the relevant screen's timeframe**, never a wall-clock
duration. That is what lets one implementation serve a weekly tide and a 4h tide.

## Data-quality flags (not indicators)
Signals carry `data_warnings` describing when their *inputs* are weaker than they look.
They never change an action — they tell the operator how much to trust it:
- **Short tide history** — the EMA26 is seeded at the first bar (`adjust=False`), so with
  few bars a large share of it is still that seed; ~60 bars puts the seed under 1%. The
  tradfi perps on the `xyz` dex are recent listings, so this fires on the swing horizon
  today (e.g. `xyz:SP500` had 27 weekly bars at time of writing).
- **Near-frozen market** — a tradfi perp over the weekend still prints bars, at a fraction
  of normal volume and range. Force Index is volume-scaled, so those bars flatten every
  indicator and an intraday signal read off them is noise.

## Risk module (the two pillars)
- **2% Rule:** `max_risk_per_trade = equity * risk_pct` with `risk_pct` default **1%**,
  hard cap **2%**. Position size = `floor(max_risk_per_trade / abs(entry - stop))`
  ("Iron Triangle"). Never silently exceed the cap.
- **6% Rule:** if `month_realized_losses + sum(open_trade_risk) >= 0.06 * equity_at_month_start`,
  block all new-entry suggestions for the rest of the month (flag clearly in the UI). The
  guard is **global**, computed once across every horizon.
- **Targets:** profit target on the **tide** value zone (between EMA13 and EMA26) or a
  tide **channel** (Elder's percentage envelope around the slow **EMA26**, fit to contain
  ~95% of recent bars) when price already trades beyond value; **stop** on the **wave**.
  Reward:risk target ≥ **2:1**; **flag** setups below it (flag, don't hide — on short
  horizons the value-zone target tightens faster than the SafeZone stop, so sub-2:1 setups
  are the norm there and the operator needs to see them).
- **Horizon stacking:** each horizon sizes its suggestion as a *standalone* trade risking
  `risk_pct`. Taking setups from several horizons at once multiplies risk; the 6% rule caps
  total *open* risk, not the number of simultaneous suggestions. Say so in every UI.

## Architecture
- `data/hyperliquid.py` — public `info` client (`httpx`); `candleSnapshot` per coin/interval;
  validate watchlist against the perp `meta` universe; `clearinghouseState` open positions
  for a public address (read-only); cache OHLCV to parquet; parse string OHLCV fields
  to float; respect the 5000-candle limit.
- `data/provider.py` — the `MarketDataProvider` Protocol the pipeline is typed against
  (satisfied by `HyperliquidClient`).
- `indicators/` — pure functions on pandas DataFrames (EMA, MACD-Hist, Force Index, Impulse color).
- `strategy/triple_screen.py` — combines screens → per-asset `Signal` (action, reason,
  tide/wave impulse, suggested entry/stop/target, reward:risk, horizon, data warnings).
- `strategy/trade_management.py` — Elder exit logic for **open positions** → per-position
  `TradeManagement` (verdict hold/take_profits/exit, reasons, target, SafeZone trailing stop).
- `strategy/params.py` — `StrategyParams`: every tunable, all bar-counts. Overridable
  globally in `[strategy]` and per horizon in `[scanner.horizons.strategy]`.
- `risk/sizing.py` — 2% Iron Triangle + 6% monthly guard.
- `app/pipeline.py` — `build_snapshot` (every horizon) and `refresh_horizon` (one horizon,
  merged into the stored snapshot — what makes per-horizon cadences cheap).
- `app/` — **FastAPI** server rendering a dashboard: horizon tabs, a signals table per
  horizon + one card per asset with **tide / wave / entry charts** using **TradingView
  Lightweight Charts v5** (candles colored by Impulse, EMA13/EMA26 overlays, MACD-Hist +
  Force Index panes). The page polls `/api/snapshot` so an intraday horizon updates itself.
- `run.py` — one command does a full refresh + (re)builds the dashboard data;
  `--watch` runs one `apscheduler` job per horizon at its own `refresh_seconds`;
  `--horizon NAME` refreshes a single chain.

## Tech & conventions
- Python 3.11+, fully type-hinted; `ruff` + `black`; `pytest`.
- Unit tests must not hit the network — use recorded fixtures (`tests/fixtures/`) or
  `synthetic_candles()` with `httpx.MockTransport`.
- Lightweight Charts is Apache-2.0 but **requires TradingView attribution**: include the
  attribution notice / `attributionLogo` per its license in the dashboard.
- Config via `config.toml` (watchlist, horizons, equity, risk_pct) + `.env` for anything sensitive.
- Watchlist in `config.toml`: explicit coins (e.g. BTC, ETH, or tradfi perps like
  `xyz:GOLD`), `"*"` to scan the **entire** tradable native (crypto) perp universe, and
  `"<dex>:*"` to scan a whole HIP-3 builder dex — `"xyz:*"` is the tradfi universe
  (stocks, indices, gold, oil, forex…); delisted assets are excluded.
  **Current default — the six tradfi markets this operator trades**, mapped from their
  common names to the `xyz` dex (verified against live marks):
  `xyz:GOLD` (XAU), `xyz:SILVER` (XAG), `xyz:CL` (USOIL/WTI), `xyz:BRENTOIL` (UKOIL),
  `xyz:SP500` (US500), `xyz:XYZ100` (US100/Nasdaq-100).
  Open positions come from the public `clearinghouseState` of `positions.address`
  (queried per dex, so `xyz` positions are visible) and/or `[[positions.manual]]`.

## Definition of done (per phase)
1. Data layer fetches+caches every horizon's candles for the watchlist; tests on fixtures pass.
2. Indicators match hand-computed reference values on a known fixture (golden test).
3. Triple Screen + Impulse produce the correct action on crafted up/down/range fixtures,
   on any timeframe chain, and the third screen never vetoes.
4. Risk module returns correct size and correctly trips the 6% guard.
5. Dashboard renders a signals table + Impulse-colored tide/wave/entry charts per horizon.
