"""Phase 13 — pair universe recheck with data-quality criteria.

Re-scans WazirX all-tickers and additionally samples 1h candle history
for each candidate pair to verify:
  - Positive 24h INR turnover (liquidity filter).
  - Spread % within reasonable bounds (data-quality filter).
  - Candle freshness (latest candle's open_time_ms is recent —
    EXCLUDES delisted pairs like MATIC whose last candle is from
    2025-10-25).
  - At least 500 candles of usable history.

Output: /home/z/my-project/download/phase13_pair_universe_vetted.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from coindcx_lab.api.wazirx_client import WazirXClient
from coindcx_lab.api.client import RateLimitError, CoinDCXAPIError


# Pairs to vet (top Phase 12 candidates + existing supported pairs).
CANDIDATE_PAIRS = [
    "B-USDTINR", "B-BTCINR", "B-ETHINR", "B-WRXINR", "B-INJINR",
    "B-PEPEINR", "B-DENTINR", "B-CKBINR", "B-TRXINR", "B-SHIBINR",
    "B-SOLINR", "B-NEARINR", "B-FETINR", "B-SKYINR", "B-BATINR",
    "B-HNTINR", "B-CELRINR", "B-COTIINR", "B-ZECINR", "B-LINKINR",
    "B-XRPINR", "B-MATICINR",  # known stale — included to verify exclusion
    "B-DOTINR", "B-LTCINR", "B-AVAXINR", "B-ADAINR",
]


def vet_pair(client: WazirXClient, pair: str) -> dict:
    """Vet a single pair: fetch ticker + small candle sample."""
    out = {"pair": pair}
    # 1. Ticker check (already in the all-tickers response from
    #    WazirXClient.current_prices()).
    # 2. Candle freshness: fetch a small sample (limit=5) and verify
    #    the latest candle's open_time_ms is within the last 6 hours.
    try:
        candles, prov = client.candles(pair=pair, interval="1h", limit=5)
        if not candles:
            out["status"] = "no_candles"
            return out
        latest = candles[-1]
        latest_open_ms = latest.open_time_ms
        age_hours = (datetime.now(timezone.utc).timestamp() * 1000 - latest_open_ms) / 3_600_000
        out["latest_candle_open_ms"] = latest_open_ms
        out["latest_candle_iso"] = datetime.fromtimestamp(
            latest_open_ms / 1000, tz=timezone.utc).isoformat()
        out["age_hours"] = round(age_hours, 2)
        out["source"] = prov.source.value
        # Phase 13 freshness threshold: latest candle must be < 6h old.
        if age_hours > 6:
            out["status"] = "stale"
            out["exclusion_reason"] = (
                f"latest candle is {age_hours:.1f}h old — pair likely "
                "delisted or has no recent trades")
        else:
            out["status"] = "fresh"
        # Also fetch a larger history (limit=500) to confirm usability
        try:
            candles_500, _ = client.candles(pair=pair, interval="1h", limit=500)
            out["history_500_count"] = len(candles_500)
            if candles_500:
                first = candles_500[0]
                last = candles_500[-1]
                out["history_500_first_iso"] = datetime.fromtimestamp(
                    first.open_time_ms / 1000, tz=timezone.utc).isoformat()
                out["history_500_last_iso"] = datetime.fromtimestamp(
                    last.open_time_ms / 1000, tz=timezone.utc).isoformat()
        except Exception as e:
            out["history_500_error"] = f"{type(e).__name__}: {e}"
    except RateLimitError as e:
        out["status"] = "rate_limited"
        out["exclusion_reason"] = f"429: {e}"
    except CoinDCXAPIError as e:
        out["status"] = "api_error"
        out["exclusion_reason"] = f"{e}"
    except Exception as e:
        out["status"] = "unexpected_error"
        out["exclusion_reason"] = f"{type(e).__name__}: {e}"
    return out


def main():
    print(f"Phase 13 — pair universe recheck with data-quality criteria")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print()

    client = WazirXClient()
    # First fetch all-tickers to get turnover/spread for ranking.
    print("Fetching all-tickers...")
    try:
        prices, prov = client.current_prices()
    except Exception as e:
        print(f"  FAILED: {e}")
        return
    print(f"  Got {len(prices)} pairs. source={prov.source.value}")
    print()

    # Build turnover+spread lookup
    ticker_lookup = {}
    for pair, t in prices.items():
        ticker_lookup[pair] = {
            "turnover_inr": float(t.get("volume_24h", 0) or 0),
            "spread_pct": float(t.get("spread_pct", 0) or 0),
            "last_price": float(t.get("last_price", 0) or 0),
        }

    # Vet each candidate pair
    results = []
    for pair in CANDIDATE_PAIRS:
        print(f"  Vetting {pair}...", end="", flush=True)
        v = vet_pair(client, pair)
        # Enrich with ticker info
        if pair in ticker_lookup:
            v["ticker"] = ticker_lookup[pair]
        results.append(v)
        print(f"  status={v.get('status', '?')} "
              f"age={v.get('age_hours', '?')}h "
              f"turnover=₹{v.get('ticker', {}).get('turnover_inr', 0):,.0f} "
              f"spread={v.get('ticker', {}).get('spread_pct', 0):.3f}%")
        time.sleep(0.3)  # polite delay

    # Final selection: top 10-15 pairs by turnover, fresh, spread < 1.0%
    eligible = [r for r in results
                if r.get("status") == "fresh"
                and r.get("history_500_count", 0) >= 500
                and r.get("ticker", {}).get("turnover_inr", 0) > 0]
    # Filter by spread < 1.0% (data-quality rule)
    eligible = [r for r in eligible
                if r.get("ticker", {}).get("spread_pct", 999) < 1.0]
    # Sort by turnover descending
    eligible.sort(key=lambda r: -r.get("ticker", {}).get("turnover_inr", 0))
    # Take top 15
    selected = eligible[:15]

    excluded = [r for r in results if r not in eligible]
    print()
    print(f"Total candidates vetted: {len(results)}")
    print(f"Eligible (fresh + >=500 history + spread<1% + positive turnover): {len(eligible)}")
    print(f"Selected top 15 by turnover:")
    for r in selected:
        print(f"  {r['pair']:14s}  turnover=₹{r['ticker']['turnover_inr']:>12,.0f}  "
              f"spread={r['ticker']['spread_pct']:.3f}%  "
              f"history={r['history_500_count']} candles")
    print()
    print(f"Excluded ({len(excluded)} pairs):")
    for r in excluded:
        print(f"  {r['pair']:14s}  status={r.get('status')}  "
              f"reason={r.get('exclusion_reason', 'ineligible')}")

    out = {
        "phase": 13,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_source": "WazirX public tickers + klines",
        "candidates_vetted": len(results),
        "eligible_count": len(eligible),
        "selected_top_15": [
            {"pair": r["pair"],
             "turnover_inr": r["ticker"]["turnover_inr"],
             "spread_pct": r["ticker"]["spread_pct"],
             "history_500_count": r["history_500_count"],
             "history_first_iso": r.get("history_500_first_iso"),
             "history_last_iso": r.get("history_500_last_iso")}
            for r in selected
        ],
        "excluded": [
            {"pair": r["pair"], "status": r.get("status"),
             "reason": r.get("exclusion_reason", "ineligible")}
            for r in excluded
        ],
        "all_vetted": results,
    }
    out_path = "/home/z/my-project/download/phase13_pair_universe_vetted.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print()
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
