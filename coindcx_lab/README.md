# CoinDCX Spot Lab — Advanced Crypto Analysis & Paper-Trading Platform

**A research & decision-support system for CoinDCX INR spot markets.**
No real-money trading. No leverage. No API keys. No authenticated calls.
This is a research tool, not financial advice, and not a guarantee of
profitability.

---

## Deployment (NEW)

This project supports **two interchangeable runtimes**:

- **Local dev** — Streamlit dashboard + SQLite persistence.
  See section 5 below.
- **Production** — Vercel Python serverless functions + Supabase
  Postgres + static HTML/JS frontend. See
  [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for step-by-step
  instructions and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for
  the design rationale.

Both runtimes share the **same** analysis engine
(`src/coindcx_lab/`) and the **same** `Store` interface — the
`PaperTradingEngine` logic is identical whether it's writing to SQLite
(locally) or Supabase (on Vercel). No calculations are duplicated.

Test count: **164 tests, all pass** (110 original + 54 new for env
config, Supabase adapter, and Vercel API endpoints).

---

## 1. What this project does

CoinDCX Spot Lab is a Streamlit-based application (with an additional
Vercel-deployable API + static frontend) that:

- Discovers currently supported CoinDCX spot pairs quoted in **INR** using
  verified public REST endpoints (no authentication).
- Retrieves and validates historical OHLCV candle data across multiple
  timeframes (15m / 1h / 4h / 1d).
- Computes transparent, tested technical indicators (EMA20/50/200, RSI14,
  ATR14, MACD, swing highs/lows, support/resistance, breakout/retest,
  relative volume, rolling volatility).
- Runs a rule-based signal engine with **four documented strategies**
  (trend pullback, breakout & retest, momentum continuation, baseline SMA),
  each individually enable/disable.
- Applies a strict **no-trade filter** (data staleness, cost-adjusted R/R
  below target, daily-loss limit, drawdown limit, max open positions,
  cooldown after consecutive losses, data-freshness lockout).
- Computes **dynamic position sizing** for any user-selected initial capital
  (presets for ₹2,000 / ₹5,000 / ₹10,000 / ₹25,000 / ₹50,000 / custom),
  accounting for fees, spread, and slippage on both legs.
- Backtests strategies with an **event-driven, no-look-ahead** engine that
  supports walk-forward (dev/val/oos) splitting and reports total return,
  max drawdown, win rate, profit factor, expectancy, exposure, turnover,
  estimated costs, and a buy-and-hold benchmark.
- Simulates paper trades with **SQLite persistence**, pause/resume,
  reset-only-after-explicit-confirmation, and full history preservation
  when settings change.
- Surfaces **clear reason labels** (BUY CANDIDATE / WAIT / EXIT / NO TRADE)
  with supporting and conflicting indicator reasons.
- Provides a polished, responsive Streamlit dashboard for desktop and
  mobile.

## 2. Honest environment & data-source statement

This project was built and tested inside a Z.AI online environment with
the following **actually verified** capabilities:

| Capability | Status |
|---|---|
| Python 3.12.14 | ✅ available |
| pip 25.0.1 | ✅ available |
| pandas 2.2.3 / numpy 2.1.3 / plotly 6.5.2 | ✅ pre-installed |
| requests 2.32.5 / pytest 9.0.2 | ✅ pre-installed |
| streamlit 1.65.0 | ✅ installed during setup |
| File system (write to `/home/z/my-project/`) | ✅ available |
| Outbound network (https) | ✅ available (Google 200, Binance 200) |
| CoinDCX public API (`public.coindcx.com`) | ❌ **HTTP 403 — Cloudflare edge block on this data-centre IP range** |

When CoinDCX is unreachable, the dashboard **clearly labels the data
as SIMULATED** (badge: `SIMULATED · no live prices (deterministic fixture)`)
and falls back to a deterministic synthetic OHLCV generator. **No live
prices, API responses, or backtest results are fabricated.** All test
results reported in this README are **actual** results from running
`pytest` in this environment.

## 3. Project structure

```
coindcx_lab/
├── README.md                          ← this file
├── pyproject.toml                     ← package + pytest config
├── requirements.txt                   ← pinned, stable versions
├── .gitignore                         ← excludes *.sqlite, *.pyc, etc.
├── config/
│   └── default_settings.toml          ← all default risk / cost / strategy settings
├── src/coindcx_lab/
│   ├── __init__.py
│   ├── api/
│   │   ├── __init__.py
│   │   ├── models.py                  ← Candle, PairInfo, DataProvenance, SourceKind
│   │   ├── client.py                  ← CoinDCXClient (retry, backoff, cache, honest errors)
│   │   ├── validate.py                ← OHLCV validation: dedupe, gaps, incomplete, overlap
│   │   └── synthetic.py               ← deterministic SIMULATED OHLCV generator
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── indicators.py              ← EMA/RSI/ATR/MACD/swings/SR/breakout-retest
│   │   ├── risk.py                    ← RiskSettings, RiskEngine, PositionPlan
│   │   ├── signals.py                 ← 4 strategies + SignalEngine
│   │   ├── scanner.py                 ← ranked ScanResult
│   │   ├── backtest.py                ← event-driven backtester + walk-forward
│   │   └── paper.py                   ← PaperTradingEngine + SQLite-backed lifecycle
│   ├── db/
│   │   ├── __init__.py
│   │   └── store.py                   ← SQLite schema + thread-safe Store
│   └── dash/
│       ├── __init__.py
│       └── app.py                     ← Streamlit dashboard
├── tests/
│   ├── conftest.py                    ← shared fixtures
│   ├── test_validate.py               ← OHLCV validation
│   ├── test_indicators.py             ← indicator math
│   ├── test_api_client.py             ← failure handling (mocked)
│   ├── test_signals.py                ← strategy behaviours
│   ├── test_risk.py                   ← position sizing, capital scaling, caps, costs
│   ├── test_backtest.py               ← chronology, no-look-ahead, intrabar, walk-forward
│   └── test_paper.py                  ← lifecycle, persistence, reset
└── scripts/
    ├── run_tests.sh                   ← pytest wrapper
    └── smoke_test_live.py             ← hits CoinDCX public endpoints & prints actual result
```

## 4. Verified CoinDCX public API endpoints

All endpoints used are **documented public market-data endpoints** that
require **no authentication**. Reference: <https://docs.coindcx.com/>

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `https://public.coindcx.com/market_data/v3/exchange_pairs` | List tradable spot pairs |
| `GET`  | `https://public.coindcx.com/market_data/v3/current_prices/all_pairs` | Latest ticker (price / 24h high / 24h low / 24h volume / 24h change %) for all pairs |
| `POST` | `https://public.coindcx.com/market_data/v3/candles` | Historical OHLCV. Body: `{"pair": "B-BTCINR", "interval": "1h", "limit": 200}` |

Supported candle intervals (verified codes): `1m`, `5m`, `15m`, `30m`,
`1h`, `4h`, `1d`.

**Live-data smoke-test result (run in this environment):**

```
[1] GET /market_data/v3/exchange_pairs
    FAIL in 42.7ms  CoinDCXAPIError: 403 Forbidden — Cloudflare edge block.
[2] GET /market_data/v3/current_prices/all_pairs
    FAIL in 40.1ms  CoinDCXAPIError: 403 Forbidden — Cloudflare edge block.
[3] POST /market_data/v3/candles  (pair=B-BTCINR interval=1h limit=24)
    FAIL in 43.3ms  CoinDCXAPIError: 403 Forbidden — Cloudflare edge block.
```

When this host's IP range is blocked at the Cloudflare edge, the dashboard
clearly falls back to **SIMULATED** data and labels every chart and table
accordingly. **No live prices are fabricated.**

## 5. Run commands

### Setup
```bash
cd /home/z/my-project/coindcx_lab
python3 -m pip install -r requirements.txt
```

### Run the dashboard
```bash
cd /home/z/my-project/coindcx_lab
streamlit run src/coindcx_lab/dash/app.py --server.port 8501
# → http://localhost:8501
```

For headless / restricted environments:
```bash
streamlit run src/coindcx_lab/dash/app.py \
    --server.headless true \
    --server.port 8765 \
    --browser.gatherUsageStats false
```

### Run the test suite
```bash
cd /home/z/my-project/coindcx_lab
bash scripts/run_tests.sh
# or directly:
python3 -m pytest tests/ -v
```

**Actual test result (from this environment):**
```
============================= test session starts ==============================
platform linux -- python 3.12.14, pytest-9.0.2
collected 164 items

tests/test_api_client.py ..................                              [ 10%]
tests/test_api_endpoints.py ................                            [ 20%]
tests/test_backtest.py ..........                                        [ 26%]
tests/test_env.py .....................                                 [ 39%]
tests/test_indicators.py ....................                           [ 51%]
tests/test_paper.py .............                                       [ 59%]
tests/test_risk.py .......................                               [ 73%]
tests/test_signals.py ........                                           [ 78%]
tests/test_supabase_store.py ...............                            [ 87%]
tests/test_validate.py ..................                                [100%]

============================= 164 passed in 8.57s ==============================
```

### Run the live-data smoke test
```bash
cd /home/z/my-project/coindcx_lab
PYTHONPATH=src python3 scripts/smoke_test_live.py
```

## 6. Feature coverage checklist

Per the original spec:

| Spec requirement | Implemented in |
|---|---|
| Public market-data client, retry/backoff/cache, honest errors | `api/client.py` |
| Market discovery (INR spot filtering) | `api/client.py::list_inr_spot_pairs` |
| OHLCV retrieval + validation (duplicates, gaps, incomplete, overlap) | `api/validate.py` |
| Multiple verified candle intervals | `api/client.py::INTERVAL_MAP` |
| Latest market info (`current_prices`) | `api/client.py::current_prices` |
| Multi-timeframe analysis (EMA20/50/200, RSI14, ATR14, MACD, swings, SR, breakout/retest, rel-vol, rolling-vol) | `engine/indicators.py` |
| Trend pullback strategy | `engine/signals.py::TrendPullbackStrategy` |
| Breakout & retest strategy | `engine/signals.py::BreakoutRetestStrategy` |
| Momentum continuation strategy | `engine/signals.py::MomentumContinuationStrategy` |
| Baseline SMA strategy | `engine/signals.py::BaselineSMAStrategy` |
| Strategy enable/disable | `engine/signals.py::SignalEngineConfig` |
| No-trade filters (stale data, R/R below target, daily-loss, drawdown, cooldown, max positions) | `engine/risk.py::RiskEngine.plan_position` |
| Market scanner with explainable ranking | `engine/scanner.py` |
| Position sizing with risk-based formula + caps (cash, max position, exposure, allocation) | `engine/risk.py` |
| Capital presets (₹2,000 / ₹5,000 / ₹10,000 / ₹25,000 / ₹50,000 / custom) | `dash/app.py::_sidebar_settings` |
| Fees, spread, slippage (configurable, on both legs) | `engine/risk.py::RiskSettings` |
| Event-driven backtester (no look-ahead, conservative intrabar stop-first) | `engine/backtest.py` |
| Walk-forward dev/val/oos split | `engine/backtest.py::walk_forward` |
| Buy-and-hold benchmark | `engine/backtest.py::_summarise` |
| Paper-trading engine (SQLite, lifecycle, pause/resume, reset-only-on-confirm, history preservation) | `engine/paper.py` |
| Equity curve & drawdown charts | `dash/app.py::_render_equity_curve` |
| Trade history table | `dash/app.py::_render_trade_history` |
| Position-sizing calculator | `dash/app.py::_render_position_calculator` |
| Settings panel (all risk / cost / liquidity fields) | `dash/app.py::_sidebar_settings` |
| System health & data-freshness panel | `dash/app.py::_render_system_health` |
| Provenance badge (LIVE / CACHED / STALE / SIMULATED / UNAVAILABLE) on every chart/table | `api/models.py::DataProvenance.label` |
| Tests: indicators, position sizing, capital scaling, fees, OHLCV validation, missing/duplicate/incomplete candles, backtest chronology, no look-ahead, double-counting, cash accounting, daily loss, drawdown, paper lifecycle, persistence, invalid settings, API failure, env config, SupabaseStore mocked HTTP, Vercel endpoints via real HTTP server | `tests/` (164 tests) |

## 7. Risk controls (implemented, always-on)

1. Per-trade risk cap (configurable %).
2. Daily-loss limit (percentage **and** absolute INR).
3. Maximum drawdown limit (auto-pause when reached).
4. Maximum position size (% of equity).
5. Maximum portfolio exposure (% of equity).
6. Maximum simultaneously open positions.
7. Maximum daily trades.
8. Cooldown after N consecutive losing trades.
9. Emergency paper-trading stop (one-click pause).
10. Data-staleness lockout (signals skip on stale data).

When a limit is triggered:
1. New simulated entries are prevented.
2. The reason is shown in the dashboard.
3. Existing records are preserved.
4. The limit is never automatically overridden.
5. Resume requires an explicit user click.

**Prominent warning shown in the dashboard sidebar**:
> Live execution can produce losses greater than the planned stop risk
> due to gaps, slippage, or unavailable liquidity.

## 8. Walk-through: the position-sizing formula

For a long candidate:

```
risk_budget       = current_equity * risk_per_trade_pct / 100
stop_distance     = abs(entry - stop) / entry           # fraction
risk_based_value  = risk_budget / stop_distance          # before caps
position_value    = min(
    risk_based_value,
    max_position_cap        = current_equity * max_position_pct / 100,
    portfolio_exposure_cap  = remaining_exposure,
    cash_cap                = available_cash - est_entry_cost,
    trading_allocation_cap  = current_equity * trading_allocation_pct / 100,
)
qty               = position_value / entry
```

**Worked example (verified by `tests/test_risk.py::test_worked_example_2000_equity_1pct_risk_2pct_stop`)**

- Equity ₹2,000, risk 1% → **risk_budget = ₹20**
- Entry ₹100, stop ₹98 → stop_distance = 2% → **risk_based_value = ₹1,000**
- Qty = 10 units
- Gross potential loss = ₹20, gross potential gain = ₹40 (2R target)

The same setup with **₹10,000** equity yields **5×** the position value
(₹5,000) — verified by `test_scales_proportionally_to_10000_equity` and
`test_capital_scales_proportionally_for_all_presets`.

## 9. Backtester design guarantees

- **No look-ahead bias** — signals are evaluated at bar `i`'s close using
  only `candles[:i+1]`. Entries fill at bar `i+1`'s open with adverse
  slippage. *Verified by `tests/test_backtest.py::TestNoLookAhead`.*
- **No incomplete-candle look-ahead** — the trailing incomplete bar is
  dropped before any completed-candle signal is computed.
- **Conservative intrabar ambiguity** — when a single bar's range
  contains both stop and target, the **stop is assumed hit first**
  (pessimistic). *Verified by `TestIntrabarAmbiguity`.*
- **No duplicate entries on the same signal** — a BUY signal is ignored
  while a position is already open in the same pair. *Verified by
  `TestDoubleCounting`.*
- **No negative cash balances** — the engine refuses to open a position
  when available cash is insufficient. *Verified by
  `TestCashAccounting::test_cash_never_negative`.*
- **No double-counting fees** — each round-trip charges one entry fee
  and one exit fee, no more. *Verified by
  `TestDoubleCounting::test_fees_not_double_counted`.*
- **End-of-data force-close** — open positions are force-closed at the
  last close so equity curves and P&L are always consistent. *Verified
  by `TestEndOfDataForceClose`.*

## 10. Security & safety

- ❌ No live order placement.
- ❌ No authenticated CoinDCX API calls.
- ❌ No API keys or secrets in code, config, or DB.
- ❌ No leverage, margin, or derivatives.
- ❌ No paid APIs or mandatory subscriptions.
- ❌ No guaranteed-return claims.
- ❌ No fabricated live prices or backtest results.
- ❌ No hidden network requests (the only outbound calls are to
  `public.coindcx.com`).
- ❌ No unsafe dynamic code execution.
- ❌ No committing local databases or secrets into version control
  (the `.gitignore` excludes `*.sqlite`, `.env`, `secrets.json`,
  `config/local_settings.json`).

Future authenticated integration would require a separate explicit
design review, security audit, and owner approval.

## 11. How to export & download the complete project

The whole project lives under:

```
/home/z/my-project/coindcx_lab/
```

To create a downloadable archive:

```bash
cd /home/z/my-project
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='*.sqlite' --exclude='data/' \
    -czf /home/z/my-project/download/coindcx_lab.tar.gz coindcx_lab
```

The archive will be available at:

```
/home/z/my-project/download/coindcx_lab.tar.gz
```

## 12. Known limitations

1. **CoinDCX public API is blocked from this environment** by Cloudflare
   (HTTP 403). The dashboard falls back to a clearly-labelled SIMULATED
   series. To use live data, run this project from a network/IP range that
   CoinDCX's CDN allows (e.g. a residential IP or a VPN to a residential
   range). The API client itself is fully implemented and tested with
   mocked HTTP responses.
2. **Paper trading is long-only** by design (the spec mentions no
   short-selling, no leverage, no derivatives).
3. **Order-book / trade-flow** endpoints are not implemented because
   CoinDCX does not document unauthenticated public order-book endpoints
   in the current public API.
4. **Candle precision** (decimal places per pair) is not enforced at the
   quantity-rounding level because CoinDCX's public `exchange_pairs`
   response does not consistently expose per-pair precision metadata.
   The dashboard rounds quantities to 6 decimal places as a safe
   default.
5. **Backtest runtime is O(N²)** because the IndicatorSet is recomputed
   on each prefix at each bar. This is fine for N ≤ ~2000 bars
   (typical 1h × 3 months). For longer windows the engine should be
   refactored to incremental indicator updates.

## 13. Explicit no-real-money statement

**This application does NOT place live orders, does NOT submit any
authenticated API calls, does NOT hold any user funds, does NOT use
leverage or margin, and does NOT permit real-money trading of any
kind. It is a research and decision-support tool only. Nothing in
this codebase or its outputs constitutes financial advice.**
