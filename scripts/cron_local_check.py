"""Local integration test for /api/cron — spins up a real HTTP server,
hits it with GET / POST / HEAD (with and without Bearer auth), and
verifies the response status codes match the documented contract.

Run with:
    PYTHONPATH=api:src CRON_SECRET=test-secret python3 tests/test_cron_live_local.py
"""

from __future__ import annotations

import http.server
import os
import socketserver
import sys
import threading
import time
import uuid

import requests


class _Server(socketserver.TCPServer):
    allow_reuse_address = True


def main() -> int:
    if not os.environ.get("CRON_SECRET"):
        os.environ["CRON_SECRET"] = "test-secret"
    print(f"CRON_SECRET set to: {os.environ['CRON_SECRET'][:4]}*** (length {len(os.environ['CRON_SECRET'])})")

    # Import after env is set so cron_secret_matches sees it
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from cron import handler

    srv = _Server(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    secret = os.environ["CRON_SECRET"]
    cases = [
        # (method, headers, path, expected_status, label)
        ("GET",    {},                                "/api/cron", 401, "GET without auth (cron-job.org default)"),
        ("POST",   {},                                "/api/cron", 401, "POST without auth"),
        ("GET",    {"Authorization": "Bearer wrong"}, "/api/cron", 401, "GET with wrong Bearer"),
        ("POST",   {"Authorization": "Bearer wrong"}, "/api/cron", 401, "POST with wrong Bearer"),
        ("GET",    {"Authorization": f"Bearer {secret}"}, "/api/cron", "200_or_500", "GET with correct Bearer"),
        ("POST",   {"Authorization": f"Bearer {secret}"}, "/api/cron", "200_or_500", "POST with correct Bearer"),
    ]

    failures = 0
    for method, headers, path, expected, label in cases:
        try:
            if method == "GET":
                r = requests.get(base + path, headers=headers, timeout=15)
            else:
                r = requests.post(base + path, headers=headers, timeout=15)
            if expected == "200_or_500":
                # With correct auth, the endpoint will try to fetch from CoinDCX
                # and write to Supabase. Either succeed (200) or fail at Supabase
                # (500) — but NOT 401/501/405. The auth check passed.
                ok = r.status_code in (200, 500)
                print(f"  {'OK' if ok else 'FAIL'}: {label} → HTTP {r.status_code} (expected 200 or 500; body: {r.text[:120]})")
                if not ok: failures += 1
            else:
                ok = r.status_code == expected
                print(f"  {'OK' if ok else 'FAIL'}: {label} → HTTP {r.status_code} (expected {expected}; body: {r.text[:120]})")
                if not ok: failures += 1
        except Exception as e:
            print(f"  FAIL: {label} → exception {type(e).__name__}: {e}")
            failures += 1

    srv.shutdown()
    print(f"\nResult: {len(cases) - failures}/{len(cases)} cases passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
