"""Tests: paper-trade lifecycle, persistence, reset behaviour."""

from __future__ import annotations

import math
import os
import tempfile
from datetime import datetime, timezone

import pytest

from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
from coindcx_lab.db.store import Store
from coindcx_lab.engine.paper import PaperTrade, PaperTradeStatus, PaperTradingEngine
from coindcx_lab.engine.risk import RiskSettings, RiskState
from coindcx_lab.engine.signals import Signal, SignalState


@pytest.fixture
def tmp_store():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    store = Store(path=path)
    yield store
    store.close()
    try:
        os.unlink(path)
    except OSError:
        pass


def _settings_for_paper() -> RiskSettings:
    return RiskSettings(
        initial_inr=2000.0, risk_per_trade_pct=1.0,
        max_position_pct=100.0, max_portfolio_exposure_pct=100.0,
        max_daily_loss_pct=100.0, max_daily_loss_abs_inr=0.0,
        max_drawdown_pct=100.0, max_open_positions=5, max_daily_trades=100,
        reward_to_risk_target=2.0,
        cooldown_after_consecutive_losses=3,
        allow_overnight=True,
        fee_pct=0.0, spread_pct=0.0, slippage_pct=0.0,
        min_24h_volume_inr=0, min_24h_trades=0,
        trading_allocation_pct=100.0,
    )


def _buy_signal(pair: str = "B-BTCINR", entry: float = 100.0,
                stop: float = 98.0, target: float = 104.0) -> Signal:
    return Signal(
        pair=pair, interval="1h", strategy="baseline_sma",
        state=SignalState.BUY_CANDIDATE,
        entry=entry, stop=stop, target=target,
        rr_after_cost=2.0,
        est_entry_cost_inr=0.0, est_exit_cost_inr=0.0,
        explanation="test",
    )


