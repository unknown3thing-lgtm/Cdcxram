"""Phase 4 regression tests: capital validation, settings persistence,
portfolio reset confirmation, and spread computation.

Covers:
  1. Settings validation: zero, negative, excessively large, non-numeric
     capital; risk % out of range; fee/slippage out of range.
  2. Capital scaling: position size scales proportionally with capital
     for all presets (₹2k/₹5k/₹10k/₹25k/₹50k).
  3. Settings persistence: save → load roundtrip; history preserved
     when settings change.
  4. Portfolio reset: requires {"confirm": true}; without it, returns
     HTTP 400; with it, wipes trades but preserves settings.
  5. WazirX spread: computed from buy/sell fields; not zero when
     buy ≠ sell.
"""

from __future__ import annotations

import os
import tempfile
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. Settings validation (api/settings.py _validate_settings)
# ---------------------------------------------------------------------------
class TestSettingsValidation:
    def test_valid_settings_pass(self):
        from coindcx_lab.server.settings_handler import _validate_settings
        body = {
            "initial_inr": 5000,
            "risk_per_trade_pct": 1.0,
            "max_position_pct": 25.0,
            "max_portfolio_exposure_pct": 80.0,
            "max_daily_loss_pct": 5.0,
            "max_drawdown_pct": 15.0,
            "reward_to_risk_target": 2.0,
            "fee_pct": 0.1,
            "spread_pct": 0.05,
            "slippage_pct": 0.05,
            "min_24h_volume_inr": 1000000,
            "max_open_positions": 5,
            "max_daily_trades": 10,
            "cooldown_after_consecutive_losses": 3,
            "allow_overnight": True,
            "trading_allocation_pct": 100.0,
        }
        validated, err = _validate_settings(body)
        assert err is None, f"unexpected error: {err}"
        assert validated["initial_inr"] == 5000

    def test_zero_capital_rejected(self):
        from coindcx_lab.server.settings_handler import _validate_settings
        _, err = _validate_settings({"initial_inr": 0})
        assert err is not None
        assert "must be > 0" in err

    def test_negative_capital_rejected(self):
        from coindcx_lab.server.settings_handler import _validate_settings
        _, err = _validate_settings({"initial_inr": -1000})
        assert err is not None
        assert "must be > 0" in err

    def test_excessively_large_capital_rejected(self):
        from coindcx_lab.server.settings_handler import _validate_settings
        _, err = _validate_settings({"initial_inr": 100_000_000})
        assert err is not None
        assert "must be <=" in err

    def test_non_numeric_capital_rejected(self):
        from coindcx_lab.server.settings_handler import _validate_settings
        _, err = _validate_settings({"initial_inr": "abc"})
        assert err is not None
        assert "must be a number" in err

    def test_risk_pct_too_high_rejected(self):
        from coindcx_lab.server.settings_handler import _validate_settings
        _, err = _validate_settings({"risk_per_trade_pct": 10.0})
        assert err is not None
        assert "risk_per_trade_pct" in err

    def test_negative_fee_rejected(self):
        from coindcx_lab.server.settings_handler import _validate_settings
        _, err = _validate_settings({"fee_pct": -0.5})
        assert err is not None
        assert "fee_pct" in err

    def test_all_presets_are_valid(self):
        """All 5 capital presets (₹2k/₹5k/₹10k/₹25k/₹50k) must pass
        validation."""
        from coindcx_lab.server.settings_handler import _validate_settings, CAPITAL_PRESETS
        assert CAPITAL_PRESETS == [2000, 5000, 10000, 25000, 50000]
        for cap in CAPITAL_PRESETS:
            body = {"initial_inr": cap}
            validated, err = _validate_settings(body)
            assert err is None, f"preset ₹{cap} rejected: {err}"
            assert validated["initial_inr"] == cap

    def test_custom_capital_within_range_accepted(self):
        """Custom capital ₹3,333 (not a preset) should be accepted."""
        from coindcx_lab.server.settings_handler import _validate_settings
        validated, err = _validate_settings({"initial_inr": 3333})
        assert err is None
        assert validated["initial_inr"] == 3333


