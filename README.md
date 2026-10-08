# Elder Triple Screen Scanner — Hyperliquid

A **read-only analysis tool** that watches a small set of perps tradable on Hyperliquid
and evaluates them with Alexander Elder's **Triple Screen** + **Impulse** method
(*The New Trading for a Living*) — on **three timeframe horizons at once**. It produces a
signals table and interactive charts per horizon so the operator can make a
**discretionary** entry decision and place orders **manually**.

> **Disclaimer:** this is an analysis/education tool, **not financial advice** and
> **not an auto-trader**. It places no orders, holds no keys, and only ever calls
> public read-only endpoints on Hyperliquid's `info` API.

## Horizons

Elder's "factor of ~5" is applied to a **role**, not a fixed interval, so one
implementation serves every horizon:

| Horizon | Tide (1st screen) | Wave (2nd screen) | Entry (3rd screen) | Refreshed |
|---------|-------------------|-------------------|--------------------|-----------|
| **swing** — days → weeks | `1w` | `1d` | `4h` | once a day |
| **scalp** — hours | `4h` | `1h` | `15m` | hourly |
| **micro** — minutes | `1h` | `15m` | `5m` | every 5 min |

Each horizon runs the same Elder logic and gets its own tab, table, charts and
"best trade" pick. Horizons are never ranked against one another, and they are
**config, not code** — adding or retuning one is a `config.toml` edit.

⚠️ **Every horizon sizes its suggestion as a standalone trade** risking `risk_pct`.
The same asset can be a swing long and a micro short. Taking several at once multiplies
your risk — the 6% rule caps total *open* risk, not the number of suggestions.

## The watched markets

The default watchlist is the six tradfi markets on Hyperliquid's HIP-3 `xyz` dex.
Their common names differ from the Hyperliquid tickers:

| Common name | Hyperliquid perp |
|-------------|------------------|
| XAU — gold | `xyz:GOLD` |
| XAG — silver | `xyz:SILVER` |
| USOIL — WTI crude | `xyz:CL` |
| UKOIL — Brent crude | `xyz:BRENTOIL` |
| US500 — S&P 500 | `xyz:SP500` |
| US100 — Nasdaq-100 | `xyz:XYZ100` |

### Trading sessions (weekends)

Hyperliquid prints these perps 24/7, but the markets behind them close for the weekend:
from Friday 21:00 to Sunday 22:00 UTC their bars carry a fraction of the usual volume and
range, and they would flatten every EMA. Elder counts trading days (five a week), so the
`xyz` dex has a weekend calendar (`[sessions.xyz]` in `config.toml`):

- bars lying **entirely** inside the closure are dropped before any indicator — Saturday's
  daily bar, about 26% of 4h bars and 29% of 1h/15m/5m bars; a bar straddling the close
  or the reopening (Friday's daily bar, the Sunday 22:00 hour) is kept;
- the **weekly tide is rebuilt from the Monday–Friday daily bars** (one bar per week, open
  Monday, close Friday): Hyperliquid's own 1w candles open on **Thursday** (epoch
  alignment) and carry the weekend;
- weekday holidays are not in the calendar — the near-frozen-market flag covers them;
- on a weekend the signal is the one of the last session bar, until the reopening.

Measured on a cache of the six perps taken 2026-10-08 13:07 UTC (a Thursday), same code
and same bars with and without the calendar: 9 of the 18 current signals change a level,
an Impulse or the R:R (e.g. `xyz:GOLD` swing wave Impulse red → blue, `xyz:SP500` swing tide
Impulse green → blue, `xyz:CL` micro R:R 0.36 → 1.05), none changes its action. Replaying
every wave-bar close (swing and scalp: last 200 wave bars; micro: last 800), the action
differs at 9% / 3% / 1% of weekday decisions (swing / scalp / micro) and the tide at
20% / 9% / 2%; on weekend decision times the action differs at 7% / 38% / 49%, since
without the calendar the frozen weekend bars drive the signal. A dex without a
`[sessions.<dex>]` block — the native crypto perps — is left 24/7.

## How it works

- **First screen (tide):** strategic bias from the slope of the tide EMA13, with tiny
  slopes treated as **flat/no-trend** so ranges do not become false signals.
