"""Phase 17 regression tests — indicator optimization equivalence
and performance.

Tests cover:
  A. Numerical equivalence (5 tests):
     1. at_bar(i) EMA values match from_candles(prefix).
     2. at_bar(i) RSI/ATR/MACD/ADX/BB values match.
     3. at_bar(i) +DI/-DI values match.
     4. at_bar(i) swing_highs_lows match (truncated, no look-ahead).
     5. at_bar(i) breakout_retest_signal matches (recomputed on prefix).

  B. No look-ahead bias (2 tests):
     6. at_bar(i) values don't change when future candles are added.
     7. at_bar(i) swing_lows don't include future pivots.

  C. Performance (1 test):
     8. at_bar() is significantly faster than from_candles(prefix).

  D. SignalEngine indicators parameter (1 test):
     9. SignalEngine.evaluate(indicators=...) uses the passed indicators.
"""
from __future__ import annotations

import math
import os
import tempfile
import time
from datetime import datetime, timezone
from typing import List

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


# ===========================================================================
# A. Numerical equivalence
# ===========================================================================
class TestIndicatorEquivalence:
    """Phase 17 — verify at_bar() produces the same values as
    from_candles(prefix)."""

    def test_ema_values_match(self):
        """EMA values at bar i via at_bar() should match EMA values
        computed via from_candles(candles[:i+1])."""
        from coindcx_lab.engine.indicators import IndicatorSet
        candles = _make_candles(300, slope=0.5)
        full = IndicatorSet.from_candles(candles)
        # Check at bars 200, 250, 299
        for i in [200, 250, 299]:
            prefix_ind = IndicatorSet.from_candles(candles[:i + 1])
            cached_ind = full.at_bar(candles, i)
            # EMA20, EMA50, EMA100, EMA200
            for name in ("ema20", "ema50", "ema100", "ema200"):
                v1 = getattr(prefix_ind, name)[i] if i < len(getattr(prefix_ind, name)) else float("nan")
                v2 = getattr(cached_ind, name)[i]
                if math.isnan(v1) and math.isnan(v2):
                    continue
                assert abs(v1 - v2) < 1e-9, \
                    f"{name}[{i}]: from_candles={v1}, at_bar={v2}, diff={abs(v1-v2)}"

    def test_rsi_atr_macd_adx_bb_match(self):
        """RSI, ATR, MACD, ADX, BB values at bar i via at_bar()
        should match from_candles(prefix)."""
        from coindcx_lab.engine.indicators import IndicatorSet
        candles = _make_candles(300, slope=0.5)
        full = IndicatorSet.from_candles(candles)
        for i in [200, 250, 299]:
            prefix_ind = IndicatorSet.from_candles(candles[:i + 1])
            cached_ind = full.at_bar(candles, i)
            for name in ("rsi14", "atr14", "macd_line", "macd_signal",
                          "macd_hist", "adx14", "bb_middle", "bb_upper",
                          "bb_lower", "relvol20", "vol20",
                          "plus_di14", "minus_di14"):
                v1 = getattr(prefix_ind, name)[i]
                v2 = getattr(cached_ind, name)[i]
                if math.isnan(v1) and math.isnan(v2):
                    continue
                assert abs(v1 - v2) < 1e-9, \
                    f"{name}[{i}]: from_candles={v1}, at_bar={v2}"

    def test_swing_highs_lows_match(self):
        """swing_highs_lows via at_bar() should match the values
        computed via from_candles(prefix) — specifically, the
        truncated swing lists should have the same non-None values
        up to index i-2 (the last lookback=2 entries should be None
        in both)."""
        from coindcx_lab.engine.indicators import IndicatorSet
        candles = _make_candles(300, slope=0.5)
        full = IndicatorSet.from_candles(candles)
        i = 250
        prefix_ind = IndicatorSet.from_candles(candles[:i + 1])
        cached_ind = full.at_bar(candles, i)
        # Compare swing_lows — both should have the same non-None values
        # at indices 2..i-2 (the last 2 should be None in both).
        prefix_sl = prefix_ind.swing_lows
        cached_sl = cached_ind.swing_lows
        # Both should have the same length (i+1)
        assert len(cached_sl) == i + 1, \
            f"cached swing_lows length={len(cached_sl)}, expected {i+1}"
        # Compare non-None entries
        for j in range(i + 1):
            v1 = prefix_sl[j] if j < len(prefix_sl) else None
            v2 = cached_sl[j]
            if v1 is None and v2 is None:
                continue
            if v1 is None or v2 is None:
                # The last 2 entries should be None in both
                assert j >= i - 1, \
                    f"swing_lows[{j}]: prefix={v1}, cached={v2} " \
                    f"(only last 2 should differ)"
            else:
                assert abs(v1 - v2) < 1e-9, \
                    f"swing_lows[{j}]: prefix={v1}, cached={v2}"

    def test_breakout_retest_matches(self):
        """breakout_retest_signal via at_bar() should match the value
        computed via from_candles(prefix)."""
        from coindcx_lab.engine.indicators import IndicatorSet
        candles = _make_candles(300, slope=0.5)
        full = IndicatorSet.from_candles(candles)
        for i in [200, 250, 299]:
            prefix_ind = IndicatorSet.from_candles(candles[:i + 1])
            cached_ind = full.at_bar(candles, i)
            br1 = prefix_ind.breakout_retest_signal
            br2 = cached_ind.breakout_retest_signal
            assert br1.has_breakout == br2.has_breakout, \
                f"breakout has_breakout mismatch at bar {i}"
            assert br1.direction == br2.direction, \
                f"breakout direction mismatch at bar {i}"
            assert br1.has_retest == br2.has_retest, \
                f"breakout has_retest mismatch at bar {i}"

    def test_support_resistance_match(self):
        """support_resistance via at_bar() should match the values
        computed via from_candles(prefix)."""
        from coindcx_lab.engine.indicators import IndicatorSet
        candles = _make_candles(300, slope=0.5)
        full = IndicatorSet.from_candles(candles)
        i = 250
        prefix_ind = IndicatorSet.from_candles(candles[:i + 1])
        cached_ind = full.at_bar(candles, i)
        # Compare support/resistance levels
        res1 = prefix_ind.resistance_levels
        res2 = cached_ind.resistance_levels
        sup1 = prefix_ind.support_levels
        sup2 = cached_ind.support_levels
        # Same length
        assert len(res1) == len(res2), \
            f"resistance_levels length: prefix={len(res1)}, cached={len(res2)}"
        assert len(sup1) == len(sup2), \
            f"support_levels length: prefix={len(sup1)}, cached={len(sup2)}"
        # Same values (within tolerance)
        for r1, r2 in zip(res1, res2):
            assert abs(r1 - r2) < 1e-9, \
                f"resistance_levels: prefix={r1}, cached={r2}"
        for s1, s2 in zip(sup1, sup2):
            assert abs(s1 - s2) < 1e-9, \
                f"support_levels: prefix={s1}, cached={s2}"


