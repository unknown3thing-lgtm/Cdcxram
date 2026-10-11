"""Tests: WazirX public market-data client (mocked HTTP — no network).

Covers:
    - Pair-code conversion (WazirX ↔ CoinDCX format)
    - list_exchange_pairs / list_inr_spot_pairs (parse tickers → PairInfo)
    - current_prices (parse tickers → dict of CoinDCX pair → ticker)
    - candles (parse klines arrays → Candle objects with open_time_ms in MILLISECONDS)
    - HTTP error handling (403, 429, 5xx, malformed JSON)
    - Cache hit returns CACHED provenance

Uses unittest.mock to inject fake HTTP responses so the tests run
without any network access.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


def _mock_response(status_code: int, body, *, headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.reason = "OK" if status_code < 400 else "ERR"
    r.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
    r.headers = headers or {"Date": "Sat, 11 Oct 2026 12:00:00 GMT"}
    r.json.return_value = body
    return r


def _sample_all_tickers():
    return {
        "btcinr": {
            "base_unit": "btc", "quote_unit": "inr",
            "low": "8139926.0", "high": "8225001.0",
            "last": "8214417.0", "type": "SPOT",
            "open": "8225001", "volume": "0.0708",
            "sell": "8214417.0", "buy": "8165749.0",
            "at": 1791693581, "name": "BTC/INR",
        },
        "ethinr": {
            "base_unit": "eth", "quote_unit": "inr",
            "low": "247000.0", "high": "249686.4",
            "last": "249610.2", "type": "SPOT",
            "open": "249606.6", "volume": "1.5",
            "sell": "249610.0", "buy": "249000.0",
            "at": 1791693581, "name": "ETH/INR",
        },
        # Non-INR pair — should be filtered out
        "btcusdt": {
            "base_unit": "btc", "quote_unit": "usdt",
            "last": "50000.0", "type": "SPOT",
            "at": 1791693581, "name": "BTC/USDT",
        },
        # Non-SPOT — should be filtered out
        "btcinr_futures": {
            "base_unit": "btc", "quote_unit": "inr",
            "last": "8200000.0", "type": "FUTURES",
            "at": 1791693581, "name": "BTC/INR-FUT",
        },
    }


def _sample_klines():
    # WazirX returns: [open_time_seconds, open, high, low, close, volume]
    return [
        [1791684000, 8214417.0, 8214417.0, 8153898.0, 8214417.0, 0.00037],
        [1791687600, 8158808.0, 8214417.0, 8158808.0, 8214417.0, 0.00106],
        [1791691200, 8158258.0, 8214417.0, 8158258.0, 8214417.0, 0.00160],
    ]


# ---------------------------------------------------------------------------
# Pair-code conversion
# ---------------------------------------------------------------------------
class TestPairConversion:
    def test_wazirx_to_coindcx(self):
        from coindcx_lab.api.wazirx_client import wazirx_pair_to_coindcx
        assert wazirx_pair_to_coindcx("btcinr") == "B-BTCINR"
        assert wazirx_pair_to_coindcx("ethinr") == "B-ETHINR"
        assert wazirx_pair_to_coindcx("shibinr") == "B-SHIBINR"

    def test_wazirx_to_coindcx_invalid(self):
        from coindcx_lab.api.wazirx_client import wazirx_pair_to_coindcx
        assert wazirx_pair_to_coindcx("") is None
        assert wazirx_pair_to_coindcx(None) is None

    def test_coindcx_to_wazirx(self):
        from coindcx_lab.api.wazirx_client import coindcx_pair_to_wazirx
        assert coindcx_pair_to_wazirx("B-BTCINR") == "btcinr"
        assert coindcx_pair_to_wazirx("B-ETHINR") == "ethinr"

    def test_coindcx_to_wazirx_invalid(self):
        from coindcx_lab.api.wazirx_client import coindcx_pair_to_wazirx
        assert coindcx_pair_to_wazirx("BTCINR") is None    # missing B- prefix
        assert coindcx_pair_to_wazirx("") is None
        assert coindcx_pair_to_wazirx(None) is None


# ---------------------------------------------------------------------------
# list_exchange_pairs / list_inr_spot_pairs
# ---------------------------------------------------------------------------
class TestListPairs:
    def test_returns_inr_spot_pairs_only(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        c = WazirXClient(max_retries=1)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            mock_req.return_value = _mock_response(200, _sample_all_tickers())
            pairs, prov = c.list_exchange_pairs()
            # Should have 2 INR spot pairs (btcinr + ethinr)
            # btcusdt and btcinr_futures are filtered out
            assert len(pairs) == 2
            codes = sorted(p.pair for p in pairs)
            assert codes == ["B-BTCINR", "B-ETHINR"]
            assert all(p.is_inr_quote for p in pairs)
            # Provenance: LIVE source, wazirx endpoint
            assert "wazirx" in prov.endpoint
            assert prov.source.value == "live"

    def test_filters_non_inr_pairs(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        c = WazirXClient(max_retries=1)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            mock_req.return_value = _mock_response(200, _sample_all_tickers())
            pairs, _ = c.list_inr_spot_pairs()
            # No USDT or FUTURES pairs in result
            for p in pairs:
                assert p.quote == "INR"

    def test_403_raises_immediately(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        from coindcx_lab.api.client import CoinDCXAPIError
        c = WazirXClient(max_retries=3, backoff_seconds=0.001)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            mock_req.return_value = _mock_response(403, "<html>Forbidden</html>")
            with pytest.raises(CoinDCXAPIError) as e:
                c.list_exchange_pairs()
            assert "403" in str(e.value)
            # 403 is non-retryable — only one HTTP call
            assert mock_req.call_count == 1

    def test_429_raises_rate_limit(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        from coindcx_lab.api.client import RateLimitError
        c = WazirXClient(max_retries=3, backoff_seconds=0.001)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            mock_req.return_value = _mock_response(429, {"error": "rate"})
            with pytest.raises(RateLimitError):
                c.list_exchange_pairs()
            assert mock_req.call_count == 1


# ---------------------------------------------------------------------------
# current_prices
# ---------------------------------------------------------------------------
class TestCurrentPrices:
    def test_returns_dict_keyed_by_coindcx_pair(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        c = WazirXClient(max_retries=1, cache_ttl_seconds=0.0)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            mock_req.return_value = _mock_response(200, _sample_all_tickers())
            prices, prov = c.current_prices()
            # Keys are CoinDCX pair codes
            assert "B-BTCINR" in prices
            assert "B-ETHINR" in prices
            # No WazirX-format keys
            assert "btcinr" not in prices
            assert "btcusdt" not in prices   # filtered out
            # Ticker shape matches CoinDCX client output
            ticker = prices["B-BTCINR"]
            assert ticker["last_price"] == 8214417.0
            assert ticker["high_24h"] == 8225001.0
            assert ticker["low_24h"] == 8139926.0
            # WazirX `volume` is in BASE asset (BTC). The client must
            # convert it to INR turnover (last * base_volume) so the
            # scanner's min_24h_volume_inr filter works correctly.
            # 0.0708 BTC * ₹8214417 = ₹581,580.72 INR turnover.
            assert ticker["volume_24h"] == 8214417.0 * 0.0708
            # And the raw base-asset volume is also exposed for transparency.
            assert ticker["volume_24h_base"] == 0.0708
            # change_pct_24h = (last - open) / open * 100
            expected = (8214417.0 - 8225001) / 8225001 * 100
            assert abs(ticker["change_pct_24h"] - expected) < 0.001
            # Provenance
            assert prov.source.value == "live"
            assert "wazirx" in prov.endpoint

    def test_cache_hit_returns_cached_provenance(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        from coindcx_lab.api.models import SourceKind
        c = WazirXClient(max_retries=1, cache_ttl_seconds=60.0)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            mock_req.return_value = _mock_response(200, _sample_all_tickers())
            # First call → LIVE
            _, prov1 = c.current_prices()
            assert prov1.source is SourceKind.LIVE
            # Second call → CACHED (within TTL)
            _, prov2 = c.current_prices()
            assert prov2.source is SourceKind.CACHED
            # Only ONE HTTP request was made
            assert mock_req.call_count == 1


# ---------------------------------------------------------------------------
# candles
# ---------------------------------------------------------------------------
class TestCandles:
    def test_returns_candle_objects_with_ms_open_time(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        from coindcx_lab.api.models import Candle
        c = WazirXClient(max_retries=1)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            mock_req.return_value = _mock_response(200, _sample_klines())
            candles, prov = c.candles(pair="B-BTCINR", interval="1h", limit=3)
            assert len(candles) == 3
            for cdl in candles:
                assert isinstance(cdl, Candle)
                # open_time_ms should be in MILLISECONDS (WazirX returns seconds)
                assert cdl.open_time_ms > 1_000_000_000_000  # ms not sec
                # OHLC consistency
                assert cdl.high >= max(cdl.open, cdl.close, cdl.low)
                assert cdl.low <= min(cdl.open, cdl.close, cdl.high)
            # First candle open_time_ms == first kline open_time_seconds * 1000
            assert candles[0].open_time_ms == 1791684000 * 1000
            # Provenance
            assert prov.source.value == "live"
            assert "wazirx" in prov.endpoint

    def test_invalid_pair_raises_value_error(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        c = WazirXClient(max_retries=1)
        with pytest.raises(ValueError):
            c.candles(pair="invalid-pair", interval="1h", limit=3)

    def test_invalid_interval_raises_value_error(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        c = WazirXClient(max_retries=1)
        with pytest.raises(ValueError):
            c.candles(pair="B-BTCINR", interval="2min", limit=3)

    def test_malformed_klines_payload_returns_empty(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        from coindcx_lab.api.client import CoinDCXAPIError
        c = WazirXClient(max_retries=1)
        # Malformed: not a list
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            mock_req.return_value = _mock_response(200, {"unexpected": "shape"})
            # Should raise OHLCVValidationError because validate_candles
            # requires >= 1 candle
            with pytest.raises(Exception):
                c.candles(pair="B-BTCINR", interval="1h", limit=3)

    def test_klines_with_short_arrays_skipped(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        c = WazirXClient(max_retries=1)
        # Klines with 5 elements (missing volume) should be skipped
        bad_klines = [
            [1791684000, 8214417.0, 8214417.0, 8153898.0, 8214417.0],
        ]
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            mock_req.return_value = _mock_response(200, bad_klines)
            with pytest.raises(Exception):
                c.candles(pair="B-BTCINR", interval="1h", limit=3)

    def test_5xx_retries_then_raises(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        from coindcx_lab.api.client import CoinDCXAPIError
        import requests as _rq
        c = WazirXClient(max_retries=2, backoff_seconds=0.001)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            mock_req.return_value = _mock_response(500, "<html>err</html>")
            with pytest.raises(CoinDCXAPIError):
                c.candles(pair="B-BTCINR", interval="1h", limit=3)
            assert mock_req.call_count == 2

    def test_malformed_json_raises(self):
        from coindcx_lab.api.wazirx_client import WazirXClient
        from coindcx_lab.api.client import CoinDCXAPIError
        c = WazirXClient(max_retries=1)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req:
            r = _mock_response(200, "not-json")
            r.json.side_effect = json.JSONDecodeError("no", "not-json", 0)
            mock_req.return_value = r
            with pytest.raises(CoinDCXAPIError):
                c.candles(pair="B-BTCINR", interval="1h", limit=3)
