"""POST /api/scanner

Body: {"interval": "1h", "pairs": ["B-BTCINR", ...], "settings": {...}}

Runs the scanner over all INR spot pairs. Returns a ranked list with
exclusion reasons.
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


DEFAULT_PAIRS = ["B-BTCINR", "B-ETHINR", "B-SOLINR", "B-MATICINR",
                  "B-ADA INR".replace(" ", ""), "B-DOTINR", "B-LTCINR",
                  "B-XRPINR", "B-LINKINR", "B-AVAXINR"]
DEFAULT_PAIRS = [p for p in DEFAULT_PAIRS if " " not in p]


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
            pairs_list = body.get("pairs") or DEFAULT_PAIRS

            from coindcx_lab.api.client import CoinDCXClient, CoinDCXAPIError
            from coindcx_lab.api.models import DataProvenance, PairInfo, SourceKind
            from coindcx_lab.api.synthetic import SyntheticGenerator
            from coindcx_lab.engine.risk import RiskSettings, RiskState, DailyLossState
            from coindcx_lab.engine.scanner import Scanner
            from coindcx_lab.engine.signals import SignalEngine
            from coindcx_lab.api.env import SupabaseConfig
            from coindcx_lab.db.supabase_store import SupabaseStore
            from datetime import datetime, timezone

            # Settings
            settings = RiskSettings()
            try:
                cfg = SupabaseConfig.from_env()
                store = SupabaseStore(config=cfg)
                loaded = store.load_settings("risk")
                if loaded:
                    settings = RiskSettings(**loaded)
            except Exception:
                pass
            # Apply per-request overrides
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

            # Fetch candles for each pair (live or SIMULATED fallback)
            candles_by_pair = {}
            prices_by_pair = {}
            client = CoinDCXClient(timeout=8, max_retries=1, cache_ttl_seconds=0)
            synth = SyntheticGenerator(seed=42)
            for pair in pairs_list:
                base_price = {
                    "B-BTCINR": 7_000_000, "B-ETHINR": 350_000,
                    "B-SOLINR": 18_000, "B-MATICINR": 90,
                    "B-DOTINR": 700, "B-LTCINR": 8_000,
                    "B-XRPINR": 65, "B-LINKINR": 1_800,
                    "B-AVAXINR": 4_000,
                    "B-ADA INR".replace(" ", ""): 65,
                }.get(pair, 1000)
                try:
                    candles, prov = client.candles(pair=pair, interval=interval,
                                                       limit=300)
                    candles_by_pair[pair] = (candles, prov)
                    # Try to get latest price
                    try:
                        prices, _ = client.current_prices()
                        if pair in prices:
                            prices_by_pair[pair] = prices[pair]
                    except Exception:
                        pass
                except CoinDCXAPIError:
                    candles, prov = synth.series(pair=pair, interval=interval,
                                                   count=300, start_price=base_price)
                    candles_by_pair[pair] = (candles, prov)
                    prices_by_pair[pair] = synth.current_prices_for(pair, candles)

            # Build PairInfo list
            pairs = [
                PairInfo(pair=p, base=p[2:].replace("INR", ""),
                         quote="INR", status="active",
                         is_inr_quote=True)
                for p in pairs_list
            ]
            signal_engine = SignalEngine(settings=settings)
            scanner = Scanner(settings=settings, signal_engine=signal_engine)
            results = scanner.scan(
                pairs=pairs, candles_by_pair=candles_by_pair,
                tickers_by_pair=prices_by_pair, risk_state=state,
                interval=interval,
            )
            send_json(self, 200, {
                "interval": interval,
                "results": [r.to_dict() for r in results],
                "count": len(results),
                "settings": {
                    "initial_inr": settings.initial_inr,
                    "risk_per_trade_pct": settings.risk_per_trade_pct,
                    "max_daily_loss_pct": settings.max_daily_loss_pct,
                    "max_drawdown_pct": settings.max_drawdown_pct,
                    "min_24h_volume_inr": settings.min_24h_volume_inr,
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
