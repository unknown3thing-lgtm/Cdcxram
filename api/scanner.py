"""POST /api/scanner

Body: {"interval": "1h", "pairs": [...optional...], "max_pairs": 20,
       "settings": {...optional overrides...}, "strategy": "...optional..."}

Runs the scanner. By default:
  1. Fetch ALL INR spot pairs from the live provider (WazirX returns 216).
  2. Fetch all tickers in a single call (1 HTTP roundtrip).
  3. Pre-filter by min_24h_volume_inr — pairs that fail are returned
     as excluded with a clear reason, no klines fetched for them.
  4. Sort remaining pairs by 24h INR turnover (descending).
  5. Take top N (default 20, configurable via max_pairs) and fetch
     klines + run indicators in PARALLEL (ThreadPoolExecutor) — keeps
     total wall time under Vercel's 10s function timeout.
  6. Run signal engine for each of the top N.
  7. Return all discovered pairs in results, plus a summary with
     counts of how many were excluded by each filter.

Returns:
    {
      "interval": "1h",
      "results": [...],
      "count": 216,
      "summary": {
        "total_discovered": 216,
        "pre_filtered_by_volume": 186,
        "candles_fetched_for": 20,
        "active": 18,
        "excluded_total": 198,
        "exclusion_breakdown": {"24h volume X < min Y": 186, ...},
        "signal_breakdown": {"BUY_CANDIDATE": 2, "WAIT": 5, "NO_TRADE": 13, "EXIT": 0},
        "buy_candidates": 2
      },
      "provenance": {...},
      "settings": {...}
    }
"""

import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from http.server import BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple

from coindcx_lab.server.common import (
    handle_preflight, read_json_body, send_error, send_json,
)


# Old hardcoded default — kept ONLY as a fallback when the live
# provider is unreachable AND allow_simulated is false.
DEFAULT_PAIRS_FALLBACK = ["B-BTCINR", "B-ETHINR", "B-SOLINR", "B-MATICINR",
                          "B-ADAINR", "B-DOTINR", "B-LTCINR", "B-XRPINR",
                          "B-LINKINR", "B-AVAXINR"]