- **Second screen (wave):** the 2-EMA Force Index looks for pullbacks *against* the wave
  but *with* the tide. Elder never buys above the upper channel line nor sells short
  below the lower one, so a long is refused when the latest completed wave close is above
  the upper line of the **wave channel**, a short when it is below the lower line — the
  scanner does not chase. Where the close sits versus the EMA13-EMA26 **value zone** is
  shown as context.
- **Third screen (entry):** buy-stop 1 tick above the prior bar's high (longs) /
  sell-stop 1 tick below the prior bar's low (shorts), timed off the latest completed
  bar of the entry timeframe, plus an alternative limit at the projected EMA13 offset by
  the average pullback penetration. The stop-entry is a theoretical Elder order: if
  unfilled it is lowered (raised, for a short) each wave bar to the latest completed bar's
  high/low, until filled — it stays valid as long as the tide holds and no Impulse
  censors the trade (Elder: "until the weekly indicator reverses"), with no fixed expiry.
  The third screen **times** the entry; it never vetoes it (see below).
  Every level sits on Hyperliquid's price grid, rounded on the prudent side: buy-stop
  up / sell-stop down, a long's stop and buy limit down / a short's stop and sell limit
  up, the target toward the entry. Reward:risk and the size come from the rounded levels.
- **Impulse censorship (applied last):** any **red** Impulse on the **tide or wave**
  forbids longs; any **green** forbids shorts.
- **Best-trade ranking — Trade Apgar:** every validated setup is scored with Elder's
  Trade Apgar (p.238–242), five questions at 0/1/2 written for this "pullback to value"
  system (for a long; mirrored for a short): tide Impulse (green 2, blue 1, red 0), wave
  Impulse (blue 2, green 1, red 0), wave close vs value (below 2, in the zone 1, above 0),
  reward:risk (≥ 2 → 2, ≥ 1 → 1, below → 0), wave divergence (bullish 2, none 1,
  bearish 0). An **A-trade** totals 7+ with no zero; the A-trade with the best Apgar
  (ties: better R:R) is that horizon's **best trade** (`★`), and there is none without
  an A-trade. The table is sorted best-first and shows the five lines. The 6% guard
  suppresses any pick.
- **Divergences:** recent bullish/bearish divergences between price and MACD-Histogram /
  13-EMA Force Index are surfaced as Elder warnings.
- **Data-quality flags:** a signal says when its own *inputs* are weak — a tide series too
  short for a converged EMA26, or a near-frozen market (a tradfi perp on a weekday
  holiday still prints bars on ~5–10% of normal volume; weekends are dropped by the
  session calendar above). Flags never change an action.
- **Risk:** 2% Rule (Iron Triangle sizing, default 1% risk per trade, hard cap 2%, on the
  equity of the first day of the month) and
  the 6% monthly guard that blocks all new entries once monthly losses + open risk reach
  6% of the month-start equity. When a public address is configured, open risk is
  calculated automatically from each held position's current Elder stop; the manual
  `open_trade_risk` field is only extra risk for positions the scanner cannot see.
  A size the exchange would refuse — a notional above the perp's max leverage × equity,
  or under Hyperliquid's $10 minimum order — is flagged (`⚠` on the size), never capped.
- **Funding:** each signal shows the perp's current hourly funding rate (public
  `metaAndAssetCtxs`; positive = longs pay shorts) and what the trade would pay over the
  horizon's `holding_hours` (swing 14 d, scalp 2 d, micro 4 h), as % of notional
  (positive = paid). It is flagged when it exceeds half the trade's risk — on 2026-10-08
  a 14-day `xyz:BRENTOIL` short paid ≈ 10.4% of notional — but it never changes an action.
- **Journal:** each refresh can append a compact JSONL entry with the per-horizon top
  picks, signal levels/reasons, open-position verdicts, stops and open risk.
- **SafeZone stops:** protective and trailing stops use Elder-style adverse bar noise
  (average downside low undercuts for longs / upside high breakouts for shorts), not
  simple volatility; profitable trades are ratcheted to at least break-even.
- **Trade management (open positions):** for trades you already hold, Elder's exit tools
  give a verdict — **hold**, **take profits**, or **exit** (see below).

### Why the third screen never vetoes

