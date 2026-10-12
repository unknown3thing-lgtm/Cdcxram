"""Phase 11 — WazirX historical-data expansion probe.

Tests whether WazirX reliably supports fetching 750-1000 hourly
candles per pair. Records:
  * actual candle count returned (may be < requested)
  * date range covered
  * timestamp validation (sorted, no duplicates)
  * missing-interval detection (gaps in open_time_ms sequence)
  * OHLC consistency (low <= high; open/close within [low, high])
  * candle ordering

Distinguishes rate-limit failures from genuinely unavailable history.

Output: /home/z/my-project/download/phase11_candle_coverage.json

This script ONLY reads public WazirX data. It does not place real
orders, modify production portfolio, or reset any data.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from coindcx_lab.api.wazirx_client import WazirXClient
from coindcx_lab.api.models import Candle
from coindcx_lab.api.validate import validate_candles
from coindcx_lab.api.client import RateLimitError, CoinDCXAPIError


# Existing supported pairs (matches prior phases — kept stable).
DEFAULT_PAIRS = [
    "B-BTCINR", "B-ETHINR", "B-SOLINR", "B-XRPINR", "B-MATICINR",
    "B-DOTINR", "B-LTCINR", "B-LINKINR", "B-AVAXINR", "B-ADAINR",
]

# Test limit ladder — discover what WazirX actually supports.
LIMIT_LADDER = [200, 500, 750, 1000]


def validate_candle_integrity(candles: List[Candle],
                                interval_ms: int) -> dict:
    """Run integrity checks on a list of candles.

    Returns a dict of:
      * n: count
      * sorted: bool (open_time_ms strictly increasing)
      * duplicate_count: int (open_time_ms collisions)
      * missing_intervals: int (gaps in the open_time_ms sequence)
      * ohlc_violations: int (low > high OR open/close outside [low, high])
      * first_open_time_ms / last_open_time_ms
      * first_date / last_date (UTC)
      * interval_ms_expected
    """
    n = len(candles)
    if n == 0:
        return {"n": 0, "sorted": True, "duplicate_count": 0,
                "missing_intervals": 0, "ohlc_violations": 0,
                "first_open_time_ms": None, "last_open_time_ms": None,
                "first_date": None, "last_date": None,
                "interval_ms_expected": interval_ms}

    times = [c.open_time_ms for c in candles]
    # Sorted (strictly increasing)?
    sorted_ok = all(times[i] < times[i + 1] for i in range(len(times) - 1))
    # Duplicates?
    duplicate_count = len(times) - len(set(times))
    # Missing intervals (gaps in the sequence)
    missing_intervals = 0
    for i in range(1, len(times)):
        gap = times[i] - times[i - 1]
        if gap != interval_ms:
            missing_intervals += 1
    # OHLC consistency
    ohlc_violations = 0
    for c in candles:
        if c.low > c.high:
            ohlc_violations += 1
            continue
        if not (c.low <= c.open <= c.high):
            ohlc_violations += 1
            continue
        if not (c.low <= c.close <= c.high):
            ohlc_violations += 1
            continue
        if c.volume < 0:
            ohlc_violations += 1

    return {
        "n": n,
        "sorted": sorted_ok,
        "duplicate_count": duplicate_count,
        "missing_intervals": missing_intervals,
        "ohlc_violations": ohlc_violations,
        "first_open_time_ms": times[0],
        "last_open_time_ms": times[-1],
        "first_date": datetime.fromtimestamp(
            times[0] / 1000, tz=timezone.utc).isoformat(),
        "last_date": datetime.fromtimestamp(
            times[-1] / 1000, tz=timezone.utc).isoformat(),
        "interval_ms_expected": interval_ms,
    }


def probe_pair_limit(client: WazirXClient, pair: str,
                     interval: str, limit: int,
                     interval_ms: int) -> dict:
    """Fetch with the new retry logic; record outcome."""
    t0 = time.time()
    try:
        candles, prov = client.candles(pair=pair, interval=interval,
                                        limit=limit)
        duration_s = time.time() - t0
        integrity = validate_candle_integrity(candles, interval_ms)
        # Also run the existing validate_candles helper for parity.
        try:
            validated = validate_candles(candles,
                                          interval_ms=interval_ms,
                                          min_count=1)
            validated_count = len(validated)
        except Exception as e:
            validated_count = -1
            validated_error = str(e)
        return {
            "pair": pair, "interval": interval, "limit_requested": limit,
            "limit_returned": len(candles),
            "duration_s": round(duration_s, 3),
            "source": prov.source.value,
            "endpoint": prov.endpoint,
            "fetch_status": "ok",
            "fetch_error": None,
            "integrity": integrity,
            "validated_count": validated_count,
        }
    except RateLimitError as e:
        return {
            "pair": pair, "interval": interval, "limit_requested": limit,
            "limit_returned": 0,
            "duration_s": round(time.time() - t0, 3),
            "source": None, "endpoint": None,
            "fetch_status": "rate_limited",
            "fetch_error": f"{type(e).__name__}: {e}",
            "integrity": None, "validated_count": None,
        }
    except CoinDCXAPIError as e:
        return {
            "pair": pair, "interval": interval, "limit_requested": limit,
            "limit_returned": 0,
            "duration_s": round(time.time() - t0, 3),
            "source": None, "endpoint": None,
            "fetch_status": "api_error",
            "fetch_error": f"{type(e).__name__}: {e}",
            "integrity": None, "validated_count": None,
        }
    except Exception as e:
        return {
            "pair": pair, "interval": interval, "limit_requested": limit,
            "limit_returned": 0,
            "duration_s": round(time.time() - t0, 3),
            "source": None, "endpoint": None,
            "fetch_status": "unexpected_error",
            "fetch_error": f"{type(e).__name__}: {e}",
            "integrity": None, "validated_count": None,
        }


# Interval→ms map (must match api/models.py / INTERVAL_MS).
INTERVAL_MS = {
    "1h": 3_600_000,
    "4h": 4 * 3_600_000,
    "1d": 24 * 3_600_000,
}


def main():
    print(f"Phase 11 — WazirX historical-data expansion probe")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print()

    client = WazirXClient()  # uses default retry/backoff (now with 429 retries)

    all_results = {
        "phase": 11,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_source": "WazirX public klines API",
        "client_config": {
            "max_retries": client.max_retries,
            "backoff_seconds": client.backoff_seconds,
            "rate_limit_retries": client.rate_limit_retries,
            "rate_limit_backoff_seconds": client.rate_limit_backoff_seconds,
            "timeout": client.timeout,
        },
        "pairs": DEFAULT_PAIRS,
        "limit_ladder": LIMIT_LADDER,
        "intervals": ["1h"],  # 4h/1d tested in the backtest runner
        "results": [],
    }

    # Probe each (pair, limit) combination on 1h interval.
    # To stay polite, we sweep limit-ladder first on BTC, then on each pair.
    for pair in DEFAULT_PAIRS:
        for limit in LIMIT_LADDER:
            print(f"  Probing {pair} limit={limit} (1h)...")
            r = probe_pair_limit(client, pair, "1h", limit,
                                 INTERVAL_MS["1h"])
            all_results["results"].append(r)
            status = r["fetch_status"]
            ret = r["limit_returned"]
            print(f"    -> status={status} returned={ret} "
                  f"duration={r['duration_s']}s")
            if r["integrity"]:
                i = r["integrity"]
                print(f"       dates: {i['first_date']} → {i['last_date']} "
                      f"sorted={i['sorted']} "
                      f"dup={i['duplicate_count']} "
                      f"missing={i['missing_intervals']} "
                      f"ohlc_bad={i['ohlc_violations']}")
            # Be polite between requests — WazirX responds well to a
            # small delay between calls.
            time.sleep(0.4)

    all_results["ended_at_utc"] = datetime.now(timezone.utc).isoformat()

    # Write to download/
    out_path = "/home/z/my-project/download/phase11_candle_coverage.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print()
    print(f"Results written to: {out_path}")
    print(f"Total probe records: {len(all_results['results'])}")
    # Summary
    ok = [r for r in all_results["results"] if r["fetch_status"] == "ok"]
    rl = [r for r in all_results["results"]
           if r["fetch_status"] == "rate_limited"]
    err = [r for r in all_results["results"]
            if r["fetch_status"] not in ("ok", "rate_limited")]
    print(f"  OK: {len(ok)}  rate-limited: {len(rl)}  errors: {len(err)}")


if __name__ == "__main__":
    main()
