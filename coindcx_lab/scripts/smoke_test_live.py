"""Live-data smoke test for CoinDCX Spot Lab.

Attempts to reach the documented CoinDCX public endpoints from this
host and prints the actual result (HTTP code, latency, parsed payload
size). Never fabricates results. Exits 0 on success, 1 on failure.

Usage:
    python scripts/smoke_test_live.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

from coindcx_lab.api.client import (
    CoinDCXAPIError, CoinDCXClient, EP_CANDLES, EP_CURRENT_PRICES,
    EP_EXCHANGE_PAIRS,
)


def _hr(t: float) -> str:
    return f"{t*1000:.1f}ms" if t < 1 else f"{t:.2f}s"


def main() -> int:
    print(f"CoinDCX Spot Lab — live-data smoke test @ "
          f"{datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)
    client = CoinDCXClient(timeout=10, max_retries=1, cache_ttl_seconds=0)

    # 1) Exchange pairs
    print(f"\n[1] GET {EP_EXCHANGE_PAIRS}")
    t0 = time.time()
    try:
        pairs, prov = client.list_exchange_pairs()
        dt = time.time() - t0
        inr = [p for p in pairs if p.is_inr_quote]
        print(f"    OK in {_hr(dt)}  pairs={len(pairs)}  INR pairs={len(inr)}")
        print(f"    provenance: {prov.label()}")
        if inr:
            print(f"    first 5 INR pairs: {[p.pair for p in inr[:5]]}")
    except CoinDCXAPIError as e:
        dt = time.time() - t0
        print(f"    FAIL in {_hr(dt)}  {type(e).__name__}: {str(e)[:300]}")
    except Exception as e:
        dt = time.time() - t0
        print(f"    ERROR in {_hr(dt)}  {type(e).__name__}: {str(e)[:300]}")

    # 2) Current prices
    print(f"\n[2] GET {EP_CURRENT_PRICES}")
    t0 = time.time()
    try:
        prices, prov = client.current_prices()
        dt = time.time() - t0
        print(f"    OK in {_hr(dt)}  pairs_with_prices={len(prices)}")
        print(f"    provenance: {prov.label()}")
        if prices:
            sample = list(prices.items())[:3]
            for k, v in sample:
                print(f"    {k}: {v}")
    except CoinDCXAPIError as e:
        dt = time.time() - t0
        print(f"    FAIL in {_hr(dt)}  {type(e).__name__}: {str(e)[:300]}")
    except Exception as e:
        dt = time.time() - t0
        print(f"    ERROR in {_hr(dt)}  {type(e).__name__}: {str(e)[:300]}")

    # 3) Candles
    print(f"\n[3] POST {EP_CANDLES}  (pair=B-BTCINR interval=1h limit=24)")
    t0 = time.time()
    try:
        candles, prov = client.candles(pair="B-BTCINR", interval="1h",
                                        limit=24)
        dt = time.time() - t0
        print(f"    OK in {_hr(dt)}  candles={len(candles)}")
        print(f"    provenance: {prov.label()}")
        if candles:
            first, last = candles[0], candles[-1]
            print(f"    first: open_time={datetime.fromtimestamp(first.open_time_ms/1000, tz=UTC).isoformat()}"
                  f" close={first.close}")
            print(f"    last:  open_time={datetime.fromtimestamp(last.open_time_ms/1000, tz=UTC).isoformat()}"
                  f" close={last.close} complete={last.complete}")
    except CoinDCXAPIError as e:
        dt = time.time() - t0
        print(f"    FAIL in {_hr(dt)}  {type(e).__name__}: {str(e)[:300]}")
    except Exception as e:
        dt = time.time() - t0
        print(f"    ERROR in {_hr(dt)}  {type(e).__name__}: {str(e)[:300]}")

    print("\n" + "=" * 72)
    print("Smoke test complete.")
    return 0


if __name__ == "__main__":
    UTC = timezone.utc
    sys.exit(main())
