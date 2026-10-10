// CoinDCX Spot Lab — main dashboard orchestrator.
// Routes between views, fetches data, renders tables & charts.

document.addEventListener("DOMContentLoaded", async () => {
  // Wire nav clicks
  for (const a of document.querySelectorAll("nav#nav a")) {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const route = a.dataset.route;
      const path = route === "overview" ? "/" : "/" + route;
      history.pushState({ route }, "", path);
      show(route);
      onRouteChange(route);
    });
  });
  // Back button
  window.addEventListener("popstate", () => {
    const route = (history.state && history.state.route)
      || (window.location.pathname.replace("/", "") || "overview");
    show(route);
    onRouteChange(route);
  });

  // Initial route
  const initial = window.location.pathname.replace("/", "") || "overview";
  show(initial);
  await loadOverview();
  onRouteChange(initial);
});

async function onRouteChange(route) {
  switch (route) {
    case "scan":       await loadScan(); break;
    case "chart":      await loadChart(); break;
    case "signals":    await loadSignals(); break;
    case "calc":       /* nothing to preload */ break;
    case "backtest":   await loadBacktestPairs(); break;
    case "portfolio":  await loadPortfolio(); break;
    case "trades":     await loadTrades(); break;
    case "overview":
    default:           await loadOverview(); break;
  }
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------
async function loadOverview() {
  const banner = el("provenance-banner");
  const cards = el("overview-cards");
  setLoading(cards, "Fetching markets and prices…");

  try {
    const [health, markets, prices] = await Promise.all([
      API.health().catch(() => null),
      API.markets(),
      API.prices(),
    ]);

    // Server health footer
    if (health) {
      el("server-health").textContent =
        `Service: ${health.status} · v${health.version} · ` +
        `Python ${health.python_version} · ${new Date().toLocaleString("en-IN")}`;
    }

    // Provenance banner: prefer prices provenance
    banner.outerHTML = `<div id="provenance-banner">${provenanceBadge(prices.provenance)}</div>`;

    // Cards
    cards.innerHTML = `
      <div class="card"><div class="label">Pairs discovered</div>
        <div class="value">${markets.count}</div></div>
      <div class="card"><div class="label">Pairs with prices</div>
        <div class="value">${prices.count}</div></div>
      <div class="card"><div class="label">Data source</div>
        <div class="value">${(prices.provenance.source || "—").toUpperCase()}</div></div>
      <div class="card"><div class="label">Fetched at (UTC)</div>
        <div class="value" style="font-size:14px">
          ${(prices.provenance.fetched_at_utc || "—").slice(11, 19)}</div></div>
    `;

    if (prices.live_unavailable_reason) {
      cards.innerHTML += `<div class="card" style="grid-column: 1/-1">
        <div class="label">Live data unavailable reason</div>
        <div class="value warn" style="font-size:13px">
          ${prices.live_unavailable_reason.slice(0, 200)}</div></div>`;
    }

    // Populate pair dropdowns
    populatePairDropdowns(markets.pairs);

  } catch (err) {
    setError(cards, err);
  }
}

function populatePairDropdowns(pairs) {
  const opts = pairs.map(p => `<option value="${p.pair}">${p.display_symbol || p.pair}</option>`).join("");
  for (const id of ["chart-pair", "signals-pair", "bt-pair"]) {
    const sel = el(id);
    if (sel) sel.innerHTML = opts;
  }
}

// ---------------------------------------------------------------------------
// Scanner
// ---------------------------------------------------------------------------
async function loadScan() {
  // Default empty state; user must click "Run scan"
  el("scan-results").innerHTML = `<p class="loading">Click "Run scan" to evaluate all pairs.</p>`;
}

el("scan-run")?.addEventListener("click", async () => {
  const interval = el("scan-interval").value;
  const target = el("scan-results");
  setLoading(target, "Scanning all INR spot pairs…");
  try {
    const r = await API.scanner(interval);
    target.innerHTML = "";
    // Provenance badge
    target.innerHTML += `<div style="margin-bottom:12px">
      ${r.results.length} markets scanned · ranked by explainable score</div>`;
    // Table
    const rows = r.results.map(row => {
      const cls = row.excluded ? "excluded" : "";
      const stateBadge = stateToBadge(row.signal_state);
      return `<tr class="${cls}">
        <td>${row.rank ?? "—"}</td>
        <td>${row.pair}</td>
        <td>${fmt(row.last_price, 4)}</td>
        <td>${pct(row.change_pct_24h)}</td>
        <td>${fmt(row.volume_24h_inr, 0)}</td>
        <td>${row.trend}</td>
        <td>${row.volatility !== null ? pct(row.volatility) : "—"}</td>
        <td>${stateBadge}</td>
        <td>${row.rr_after_cost !== null ? Number(row.rr_after_cost).toFixed(2) : "—"}</td>
        <td>${row.score !== null ? Number(row.score).toFixed(1) : "—"}</td>
        <td>${row.excluded ? `<span class="badge badge-err">${row.exclusion_reason || "excluded"}</span>` : ""}</td>
      </tr>`;
    }).join("");
    target.innerHTML += `<table>
      <thead><tr>
        <th>Rank</th><th>Pair</th><th>Last</th><th>24h%</th><th>Vol24 (INR)</th>
        <th>Trend</th><th>Volat</th><th>Signal</th><th>R/R</th><th>Score</th>
        <th>Note</th>
      </tr></thead>
      <tbody>${rows || '<tr><td colspan="11" class="loading">No results</td></tr>'}</tbody>
    </table>`;
  } catch (err) {
    setError(target, err);
  }
});

function stateToBadge(state) {
  const m = {
    "BUY_CANDIDATE": "badge-ok",
    "WAIT": "badge-warn",
    "EXIT": "badge-info",
    "NO_TRADE": "badge-err",
  };
  return `<span class="badge ${m[state] || "badge-info"}">${state}</span>`;
}

// ---------------------------------------------------------------------------
// Chart
// ---------------------------------------------------------------------------
let _chartPair = null, _chartInterval = "1h";

async function loadChart() {
  // Defer until pairs are loaded
  if (!el("chart-pair").options.length) {
    setTimeout(loadChart, 200);
    return;
  }
  await renderChart();
}

async function renderChart() {
  const pair = el("chart-pair").value;
  const interval = el("chart-interval").value;
  _chartPair = pair; _chartInterval = interval;
  const target = el("chart-candles");
  setLoading(target, `Fetching candles for ${pair} ${interval}…`);
  try {
    const r = await API.candles(pair, interval, 200);
    const candles = r.candles || [];
    if (!candles.length) {
      target.innerHTML = `<p class="loading">No candles</p>`;
      return;
    }
    const times = candles.map(c => new Date(c.open_time_ms).toISOString());
    const opens = candles.map(c => c.open);
    const highs = candles.map(c => c.high);
    const lows = candles.map(c => c.low);
    const closes = candles.map(c => c.close);
    const vols = candles.map(c => c.volume);

    const fig = Plotly.makeGraphDiv ? Plotly.makeGraphDiv : Plotly.newPlot;
    Plotly.newPlot("chart-candles", [
      { type: "candlestick", x: times, open: opens, high: highs,
        low: lows, close: closes, name: "OHLC",
        increasing: { line: { color: "#3fb950" } },
        decreasing: { line: { color: "#f85149" } } },
    ], {
      title: `${pair} · ${interval} · ${r.provenance.label}`,
      dragmode: "zoom",
      xaxis: { rangeslider: { visible: false } },
      yaxis: { title: "Price (INR)" },
      plot_bgcolor: "#161b22", paper_bgcolor: "#161b22",
      font: { color: "#e6edf3" },
      margin: { l: 50, r: 10, t: 40, b: 20 },
      height: 400,
    }, { responsive: true });

    // Volume sub-chart
    Plotly.plot("chart-candles", [
      { x: times, y: vols, type: "bar", name: "Volume",
        marker: { color: "#8b949e" },
        xaxis: "x", yaxis: "y2" },
    ], {}, { responsive: true });
  } catch (err) {
    setError(target, err);
  }
}

["chart-pair", "chart-interval"].forEach(id =>
  el(id)?.addEventListener("change", renderChart));

// ---------------------------------------------------------------------------
// Signals
// ---------------------------------------------------------------------------
async function loadSignals() {
  if (!el("signals-pair").options.length) {
    setTimeout(loadSignals, 200);
    return;
  }
}

el("signals-run")?.addEventListener("click", async () => {
  const pair = el("signals-pair").value;
  const interval = el("signals-interval").value;
  const target = el("signals-list");
  setLoading(target, `Evaluating signals for ${pair} ${interval}…`);
  try {
    const r = await API.signals(pair, interval);
    target.innerHTML = "";
    if (!r.signals.length) {
      target.innerHTML = `<p class="loading">No signals produced.</p>`;
      return;
    }
    for (const s of r.signals) {
      const supports = s.supporting.map(t => `<li>+ ${t}</li>`).join("");
      const conflicts = s.conflicting.map(t => `<li>− ${t}</li>`).join("");
      const warnings = (s.data_quality_warnings || []).map(t => `<li>⚠ ${t}</li>`).join("");
      const stateBadge = stateToBadge(s.state);
      target.innerHTML += `<div class="signal">
        <h3>${s.strategy} ${stateBadge}
          <span class="badge">${s.interval}</span>
          ${s.rr_after_cost !== null ? `<span class="badge badge-info">R/R ${Number(s.rr_after_cost).toFixed(2)}</span>` : ""}
        </h3>
        <div class="explanation">${s.explanation}</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px">
          <div><strong>Supporting</strong><ul>${supports || '<li class="loading">none</li>'}</ul></div>
          <div><strong>Conflicting</strong><ul>${conflicts || '<li class="loading">none</li>'}</ul></div>
        </div>
        ${warnings ? `<div><strong>Warnings</strong><ul>${warnings}</ul></div>` : ""}
        ${s.entry !== null ? `<div style="margin-top:8px">
          <strong>Entry:</strong> ${fmt(s.entry, 4)} |
          <strong>Stop:</strong> ${fmt(s.stop, 4)} |
          <strong>Target:</strong> ${fmt(s.target, 4)} |
          <strong>Est. entry cost:</strong> ${fmt(s.est_entry_cost_inr)} |
          <strong>Est. exit cost:</strong> ${fmt(s.est_exit_cost_inr)}
        </div>` : ""}
      </div>`;
    }
    // Provenance update
    const banner = el("provenance-banner");
    if (banner) banner.outerHTML = `<div id="provenance-banner">${provenanceBadge(r.provenance)}</div>`;
  } catch (err) {
    setError(target, err);
  }
});

// ---------------------------------------------------------------------------
// Calculator
// ---------------------------------------------------------------------------
el("calc-run")?.addEventListener("click", async () => {
  const entry = parseFloat(el("calc-entry").value);
  const stop = parseFloat(el("calc-stop").value);
  const target = parseFloat(el("calc-target").value);
  const targetEl = el("calc-result");
  if (!entry || !stop || !target) {
    targetEl.innerHTML = `<p class="error-msg">Fill in entry, stop, target.</p>`;
    return;
  }
  setLoading(targetEl, "Calculating…");
  try {
    const r = await API.risk(entry, stop, target, "CALC");
    const ok = r.decision === "accepted";
    targetEl.innerHTML = `
      <div class="card" style="grid-column: 1/-1">
        <div class="label">Decision</div>
        <div class="value ${ok ? "ok" : "err"}">${r.decision}</div>
        <div class="loading">${r.reason || ""}</div>
      </div>
      <div class="cards">
        <div class="card"><div class="label">Position value</div><div class="value">₹${fmt(r.position_value)}</div></div>
        <div class="card"><div class="label">Quantity</div><div class="value">${fmt(r.qty, 6)}</div></div>
        <div class="card"><div class="label">Risk budget</div><div class="value">₹${fmt(r.risk_budget)}</div></div>
        <div class="card"><div class="label">Cap reason</div><div class="value" style="font-size:14px">${r.cap_reason}</div></div>
      </div>
      <div class="cards">
        <div class="card"><div class="label">Potential loss (net)</div><div class="value err">₹${fmt(r.potential_loss_net)}</div></div>
        <div class="card"><div class="label">Potential gain (net)</div><div class="value ok">₹${fmt(r.potential_gain_net)}</div></div>
        <div class="card"><div class="label">R/R after cost</div><div class="value">${r.rr_after_cost ? Number(r.rr_after_cost).toFixed(2) : "—"}</div></div>
        <div class="card"><div class="label">Est. costs (entry+exit)</div><div class="value">₹${fmt(r.est_entry_cost + r.est_exit_cost)}</div></div>
      </div>`;
  } catch (err) {
    setError(targetEl, err);
  }
});

// ---------------------------------------------------------------------------
// Backtest
// ---------------------------------------------------------------------------
async function loadBacktestPairs() {
  if (!el("bt-pair").options.length) {
    setTimeout(loadBacktestPairs, 200);
  }
}

el("bt-run")?.addEventListener("click", async () => {
  const pair = el("bt-pair").value;
  const interval = el("bt-interval").value;
  const strategy = el("bt-strategy").value;
  const capital = parseFloat(el("bt-capital").value);
  const targetEl = el("bt-result");
  setLoading(targetEl, "Running event-driven backtest…");
  try {
    const r = await API.backtest(pair, interval, strategy, capital, false);
    targetEl.innerHTML = r.segments.map(seg => {
      const s = seg.summary;
      return `<div class="card" style="grid-column:1/-1;margin-bottom:12px">
        <h3 style="margin-top:0">${seg.name.toUpperCase()} segment</h3>
        <div class="cards">
          <div class="card"><div class="label">Total return</div><div class="value ${s.total_return_pct >= 0 ? "ok" : "err"}">${pct(s.total_return_pct)}<br><span style="font-size:12px;color:var(--muted)">₹${fmt(s.total_return_inr)}</span></div></div>
          <div class="card"><div class="label">Max drawdown</div><div class="value warn">${pct(s.max_drawdown_pct)}</div></div>
          <div class="card"><div class="label">Win rate</div><div class="value">${pct(s.win_rate_pct)}</div></div>
          <div class="card"><div class="label">Profit factor</div><div class="value">${isFinite(s.profit_factor) ? Number(s.profit_factor).toFixed(2) : "—"}</div></div>
          <div class="card"><div class="label">Expectancy</div><div class="value">₹${fmt(s.expectancy_inr)}</div></div>
          <div class="card"><div class="label">Trades</div><div class="value">${s.num_trades}</div></div>
          <div class="card"><div class="label">Avg win / loss</div><div class="value" style="font-size:14px"><span class="ok">₹${fmt(s.avg_win_inr)}</span> / <span class="err">₹${fmt(s.avg_loss_inr)}</span></div></div>
          <div class="card"><div class="label">Buy &amp; hold</div><div class="value">${pct(s.buy_hold_return_pct)}</div></div>
          <div class="card"><div class="label">Exposure</div><div class="value">${pct(s.exposure_pct)}</div></div>
          <div class="card"><div class="label">Est. fees</div><div class="value">₹${fmt(s.est_fees_inr)}</div></div>
          <div class="card"><div class="label">Est. slippage</div><div class="value">₹${fmt(s.est_slippage_inr)}</div></div>
        </div>
      </div>`;
    }).join("");
  } catch (err) {
    setError(targetEl, err);
  }
});

// ---------------------------------------------------------------------------
// Portfolio
// ---------------------------------------------------------------------------
async function loadPortfolio() {
  const target = el("portfolio-summary");
  setLoading(target, "Loading portfolio…");
  try {
    const s = await API.portfolio();
    target.innerHTML = `
      <div class="card"><div class="label">Initial equity</div><div class="value">₹${fmt(s.initial_equity)}</div></div>
      <div class="card"><div class="label">Current equity</div><div class="value ${s.current_equity >= s.initial_equity ? "ok" : "err"}">₹${fmt(s.current_equity)}</div></div>
      <div class="card"><div class="label">Available cash</div><div class="value">₹${fmt(s.available_cash)}</div></div>
      <div class="card"><div class="label">Open positions</div><div class="value">${s.open_positions_count} / ${s.max_open_positions}</div></div>
      <div class="card"><div class="label">Realized P&amp;L</div><div class="value ${s.realized_pnl >= 0 ? "ok" : "err"}">₹${fmt(s.realized_pnl)}</div></div>
      <div class="card"><div class="label">Unrealized P&amp;L</div><div class="value">₹${fmt(s.unrealized_pnl)}</div></div>
      <div class="card"><div class="label">Drawdown</div><div class="value warn">${pct(s.drawdown_pct)}</div></div>
      <div class="card"><div class="label">Risk budget / trade</div><div class="value">₹${fmt(s.risk_budget_per_trade)}</div></div>
      <div class="card"><div class="label">Daily realized P&amp;L</div><div class="value ${s.daily_realized_pnl >= 0 ? "ok" : "err"}">₹${fmt(s.daily_realized_pnl)}</div></div>
      <div class="card"><div class="label">Daily trades</div><div class="value">${s.daily_trades} / ${s.max_daily_trades}</div></div>
      <div class="card"><div class="label">Consecutive losses</div><div class="value ${s.consecutive_losses > 0 ? "warn" : ""}">${s.consecutive_losses}</div></div>
      <div class="card"><div class="label">Status</div><div class="value ${s.paused ? "err" : "ok"}">${s.paused ? "PAUSED" : "ACTIVE"}</div></div>
    `;
  } catch (err) {
    setError(target, err);
  }
}

// ---------------------------------------------------------------------------
// Trades
// ---------------------------------------------------------------------------
async function loadTrades() {
  const target = el("trades-list");
  setLoading(target, "Loading trades…");
  try {
    const r = await API.trades();
    if (!r.trades.length) {
      target.innerHTML = `<p class="loading">No paper trades yet.</p>`;
      return;
    }
    const rows = r.trades.map(t => {
      const time = new Date(t.entry_time_ms).toLocaleString("en-IN");
      return `<tr>
        <td>${t.id ?? "—"}</td>
        <td>${t.pair}</td>
        <td>${t.strategy}</td>
        <td>${t.status}</td>
        <td>${fmt(t.entry, 4)}</td>
        <td>${fmt(t.stop, 4)}</td>
        <td>${fmt(t.target, 4)}</td>
        <td>${t.exit_price ? fmt(t.exit_price, 4) : "—"}</td>
        <td>${t.pnl_net !== null ? `₹${fmt(t.pnl_net)}` : "—"}</td>
        <td>${t.exit_reason || "—"}</td>
        <td>${time}</td>
      </tr>`;
    }).join("");
    target.innerHTML = `<table>
      <thead><tr>
        <th>ID</th><th>Pair</th><th>Strategy</th><th>Status</th>
        <th>Entry</th><th>Stop</th><th>Target</th><th>Exit</th>
        <th>PnL</th><th>Reason</th><th>Entry time</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  } catch (err) {
    setError(target, err);
  }
}