class TestLifecycle:
    def test_open_position_records_trade(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, reason = pe.open_position(signal=sig)
        assert trade is not None
        assert trade.status is PaperTradeStatus.OPEN
        assert reason == "ok"
        # Trade is in the store
        opens = pe.store.get_open_trades()
        assert len(opens) == 1
        assert opens[0]["pair"] == "B-BTCINR"

    def test_check_exits_closes_on_target(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal(entry=100.0, stop=98.0, target=104.0)
        trade, _ = pe.open_position(signal=sig)
        # Phase 10: candle must be AFTER entry time (matches real cron
        # behaviour — cron always fetches recent candles).
        candle = Candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                        open=100, high=105, low=99, close=104,
                        volume=10,
                        close_time_ms=trade.entry_time_ms + 7_200_000 - 1,
                        complete=True)
        closed = pe.check_exits(candles_by_pair={"B-BTCINR": [candle]})
        assert len(closed) == 1
        # Realized PnL = (104-100) * qty
        # qty = 1000 (risk_based) / 100 (entry) = 10
        assert math.isclose(pe.state.realized_pnl, 40.0, rel_tol=1e-9)
        # Trade is now CLOSED in the store
        opens = pe.store.get_open_trades()
        assert len(opens) == 0
        all_trades = pe.list_trades()
        assert all_trades[0].status is PaperTradeStatus.CLOSED

    def test_check_exits_closes_on_stop(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal(entry=100.0, stop=98.0, target=104.0)
        trade, _ = pe.open_position(signal=sig)
        candle = Candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                        open=100, high=101, low=97, close=98,
                        volume=10,
                        close_time_ms=trade.entry_time_ms + 7_200_000 - 1,
                        complete=True)
        closed = pe.check_exits(candles_by_pair={"B-BTCINR": [candle]})
        assert len(closed) == 1
        assert closed[0].exit_reason == "stop"
        # Realized PnL = (98-100) * 10 = -20
        assert math.isclose(pe.state.realized_pnl, -20.0, rel_tol=1e-9)

    def test_conservative_when_both_touched(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal(entry=100.0, stop=98.0, target=104.0)
        trade, _ = pe.open_position(signal=sig)
        # A bar whose range covers BOTH stop and target
        candle = Candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                        open=100, high=105, low=97, close=99,
                        volume=10,
                        close_time_ms=trade.entry_time_ms + 7_200_000 - 1,
                        complete=True)
        closed = pe.check_exits(candles_by_pair={"B-BTCINR": [candle]},
                                  assume_stop_first_on_ambiguous=True)
        assert closed[0].exit_reason == "stop"

    def test_no_action_on_incomplete_latest(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal(entry=100.0, stop=98.0, target=104.0)
        trade, _ = pe.open_position(signal=sig)
        # Latest bar is INCOMPLETE and would hit the target — but should be
        # ignored because we only act on completed bars.
        candle_complete = Candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                                  open=100, high=101, low=99, close=100,
                                  volume=10,
                                  close_time_ms=trade.entry_time_ms + 7_200_000 - 1,
                                  complete=True)
        candle_incomplete = Candle(open_time_ms=trade.entry_time_ms + 7_200_000,
                                    open=100, high=105, low=99, close=104,
                                    volume=10,
                                    close_time_ms=trade.entry_time_ms + 10_800_000 - 1,
                                    complete=False)
        closed = pe.check_exits(
            candles_by_pair={"B-BTCINR": [candle_complete, candle_incomplete]})
        assert len(closed) == 0   # incomplete bar ignored


class TestPersistence:
    def test_state_persists_across_engines(self, tmp_store):
        s = _settings_for_paper()
        pe1 = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        pe1.open_position(signal=sig)
        # Re-open the same store with a fresh engine instance
        pe2 = PaperTradingEngine(settings=s, store=tmp_store)
        # The open trade should still be there
        assert len(pe2.store.get_open_trades()) == 1
        # And the state's open_positions_value should be non-zero
        assert pe2.state.open_positions_value > 0

    def test_settings_change_preserves_history(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        pe.open_position(signal=sig)
        # Update settings
        new_s = _settings_for_paper()
        new_s.risk_per_trade_pct = 2.0
        pe.update_settings(new_s)
        # History must still be there
        assert len(pe.store.get_open_trades()) == 1
        # New risk engine uses the new setting
        assert pe.risk_engine.s.risk_per_trade_pct == 2.0


class TestReset:
    def test_reset_without_confirmation_returns_false(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        pe.open_position(signal=_buy_signal())
        ok = pe.reset(require_confirmation=True, confirmation_token=None)
        assert ok is False
        # History still intact
        assert len(pe.store.get_open_trades()) == 1

    def test_reset_with_confirmation_wipes_all(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        pe.open_position(signal=_buy_signal())
        ok = pe.reset(require_confirmation=True,
                       confirmation_token="RESET-ALL-CONFIRMED")
        assert ok is True
        # History wiped
        assert len(pe.store.get_open_trades()) == 0
        assert len(pe.store.list_trades()) == 0
        assert pe.state.realized_pnl == 0.0
        assert pe.state.open_positions_value == 0.0


class TestInvalidSettings:
    def test_invalid_settings_in_engine_construction(self, tmp_store):
        s = RiskSettings(initial_inr=-1)   # invalid
        with pytest.raises(Exception):
            PaperTradingEngine(settings=s, store=tmp_store)


class TestNoTradeOnNonBuy:
    def test_no_trade_on_wait_signal(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = Signal(pair="B-BTCINR", interval="1h", strategy="x",
                     state=SignalState.WAIT, explanation="no setup")
        trade, reason = pe.open_position(signal=sig)
        assert trade is None
        assert "not BUY_CANDIDATE" in reason


class TestCooldown:
    def test_pause_blocks_new_entries(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        pe.pause()
        trade, reason = pe.open_position(signal=_buy_signal())
        assert trade is None
        assert "paused" in reason

    def test_resume_allows_entries(self, tmp_store):
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        pe.pause()
        pe.resume()
        trade, reason = pe.open_position(signal=_buy_signal())
        assert trade is not None
        assert reason == "ok"
