"""Shared HTTP helpers for the Vercel Python serverless functions.

Re-exported from `coindcx_lab.server` package init for convenience.
This file is kept separate so the package has a discoverable `common`
module that other code can `from coindcx_lab.server.common import ...`.
"""

# All public symbols are re-exported from __init__.py via the explicit
# imports there. This module exists to keep the file structure
# discoverable; direct imports should go via the package init.
from .__init__ import (  # noqa: F401
    cors_headers, handle_preflight, send_error, send_json,
    parse_query, read_json_body, current_user_id,
)
