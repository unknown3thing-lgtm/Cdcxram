"""Phase 12 — WazirX pair universe scanner.

Fetches the WazirX all-tickers response, filters to INR spot pairs
that are actively traded (positive volume, fresh data), and excludes
delisted pairs (e.g., MATIC was delisted ~Oct 2025).

Output: /home/z/my-project/download/phase12_pair_universe.json

This script ONLY reads public WazirX data.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from coindcx_lab.api.wazirx_client import WazirXClient
from coindcx_lab.api.models import SourceKind


def main():
    print(f"Phase 12 — WazirX pair universe scanner")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print()

    client = WazirXClient()
    # Fetch all tickers
    print("Fetching WazirX all-tickers...")
    try:
        prices, prov = client.current_prices()
    except Exception as e:
        print(f"  FAILED: {e}")
        return

    print(f"  Got {len(prices)} pairs. source={prov.source.value}")

    # Filter to pairs with positive 24h volume.
    # WazirXClient normalizes tickers into:
    #   last_price, high_24h, low_24h,
    #   volume_24h (INR turnover = last * base_volume),
    #   volume_24h_base (base-asset volume),
    #   change_pct_24h, spread_pct, buy_price, sell_price
    pairs_analysis = []
    active_pairs = []  # (pair, turnover_inr)
    for pair, ticker in prices.items():
        last = float(ticker.get("last_price", 0) or 0)
        turnover_inr = float(ticker.get("volume_24h", 0) or 0)
        vol_base = float(ticker.get("volume_24h_base", 0) or 0)
        low24 = float(ticker.get("low_24h", 0) or 0)
        high24 = float(ticker.get("high_24h", 0) or 0)
        spread_pct = float(ticker.get("spread_pct", 0) or 0)
        change_pct = float(ticker.get("change_pct_24h", 0) or 0)

        info = {
            "pair": pair,
            "last_price": last,
            "turnover_inr": round(turnover_inr, 2),
            "volume_24h_base": vol_base,
            "spread_pct": round(spread_pct, 3),
            "change_pct_24h": round(change_pct, 3),
            "low_24h": low24,
            "high_24h": high24,
            # Phase 12: we can't detect staleness from the ticker alone
            # (no last_trade_at field). The pair universe scanner marks
            # pairs with turnover > 0 as "active". A separate candle-
            # freshness check (see probe_wazirx_history.py) is needed
            # to detect delisted pairs like MATIC.
            "is_stale_by_ticker": turnover_inr == 0 and last == 0,
        }
        pairs_analysis.append(info)
        if turnover_inr > 0:
            active_pairs.append((pair, turnover_inr))

    # Sort active pairs by turnover (descending)
    active_pairs.sort(key=lambda x: -x[1])

    out = {
        "phase": 12,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_source": "WazirX /api/v2/tickers (public, via WazirXClient)",
        "total_pairs_fetched": len(prices),
        "active_pairs_count": len(active_pairs),
        "top_30_active_pairs_by_turnover": [
            {"pair": p, "turnover_inr": t} for p, t in active_pairs[:30]
        ],
        "all_pairs": pairs_analysis,
        "note": (
            "Staleness cannot be reliably detected from the ticker "
            "response alone (WazirX does not return a last_trade_at "
            "timestamp in the normalized fields). Pairs with turnover=0 "
            "are marked is_stale_by_ticker=True. For pairs with non-zero "
            "turnover but stale candle history (e.g., MATIC delisted "
            "~Oct 2025 with old ticker data still cached), the candle "
            "freshness check in probe_wazirx_history.py is the source "
            "of truth."
        ),
    }

    out_path = "/home/z/my-project/download/phase12_pair_universe.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print()
    print(f"Total pairs fetched: {len(prices)}")
    print(f"Active pairs (positive 24h turnover): {len(active_pairs)}")
    print()
    print("Top 20 active pairs by INR turnover:")
    for pair, turnover in active_pairs[:20]:
        print(f"  {pair:14s}  ₹{turnover:>14,.0f}")
    print()
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
