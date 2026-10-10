"""Environment-variable-based configuration for CoinDCX Spot Lab.

This module reads runtime secrets and configuration from environment
variables. NO secrets are hardcoded. NO secrets are logged. The
.env.example file at the project root documents the variable names.

Used by:
    - Vercel serverless functions in /api/
    - The SupabaseStore adapter (src/coindcx_lab/db/supabase_store.py)
    - The cron endpoint (api/cron.py)
    - Test scripts that need to verify connectivity (only when
      explicitly invoked by the developer — never as part of the
      default pytest run)

Design rules:
    - Missing env vars must NOT crash the import path; only the call
      site that needs them may raise.
    - The publishable (anon) key is safe to expose to the browser.
    - The service-role key MUST NEVER appear in browser code or be
      returned by any API endpoint.
    - All values are read lazily via functions so the module can be
      imported in test environments without env vars set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Variable names — documented in .env.example
# ---------------------------------------------------------------------------
# Public, safe to expose to the browser:
ENV_SUPABASE_URL = "SUPABASE_URL"
ENV_SUPABASE_PUBLISHABLE_KEY = "SUPABASE_PUBLISHABLE_KEY"   # aka anon key

# Server-only secrets (must NEVER be embedded in client-side code):
ENV_SUPABASE_SERVICE_KEY = "SUPABASE_SERVICE_KEY"            # aka service role
ENV_CRON_SECRET = "CRON_SECRET"                              # protects /api/cron
ENV_API_ALLOWED_ORIGIN = "API_ALLOWED_ORIGIN"               # optional CORS

# CoinDCX client tuning (optional overrides):
ENV_COINDCX_BASE_URL = "COINDCX_BASE_URL"
ENV_COINDCX_TIMEOUT = "COINDCX_TIMEOUT"
ENV_COINDCX_MAX_RETRIES = "COINDCX_MAX_RETRIES"

# Whether the deployed environment may fall back to clearly-labelled
# SIMULATED data when live CoinDCX data is unavailable. Default true.
ENV_ALLOW_SIMULATED = "ALLOW_SIMULATED_WHEN_LIVE_UNAVAILABLE"


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or invalid."""


@dataclass(frozen=True)
class SupabaseConfig:
    """Supabase connection config. Reads env vars lazily at construction."""

    url: str
    publishable_key: str
    service_key: Optional[str]    # may be None in browser-only contexts
    allow_simulated: bool = True

    @classmethod
    def from_env(cls) -> "SupabaseConfig":
        url = os.environ.get(ENV_SUPABASE_URL, "").strip()
        pub = os.environ.get(ENV_SUPABASE_PUBLISHABLE_KEY, "").strip()
        svc = os.environ.get(ENV_SUPABASE_SERVICE_KEY, "").strip() or None
        allow_sim = _parse_bool(os.environ.get(ENV_ALLOW_SIMULATED, "true"))
        if not url or not pub:
            raise ConfigError(
                f"Missing required env vars. Set {ENV_SUPABASE_URL} and "
                f"{ENV_SUPABASE_PUBLISHABLE_KEY} (and optionally "
                f"{ENV_SUPABASE_SERVICE_KEY} for server-side writes). "
                "See .env.example.")
        # Basic URL format sanity.
        if not (url.startswith("https://") and "supabase.co" in url):
            raise ConfigError(
                f"{ENV_SUPABASE_URL}={url!r} does not look like a Supabase "
                "project URL (expected https://<project>.supabase.co).")
        return cls(url=url, publishable_key=pub, service_key=svc,
                   allow_simulated=allow_sim)

    def rest_url(self, table: str) -> str:
        """PostgREST endpoint for a specific table."""
        # Strip trailing slash from URL.
        base = self.url.rstrip("/")
        return f"{base}/rest/v1/{table}"

    def auth_url(self) -> str:
        return f"{self.url.rstrip('/')}/auth/v1"

    def is_server_side(self) -> bool:
        """True when we have the service-role key (i.e. running on the
        server, where RLS can be bypassed for trusted writes)."""
        return self.service_key is not None

    def headers(self, *, use_service: bool = False) -> dict:
        """Return the headers required by PostgREST.

        With use_service=True, uses the service-role key. This must ONLY
        be called from server-side code.
        """
        if use_service:
            if self.service_key is None:
                raise ConfigError(
                    f"Server-side operation requires {ENV_SUPABASE_SERVICE_KEY} "
                    "to be set. Refusing to use the publishable key for "
                    "writes that should bypass RLS.")
            return {
                "apikey": self.service_key,
                "Authorization": f"Bearer {self.service_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        return {
            "apikey": self.publishable_key,
            "Authorization": f"Bearer {self.publishable_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


# ---------------------------------------------------------------------------
# CoinDCX client tuning (optional)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CoinDCXClientConfig:
    base_url: str = "https://public.coindcx.com"
    timeout: float = 10.0
    max_retries: int = 3
    backoff_seconds: float = 0.6
    cache_ttl_seconds: float = 5.0
    user_agent: str = "coindcx-lab/0.1 (+research; no-auth)"

    @classmethod
    def from_env(cls) -> "CoinDCXClientConfig":
        return cls(
            base_url=os.environ.get(ENV_COINDCX_BASE_URL,
                                     "https://public.coindcx.com").strip(),
            timeout=float(os.environ.get(ENV_COINDCX_TIMEOUT, "10")),
            max_retries=int(os.environ.get(ENV_COINDCX_MAX_RETRIES, "3")),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_bool(s: str) -> bool:
    return s.strip().lower() in {"1", "true", "yes", "on", "y"}


def cron_secret_matches(provided: Optional[str]) -> bool:
    """Constant-time comparison of the provided CRON_SECRET."""
    expected = os.environ.get(ENV_CRON_SECRET, "")
    if not expected or not provided:
        return False
    # Constant-time comparison
    import hmac
    return hmac.compare_digest(expected, provided)


def allowed_origin() -> Optional[str]:
    """CORS allowed origin. None means 'no CORS header' (same-origin only)."""
    v = os.environ.get(ENV_API_ALLOWED_ORIGIN, "").strip()
    return v or None