The Triple Screen buys *into* a pullback, so the entry timeframe prints **red** exactly
when a long setup is valid (and green when a short is). Censoring on it would cancel the
very setups the second screen just found — measured on the live watchlist, it silenced
every signal on all three horizons. Elder's Impulse censorship applies to screens 1 and 2;
the third screen is an entry *technique*. Its color is shown as context and nothing more.
A regression test guards this.

### A note on reward:risk at short horizons

The profit target is the tide value zone, which tightens around price on short
timeframes, while the SafeZone stop (noise × 2 or × 3) does not shrink proportionally.
So on the scalp and micro horizons most setups land **below** Elder's 2:1 floor —
measured over ~3 700 historical evaluations, roughly 10% of actionable setups clear it,
versus ~31% on swing. That is Elder's own point: most scalps do not pay for their risk.
The tool **flags** sub-2:1 setups (`⚠` on the R:R cell) rather than hiding them, exactly
as the spec asks. Expect roughly one qualifying scalp setup a day across the six markets.

## Managing open trades

The Triple Screen says *when to enter*; this answers the other question — *"I already
hold this, do I hold, take profits, or get out?"* If you set a **public** wallet address
in `config.toml` (`[positions].address`) the tool reads your **open positions** from
Hyperliquid's public `clearinghouseState` info endpoint — a read-only account lookup,
like a block explorer. **No private key, no signing, no order** is ever involved, and you
can leave the field empty to disable the feature. `clearinghouseState` is queried per dex,
so positions on the `xyz` tradfi dex are found too.

Positions are judged on the horizon named by `scanner.positions_horizon` (default
`swing`): a held position is strategic, so it is managed on the chain that would have
entered it. For each one, only Elder's own exit logic applies (no new indicators):

- **Impulse used for exits.** While long you may keep holding as long as the Impulse is
  **green**; once *neither* the tide nor the wave Impulse is green any more (both have
  gone **blue**) the prohibition against selling is lifted → **permission to take
  profits** (only flagged while in profit). A **red** Impulse goes further — momentum has
  reversed → **exit**. Mirror image for shorts.
- **Premise invalidated.** If the **tide flips** against the position, the reason you took
  the trade is gone → **exit** ("the trade no longer earns its risk").
- **Profit target.** When price reaches the **tide value zone** (EMA13–EMA26) or, if it
  already trades beyond value, the tide channel → **take profits**.
- **Trailing stop (SafeZone).** A suggested stop tucked behind the recent wave extreme by
  the average adverse noise, ratcheted to at least break-even once the trade is in profit.
  It never moves back (Elder: "move your stop only in the direction of your trade"): the
  snapshot remembers the last suggestion per position (asset, side, entry price), and the
  next refresh can only tighten it; a new entry price starts fresh.

Verdict precedence is **exit > take profits > hold**. The result appears as an "Open
positions" table at the top of the dashboard and as a panel on the held asset's card, and
is printed by `run.py`. As everywhere, it is **informational only** — you decide and place
any order manually.

Two PnL columns are shown for each position: **Elder** (computed from the last *completed*
wave close — the same basis as the verdict) and **live** (the exchange mark price /
`unrealizedPnl`, which matches what Hyperliquid shows in real time). The verdict and the
"in profit" gate always use the Elder/close value, so they don't flicker with intraday
noise; the live column is there to reconcile with your exchange screen. A **funding paid**
column (Hyperliquid's `cumFunding.sinceOpen`, from the same read-only `clearinghouseState`
call; negative = received) shows the holding cost neither PnL includes — in the dashboard,
the CLI positions table and the journal.

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

## Run

```sh
uv run python run.py                  # one full refresh of every horizon
uv run python run.py --horizon micro  # refresh just one chain and merge it in
```

This validates the watchlist against the perp universe, incrementally refreshes each
horizon's candles into `cache/*.parquet`, computes signals, writes `cache/snapshot.json`,
and prints one table per horizon. A full refresh of six assets across three horizons is
36 candle requests, about 20 seconds.

## Open the dashboard

```sh
uv run python run.py --serve            # refresh, then serve
uv run python run.py --serve --watch    # …and keep every horizon fresh
# then open http://127.0.0.1:8000
```

`--watch` starts one `apscheduler` job per horizon at its own `refresh_seconds`
(swing daily, scalp hourly, micro every 5 minutes). A partial refresh rewrites only that
horizon's block of the snapshot, so the swing block keeps its own timestamp. Without
`--serve`, `--watch` just keeps the snapshot up to date in the foreground.

