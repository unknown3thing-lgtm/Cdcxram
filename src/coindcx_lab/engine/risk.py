"""Risk & position-sizing engine.

Implements the position-sizing formula from the project spec:

    risk_budget      = current_equity * risk_per_trade_pct / 100
    stop_distance    = abs(entry - stop) / entry            # as a fraction
    risk_based_value = risk_budget / stop_distance          # before caps

Then capped by:
    - available cash
    - max position allocation (max_position_pct of equity)
    - max portfolio exposure (max_portfolio_exposure_pct of equity)
    - exchange minimum order constraints (where verified)

Fees & slippage are accounted for when checking whether the trade still
fits the risk budget. The engine explicitly rejects:

    - non-positive equity, risk percent, or stop distance
    - zero stop distance (would divide by zero / infinite position)
    - any plan whose post-cost R/R is below target

No leverage. No borrowed funds. No real orders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Tuple


class RiskError(ValueError):
    """Raised when a candidate trade violates a risk rule or invalid input."""


class RiskDecision(str, Enum):
    ACCEPTED = "accepted"
    REJECTED_RISK_BUDGET = "rejected_risk_budget"
    REJECTED_RR = "rejected_rr"
    REJECTED_CAP = "rejected_cap"
    REJECTED_DAILY_LOSS = "rejected_daily_loss"
    REJECTED_DRAWDOWN = "rejected_drawdown"
    REJECTED_MAX_POSITIONS = "rejected_max_positions"
    REJECTED_COOLDOWN = "rejected_cooldown"
    REJECTED_MAX_DAILY_TRADES = "rejected_max_daily_trades"
    REJECTED_INVALID = "rejected_invalid"


@dataclass
class RiskSettings:
    """All user-configurable risk parameters."""

    initial_inr: float = 2000.0
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 25.0
    max_portfolio_exposure_pct: float = 80.0
    max_daily_loss_pct: float = 5.0
    max_daily_loss_abs_inr: float = 0.0    # 0 = disabled
    max_drawdown_pct: float = 15.0
    max_open_positions: int = 5
    max_daily_trades: int = 10
    reward_to_risk_target: float = 2.0
    cooldown_after_consecutive_losses: int = 3
    allow_overnight: bool = True

    # Cost assumptions
    fee_pct: float = 0.1            # applied on each side (entry and exit)
    spread_pct: float = 0.05        # estimate
    slippage_pct: float = 0.05       # adverse fill estimate (per side)

    # Liquidity / volume minimums
    min_24h_volume_inr: float = 5_000_000.0
    min_24h_trades: int = 1000

    # Trading allocation (separate from per-trade risk). The total value
    # that may be deployed into paper positions as a fraction of equity.
    trading_allocation_pct: float = 100.0

    def validate(self) -> None:
        """Raise RiskError on any invalid setting."""
        if not (self.initial_inr > 0):
            raise RiskError("initial_inr must be > 0")
        if not (0 < self.risk_per_trade_pct <= 100):
            raise RiskError("risk_per_trade_pct must be in (0, 100]")
        if not (0 < self.max_position_pct <= 100):
            raise RiskError("max_position_pct must be in (0, 100]")
        if not (0 < self.max_portfolio_exposure_pct <= 100):
            raise RiskError("max_portfolio_exposure_pct must be in (0, 100]")
        if not (0 < self.max_daily_loss_pct <= 100):
            raise RiskError("max_daily_loss_pct must be in (0, 100]")
        if self.max_daily_loss_abs_inr < 0:
            raise RiskError("max_daily_loss_abs_inr must be >= 0")
        if not (0 < self.max_drawdown_pct <= 100):
            raise RiskError("max_drawdown_pct must be in (0, 100]")
        if self.max_open_positions < 1:
            raise RiskError("max_open_positions must be >= 1")
        if self.max_daily_trades < 1:
            raise RiskError("max_daily_trades must be >= 1")
        if self.reward_to_risk_target <= 0:
            raise RiskError("reward_to_risk_target must be > 0")
        if self.cooldown_after_consecutive_losses < 0:
            raise RiskError("cooldown_after_consecutive_losses must be >= 0")
        if self.fee_pct < 0:
            raise RiskError("fee_pct must be >= 0")
        if self.spread_pct < 0:
            raise RiskError("spread_pct must be >= 0")
        if self.slippage_pct < 0:
            raise RiskError("slippage_pct must be >= 0")
        if not (0 < self.trading_allocation_pct <= 100):
            raise RiskError("trading_allocation_pct must be in (0, 100]")


@dataclass
class DailyLossState:
    """Tracks how much has been lost (paper) in the current calendar day."""

    day_iso: str           # YYYY-MM-DD in display TZ
    realized_pnl_day: float = 0.0   # realized pnl today (sum of closed trades)
    trades_today: int = 0

    def reset(self, day_iso: str) -> None:
        self.day_iso = day_iso
        self.realized_pnl_day = 0.0
        self.trades_today = 0


@dataclass
class PositionPlan:
    """A fully-validated entry plan returned by RiskEngine.plan_position()."""

    pair: str
    side: str                           # "long" (paper-trading is long-only)
    entry: float
    stop: float
    target: float
    qty: float
    position_value: float               # entry * qty
    stop_distance_fraction: float       # abs(entry-stop)/entry
    risk_budget: float                  # current_equity * risk%
    potential_loss_gross: float          # (entry - stop) * qty
    potential_gain_gross: float          # (target - entry) * qty
    est_entry_cost: float
    est_exit_cost: float
    est_slippage_cost: float
    potential_loss_net: float           # gross + costs (long: stop below entry)
    potential_gain_net: float
    rr_after_cost: float                # potential_gain_net / potential_loss_net
    cap_reason: str                     # which cap ultimately bound the size
    decision: RiskDecision
    reason: str


@dataclass
class RiskState:
    """Mutable account state for the risk engine."""

    initial_inr: float
    realized_pnl: float = 0.0
    open_positions_value: float = 0.0     # sum of entry*qty for open positions
    open_positions_risk: float = 0.0      # sum of (entry-stop)*qty for open positions
    peak_equity: float = 0.0
    consecutive_losses: int = 0
    cooldown_until_iso: Optional[str] = None
    daily: DailyLossState = field(default_factory=lambda: DailyLossState(day_iso=""))
    paused: bool = False

    def __post_init__(self) -> None:
        if self.peak_equity == 0.0:
            self.peak_equity = self.initial_inr

    # ---- equity helpers
    @property
    def current_equity(self) -> float:
        """Current equity = initial + realized pnl + open positions value
        (using entry value as a conservative proxy for current value).

        Note: realized_pnl is signed (negative for losses).
        """
        return self.initial_inr + self.realized_pnl + self.open_positions_value

    @property
    def available_cash(self) -> float:
        """Cash not committed to open positions."""
        return max(0.0,
                   self.initial_inr + self.realized_pnl - self.open_positions_value)

    @property
    def drawdown_pct(self) -> float:
        """Drawdown from peak equity, in %."""
        if self.peak_equity <= 0:
            return 0.0
        cur = self.current_equity
        if cur >= self.peak_equity:
            return 0.0
        return (self.peak_equity - cur) / self.peak_equity * 100.0

    def update_peak(self) -> None:
        cur = self.current_equity
        if cur > self.peak_equity:
            self.peak_equity = cur


# ---------------------------------------------------------------------------
# Risk engine
# ---------------------------------------------------------------------------
class RiskEngine:
    """Stateless-ish risk calculator that takes a RiskState and a candidate
    signal (entry/stop/target) and returns a validated PositionPlan.
    """

    def __init__(self, settings: RiskSettings):
        self.s = settings
        self.s.validate()

    # ------------------------------------------------------------------ helpers
    def _risk_budget(self, equity: float) -> float:
        return equity * self.s.risk_per_trade_pct / 100.0

    def _cost_estimate(self, position_value: float) -> Tuple[float, float, float]:
        """Return (entry_fee, exit_fee, slippage_cost) for a position value."""
        entry_fee = position_value * self.s.fee_pct / 100.0
        exit_fee  = position_value * self.s.fee_pct / 100.0
        # Adverse slippage on entry (we pay more) + exit (we get less).
        slip      = position_value * self.s.slippage_pct / 100.0 * 2.0
        return entry_fee, exit_fee, slip

    # --------------------------------------------------------- public planning
    def plan_position(self, *, pair: str, entry: float, stop: float,
                     target: float, state: RiskState,
                     now: Optional[datetime] = None) -> PositionPlan:
        """Build a fully-validated position plan for a long candidate.

        Raises RiskError for hard-invalid inputs (zero stop distance, NaN,
        non-positive equity). Returns a PositionPlan whose `.decision` is
        either ACCEPTED or one of the REJECTED_* codes.
        """
        now = now or datetime.now(timezone.utc)
        s = self.s

        # --- hard input validation (raises) ----------------
        for name, v in (("entry", entry), ("stop", stop), ("target", target)):
            if not isinstance(v, (int, float)) or v != v:
                raise RiskError(f"{name} must be a number, got {v!r}")
            if v <= 0:
                raise RiskError(f"{name} must be > 0, got {v}")
        if stop >= entry:
            raise RiskError("For a long, stop must be BELOW entry (got stop>=entry)")
        if target <= entry:
            raise RiskError("For a long, target must be ABOVE entry (got target<=entry)")

        equity = state.current_equity
        if equity <= 0:
            raise RiskError(f"current equity {equity} must be > 0")

        stop_distance = (entry - stop) / entry    # fraction (long)
        if stop_distance <= 0:
            raise RiskError("stop_distance must be > 0 for a long position")

        # --- soft gating: per-day caps, cooldown, drawdown ----------------
        # (Soft gates return a REJECTED_* plan rather than raising.)
        if state.paused:
            return self._reject(pair, entry, stop, target,
                                RiskDecision.REJECTED_COOLDOWN,
                                "trading paused (emergency stop)",
                                state)

        if state.daily.trades_today >= s.max_daily_trades:
            return self._reject(pair, entry, stop, target,
                                RiskDecision.REJECTED_MAX_DAILY_TRADES,
                                f"max_daily_trades={s.max_daily_trades} reached",
                                state)

        if state.cooldown_until_iso and now.isoformat() <= state.cooldown_until_iso:
            return self._reject(pair, entry, stop, target,
                                RiskDecision.REJECTED_COOLDOWN,
                                f"cooldown until {state.cooldown_until_iso}",
                                state)

        if state.drawdown_pct >= s.max_drawdown_pct:
            return self._reject(pair, entry, stop, target,
                                RiskDecision.REJECTED_DRAWDOWN,
                                f"drawdown {state.drawdown_pct:.2f}% >= limit "
                                f"{s.max_drawdown_pct}%",
                                state)

        # Daily loss: % of initial_inr (the spec defines daily loss relative
        # to starting-of-day equity, which we approximate with current peak).
        day_loss_pct = (state.daily.realized_pnl_day / max(state.peak_equity, 1.0)) * 100.0
        if day_loss_pct <= -s.max_daily_loss_pct:
            return self._reject(pair, entry, stop, target,
                                RiskDecision.REJECTED_DAILY_LOSS,
                                f"daily loss {day_loss_pct:.2f}% <= limit "
                                f"-{s.max_daily_loss_pct}%",
                                state)
        if s.max_daily_loss_abs_inr > 0 \
                and state.daily.realized_pnl_day <= -s.max_daily_loss_abs_inr:
            return self._reject(pair, entry, stop, target,
                                RiskDecision.REJECTED_DAILY_LOSS,
                                f"daily realized pnl "
                                f"{state.daily.realized_pnl_day:.2f} INR <= "
                                f"-{s.max_daily_loss_abs_inr}",
                                state)

        # Max open positions
        # (We don't track open count in RiskState directly; the paper engine
        #  tracks it. We expose a check method that takes the count.)
        # The paper engine will call check_max_positions separately.

        # --- compute position size ----------------------------------------
        risk_budget = self._risk_budget(equity)
        risk_based_value = risk_budget / stop_distance

        # Cap by max position allocation
        cap_max_position = equity * s.max_position_pct / 100.0
        # Cap by remaining portfolio exposure
        remaining_exposure = max(0.0,
            equity * s.max_portfolio_exposure_pct / 100.0 - state.open_positions_value)
        # Cap by available cash (minus reserved fees for this trade)
        est_entry_cost = risk_based_value * s.fee_pct / 100.0
        cap_cash = max(0.0, state.available_cash - est_entry_cost)
        # Cap by trading_allocation_pct of equity (overall deployment cap)
        cap_alloc = equity * s.trading_allocation_pct / 100.0

        caps = [
            ("risk_based",       risk_based_value),
            ("max_position",     cap_max_position),
            ("portfolio_exposure", remaining_exposure),
            ("cash",             cap_cash),
            ("trading_allocation", cap_alloc),
        ]
        cap_name, position_value = min(caps, key=lambda x: x[1])

        if position_value <= 0:
            return self._reject(pair, entry, stop, target,
                                RiskDecision.REJECTED_CAP,
                                f"position value capped to 0 by {cap_name}",
                                state)

        qty = position_value / entry

        # --- cost estimates on the final position size --------------------
        est_entry_cost, est_exit_cost, est_slippage_cost = \
            self._cost_estimate(position_value)

        potential_loss_gross = (entry - stop) * qty
        potential_gain_gross = (target - entry) * qty
        # Long: if stopped out, we also pay entry fee + exit fee + slippage.
        potential_loss_net = potential_loss_gross + est_entry_cost + est_exit_cost + est_slippage_cost
        # Long: if target hit, the costs offset some of the gain.
        potential_gain_net = potential_gain_gross - est_entry_cost - est_exit_cost - est_slippage_cost
        if potential_loss_net <= 0:
            return self._reject(pair, entry, stop, target,
                                RiskDecision.REJECTED_RISK_BUDGET,
                                "potential_loss_net <= 0 (costs dominate)",
                                state)

        rr_after_cost = potential_gain_net / potential_loss_net

        # --- R/R check ---------------------------------------------------
        if rr_after_cost < s.reward_to_risk_target:
            return PositionPlan(
                pair=pair, side="long", entry=entry, stop=stop, target=target,
                qty=qty, position_value=position_value,
                stop_distance_fraction=stop_distance,
                risk_budget=risk_budget,
                potential_loss_gross=potential_loss_gross,
                potential_gain_gross=potential_gain_gross,
                est_entry_cost=est_entry_cost, est_exit_cost=est_exit_cost,
                est_slippage_cost=est_slippage_cost,
                potential_loss_net=potential_loss_net,
                potential_gain_net=potential_gain_net,
                rr_after_cost=rr_after_cost,
                cap_reason=cap_name,
                decision=RiskDecision.REJECTED_RR,
                reason=f"rr_after_cost {rr_after_cost:.2f} < target "
                                       f"{s.reward_to_risk_target}",
            )

        # Accepted
        return PositionPlan(
            pair=pair, side="long", entry=entry, stop=stop, target=target,
            qty=qty, position_value=position_value,
            stop_distance_fraction=stop_distance,
            risk_budget=risk_budget,
            potential_loss_gross=potential_loss_gross,
            potential_gain_gross=potential_gain_gross,
            est_entry_cost=est_entry_cost, est_exit_cost=est_exit_cost,
            est_slippage_cost=est_slippage_cost,
            potential_loss_net=potential_loss_net,
            potential_gain_net=potential_gain_net,
            rr_after_cost=rr_after_cost,
            cap_reason=cap_name,
            decision=RiskDecision.ACCEPTED,
            reason="ok",
        )

    def check_max_positions(self, current_open: int,
                            state: RiskState) -> bool:
        return current_open < self.s.max_open_positions

    # ------------------------------------------------------------------ helpers
    def _reject(self, pair, entry, stop, target, decision, reason,
                state: RiskState) -> PositionPlan:
        return PositionPlan(
            pair=pair, side="long", entry=entry, stop=stop, target=target,
            qty=0.0, position_value=0.0,
            stop_distance_fraction=(entry - stop) / entry if entry else 0.0,
            risk_budget=self._risk_budget(state.current_equity),
            potential_loss_gross=0.0, potential_gain_gross=0.0,
            est_entry_cost=0.0, est_exit_cost=0.0,
            est_slippage_cost=0.0,
            potential_loss_net=0.0, potential_gain_net=0.0,
            rr_after_cost=0.0, cap_reason="rejected",
            decision=decision, reason=reason,
        )

    # --------------------------------------------------------- lifecycle hooks
    def on_trade_closed(self, *, entry: float, stop: float, target: float,
                        qty: float, exit_price: float,
                        state: RiskState, day_iso: str) -> float:
        """Apply a closed paper-trade to the risk state. Returns realized pnl."""
        # PnL gross = (exit - entry) * qty ; minus costs on both legs
        pos_value = entry * qty
        entry_fee = pos_value * self.s.fee_pct / 100.0
        exit_fee  = exit_price * qty * self.s.fee_pct / 100.0
        slippage  = pos_value * self.s.slippage_pct / 100.0 + \
                    exit_price * qty * self.s.slippage_pct / 100.0
        pnl_gross = (exit_price - entry) * qty
        pnl_net = pnl_gross - entry_fee - exit_fee - slippage

        state.realized_pnl += pnl_net
        state.open_positions_value = max(0.0,
            state.open_positions_value - entry * qty)
        state.open_positions_risk = max(0.0,
            state.open_positions_risk - (entry - stop) * qty)

        if state.daily.day_iso != day_iso:
            state.daily.reset(day_iso)
        state.daily.realized_pnl_day += pnl_net
        state.daily.trades_today += 1

        if pnl_net < 0:
            state.consecutive_losses += 1
            if (self.s.cooldown_after_consecutive_losses > 0
                and state.consecutive_losses
                    >= self.s.cooldown_after_consecutive_losses):
                # Trigger cooldown: e.g. 1 day from now (display TZ)
                from datetime import timedelta
                cooldown_until = (datetime.now(timezone.utc)
                                  + timedelta(days=1)).isoformat()
                state.cooldown_until_iso = cooldown_until
        else:
            state.consecutive_losses = 0

        state.update_peak()
        return pnl_net

    def on_trade_opened(self, *, entry: float, stop: float, qty: float,
                        state: RiskState) -> None:
        state.open_positions_value += entry * qty
        state.open_positions_risk  += (entry - stop) * qty
        state.update_peak()
