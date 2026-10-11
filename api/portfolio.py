"""GET /api/portfolio          — portfolio summary
POST /api/portfolio_reset    — reset portfolio (with confirmation)

Consolidated from api/portfolio.py + api/portfolio_reset.py to reduce
the Vercel serverless function count (Hobby plan limit: 12).

The vercel.json rewrite maps:
  /api/portfolio_reset → /api/portfolio?action=reset

Inside the handler, we check the 'action' query param to distinguish
reset (POST + action=reset) from summary (GET, no action param).
"""

import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from http.server import BaseHTTPRequestHandler

from coindcx_lab.server.common import (
    handle_preflight, send_error, send_json,
)


class handler(BaseHTTPRequestHandler):

    def _handle(self, method: str) -> None:
        if handle_preflight(method, self):
            return
        try:
            # Check if this is a reset request (action=reset query param,
            # injected by vercel.json rewrite from /api/portfolio_reset)
            from coindcx_lab.server.common import parse_query
            q = parse_query(self.path)
            if q.get("action") == "reset" or "reset" in self.path:
                from coindcx_lab.server.portfolio_reset_handler import handle_reset
                handle_reset(self, method)
                return

            # Default: portfolio summary (GET only)
            if method != "GET":
                send_error(self, 405, "method_not_allowed", "Use GET")
                return

            from coindcx_lab.server.paper_common import build_paper_engine
            pe = build_paper_engine(self)
            if pe is None:
                return
            summ = pe.portfolio_summary()
            send_json(self, 200, summ)
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):  self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")