The dashboard shows **one tab per horizon** (remembered across reloads), each with its
chain (`4h / 1h / 15m`) and how long ago it was refreshed. Per tab: the signals table
— with a banner when the 6% guard is active, a green banner naming that horizon's best
trade, and a standing note about stacking risk across horizons — and, per asset, tide /
wave / entry TradingView Lightweight Charts with Impulse-colored candles, EMA13/EMA26
overlays, MACD-Histogram / Force Index panes, and a legend explaining every series.
The table header stays visible while scrolling, and a filter (on by default) hides
"stand aside" assets from both the table and the chart cards — charts for hidden assets
are only rendered if you reveal them. The page re-polls `/api/snapshot` every minute and
re-renders when any horizon has been refreshed, so an intraday tab updates itself.

## Configuration

Edit `config.toml`:

- `scanner.watchlist` — explicit perps, e.g. `["xyz:GOLD", "BTC"]`, and/or wildcards:
  `"*"` scans **every** tradable native (crypto) Hyperliquid perp and `"xyz:*"` scans the
  whole HIP-3 `xyz` builder dex — the **tradfi** universe (stocks, indices, gold, oil,
  forex…). The default is the six markets in the table above. Delisted assets are
  excluded; a full universe fires several requests per asset per horizon, so it takes
  minutes, and assets without two completed bars on a screen are listed as skipped.
- `scanner.positions_horizon` — which horizon's bars manage open positions (default
  `"swing"`).
- `[[scanner.horizons]]` — one block per timeframe chain: `name`, `label`, the three
  intervals (`tide`, `wave`, `entry`), how much history to keep (`lookback_*`, in bars),
  `refresh_seconds`, `min_tide_bars` (below which the tide is flagged as
  not-yet-converged) and `holding_hours` (typical holding time, for the funding-cost
  estimate; omit it for no estimate). Any Hyperliquid candle interval works: `1m`…`1w`.
- `[strategy]` — tune the Elder thresholds without editing code:
  flat tide-slope cutoff, EMA-penetration/channel/divergence lookbacks, SafeZone
  lookback/factors, minimum R:R (also the Apgar's full-marks R:R), and the low-volume
  data-quality thresholds. **Every lookback is a count of bars** on the
  relevant screen, so the same numbers carry across horizons. Any key can be overridden
  for one horizon with a `[scanner.horizons.strategy]` sub-block.
- `risk.equity` — current account equity (shown in the header, and the margin behind
  the max-leverage check)
- `risk.risk_pct` — risk per trade (default `0.01` = 1%; hard-capped at 2%), as a fraction
  of `risk.equity_at_month_start`: Elder sets the 2% limit once a month, from the equity on
  the first day of the month
- `risk.equity_at_month_start`, `risk.month_realized_losses`, `risk.open_trade_risk` —
  bookkeeping inputs for the 2% and 6% Rules. `open_trade_risk` is an optional manual add-on for
  trades not visible from the configured public address; visible positions are risked
  automatically from their Elder trailing stop.
- `positions.address` — **public** wallet address (0x…) used to read your open positions
  for trade management. Read-only: a public address only, never a private key; nothing is
  signed and no order is placed. Empty disables trade management.
- `[[positions.manual]]` — manually declared open positions, for a trade the configured
  address cannot see. Each entry takes `asset` (watchlist naming, e.g. `"xyz:GOLD"`),
  `side` (`"long"`/`"short"`), `size` (asset units) and `entry` (price); they get the same
  Elder exit analysis as positions read from Hyperliquid.
- `[journal]` — set `enabled` and `path` for the append-only JSONL scan journal.

`EQUITY`, `RISK_PCT`, and `HL_ADDRESS` can also be overridden via environment variables /
`.env` (see `.env.example`). No secrets are needed anywhere.

## Development

```sh
uv run pytest        # tests use recorded fixtures / mock transports — no network
uv run ruff check .
uv run black .
```

## Attribution

The dashboard uses the [TradingView Lightweight Charts™](https://www.tradingview.com/lightweight-charts/)
library (Apache-2.0). Charts powered by TradingView — Copyright © TradingView, Inc.
<https://www.tradingview.com/>. The attribution logo on the charts is enabled as
required by the library's license.
