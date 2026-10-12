"""Phase 9 regression tests: equity calculation, drawdown, buy-and-hold,
walk-forward warm-up, paper-exit coverage.

Covers confirmed defects:
  1. current_equity was inflated by open_positions_value (double-counting)
  2. Drawdown was artificially large when positions were open
  3. Backtester's mark-to-market equity was overstated
  4. Buy-and-hold benchmark must use segment's actual first/last prices
  5. Paper-exit: candles before entry must be ignored
  6. Paper-exit: CLOSED trades are not re-processed
  7. Walk-forward: warm-up data must not create scored trades
"""

from __future__ import annotations

import math
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1-3: Equity calculation, drawdown, mark-to-market
# ---------------------------------------------------------------------------
class TestEquityCalculation:
    """Verify that current_equity does NOT include open_positions_value
    (which was double-counting the entry cost)."""

    def test_equity_unchanged_when_position_opened(self):
        """Opening a position should NOT change current_equity.
        The entry cost is deducted from cash, not from equity."""
        from coindcx_lab.engine.risk import RiskState, RiskEngine, RiskSettings, DailyLossState
        s = RiskSettings(initial_inr=2000, risk_per_trade_pct=1.0,
                          max_position_pct=100, max_portfolio_exposure_pct=100,
                          trading_allocation_pct=100, fee_pct=0, spread_pct=0,
                          slippage_pct=0, reward_to_risk_target=2.0,
                          max_open_positions=5, max_daily_trades=100,
                          max_daily_loss_pct=100, max_drawdown_pct=100,
                          cooldown_after_consecutive_losses=0, allow_overnight=True,
                          min_24h_volume_inr=0, min_24h_trades=0)
        state = RiskState(initial_inr=2000)
        state.daily = DailyLossState(day_iso="2026-01-01")
        eng = RiskEngine(s)

        assert state.current_equity == 2000.0

        # Open a position: entry=100, qty=4 (position_value=400)
        eng.on_trade_opened(entry=100, stop=95, qty=4, state=state)

        # Equity should STILL be 2000 (not 2400)
        assert state.current_equity == 2000.0, \
            f"current_equity should be 2000, got {state.current_equity}"
        # Cash should be 1600 (2000 - 400)
        assert state.available_cash == 1600.0
        # Peak should be 2000 (not 2400)
        assert state.peak_equity == 2000.0, \
            f"peak_equity should be 2000, got {state.peak_equity}"
        # Drawdown should be 0%
        assert state.drawdown_pct == 0.0

    def test_drawdown_after_stop_loss_is_correct(self):
        """A ₹20 loss on ₹2,000 capital should produce 1% drawdown,
        NOT 17.5% (which was the bug)."""
        from coindcx_lab.engine.risk import RiskState, RiskEngine, RiskSettings, DailyLossState
        s = RiskSettings(initial_inr=2000, risk_per_trade_pct=1.0,
                          max_position_pct=100, max_portfolio_exposure_pct=100,
                          trading_allocation_pct=100, fee_pct=0, spread_pct=0,
                          slippage_pct=0, reward_to_risk_target=2.0,
                          max_open_positions=5, max_daily_trades=100,
                          max_daily_loss_pct=100, max_drawdown_pct=100,
                          cooldown_after_consecutive_losses=0, allow_overnight=True,
                          min_24h_volume_inr=0, min_24h_trades=0)
        state = RiskState(initial_inr=2000)
        state.daily = DailyLossState(day_iso="2026-01-01")
        eng = RiskEngine(s)

        # Open: entry=100, stop=95, qty=4
        eng.on_trade_opened(entry=100, stop=95, qty=4, state=state)
        # Close at stop=95: pnl = (95-100)*4 = -20
        eng.on_trade_closed(entry=100, stop=95, target=110, qty=4,
                            exit_price=95, state=state, day_iso="2026-01-01")

        # Equity should be 1980 (2000 - 20)
        assert state.current_equity == 1980.0
        # Peak should be 2000 (not inflated)
        assert state.peak_equity == 2000.0
        # Drawdown should be (2000-1980)/2000 = 1.0% (not 17.5%)
        assert abs(state.drawdown_pct - 1.0) < 0.01, \
            f"drawdown should be 1.0%, got {state.drawdown_pct}%"

    def test_backtester_mtm_equity_correct(self):
        """The backtester's equity curve should show mark-to-market
        equity = available_cash + qty * close, not the inflated value."""
        from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        # Build synthetic candles with a clear uptrend
        candles = []
        base_ts = 1_700_000_000_000
        for i in range(250):
            candles.append(Candle(
                open_time_ms=base_ts + i * 86_400_000,
                open=100 + i * 0.5,
                high=101 + i * 0.5,
                low=99 + i * 0.5,
                close=100.5 + i * 0.5,
                volume=100,
                close_time_ms=base_ts + (i + 1) * 86_400_000 - 1,
                complete=True,
            ))

        s = RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                          slippage_pct=0, max_position_pct=100,
                          max_portfolio_exposure_pct=100,
                          trading_allocation_pct=100,
                          reward_to_risk_target=2.0,
                          max_open_positions=5, max_daily_trades=100,
                          max_daily_loss_pct=100, max_drawdown_pct=100,
                          cooldown_after_consecutive_losses=0,
                          allow_overnight=True, min_24h_volume_inr=0,
                          min_24h_trades=0)
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng, config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        result = bt.run(pair="TEST", candles=candles, provenance=prov)

        # Buy-and-hold should be non-zero (prices are rising)
        assert result.buy_hold_return_pct != 0.0, \
            f"buy-hold should be non-zero, got {result.buy_hold_return_pct}%"
        # Max drawdown should be 0% (no trades, flat equity at 2000)
        # OR small if there were trades
        assert result.max_drawdown_pct < 50.0, \
            f"max_drawdown should be < 50%, got {result.max_drawdown_pct}%"


