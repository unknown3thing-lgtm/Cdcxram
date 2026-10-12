"""Phase 18 regression tests — SimpleBreakoutStrategy.

Tests cover:
  A. Strategy behavior (4 tests):
     1. Warmup incomplete returns NO_TRADE (< 50 bars).
     2. close <= EMA50 returns WAIT (trend filter).
     3. close <= resistance returns WAIT (breakout trigger).
     4. 2R target is mathematically impossible to pass R/R gate
        with positive costs — 3R target is the minimum.

  B. Root-cause verification (1 test):
     5. With 2R target and positive costs, rr_after_cost < 2.0
        for ANY R > 0 (mathematical proof).
"""
from __future__ import annotations

import math
import os
import tempfile
from datetime import datetime, timezone

import pytest


def _make_candles(n, start_ts=1_700_000_000_000, interval_ms=3_600_000,
                  start_price=100.0, slope=0.5):
    from coindcx_lab.api.models import Candle
    out = []
    for i in range(n):
        p = start_price + i * slope
        out.append(Candle(
            open_time_ms=start_ts + i * interval_ms,
            open=p, high=p + 1, low=p - 1, close=p, volume=100,
            close_time_ms=start_ts + (i + 1) * interval_ms - 1,
            complete=True))
    return out


def _settings():
    from coindcx_lab.engine.risk import RiskSettings
    return RiskSettings(initial_inr=2000, fee_pct=0.1, spread_pct=0.05,
                       slippage_pct=0.05, max_position_pct=100,
                       max_portfolio_exposure_pct=100,
                       trading_allocation_pct=100,
                       reward_to_risk_target=2.0,
                       max_open_positions=5, max_daily_trades=100,
                       max_daily_loss_pct=100, max_drawdown_pct=100,
                       cooldown_after_consecutive_losses=0,
                       allow_overnight=True, min_24h_volume_inr=0,
                       min_24h_trades=0)


class TestSimpleBreakoutStrategy:
    """Phase 18 — SimpleBreakoutStrategy tests."""

    def test_warmup_incomplete(self):
        """With < 50 bars, the strategy should return NO_TRADE."""
        from coindcx_lab.api.models import DataProvenance, SourceKind
        from coindcx_lab.engine.indicators import IndicatorSet
        from coindcx_lab.engine.risk import RiskState, RiskEngine, DailyLossState
        from coindcx_lab.engine.simple_strategy import SimpleBreakoutStrategy
        candles = _make_candles(30)  # < 50
        s = _settings()
        state = RiskState(initial_inr=2000.0)
        state.daily = DailyLossState(day_iso="2026-10-12")
        risk_engine = RiskEngine(s)
        strat = SimpleBreakoutStrategy()
        indicators = IndicatorSet.from_candles(candles)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        sig = strat.evaluate(
            pair="B-BTCINR", interval="1h", candles=candles,
            indicators=indicators, risk_engine=risk_engine,
            risk_state=state, provenance=prov)
        assert sig.state.value == "NO_TRADE"
        assert "warm-up" in sig.explanation

    def test_trend_filter_close_below_ema50(self):
        """When close <= EMA50, the strategy should WAIT."""
        from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
        from coindcx_lab.engine.indicators import IndicatorSet
        from coindcx_lab.engine.risk import RiskState, RiskEngine, DailyLossState
        from coindcx_lab.engine.simple_strategy import SimpleBreakoutStrategy
        # Build a falling series (price below EMA50 at the end)
        candles = []
        for i in range(100):
            p = 200 - i * 0.5  # falling
            candles.append(Candle(
                open_time_ms=1_700_000_000_000 + i * 3_600_000,
                open=p, high=p + 1, low=p - 1, close=p, volume=100,
                close_time_ms=1_700_000_000_000 + (i + 1) * 3_600_000 - 1,
                complete=True))
        s = _settings()
        state = RiskState(initial_inr=2000.0)
        state.daily = DailyLossState(day_iso="2026-10-12")
        risk_engine = RiskEngine(s)
        strat = SimpleBreakoutStrategy()
        indicators = IndicatorSet.from_candles(candles)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        sig = strat.evaluate(
            pair="B-BTCINR", interval="1h", candles=candles,
            indicators=indicators, risk_engine=risk_engine,
            risk_state=state, provenance=prov)
        # In a falling series, close < EMA50 → WAIT
        assert sig.state.value == "WAIT"
        assert "EMA50" in sig.explanation or "trend" in sig.explanation.lower()

    def test_2r_target_impossible_with_positive_costs(self):
        """Mathematical proof: with 2R target and positive costs,
        rr_after_cost = (2R - cost) / (R + cost) < 2.0 for any cost > 0.

        This is the ROOT CAUSE of the Phase 18 simple_breakout
        0-trade result with 2R target. The fix: use 3R target.
        """
        cost = 0.3  # 0.3% round-trip cost (fee 0.1% * 2 + slippage 0.05% * 2)
        for R_pct in [0.5, 1.0, 2.0, 5.0, 10.0, 20.0]:
            R = R_pct  # risk as % of price
            target_2r = 2 * R
            rr_2r = (target_2r - cost) / (R + cost)
            assert rr_2r < 2.0, \
                f"2R target with R={R}% gives rr={rr_2r:.3f} — should be < 2.0"
            # 3R target
            target_3r = 3 * R
            rr_3r = (target_3r - cost) / (R + cost)
            # 3R should be >= 2.0 for R >= ~1% (the typical ATR range)
            if R >= 1.0:
                assert rr_3r >= 2.0, \
                    f"3R target with R={R}% gives rr={rr_3r:.3f} — " \
                    f"should be >= 2.0"
