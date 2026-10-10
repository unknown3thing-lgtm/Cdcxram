"""Tests: risk engine, position sizing, capital scaling, fees, slippage,
stop-loss & target calculations, daily loss & drawdown limits, invalid
settings.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta

import pytest

from coindcx_lab.engine.risk import (
    DailyLossState, PositionPlan, RiskDecision, RiskEngine, RiskError,
    RiskSettings, RiskState,
)


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------
class TestSettingsValidation:
    def test_negative_capital_rejected(self):
        with pytest.raises(RiskError):
            RiskSettings(initial_inr=-1).validate()

    def test_zero_capital_rejected(self):
        with pytest.raises(RiskError):
            RiskSettings(initial_inr=0).validate()

    def test_zero_risk_rejected(self):
        s = RiskSettings(initial_inr=1000, risk_per_trade_pct=0)
        with pytest.raises(RiskError):
            s.validate()

    def test_excess_risk_rejected(self):
        s = RiskSettings(initial_inr=1000, risk_per_trade_pct=150)
        with pytest.raises(RiskError):
            s.validate()

    def test_zero_max_positions_rejected(self):
        s = RiskSettings(initial_inr=1000, max_open_positions=0)
        with pytest.raises(RiskError):
            s.validate()

    def test_negative_fee_rejected(self):
        s = RiskSettings(initial_inr=1000, fee_pct=-0.1)
        with pytest.raises(RiskError):
            s.validate()

    def test_negative_slippage_rejected(self):
        s = RiskSettings(initial_inr=1000, slippage_pct=-0.1)
        with pytest.raises(RiskError):
            s.validate()


# ---------------------------------------------------------------------------
# Position sizing — the spec's worked example
# ---------------------------------------------------------------------------
class TestPositionSizing:
    def test_worked_example_2000_equity_1pct_risk_2pct_stop(self,
                                                            default_settings):
        """Spec example: ₹2,000 equity, 1% risk => ₹20 risk budget.
        2% stop distance => ₹1,000 risk-based value (before caps)."""
        s = default_settings   # initial_inr=2000, risk_per_trade_pct=1.0
        # Loosen caps to expose the risk-based value
        s.max_position_pct = 100.0
        s.max_portfolio_exposure_pct = 100.0
        s.trading_allocation_pct = 100.0
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = RiskEngine(s)
        state = RiskState(initial_inr=2000)
        state.daily = DailyLossState(day_iso="2024-01-01")
        entry, stop, target = 100.0, 98.0, 104.0   # 2% stop, 2R target
        plan = eng.plan_position(pair="TEST", entry=entry, stop=stop,
                                 target=target, state=state)
        assert plan.decision is RiskDecision.ACCEPTED
        assert math.isclose(plan.risk_budget, 20.0, rel_tol=1e-9)
        assert math.isclose(plan.position_value, 1000.0, rel_tol=1e-9)
        assert math.isclose(plan.qty, 10.0, rel_tol=1e-9)
        assert math.isclose(plan.rr_after_cost, 2.0, rel_tol=1e-9)

    def test_scales_proportionally_to_10000_equity(self, default_settings):
        """Same setup but 5x the equity => 5x the position value."""
        s = default_settings
        s.max_position_pct = 100.0
        s.max_portfolio_exposure_pct = 100.0
        s.trading_allocation_pct = 100.0
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        s.initial_inr = 10000.0
        eng = RiskEngine(s)
        state = RiskState(initial_inr=10000)
        state.daily = DailyLossState(day_iso="2024-01-01")
        entry, stop, target = 100.0, 98.0, 104.0
        plan = eng.plan_position(pair="TEST", entry=entry, stop=stop,
                                 target=target, state=state)
        assert plan.decision is RiskDecision.ACCEPTED
        assert math.isclose(plan.risk_budget, 100.0, rel_tol=1e-9)   # 1% of 10k
        assert math.isclose(plan.position_value, 5000.0, rel_tol=1e-9)
        assert math.isclose(plan.qty, 50.0, rel_tol=1e-9)

    def test_capital_scales_proportionally_for_all_presets(self,
                                                            default_settings):
        """For ₹2000 / ₹5000 / ₹10000 / ₹25000 / ₹50000 with the same risk
        % and stop distance, position_value scales linearly."""
        for cap in (2000, 5000, 10000, 25000, 50000):
            s = RiskSettings(
                initial_inr=cap,
                risk_per_trade_pct=1.0,
                max_position_pct=100.0,
                max_portfolio_exposure_pct=100.0,
                trading_allocation_pct=100.0,
                fee_pct=0, spread_pct=0, slippage_pct=0,
                reward_to_risk_target=2.0,
                max_open_positions=5, max_daily_trades=10,
                max_daily_loss_pct=5.0, max_drawdown_pct=15.0,
                cooldown_after_consecutive_losses=3,
                allow_overnight=True, min_24h_volume_inr=0,
                min_24h_trades=0,
            )
            eng = RiskEngine(s)
            state = RiskState(initial_inr=cap)
            state.daily = DailyLossState(day_iso="2024-01-01")
            plan = eng.plan_position(pair="TEST", entry=100.0, stop=98.0,
                                     target=104.0, state=state)
            assert plan.decision is RiskDecision.ACCEPTED
            # Expected: cap * 0.01 / 0.02 = cap * 0.5
            assert math.isclose(plan.position_value, cap * 0.5, rel_tol=1e-9), \
                f"failed for cap={cap}: got {plan.position_value}"

    def test_zero_stop_distance_rejected(self, default_settings):
        """Zero stop distance (stop == entry) must raise immediately."""
        eng = RiskEngine(default_settings)
        state = RiskState(initial_inr=default_settings.initial_inr)
        state.daily = DailyLossState(day_iso="2024-01-01")
        with pytest.raises(RiskError):
            eng.plan_position(pair="TEST", entry=100.0, stop=100.0,
                              target=110.0, state=state)

    def test_stop_above_entry_rejected(self, default_settings):
        eng = RiskEngine(default_settings)
        state = RiskState(initial_inr=default_settings.initial_inr)
        state.daily = DailyLossState(day_iso="2024-01-01")
        with pytest.raises(RiskError):
            eng.plan_position(pair="TEST", entry=100.0, stop=105.0,
                              target=110.0, state=state)

    def test_target_below_entry_rejected(self, default_settings):
        eng = RiskEngine(default_settings)
        state = RiskState(initial_inr=default_settings.initial_inr)
        state.daily = DailyLossState(day_iso="2024-01-01")
        with pytest.raises(RiskError):
            eng.plan_position(pair="TEST", entry=100.0, stop=98.0,
                              target=95.0, state=state)


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------
class TestCaps:
    def test_max_position_pct_caps_value(self, default_settings):
        """If risk-based value would exceed max_position_pct of equity,
        the cap kicks in."""
        s = default_settings
        s.initial_inr = 2000
        s.risk_per_trade_pct = 5.0       # huge risk
        s.max_position_pct = 25.0       # cap at 25% of equity
        s.max_portfolio_exposure_pct = 100.0
        s.trading_allocation_pct = 100.0
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = RiskEngine(s)
        state = RiskState(initial_inr=2000)
        state.daily = DailyLossState(day_iso="2024-01-01")
        plan = eng.plan_position(pair="TEST", entry=100.0, stop=98.0,
                                 target=104.0, state=state)
        # max_position cap = 2000 * 0.25 = 500
        assert math.isclose(plan.position_value, 500.0, rel_tol=1e-9)
        assert plan.cap_reason == "max_position"

    def test_cash_caps_when_low(self, default_settings):
        s = default_settings
        s.initial_inr = 2000
        s.risk_per_trade_pct = 5.0
        s.max_position_pct = 100.0
        s.max_portfolio_exposure_pct = 100.0
        s.trading_allocation_pct = 100.0
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = RiskEngine(s)
        state = RiskState(initial_inr=2000)
        state.daily = DailyLossState(day_iso="2024-01-01")
        # Pretend 1500 is already committed
        state.open_positions_value = 1500
        plan = eng.plan_position(pair="TEST", entry=100.0, stop=98.0,
                                 target=104.0, state=state)
        # Available cash = 500, which should cap
        assert plan.position_value <= 500 + 1e-6
        assert plan.cap_reason == "cash"


# ---------------------------------------------------------------------------
# Costs & R-R
# ---------------------------------------------------------------------------
class TestCostsAndRR:
    def test_costs_reduce_net_rr(self, default_settings):
        s = default_settings
        s.initial_inr = 10000
        s.risk_per_trade_pct = 1.0
        s.max_position_pct = 100.0
        s.max_portfolio_exposure_pct = 100.0
        s.trading_allocation_pct = 100.0
        s.fee_pct = 0.5; s.spread_pct = 0.0; s.slippage_pct = 0.5
        eng = RiskEngine(s)
        state = RiskState(initial_inr=10000)
        state.daily = DailyLossState(day_iso="2024-01-01")
        plan = eng.plan_position(pair="TEST", entry=100.0, stop=98.0,
                                 target=104.0, state=state)
        # Gross R = 2.0, costs eat into both sides => net R < 2.0
        assert plan.rr_after_cost < 2.0
        # And the gross values should equal (entry-stop)*qty and
        # (target-entry)*qty
        assert math.isclose(plan.potential_loss_gross,
                            (100 - 98) * plan.qty, rel_tol=1e-9)
        assert math.isclose(plan.potential_gain_gross,
                            (104 - 100) * plan.qty, rel_tol=1e-9)
        # Net = gross + costs on loss side; net = gross - costs on gain side
        assert plan.potential_loss_net > plan.potential_loss_gross
        assert plan.potential_gain_net < plan.potential_gain_gross

    def test_low_rr_rejected(self, default_settings):
        """Target with R-R below target triggers REJECTED_RR."""
        s = default_settings
        eng = RiskEngine(s)
        state = RiskState(initial_inr=s.initial_inr)
        state.daily = DailyLossState(day_iso="2024-01-01")
        # 1% stop, 1.5% target => R = 1.5; below 2.0 target
        plan = eng.plan_position(pair="TEST", entry=100.0, stop=99.0,
                                 target=101.5, state=state)
        assert plan.decision is RiskDecision.REJECTED_RR


# ---------------------------------------------------------------------------
# Daily loss & drawdown
# ---------------------------------------------------------------------------
class TestDailyLoss:
    def test_blocks_new_entry_after_daily_loss_hit(self, default_settings):
        s = default_settings
        s.initial_inr = 2000
        s.max_daily_loss_pct = 5.0     # 100 INR
        eng = RiskEngine(s)
        state = RiskState(initial_inr=2000)
        state.daily = DailyLossState(day_iso="2024-01-01")
        state.daily.realized_pnl_day = -110     # exceeded 100
        plan = eng.plan_position(pair="TEST", entry=100.0, stop=98.0,
                                 target=104.0, state=state)
        assert plan.decision is RiskDecision.REJECTED_DAILY_LOSS

    def test_blocks_new_entry_after_abs_daily_loss_hit(self, default_settings):
        s = default_settings
        s.max_daily_loss_pct = 100.0    # disabled %, use abs
        s.max_daily_loss_abs_inr = 50
        eng = RiskEngine(s)
        state = RiskState(initial_inr=2000)
        state.daily = DailyLossState(day_iso="2024-01-01")
        state.daily.realized_pnl_day = -60
        plan = eng.plan_position(pair="TEST", entry=100.0, stop=98.0,
                                 target=104.0, state=state)
        assert plan.decision is RiskDecision.REJECTED_DAILY_LOSS


class TestDrawdown:
    def test_blocks_after_drawdown_exceeded(self, default_settings):
        s = default_settings
        s.max_drawdown_pct = 15.0
        eng = RiskEngine(s)
        state = RiskState(initial_inr=2000, peak_equity=2000)
        # Force current_equity below drawdown threshold
        state.realized_pnl = -350   # 17.5% drawdown from peak 2000
        state.daily = DailyLossState(day_iso="2024-01-01")
        plan = eng.plan_position(pair="TEST", entry=100.0, stop=98.0,
                                 target=104.0, state=state)
        assert plan.decision is RiskDecision.REJECTED_DRAWDOWN


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_open_close_pnl_accounting(self, default_settings):
        s = default_settings
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = RiskEngine(s)
        state = RiskState(initial_inr=2000)
        state.daily = DailyLossState(day_iso="2024-01-01")
        # Open: entry=100, stop=98, qty=10
        eng.on_trade_opened(entry=100.0, stop=98.0, qty=10.0, state=state)
        assert math.isclose(state.open_positions_value, 1000.0)
        assert math.isclose(state.open_positions_risk, 20.0)
        # Close at 104 (target) => pnl = (104-100)*10 = 40
        pnl = eng.on_trade_closed(entry=100.0, stop=98.0, target=104.0,
                                    qty=10.0, exit_price=104.0,
                                    state=state, day_iso="2024-01-01")
        assert math.isclose(pnl, 40.0)
        assert math.isclose(state.realized_pnl, 40.0)
        assert math.isclose(state.open_positions_value, 0.0)
        assert state.consecutive_losses == 0
        assert state.daily.realized_pnl_day == 40.0
        assert state.daily.trades_today == 1

    def test_consecutive_losses_trigger_cooldown(self, default_settings):
        s = default_settings
        s.cooldown_after_consecutive_losses = 3
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = RiskEngine(s)
        state = RiskState(initial_inr=2000)
        state.daily = DailyLossState(day_iso="2024-01-01")
        # 3 losing trades in a row
        for _ in range(3):
            eng.on_trade_opened(entry=100.0, stop=98.0, qty=10.0, state=state)
            eng.on_trade_closed(entry=100.0, stop=98.0, target=104.0,
                                  qty=10.0, exit_price=98.0,
                                  state=state, day_iso="2024-01-01")
        assert state.consecutive_losses == 3
        assert state.cooldown_until_iso is not None

    def test_winning_trade_resets_consecutive_losses(self, default_settings):
        s = default_settings
        s.cooldown_after_consecutive_losses = 3
        s.fee_pct = 0; s.spread_pct = 0; s.slippage_pct = 0
        eng = RiskEngine(s)
        state = RiskState(initial_inr=2000)
        state.daily = DailyLossState(day_iso="2024-01-01")
        # 2 losses
        for _ in range(2):
            eng.on_trade_opened(entry=100.0, stop=98.0, qty=10.0, state=state)
            eng.on_trade_closed(entry=100.0, stop=98.0, target=104.0,
                                  qty=10.0, exit_price=98.0,
                                  state=state, day_iso="2024-01-01")
        assert state.consecutive_losses == 2
        # A win resets it
        eng.on_trade_opened(entry=100.0, stop=98.0, qty=10.0, state=state)
        eng.on_trade_closed(entry=100.0, stop=98.0, target=104.0,
                              qty=10.0, exit_price=104.0,
                              state=state, day_iso="2024-01-01")
        assert state.consecutive_losses == 0
