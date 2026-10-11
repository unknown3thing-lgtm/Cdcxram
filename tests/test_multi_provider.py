"""Tests: MultiProviderClient — provider fallback logic.

Verifies that:
    - When the first provider (CoinDCX) fails, the second (WazirX) is tried.
    - When the second provider succeeds, the response carries the WazirX
      provenance (endpoint=wazirx, source=live).
    - When ALL providers fail, CoinDCXAPIError is raised so the caller
      can fall back to clearly-labelled SIMULATED data.
    - When the FIRST provider succeeds, the second is NOT tried.
    - The `skip_coindcx=True` flag bypasses CoinDCX entirely.
    - ValueError on invalid pair/interval propagates immediately (no fallback).
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


def _make_provider(name: str, *, succeed: bool, payload=None, error=None):
    """Build a fake provider with the standard interface."""
    p = MagicMock()
    p.__class__.__name__ = name
    type(p).__name__ = name   # for isinstance checks if needed
    if succeed:
        p.list_exchange_pairs.return_value = (payload or [], _live_prov(name))
        p.list_inr_spot_pairs.return_value = (payload or [], _live_prov(name))
        p.current_prices.return_value = (payload or {}, _live_prov(name))
        p.candles.return_value = (payload or [], _live_prov(name))
    else:
        from coindcx_lab.api.client import CoinDCXAPIError
        err = error or CoinDCXAPIError(f"{name} failed")
        p.list_exchange_pairs.side_effect = err
        p.list_inr_spot_pairs.side_effect = err
        p.current_prices.side_effect = err
        p.candles.side_effect = err
    return p


def _live_prov(name: str):
    from coindcx_lab.api.models import DataProvenance, SourceKind
    return DataProvenance(
        source=SourceKind.LIVE,
        endpoint=f"https://{name.lower()}.example.com/",
        fetched_at_utc=datetime.now(timezone.utc),
        note=f"{name} live data",
    )


# ---------------------------------------------------------------------------
# current_prices
# ---------------------------------------------------------------------------
class TestCurrentPricesFallback:
    def test_first_provider_succeeds_second_not_tried(self):
        from coindcx_lab.api.multi_provider import MultiProviderClient
        p1 = _make_provider("CoinDCX", succeed=True, payload={"B-BTCINR": {"last_price": 100.0}})
        p2 = _make_provider("WazirX",   succeed=True, payload={"B-BTCINR": {"last_price": 200.0}})
        c = MultiProviderClient(providers=[p1, p2])
        prices, prov = c.current_prices()
        # Should use p1's data
        assert prices == {"B-BTCINR": {"last_price": 100.0}}
        # p2 should not have been called
        p2.current_prices.assert_not_called()

    def test_first_fails_second_succeeds(self):
        from coindcx_lab.api.multi_provider import MultiProviderClient
        p1 = _make_provider("CoinDCX", succeed=False)
        p2 = _make_provider("WazirX",   succeed=True,
                              payload={"B-BTCINR": {"last_price": 8214417.0}})
        c = MultiProviderClient(providers=[p1, p2])
        prices, prov = c.current_prices()
        assert prices == {"B-BTCINR": {"last_price": 8214417.0}}
        # Provenance should be from WazirX
        assert "wazirx" in prov.endpoint
        # Both providers were tried
        p1.current_prices.assert_called_once()
        p2.current_prices.assert_called_once()

    def test_all_providers_fail_raises(self):
        from coindcx_lab.api.multi_provider import MultiProviderClient
        from coindcx_lab.api.client import CoinDCXAPIError
        p1 = _make_provider("CoinDCX", succeed=False,
                              error=CoinDCXAPIError("coindcx 404"))
        p2 = _make_provider("WazirX",   succeed=False,
                              error=CoinDCXAPIError("wazirx timeout"))
        c = MultiProviderClient(providers=[p1, p2])
        with pytest.raises(CoinDCXAPIError) as e:
            c.current_prices()
        # Error should mention the last failure
        assert "wazirx timeout" in str(e.value) or "All providers failed" in str(e.value)

    def test_empty_response_treated_as_failure(self):
        """If a provider returns ({}, prov) it should fall through to next."""
        from coindcx_lab.api.multi_provider import MultiProviderClient
        p1 = _make_provider("CoinDCX", succeed=True, payload={})  # empty
        p2 = _make_provider("WazirX",   succeed=True,
                              payload={"B-BTCINR": {"last_price": 1.0}})
        c = MultiProviderClient(providers=[p1, p2])
        prices, _ = c.current_prices()
        # p1's empty dict is treated as failure → fall through to p2
        assert prices == {"B-BTCINR": {"last_price": 1.0}}


# ---------------------------------------------------------------------------
# list_inr_spot_pairs
# ---------------------------------------------------------------------------
class TestListPairsFallback:
    def test_first_fails_second_succeeds(self):
        from coindcx_lab.api.multi_provider import MultiProviderClient
        from coindcx_lab.api.models import PairInfo
        p1 = _make_provider("CoinDCX", succeed=False)
        p2_pairs = [PairInfo(pair="B-BTCINR", base="BTC", quote="INR",
                              status="active", is_inr_quote=True)]
        p2 = _make_provider("WazirX", succeed=True, payload=p2_pairs)
        c = MultiProviderClient(providers=[p1, p2])
        pairs, prov = c.list_inr_spot_pairs()
        assert len(pairs) == 1
        assert pairs[0].pair == "B-BTCINR"
        assert "wazirx" in prov.endpoint


# ---------------------------------------------------------------------------
# candles
# ---------------------------------------------------------------------------
class TestCandlesFallback:
    def test_first_fails_second_succeeds(self):
        from coindcx_lab.api.multi_provider import MultiProviderClient
        from coindcx_lab.api.models import Candle
        p1 = _make_provider("CoinDCX", succeed=False)
        p2_candles = [Candle(open_time_ms=1791684000000, open=100, high=101,
                              low=99, close=100.5, volume=10,
                              close_time_ms=1791684000000+3600000-1)]
        p2 = _make_provider("WazirX", succeed=True, payload=p2_candles)
        c = MultiProviderClient(providers=[p1, p2])
        candles, prov = c.candles(pair="B-BTCINR", interval="1h", limit=10)
        assert len(candles) == 1
        assert candles[0].open_time_ms == 1791684000000
        assert "wazirx" in prov.endpoint

    def test_value_error_propagates_no_fallback(self):
        """Invalid pair/interval should raise ValueError immediately,
        NOT fall through to the next provider."""
        from coindcx_lab.api.multi_provider import MultiProviderClient
        p1 = _make_provider("CoinDCX", succeed=True)
        p1.candles.side_effect = ValueError("invalid pair code")
        p2 = _make_provider("WazirX", succeed=True)
        c = MultiProviderClient(providers=[p1, p2])
        with pytest.raises(ValueError):
            c.candles(pair="invalid", interval="1h", limit=10)
        # p2 should NOT have been called
        p2.candles.assert_not_called()


# ---------------------------------------------------------------------------
# skip_coindcx
# ---------------------------------------------------------------------------
class TestSkipCoinDCX:
    def test_skip_coindcx_bypasses_first_provider(self):
        from coindcx_lab.api.multi_provider import MultiProviderClient
        from coindcx_lab.api.client import CoinDCXClient
        from coindcx_lab.api.wazirx_client import WazirXClient
        # Real instances so isinstance check works
        p1 = MagicMock(spec=CoinDCXClient)
        p2 = MagicMock(spec=WazirXClient)
        p2.current_prices.return_value = ({"B-BTCINR": {"last_price": 1.0}},
                                            _live_prov("WazirX"))
        c = MultiProviderClient(providers=[p1, p2], skip_coindcx=True)
        prices, prov = c.current_prices()
        # p1 (CoinDCX) should not have been called
        p1.current_prices.assert_not_called()
        # p2 (WazirX) was called
        p2.current_prices.assert_called_once()
        assert prices == {"B-BTCINR": {"last_price": 1.0}}