# ===========================================================================
# B. No look-ahead bias
# ===========================================================================
class TestNoLookAheadOptimized:
    """Phase 17 — verify at_bar() has no look-ahead bias."""

    def test_values_unchanged_when_future_candles_added(self):
        """Indicator values at bar i should NOT change when future
        candles are added to the series."""
        from coindcx_lab.engine.indicators import IndicatorSet
        candles = _make_candles(250, slope=0.5)
        full1 = IndicatorSet.from_candles(candles)
        ind1 = full1.at_bar(candles, 200)
        # Extend the series with 50 more candles
        extra = _make_candles(50, start_ts=candles[-1].open_time_ms + 3_600_000,
                               start_price=candles[-1].close, slope=0.5)
        extended = candles + extra
        full2 = IndicatorSet.from_candles(extended)
        ind2 = full2.at_bar(extended, 200)
        # EMA values at index 200 should be the same
        for name in ("ema20", "ema50", "ema100", "ema200",
                      "rsi14", "atr14", "adx14"):
            v1 = getattr(ind1, name)[200]
            v2 = getattr(ind2, name)[200]
            if math.isnan(v1) and math.isnan(v2):
                continue
            assert abs(v1 - v2) < 1e-9, \
                f"{name}[200] changed when future candles added: {v1} -> {v2}"

    def test_swing_lows_no_future_pivots(self):
        """swing_lows via at_bar(i) should NOT include pivots at
        indices > i (future pivots)."""
        from coindcx_lab.engine.indicators import IndicatorSet
        candles = _make_candles(300, slope=0.5)
        full = IndicatorSet.from_candles(candles)
        i = 250
        cached_ind = full.at_bar(candles, i)
        # The swing_lows list should have length i+1 (251), not 300.
        assert len(cached_ind.swing_lows) == i + 1, \
            f"swing_lows length={len(cached_ind.swing_lows)}, expected {i+1}"
        # The last 2 entries (indices 249, 250) should be None
        # (unconfirmed pivots that need future bars).
        assert cached_ind.swing_lows[-1] is None, \
            "last swing_low should be None (unconfirmed)"
        assert cached_ind.swing_lows[-2] is None, \
            "second-to-last swing_low should be None (unconfirmed)"


