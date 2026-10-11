"""POST /api/signals

Body: {"pair": "B-BTCINR", "interval": "1h", "candles": [...]}

Or GET /api/signals?pair=B-BTCINR&interval=1h (auto-fetches candles).

Returns the signals produced by every enabled strategy for the given
pair / interval. Signals are research outputs, NOT guaranteed
profitable recommendations.
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
        try:
            from coindcx_lab.server.common import parse_query, current_user_id
            from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
            from coindcx_lab.engine.risk import RiskSettings, RiskState, DailyLossState
            from coindcx_lab.engine.signals import SignalEngine
            from datetime import datetime, timezone

            pair = None
            interval = "1h"
            candles_arg = None

            if method == "GET":
                q = parse_query(self.path)
                pair = q.get("pair")
                interval = q.get("interval", "1h")
            elif method == "POST":
                body, err = read_json_body(self)
                if err:
                    send_error(self, 400, "bad_request", err)
                    return
                if body is None:
                    send_error(self, 400, "bad_request", "empty body")
                    return
                pair = body.get("pair")
                interval = body.get("interval", "1h")
                candles_arg = body.get("candles")

            if not pair:
                send_error(self, 400, "bad_request", "pair is required")
                return

            # Build candles: either from the request body, or fetch live/sim
            if candles_arg:
                candles = [
                    Candle(
                        open_time_ms=c["open_time_ms"],
                        open=float(c["open"]), high=float(c["high"]),
                        low=float(c["low"]), close=float(c["close"]),
                        volume=float(c["volume"]),
                        close_time_ms=c["close_time_ms"],
                        complete=bool(c.get("complete", True)),
                    )
                    for c in candles_arg
                ]
                prov = DataProvenance(
                    source=SourceKind.SIMULATED,
                    endpoint="caller-provided",
                    fetched_at_utc=datetime.now(timezone.utc),
                    note="candles supplied in request body",
                )
            else:
                # Reuse the candles endpoint logic
                from api.candles import handler as CandlesHandler
                # We can't easily reuse the handler class. Instead, fetch
                # candles directly via the CoinDCXClient + synth fallback.
                from coindcx_lab.api.client import CoinDCXClient, CoinDCXAPIError
                from coindcx_lab.api.synthetic import SyntheticGenerator
                base_price = {
                    "B-BTCINR": 7_000_000, "B-ETHINR": 350_000,
                    "B-SOLINR": 18_000, "B-MATICINR": 90,
                    "B-DOTINR": 700, "B-LTCINR": 8_000,
                    "B-XRPINR": 65, "B-LINKINR": 1_800,
                    "B-AVAXINR": 4_000,
                    "B-ADA INR".replace(" ", ""): 65,
                }.get(pair, 1000)
                client = CoinDCXClient(timeout=8, max_retries=1)
                try:
                    candles, prov = client.candles(pair=pair, interval=interval,
                                                       limit=300)
                except CoinDCXAPIError:
                    synth = SyntheticGenerator(seed=42)
                    candles, prov = synth.series(pair=pair, interval=interval,
                                                   count=300, start_price=base_price)

            # Build settings + state
            settings = _settings_from_request_or_env()
            state = RiskState(initial_inr=settings.initial_inr)
            state.daily = DailyLossState(
                day_iso=datetime.now(timezone.utc).strftime("%Y-%m-%d"))

            engine = SignalEngine(settings=settings)
            sigs = engine.evaluate(
                pair=pair, interval=interval, candles=candles,
                provenance=prov, risk_state=state)

            send_json(self, 200, {
                "pair": pair, "interval": interval,
                "signals": [s.to_dict() for s in sigs],
                "provenance": {
                    "source": prov.source.value,
                    "endpoint": prov.endpoint,
                    "fetched_at_utc": prov.fetched_at_utc.isoformat(),
                    "label": prov.label(),
                    "note": prov.note,
                },
                "settings": _settings_dict(settings),
                "state": {
                    "initial_equity": state.initial_inr,
                    "current_equity": state.current_equity,
                    "available_cash": state.available_cash,
                    "drawdown_pct": state.drawdown_pct,
                },
            })
        except ValueError as e:
            send_error(self, 400, "bad_request", str(e))
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):  self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")


def _settings_from_request_or_env() -> "RiskSettings":
    """Load settings from Supabase if available, else defaults."""
    from coindcx_lab.engine.risk import RiskSettings
    try:
        from coindcx_lab.api.env import SupabaseConfig
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = SupabaseConfig.from_env()
        store = SupabaseStore(config=cfg)
        loaded = store.load_settings("risk")
        if loaded:
            return RiskSettings(**loaded)
    except Exception:
        pass
    return RiskSettings()


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
