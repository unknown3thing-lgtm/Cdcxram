"""GET /api/settings   POST /api/settings

GET  → load current settings from Supabase user_settings (or defaults).
POST → validate + save settings to Supabase user_settings.

Capital presets: ₹2,000 / ₹5,000 / ₹10,000 / ₹25,000 / ₹50,000 / Custom.

Validation rules (reject with HTTP 400):
  - initial_inr: must be > 0 and <= 10,000,000 (₹1 crore)
  - risk_per_trade_pct: must be > 0 and <= 5.0
  - max_position_pct: must be > 0 and <= 100
  - max_portfolio_exposure_pct: must be > 0 and <= 100
  - max_daily_loss_pct: must be > 0 and <= 100
  - max_drawdown_pct: must be > 0 and <= 100
  - reward_to_risk_target: must be > 0
  - fee_pct: must be >= 0 and <= 5
  - spread_pct: must be >= 0 and <= 5
  - slippage_pct: must be >= 0 and <= 5
  - min_24h_volume_inr: must be >= 0
  - max_open_positions: must be >= 1 and <= 20
  - max_daily_trades: must be >= 1 and <= 100
  - cooldown_after_consecutive_losses: must be >= 0 and <= 20

Changing settings does NOT reset the paper portfolio or rewrite
historical trades. The new settings apply to FUTURE entries only.
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


CAPITAL_PRESETS = [2000, 5000, 10000, 25000, 50000]
MAX_CAPITAL_INR = 10_000_000  # ₹1 crore
MAX_RISK_PCT = 5.0
MAX_FEE_PCT = 5.0


def _validate_settings(body: dict) -> tuple:
    """Validate a settings dict. Returns (settings_dict, error_msg)."""
    errors = []
    validated = {}

    # initial_inr
    try:
        cap = float(body.get("initial_inr", 2000))
        if cap <= 0:
            errors.append("initial_inr must be > 0")
        elif cap > MAX_CAPITAL_INR:
            errors.append(f"initial_inr must be <= {MAX_CAPITAL_INR} (₹1 crore)")
        else:
            validated["initial_inr"] = cap
    except (TypeError, ValueError):
        errors.append("initial_inr must be a number")

    # risk_per_trade_pct
    try:
        r = float(body.get("risk_per_trade_pct", 1.0))
        if r <= 0 or r > MAX_RISK_PCT:
            errors.append(f"risk_per_trade_pct must be in (0, {MAX_RISK_PCT}]")
        else:
            validated["risk_per_trade_pct"] = r
    except (TypeError, ValueError):
        errors.append("risk_per_trade_pct must be a number")

    # max_position_pct
    try:
        v = float(body.get("max_position_pct", 25.0))
        if v <= 0 or v > 100:
            errors.append("max_position_pct must be in (0, 100]")
        else:
            validated["max_position_pct"] = v
    except (TypeError, ValueError):
        errors.append("max_position_pct must be a number")

    # max_portfolio_exposure_pct
    try:
        v = float(body.get("max_portfolio_exposure_pct", 80.0))
        if v <= 0 or v > 100:
            errors.append("max_portfolio_exposure_pct must be in (0, 100]")
        else:
            validated["max_portfolio_exposure_pct"] = v
    except (TypeError, ValueError):
        errors.append("max_portfolio_exposure_pct must be a number")

    # max_daily_loss_pct
    try:
        v = float(body.get("max_daily_loss_pct", 5.0))
        if v <= 0 or v > 100:
            errors.append("max_daily_loss_pct must be in (0, 100]")
        else:
            validated["max_daily_loss_pct"] = v
    except (TypeError, ValueError):
        errors.append("max_daily_loss_pct must be a number")

    # max_drawdown_pct
    try:
        v = float(body.get("max_drawdown_pct", 15.0))
        if v <= 0 or v > 100:
            errors.append("max_drawdown_pct must be in (0, 100]")
        else:
            validated["max_drawdown_pct"] = v
    except (TypeError, ValueError):
        errors.append("max_drawdown_pct must be a number")

    # reward_to_risk_target
    try:
        v = float(body.get("reward_to_risk_target", 2.0))
        if v <= 0:
            errors.append("reward_to_risk_target must be > 0")
        else:
            validated["reward_to_risk_target"] = v
    except (TypeError, ValueError):
        errors.append("reward_to_risk_target must be a number")

    # fee_pct
    try:
        v = float(body.get("fee_pct", 0.1))
        if v < 0 or v > MAX_FEE_PCT:
            errors.append(f"fee_pct must be in [0, {MAX_FEE_PCT}]")
        else:
            validated["fee_pct"] = v
    except (TypeError, ValueError):
        errors.append("fee_pct must be a number")

    # spread_pct
    try:
        v = float(body.get("spread_pct", 0.05))
        if v < 0 or v > MAX_FEE_PCT:
            errors.append(f"spread_pct must be in [0, {MAX_FEE_PCT}]")
        else:
            validated["spread_pct"] = v
    except (TypeError, ValueError):
        errors.append("spread_pct must be a number")

    # slippage_pct
    try:
        v = float(body.get("slippage_pct", 0.05))
        if v < 0 or v > MAX_FEE_PCT:
            errors.append(f"slippage_pct must be in [0, {MAX_FEE_PCT}]")
        else:
            validated["slippage_pct"] = v
    except (TypeError, ValueError):
        errors.append("slippage_pct must be a number")

    # min_24h_volume_inr
    try:
        v = float(body.get("min_24h_volume_inr", 5_000_000))
        if v < 0:
            errors.append("min_24h_volume_inr must be >= 0")
        else:
            validated["min_24h_volume_inr"] = v
    except (TypeError, ValueError):
        errors.append("min_24h_volume_inr must be a number")

    # max_open_positions
    try:
        v = int(body.get("max_open_positions", 5))
        if v < 1 or v > 20:
            errors.append("max_open_positions must be in [1, 20]")
        else:
            validated["max_open_positions"] = v
    except (TypeError, ValueError):
        errors.append("max_open_positions must be an integer")

    # max_daily_trades
    try:
        v = int(body.get("max_daily_trades", 10))
        if v < 1 or v > 100:
            errors.append("max_daily_trades must be in [1, 100]")
        else:
            validated["max_daily_trades"] = v
    except (TypeError, ValueError):
        errors.append("max_daily_trades must be an integer")

    # cooldown_after_consecutive_losses
    try:
        v = int(body.get("cooldown_after_consecutive_losses", 3))
        if v < 0 or v > 20:
            errors.append("cooldown_after_consecutive_losses must be in [0, 20]")
        else:
            validated["cooldown_after_consecutive_losses"] = v
    except (TypeError, ValueError):
        errors.append("cooldown_after_consecutive_losses must be an integer")

    # allow_overnight
    validated["allow_overnight"] = bool(body.get("allow_overnight", True))

    # trading_allocation_pct
    try:
        v = float(body.get("trading_allocation_pct", 100.0))
        if v <= 0 or v > 100:
            errors.append("trading_allocation_pct must be in (0, 100]")
        else:
            validated["trading_allocation_pct"] = v
    except (TypeError, ValueError):
        errors.append("trading_allocation_pct must be a number")

    if errors:
        return None, "; ".join(errors)
    return validated, None


class handler(BaseHTTPRequestHandler):

    def _handle(self, method: str) -> None:
        if handle_preflight(method, self):
            return
        try:
            from _common import current_user_id
            from coindcx_lab.api.env import SupabaseConfig, ConfigError
            from coindcx_lab.db.supabase_store import SupabaseStore
            from coindcx_lab.engine.risk import RiskSettings

            try:
                cfg = SupabaseConfig.from_env()
                store = SupabaseStore(config=cfg, user_id=current_user_id(self))
            except ConfigError as e:
                send_error(self, 500, "config_error",
                           f"Supabase env not configured: {e}")
                return

            if method == "GET":
                # Load settings from Supabase, or return defaults
                loaded = store.load_settings("risk")
                if loaded:
                    settings = RiskSettings(**loaded)
                else:
                    settings = RiskSettings()
                send_json(self, 200, {
                    "settings": {
                        "initial_inr": settings.initial_inr,
                        "risk_per_trade_pct": settings.risk_per_trade_pct,
                        "max_position_pct": settings.max_position_pct,
                        "max_portfolio_exposure_pct": settings.max_portfolio_exposure_pct,
                        "max_daily_loss_pct": settings.max_daily_loss_pct,
                        "max_daily_loss_abs_inr": settings.max_daily_loss_abs_inr,
                        "max_drawdown_pct": settings.max_drawdown_pct,
                        "max_open_positions": settings.max_open_positions,
                        "max_daily_trades": settings.max_daily_trades,
                        "reward_to_risk_target": settings.reward_to_risk_target,
                        "cooldown_after_consecutive_losses": settings.cooldown_after_consecutive_losses,
                        "allow_overnight": settings.allow_overnight,
                        "fee_pct": settings.fee_pct,
                        "spread_pct": settings.spread_pct,
                        "slippage_pct": settings.slippage_pct,
                        "min_24h_volume_inr": settings.min_24h_volume_inr,
                        "min_24h_trades": settings.min_24h_trades,
                        "trading_allocation_pct": settings.trading_allocation_pct,
                    },
                    "presets": CAPITAL_PRESETS,
                    "max_capital_inr": MAX_CAPITAL_INR,
                    "note": ("Changing settings does NOT reset the paper "
                             "portfolio or rewrite historical trades. "
                             "New settings apply to FUTURE entries only."),
                })
                return

            if method == "POST":
                body, err = read_json_body(self)
                if err:
                    send_error(self, 400, "bad_request", err)
                    return
                if body is None:
                    body = {}
                validated, v_err = _validate_settings(body)
                if v_err:
                    send_error(self, 400, "validation_error", v_err)
                    return
                # Save to Supabase (upsert on user_id)
                store.save_settings("risk", validated)
                send_json(self, 200, {
                    "status": "ok",
                    "settings": validated,
                    "presets": CAPITAL_PRESETS,
                    "note": ("Settings saved. Existing paper trades and "
                             "portfolio history are preserved. New settings "
                             "apply to FUTURE entries only."),
                })
                return

            send_error(self, 405, "method_not_allowed",
                       f"Method {method} not supported on /api/settings")
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):  self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")
