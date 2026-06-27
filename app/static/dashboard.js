/* Elder Triple Screen dashboard — renders the snapshot with
 * TradingView Lightweight Charts v5 (attribution logo enabled per license). */

const { createChart, CandlestickSeries, LineSeries, HistogramSeries } = LightweightCharts;

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
  timeScale: { borderColor: "#2d333b" },
  rightPriceScale: { borderColor: "#2d333b" },
};

function fmt(x, digits = 5) {
  if (x === null || x === undefined) return "—";
  return Number(x).toLocaleString("en-US", { maximumSignificantDigits: digits });
}

function impulseDot(color) {
  return `<span class="dot ${color}" title="${color}">●</span>`;
}

// Best trade first: tradable setups ranked by Elder quality score (desc),
// then the stand-aside rest by name. Returns a sorted copy.
function rankedSignals(snapshot) {
  return [...snapshot.signals].sort((a, b) => {
    const aside = (s) => (s.action === "stand_aside" ? 1 : 0);
    if (aside(a) !== aside(b)) return aside(a) - aside(b);
    if (aside(a) === 1) return a.asset.localeCompare(b.asset);
    return (b.quality_score ?? 0) - (a.quality_score ?? 0);
  });
}

function scoreCell(s) {
  if (s.quality_score === null || s.quality_score === undefined) return "—";
  const pct = Math.round(s.quality_score * 100);
  const star = s.is_top_pick ? ' <span class="top-star" title="best trade">★</span>' : "";
  return `<span class="score">${pct}</span>${star}`;
}

