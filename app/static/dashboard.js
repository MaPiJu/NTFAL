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

function renderTable(snapshot) {
  const tbody = document.querySelector("#signals-table tbody");
  tbody.innerHTML = "";
  for (const s of snapshot.signals) {
    const rr = s.reward_risk;
    const rrCell =
      rr === null
        ? "—"
        : `<span class="${s.rr_ok ? "rr-good" : "rr-bad"}">${rr.toFixed(2)}${s.rr_ok ? "" : " ⚠"}</span>`;
    const size = s.position_size ? fmt(s.position_size.size, 6) : "—";
    const row = document.createElement("tr");
    row.dataset.action = s.action;
    row.innerHTML = `
      <td><strong>${s.asset}</strong></td>
      <td>${s.weekly_trend}</td>
      <td>${impulseDot(s.weekly_impulse)} / ${impulseDot(s.daily_impulse)}</td>
      <td>${fmt(s.force_index_2, 4)}</td>
      <td><span class="badge ${s.action}">${s.action.replace("_", " ")}</span></td>
      <td>${fmt(s.entry)}</td>
      <td>${fmt(s.entry_limit)}</td>
      <td>${fmt(s.stop)}</td>
      <td>${fmt(s.target)}</td>
      <td>${rrCell}</td>
      <td>${size}</td>
      <td class="reason">${s.reason}</td>`;
    tbody.appendChild(row);
  }
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
  for (const s of snapshot.signals) {
    const card = document.createElement("section");
    card.className = "card";
    card.dataset.action = s.action;
    card.dataset.asset = s.asset;
    card.innerHTML = `
      <h2>${s.asset} <span class="badge ${s.action}">${s.action.replace("_", " ")}</span></h2>
      ${LEGEND_HTML}
      <div class="charts">
        <div><div class="chart-title">Weekly (tide)</div><div class="chart chart-w"></div></div>
        <div><div class="chart-title">Daily (wave)</div><div class="chart chart-d"></div></div>
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
    const out = hide && card.dataset.action === "stand_aside";
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
    `risk/trade ${(snapshot.risk_pct * 100).toFixed(1)}%`;

  const banner = document.getElementById("guard-banner");
  if (snapshot.guard.blocked) {
    banner.textContent =
      `⚠ 6% RULE ACTIVE — monthly losses + open risk $${fmt(snapshot.guard.total_at_risk, 8)} ` +
      `≥ limit $${fmt(snapshot.guard.limit, 8)}. No new entries for the rest of the month.`;
    banner.classList.remove("hidden");
  }

  renderTable(snapshot);
  renderCards(snapshot);
  applyStandAsideFilter(snapshot);
  document
    .getElementById("hide-stand-aside")
    .addEventListener("change", () => applyStandAsideFilter(snapshot));
}

main();
