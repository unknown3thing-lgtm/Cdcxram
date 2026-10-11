// CoinDCX Spot Lab — API client for the Vercel Python functions.
// All requests go to relative /api/<name> paths so the same-origin
// deployment works without CORS configuration.

const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) {
      let detail = "";
      try { detail = (await r.json()).detail || ""; } catch (e) {}
      throw new Error(`${r.status} ${r.statusText} ${detail}`);
    }
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      let detail = "";
      try { detail = (await r.json()).detail || ""; } catch (e) {}
      throw new Error(`${r.status} ${r.statusText} ${detail}`);
    }
    return r.json();
  },

  // Endpoints
  health:        () => API.get("/api/health"),
  markets:       () => API.get("/api/markets"),
  prices:        () => API.get("/api/prices"),
  candles:       (pair, interval, limit) =>
    API.get(`/api/candles?pair=${encodeURIComponent(pair)}&interval=${encodeURIComponent(interval)}&limit=${limit || 200}`),
  signals:       (pair, interval) =>
    API.get(`/api/signals?pair=${encodeURIComponent(pair)}&interval=${encodeURIComponent(interval)}`),
  scanner:       (interval, pairs) =>
    API.post("/api/scanner", { interval, pairs }),
  risk:          (entry, stop, target, pair, overrides) =>
    API.post("/api/risk", Object.assign({ entry, stop, target, pair }, overrides || {})),
  backtest:      (pair, interval, strategy, startingCapital, walkForward) =>
    API.post("/api/backtest", {
      pair, interval, strategy,
      starting_capital_inr: startingCapital,
      walk_forward: walkForward !== false,
    }),
  portfolio:     () => API.get("/api/portfolio"),
  trades:        (status, limit) =>
    API.get(`/api/trades?${status ? "status=" + status + "&" : ""}limit=${limit || 200}`),
  openTrade:     (sig) => API.post("/api/trades", sig),
  settings:      () => API.get("/api/settings"),
  saveSettings:  (s) => API.post("/api/settings", s),
  resetPortfolio: () => API.post("/api/portfolio_reset", { confirm: true }),
};

// UI helpers ---------------------------------------------------------------
function provenanceBadge(prov) {
  const src = (prov && prov.source) || "unavailable";
  const label = (prov && prov.label) || "UNAVAILABLE";
  const cls = {
    live: "badge-ok", cached: "badge-info", stale: "badge-warn",
    simulated: "badge-sim", unavailable: "badge-err",
  }[src] || "badge-info";
  return `<span class="badge ${cls}">${label}</span>`;
}

function fmt(n, digits = 2) {
  if (n === null || n === undefined) return "—";
  const v = Number(n);
  if (!isFinite(v)) return "—";
  if (Math.abs(v) >= 1e7) return (v / 1e7).toFixed(digits) + " Cr";
  if (Math.abs(v) >= 1e5) return (v / 1e5).toFixed(digits) + " L";
  if (Math.abs(v) >= 1000) return v.toLocaleString("en-IN", { maximumFractionDigits: digits });
  return v.toLocaleString("en-IN", { maximumFractionDigits: digits });
}

function pct(n, digits = 2) {
  if (n === null || n === undefined) return "—";
  return Number(n).toFixed(digits) + "%";
}

function el(id) { return document.getElementById(id); }
function show(viewId) {
  for (const v of document.querySelectorAll(".view")) {
    v.classList.remove("active");
    v.hidden = true;
  }
  const target = el("view-" + viewId);
  if (target) {
    target.classList.add("active");
    target.hidden = false;
  }
  for (const a of document.querySelectorAll("nav#nav a")) {
    a.classList.toggle("active", a.dataset.route === viewId);
  }
}

function setLoading(target, msg) {
  target.innerHTML = `<p class="loading">${msg || "Loading…"}</p>`;
}
function setError(target, err) {
  target.innerHTML = `<p class="error-msg">Error: ${err.message || err}</p>`;
}
