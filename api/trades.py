"""GET /api/trades        — list paper trades (most recent first)
POST /api/trades        — open a new paper position from a Signal

GET  /api/trades?status=OPEN&limit=200
POST body: {
    "pair": "B-BTCINR",
    "entry": 100, "stop": 98, "target": 104,
    "strategy": "baseline_sma",
    "state": "BUY_CANDIDATE",
    "rr_after_cost": 2.0,
    "est_entry_cost_inr": 0.0, "est_exit_cost_inr": 0.0,
    "current_price": 100   # optional, defaults to entry
}

Returns:
    {"accepted": bool, "reason": str, "trade": {...} | null}
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
            from coindcx_lab.server.paper_common import build_paper_engine
            pe = build_paper_engine(self)
            if pe is None:
                return  # error already sent

            if method == "GET":
                from coindcx_lab.server.common import parse_query
                q = parse_query(self.path)
                status = q.get("status")
                try:
                    limit = int(q.get("limit", "200"))
                except ValueError:
                    limit = 200
                trades = pe.list_trades(status=status, limit=limit)
                send_json(self, 200, {
                    "trades": [t.to_dict() for t in trades],
                    "count": len(trades),
                })
                return
            if method == "POST":
                body, err = read_json_body(self)
                if err:
                    send_error(self, 400, "bad_request", err)
                    return
                if body is None:
                    send_error(self, 400, "bad_request", "empty body")
                    return

                # ---- Idempotency check ----
                # If the client sends a request_key (UUID), check if a trade
                # with that key already exists. If so, return the original
                # trade instead of creating a duplicate. This prevents
                # double-clicks, retries, and concurrent requests from
                # creating multiple trades for the same user intent.
                request_key = body.get("request_key")
                if request_key:
                    existing = pe.store.find_trade_by_request_key(request_key)
                    if existing is not None:
                        # Return the original trade — idempotent response
                        from coindcx_lab.engine.paper import PaperTrade, PaperTradeStatus
                        trade = PaperTrade(
                            id=existing.get("id"),
                            pair=existing.get("pair", ""),
                            strategy=existing.get("strategy", ""),
                            side=existing.get("side", "long"),
                            status=PaperTradeStatus(existing.get("status", "OPEN")),
                            qty=existing.get("qty", 0) or 0,
                            entry=existing.get("entry", 0) or 0,
                            stop=existing.get("stop", 0) or 0,
                            target=existing.get("target", 0) or 0,
                            entry_time_ms=existing.get("entry_time_ms", 0) or 0,
                            exit_time_ms=existing.get("exit_time_ms"),
                            exit_price=existing.get("exit_price"),
                            pnl_net=existing.get("pnl_net"),
                            exit_reason=existing.get("exit_reason"),
                            fee_inr=existing.get("fee_inr", 0) or 0,
                            slippage_inr=existing.get("slippage_inr", 0) or 0,
                            signal_json=existing.get("signal_json", "") or "",
                            created_at_ms=existing.get("created_at_ms", 0) or 0,
                        )
                        send_json(self, 200, {
                            "accepted": True,
                            "reason": "idempotent: request_key already processed",
                            "trade": trade.to_dict(),
                            "idempotent": True,
                        })
                        return

                from coindcx_lab.engine.signals import (
                    Signal, SignalState,
                )
                from coindcx_lab.engine.risk import RiskError
                import json as _json
                state_val = (body.get("state")
                              or "BUY_CANDIDATE").upper()
                try:
                    state = SignalState(state_val)
                except ValueError:
                    send_error(self, 400, "bad_request",
                               f"invalid state {state_val!r}")
                    return
                sig = Signal(
                    pair=body.get("pair", "B-BTCINR"),
                    interval=body.get("interval", "1h"),
                    strategy=body.get("strategy", "unknown"),
                    state=state,
                    entry=body.get("entry"),
                    stop=body.get("stop"),
                    target=body.get("target"),
                    rr_after_cost=body.get("rr_after_cost"),
                    est_entry_cost_inr=body.get("est_entry_cost_inr"),
                    est_exit_cost_inr=body.get("est_exit_cost_inr"),
                    explanation=body.get("explanation", ""),
                    request_key=request_key,  # embedded in signal_json at INSERT
                )
                try:
                    trade, reason = pe.open_position(
                        signal=sig,
                        current_price=body.get("current_price"),
                    )
                except RiskError as e:
                    send_error(self, 400, "risk_error", str(e))
                    return
                if trade is None:
                    send_json(self, 200, {
                        "accepted": False, "reason": reason, "trade": None,
                    })
                    return
                send_json(self, 200, {
                    "accepted": True, "reason": reason,
                    "trade": trade.to_dict(),
                })
                return
            send_error(self, 405, "method_not_allowed",
                       f"Method {method} not supported on /api/trades")
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):  self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")
