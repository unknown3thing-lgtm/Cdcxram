"""GET /api/candles?pair=B-BTCINR&interval=1h&limit=200

Returns historical OHLCV candles for a pair. Falls back to a clearly-
labelled SIMULATED series when the live API is unreachable.
"""

import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from http.server import BaseHTTPRequestHandler

from coindcx_lab.server.common import handle_preflight, send_error, send_json


class handler(BaseHTTPRequestHandler):

    def _handle(self, method: str) -> None:
        if handle_preflight(method, self):
            return
        try:
            from coindcx_lab.server.common import parse_query
            from coindcx_lab.api.multi_provider import MultiProviderClient
            from coindcx_lab.api.client import CoinDCXAPIError
            from coindcx_lab.api.models import DataProvenance, SourceKind
            from coindcx_lab.api.synthetic import SyntheticGenerator
            from coindcx_lab.api.validate import validate_candles
            from datetime import datetime, timezone

            q = parse_query(self.path)
            pair = q.get("pair", "B-BTCINR")
            interval = q.get("interval", "1h")
            try:
                limit = int(q.get("limit", "200"))
                limit = max(10, min(limit, 1000))
            except ValueError:
                send_error(self, 400, "bad_request",
                           "limit must be an integer")
                return

            # Try live (MultiProviderClient tries CoinDCX first, falls back
            # to WazirX which is verified LIVE for INR spot pairs).
            client = MultiProviderClient()
            live_error = None
            try:
                candles, prov = client.candles(pair=pair, interval=interval,
                                                  limit=limit)
                if candles:
                    send_json(self, 200, {
                        "pair": pair, "interval": interval,
                        "candles": [c.to_dict() for c in candles],
                        "count": len(candles),
                        "provenance": _provenance_dict(prov),
                    })
                    return
            except CoinDCXAPIError as e:
                live_error = str(e)
            except Exception as e:
                live_error = f"{type(e).__name__}: {e}"

            # SIMULATED fallback
            base_price = {
                "B-BTCINR":   7_000_000,
                "B-ETHINR":   350_000,
                "B-SOLINR":   18_000,
                "B-MATICINR": 90,
                "B-DOTINR":   700,
                "B-LTCINR":   8_000,
                "B-XRPINR":   65,
                "B-LINKINR":  1_800,
                "B-AVAXINR":  4_000,
                "B-ADA INR".replace(" ", ""): 65,
            }.get(pair, 1000)
            synth = SyntheticGenerator(seed=42)
            candles, prov = synth.series(pair=pair, interval=interval,
                                           count=limit, start_price=base_price,
                                           drift_per_bar=0.0002,
                                           vol_per_bar=0.012,
                                           jump_prob=0.02, jump_size=0.04)
            send_json(self, 200, {
                "pair": pair, "interval": interval,
                "candles": [c.to_dict() for c in candles],
                "count": len(candles),
                "provenance": _provenance_dict(prov),
                "live_unavailable_reason": live_error,
            })
        except ValueError as e:
            send_error(self, 400, "bad_request", str(e))
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):  self._handle("GET")
    def do_OPTIONS(self): self._handle("OPTIONS")


def _provenance_dict(prov) -> dict:
    return {
        "source": prov.source.value,
        "endpoint": prov.endpoint,
        "fetched_at_utc": prov.fetched_at_utc.isoformat(),
        "label": prov.label(),
        "note": prov.note,
    }
