"""Tests: signal engine & strategies.

Synthetic candle series with KNOWN structural properties drive these
tests so assertions can be exact.
"""

from __future__ import annotations

import math

import pytest

from coindcx_lab.api.models import Candle
from coindcx_lab.engine.risk import RiskSettings, RiskState, DailyLossState
from coindcx_lab.engine.signals import (
    BaselineSMAStrategy, BreakoutRetestStrategy, MomentumContinuationStrategy,
    SignalEngine, SignalState, TrendPullbackStrategy,
)
from coindcx_lab.engine.indicators import IndicatorSet


def _state_for(s: RiskSettings) -> RiskState:
    st = RiskState(initial_inr=s.initial_inr)
    st.daily = DailyLossState(day_iso="2024-01-01")
    return st


# ---------------------------------------------------------------------------
# Trend pullback
# ---------------------------------------------------------------------------
class TestTrendPullback:
    def test_no_signal_when_warmup_incomplete(self, default_settings,
                                              provenance_sim):
        s = default_settings
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = SignalEngine(settings=s,
                           strategies=[TrendPullbackStrategy()])
        # Only 50 candles; warmup is 200
        cs = [Candle(open_time_ms=i*3_600_000, open=100, high=101,
                     low=99, close=100, volume=1,
                     close_time_ms=(i+1)*3_600_000-1)
              for i in range(50)]
        sigs = eng.evaluate(pair="B-BTCINR", interval="1h", candles=cs,
                            provenance=provenance_sim,
                            risk_state=_state_for(s))
        assert len(sigs) == 1
        assert sigs[0].state in (SignalState.NO_TRADE, SignalState.WAIT)

    def test_emits_buy_when_trend_up_and_pullback(self, default_settings,
                                                    provenance_sim):
        s = default_settings
        # Loosen caps so we get a clear BUY_CANDIDATE
        s.max_position_pct = 100.0
        s.max_portfolio_exposure_pct = 100.0
        s.trading_allocation_pct = 100.0
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = SignalEngine(settings=s,
                           strategies=[TrendPullbackStrategy()])

        # Build a series with strong up trend then a pullback at the end.
        cs = []
        base_ts = 1_700_000_000_000
        for i in range(220):
            # Trend up for 210 bars
            close = 100 * (1.005 ** i)
            cs.append(Candle(open_time_ms=base_ts + i*3_600_000,
                             open=close, high=close+1, low=close-1,
                             close=close, volume=100,
                             close_time_ms=base_ts + (i+1)*3_600_000 - 1,
                             complete=True))
        # Pullback: 10 bars down
        last_close = cs[-1].close
        for j in range(10):
            close = last_close * (0.997 ** (j + 1))
            cs.append(Candle(open_time_ms=base_ts + (220+j)*3_600_000,
                             open=close, high=close+1, low=close-1,
                             close=close, volume=120,
                             close_time_ms=base_ts + (221+j)*3_600_000 - 1,
                             complete=True))
        sigs = eng.evaluate(pair="B-BTCINR", interval="1h", candles=cs,
                            provenance=provenance_sim,
                            risk_state=_state_for(s))
        assert any(s.state is SignalState.BUY_CANDIDATE for s in sigs)


# ---------------------------------------------------------------------------
# Baseline SMA
# ---------------------------------------------------------------------------
class TestBaselineSMA:
    def test_no_signal_when_close_below_ema50(self, default_settings,
                                                provenance_sim, up_trend_candles):
        s = default_settings
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = SignalEngine(settings=s,
                           strategies=[BaselineSMAStrategy()])
        # Replace the last 20 bars with a sharp down move so the close
        # drops below the EMA50. Build candles with valid OHLC geometry
        # (low <= min(O,C), high >= max(O,C)).
        cs = list(up_trend_candles[:-20])
        last_close = cs[-1].close
        # 20 bars of sharp decline
        for j in range(20):
            o = last_close
            c = last_close * 0.96   # 4% drop per bar
            cs.append(Candle(open_time_ms=cs[-1].open_time_ms + 3_600_000,
                             open=o, high=max(o, c) + 0.5, low=min(o, c) - 0.5,
                             close=c, volume=200,
                             close_time_ms=cs[-1].open_time_ms + 2*3_600_000 - 1,
                             complete=True))
            last_close = c
        sigs = eng.evaluate(pair="B-BTCINR", interval="1h", candles=cs,
                            provenance=provenance_sim,
                            risk_state=_state_for(s))
        assert all(s.state is SignalState.WAIT or
                   s.state is SignalState.NO_TRADE for s in sigs)

    def test_emits_buy_when_close_above_rising_ema50(self, default_settings,
                                                       provenance_sim,
                                                       up_trend_candles):
        s = default_settings
        s.max_position_pct = 100.0
        s.max_portfolio_exposure_pct = 100.0
        s.trading_allocation_pct = 100.0
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = SignalEngine(settings=s,
                           strategies=[BaselineSMAStrategy()])
        sigs = eng.evaluate(pair="B-BTCINR", interval="1h",
                            candles=up_trend_candles,
                            provenance=provenance_sim,
                            risk_state=_state_for(s))
        assert any(s.state is SignalState.BUY_CANDIDATE for s in sigs)


