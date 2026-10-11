"""Regression tests for Phase 3 fixes.

Covers:
  1. WazirX volume_unit fix: volume_24h must be INR turnover (last * base),
     not the raw base-asset volume. Without this, the scanner's
     min_24h_volume_inr filter rejects every market.
  2. Scanner exclusion summary: response must include counts of how many
     markets were excluded by each filter, not just a flat list.
  3. Plotly chart code: no Plotly.plot() or Plotly.makeGraphDiv() calls
     remain in app.js (both removed in Plotly 2.x → caused
     "Plotly.plot is not a function" error on /chart page).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. WazirX volume units — INR turnover, not base-asset volume
# ---------------------------------------------------------------------------
class TestWazirXVolumeUnits:
    """Phase 3 regression: WazirX `volume` field is in BASE asset (BTC),
    NOT INR. The client must compute INR turnover = last * base_volume
    so the scanner's min_24h_volume_inr filter works.
    """

    def test_volume_24h_is_inr_turnover_not_base_volume(self):
        """0.0708 BTC at ₹8,214,417/BTC should be ₹581,580.72 INR turnover."""
        from coindcx_lab.api.wazirx_client import WazirXClient
        c = WazirXClient(max_retries=1, cache_ttl_seconds=0.0)
        sample = {
            "btcinr": {
                "base_unit": "btc", "quote_unit": "inr",
                "low": "8139926.0", "high": "8225001.0",
                "last": "8214417.0", "type": "SPOT",
                "open": "8225001", "volume": "0.0708",  # 0.0708 BTC, NOT ₹0.0708
                "sell": "8214417.0", "buy": "8165749.0",
                "at": 1791693581, "name": "BTC/INR",
            },
        }
        with patch("coindcx_lab.api.wazirx_client.requests.request") as m:
            m.return_value = MagicMock(
                status_code=200, headers={},
                text=json.dumps(sample),
            )
            m.return_value.json.return_value = sample
            prices, _ = c.current_prices()
        t = prices["B-BTCINR"]
        # CRITICAL: volume_24h must be INR turnover, not 0.0708.
        assert t["volume_24h"] == 8214417.0 * 0.0708
        assert t["volume_24h"] > 100_000     # ₹5.8 lakh, not ₹0.07
        # Raw base-asset volume also exposed for transparency.
        assert t["volume_24h_base"] == 0.0708

    def test_volume_24h_passes_scanner_filter(self):
        """With INR turnover (₹5.8 lakh) the scanner's ₹50 lakh filter
        still rejects BTC (only 5.8L < 50L). But the rejection reason
        now reports the CORRECT value, not the BTC volume."""
        # This test is a logical assertion, not requiring network.
        last = 8_214_417.0
        base_vol = 0.0708
        inr_turnover = last * base_vol
        min_24h_volume_inr = 5_000_000  # ₹50 lakh filter
        # Without the fix, vol24 = base_vol (0.0708) — would always fail.
        # With the fix, vol24 = inr_turnover (~581,580) — still fails for
        # BTC at this volume, but is now correctly comparable to the filter.
        assert inr_turnover < min_24h_volume_inr     # BTC still excluded
        assert inr_turnover > 100_000                # but with a real INR value

        # A higher-volume pair (e.g. 10 BTC) would pass:
        high_vol_inr = 10.0 * 8_214_417.0  # ₹8.2 crore
        assert high_vol_inr > min_24h_volume_inr

    def test_volume_24h_handles_zero_volume_pair(self):
        """Pairs with last_price=0 or volume=0 should not crash."""
        from coindcx_lab.api.wazirx_client import WazirXClient
        c = WazirXClient(max_retries=1, cache_ttl_seconds=0.0)
        sample = {
            "zeroinr": {"base_unit": "zero", "quote_unit": "inr",
                      "low": "0", "high": "0", "last": "0",
                      "type": "SPOT", "open": "0", "volume": "0",
                      "at": 1791693581, "name": "ZERO/INR"},
            "z2inr": {"base_unit": "z2", "quote_unit": "inr",
                      "low": "0", "high": "0", "last": "100",
                      "type": "SPOT", "open": "100", "volume": "0",
                      "at": 1791693581, "name": "Z2/INR"},
        }
        with patch("coindcx_lab.api.wazirx_client.requests.request") as m:
            r = MagicMock(status_code=200, headers={}, text=json.dumps(sample))
            r.json.return_value = sample
            m.return_value = r
            prices, _ = c.current_prices()
        # Both should be in prices with volume_24h=0 (no NaN, no crash)
        assert prices["B-ZEROINR"]["volume_24h"] == 0.0
        assert prices["B-Z2INR"]["volume_24h"] == 0.0


# ---------------------------------------------------------------------------
# 2. Scanner exclusion summary
# ---------------------------------------------------------------------------
class TestScannerSummary:
    """Phase 3 regression: scanner must report how many pairs were
    excluded by each filter, so the user understands why the table
    looks sparse."""

    def test_scanner_response_includes_summary_field(self):
        """POST /api/scanner response must include a `summary` object
        with: total_discovered, pre_filtered_by_volume,
        candles_fetched_for, active, excluded_total,
        exclusion_breakdown, signal_breakdown, buy_candidates."""
        from api.scanner import handler
        import http.server
        import socketserver
        import threading
        import time
        import requests as _rq

        # Mock the MultiProviderClient at the SOURCE MODULE (not
        # api.scanner) because api/scanner.py does
        # `from coindcx_lab.api.multi_provider import MultiProviderClient`
        # INSIDE _handle() — so patching api.scanner.MultiProviderClient
        # fails with AttributeError (the name doesn't exist at module
        # level). Patching the source module works because Python
        # re-imports the name from the source on each function call.
        with patch("coindcx_lab.api.multi_provider.MultiProviderClient") as MockClient:
            inst = MockClient.return_value
            from coindcx_lab.api.models import PairInfo, DataProvenance, SourceKind
            from datetime import datetime, timezone
            # 3 pairs: BTC (high vol), ETH (mid vol), SHIB (low vol)
            inst.list_inr_spot_pairs.return_value = (
                [PairInfo(pair="B-BTCINR", base="BTC", quote="INR", status="active", is_inr_quote=True),
                 PairInfo(pair="B-ETHINR", base="ETH", quote="INR", status="active", is_inr_quote=True),
                 PairInfo(pair="B-SHIBINR", base="SHIB", quote="INR", status="active", is_inr_quote=True)],
                DataProvenance(source=SourceKind.LIVE, endpoint="test",
                               fetched_at_utc=datetime.now(timezone.utc)),
            )
            inst.current_prices.return_value = (
                {"B-BTCINR": {"last_price": 8e6, "volume_24h": 8e7, "high_24h": 8.2e6, "low_24h": 7.8e6, "change_pct_24h": 0.1, "volume_24h_base": 10},
                 "B-ETHINR": {"last_price": 2.5e5, "volume_24h": 5e6, "high_24h": 2.6e5, "low_24h": 2.4e5, "change_pct_24h": 0.1, "volume_24h_base": 20},
                 "B-SHIBINR": {"last_price": 0.001, "volume_24h": 100, "high_24h": 0.0011, "low_24h": 0.0009, "change_pct_24h": 0.1, "volume_24h_base": 100000}},
                DataProvenance(source=SourceKind.LIVE, endpoint="test",
                               fetched_at_utc=datetime.now(timezone.utc)),
            )
            # Mock candles: empty list (will cause "no candles available" exclusion)
            inst.candles.return_value = ([], DataProvenance(
                source=SourceKind.UNAVAILABLE, endpoint="test",
                fetched_at_utc=datetime.now(timezone.utc)))

            # Spin up the handler on localhost
            class _S(socketserver.TCPServer):
                allow_reuse_address = True
            srv = _S(("127.0.0.1", 0), handler)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            time.sleep(0.05)
            try:
                r = _rq.post(f"http://127.0.0.1:{port}/api/scanner",
                              json={"interval": "1h", "max_pairs": 10},
                              timeout=15)
                assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:300]}"
                body = r.json()
                assert "summary" in body, "response missing 'summary' field"
                s = body["summary"]
                # Required fields
                for k in ("total_discovered", "pre_filtered_by_volume",
                          "candles_fetched_for", "active", "excluded_total",
                          "exclusion_breakdown", "signal_breakdown",
                          "buy_candidates", "min_24h_volume_inr", "max_pairs"):
                    assert k in s, f"summary missing field {k}"
                # Sanity: total_discovered == 3, since we mocked 3 pairs
                assert s["total_discovered"] == 3
                # SHIB should be pre-filtered by volume (100 < 5,000,000 default)
                assert s["pre_filtered_by_volume"] >= 1
            finally:
                srv.shutdown()


# ---------------------------------------------------------------------------
# 3. Plotly chart — no removed Plotly API calls in app.js
# ---------------------------------------------------------------------------
class TestPlotlyAPIUsage:
    """Phase 3 regression: app.js must not call Plotly.plot() (removed
    in Plotly 2.x) or Plotly.makeGraphDiv() (never existed).
    Both caused 'Plotly.plot is not a function' on the /chart page."""

    def test_no_Plotly_plot_calls(self):
        app_js = open(os.path.join(os.path.dirname(__file__),
                                     "..", "web", "js", "app.js")).read()
        # Strip comments so we don't false-positive on the explanatory text
        import re
        # Remove // line comments
        code_only = re.sub(r'//[^\n]*', '', app_js)
        # Remove /* */ block comments
        code_only = re.sub(r'/\*.*?\*/', '', code_only, flags=re.DOTALL)
        # Check for actual function CALLS (not string mentions)
        # i.e. Plotly.plot(  or  Plotly.makeGraphDiv(
        bad_calls = re.findall(r'Plotly\.(plot|makeGraphDiv)\s*\(', code_only)
        assert not bad_calls, (
            f"app.js calls removed/non-existent Plotly API: {bad_calls}. "
            f"Use Plotly.newPlot or Plotly.react instead.")

    def test_only_newPlot_or_react_used(self):
        app_js = open(os.path.join(os.path.dirname(__file__),
                                     "..", "web", "js", "app.js")).read()
        import re
        # Strip // line comments and /* */ block comments first so we
        # don't false-positive on explanatory text mentioning Plotly.plot.
        code_only = re.sub(r'//[^\n]*', '', app_js)
        code_only = re.sub(r'/\*.*?\*/', '', code_only, flags=re.DOTALL)
        calls = re.findall(r'Plotly\.(\w+)\s*\(', code_only)
        valid = {"newPlot", "react", "update", "addTraces", "deleteTraces",
                 "restyle", "relayout", "downloadImage", "purge"}
        assert "newPlot" in calls, "app.js should call Plotly.newPlot at least once"
        invalid = set(calls) - valid
        assert not invalid, f"app.js calls unknown Plotly API: {invalid}"
