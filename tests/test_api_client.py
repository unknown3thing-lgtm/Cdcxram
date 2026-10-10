"""Tests: API client failure handling, mock responses, no-network.

These tests use unittest.mock to inject fake HTTP responses so that the
API client's parsing, retry, and error-handling paths are exercised
without any network access.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from coindcx_lab.api.client import (
    CoinDCXAPIError, CoinDCXClient, RateLimitError,
)
from coindcx_lab.api.models import SourceKind


def _make_response(status_code: int, body, *, headers: dict = None):
    r = MagicMock()
    r.status_code = status_code
    r.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
    r.headers = headers or {}
    r.json.return_value = body
    return r


# ---------------------------------------------------------------------------
# Exchange pairs parsing
# ---------------------------------------------------------------------------
class TestParsePairs:
    def test_parses_dict_form(self):
        body = [
            {"pair": "B-BTCINR", "base_currency": "INR",
             "target_currency": "BTC", "status": "active"},
            {"pair": "B-ETHINR", "base_currency": "INR",
             "target_currency": "ETH", "status": "active"},
        ]
        pairs = CoinDCXClient._parse_pairs(body)
        assert len(pairs) == 2
        assert pairs[0].base == "BTC"
        assert pairs[0].quote == "INR"
        assert pairs[0].is_inr_quote is True

    def test_parses_when_no_metadata_uses_pair_suffix(self):
        body = [{"pair": "B-SOLINR", "status": "active"}]
        pairs = CoinDCXClient._parse_pairs(body)
        assert pairs[0].base == "SOL"
        assert pairs[0].quote == "INR"

    def test_skips_non_dict_entries(self):
        body = [{"pair": "B-BTCINR"}, "junk", 42, None]
        pairs = CoinDCXClient._parse_pairs(body)
        assert len(pairs) == 1


# ---------------------------------------------------------------------------
# Candles parsing
# ---------------------------------------------------------------------------
class TestParseCandles:
    def test_parses_dict_form(self):
        body = [
            {"open": "100.0", "high": "101.0", "low": "99.0",
             "close": "100.5", "volume": "1000", "time": 1700000000000},
        ]
        cs = CoinDCXClient._parse_candles(body, pair="B-BTCINR", interval="1h")
        assert len(cs) == 1
        assert cs[0].open == 100.0
        assert cs[0].close_time_ms == 1700000000000 + 3_600_000 - 1

    def test_parses_list_form(self):
        body = [[100.0, 101.0, 99.0, 100.5, 1000, 1700000000000]]
        cs = CoinDCXClient._parse_candles(body, pair="B-BTCINR", interval="1h")
        assert len(cs) == 1
        assert cs[0].open == 100.0

    def test_skips_missing_time(self):
        body = [
            {"open": "100", "high": "101", "low": "99",
             "close": "100", "volume": "0", "time": 0},
            {"open": "100", "high": "101", "low": "99",
             "close": "100", "volume": "0", "time": 1700000000000},
        ]
        cs = CoinDCXClient._parse_candles(body, pair="B-BTCINR", interval="1h")
        assert len(cs) == 1   # only the one with a valid time


# ---------------------------------------------------------------------------
# Failure handling — mocked HTTP
# ---------------------------------------------------------------------------
class TestFailureHandling:
    def test_403_raises_immediately_no_retry(self):
        """Cloudflare edge block must NOT be retried."""
        client = CoinDCXClient(max_retries=3, backoff_seconds=0.001)
        with patch("coindcx_lab.api.client.requests.request") as mock_req:
            mock_req.return_value = _make_response(403, "<html>blocked</html>")
            with pytest.raises(CoinDCXAPIError) as exc:
                client.list_exchange_pairs()
            assert "403" in str(exc.value)
            # Only ONE call — 403 is not retried.
            assert mock_req.call_count == 1

    def test_429_raises_rate_limit(self):
        client = CoinDCXClient(max_retries=3, backoff_seconds=0.001)
        with patch("coindcx_lab.api.client.requests.request") as mock_req:
            mock_req.return_value = _make_response(429, {"error": "rate"})
            with pytest.raises(RateLimitError):
                client.current_prices()
            assert mock_req.call_count == 1   # not retried

    def test_timeout_retries_then_raises(self):
        """A timeout should retry up to max_retries, then raise."""
        import requests as _rq
        client = CoinDCXClient(max_retries=3, backoff_seconds=0.001)
        with patch("coindcx_lab.api.client.requests.request") as mock_req:
            mock_req.side_effect = _rq.Timeout("timeout")
            with pytest.raises(CoinDCXAPIError):
                client.current_prices()
            assert mock_req.call_count == 3

    def test_500_retries_then_raises(self):
        """5xx errors are retryable. We try max_retries times then raise."""
        client = CoinDCXClient(max_retries=2, backoff_seconds=0.001)
        with patch("coindcx_lab.api.client.requests.request") as mock_req:
            mock_req.return_value = _make_response(500, "<html>err</html>")
            with pytest.raises(CoinDCXAPIError):
                client.current_prices()
            assert mock_req.call_count == 2

    def test_malformed_json_raises(self):
        client = CoinDCXClient()
        with patch("coindcx_lab.api.client.requests.request") as mock_req:
            mock_req.return_value = _make_response(200, "not-json")
            # Patch .json to raise
            mock_req.return_value.json.side_effect = json.JSONDecodeError(
                "no", "not-json", 0)
            with pytest.raises(CoinDCXAPIError):
                client.current_prices()

    def test_unsupported_interval_rejected(self):
        client = CoinDCXClient()
        with pytest.raises(ValueError):
            client.candles(pair="B-BTCINR", interval="2h")   # not supported


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
class TestCache:
    def test_current_prices_caches(self):
        client = CoinDCXClient(cache_ttl_seconds=5.0)
        with patch("coindcx_lab.api.client.requests.request") as mock_req:
            mock_req.return_value = _make_response(200,
                {"B-BTCINR": {"last_price": "100", "h": "101",
                              "l": "99", "v": "1000", "c": "0.5"}})
            prices1, prov1 = client.current_prices()
            prices2, prov2 = client.current_prices()
            assert prov1.source is SourceKind.LIVE
            assert prov2.source is SourceKind.CACHED
            assert mock_req.call_count == 1     # second call hit the cache
            assert prices1 == prices2


# ---------------------------------------------------------------------------
# Provenance labelling
# ---------------------------------------------------------------------------
class TestProvenance:
    def test_live_label(self):
        from coindcx_lab.api.models import DataProvenance
        prov = DataProvenance(
            source=SourceKind.LIVE, endpoint="/x",
            fetched_at_utc=datetime.now(timezone.utc),
        )
        assert "LIVE" in prov.label()

    def test_simulated_label(self):
        from coindcx_lab.api.models import DataProvenance
        prov = DataProvenance(
            source=SourceKind.SIMULATED, endpoint="synthetic://x",
            fetched_at_utc=datetime.now(timezone.utc),
        )
        assert "SIMULATED" in prov.label()
        assert "no live prices" in prov.label()

    def test_unavailable_label(self):
        from coindcx_lab.api.models import DataProvenance
        prov = DataProvenance(
            source=SourceKind.UNAVAILABLE, endpoint="x",
            fetched_at_utc=datetime.now(timezone.utc),
        )
        assert "UNAVAILABLE" in prov.label()


# ---------------------------------------------------------------------------
# Synthetic generator determinism
# ---------------------------------------------------------------------------
class TestSynthetic:
    def test_deterministic_for_same_args(self, synth):
        c1, _ = synth.series(pair="B-BTCINR", interval="1h", count=50)
        c2, _ = synth.series(pair="B-BTCINR", interval="1h", count=50)
        assert [c.open for c in c1] == [c.open for c in c2]

    def test_different_pair_different_series(self, synth):
        c1, _ = synth.series(pair="B-BTCINR", interval="1h", count=50)
        c2, _ = synth.series(pair="B-ETHINR", interval="1h", count=50)
        assert [c.open for c in c1] != [c.open for c in c2]
