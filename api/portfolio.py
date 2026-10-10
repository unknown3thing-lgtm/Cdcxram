"""GET /api/portfolio

Returns the paper portfolio summary: initial equity, current equity,
available cash, open positions value, realized PnL, drawdown %,
daily loss, consecutive losses, etc.

Backed by Supabase (via SupabaseStore). Reads only — uses the
publishable (anon) key when the service key is unavailable.
"""

import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from http.server import BaseHTTPRequestHandler

from coindcx_lab.server.common import handle_preflight, send_error, send_json


class handler(BaseHTTPRequestHandler):

    def _handle(self, method: str) -> None:
        if handle_preflight(method, self):
            return
        if method != "GET":
            send_error(self, 405, "method_not_allowed", "Use GET")
            return
        try:
            from coindcx_lab.server.paper_common import build_paper_engine
            pe = build_paper_engine(self)
            if pe is None:
                return  # error already sent
            summ = pe.portfolio_summary()
            send_json(self, 200, summ)
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):  self._handle("GET")
    def do_OPTIONS(self): self._handle("OPTIONS")
