"""GET /api/prices

Returns current prices for all pairs. Falls back to SIMULATED prices
derived from cached candles when the live API is unreachable.
"""

import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from http.server import BaseHTTPRequestHandler

from coindcx_lab.server.common import handle_preflight, send_error, send_json


DEFAULT_PAIRS = ["B-BTCINR", "B-ETHINR", "B-SOLINR", "B-MATICINR",
                  "B-ADA INR".replace(" ", ""), "B-DOTINR", "B-LTCINR",
                  "B-XRPINR", "B-LINKINR", "B-AVAXINR"]
DEFAULT_PAIRS = [p for p in DEFAULT_PAIRS if " " not in p]


class handler(BaseHTTPRequestHandler):

    def _handle(self, method: str) -> None:
        if handle_preflight(method, self):
            return
        try:
            from coindcx_lab.api.multi_provider import MultiProviderClient
            from coindcx_lab.api.client import CoinDCXAPIError
            from coindcx_lab.api.models import DataProvenance, SourceKind
            from coindcx_lab.api.synthetic import SyntheticGenerator
            from datetime import datetime, timezone

            client = MultiProviderClient()
            try:
                prices, prov = client.current_prices()
                if prices:
                    send_json(self, 200, {
                        "prices": prices,
                        "count": len(prices),
                        "provenance": _prov(prov),
                    })
                    return
            except CoinDCXAPIError as e:
                live_error = str(e)
            except Exception as e:
                live_error = f"{type(e).__name__}: {e}"

            # SIMULATED fallback: derive prices from synthetic candles
            synth = SyntheticGenerator(seed=42)
            sim_prices = {}
            for pair in DEFAULT_PAIRS:
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
                candles, _ = synth.series(pair=pair, interval="1h",
                                            count=48, start_price=base_price)
                sim_prices[pair] = synth.current_prices_for(pair, candles)
            sim_prov = DataProvenance(
                source=SourceKind.SIMULATED,
                endpoint="synthetic://prices",
                fetched_at_utc=datetime.now(timezone.utc),
                note="CoinDCX public API unreachable; SIMULATED prices derived "
                     "from deterministic synthetic candles. NOT live prices.",
            )
            send_json(self, 200, {
                "prices": sim_prices,
                "count": len(sim_prices),
                "provenance": _prov(sim_prov),
                "live_unavailable_reason": live_error,
            })
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):  self._handle("GET")
    def do_OPTIONS(self): self._handle("OPTIONS")


def _prov(prov) -> dict:
    return {
        "source": prov.source.value,
        "endpoint": prov.endpoint,
        "fetched_at_utc": prov.fetched_at_utc.isoformat(),
        "label": prov.label(),
        "note": prov.note,
    }
