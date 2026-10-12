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
            # skip_coindcx=True because CoinDCX's public API endpoints
            # return HTTP 404 "Link not found" from Vercel — saving the
            # ~1-2s of failed-404 time per cron run keeps us under the
            # Vercel 10s function timeout.
            from coindcx_lab.api.multi_provider import MultiProviderClient
            client = MultiProviderClient(skip_coindcx=True)

            live_error = None
            try:
                prices, prov = client.current_prices()
                if not prices:
                    raise CoinDCXAPIError("no prices returned from any provider")
            except CoinDCXAPIError as e:
                live_error = str(e)
                # SIMULATED fallback (last resort — only when WazirX also
                # fails, AND allow_simulated is True).
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
                # SIMULATED fallback — only 10 default pairs (to keep the
                # SIMULATED path fast; the LIVE path uses the batch upsert
                # for all 216 WazirX pairs).
                DEFAULT_SIM_PAIRS = ["B-BTCINR", "B-ETHINR", "B-SOLINR",
                                      "B-MATICINR", "B-ADAINR", "B-DOTINR",
                                      "B-LTCINR", "B-XRPINR", "B-LINKINR",
                                      "B-AVAXINR"]
                synth = SyntheticGenerator(seed=42)
                sim_prices = {}
                for pair in DEFAULT_SIM_PAIRS:
                    base_price = {
                        "B-BTCINR": 7_000_000, "B-ETHINR": 350_000,
                        "B-SOLINR": 18_000, "B-MATICINR": 90,
                        "B-DOTINR": 700, "B-LTCINR": 8_000,
                        "B-XRPINR": 65, "B-LINKINR": 1_800,
                        "B-AVAXINR": 4_000, "B-ADAINR": 65,
                    }.get(pair, 1000)
                    candles, _ = synth.series(pair=pair, interval="1h",
                                                count=48, start_price=base_price)
                    sim_prices[pair] = synth.current_prices_for(pair, candles)
                prices = sim_prices
                prov = DataProvenance(
                    source=SourceKind.SIMULATED,
                    endpoint="synthetic://cron-fallback",
                    fetched_at_utc=datetime.now(timezone.utc),
                    note=f"All live providers unreachable ({live_error}); "
                         "SIMULATED prices derived from deterministic "
                         "synthetic candles.",
                )

            # Persist all tickers in a SINGLE batched Supabase POST
            # (was 216 individual POSTs taking 110s total; batched
            # brings it to ~1-2s — well within Vercel's 10s timeout).
            try:
                upserted = store.upsert_market_data_batch(
                    items=prices,
                    provenance_note=prov.label(),
                    endpoint=("wazirx_public" if prov.source is SourceKind.LIVE
                              else "synthetic_fallback"),
                )
            except Exception as e:
                # If the batch upsert fails, log the error and return 500
                # (don't pretend success).
                store.log_job(
                    job_name="cron_refresh", status="error",
                    started_at=started_at,
                    ended_at=datetime.now(timezone.utc).isoformat(),
                    duration_ms=int(
                        (datetime.now(timezone.utc).timestamp() * 1000) - t0_ms),
                    error=f"batch upsert failed: {type(e).__name__}: {e}",
                )
                send_error(self, 500, "persist_failed",
                           f"Supabase batch upsert failed: {e}")
                return

            # ---- Step 3: Check open paper trades for stop/target exits ----
            # Phase 10: gap-aware exit checking. The cron now:
            #   1. Reads PaperTradingEngine.last_exit_check_ms (persisted
            #      in the engine's state blob).
            #   2. Computes the gap (hours since last successful exit check).
            #   3. If gap > 5h, fetches MORE candles per pair (up to a
            #      cap of 200 to stay within WazirX's reasonable limit
            #      and the Vercel 10s function timeout).
            #   4. Calls check_exits_with_recovery() which returns
            #      structured info including incomplete_pairs (pairs that
            #      could NOT be reliably exit-checked).
            #   5. Surfaces incomplete_pairs in the cron response JSON —
            #      never silently reports "ok" when coverage is incomplete.
            exits_processed = 0
            exits_detail = []
            incomplete_pairs = []
            gap_detected = False
            gap_hours = None
            try:
                from coindcx_lab.engine.paper import PaperTradingEngine
                from coindcx_lab.engine.risk import RiskSettings
                # Load settings for the paper engine
                loaded_settings = store.load_settings("risk")
                pe_settings = RiskSettings(**loaded_settings) if loaded_settings else RiskSettings()
                pe = PaperTradingEngine(settings=pe_settings, store=store)
                pe.reconcile_state()  # fix any stale state

                # Get all OPEN trades
                open_trades = store.get_open_trades()
                if open_trades:
                    open_pairs = list(set(t["pair"] for t in open_trades))
                    # Phase 10: gap-aware candle limit. If the engine
                    # has never been checked (last_exit_check_ms == 0)
                    # or the gap exceeds 5h, fetch up to 200 candles
                    # per pair (capped). Otherwise the default is 5.
                    interval_ms = 3_600_000  # 1h candles
                    last_check = getattr(pe, "last_exit_check_ms", 0)
                    now_ms_check = int(datetime.now(timezone.utc).timestamp() * 1000)
                    if last_check > 0:
                        gap_hours_val = (now_ms_check - last_check) / 3_600_000.0
                    else:
                        gap_hours_val = None
                    gap_detected = (gap_hours_val is not None
                                    and gap_hours_val > pe.DEFAULT_GAP_THRESHOLD_HOURS)
                    # Determine per-pair candle limit
                    if gap_detected:
                        # Aim to cover the gap + a 5-bar safety margin.
                        # Cap at 200 (WazirX supports up to 500; we leave
                        # headroom for timeout).
                        gap_bars = int((gap_hours_val or 0) + 5)
                        candle_limit = max(5, min(200, gap_bars))
                    elif last_check == 0:
                        # First-ever check: pull a small default window.
                        candle_limit = 10
                    else:
                        candle_limit = 5
                    gap_hours = gap_hours_val

                    candles_by_pair = {}
                    fetch_errors = []
                    for pair in open_pairs:
                        try:
                            candles, cprov = client.candles(
                                pair=pair, interval="1h", limit=candle_limit)
                            candles_by_pair[pair] = candles
                        except Exception as e:
                            # Phase 10: DO NOT silently swallow. Record
                            # the fetch failure so check_exits_with_recovery
                            # can flag this pair as incomplete.
                            fetch_errors.append({
                                "pair": pair,
                                "error": f"{type(e).__name__}: {e}",
                            })

                    # If we got fetch errors, those pairs will be flagged
                    # as missing by check_exits_with_recovery (since they
                    # won't be in candles_by_pair).

                    if candles_by_pair or fetch_errors:
                        # Phase 10: surface fetch errors BEFORE calling
                        # check_exits so they're recorded even if the
                        # exit-check itself throws.
                        for fe in fetch_errors:
                            incomplete_pairs.append({
                                "pair": fe["pair"],
                                "reason": "candle_fetch_failed",
                                "detail": fe["error"],
                                "gap_hours": gap_hours,
                            })

                        result = pe.check_exits_with_recovery(
                            candles_by_pair=candles_by_pair,
                            current_time_ms=now_ms_check)
                        closed = result["closed_trades"]
                        exits_processed = len(closed)
                        for t in closed:
                            exits_detail.append({
                                "pair": t.pair,
                                "exit_reason": t.exit_reason,
                                "exit_price": t.exit_price,
                                "pnl_net": t.pnl_net,
                            })
                        # Phase 10: append incomplete-pair reports from
                        # the engine (missing pairs / no eligible candles
                        # / all-incomplete candles).
                        for ip in result.get("incomplete_pairs", []):
                            incomplete_pairs.append({
                                "pair": ip["pair"],
                                "trade_id": ip.get("trade_id"),
                                "reason": ip.get("reason", "unknown"),
                                "gap_hours": ip.get("gap_hours"),
                            })
            except Exception as e:
                # Exit-checking failure should NOT fail the whole cron
                exits_detail.append({"error": f"exit check failed: {type(e).__name__}: {e}"})
                # Phase 10: an exit-check failure is also a form of
                # incomplete coverage — flag it for review.
                incomplete_pairs.append({
                    "pair": "*",
                    "reason": "exit_check_exception",
                    "detail": f"{type(e).__name__}: {e}",
                })

            ended_at = datetime.now(timezone.utc).isoformat()
            duration_ms = int(
                (datetime.now(timezone.utc).timestamp() * 1000) - t0_ms)

            # Phase 10: cron status. If we have incomplete_pairs, the
            # cron ran but coverage was incomplete — status="partial"
            # signals to operators that some open trades could NOT be
            # reliably exit-checked and need manual review.
            cron_status = "partial" if incomplete_pairs else "ok"
            candle_limit_used = locals().get("candle_limit")

            store.log_job(
                job_name="cron_refresh", status=cron_status,
                started_at=started_at, ended_at=ended_at,
                duration_ms=duration_ms,
                detail={"pairs_upserted": upserted,
                        "live_error": live_error,
                        "source": prov.source.value,
                        "exits_processed": exits_processed,
                        "exits_detail": exits_detail,
                        "incomplete_pairs": incomplete_pairs,
                        "gap_detected": gap_detected,
                        "gap_hours": gap_hours,
                        "candle_limit_used": candle_limit_used},
            )

            send_json(self, 200, {
                "status": cron_status,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
                "pairs_upserted": upserted,
                "exits_processed": exits_processed,
                "exits_detail": exits_detail,
                "incomplete_pairs": incomplete_pairs,
                "gap_detected": gap_detected,
                "gap_hours": gap_hours,
                "candle_limit_used": candle_limit_used,
                "provenance": {
                    "source": prov.source.value,
                    "label": prov.label(),
                    "note": prov.note,
                },
                "live_unavailable_reason": live_error,
                "note": ("Cron refreshes market data AND checks open paper "
                         "trades for stop/target exits. Idempotent — "
                         "CLOSED trades are never re-processed. Phase 10 "
                         "adds gap-aware recovery: if the gap since the "
                         "last successful exit check exceeds 5h, more "
                         "candles are fetched per pair. "
                         "status='partial' means some pairs could NOT be "
                         "reliably exit-checked (missing candles, fetch "
                         "errors, or all-incomplete candles) and need "
                         "manual review."),
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