# ===========================================================================
# C. Performance
# ===========================================================================
class TestPerformance:
    """Phase 17 — verify at_bar() is faster than from_candles(prefix)."""

    def test_at_bar_faster_than_from_candles(self):
        """at_bar() should be significantly faster than
        from_candles(prefix) for the same bar count."""
        from coindcx_lab.engine.indicators import IndicatorSet
        candles = _make_candles(500, slope=0.5)
        # Old path: from_candles at each bar
        t0 = time.time()
        for i in range(200, 500):
            IndicatorSet.from_candles(candles[:i + 1])
        t_old = time.time() - t0
        # New path: pre-compute once, at_bar at each bar
        full = IndicatorSet.from_candles(candles)
        t0 = time.time()
        for i in range(200, 500):
            full.at_bar(candles, i)
        t_new = time.time() - t0
        # New should be at least 5x faster
        assert t_new < t_old / 5, \
            f"at_bar ({t_new*1000:.1f}ms) should be >5x faster than " \
            f"from_candles ({t_old*1000:.1f}ms), got {t_old/t_new:.1f}x"


# ===========================================================================
# D. SignalEngine indicators parameter
# ===========================================================================
class TestSignalEngineIndicatorsParam:
    """Phase 17 — verify SignalEngine.evaluate(indicators=...) uses
    the passed indicators."""

    def test_indicators_parameter_used(self):
        """When indicators= is passed, SignalEngine.evaluate() should
        use those indicators instead of calling from_candles()."""
        from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
        from coindcx_lab.engine.indicators import IndicatorSet
        from coindcx_lab.engine.risk import RiskSettings, RiskState, RiskEngine, DailyLossState
        from coindcx_lab.engine.signals import BaselineSMAStrategy, SignalEngine
        candles = _make_candles(250, slope=0.5)
        s = RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                         slippage_pct=0, max_position_pct=100,
                         max_portfolio_exposure_pct=100,
                         trading_allocation_pct=100,
                         reward_to_risk_target=1.5,
                         max_open_positions=5, max_daily_trades=100,
                         max_daily_loss_pct=100, max_drawdown_pct=100,
                         cooldown_after_consecutive_losses=0,
                         allow_overnight=True, min_24h_volume_inr=0,
                         min_24h_trades=0)
        eng = SignalEngine(settings=s, strategies=[BaselineSMAStrategy()])
        state = RiskState(initial_inr=2000.0)
        state.daily = DailyLossState(day_iso="2026-10-12")
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        # Pre-compute indicators
        full_ind = IndicatorSet.from_candles(candles)
        cached_ind = full_ind.at_bar(candles, 249)
        # Evaluate with pre-computed indicators
        sigs1 = eng.evaluate(pair="B-BTCINR", interval="1h",
                              candles=candles, provenance=prov,
                              risk_state=state, indicators=cached_ind)
        # Evaluate without pre-computed indicators (old path)
        sigs2 = eng.evaluate(pair="B-BTCINR", interval="1h",
                              candles=candles, provenance=prov,
                              risk_state=state)
        # The signal state should be the same (both use the same indicator
        # values at the last bar).
        assert sigs1[0].state == sigs2[0].state, \
            f"signal state differs: cached={sigs1[0].state.value}, " \
            f"from_scratch={sigs2[0].state.value}"