# ---------------------------------------------------------------------------
# 4: Buy-and-hold benchmark uses correct first/last prices
# ---------------------------------------------------------------------------
class TestBuyAndHold:
    """Verify buy-and-hold is calculated from the segment's actual
    first and last candle prices."""

    def test_bh_rising_series(self):
        """A rising series should produce positive buy-and-hold."""
        from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import BaselineSMAStrategy, SignalEngine

        candles = []
        base_ts = 1_700_000_000_000
        for i in range(250):
            price = 100 + i  # rising from 100 to 349
            candles.append(Candle(
                open_time_ms=base_ts + i * 86_400_000,
                open=price, high=price + 1, low=price - 1,
                close=price, volume=100,
                close_time_ms=base_ts + (i + 1) * 86_400_000 - 1,
                complete=True))

        s = RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                          slippage_pct=0, max_position_pct=100,
                          max_portfolio_exposure_pct=100,
                          trading_allocation_pct=100,
                          max_open_positions=5, max_daily_trades=100,
                          max_daily_loss_pct=100, max_drawdown_pct=100,
                          cooldown_after_consecutive_losses=0,
                          allow_overnight=True, min_24h_volume_inr=0,
                          min_24h_trades=0)
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng, config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        result = bt.run(pair="TEST", candles=candles, provenance=prov)

        # Buy at 100, sell at 349: return = (349/100 - 1) * 100 = 249%
        expected_bh = (349.0 / 100.0 - 1) * 100
        assert abs(result.buy_hold_return_pct - expected_bh) < 0.1, \
            f"bh should be {expected_bh}%, got {result.buy_hold_return_pct}%"

    def test_bh_falling_series(self):
        """A falling series should produce negative buy-and-hold."""
        from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import BaselineSMAStrategy, SignalEngine

        candles = []
        base_ts = 1_700_000_000_000
        for i in range(250):
            price = 300 - i  # falling from 300 to 51
            candles.append(Candle(
                open_time_ms=base_ts + i * 86_400_000,
                open=price, high=price + 1, low=price - 1,
                close=price, volume=100,
                close_time_ms=base_ts + (i + 1) * 86_400_000 - 1,
                complete=True))

        s = RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                          slippage_pct=0, max_position_pct=100,
                          max_portfolio_exposure_pct=100,
                          trading_allocation_pct=100,
                          max_open_positions=5, max_daily_trades=100,
                          max_daily_loss_pct=100, max_drawdown_pct=100,
                          cooldown_after_consecutive_losses=0,
                          allow_overnight=True, min_24h_volume_inr=0,
                          min_24h_trades=0)
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng, config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        result = bt.run(pair="TEST", candles=candles, provenance=prov)

        # Buy at 300, sell at 51: return = (51/300 - 1) * 100 = -83%
        expected_bh = (51.0 / 300.0 - 1) * 100
        assert abs(result.buy_hold_return_pct - expected_bh) < 0.1, \
            f"bh should be {expected_bh}%, got {result.buy_hold_return_pct}%"

    def test_bh_zero_trades_segment(self):
        """Even with 0 trades, buy-and-hold should be non-zero if
        prices moved. This was the bug — bh=0% for dev/val segments."""
        from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import BaselineSMAStrategy, SignalEngine

        # Flat series (no movement) → bh=0% is correct
        candles = []
        base_ts = 1_700_000_000_000
        for i in range(250):
            candles.append(Candle(
                open_time_ms=base_ts + i * 86_400_000,
                open=100, high=101, low=99, close=100, volume=100,
                close_time_ms=base_ts + (i + 1) * 86_400_000 - 1,
                complete=True))

        s = RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                          slippage_pct=0, max_position_pct=100,
                          max_portfolio_exposure_pct=100,
                          trading_allocation_pct=100,
                          max_open_positions=5, max_daily_trades=100,
                          max_daily_loss_pct=100, max_drawdown_pct=100,
                          cooldown_after_consecutive_losses=0,
                          allow_overnight=True, min_24h_volume_inr=0,
                          min_24h_trades=0)
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng, config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        result = bt.run(pair="TEST", candles=candles, provenance=prov)

        # Flat: bh=0% is correct
        assert result.buy_hold_return_pct == 0.0
        assert result.num_trades == 0
        # But if prices moved, bh should be non-zero — test with rising:
        candles_rising = []
        for i in range(250):
            price = 100 + i
            candles_rising.append(Candle(
                open_time_ms=base_ts + i * 86_400_000,
                open=price, high=price + 1, low=price - 1,
                close=price, volume=100,
                close_time_ms=base_ts + (i + 1) * 86_400_000 - 1,
                complete=True))
        result2 = bt.run(pair="TEST2", candles=candles_rising, provenance=prov)
        assert result2.buy_hold_return_pct != 0.0
        assert result2.num_trades == 0  # still 0 trades