# ---------------------------------------------------------------------------
# Breakout & retest
# ---------------------------------------------------------------------------
class TestBreakoutRetest:
    def test_no_signal_without_breakout(self, default_settings,
                                         provenance_sim, flat_candles):
        s = default_settings
        eng = SignalEngine(settings=s,
                           strategies=[BreakoutRetestStrategy()])
        sigs = eng.evaluate(pair="B-BTCINR", interval="1h",
                            candles=flat_candles,
                            provenance=provenance_sim,
                            risk_state=_state_for(s))
        # No breakout in a flat series -> WAIT
        assert all(s.state is SignalState.WAIT or
                   s.state is SignalState.NO_TRADE for s in sigs)

    def test_emits_buy_after_breakout_with_retest(self, default_settings,
                                                    provenance_sim):
        s = default_settings
        s.max_position_pct = 100.0
        s.max_portfolio_exposure_pct = 100.0
        s.trading_allocation_pct = 100.0
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = SignalEngine(settings=s,
                           strategies=[BreakoutRetestStrategy()])
        # Build 100 bars: first 80 around 100, then push to 110 over 5 bars
        # (breakout), then pullback to 109 (retest), then resume.
        cs = []
        base_ts = 1_700_000_000_000
        for i in range(80):
            cs.append(Candle(open_time_ms=base_ts + i*3_600_000,
                             open=100, high=101, low=99, close=100,
                             volume=10,
                             close_time_ms=base_ts + (i+1)*3_600_000 - 1))
        # Breakout up: bars 80..84 close at 102..110. Build each bar so that
        # OHLC geometry is valid (low <= min(open, close); high >= max(open, close)).
        prev_close = 100.0
        for j, c in enumerate([102, 104, 106, 108, 110]):
            i = 80 + j
            o = prev_close
            cs.append(Candle(open_time_ms=base_ts + i*3_600_000,
                             open=o, high=max(o, c) + 1, low=min(o, c) - 1,
                             close=c, volume=30,
                             close_time_ms=base_ts + (i+1)*3_600_000 - 1))
            prev_close = c
        # Retest: pullback to ~109.5 (just above breakout level 101)
        for j, c in enumerate([108, 109, 109.5, 109.8, 110]):
            i = 85 + j
            o = prev_close
            cs.append(Candle(open_time_ms=base_ts + i*3_600_000,
                             open=o, high=max(o, c) + 0.5, low=min(o, c) - 0.5,
                             close=c, volume=15,
                             close_time_ms=base_ts + (i+1)*3_600_000 - 1))
            prev_close = c
        sigs = eng.evaluate(pair="B-BTCINR", interval="1h", candles=cs,
                            provenance=provenance_sim,
                            risk_state=_state_for(s))
        # Should detect a breakout+retest, but the BUY_CANDIDATE flag
        # depends on cost-adjusted R-R; with 0 costs it should be ACCEPTED.
        br_sigs = [s for s in sigs if s.strategy == "breakout_retest"]
        assert br_sigs
        # Either BUY_CANDIDATE (R-R ok) or NO_TRADE (R-R rejected). With 0
        # costs and 2R target, it should be BUY_CANDIDATE.
        assert br_sigs[0].state is SignalState.BUY_CANDIDATE


# ---------------------------------------------------------------------------
# Disabled strategy
# ---------------------------------------------------------------------------
class TestEnabledFlag:
    def test_disabled_strategy_not_run(self, default_settings,
                                         provenance_sim, up_trend_candles):
        from coindcx_lab.engine.signals import SignalEngineConfig
        s = default_settings
        eng = SignalEngine(settings=s, strategies=[TrendPullbackStrategy(),
                                                   BaselineSMAStrategy()],
                            config=SignalEngineConfig(enabled={
                                "trend_pullback": False,
                                "baseline_sma": True,
                            }))
        sigs = eng.evaluate(pair="B-BTCINR", interval="1h",
                            candles=up_trend_candles,
                            provenance=provenance_sim,
                            risk_state=_state_for(s))
        strategies_run = [s.strategy for s in sigs]
        assert "trend_pullback" not in strategies_run
        assert "baseline_sma" in strategies_run


# ---------------------------------------------------------------------------
# Empty candles
# ---------------------------------------------------------------------------
class TestEmptyInput:
    def test_returns_no_trade_on_empty(self, default_settings,
                                        provenance_sim):
        s = default_settings
        eng = SignalEngine(settings=s)
        sigs = eng.evaluate(pair="X", interval="1h", candles=[],
                            provenance=provenance_sim,
                            risk_state=_state_for(s))
        assert len(sigs) == 1
        assert sigs[0].state is SignalState.NO_TRADE
        assert any("no candles" in w for w in sigs[0].data_quality_warnings)
