"""Paper-trading engine.

Public interface:
    PaperTradingEngine(settings, store)
    .open_position(signal, current_price) -> PaperTrade | RiskError
    .check_exits(candles_by_pair) -> List[PaperTrade]
    .pause() / .resume()
    .reset(require_confirmation=True) -> bool
    .portfolio_summary() -> dict
    .list_trades() -> List[PaperTrade]

Storage:
    All state and trades persist in SQLite via the Store. Configuration
    changes never overwrite trade history; the engine re-applies new risk
    settings to FUTURE entries only.

Default starting capital is ₹2,000 unless the user changes it via the
dashboard's settings panel. NO real-money trading. NO authenticated API
calls.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

from ..api.models import Candle, DataProvenance, SourceKind
from ..db.store import Store
from .risk import (RiskEngine, RiskError, RiskSettings, RiskState,
                   DailyLossState, RiskDecision, PositionPlan)
from .signals import Signal, SignalState


class PaperTradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class PaperTrade:
    id: Optional[int]
    pair: str
    strategy: str
    side: str
    status: PaperTradeStatus
    qty: float
    entry: float
    stop: float
    target: float
    entry_time_ms: int
    exit_time_ms: Optional[int] = None
    exit_price: Optional[float] = None
    pnl_net: Optional[float] = None
    exit_reason: Optional[str] = None
    fee_inr: float = 0.0
    slippage_inr: float = 0.0
    signal_json: str = ""
    created_at_ms: int = 0

    @classmethod
    def from_row(cls, row: dict) -> "PaperTrade":
        return cls(
            id=row.get("id"),
            pair=row["pair"], strategy=row["strategy"],
            side=row["side"], status=PaperTradeStatus(row["status"]),
            qty=row["qty"], entry=row["entry"], stop=row["stop"],
            target=row["target"], entry_time_ms=row["entry_time_ms"],
            exit_time_ms=row.get("exit_time_ms"),
            exit_price=row.get("exit_price"),
            pnl_net=row.get("pnl_net"),
            exit_reason=row.get("exit_reason"),
            fee_inr=row.get("fee_inr") or 0.0,
            slippage_inr=row.get("slippage_inr") or 0.0,
            signal_json=row.get("signal_json") or "",
            created_at_ms=row.get("created_at_ms") or 0,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "pair": self.pair, "strategy": self.strategy,
            "side": self.side, "status": self.status.value,
            "qty": self.qty, "entry": self.entry, "stop": self.stop,
            "target": self.target,
            "entry_time_ms": self.entry_time_ms,
            "exit_time_ms": self.exit_time_ms,
            "exit_price": self.exit_price, "pnl_net": self.pnl_net,
            "exit_reason": self.exit_reason,
            "fee_inr": self.fee_inr, "slippage_inr": self.slippage_inr,
        }


# ---------------------------------------------------------------------------
class PaperTradingEngine:
    """Single shared paper-trading engine. Backed by SQLite."""

    DEFAULT_STARTING_CAPITAL = 2000.0

    def __init__(self, *, settings: RiskSettings, store: Store):
        self.settings = settings
        self.store = store
        self.risk_engine = RiskEngine(settings)

        # Load or initialise state
        blob = store.load_state()
        if blob is None:
            self.state = RiskState(initial_inr=settings.initial_inr)
            self.state.daily = DailyLossState(
                day_iso=_day_iso_from_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
            )
            self._persist_state()
        else:
            self.state = _state_from_blob(blob, settings)
        # Persist latest settings (does not touch history)
        store.save_settings("risk", _settings_to_dict(settings))

    # ---------------------------------------------------------- persistence
    def _persist_state(self) -> None:
        self.store.save_state(_state_to_blob(self.state))

    def reconcile_state(self) -> dict:
        """Reconcile paper_state with the actual trades in the database.

        This fixes inconsistencies caused by external modifications to
        the paper_trades table (e.g., direct DB deletes that bypass the
        PaperTradingEngine). It recalculates:
          - open_positions_value = sum of (entry * qty) for OPEN trades
          - open_positions_risk = sum of (entry - stop) * qty for OPEN trades
          - peak_equity = max of all equity_snapshots (or initial_inr)

        This does NOT delete trades, reset history, or change settings.
        It only corrects derived state values to match reality.
        """
        open_trades = self.store.get_open_trades()
        actual_opv = sum(float(t["entry"]) * float(t["qty"]) for t in open_trades)
        actual_opr = sum((float(t["entry"]) - float(t["stop"])) * float(t["qty"])
                         for t in open_trades)

        changes = {}
        if abs(self.state.open_positions_value - actual_opv) > 0.01:
            changes["open_positions_value"] = {
                "from": self.state.open_positions_value,
                "to": actual_opv}
            self.state.open_positions_value = actual_opv

        if abs(self.state.open_positions_risk - actual_opr) > 0.01:
            changes["open_positions_risk"] = {
                "from": self.state.open_positions_risk,
                "to": actual_opr}
            self.state.open_positions_risk = actual_opr

        # Recalculate peak_equity from snapshots
        snapshots = self.store.list_equity_snapshots(limit=1000)
        if snapshots:
            actual_peak = max(float(s["equity"]) for s in snapshots)
        else:
            actual_peak = self.state.initial_inr
        # Also consider current equity
        actual_peak = max(actual_peak, self.state.current_equity)
        if abs(self.state.peak_equity - actual_peak) > 0.01:
            changes["peak_equity"] = {
                "from": self.state.peak_equity,
                "to": actual_peak}
            self.state.peak_equity = actual_peak

        if changes:
            self._persist_state()

        return {"reconciled": bool(changes), "changes": changes,
                "open_trades_count": len(open_trades),
                "open_positions_value": actual_opv,
                "open_positions_risk": actual_opr}

    # ---------------------------------------------------------- queries
    def portfolio_summary(self) -> dict:
        st = self.state
        s = self.settings
        open_trades = self.store.get_open_trades()
        # Compute unrealized PnL over open trades using the LAST candle's
        # close from the latest signal evaluation. For a clean summary
        # without external data, we use entry vs current mark provided by
        # caller via update_marks(); if not, unrealized = 0.
        unrealized = 0.0
        for t in open_trades:
            qty = t["qty"]; entry = t["entry"]
            # No mark: leave unrealized = 0; the dashboard will pass marks.
            unrealized += 0.0
        return {
            "initial_equity": st.initial_inr,
            "current_equity": st.current_equity,
            "available_cash": st.available_cash,
            "open_positions_value": st.open_positions_value,
            "open_positions_count": len(open_trades),
            "unrealized_pnl": unrealized,
            "realized_pnl": st.realized_pnl,
            "peak_equity": st.peak_equity,
            "drawdown_pct": st.drawdown_pct,
            "risk_budget_per_trade": st.current_equity * s.risk_per_trade_pct / 100.0,
            "daily_realized_pnl": st.daily.realized_pnl_day,
            "daily_trades": st.daily.trades_today,
            "max_daily_trades": s.max_daily_trades,
            "max_daily_loss_pct": s.max_daily_loss_pct,
            "max_drawdown_pct": s.max_drawdown_pct,
            "max_open_positions": s.max_open_positions,
            "cooldown_until": st.cooldown_until_iso,
            "paused": st.paused,
            "consecutive_losses": st.consecutive_losses,
        }

    def list_trades(self, *, status: Optional[str] = None,
                    limit: int = 500) -> List[PaperTrade]:
        rows = self.store.list_trades(status=status, limit=limit)
        return [PaperTrade.from_row(r) for r in rows]

    # ---------------------------------------------------------- open
    def open_position(self, *, signal: Signal,
                     current_price: Optional[float] = None
                     ) -> Tuple[Optional[PaperTrade], str]:
        """Attempt to open a paper position from a Signal. Returns
        (trade_or_None, reason_str).
        """
        if signal.state is not SignalState.BUY_CANDIDATE:
            return None, f"signal not BUY_CANDIDATE (state={signal.state.value})"
        if signal.entry is None or signal.stop is None or signal.target is None:
            return None, "signal missing entry/stop/target"
        if self.state.paused:
            return None, "engine paused"

        entry = current_price or signal.entry
        # Apply adverse entry slippage
        entry_adjusted = entry * (1 + self.settings.slippage_pct / 100.0)
        stop, target = signal.stop, signal.target
        if stop >= entry_adjusted:
            return None, f"stop {stop} >= entry_adjusted {entry_adjusted}"

        # Check max open positions
        if (len(self.store.get_open_trades())
                >= self.settings.max_open_positions):
            return None, f"max_open_positions {self.settings.max_open_positions} reached"

        # Plan via risk engine
        try:
            plan = self.risk_engine.plan_position(
                pair=signal.pair, entry=entry_adjusted,
                stop=stop, target=target, state=self.state)
        except RiskError as e:
            return None, f"risk error: {e}"

        if plan.decision is not RiskDecision.ACCEPTED:
            return None, f"risk gate rejected: {plan.decision.value} ({plan.reason})"

        # Cash sufficiency
        required = plan.position_value + plan.est_entry_cost + plan.est_slippage_cost / 2
        if self.state.available_cash < required:
            return None, (f"insufficient cash: available "
                          f"{self.state.available_cash:.2f} < required "
                          f"{required:.2f}")

        # All checks passed — persist the trade.
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        trade = PaperTrade(
            id=None, pair=signal.pair, strategy=signal.strategy,
            side="long", status=PaperTradeStatus.OPEN,
            qty=plan.qty, entry=entry_adjusted, stop=stop, target=target,
            entry_time_ms=now_ms,
            fee_inr=plan.est_entry_cost,
            slippage_inr=plan.est_slippage_cost / 2,
            signal_json=json.dumps(signal.to_dict(), default=str),
            created_at_ms=now_ms,
        )
        row = trade.to_dict()
        row["status"] = trade.status.value
        row["created_at_ms"] = now_ms
        # Pass request_key from the signal into the row dict so that
        # _trade_to_row can include it as a dedicated column (migration 0004).
        row["request_key"] = signal.request_key
        trade_id = self.store.insert_trade(row)
        trade.id = trade_id

        # Update risk state
        self.risk_engine.on_trade_opened(
            entry=entry_adjusted, stop=stop, qty=plan.qty,
            state=self.state)
        self._persist_state()

        # Equity snapshot
        self.store.append_equity_snapshot(
            time_ms=now_ms, equity=self.state.current_equity,
            cash=self.state.available_cash,
            open_positions_value=self.state.open_positions_value,
            peak_equity=self.state.peak_equity,
            note=f"open {signal.pair} qty={plan.qty:.6f}",
        )
        return trade, "ok"

    # ---------------------------------------------------------- exit
    def check_exits(self, *,
                    candles_by_pair: Dict[str, List[Candle]],
                    assume_stop_first_on_ambiguous: bool = True
                    ) -> List[PaperTrade]:
        """For each open trade, check whether the latest candle hit the
        stop or target. If so, close the trade.

        Conservative: if both stop and target fall within the same bar's
        range, assume the stop hit first by default.
        """
        closed: List[PaperTrade] = []
        open_rows = self.store.get_open_trades()
        for row in open_rows:
            pair = row["pair"]
            if pair not in candles_by_pair:
                continue
            candles = candles_by_pair[pair]
            if not candles:
                continue
            # Use the most recent *completed* candle for the exit check.
            recent = candles[-1]
            # Incomplete latest: skip — we only act on closed candles.
            if not recent.complete and len(candles) >= 2:
                recent = candles[-2]
            stop, target = row["stop"], row["target"]
            hit_stop = recent.low <= stop
            hit_target = recent.high >= target
            if not (hit_stop or hit_target):
                continue
            if hit_stop and hit_target:
                if assume_stop_first_on_ambiguous:
                    exit_price, reason = stop, "stop"
                else:
                    exit_price, reason = target, "target"
            elif hit_stop:
                exit_price, reason = stop, "stop"
            else:
                exit_price, reason = target, "target"
            # Apply adverse exit slippage.
            exit_price_adjusted = exit_price * (1 - self.settings.slippage_pct / 100.0)
            pnl_net = self.risk_engine.on_trade_closed(
                entry=row["entry"], stop=stop, target=target,
                qty=row["qty"], exit_price=exit_price_adjusted,
                state=self.state,
                day_iso=_day_iso_from_ms(recent.open_time_ms),
            )
            exit_fee = exit_price_adjusted * row["qty"] * self.settings.fee_pct / 100.0
            exit_slip = exit_price * row["qty"] * self.settings.slippage_pct / 100.0
            self.store.update_trade_exit(
                trade_id=row["id"],
                exit_time_ms=recent.close_time_ms,
                exit_price=exit_price_adjusted,
                pnl_net=pnl_net,
                exit_reason=reason,
                fee_inr=row.get("fee_inr", 0) + exit_fee,
                slippage_inr=row.get("slippage_inr", 0) + exit_slip,
                status=PaperTradeStatus.CLOSED.value,
            )
            self._persist_state()
            self.store.append_equity_snapshot(
                time_ms=recent.close_time_ms,
                equity=self.state.current_equity,
                cash=self.state.available_cash,
                open_positions_value=self.state.open_positions_value,
                peak_equity=self.state.peak_equity,
                note=f"close {pair} reason={reason} pnl={pnl_net:.2f}",
            )
            # Refresh from DB
            refreshed = self.store.list_trades(limit=1)
            if refreshed:
                closed.append(PaperTrade.from_row(refreshed[0]))
        return closed

    def update_marks(self, marks: Dict[str, float]) -> float:
        """Update unrealized PnL based on latest marks per pair."""
        open_rows = self.store.get_open_trades()
        unrealized = 0.0
        for row in open_rows:
            mark = marks.get(row["pair"])
            if mark is None:
                continue
            unrealized += (mark - row["entry"]) * row["qty"]
        return unrealized

    # ---------------------------------------------------------- lifecycle
    def pause(self) -> None:
        self.state.paused = True
        self._persist_state()

    def resume(self) -> None:
        self.state.paused = False
        self._persist_state()

    def reset(self, require_confirmation: bool = True,
              confirmation_token: Optional[str] = None) -> bool:
        """DANGEROUS: wipes all trades, equity snapshots, and state.

        Returns True if reset was performed, False otherwise.
        Caller must pass confirmation_token='RESET-ALL-CONFIRMED' if
        require_confirmation is True.
        """
        if require_confirmation and confirmation_token != "RESET-ALL-CONFIRMED":
            return False
        self.store.reset_all_trades()
        self.state = RiskState(initial_inr=self.settings.initial_inr)
        self.state.daily = DailyLossState(
            day_iso=_day_iso_from_ms(int(datetime.now(timezone.utc).timestamp() * 1000))
        )
        self._persist_state()
        return True

    def update_settings(self, new_settings: RiskSettings) -> None:
        """Update risk settings for FUTURE trades only. History preserved."""
        self.settings = new_settings
        self.risk_engine = RiskEngine(new_settings)
        self.store.save_settings("risk", _settings_to_dict(new_settings))
        # Note: state.initial_inr is NOT changed — changing initial capital
        # mid-session is a separate explicit operation (handled by the
        # dashboard's "start new simulation" flow which creates a fresh
        # PaperTradingEngine with the new initial).

    # ---------------------------------------------------------- emergency stop
    def emergency_stop(self) -> None:
        """Hard pause that requires explicit resume."""
        self.state.paused = True
        self._persist_state()


# ---------------------------------------------------------------------------
def _day_iso_from_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _state_to_blob(state: RiskState) -> dict:
    return {
        "initial_inr": state.initial_inr,
        "realized_pnl": state.realized_pnl,
        "open_positions_value": state.open_positions_value,
        "open_positions_risk": state.open_positions_risk,
        "peak_equity": state.peak_equity,
        "consecutive_losses": state.consecutive_losses,
        "cooldown_until_iso": state.cooldown_until_iso,
        "paused": state.paused,
        "daily_day_iso": state.daily.day_iso,
        "daily_realized_pnl_day": state.daily.realized_pnl_day,
        "daily_trades_today": state.daily.trades_today,
    }


def _state_from_blob(blob: dict, settings: RiskSettings) -> RiskState:
    st = RiskState(
        initial_inr=float(blob.get("initial_inr", settings.initial_inr)),
        realized_pnl=float(blob.get("realized_pnl", 0.0)),
        open_positions_value=float(blob.get("open_positions_value", 0.0)),
        open_positions_risk=float(blob.get("open_positions_risk", 0.0)),
        peak_equity=float(blob.get("peak_equity",
                                   blob.get("initial_inr",
                                            settings.initial_inr))),
        consecutive_losses=int(blob.get("consecutive_losses", 0)),
        cooldown_until_iso=blob.get("cooldown_until_iso"),
        paused=bool(blob.get("paused", False)),
    )
    st.daily = DailyLossState(
        day_iso=blob.get("daily_day_iso", ""),
        realized_pnl_day=float(blob.get("daily_realized_pnl_day", 0.0)),
        trades_today=int(blob.get("daily_trades_today", 0)),
    )
    return st


def _settings_to_dict(s: RiskSettings) -> dict:
    return {
        "initial_inr": s.initial_inr,
        "risk_per_trade_pct": s.risk_per_trade_pct,
        "max_position_pct": s.max_position_pct,
        "max_portfolio_exposure_pct": s.max_portfolio_exposure_pct,
        "max_daily_loss_pct": s.max_daily_loss_pct,
        "max_daily_loss_abs_inr": s.max_daily_loss_abs_inr,
        "max_drawdown_pct": s.max_drawdown_pct,
        "max_open_positions": s.max_open_positions,
        "max_daily_trades": s.max_daily_trades,
        "reward_to_risk_target": s.reward_to_risk_target,
        "cooldown_after_consecutive_losses": s.cooldown_after_consecutive_losses,
        "allow_overnight": s.allow_overnight,
        "fee_pct": s.fee_pct,
        "spread_pct": s.spread_pct,
        "slippage_pct": s.slippage_pct,
        "min_24h_volume_inr": s.min_24h_volume_inr,
        "min_24h_trades": s.min_24h_trades,
        "max_spread_pct": s.max_spread_pct,
        "trading_allocation_pct": s.trading_allocation_pct,
    }
