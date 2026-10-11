# CoinDCX Spot Lab — Architecture

This document explains the architecture choices that allow the project
to support **two interchangeable runtimes** (local Streamlit + Vercel
serverless) and **two interchangeable storage backends** (SQLite +
Supabase) without duplicating any of the analysis logic.

---

## 1. Layered architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Runtime layer                                                        │
│  ┌─────────────────────────────┐  ┌────────────────────────────────┐│
│  │  Streamlit (local dev)        │  │  Vercel Python (serverless)    ││
│  │  src/coindcx_lab/dash/app.py │  │  api/*.py (11 endpoints)       ││
│  └────────────┬────────────────┘  └────────────┬───────────────────┘│
│               │                                  │                   │
│               │  both import ↓                   │                   │
│               ▼                                  ▼                   │
├──────────────────────────────────────────────────────────────────────┤
│  Engine layer  (src/coindcx_lab/engine/)                              │
│    indicators.py    EMA, RSI, ATR, MACD, swings, SR, breakout/retest │
│    risk.py          position sizing, caps, costs, R/R                 │
│    signals.py       4 strategies + SignalEngine + no-trade filters   │
│    scanner.py       ranked ScanResult                                 │
│    backtest.py      event-driven, no look-ahead                       │
│    paper.py         PaperTradingEngine (backend-agnostic)            │
│                                                                       │
│  API/data layer (src/coindcx_lab/api/, db/)                           │
│    api/client.py    CoinDCXClient (retry, backoff, cache)            │
│    api/validate.py  OHLCV validation                                 │
│    api/synthetic.py  labelled SIMULATED generator                    │
│    api/env.py       env-var config (SupabaseConfig, etc.)             │
│    db/store.py      SQLite Store                                      │
│    db/supabase_store.py  SupabaseStore (same interface as Store)     │
│    server/          HTTP helpers used by api/*.py                    │
├──────────────────────────────────────────────────────────────────────┤
│  Storage layer                                                        │
│  ┌─────────────┐                          ┌────────────────────────┐ │
│  │  SQLite     │  ← Store (file)           │  Supabase (REST+RLS)   │ │
│  │  *.sqlite   │                           │  7 tables (see below)  │ │
│  └─────────────┘                          └────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. Why this design works

### No duplicated calculations

Every endpoint in `api/*.py` imports from `src/coindcx_lab/` and calls
the **same** indicators, risk engine, signals, scanner, backtester,
and paper-trading engine that the Streamlit dashboard uses. There is
no JavaScript re-implementation of any calculation. The static
frontend (`web/`) is purely a JSON consumer.

### Backend-agnostic paper trading

The `Store` interface (`src/coindcx_lab/db/store.py`) defines the
methods the `PaperTradingEngine` needs: `save_settings`, `load_settings`,
`save_state`, `load_state`, `insert_trade`, `update_trade_exit`,
`list_trades`, `get_open_trades`, `count_trades_today`,
`append_equity_snapshot`, `list_equity_snapshots`, `reset_all_trades`.

- The SQLite `Store` implements all of these against a local file.
- The Supabase `SupabaseStore` implements the same methods against the
  Supabase REST API (PostgREST).

The `PaperTradingEngine` is constructed with whichever backend you
pass in. **It never knows which it's using.**

### Honest data provenance

Every candle batch and ticker response carries a `DataProvenance`
object whose `source` field is one of:

- `LIVE`        — fetched from CoinDCX public API successfully
- `CACHED`     — served from local cache within TTL
- `STALE`      — live fetch failed, served expired cache
- `SIMULATED`  — deterministic synthetic series (no live prices)
- `UNAVAILABLE` — no data available at all

The Vercel API endpoints return this provenance in their JSON
response (`provenance: { source, endpoint, label, note, ... }`).
The static frontend renders it as a colored badge on every chart
and table. **Simulated data is never presented as live.**

---

## 3. Supabase schema (additive, RLS-protected)

| Table | PK | Purpose | RLS |
|---|---|---|---|
| `user_settings` | `user_id UUID` | per-user risk/cost/capital config | owner-scoped + anon |
| `paper_state` | `user_id UUID` | per-user account state (equity, cash, daily loss) | owner-scoped + anon |
| `paper_trades` | `id BIGSERIAL` | full lifecycle of each paper trade | owner-scoped + anon |
| `paper_signals` | `id BIGSERIAL` | persisted signal snapshots | owner-scoped + anon |
| `equity_snapshots` | `id BIGSERIAL` | equity curve points | owner-scoped + anon |
| `market_data_cache` | `pair TEXT` | latest ticker per pair (cron-written) | public read, server-only write |
| `job_log` | `id BIGSERIAL` | cron job execution log | public read, server-only write |

**Migration files** (under `supabase/migrations/`, all idempotent):

1. `0001_initial_schema.sql` — creates all 7 tables
2. `0002_rls_policies.sql` — enables RLS + owner-scoped policies
3. `0003_anon_user_policies.sql` — allows the shared anonymous user
   to access their own rows (for the MVP without Supabase Auth)

To apply: see `docs/DEPLOYMENT.md` section 3.

---

## 4. Vercel API surface

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET`  | `/api/health` | service health (no secrets) | none |
| `GET`  | `/api/markets` | INR spot pairs list | none |
| `GET`  | `/api/prices` | latest tickers | none |
| `GET`  | `/api/candles` | historical OHLCV (?pair=&interval=&limit=) | none |
| `GET`  | `/api/signals` | signal explanations (?pair=&interval=) | none |
| `POST` | `/api/risk` | position-sizing calculator | none |
| `POST` | `/api/scanner` | ranked market scanner | none |
| `POST` | `/api/backtest` | event-driven backtest | none |
| `GET`  | `/api/portfolio` | paper portfolio summary | Supabase env required |
| `GET`  | `/api/trades` | paper trade history | Supabase env required |
| `POST` | `/api/trades` | open a new paper trade | Supabase env required |
| `POST` | `/api/cron` | scheduled market-data refresh | CRON_SECRET |

The Supabase-env-required endpoints return HTTP 500 `config_error`
when `SUPABASE_URL` or `SUPABASE_PUBLISHABLE_KEY` are unset, so they
degrade gracefully in local dev without crashing the import path.

---

## 5. Frontend

`web/index.html` is a single-page vanilla-HTML/CSS/JS app. It uses
Plotly.js (loaded from CDN) for charts. No build step, no Node.js,
no framework. It calls the Vercel API via relative paths
(`/api/<name>`) so the same-origin deployment works without CORS.

Routing is hash-free and uses `pushState` + `popstate` — Vercel's
`vercel.json` rewrites the SPA routes (`/scan`, `/chart`, etc.) back
to `web/index.html` so direct deep-linking works.
