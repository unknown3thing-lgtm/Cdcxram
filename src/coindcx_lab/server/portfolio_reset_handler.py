"""Portfolio reset handler logic — moved from api/portfolio_reset.py
to avoid creating an extra Vercel serverless function.

Called by api/portfolio.py when the request path contains 'action=reset'.
"""

from __future__ import annotations


def handle_reset(request, method: str) -> bool:
    """Handle POST /api/portfolio_reset. Returns True if handled.

    Without {"confirm": true} in the body, returns HTTP 400.
    """
    from coindcx_lab.server.common import send_error, send_json, read_json_body
    from coindcx_lab.server.paper_common import build_paper_engine

    if method != "POST":
        send_error(request, 405, "method_not_allowed",
                   'Use POST with {"confirm": true}')
        return True

    pe = build_paper_engine(request)
    if pe is None:
        return True  # error already sent by build_paper_engine

    body, err = read_json_body(request)
    if err:
        send_error(request, 400, "bad_request", err)
        return True
    if body is None or not body.get("confirm"):
        send_error(request, 400, "confirmation_required",
                   'Reset requires {"confirm": true} in the body. '
                   "This prevents accidental resets.")
        return True

    ok = pe.reset(require_confirmation=True,
                  confirmation_token="RESET-ALL-CONFIRMED")
    if ok:
        send_json(request, 200, {
            "status": "ok",
            "reset": True,
            "note": ("All paper trades, equity snapshots, and "
                     "portfolio state have been wiped. Settings "
                     "and market_data_cache are preserved. "
                     "The portfolio now starts fresh from the "
                     "current initial_inr setting."),
        })
    else:
        send_error(request, 500, "reset_failed",
                   "Reset was not performed (unknown reason)")
    return True
