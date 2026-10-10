"""POST /api/cron

Scheduled refresh endpoint. Secured with CRON_SECRET.

Vercel Cron (Hobby plan: 1 invocation per day for free; Pro/Enterprise
support more frequent schedules). This endpoint:

    1. Fetches latest current_prices from CoinDCX (live or simulated).
    2. Persists the ticker per pair into market_data_cache (Supabase).
    3. Logs the job execution to job_log.

The endpoint refuses to run if:
    - the CRON_SECRET env var is unset
    - the Authorization header doesn't match (constant-time)

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
        if method != "POST":
            send_error(self, 405, "method_not_allowed", "Use POST")
            return
        try:
            from coindcx_lab.server.common import current_user_id
            from coindcx_lab.api.env import (
                ConfigError, SupabaseConfig, cron_secret_matches,
            )
            from coindcx_lab.db.supabase_store import SupabaseStore
            from coindcx_lab.api.client import CoinDCXClient, CoinDCXAPIError
            from coindcx_lab.api.models import DataProvenance, SourceKind
            from coindcx_lab.api.synthetic import SyntheticGenerator
            from datetime import datetime, timezone

            # Auth check
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

            started_at = datetime.now(timezone.utc).isoformat()
            t0_ms = int(started_at.replace(tzinfo=timezone.utc).timestamp() * 1000)

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

            # Fetch prices (live or simulated fallback)
            client = CoinDCXClient(timeout=10, max_retries=2, cache_ttl_seconds=0)
            pairs_to_cache = ["B-BTCINR", "B-ETHINR", "B-SOLINR", "B-MATICINR",
                                "B-ADA INR".replace(" ", ""), "B-DOTINR",
                                "B-LTCINR", "B-XRPINR", "B-LINKINR",
                                "B-AVAXINR"]
            pairs_to_cache = [p for p in pairs_to_cache if " " not in p]

            live_error = None
            try:
                prices, prov = client.current_prices()
                if not prices:
                    raise CoinDCXAPIError("no prices returned")
            except CoinDCXAPIError as e:
                live_error = str(e)
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

    def do_POST(self):  self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")