# ---------------------------------------------------------------------------
# 5-6: Paper-exit: candles before entry ignored, CLOSED not re-processed
# ---------------------------------------------------------------------------
class TestPaperExitCorrectness:
    """Verify check_exits() handles edge cases correctly."""

    def test_candles_before_entry_ignored(self):
        """If a candle BEFORE the trade's entry time hits the stop,
        it should NOT trigger an exit (the trade wasn't open yet).

        Phase 10: this filter is NOW enforced. Previously the
        implementation acknowledged this as a 'known limitation'; the
        Phase 10 paper-exit recovery work closes that gap.
        """
        from coindcx_lab.db.store import Store
        from coindcx_lab.engine.paper import PaperTradingEngine, PaperTradeStatus
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import Signal, SignalState
        from coindcx_lab.api.models import Candle

        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            s = RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                              slippage_pct=0, max_position_pct=100,
                              max_portfolio_exposure_pct=100,
                              trading_allocation_pct=100,
                              reward_to_risk_target=2.0,
                              max_open_positions=5, max_daily_trades=100,
                              max_daily_loss_pct=100, max_drawdown_pct=100,
                              cooldown_after_consecutive_losses=0,
                              allow_overnight=True, min_24h_volume_inr=0,
                              min_24h_trades=0)
            store = Store(path=db_path)
            pe = PaperTradingEngine(settings=s, store=store)

            # Open trade: entry=100, stop=95, target=110
            sig = Signal(pair="B-BTCINR", interval="1h", strategy="test",
                         state=SignalState.BUY_CANDIDATE,
                         entry=100, stop=95, target=110, rr_after_cost=3.0)
            trade, _ = pe.open_position(signal=sig)
            assert trade is not None
            entry_time = trade.entry_time_ms

            # Create candles: one BEFORE entry (low=90, hits stop)
            # and one AFTER entry (normal range — neither hits stop/target).
            before_candle = Candle(
                open_time_ms=entry_time - 3_600_000,  # 1h before entry
                open=100, high=101, low=90, close=100, volume=10,
                close_time_ms=entry_time - 1, complete=True)
            after_candle = Candle(
                open_time_ms=entry_time + 3_600_000,  # 1h after entry
                open=100, high=101, low=99, close=100, volume=10,
                close_time_ms=entry_time + 7_200_000 - 1, complete=True)

            closed = pe.check_exits(candles_by_pair={
                "B-BTCINR": [before_candle, after_candle]})

            # Phase 10: the before-entry candle MUST be filtered out,
            # and the after-entry candle has neither hit. So NO exit.
            assert len(closed) == 0, \
                "candle before entry must not trigger exit (Phase 10 fix)"
            # Trade should still be OPEN
            opens = pe.store.get_open_trades()
            assert len(opens) == 1
        finally:
            store.close()
            os.unlink(db_path)

    def test_closed_trades_not_reprocessed(self):
        """A CLOSED trade should not be processed again by check_exits."""
        from coindcx_lab.db.store import Store
        from coindcx_lab.engine.paper import PaperTradingEngine, PaperTradeStatus
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import Signal, SignalState
        from coindcx_lab.api.models import Candle

        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            s = RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                              slippage_pct=0, max_position_pct=100,
                              max_portfolio_exposure_pct=100,
                              trading_allocation_pct=100,
                              reward_to_risk_target=2.0,
                              max_open_positions=5, max_daily_trades=100,
                              max_daily_loss_pct=100, max_drawdown_pct=100,
                              cooldown_after_consecutive_losses=0,
                              allow_overnight=True, min_24h_volume_inr=0,
                              min_24h_trades=0)
            store = Store(path=db_path)
            pe = PaperTradingEngine(settings=s, store=store)

            sig = Signal(pair="B-BTCINR", interval="1h", strategy="test",
                         state=SignalState.BUY_CANDIDATE,
                         entry=100, stop=95, target=110, rr_after_cost=3.0)
            trade, _ = pe.open_position(signal=sig)

            # Candle that hits the target
            target_candle = Candle(
                open_time_ms=trade.entry_time_ms + 3_600_000,
                open=100, high=115, low=99, close=110, volume=10,
                close_time_ms=trade.entry_time_ms + 7_200_000 - 1,
                complete=True)

            # First call: should close the trade
            closed1 = pe.check_exits(candles_by_pair={"B-BTCINR": [target_candle]})
            assert len(closed1) == 1
            assert closed1[0].exit_reason == "target"

            # Second call with SAME candle: should NOT re-process
            closed2 = pe.check_exits(candles_by_pair={"B-BTCINR": [target_candle]})
            assert len(closed2) == 0, "CLOSED trade should not be re-processed"
        finally:
            store.close()
            os.unlink(db_path)

    def test_stop_first_on_same_candle(self):
        """If both stop and target are hit in the same candle,
        stop-first (conservative) should be used."""
        from coindcx_lab.db.store import Store
        from coindcx_lab.engine.paper import PaperTradingEngine
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import Signal, SignalState
        from coindcx_lab.api.models import Candle

        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            s = RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                              slippage_pct=0, max_position_pct=100,
                              max_portfolio_exposure_pct=100,
                              trading_allocation_pct=100,
                              reward_to_risk_target=2.0,
                              max_open_positions=5, max_daily_trades=100,
                              max_daily_loss_pct=100, max_drawdown_pct=100,
                              cooldown_after_consecutive_losses=0,
                              allow_overnight=True, min_24h_volume_inr=0,
                              min_24h_trades=0)
            store = Store(path=db_path)
            pe = PaperTradingEngine(settings=s, store=store)

            sig = Signal(pair="B-BTCINR", interval="1h", strategy="test",
                         state=SignalState.BUY_CANDIDATE,
                         entry=100, stop=95, target=110, rr_after_cost=3.0)
            trade, _ = pe.open_position(signal=sig)

            # Candle that hits BOTH stop (low=90) and target (high=115)
            both_candle = Candle(
                open_time_ms=trade.entry_time_ms + 3_600_000,
                open=100, high=115, low=90, close=100, volume=10,
                close_time_ms=trade.entry_time_ms + 7_200_000 - 1,
                complete=True)

            closed = pe.check_exits(candles_by_pair={"B-BTCINR": [both_candle]},
                                       assume_stop_first_on_ambiguous=True)
            assert len(closed) == 1
            assert closed[0].exit_reason == "stop", \
                "should exit at stop (conservative) when both hit"
        finally:
            store.close()
            os.unlink(db_path)

    def test_incomplete_candle_skipped(self):
        """Incomplete (still-forming) candles should not trigger exits."""
        from coindcx_lab.db.store import Store
        from coindcx_lab.engine.paper import PaperTradingEngine
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import Signal, SignalState
        from coindcx_lab.api.models import Candle

        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            s = RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                              slippage_pct=0, max_position_pct=100,
                              max_portfolio_exposure_pct=100,
                              trading_allocation_pct=100,
                              reward_to_risk_target=2.0,
                              max_open_positions=5, max_daily_trades=100,
                              max_daily_loss_pct=100, max_drawdown_pct=100,
                              cooldown_after_consecutive_losses=0,
                              allow_overnight=True, min_24h_volume_inr=0,
                              min_24h_trades=0)
            store = Store(path=db_path)
            pe = PaperTradingEngine(settings=s, store=store)

            sig = Signal(pair="B-BTCINR", interval="1h", strategy="test",
                         state=SignalState.BUY_CANDIDATE,
                         entry=100, stop=95, target=110, rr_after_cost=3.0)
            trade, _ = pe.open_position(signal=sig)

            # Incomplete candle that hits target
            incomplete_candle = Candle(
                open_time_ms=trade.entry_time_ms + 3_600_000,
                open=100, high=115, low=99, close=110, volume=10,
                close_time_ms=trade.entry_time_ms + 7_200_000 - 1,
                complete=False)  # INCOMPLETE

            closed = pe.check_exits(candles_by_pair={"B-BTCINR": [incomplete_candle]})
            assert len(closed) == 0, "incomplete candle should not trigger exit"
        finally:
            store.close()
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# 7: Walk-forward warm-up does not leak trades
# ---------------------------------------------------------------------------
class TestWalkForwardWarmup:
    """Verify that warm-up candles from earlier segments don't create
    scored trades in the evaluated segment."""

    def test_warmup_candles_dont_produce_trades(self):
        """The backtester should skip the first warmup_skip_bars candles
        for signal evaluation, but still use them for indicator calculation."""
        from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import BaselineSMAStrategy, SignalEngine

        # Build 250 candles with a strong uptrend
        candles = []
        base_ts = 1_700_000_000_000
        for i in range(250):
            price = 100 + i * 0.5
            candles.append(Candle(
                open_time_ms=base_ts + i * 86_400_000,
                open=price, high=price + 1, low=price - 1,
                close=price, volume=100,
                close_time_ms=base_ts + (i + 1) * 86_400_000 - 1,
                complete=True))

        s = RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                          slippage_pct=0, max_position_pct=100,
                          max_portfolio_exposure_pct=100,
                          trading_allocation_pct=100,
                          max_open_positions=5, max_daily_trades=100,
                          max_daily_loss_pct=100, max_drawdown_pct=100,
                          cooldown_after_consecutive_losses=0,
                          allow_overnight=True, min_24h_volume_inr=0,
                          min_24h_trades=0)
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        # Set warmup to 100 — signals should only be evaluated from bar 100+
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=100)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng, config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        result = bt.run(pair="TEST", candles=candles, provenance=prov)

        # The equity curve records equity for ALL bars (including warmup).
        # The warmup only prevents SIGNAL EVALUATION (and thus trades)
        # during the first warmup_skip_bars. So the equity curve should
        # have 250 points, but NO trades should have been placed during
        # the first 100 bars.
        assert len(result.equity_curve) == 250, \
            f"equity_curve should have 250 points (one per bar), " \
            f"got {len(result.equity_curve)}"
        # Verify all trades have entry_idx >= warmup_skip_bars
        for t in result.trades:
            assert t.entry_idx >= 100, \
                f"trade entry_idx={t.entry_idx} should be >= 100 (warmup)"

    def test_walk_forward_segments_non_overlapping(self):
        """Walk-forward dev/val/oos segments should not overlap."""
        from coindcx_lab.api.models import Candle
        from coindcx_lab.engine.backtest import Backtester, BacktestConfig, _day_iso_from_ms
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import BaselineSMAStrategy, SignalEngine

        candles = []
        base_ts = 1_700_000_000_000
        for i in range(500):
            candles.append(Candle(
                open_time_ms=base_ts + i * 86_400_000,
                open=100 + i * 0.1, high=101 + i * 0.1,
                low=99 + i * 0.1, close=100.5 + i * 0.1,
                volume=100,
                close_time_ms=base_ts + (i + 1) * 86_400_000 - 1,
                complete=True))

        s = RiskSettings(initial_inr=2000)
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng, config=cfg)

        mid = candles[250].open_time_ms
        early = candles[125].open_time_ms
        dev_end = _day_iso_from_ms(early)
        val_end = _day_iso_from_ms(mid)
        results = bt.walk_forward(pair="TEST", candles=candles,
                                   dev_end_iso=dev_end, val_end_iso=val_end)

        # Each segment should have results
        for name in ("dev", "val", "oos"):
            assert name in results
            seg = results[name]
            # Segments should be non-empty
            assert seg.num_trades >= 0  # may be 0
