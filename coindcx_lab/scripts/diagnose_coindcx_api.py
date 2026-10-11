"""Thorough CoinDCX API diagnostic.

Hits every documented endpoint with a variety of headers, methods, and
bodies, and prints the actual response (status, headers, body prefix).
This script does NOT bypass Cloudflare or any access control. It simply
reports what the server says, so we can classify the cause of the 403.

Outputs JSON to stdout for downstream parsing.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

import requests


PUBLIC_BASE = "https://public.coindcx.com"

# Documented endpoints from https://docs.coindcx.com/
ENDPOINTS = [
    ("GET",  "/market_data/v3/current_prices/all_pairs", None),
    ("GET",  "/market_data/v3/exchange_pairs",            None),
    ("POST", "/market_data/v3/candles",
        {"pair": "B-BTCINR", "interval": "1h", "limit": 24}),
    # Common alternate paths observed in older docs / community wrappers:
    ("GET",  "/market_data/v3/current_prices/all_pairs", None),  # repeated for header A/B
]

HEADER_VARIANTS = [
    {
        "name": "library_ua",
        "headers": {
            "User-Agent": "coindcx-lab/0.1 (+research; no-auth)",
            "Accept": "application/json",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    },
    {
        "name": "browser_ua",
        "headers": {
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
    },
    {
        "name": "curl_ua",
        "headers": {
            "User-Agent": "curl/8.4.0",
            "Accept": "*/*",
        },
    },
]


def probe(label: str, method: str, path: str, body, headers: dict,
          timeout: float = 8.0) -> dict:
    url = PUBLIC_BASE + path
    t0 = time.time()
    out = {"label": label, "method": method, "url": url,
           "body": body, "headers_sent": headers, "timeout_s": timeout}
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=timeout)
        else:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        out["status_code"] = r.status_code
        out["reason"] = r.reason
        # Selected response headers (sanitised)
        selected = {}
        for h in ("Server", "CF-Ray", "CF-MITigated", "CF-Cache-Status",
                  "Content-Type", "Content-Length", "Retry-After",
                  "Date", "X-RateLimit-Remaining"):
            v = r.headers.get(h)
            if v is not None:
                selected[h] = v
        out["resp_headers"] = selected
        body_text = r.text or ""
        out["resp_body_prefix"] = body_text[:300]
        out["resp_body_len"] = len(body_text)
        # Classification
        if r.status_code == 403:
            if "cloudflare" in r.headers.get("Server", "").lower() \
                    or "cf-ray" in {k.lower() for k in r.headers.keys()}:
                out["classification"] = (
                    "Cloudflare edge block — the request was rejected at "
                    "the CDN edge before reaching the application. "
                    "Typical cause: data-centre IP range filter, "
                    "geographic block, or anti-bot challenge.")
            else:
                out["classification"] = (
                    "403 Forbidden — server denied access; cause not at "
                    "Cloudflare edge but at the application.")
        elif r.status_code == 429:
            out["classification"] = "Rate-limited."
        elif r.status_code >= 500:
            out["classification"] = "Server error."
        elif r.status_code >= 400:
            out["classification"] = "Client error."
        else:
            out["classification"] = "OK."
    except requests.Timeout as e:
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        out["error"] = f"Timeout: {e}"
        out["classification"] = "Network timeout."
    except requests.ConnectionError as e:
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        out["error"] = f"ConnectionError: {e}"
        out["classification"] = "Connection error."
    except Exception as e:
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        out["error"] = f"{type(e).__name__}: {e}"
        out["classification"] = "Unexpected error."
    return out


def main() -> int:
    print(f"# CoinDCX API diagnostic @ "
          f"{datetime.now(timezone.utc).isoformat()}\n")
    results = []
    for name, (method, path, body) in [
        ("current_prices_library",   ("GET",  "/market_data/v3/current_prices/all_pairs", None)),
        ("exchange_pairs_library",    ("GET",  "/market_data/v3/exchange_pairs",            None)),
        ("candles_library",           ("POST", "/market_data/v3/candles",
                                          {"pair": "B-BTCINR", "interval": "1h", "limit": 24})),
    ]:
        for variant in HEADER_VARIANTS:
            label = f"{name}__{variant['name']}"
            r = probe(label, method, path, body, variant["headers"])
            results.append(r)

    # Also test that general outbound HTTPS works (sanity check)
    sanity = []
    for url in ("https://www.google.com", "https://api.binance.com/api/v3/ping"):
        t0 = time.time()
        try:
            r = requests.get(url, timeout=8)
            sanity.append({
                "url": url, "status": r.status_code,
                "elapsed_ms": round((time.time() - t0) * 1000, 1),
            })
        except Exception as e:
            sanity.append({"url": url, "error": str(e)[:200]})

    print(json.dumps({"results": results, "sanity": sanity}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
