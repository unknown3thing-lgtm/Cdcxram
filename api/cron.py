"""POST /api/cron  (also GET /api/cron for external schedulers)

Scheduled refresh endpoint. Secured with CRON_SECRET.

This endpoint accepts BOTH POST and GET so that it works with:
  - Vercel Cron (sends POST with Authorization: Bearer <CRON_SECRET>)
  - External schedulers like cron-job.org, EasyCron, UptimeRobot, etc.
    that default to GET (their "Test Run" button issues a GET).

Both methods require the same Authorization: Bearer <CRON_SECRET> header.
Without a valid secret, the response is HTTP 401 unauthorized.

HEAD requests are intentionally NOT implemented — they return HTTP 501
because HEAD should not have side effects (and this endpoint writes to
the database). cron-job.org's "Test Run" uses GET, not HEAD.

This endpoint:
    1. Fetches latest current_prices from CoinDCX (live or simulated).
    2. Persists the ticker per pair into market_data_cache (Supabase).
    3. Logs the job execution to job_log.

Idempotency: the same timestamp will not be inserted twice because
market_data_cache uses UPSERT on (pair).
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
        # Accept both POST (Vercel Cron) and GET (cron-job.org, UptimeRobot,
        # EasyCron, etc. — most external schedulers default to GET).
        # Both methods require the same Bearer auth.
        if method not in ("POST", "GET"):
            send_error(self, 405, "method_not_allowed",
                       "Use POST or GET with Authorization: Bearer <CRON_SECRET>")
            return
        try:
            from coindcx_lab.server.common import current_user_id
            from coindcx_lab.api.env import (
                ConfigError, SupabaseConfig, cron_secret_matches,
            )
            from coindcx_lab.db.supabase_store import SupabaseStore
            from coindcx_lab.api.multi_provider import MultiProviderClient
            from coindcx_lab.api.client import CoinDCXAPIError
            from coindcx_lab.api.models import DataProvenance, SourceKind
            from coindcx_lab.api.synthetic import SyntheticGenerator
            from datetime import datetime, timezone

            # Auth check (same for GET and POST — constant-time comparison)
            auth = self.headers.get("Authorization", "")
            token = ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            elif auth:
                token = auth.strip()
            if not cron_secret_matches(token):
                send_error(self, 401, "unauthorized",
                           "missing or incorrect CRON_SECRET")
                return

            # Capture the datetime object BEFORE isoformat() — isoformat()
            # returns a string, and we need the datetime object for the
            # epoch-millis calculation below.
            started_dt = datetime.now(timezone.utc)
            started_at = started_dt.isoformat()
            t0_ms = int(started_dt.timestamp() * 1000)

            try:
                cfg = SupabaseConfig.from_env()
                store = SupabaseStore(config=cfg, user_id=current_user_id(self))
            except ConfigError as e:
                store.log_job(job_name="cron_refresh", status="error",
                               started_at=started_at,
                               ended_at=datetime.now(timezone.utc).isoformat(),
                               duration_ms=int(
                                   (datetime.now(timezone.utc).timestamp()*1000)
                                   - t0_ms),
                               error=f"config: {e}") if 'store' in dir() else None
                send_error(self, 500, "config_error", str(e))
                return

            # Fetch prices (live via MultiProviderClient — tries CoinDCX
            # first, falls back to WazirX which is verified LIVE for INR
            # spot pairs. If both live providers fail, fall back to
            # clearly-labelled SIMULATED.)
            client = MultiProviderClient()
            pairs_to_cache = ["B-BTCINR", "B-ETHINR", "B-SOLINR", "B-MATICINR",
                                "B-ADA INR".replace(" ", ""), "B-DOTINR",
                                "B-LTCINR", "B-XRPINR", "B-LINKINR",
                                "B-AVAXINR"]
            pairs_to_cache = [p for p in pairs_to_cache if " " not in p]

            live_error = None
            try:
                prices, prov = client.current_prices()
                if not prices:
                    raise CoinDCXAPIError("no prices returned from any provider")
            except CoinDCXAPIError as e:
                live_error = str(e)
                # SIMULATED fallback (last resort — only when both CoinDCX
                # and WazirX fail, AND allow_simulated is True).
                from coindcx_lab.api.env import SupabaseConfig
                allow_sim = True
                try:
                    cfg = SupabaseConfig.from_env()
                    allow_sim = cfg.allow_simulated
                except Exception:
                    pass
                if not allow_sim:
                    send_error(self, 502, "live_data_unavailable",
                               f"All market-data providers failed and "
                               f"SIMULATED fallback disabled. Last error: "
                               f"{live_error}")
                    return
                # SIMULATED fallback
                synth = SyntheticGenerator(seed=42)
                sim_prices = {}
                for pair in pairs_to_cache:
                    base_price = {
                        "B-BTCINR": 7_000_000, "B-ETHINR": 350_000,
                        "B-SOLINR": 18_000, "B-MATICINR": 90,
                        "B-DOTINR": 700, "B-LTCINR": 8_000,
                        "B-XRPINR": 65, "B-LINKINR": 1_800,
                        "B-AVAXINR": 4_000,
                        "B-ADA INR".replace(" ", ""): 65,
                    }.get(pair, 1000)
                    candles, _ = synth.series(pair=pair, interval="1h",
                                                count=48, start_price=base_price)
                    sim_prices[pair] = synth.current_prices_for(pair, candles)
                prices = sim_prices
                prov = DataProvenance(
                    source=SourceKind.SIMULATED,
                    endpoint="synthetic://cron-fallback",
                    fetched_at_utc=datetime.now(timezone.utc),
                    note=f"Live CoinDCX API unreachable ({live_error}); "
                         "SIMULATED prices derived from deterministic "
                         "synthetic candles.",
                )

            # Persist each pair's ticker
            upserted = 0
            for pair, ticker in prices.items():
                try:
                    store.upsert_market_data(
                        pair=pair, ticker=ticker,
                        provenance_note=prov.label(),
                    )
                    upserted += 1
                except Exception as e:
                    # Log individual failures but continue
                    store.log_job(
                        job_name="cron_refresh_pair", status="error",
                        started_at=started_at,
                        ended_at=datetime.now(timezone.utc).isoformat(),
                        duration_ms=0,
                        error=f"pair={pair} {type(e).__name__}: {e}",
                    )

            ended_at = datetime.now(timezone.utc).isoformat()
            duration_ms = int(
                (datetime.now(timezone.utc).timestamp() * 1000) - t0_ms)

            store.log_job(
                job_name="cron_refresh", status="ok",
                started_at=started_at, ended_at=ended_at,
                duration_ms=duration_ms,
                detail={"pairs_upserted": upserted,
                        "live_error": live_error,
                        "source": prov.source.value},
            )

            send_json(self, 200, {
                "status": "ok",
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
                "pairs_upserted": upserted,
                "provenance": {
                    "source": prov.source.value,
                    "label": prov.label(),
                    "note": prov.note,
                },
                "live_unavailable_reason": live_error,
                "note": ("Vercel Hobby plan Cron supports 1 daily invocation. "
                         "This endpoint is also callable on demand."),
            })
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):   self._handle("GET")    # for cron-job.org, UptimeRobot, etc.
    def do_POST(self):  self._handle("POST")   # for Vercel Cron
    def do_OPTIONS(self): self._handle("OPTIONS")
    # NOTE: do_HEAD is intentionally NOT defined — HEAD should not trigger
    # side effects. Default BaseHTTPRequestHandler returns 501 for HEAD.