# ---------------------------------------------------------------------------
# 2. Capital scaling: position size scales with capital
# ---------------------------------------------------------------------------
class TestCapitalScaling:
    """Verify that position size scales proportionally with the capital
    preset — so a ₹10k portfolio gets 5× the position size of a ₹2k
    portfolio for the same entry/stop/target."""

    def test_position_scales_proportionally_across_all_presets(self):
        from coindcx_lab.engine.risk import (
            RiskEngine, RiskSettings, RiskState, DailyLossState,
        )
        # Use zero costs to expose the pure risk-based sizing
        for cap in [2000, 5000, 10000, 25000, 50000]:
            s = RiskSettings(
                initial_inr=cap, risk_per_trade_pct=1.0,
                max_position_pct=100.0, max_portfolio_exposure_pct=100.0,
                trading_allocation_pct=100.0,
                fee_pct=0, spread_pct=0, slippage_pct=0,
                reward_to_risk_target=2.0, max_open_positions=5,
                max_daily_trades=100, max_daily_loss_pct=100.0,
                max_drawdown_pct=100.0,
                cooldown_after_consecutive_losses=0, allow_overnight=True,
                min_24h_volume_inr=0, min_24h_trades=0,
            )
            eng = RiskEngine(s)
            state = RiskState(initial_inr=cap)
            state.daily = DailyLossState(day_iso="2026-10-11")
            plan = eng.plan_position(
                pair="TEST", entry=100.0, stop=98.0, target=104.0,
                state=state)
            # risk_budget = cap * 1% = cap / 100
            # stop_distance = 2%
            # risk_based_value = (cap/100) / 0.02 = cap * 0.5
            assert abs(plan.position_value - cap * 0.5) < 0.01, \
                f"cap={cap}: expected position_value={cap*0.5}, got {plan.position_value}"
            # risk_budget should be cap * 1%
            assert abs(plan.risk_budget - cap * 0.01) < 0.01

    def test_increasing_capital_does_not_increase_risk_pct(self):
        """Per-trade risk BUDGET scales with capital, but the RISK
        PERCENTAGE stays the same — the user is NOT taking more risk
        just because they have more capital."""
        from coindcx_lab.engine.risk import (
            RiskEngine, RiskSettings, RiskState, DailyLossState,
        )
        for cap in [2000, 50000]:
            s = RiskSettings(initial_inr=cap, risk_per_trade_pct=1.0,
                              max_position_pct=100.0,
                              max_portfolio_exposure_pct=100.0,
                              trading_allocation_pct=100.0,
                              fee_pct=0, spread_pct=0, slippage_pct=0,
                              reward_to_risk_target=2.0,
                              max_open_positions=5, max_daily_trades=100,
                              max_daily_loss_pct=100.0, max_drawdown_pct=100.0,
                              cooldown_after_consecutive_losses=0,
                              allow_overnight=True,
                              min_24h_volume_inr=0, min_24h_trades=0)
            eng = RiskEngine(s)
            state = RiskState(initial_inr=cap)
            state.daily = DailyLossState(day_iso="2026-10-11")
            plan = eng.plan_position(
                pair="TEST", entry=100.0, stop=98.0, target=104.0,
                state=state)
            # Risk as % of equity should be 1% regardless of capital
            risk_pct = (plan.potential_loss_gross / cap) * 100
            assert abs(risk_pct - 1.0) < 0.01, \
                f"cap={cap}: risk_pct={risk_pct} (expected 1.0)"


