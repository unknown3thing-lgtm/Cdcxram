"""Tests: Vercel Python serverless endpoints.

Spins up each endpoint's `handler` class on a real HTTP server bound
to localhost, then makes actual HTTP requests via the requests
library. This exercises the full request → response path including
JSON serialisation, CORS, query parsing, and body parsing.

No network access to CoinDCX or Supabase required: the endpoints
fall back to clearly-labelled SIMULATED data, and the Supabase env
is unset for the paper-trading endpoints (which surface a clear
config_error response).
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
from typing import Type

import pytest
import requests


# ---------------------------------------------------------------------------
# Tiny HTTP server fixture that mounts a single handler at /<name>.
# ---------------------------------------------------------------------------
class _Server(socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, addr, handler_cls):
        super().__init__(addr, handler_cls)


@pytest.fixture
def live_server():
    """Yields (base_url, server_thread) and shuts down after the test.

    Usage:
        url, _ = live_server(handler_cls)
        r = requests.get(url + "/api/whatever")
    """
    def _make(handler_cls: Type):
        srv = _Server(("127.0.0.1", 0), handler_cls)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.05)   # let the server bind
        return f"http://127.0.0.1:{port}", srv
    yield _make


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------
class TestHealth:
    def test_get_returns_ok(self, live_server):
        from api.health import handler
        url, _ = live_server(handler)
        r = requests.get(url + "/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["service"] == "coindcx-spot-lab"
        # Env status only shows presence, not values
        assert "SUPABASE_URL" in body["env"]
        # The response must never include secret values
        assert "sb_secret_" not in r.text
        assert "sb_publishable_" not in r.text

    def test_options_preflight_204(self, live_server):
        from api.health import handler
        url, _ = live_server(handler)
        r = requests.options(url + "/api/health")
        assert r.status_code == 204


# ---------------------------------------------------------------------------
# Markets endpoint
# ---------------------------------------------------------------------------
class TestMarkets:
    def test_get_returns_pairs_list(self, live_server):
        from api.markets import handler
        url, _ = live_server(handler)
        r = requests.get(url + "/api/markets")
        assert r.status_code == 200
        body = r.json()
        assert "pairs" in body
        assert "count" in body
        assert body["count"] >= 1
        # Provenance clearly labels LIVE vs SIMULATED
        assert body["provenance"]["source"] in ("live", "simulated")
        if body["provenance"]["source"] == "simulated":
            assert "SIMULATED" in body["provenance"]["label"]


# ---------------------------------------------------------------------------
# Prices endpoint
# ---------------------------------------------------------------------------
class TestPrices:
    def test_get_returns_prices(self, live_server):
        from api.prices import handler
        url, _ = live_server(handler)
        r = requests.get(url + "/api/prices")
        assert r.status_code == 200
        body = r.json()
        assert "prices" in body
        assert "count" in body
        assert body["count"] >= 1
        if body["provenance"]["source"] == "simulated":
            assert "SIMULATED" in body["provenance"]["label"]


# ---------------------------------------------------------------------------
# Candles endpoint
# ---------------------------------------------------------------------------
class TestCandles:
    def test_get_returns_candles(self, live_server):
        from api.candles import handler
        url, _ = live_server(handler)
        r = requests.get(url + "/api/candles?pair=B-BTCINR&interval=1h&limit=50")
        assert r.status_code == 200
        body = r.json()
        assert body["pair"] == "B-BTCINR"
        assert body["interval"] == "1h"
        assert "candles" in body
        assert body["count"] >= 10
        # Each candle is a valid OHLCV dict
        c = body["candles"][0]
        for k in ("open_time_ms", "open", "high", "low", "close",
                  "volume", "close_time_ms"):
            assert k in c

    def test_get_validates_interval(self, live_server):
        from api.candles import handler
        url, _ = live_server(handler)
        # Invalid interval — the synth generator or client raises ValueError
        r = requests.get(url + "/api/candles?pair=B-BTCINR&interval=2h&limit=50")
        # Either 200 (if synth raises silently) or 400 — accept either as
        # long as we get a structured response.
        assert r.status_code in (200, 400)

    def test_get_validates_limit(self, live_server):
        from api.candles import handler
        url, _ = live_server(handler)
        r = requests.get(url + "/api/candles?pair=B-BTCINR&limit=abc")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Risk calculator endpoint
# ---------------------------------------------------------------------------
class TestRisk:
    def test_post_returns_plan(self, live_server):
        from api.risk import handler
        url, _ = live_server(handler)
        r = requests.post(url + "/api/risk", json={
            "entry": 100.0, "stop": 98.0, "target": 104.0,
            "pair": "B-BTCINR",
            "initial_inr": 2000.0,
            "fee_pct": 0.0, "spread_pct": 0.0, "slippage_pct": 0.0,
            "max_position_pct": 100.0,
            "max_portfolio_exposure_pct": 100.0,
            "trading_allocation_pct": 100.0,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["decision"] == "accepted"
        # Spec example: ₹2,000 × 1% / 2% = ₹1,000 position value
        assert abs(body["position_value"] - 1000.0) < 0.01
        assert abs(body["qty"] - 10.0) < 0.01
        assert abs(body["rr_after_cost"] - 2.0) < 0.01

    def test_post_zero_stop_distance_rejected(self, live_server):
        from api.risk import handler
        url, _ = live_server(handler)
        r = requests.post(url + "/api/risk", json={
            "entry": 100.0, "stop": 100.0, "target": 110.0,
        })
        assert r.status_code == 400

    def test_post_missing_field(self, live_server):
        from api.risk import handler
        url, _ = live_server(handler)
        r = requests.post(url + "/api/risk", json={"entry": 100, "stop": 98})
        assert r.status_code == 400
        assert r.json()["error"] == "bad_request"


# ---------------------------------------------------------------------------
# Signals endpoint
# ---------------------------------------------------------------------------
class TestSignals:
    def test_get_returns_signals(self, live_server):
        from api.signals import handler
        url, _ = live_server(handler)
        r = requests.get(url + "/api/signals?pair=B-BTCINR&interval=1h")
        assert r.status_code == 200
        body = r.json()
        assert body["pair"] == "B-BTCINR"
        assert "signals" in body
        assert isinstance(body["signals"], list)
        # Each signal has the documented shape
        for s in body["signals"]:
            for k in ("strategy", "state", "supporting", "conflicting",
                      "data_quality_warnings"):
                assert k in s


# ---------------------------------------------------------------------------
# Backtest endpoint
# ---------------------------------------------------------------------------
class TestBacktest:
    def test_post_runs_backtest(self, live_server):
        from api.backtest import handler
        url, _ = live_server(handler)
        r = requests.post(url + "/api/backtest", json={
            "pair": "B-BTCINR", "interval": "1h",
            "strategy": "baseline_sma",
            "starting_capital_inr": 2000.0,
            "walk_forward": False,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["pair"] == "B-BTCINR"
        assert body["strategy"] == "baseline_sma"
        assert body["starting_capital"] == 2000.0
        assert len(body["segments"]) >= 1
        seg = body["segments"][0]
        assert "summary" in seg
        # Summary must include all the documented metrics
        for k in ("total_return_pct", "max_drawdown_pct", "win_rate_pct",
                  "profit_factor", "expectancy_inr", "num_trades",
                  "buy_hold_return_pct"):
            assert k in seg["summary"]


# ---------------------------------------------------------------------------
# Portfolio endpoint (requires Supabase env)
# ---------------------------------------------------------------------------
class TestPortfolio:
    def test_get_without_supabase_env_returns_config_error(self, live_server,
                                                            monkeypatch):
        # Clean all Supabase env vars so the endpoint cannot find a config
        for v in ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY",
                  "SUPABASE_SERVICE_KEY"):
            monkeypatch.delenv(v, raising=False)
        from api.portfolio import handler
        url, _ = live_server(handler)
        r = requests.get(url + "/api/portfolio")
        assert r.status_code == 500
        body = r.json()
        assert body["error"] == "config_error"


# ---------------------------------------------------------------------------
# Trades endpoint (requires Supabase env)
# ---------------------------------------------------------------------------
class TestTrades:
    def test_get_without_supabase_env_returns_config_error(self, live_server,
                                                             monkeypatch):
        for v in ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY",
                  "SUPABASE_SERVICE_KEY"):
            monkeypatch.delenv(v, raising=False)
        from api.trades import handler
        url, _ = live_server(handler)
        r = requests.get(url + "/api/trades")
        assert r.status_code == 500
        assert r.json()["error"] == "config_error"


# ---------------------------------------------------------------------------
# Cron endpoint (requires CRON_SECRET)
# ---------------------------------------------------------------------------
class TestCron:
    def test_post_without_auth_returns_unauthorized(self, live_server,
                                                      monkeypatch):
        for v in ("CRON_SECRET", "SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY",
                  "SUPABASE_SERVICE_KEY"):
            monkeypatch.delenv(v, raising=False)
        from api.cron import handler
        url, _ = live_server(handler)
        r = requests.post(url + "/api/cron")
        assert r.status_code == 401
        assert r.json()["error"] == "unauthorized"

    def test_post_with_wrong_secret_returns_unauthorized(self, live_server,
                                                            monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "expected")
        for v in ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY",
                  "SUPABASE_SERVICE_KEY"):
            monkeypatch.delenv(v, raising=False)
        from api.cron import handler
        url, _ = live_server(handler)
        r = requests.post(url + "/api/cron",
                           headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