function renderTable(snapshot) {
  const tbody = document.querySelector("#signals-table tbody");
  tbody.innerHTML = "";
  for (const s of rankedSignals(snapshot)) {
    const rr = s.reward_risk;
    const rrCell =
      rr === null
        ? "—"
        : `<span class="${s.rr_ok ? "rr-good" : "rr-bad"}">${rr.toFixed(2)}${s.rr_ok ? "" : " ⚠"}</span>`;
    const limitRr = s.reward_risk_limit;
    const limitRrCell = limitRr === null || limitRr === undefined ? "—" : limitRr.toFixed(2);
    const size = s.position_size ? fmt(s.position_size.size, 6) : "—";
    const row = document.createElement("tr");
    row.dataset.action = s.action;
    if (s.is_top_pick) row.classList.add("top-pick");
    row.innerHTML = `
      <td><strong>${s.asset}</strong></td>
      <td>${s.market_regime ?? "—"}</td>
      <td>${s.weekly_trend}</td>
      <td>${impulseDot(s.weekly_impulse)} / ${impulseDot(s.daily_impulse)}${s.third_screen_impulse ? ` / ${impulseDot(s.third_screen_impulse)}` : ""}</td>
      <td>${fmt(s.force_index_2, 4)}</td>
      <td><span class="badge ${s.action}">${s.action.replace("_", " ")}</span></td>
      <td>${fmt(s.last_close)}</td>
      <td>${fmt(s.entry)}</td>
      <td>${fmt(s.stop)}</td>
      <td>${rrCell}</td>
      <td>${fmt(s.entry_limit)}</td>
      <td>${fmt(s.entry_limit_stop)}</td>
      <td>${limitRrCell}</td>
      <td>${fmt(s.target)}</td>
      <td>${scoreCell(s)}</td>
      <td>${size}</td>
      <td class="reason">${s.reason}<br><strong>Value zone:</strong> ${(s.value_zone_status || "—").replace("_", " ")}${s.entry_order_plan ? `<br><strong>Order plan:</strong> ${s.entry_order_plan}` : ""}${(s.divergences || []).length ? `<br><strong>Divergences:</strong> ${s.divergences.join(", ")}` : ""}</td>`;
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

function verdictBadge(verdict) {
  return `<span class="badge verdict-${verdict}">${VERDICT_LABEL[verdict] || verdict}</span>`;
}

function renderPositions(snapshot) {
  const section = document.getElementById("positions");
  const positions = snapshot.positions || [];
  if (!snapshot.position_address && positions.length === 0) return; // disabled
  section.classList.remove("hidden");

  const sub = document.getElementById("positions-sub");
  sub.textContent = snapshot.position_address
    ? `${positions.length} open · ${snapshot.position_address}`
    : "";

  const tbody = document.querySelector("#positions-table tbody");
  tbody.innerHTML = "";
  if (positions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="12" class="reason">No open positions (or held coins are too new to evaluate).</td></tr>`;
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
      <td>${impulseDot(p.weekly_impulse)} / ${impulseDot(p.daily_impulse)}</td>
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
        <span>Daily close <b>${fmt(p.close_price)}</b></span>
        <span>Mark (live) <b>${fmt(p.live_price)}</b></span>
        <span>PnL Elder <b>${pnlText(p.pnl_elder, p.return_pct_elder)}</b></span>
        <span>PnL live <b>${pnlText(p.pnl_live, p.return_pct_live)}</b></span>
        <span>Target <b>${fmt(p.target)}${p.target_reached ? " ✓" : ""}</b></span>
        <span>Trail stop <b>${fmt(p.suggested_stop)}</b></span>
      </div>
      <ul class="position-reasons">${p.reasons.map((r) => `<li>${r}</li>`).join("")}</ul>
    </div>`;
}

function renderChart(container, data) {
  const chart = createChart(container, CHART_OPTS);

  const candles = chart.addSeries(CandlestickSeries, {}, 0);
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

function renderCards(snapshot) {
  const cards = document.getElementById("cards");
  cards.innerHTML = "";
  const positions = positionsByAsset(snapshot);
  for (const s of rankedSignals(snapshot)) {
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
    card.innerHTML = `
      <h2>${s.asset} <span class="badge ${s.action}">${s.action.replace("_", " ")}</span>${pickTag}${posTag}</h2>
      ${pos ? positionPanelHTML(pos) : ""}
      ${LEGEND_HTML}
      <div class="charts">
        <div><div class="chart-title">Weekly (tide)</div><div class="chart chart-w"></div></div>
        <div><div class="chart-title">Daily (wave)</div><div class="chart chart-d"></div></div>
        ${snapshot.charts[s.asset].third_screen ? '<div><div class="chart-title">4h (optional third screen)</div><div class="chart chart-4h"></div></div>' : ''}
      </div>`;
    cards.appendChild(card);
  }
}

// Charts are built lazily, the first time a card is actually shown — with the
// stand-aside filter on by default this skips most of the universe.
function ensureChartsRendered(card, snapshot) {
  if (card.dataset.rendered) return;
  const assetCharts = snapshot.charts[card.dataset.asset];
  renderChart(card.querySelector(".chart-w"), assetCharts.weekly);
  renderChart(card.querySelector(".chart-d"), assetCharts.daily);
  if (assetCharts.third_screen) renderChart(card.querySelector(".chart-4h"), assetCharts.third_screen);
  card.dataset.rendered = "1";
}

function applyStandAsideFilter(snapshot) {
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
    if (!out) ensureChartsRendered(card, snapshot);
  }
  document.getElementById("filter-count").textContent = hide
    ? `${hidden} stand-aside asset${hidden === 1 ? "" : "s"} hidden`
    : "";
}

async function main() {
  const resp = await fetch("/api/snapshot");
  const meta = document.getElementById("meta");
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    meta.textContent = err.error || "No snapshot available — run `python run.py` first.";
    return;
  }
  const snapshot = await resp.json();
  meta.textContent =
    `Generated ${snapshot.generated_at} · equity $${fmt(snapshot.equity, 8)} · ` +
    `risk/trade ${(snapshot.risk_pct * 100).toFixed(1)}% · ` +
    `open risk $${fmt(snapshot.total_open_trade_risk ?? snapshot.guard.total_at_risk, 8)}`;

  const banner = document.getElementById("guard-banner");
  if (snapshot.guard.blocked) {
    banner.textContent =
      `⚠ 6% RULE ACTIVE — monthly losses + open risk $${fmt(snapshot.guard.total_at_risk, 8)} ` +
      `≥ limit $${fmt(snapshot.guard.limit, 8)}. No new entries for the rest of the month.`;
    banner.classList.remove("hidden");
  }

  const pickBanner = document.getElementById("best-pick-banner");
  const best = snapshot.top_pick
    ? snapshot.signals.find((s) => s.asset === snapshot.top_pick)
    : null;
  if (best) {
    pickBanner.innerHTML =
      `★ Best trade — <strong>${best.asset}</strong> ` +
      `<span class="badge ${best.action}">${best.action.replace("_", " ")}</span> · ` +
      `score ${Math.round(best.quality_score * 100)}/100 · ` +
      `R:R ${best.reward_risk.toFixed(2)} · entry ${fmt(best.entry)} · stop ${fmt(best.stop)} · ` +
      `target ${fmt(best.target)}`;
    pickBanner.classList.remove("hidden");
  }

  renderPositions(snapshot);
  renderTable(snapshot);
  renderCards(snapshot);
  applyStandAsideFilter(snapshot);
  document
    .getElementById("hide-stand-aside")
    .addEventListener("change", () => applyStandAsideFilter(snapshot));
}

main();