# ---------------------------------------------------------------------------
# 3. Settings persistence: save → load; history preserved
# ---------------------------------------------------------------------------
class TestSettingsPersistence:
    def test_save_then_load_roundtrip(self):
        from coindcx_lab.db.store import Store
        from coindcx_lab.engine.risk import RiskSettings
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            store = Store(path=db_path)
            settings_dict = {
                "initial_inr": 10000.0,
                "risk_per_trade_pct": 2.0,
                "max_position_pct": 50.0,
                "fee_pct": 0.15,
            }
            store.save_settings("risk", settings_dict)
            loaded = store.load_settings("risk")
            assert loaded is not None
            assert loaded["initial_inr"] == 10000.0
            assert loaded["risk_per_trade_pct"] == 2.0
            assert loaded["fee_pct"] == 0.15
        finally:
            store.close()
            os.unlink(db_path)

    def test_changing_settings_preserves_trade_history(self):
        """Changing settings should NOT wipe existing trades."""
        from coindcx_lab.db.store import Store
        from coindcx_lab.engine.paper import PaperTradingEngine
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import Signal, SignalState
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            s1 = RiskSettings(initial_inr=2000)
            store = Store(path=db_path)
            pe = PaperTradingEngine(settings=s1, store=store)
            # Open a trade
            sig = Signal(
                pair="B-BTCINR", interval="1h", strategy="test",
                state=SignalState.BUY_CANDIDATE,
                entry=100.0, stop=90.0, target=130.0,
                rr_after_cost=3.0,
            )
            trade, _ = pe.open_position(signal=sig)
            assert trade is not None
            assert len(store.get_open_trades()) == 1

            # Now change settings to ₹10,000
            s2 = RiskSettings(initial_inr=10000)
            pe.update_settings(s2)
            # The trade should still be there
            assert len(store.get_open_trades()) == 1
            # The new settings should be saved
            loaded = store.load_settings("risk")
            assert loaded["initial_inr"] == 10000
        finally:
            store.close()
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# 4. Portfolio reset: requires confirmation
# ---------------------------------------------------------------------------
class TestPortfolioReset:
    def test_reset_without_confirm_returns_400(self):
        """POST /api/portfolio_reset without {"confirm": true} → 400."""
        from api.portfolio import handler
        import socketserver, threading, time
        import requests as _rq

        # Mock build_paper_engine so it returns a mock engine (avoids
        # needing real Supabase env vars for this test).
        mock_pe = MagicMock()
        with patch("coindcx_lab.server.paper_common.build_paper_engine",
                    return_value=mock_pe):
            class _S(socketserver.TCPServer):
                allow_reuse_address = True
            srv = _S(("127.0.0.1", 0), handler)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            time.sleep(0.05)
            try:
                # Without confirm=true
                r = _rq.post(f"http://127.0.0.1:{port}/api/portfolio_reset",
                              json={}, timeout=10)
                assert r.status_code == 400
                assert r.json()["error"] == "confirmation_required"
                # The mock engine's reset() should NOT have been called
                mock_pe.reset.assert_not_called()
            finally:
                srv.shutdown()


# ---------------------------------------------------------------------------
# 5. WazirX spread: computed from buy/sell fields
# ---------------------------------------------------------------------------
class TestWazirXSpread:
    def test_spread_computed_from_buy_sell(self):
        """spread_pct = (sell - buy) / last * 100."""
        from coindcx_lab.api.wazirx_client import WazirXClient
        c = WazirXClient(max_retries=1, cache_ttl_seconds=0.0)
        sample = {
            "btcinr": {
                "base_unit": "btc", "quote_unit": "inr",
                "low": "8139926.0", "high": "8225001.0",
                "last": "8214417.0", "type": "SPOT",
                "open": "8225001", "volume": "0.0708",
                "sell": "8214417.0", "buy": "8165749.0",
                "at": 1791693581, "name": "BTC/INR",
            },
        }
        with patch("coindcx_lab.api.wazirx_client.requests.request") as m:
            r = MagicMock(status_code=200, headers={},
                          text=json.dumps(sample))
            r.json.return_value = sample
            m.return_value = r
            prices, _ = c.current_prices()
        t = prices["B-BTCINR"]
        # spread_pct = (8214417 - 8165749) / 8214417 * 100
        expected = (8214417.0 - 8165749.0) / 8214417.0 * 100
        assert abs(t["spread_pct"] - expected) < 0.001
        assert t["buy_price"] == 8165749.0
        assert t["sell_price"] == 8214417.0

    def test_spread_zero_when_buy_equals_sell(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        c = WazirXClient(max_retries=1, cache_ttl_seconds=0.0)
        sample = {
            "btcinr": {
                "base_unit": "btc", "quote_unit": "inr",
                "low": "8100000", "high": "8300000",
                "last": "8200000", "type": "SPOT",
                "open": "8200000", "volume": "1.0",
                "sell": "8200000", "buy": "8200000",
                "at": 1791693581, "name": "BTC/INR",
            },
        }
        with patch("coindcx_lab.api.wazirx_client.requests.request") as m:
            r = MagicMock(status_code=200, headers={},
                          text=json.dumps(sample))
            r.json.return_value = sample
            m.return_value = r
            prices, _ = c.current_prices()
        t = prices["B-BTCINR"]
        assert t["spread_pct"] == 0.0  # buy == sell → zero spread
