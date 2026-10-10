"""Shared HTTP helpers for the Vercel Python serverless functions.

CORS, JSON response, request body parsing, and the (currently single
shared anon) user_id resolution. The env-var-based configuration
lives in `coindcx_lab.api.env`.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional, Tuple


# ---------------------------------------------------------------------------
# Make `src/coindcx_lab` importable in the Vercel function runtime.
# Each /api/<name>.py file is at project-root/api/, so src/ is one level up.
# This is harmless in local tests because we add only if not present.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "src"))
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
def cors_headers() -> dict:
    """CORS headers. If API_ALLOWED_ORIGIN is unset, no Allow-Origin header
    is sent (same-origin only — recommended for the production deploy)."""
    h = {
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }
    allowed = os.environ.get("API_ALLOWED_ORIGIN", "").strip()
    if allowed:
        h["Access-Control-Allow-Origin"] = allowed
    return h


def handle_preflight(method: str, request) -> bool:
    """Handle OPTIONS preflight. Returns True if handled (and the
    204 response has been sent); False otherwise."""
    if method == "OPTIONS":
        request.send_response(204)
        for k, v in cors_headers().items():
            request.send_header(k, v)
        request.end_headers()
        return True
    return False


# ---------------------------------------------------------------------------
# JSON response
# ---------------------------------------------------------------------------
def send_json(request, status: int, payload: Any,
              extra_headers: Optional[dict] = None) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    request.send_response(status)
    request.send_header("Content-Type", "application/json; charset=utf-8")
    request.send_header("Content-Length", str(len(body)))
    for k, v in cors_headers().items():
        request.send_header(k, v)
    if extra_headers:
        for k, v in extra_headers.items():
            request.send_header(k, v)
    request.end_headers()
    request.wfile.write(body)


def send_error(request, status: int, message: str,
               detail: Optional[str] = None) -> None:
    send_json(request, status, {
        "error": message,
        "detail": detail or "",
        "status": status,
    })


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------
def parse_query(path: str) -> dict:
    if "?" not in path:
        return {}
    from urllib.parse import unquote
    qs = path.split("?", 1)[1]
    out = {}
    for pair in qs.split("&"):
        if not pair:
            continue
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[unquote(k)] = unquote(v)
        else:
            out[unquote(pair)] = ""
    return out


def read_json_body(request) -> Tuple[Optional[dict], Optional[str]]:
    try:
        length = int(request.headers.get("Content-Length", "0"))
    except ValueError:
        return None, "Invalid Content-Length header"
    if length == 0:
        return None, None
    raw = request.rfile.read(length)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON body: {e}"
    if not isinstance(body, dict):
        return None, "Body must be a JSON object"
    return body, None


# ---------------------------------------------------------------------------
# User identity
# ---------------------------------------------------------------------------
def current_user_id(request) -> str:
    """Return the user_id to scope this request to.

    For the MVP this is always the shared anonymous user UUID defined
    in SupabaseStore.DEFAULT_USER_ID. When real Supabase Auth is added
    later, this function will read the JWT from the Authorization
    header and return the authenticated user's UUID.
    """
    from coindcx_lab.db.supabase_store import DEFAULT_USER_ID
    return DEFAULT_USER_ID
