/* Elder Triple Screen dashboard — renders the snapshot with
 * TradingView Lightweight Charts v5 (attribution logo enabled per license).
 *
 * The snapshot carries one block per horizon (timeframe chain); the page shows
 * one horizon at a time, chosen with the tabs and remembered across reloads.
 * Horizons refresh on their own cadence server-side, so the page re-polls and
 * re-renders whenever a block's `generated_at` changes. */

// The charting library is loaded from a CDN. If that fetch fails (offline, a
// proxy, a blocked CDN) the tables must still render — they carry the actual
// decision data, the charts only illustrate it. So this is read defensively
// instead of destructured at the top level, which would abort the whole script.
const LWC = window.LightweightCharts;

const POLL_MS = 60_000;
const STORAGE_KEY = "elder.horizon";
const SCREEN_LABEL = { tide: "Tide (1st screen)", wave: "Wave (2nd screen)", entry: "Entry (3rd screen)" };

const CHART_OPTS = {
  height: 480,
  layout: {
    background: { color: "#161b22" },
    textColor: "#c9d1d9",
    attributionLogo: true, // required TradingView attribution
    panes: { separatorColor: "#2d333b" },
  },
  grid: {
    vertLines: { color: "#21262d" },
    horzLines: { color: "#21262d" },
  },
  timeScale: { borderColor: "#2d333b", timeVisible: true },
  rightPriceScale: { borderColor: "#2d333b" },
};

// Page state: the whole snapshot plus which horizon is on screen.
const state = { snapshot: null, horizon: null, stamps: "" };

function fmt(x, digits = 5) {
  if (x === null || x === undefined) return "—";
  return Number(x).toLocaleString("en-US", { maximumSignificantDigits: digits });
}

function impulseDot(color) {
  if (!color) return "—";
  return `<span class="dot ${color}" title="${color}">●</span>`;
}

function ago(iso) {
  if (!iso) return "—";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 90) return `${Math.round(secs)}s ago`;
  if (secs < 5400) return `${Math.round(secs / 60)}min ago`;
  if (secs < 172800) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function horizonNames(snapshot) {
  const blocks = snapshot.horizons || {};
  const order = snapshot.horizon_order || Object.keys(blocks);
  return order.filter((n) => blocks[n]);
}

function currentBlock() {
  return (state.snapshot.horizons || {})[state.horizon] || null;
}

function chainOf(block) {
  const iv = block.intervals || {};
  return [iv.tide, iv.wave, iv.entry].filter(Boolean).join(" / ");
}

// A fingerprint of every block's timestamp: when it changes, something was
// refreshed and the page needs re-rendering.
function stampsOf(snapshot) {
  return horizonNames(snapshot)
    .map((n) => `${n}:${snapshot.horizons[n].generated_at}`)
    .join("|");
}

// Best trade first: tradable setups ranked by Elder quality score (desc),
// then the stand-aside rest by name. Returns a sorted copy.
function rankedSignals(block) {
  return [...block.signals].sort((a, b) => {
    const aside = (s) => (s.action === "stand_aside" ? 1 : 0);
    if (aside(a) !== aside(b)) return aside(a) - aside(b);
    if (aside(a) === 1) return a.asset.localeCompare(b.asset);
    return (b.quality_score ?? 0) - (a.quality_score ?? 0);
  });
}

// Live mark (still-open bar) + how far it has drifted from the closed-bar basis
// the signal was computed on. A drifted/alerting row is flagged so the operator
// does not act on a stale signal.
function markCell(s) {
  if (s.live_price === null || s.live_price === undefined) return "—";
  let drift = "";
  if (s.last_close) {
    const pct = (s.live_price / s.last_close - 1) * 100;
    drift = ` <span class="drift">(${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%)</span>`;
  }
  const cls = s.price_alert ? ' class="rr-bad"' : "";
  return `<span${cls} title="${s.price_alert || ""}">${fmt(s.live_price)}${drift}</span>`;
}

function scoreCell(s) {
  if (s.quality_score === null || s.quality_score === undefined) return "—";
  const pct = Math.round(s.quality_score * 100);
  const star = s.is_top_pick ? ' <span class="top-star" title="best trade">★</span>' : "";
  return `<span class="score">${pct}</span>${star}`;
}

