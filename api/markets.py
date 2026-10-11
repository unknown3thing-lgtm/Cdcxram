"""GET /api/markets

Returns the list of CoinDCX INR spot pairs. Falls back to a
clearly-labelled SIMULATED pairs list when the live API is unreachable.
"""

import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from http.server import BaseHTTPRequestHandler

from coindcx_lab.server.common import handle_preflight, send_error, send_json


# Default pairs used when the CoinDCX API is unreachable.
# Matches the constants in dash/app.py.
DEFAULT_INR_PAIRS = [
    "B-BTCINR", "B-ETHINR", "B-SOLINR", "B-MATICINR", "B-ADA INR".replace(" ", ""),
    "B-DOTINR", "B-LTCINR", "B-XRPINR", "B-LINKINR", "B-AVAXINR",
]
DEFAULT_INR_PAIRS = [p for p in DEFAULT_INR_PAIRS if " " not in p]


class handler(BaseHTTPRequestHandler):

    def _handle(self, method: str) -> None:
        if handle_preflight(method, self):
            return
        try:
            from coindcx_lab.api.multi_provider import MultiProviderClient
            from coindcx_lab.api.client import CoinDCXAPIError
            from coindcx_lab.api.models import DataProvenance, PairInfo, SourceKind
            from datetime import datetime, timezone

            client = MultiProviderClient()
            try:
                pairs, prov = client.list_inr_spot_pairs()
                if pairs:
                    send_json(self, 200, {
                        "pairs": [
                            {"pair": p.pair, "base": p.base,
                             "quote": p.quote, "status": p.status,
                             "display_symbol": p.display_symbol}
                            for p in pairs
                        ],
                        "count": len(pairs),
                        "provenance": _provenance_dict(prov),
                    })
                    return
            except CoinDCXAPIError as e:
                # Fall through to SIMULATED
                live_error = str(e)
            except Exception as e:
                live_error = f"{type(e).__name__}: {e}"
            else:
                live_error = "no pairs returned"

            # SIMULATED fallback
            from coindcx_lab.api.env import SupabaseConfig
            allow_sim = True
            try:
                cfg = SupabaseConfig.from_env()
                allow_sim = cfg.allow_simulated
            except Exception:
                # env not set — default allow_sim = True
                pass

            if not allow_sim:
                send_error(self, 502, "live_data_unavailable",
                           f"CoinDCX API unreachable and simulated fallback "
                           f"disabled. Live error: {live_error}")
                return

            sim_pairs = [
                {"pair": p, "base": p[2:].replace("INR", ""),
                 "quote": "INR", "status": "active",
                 "display_symbol": f"{p[2:].replace('INR', '')}/INR"}
                for p in DEFAULT_INR_PAIRS
            ]
            sim_prov = DataProvenance(
                source=SourceKind.SIMULATED,
                endpoint="synthetic://pairs",
                fetched_at_utc=datetime.now(timezone.utc),
                note="CoinDCX public API unreachable from this host; using "
                     "the documented default INR spot pair list as SIMULATED.",
            )
            send_json(self, 200, {
                "pairs": sim_pairs,
                "count": len(sim_pairs),
                "provenance": _provenance_dict(sim_prov),
                "live_unavailable_reason": live_error,
            })
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
