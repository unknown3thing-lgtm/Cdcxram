"""POST /api/portfolio/reset

DANGER: wipes ALL paper trades, equity_snapshots, and state for the
current user. Requires explicit confirmation.

Body: {"confirm": true}

Without {"confirm": true}, returns HTTP 400. This prevents accidental
resets from misclicks or curl without the body.

Preserves: user_settings (risk config is kept), market_data_cache
(shared across users), job_log (audit trail).
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
                       "Use POST with {\"confirm\": true}")
            return
        try:
            from coindcx_lab.server.paper_common import build_paper_engine
            pe = build_paper_engine(self)
            if pe is None:
                return  # error already sent

            body, err = read_json_body(self)
            if err:
                send_error(self, 400, "bad_request", err)
                return
            if body is None or not body.get("confirm"):
                send_error(self, 400, "confirmation_required",
                           "Reset requires {\"confirm\": true} in the body. "
                           "This prevents accidental resets.")
                return

            # User explicitly confirmed — perform the reset.
            # The PaperTradingEngine.reset() method requires a
            # confirmation_token internally, which we provide here.
            ok = pe.reset(require_confirmation=True,
                          confirmation_token="RESET-ALL-CONFIRMED")
            if ok:
                send_json(self, 200, {
                    "status": "ok",
                    "reset": True,
                    "note": ("All paper trades, equity snapshots, and "
                             "portfolio state have been wiped. Settings "
                             "and market_data_cache are preserved. "
                             "The portfolio now starts fresh from the "
                             "current initial_inr setting."),
                })
            else:
                send_error(self, 500, "reset_failed",
                           "Reset was not performed (unknown reason)")
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_POST(self):  self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")
