"""GET /api/health          — service health + non-secret env status
GET/POST /api/settings     — load/save capital + risk config

Consolidated from api/health.py + api/settings.py to reduce the Vercel
serverless function count (Hobby plan limit: 12).

The vercel.json rewrite maps:
  /api/settings → /api/health?action=settings

Inside the handler, we check the 'action' query param to distinguish
settings (GET/POST + action=settings) from health (GET, no action).
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
            # Check if this is a settings request (action=settings query
            # param, injected by vercel.json rewrite from /api/settings)
            from coindcx_lab.server.common import parse_query
            q = parse_query(self.path)
            if q.get("action") == "settings":
                from coindcx_lab.server.settings_handler import handle_settings
                handle_settings(self, method)
                return

            # Default: health check
            env_status = {
                "SUPABASE_URL":          bool(os.environ.get("SUPABASE_URL")),
                "SUPABASE_PUBLISHABLE_KEY": bool(os.environ.get("SUPABASE_PUBLISHABLE_KEY")),
                "SUPABASE_SERVICE_KEY_SET": bool(os.environ.get("SUPABASE_SERVICE_KEY")),
                "CRON_SECRET_SET":        bool(os.environ.get("CRON_SECRET")),
                "API_ALLOWED_ORIGIN_SET": bool(os.environ.get("API_ALLOWED_ORIGIN")),
                "ALLOW_SIMULATED_WHEN_LIVE_UNAVAILABLE":
                    os.environ.get("ALLOW_SIMULATED_WHEN_LIVE_UNAVAILABLE", "true"),
            }
            send_json(self, 200, {
                "status": "ok",
                "service": "coindcx-spot-lab",
                "version": "0.1.0",
                "python_version": self._py_version(),
                "env": env_status,
                "timestamp_utc": self._now_iso(),
                "note": ("Health endpoint exposes only the presence of env "
                         "vars, never their values."),
            })
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    @staticmethod
    def _py_version() -> str:
        import sys
        return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):  self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")