function warningsHTML(s) {
  const warns = s.data_warnings || [];
  if (!warns.length) return "";
  return `<br><strong class="warn">! Data quality:</strong> <span class="warn">${warns.join(" · ")}</span>`;
}

// The suggested size breaks an exchange limit (max leverage, $10 minimum order).
// Flagged, never capped: the size shown is still the Iron Triangle's.
function sizeCell(s) {
  if (!s.position_size) return "—";
  const warns = s.size_warnings || [];
  const size = fmt(s.position_size.size, 6);
  if (!warns.length) return size;
  const title = warns.join(" · ").replace(/"/g, "&quot;");
  return `<span class="rr-bad" title="${title}">${size} ⚠</span>`;
}

function sizeWarningsHTML(s) {
  const warns = s.size_warnings || [];
  if (!warns.length) return "";
  return `<br><strong class="warn">! Size:</strong> <span class="warn">${warns.join(" · ")}</span>`;
}

function pctText(x, digits = 2) {
  return `${x >= 0 ? "+" : ""}${(x * 100).toFixed(digits)}%`;
}

function holdingLabel(hours) {
  return hours >= 24 && hours % 24 === 0 ? `${hours / 24} d` : `${hours} h`;
}

// Current hourly funding rate (+: longs pay shorts) and, for a trade, the funding
// it would pay over the horizon's holding time, as % of notional (+: paid).
function fundingCell(s) {
  if (s.funding_rate === null || s.funding_rate === undefined) return "—";
  let html = `${pctText(s.funding_rate, 4)}/h`;
  if (s.funding_cost !== null && s.funding_cost !== undefined) {
    const cls = s.funding_cost > 0 ? "rr-bad" : "rr-good";
    const flag = s.funding_warning ? " ⚠" : "";
    html +=
      `<br><span class="${cls}" title="${(s.funding_warning || "").replace(/"/g, "&quot;")}">` +
      `${pctText(s.funding_cost)} / ${holdingLabel(s.funding_hours)}${flag}</span>`;
  }
  return html;
}

function fundingWarningHTML(s) {
  if (!s.funding_warning) return "";
  return `<br><strong class="warn">! Funding:</strong> <span class="warn">${s.funding_warning}</span>`;
}

function warnBadge(s) {
  const warns = s.data_warnings || [];
  if (!warns.length) return "";
  return ` <span class="warn" title="${warns.join(" · ").replace(/"/g, "&quot;")}">!</span>`;
}

// --- Horizon tabs ---------------------------------------------------------

function renderTabs() {
  const nav = document.getElementById("horizon-tabs");
  nav.innerHTML = "";
  for (const name of horizonNames(state.snapshot)) {
    const block = state.snapshot.horizons[name];
    const tab = document.createElement("button");
    tab.className = "tab" + (name === state.horizon ? " active" : "");
    tab.innerHTML =
      `<span class="tab-label">${block.label || name}</span>` +
      `<span class="tab-chain">${chainOf(block)}</span>` +
      `<span class="tab-stamp">${ago(block.generated_at)}</span>`;
    tab.addEventListener("click", () => {
      state.horizon = name;
      try {
        localStorage.setItem(STORAGE_KEY, name);
      } catch {
        /* private mode: the tab choice just won't be remembered */
      }
      renderAll();
    });
    nav.appendChild(tab);
  }
}

// --- Signals table --------------------------------------------------------

function renderTable(block) {
  const tbody = document.querySelector("#signals-table tbody");
  tbody.innerHTML = "";
  for (const s of rankedSignals(block)) {
    const rr = s.reward_risk;
    const rrCell =
      rr === null
        ? "—"
        : `<span class="${s.rr_ok ? "rr-good" : "rr-bad"}">${rr.toFixed(2)}${s.rr_ok ? "" : " ⚠"}</span>`;
    const limitRr = s.reward_risk_limit;
    const limitRrCell = limitRr === null || limitRr === undefined ? "—" : limitRr.toFixed(2);
    const row = document.createElement("tr");
    row.dataset.action = s.action;
    if (s.is_top_pick) row.classList.add("top-pick");
    row.innerHTML = `
      <td><strong>${s.asset}</strong>${warnBadge(s)}</td>
      <td>${s.market_regime ?? "—"}</td>
      <td>${s.tide_trend}</td>
      <td>${impulseDot(s.tide_impulse)} / ${impulseDot(s.wave_impulse)} / ${impulseDot(s.entry_impulse)}</td>
      <td>${fmt(s.force_index_2, 4)}</td>
      <td><span class="badge ${s.action}">${s.action.replace("_", " ")}${s.price_alert ? " ⚠" : ""}</span></td>
      <td>${fmt(s.last_close)}</td>
      <td>${markCell(s)}</td>
      <td>${fmt(s.entry)}</td>
      <td>${fmt(s.stop)}</td>
      <td>${rrCell}</td>
      <td>${fmt(s.entry_limit)}</td>
      <td>${fmt(s.entry_limit_stop)}</td>
      <td>${limitRrCell}</td>
      <td>${fmt(s.target)}</td>
      <td>${scoreCell(s)}</td>
      <td>${sizeCell(s)}</td>
      <td>${fundingCell(s)}</td>
      <td class="reason">${s.reason}<br><strong>Value zone:</strong> ${(s.value_zone_status || "—").replace("_", " ")}${s.price_alert ? `<br><strong>⚠ Live price:</strong> ${s.price_alert}` : ""}${s.entry_order_plan ? `<br><strong>Order plan:</strong> ${s.entry_order_plan}` : ""}${(s.divergences || []).length ? `<br><strong>Divergences:</strong> ${s.divergences.join(", ")}` : ""}${warningsHTML(s)}${sizeWarningsHTML(s)}${fundingWarningHTML(s)}</td>`;
    tbody.appendChild(row);
  }
}

// --- Open positions (Elder trade management) -----------------------------

const VERDICT_LABEL = { hold: "hold", take_profits: "take profits", exit: "exit" };

function positionsByAsset(snapshot) {
  const map = new Map();
  for (const p of snapshot.positions || []) map.set(p.asset, p);
  return map;
}

// Hyperliquid's cumFunding.sinceOpen: funding PAID since the position opened
// (negative = received). A cost the price PnL doesn't show. Unknown for manual
// positions.
function fundingText(p) {
  if (p.cum_funding === null || p.cum_funding === undefined) return "—";
  const cls = p.cum_funding > 0 ? "rr-bad" : "rr-good";
  return `<span class="${cls}">${fmt(p.cum_funding, 6)}</span>`;
}

function verdictBadge(verdict) {
  return `<span class="badge verdict-${verdict}">${VERDICT_LABEL[verdict] || verdict}</span>`;
}

function renderPositions(snapshot) {
  const section = document.getElementById("positions");
  const positions = snapshot.positions || [];
  if (!snapshot.position_address && positions.length === 0) return; // disabled
  section.classList.remove("hidden");

  const sub = document.getElementById("positions-sub");
  const managed = snapshot.positions_horizon ? ` · managed on '${snapshot.positions_horizon}'` : "";
  sub.textContent = snapshot.position_address
    ? `${positions.length} open · ${snapshot.position_address}${managed}`
    : managed.replace(" · ", "");

  const tbody = document.querySelector("#positions-table tbody");
  tbody.innerHTML = "";
  if (positions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="13" class="reason">No open positions (or held coins are too new to evaluate).</td></tr>`;
    return;
  }
  // Most urgent first: exit, then take profits, then hold.
  const order = { exit: 0, take_profits: 1, hold: 2 };
  for (const p of [...positions].sort((a, b) => order[a.verdict] - order[b.verdict])) {
    const elderCls = p.pnl_elder >= 0 ? "rr-good" : "rr-bad";
    const liveCls = p.pnl_live >= 0 ? "rr-good" : "rr-bad";
    const target = `${fmt(p.target)}${p.target_reached ? ' <span class="hit">✓</span>' : ""}`;
    const row = document.createElement("tr");
    row.dataset.verdict = p.verdict;
    row.innerHTML = `
      <td><strong>${p.asset}</strong></td>
      <td><span class="badge ${p.side === "long" ? "long" : "short"}">${p.side}</span></td>
      <td>${fmt(p.entry)}</td>
      <td>${fmt(p.close_price)}</td>
      <td>${fmt(p.live_price)}</td>
      <td class="${elderCls}">${fmt(p.pnl_elder, 6)}</td>
      <td class="${liveCls}">${fmt(p.pnl_live, 6)}</td>
      <td>${fundingText(p)}</td>
      <td>${impulseDot(p.tide_impulse)} / ${impulseDot(p.wave_impulse)}</td>
      <td>${target}</td>
      <td>${fmt(p.suggested_stop)}</td>
      <td>${verdictBadge(p.verdict)}</td>
      <td class="reason">${p.reasons.join(" · ")}<br><strong>Open risk:</strong> $${fmt(p.open_risk, 8)}</td>`;
    tbody.appendChild(row);
  }
}

function pnlText(pnl, retPct) {
  const ret = `${(retPct * 100 >= 0 ? "+" : "") + (retPct * 100).toFixed(1)}%`;
  return `${fmt(pnl, 6)} (${ret})`;
}

function positionPanelHTML(p) {
  return `
    <div class="position-panel verdict-${p.verdict}">
      <div class="position-head">
        <span class="badge ${p.side === "long" ? "long" : "short"}">${p.side}</span>
        Open position — Elder management: ${verdictBadge(p.verdict)}
      </div>
      <div class="position-grid">
        <span>Entry <b>${fmt(p.entry)}</b></span>
        <span>Close <b>${fmt(p.close_price)}</b></span>
        <span>Mark (live) <b>${fmt(p.live_price)}</b></span>
        <span>PnL Elder <b>${pnlText(p.pnl_elder, p.return_pct_elder)}</b></span>
        <span>PnL live <b>${pnlText(p.pnl_live, p.return_pct_live)}</b></span>
        <span title="cumFunding since open — positive = paid, negative = received">Funding paid <b>${fundingText(p)}</b></span>
        <span>Target <b>${fmt(p.target)}${p.target_reached ? " ✓" : ""}</b></span>
        <span>Trail stop <b>${fmt(p.suggested_stop)}</b></span>
      </div>
      <ul class="position-reasons">${p.reasons.map((r) => `<li>${r}</li>`).join("")}</ul>
    </div>`;
}

// --- Charts ---------------------------------------------------------------

function renderChart(container, data) {
  if (!LWC) {
    container.innerHTML =
      '<div class="chart-missing">Charts unavailable — the TradingView Lightweight ' +
      "Charts library could not be loaded. The tables above are unaffected.</div>";
    return;
  }
  const { createChart, CandlestickSeries, LineSeries, HistogramSeries } = LWC;
  const chart = createChart(container, CHART_OPTS);

  // Each bar carries its own Impulse body/wick color; borders are off because an
  // Impulse-colored border on an Impulse-colored body draws nothing visible.
  const candles = chart.addSeries(CandlestickSeries, { borderVisible: false }, 0);
  candles.setData(data.candles);

  const ema13 = chart.addSeries(
    LineSeries,
    { color: "#ffa726", lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
    0
  );
  ema13.setData(data.ema13);
  const ema26 = chart.addSeries(
    LineSeries,
    { color: "#42a5f5", lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
    0
  );
  ema26.setData(data.ema26);

  const hist = chart.addSeries(
    HistogramSeries,
    { priceLineVisible: false, lastValueVisible: false },
    1
  );
  hist.setData(data.macd_hist);

  const fi2 = chart.addSeries(
    LineSeries,
    { color: "#ab47bc", lineWidth: 2, priceLineVisible: false, lastValueVisible: false },
    2
  );
  fi2.setData(data.force_index_2);
  const fi13 = chart.addSeries(
    LineSeries,
    { color: "#8b949e", lineWidth: 1, priceLineVisible: false, lastValueVisible: false },
    2
  );
  fi13.setData(data.force_index_13);
  fi2.createPriceLine({ price: 0, color: "#8b949e", lineWidth: 1, lineStyle: 2, title: "0" });

  const panes = chart.panes();
  if (panes[1]) panes[1].setHeight(100);
  if (panes[2]) panes[2].setHeight(100);
  chart.timeScale().fitContent();
}

const LEGEND_HTML = `
  <div class="chart-legend">
    <span class="key"><i class="swatch" style="background:#ffa726"></i>EMA 13</span>
    <span class="key"><i class="swatch" style="background:#42a5f5"></i>EMA 26</span>
    <span class="key">Candles = Impulse:
      <span class="dot green">●</span> bullish
      <span class="dot red">●</span> bearish
      <span class="dot blue">●</span> neutral</span>
    <span class="key">Middle pane — MACD-Histogram(12,26,9) bars:
      <span class="dot green">▮</span> rising slope
      <span class="dot red">▮</span> falling slope</span>
    <span class="key">Bottom pane — Force Index:
      <i class="swatch" style="background:#ab47bc"></i>EMA-2
      <i class="swatch" style="background:#8b949e"></i>EMA-13 · dashed line = 0</span>
  </div>`;

function renderCards(block, snapshot) {
  const cards = document.getElementById("cards");
  cards.innerHTML = "";
  const positions = positionsByAsset(snapshot);
  const iv = block.intervals || {};
  for (const s of rankedSignals(block)) {
    const assetCharts = block.charts[s.asset];
    if (!assetCharts) continue;
    const card = document.createElement("section");
    card.className = "card";
    if (s.is_top_pick) card.classList.add("top-pick");
    const pos = positions.get(s.asset);
    if (pos) card.classList.add("has-position");
    card.dataset.action = s.action;
    card.dataset.asset = s.asset;
    card.dataset.haspos = pos ? "1" : "";
    const pickTag = s.is_top_pick ? ' <span class="badge top-pick-badge">★ best trade</span>' : "";
    const posTag = pos ? ' <span class="badge held">● held</span>' : "";
    const panes = ["tide", "wave", "entry"]
      .filter((role) => assetCharts[role])
      .map(
        (role) =>
          `<div><div class="chart-title">${SCREEN_LABEL[role]} — ${iv[role] || ""}</div>` +
          `<div class="chart chart-${role}"></div></div>`
      )
      .join("");
    const warns = (s.data_warnings || []).length
      ? `<div class="card-warn">! ${s.data_warnings.join("<br>! ")}</div>`
      : "";
    card.innerHTML = `
      <h2>${s.asset} <span class="badge ${s.action}">${s.action.replace("_", " ")}</span>${pickTag}${posTag}</h2>
      ${warns}
      ${pos ? positionPanelHTML(pos) : ""}
      ${LEGEND_HTML}
      <div class="charts">${panes}</div>`;
    cards.appendChild(card);
  }
}

// Charts are built lazily, the first time a card is actually shown.
function ensureChartsRendered(card, block) {
  if (card.dataset.rendered) return;
  const assetCharts = block.charts[card.dataset.asset];
  if (!assetCharts) return;
  for (const role of ["tide", "wave", "entry"]) {
    const el = card.querySelector(`.chart-${role}`);
    if (el && assetCharts[role]) renderChart(el, assetCharts[role]);
  }
  card.dataset.rendered = "1";
}

function applyStandAsideFilter() {
  const block = currentBlock();
  if (!block) return;
  const hide = document.getElementById("hide-stand-aside").checked;
  let hidden = 0;
  for (const row of document.querySelectorAll("#signals-table tbody tr")) {
    const out = hide && row.dataset.action === "stand_aside";
    row.classList.toggle("hidden", out);
    if (out) hidden += 1;
  }
  for (const card of document.querySelectorAll("#cards .card")) {
    // Always keep cards for held positions visible, even when standing aside.
    const out = hide && card.dataset.action === "stand_aside" && !card.dataset.haspos;
    card.classList.toggle("hidden", out);
    if (!out) ensureChartsRendered(card, block);
  }
  document.getElementById("filter-count").textContent = hide
    ? `${hidden} stand-aside asset${hidden === 1 ? "" : "s"} hidden`
    : "";
}

// --- Top-level render -----------------------------------------------------

function renderAll() {
  const snapshot = state.snapshot;
  const block = currentBlock();

  document.getElementById("meta").textContent =
    `equity $${fmt(snapshot.equity, 8)} · risk/trade ${(snapshot.risk_pct * 100).toFixed(1)}% ` +
    `of month-start equity $${fmt(snapshot.equity_at_month_start ?? snapshot.equity, 8)} · ` +
    `open risk $${fmt(snapshot.total_open_trade_risk ?? snapshot.guard.total_at_risk, 8)} · ` +
    `updated ${ago(snapshot.generated_at)}`;

  const banner = document.getElementById("guard-banner");
  banner.classList.toggle("hidden", !snapshot.guard.blocked);
  if (snapshot.guard.blocked) {
    banner.textContent =
      `⚠ 6% RULE ACTIVE — monthly losses + open risk $${fmt(snapshot.guard.total_at_risk, 8)} ` +
      `≥ limit $${fmt(snapshot.guard.limit, 8)}. No new entries for the rest of the month.`;
  }

  // Each horizon sizes independently, so the same asset can show a long here and
  // a short one tab over. Say so once, loudly.
  const stacking = document.getElementById("stacking-note");
  stacking.textContent =
    `Every horizon sizes its suggestion as a standalone trade risking ` +
    `${(snapshot.risk_pct * 100).toFixed(1)}% of month-start equity. Taking setups from several ` +
    `horizons at once multiplies your risk — the 6% rule caps total open risk, not ` +
    `the number of simultaneous suggestions.`;
  stacking.classList.remove("hidden");

  renderPositions(snapshot);
  renderTabs();

  const pickBanner = document.getElementById("best-pick-banner");
  const best = block && block.top_pick
    ? block.signals.find((s) => s.asset === block.top_pick)
    : null;
  pickBanner.classList.toggle("hidden", !best);
  if (best) {
    pickBanner.innerHTML =
      `★ Best ${block.label || state.horizon} trade — <strong>${best.asset}</strong> ` +
      `<span class="badge ${best.action}">${best.action.replace("_", " ")}</span> · ` +
      `score ${Math.round(best.quality_score * 100)}/100 · ` +
      `R:R ${best.reward_risk.toFixed(2)} · entry ${fmt(best.entry)} · stop ${fmt(best.stop)} · ` +
      `target ${fmt(best.target)}`;
  }

  if (!block) return;
  document.getElementById("horizon-meta").textContent =
    `${chainOf(block)} · refreshed ${ago(block.generated_at)}` +
    (block.skipped && block.skipped.length ? ` · skipped: ${block.skipped.join(", ")}` : "");

  renderTable(block);
  renderCards(block, snapshot);
  applyStandAsideFilter();
}

async function poll() {
  let snapshot;
  try {
    const resp = await fetch("/api/snapshot");
    if (!resp.ok) return;
    snapshot = await resp.json();
  } catch {
    return; // server restarting mid-refresh: try again next tick
  }
  const stamps = stampsOf(snapshot);
  if (stamps === state.stamps) return; // nothing changed
  state.snapshot = snapshot;
  state.stamps = stamps;
  const names = horizonNames(snapshot);
  if (!names.includes(state.horizon)) state.horizon = names[0] ?? null;
  renderAll();
}

async function main() {
  const meta = document.getElementById("meta");
  const resp = await fetch("/api/snapshot");
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    meta.textContent = err.error || "No snapshot available — run `python run.py` first.";
    return;
  }
  state.snapshot = await resp.json();
  state.stamps = stampsOf(state.snapshot);

  const names = horizonNames(state.snapshot);
  let remembered = null;
  try {
    remembered = localStorage.getItem(STORAGE_KEY);
  } catch {
    /* private mode */
  }
  state.horizon = names.includes(remembered) ? remembered : names[0] ?? null;

  renderAll();
  document.getElementById("hide-stand-aside").addEventListener("change", applyStandAsideFilter);
  setInterval(poll, POLL_MS);
}

main();
