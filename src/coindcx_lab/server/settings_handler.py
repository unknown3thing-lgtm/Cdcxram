"""Settings handler logic — moved from api/settings.py to avoid
creating an extra Vercel serverless function.

Called by api/health.py when the request path contains 'action=settings'.
"""

from __future__ import annotations

from typing import Tuple


CAPITAL_PRESETS = [2000, 5000, 10000, 25000, 50000]
MAX_CAPITAL_INR = 10_000_000  # ₹1 crore
MAX_RISK_PCT = 5.0
MAX_FEE_PCT = 5.0


def _validate_settings(body: dict) -> Tuple[dict, str]:
    """Validate a settings dict. Returns (settings_dict, error_msg)."""
    errors = []
    validated = {}

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

    try:
        r = float(body.get("risk_per_trade_pct", 1.0))
        if r <= 0 or r > MAX_RISK_PCT:
            errors.append(f"risk_per_trade_pct must be in (0, {MAX_RISK_PCT}]")
        else:
            validated["risk_per_trade_pct"] = r
    except (TypeError, ValueError):
        errors.append("risk_per_trade_pct must be a number")

    for field, default, lo, hi in [
        ("max_position_pct", 25.0, 0, 100),
        ("max_portfolio_exposure_pct", 80.0, 0, 100),
        ("max_daily_loss_pct", 5.0, 0, 100),
        ("max_drawdown_pct", 15.0, 0, 100),
    ]:
        try:
            v = float(body.get(field, default))
            if v <= 0 or v > hi:
                errors.append(f"{field} must be in (0, {hi}]")
            else:
                validated[field] = v
        except (TypeError, ValueError):
            errors.append(f"{field} must be a number")

    try:
        v = float(body.get("reward_to_risk_target", 2.0))
        if v <= 0:
            errors.append("reward_to_risk_target must be > 0")
        else:
            validated["reward_to_risk_target"] = v
    except (TypeError, ValueError):
        errors.append("reward_to_risk_target must be a number")

    for field, default, lo, hi in [
        ("fee_pct", 0.1, 0, MAX_FEE_PCT),
        ("spread_pct", 0.05, 0, MAX_FEE_PCT),
        ("slippage_pct", 0.05, 0, MAX_FEE_PCT),
    ]:
        try:
            v = float(body.get(field, default))
            if v < 0 or v > hi:
                errors.append(f"{field} must be in [0, {hi}]")
            else:
                validated[field] = v
        except (TypeError, ValueError):
            errors.append(f"{field} must be a number")

    try:
        v = float(body.get("min_24h_volume_inr", 5_000_000))
        if v < 0:
            errors.append("min_24h_volume_inr must be >= 0")
        else:
            validated["min_24h_volume_inr"] = v
    except (TypeError, ValueError):
        errors.append("min_24h_volume_inr must be a number")

    # max_spread_pct — maximum bid-ask spread for scanner filter
    try:
        v = float(body.get("max_spread_pct", 1.0))
        if v < 0 or v > 10:
            errors.append("max_spread_pct must be in [0, 10]")
        else:
            validated["max_spread_pct"] = v
    except (TypeError, ValueError):
        errors.append("max_spread_pct must be a number")

    for field, default, lo, hi in [
        ("max_open_positions", 5, 1, 20),
        ("max_daily_trades", 10, 1, 100),
        ("cooldown_after_consecutive_losses", 3, 0, 20),
    ]:
        try:
            v = int(body.get(field, default))
            if v < lo or v > hi:
                errors.append(f"{field} must be in [{lo}, {hi}]")
            else:
                validated[field] = v
        except (TypeError, ValueError):
            errors.append(f"{field} must be an integer")

    validated["allow_overnight"] = bool(body.get("allow_overnight", True))

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


def handle_settings(request, method: str) -> bool:
    """Handle GET/POST /api/settings. Returns True if handled, False if
    the caller should fall through to the default health handler."""
    from coindcx_lab.server.common import (
        send_error, send_json, read_json_body, current_user_id,
    )
    from coindcx_lab.api.env import SupabaseConfig, ConfigError
    from coindcx_lab.db.supabase_store import SupabaseStore
    from coindcx_lab.engine.risk import RiskSettings

    try:
        cfg = SupabaseConfig.from_env()
        store = SupabaseStore(config=cfg, user_id=current_user_id(request))
    except ConfigError as e:
        send_error(request, 500, "config_error", f"Supabase env not configured: {e}")
        return True

    if method == "GET":
        loaded = store.load_settings("risk")
        settings = RiskSettings(**loaded) if loaded else RiskSettings()
        send_json(request, 200, {
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
                "max_spread_pct": settings.max_spread_pct,
                "trading_allocation_pct": settings.trading_allocation_pct,
            },
            "presets": CAPITAL_PRESETS,
            "max_capital_inr": MAX_CAPITAL_INR,
            "note": ("Changing settings does NOT reset the paper "
                     "portfolio or rewrite historical trades. "
                     "New settings apply to FUTURE entries only."),
        })
        return True

    if method == "POST":
        body, err = read_json_body(request)
        if err:
            send_error(request, 400, "bad_request", err)
            return True
        if body is None:
            body = {}
        validated, v_err = _validate_settings(body)
        if v_err:
            send_error(request, 400, "validation_error", v_err)
            return True
        store.save_settings("risk", validated)
        send_json(request, 200, {
            "status": "ok",
            "settings": validated,
            "presets": CAPITAL_PRESETS,
            "note": ("Settings saved. Existing paper trades and "
                     "portfolio history are preserved. New settings "
                     "apply to FUTURE entries only."),
        })
        return True

    return False