class handler(BaseHTTPRequestHandler):

    def _handle(self, method: str) -> None:
        if handle_preflight(method, self):
            return
        if method != "POST":
            send_error(self, 405, "method_not_allowed", "Use POST")
            return
        try:
            body, err = read_json_body(self)
            if err:
                send_error(self, 400, "bad_request", err)
                return
            if body is None:
                body = {}
            interval = body.get("interval", "1h")
            max_pairs = int(body.get("max_pairs", 20))
            max_pairs = max(1, min(max_pairs, 50))  # cap at 50 for safety
            explicit_pairs = body.get("pairs")

            from coindcx_lab.api.multi_provider import MultiProviderClient
            from coindcx_lab.api.client import CoinDCXAPIError
            from coindcx_lab.api.models import DataProvenance, PairInfo, SourceKind
            from coindcx_lab.api.synthetic import SyntheticGenerator
            from coindcx_lab.engine.risk import RiskSettings, RiskState, DailyLossState
            from coindcx_lab.engine.scanner import Scanner
            from coindcx_lab.engine.signals import SignalEngine
            from coindcx_lab.api.env import SupabaseConfig
            from coindcx_lab.db.supabase_store import SupabaseStore
            from datetime import datetime, timezone

            # ---- Settings (load from Supabase if available, then apply overrides)
            settings = RiskSettings()
            try:
                cfg = SupabaseConfig.from_env()
                store = SupabaseStore(config=cfg)
                loaded = store.load_settings("risk")
                if loaded:
                    settings = RiskSettings(**loaded)
            except Exception:
                pass
            for k in ("initial_inr", "risk_per_trade_pct",
                      "max_position_pct", "max_portfolio_exposure_pct",
                      "max_daily_loss_pct", "max_drawdown_pct",
                      "max_open_positions", "max_daily_trades",
                      "reward_to_risk_target",
                      "fee_pct", "spread_pct", "slippage_pct",
                      "min_24h_volume_inr"):
                if k in body and body[k] is not None:
                    setattr(settings, k, body[k])

            state = RiskState(initial_inr=settings.initial_inr)
            state.daily = DailyLossState(
                day_iso=datetime.now(timezone.utc).strftime("%Y-%m-%d"))

            client = MultiProviderClient(skip_coindcx=True)

            # ---- Step 1: discover all INR spot pairs + all tickers
            if explicit_pairs:
                pairs = [PairInfo(pair=p, base=p[2:].replace("INR", ""),
                                   quote="INR", status="active",
                                   is_inr_quote=True)
                         for p in explicit_pairs]
                pairs_prov = DataProvenance(
                    source=SourceKind.SIMULATED,
                    endpoint="caller-provided",
                    fetched_at_utc=datetime.now(timezone.utc),
                    note="pairs supplied in request body",
                )
            else:
                try:
                    pairs, pairs_prov = client.list_inr_spot_pairs()
                except CoinDCXAPIError as e:
                    send_error(self, 502, "live_data_unavailable",
                               f"Cannot discover INR pairs from any provider: {e}")
                    return

            # ---- Step 2: fetch ALL tickers in a single call
            try:
                all_prices, prices_prov = client.current_prices()
            except CoinDCXAPIError as e:
                send_error(self, 502, "live_data_unavailable",
                           f"Cannot fetch current prices: {e}")
                return

            # ---- Step 3: pre-filter by 24h INR volume AND spread
            pairs_with_vol = []
            pre_filtered_results = []
            pre_filtered_out = 0
            spread_filtered_out = 0
            for p in pairs:
                ticker = all_prices.get(p.pair, {})
                vol24_inr = ticker.get("volume_24h") or 0.0
                spread_pct = ticker.get("spread_pct") or 0.0
                # Volume filter
                if vol24_inr < settings.min_24h_volume_inr:
                    pre_filtered_out += 1
                    from coindcx_lab.engine.signals import SignalState
                    pre_filtered_results.append({
                        "pair": p.pair, "base": p.base, "quote": p.quote,
                        "last_price": ticker.get("last_price"),
                        "change_pct_24h": ticker.get("change_pct_24h"),
                        "volume_24h_inr": vol24_inr,
                        "spread_pct": spread_pct,
                        "trend": "n/a (excluded by volume filter)",
                        "volatility": None,
                        "signal_state": SignalState.NO_TRADE.value,
                        "best_strategy": None, "rr_after_cost": None,
                        "est_cost_inr": None, "score": 0.0,
                        "rank": None,
                        "excluded": True,
                        "exclusion_reason": (
                            f"24h volume ₹{vol24_inr:,.0f} < min "
                            f"₹{settings.min_24h_volume_inr:,.0f}"),
                        "provenance_label": prices_prov.label(),
                    })
                    continue
                # Spread filter (only if max_spread_pct > 0)
                if settings.max_spread_pct > 0 and spread_pct > settings.max_spread_pct:
                    spread_filtered_out += 1
                    from coindcx_lab.engine.signals import SignalState
                    pre_filtered_results.append({
                        "pair": p.pair, "base": p.base, "quote": p.quote,
                        "last_price": ticker.get("last_price"),
                        "change_pct_24h": ticker.get("change_pct_24h"),
                        "volume_24h_inr": vol24_inr,
                        "spread_pct": spread_pct,
                        "trend": "n/a (excluded by spread filter)",
                        "volatility": None,
                        "signal_state": SignalState.NO_TRADE.value,
                        "best_strategy": None, "rr_after_cost": None,
                        "est_cost_inr": None, "score": 0.0,
                        "rank": None,
                        "excluded": True,
                        "exclusion_reason": (
                            f"spread {spread_pct:.2f}% > max "
                            f"{settings.max_spread_pct:.2f}%"),
                        "provenance_label": prices_prov.label(),
                    })
                    continue
                pairs_with_vol.append((p, ticker, vol24_inr))
            pairs_with_vol.sort(key=lambda x: -x[2])
            top_pairs = pairs_with_vol[:max_pairs]

            # ---- Step 4: fetch klines + run indicators for top N IN PARALLEL
            candles_by_pair: dict = {}
            tickers_by_pair: dict = {p.pair: t for p, t, _ in pairs_with_vol}

            def _fetch_klines(p: PairInfo) -> Tuple[str, List, DataProvenance]:
                try:
                    c, pr = client.candles(pair=p.pair, interval=interval, limit=300)
                    return p.pair, c, pr
                except Exception:
                    return p.pair, [], DataProvenance(
                        source=SourceKind.UNAVAILABLE, endpoint="error",
                        fetched_at_utc=datetime.now(timezone.utc))

            # Only create the ThreadPoolExecutor if there are pairs to
            # fetch klines for. If all pairs failed the volume filter
            # (top_pairs is empty), skip the parallel fetch entirely —
            # ThreadPoolExecutor(max_workers=0) raises ValueError.
            if top_pairs:
                with ThreadPoolExecutor(max_workers=min(10, len(top_pairs))) as ex:
                    futures = [ex.submit(_fetch_klines, p) for p, _, _ in top_pairs]
                    for f in as_completed(futures):
                        pair_code, candles, prov = f.result()
                        candles_by_pair[pair_code] = (candles, prov)

            # ---- Step 5: run the scanner over the TOP PAIRS only
            # (pre-filtered pairs are already in pre_filtered_results)
            signal_engine = SignalEngine(settings=settings)
            scanner = Scanner(settings=settings, signal_engine=signal_engine)
            top_pair_infos = [p for p, _, _ in top_pairs]
            scanned_results = scanner.scan(
                pairs=top_pair_infos, candles_by_pair=candles_by_pair,
                tickers_by_pair=tickers_by_pair, risk_state=state,
                interval=interval,
            )
            # Combine: pre-filtered (excluded by volume) + scanned (top N)
            results = pre_filtered_results + [r.to_dict() for r in scanned_results]

            # ---- Step 6: build the summary
            from collections import Counter
            # results is a list of dicts (pre_filtered_results are dicts,
            # scanned_results were .to_dict()'d)
            excluded = [r for r in results if r.get("excluded")]
            active = [r for r in results if not r.get("excluded")]
            exclusion_reasons = Counter()
            for r in excluded:
                reason = r.get("exclusion_reason") or "unknown"
                if "24h volume" in reason and "< min" in reason:
                    reason = "24h_volume_below_min"
                elif "spread" in reason and "> max" in reason:
                    reason = "excessive_spread"
                elif "no candles" in reason:
                    reason = "no_candles_available"
                elif "data UNAVAILABLE" in reason:
                    reason = "data_unavailable"
                exclusion_reasons[reason] += 1
            signal_states = Counter(r.get("signal_state") for r in active)
            buy_candidates = sum(1 for r in active
                                  if r.get("signal_state") == "BUY_CANDIDATE")

            summary = {
                "total_discovered": len(pairs),
                "pre_filtered_by_volume": pre_filtered_out,
                "spread_filtered_out": spread_filtered_out,
                "candles_fetched_for": len(top_pairs),
                "active": len(active),
                "excluded_total": len(excluded),
                "exclusion_breakdown": dict(exclusion_reasons),
                "signal_breakdown": dict(signal_states),
                "buy_candidates": buy_candidates,
                "min_24h_volume_inr": settings.min_24h_volume_inr,
                "max_spread_pct": settings.max_spread_pct,
                "max_pairs": max_pairs,
            }

            send_json(self, 200, {
                "interval": interval,
                "results": results,   # already a list of dicts
                "count": len(results),
                "summary": summary,
                "provenance": {
                    "pairs_source": pairs_prov.source.value,
                    "pairs_endpoint": pairs_prov.endpoint,
                    "pairs_label": pairs_prov.label(),
                    "prices_source": prices_prov.source.value,
                    "prices_endpoint": prices_prov.endpoint,
                    "prices_label": prices_prov.label(),
                    "prices_fetched_at_utc": prices_prov.fetched_at_utc.isoformat(),
                },
                "settings": {
                    "initial_inr": settings.initial_inr,
                    "risk_per_trade_pct": settings.risk_per_trade_pct,
                    "max_daily_loss_pct": settings.max_daily_loss_pct,
                    "max_drawdown_pct": settings.max_drawdown_pct,
                    "min_24h_volume_inr": settings.min_24h_volume_inr,
                    "reward_to_risk_target": settings.reward_to_risk_target,
                    "fee_pct": settings.fee_pct,
                    "spread_pct": settings.spread_pct,
                    "slippage_pct": settings.slippage_pct,
                },
            })
        except ValueError as e:
            send_error(self, 400, "bad_request", str(e))
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_POST(self):  self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")
