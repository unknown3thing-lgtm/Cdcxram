"""POST /api/risk

Body: {"entry": 100, "stop": 98, "target": 104, "pair": "B-BTCINR",
       "initial_inr": 2000, ...optional overrides}

Returns the validated position plan: position_value, qty, R/R after
cost, cap reason, decision.
"""

import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from http.server import BaseHTTPRequestHandler

from coindcx_lab.server.common import (
    handle_preflight, read_json_body, send_error, send_json,
)


class handler(BaseHTTPRequestHandler):

    def _handle(self, method: str) -> None:
        if handle_preflight(method, self):
            return
        if method != "POST":
            send_error(self, 405, "method_not_allowed",
                       "Use POST with a JSON body")
            return
        try:
            body, err = read_json_body(self)
            if err:
                send_error(self, 400, "bad_request", err)
                return
            if body is None:
                send_error(self, 400, "bad_request", "empty body")
                return
            for k in ("entry", "stop", "target"):
                if k not in body:
                    send_error(self, 400, "bad_request",
                               f"{k} is required")
                    return
            from coindcx_lab.engine.risk import (
                RiskEngine, RiskError, RiskSettings, RiskState, DailyLossState,
            )
            from datetime import datetime, timezone

            # Build settings: from request overrides or Supabase load or defaults.
            settings = _settings_from_request(body)
            state = RiskState(initial_inr=settings.initial_inr)
            state.daily = DailyLossState(
                day_iso=datetime.now(timezone.utc).strftime("%Y-%m-%d"))

            eng = RiskEngine(settings)
            try:
                plan = eng.plan_position(
                    pair=body.get("pair", "CALC"),
                    entry=float(body["entry"]),
                    stop=float(body["stop"]),
                    target=float(body["target"]),
                    state=state,
                )
            except RiskError as e:
                send_error(self, 400, "invalid_input", str(e))
                return

            send_json(self, 200, {
                "decision": plan.decision.value,
                "reason": plan.reason,
                "pair": plan.pair,
                "entry": plan.entry, "stop": plan.stop, "target": plan.target,
                "qty": plan.qty,
                "position_value": plan.position_value,
                "stop_distance_fraction": plan.stop_distance_fraction,
                "risk_budget": plan.risk_budget,
                "potential_loss_gross": plan.potential_loss_gross,
                "potential_gain_gross": plan.potential_gain_gross,
                "est_entry_cost": plan.est_entry_cost,
                "est_exit_cost": plan.est_exit_cost,
                "est_slippage_cost": plan.est_slippage_cost,
                "potential_loss_net": plan.potential_loss_net,
                "potential_gain_net": plan.potential_gain_net,
                "rr_after_cost": plan.rr_after_cost,
                "cap_reason": plan.cap_reason,
                "settings": _settings_dict(settings),
                "state": {
                    "initial_equity": state.initial_inr,
                    "current_equity": state.current_equity,
                    "available_cash": state.available_cash,
                    "risk_budget_per_trade":
                        state.current_equity * settings.risk_per_trade_pct / 100.0,
                    "drawdown_pct": state.drawdown_pct,
                },
            })
        except ValueError as e:
            send_error(self, 400, "bad_request", str(e))
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_POST(self):  self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")


def _settings_from_request(body: dict) -> "RiskSettings":
    """Load settings, applying per-request overrides if provided."""
    from coindcx_lab.engine.risk import RiskSettings
    try:
        from coindcx_lab.api.env import SupabaseConfig
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = SupabaseConfig.from_env()
        store = SupabaseStore(config=cfg)
        loaded = store.load_settings("risk")
        if loaded:
            base = loaded
        else:
            base = {}
    except Exception:
        base = {}
    # Apply per-request overrides
    for k in ("initial_inr", "risk_per_trade_pct", "max_position_pct",
              "max_portfolio_exposure_pct", "max_daily_loss_pct",
              "max_daily_loss_abs_inr", "max_drawdown_pct",
              "max_open_positions", "max_daily_trades",
              "reward_to_risk_target",
              "cooldown_after_consecutive_losses", "allow_overnight",
              "fee_pct", "spread_pct", "slippage_pct",
              "min_24h_volume_inr", "min_24h_trades",
              "trading_allocation_pct"):
        if k in body and body[k] is not None:
            base[k] = body[k]
    return RiskSettings(**base)


def _settings_dict(s) -> dict:
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
        "fee_pct": s.fee_pct,
        "spread_pct": s.spread_pct,
        "slippage_pct": s.slippage_pct,
    }
