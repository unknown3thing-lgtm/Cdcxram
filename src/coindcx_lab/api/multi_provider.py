"""Multi-provider market-data client.

Tries providers in priority order, returns the first one that succeeds
with LIVE data. If all live providers fail, raises the last error so
the caller (api/*.py) can fall back to clearly-labelled SIMULATED
data via SyntheticGenerator.

Provider order (configurable):
    1. CoinDCXClient   (https://public.coindcx.com) — currently 404s
                         on documented endpoints; left as primary for
                         compatibility, but will fail in practice.
    2. WazirXClient     (https://api.wazirx.com)      — verified LIVE
                         INR spot pairs + OHLCV as of 2026-10-11.
    3. (caller falls back to SyntheticGenerator if both fail)

The MultiProviderClient implements the SAME interface as CoinDCXClient
and WazirXClient:
    list_exchange_pairs() -> (List[PairInfo], DataProvenance)
    list_inr_spot_pairs() -> (List[PairInfo], DataProvenance)
    current_prices()       -> (Dict[str, dict], DataProvenance)
    candles(pair, interval, limit=200, end_time_ms=None) -> (List[Candle], DataProvenance)

Each successful response carries a DataProvenance whose `endpoint`
field identifies which provider produced it:
    "https://public.coindcx.com/..."  — CoinDCX (would-be primary)
    "https://api.wazirx.com/..."      — WazirX (actual live source)
    SourceKind.SIMULATED               — synthetic fallback (set by caller)

This means the dashboard can correctly label every chart and table
with the actual source. We never label simulated data as live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .client import CoinDCXClient, CoinDCXAPIError
from .models import Candle, DataProvenance, PairInfo
from .wazirx_client import WazirXClient

log = logging.getLogger(__name__)


@dataclass
class MultiProviderClient:
    """Composite market-data client that tries each provider in order.

    The default order is [CoinDCX, WazirX] — CoinDCX first for
    compatibility (in case their public API is restored), WazirX as
    the verified-working fallback.

    Pass `providers=[...]` to override the order or add a new provider.
    Each provider must implement the same interface as CoinDCXClient.
    """

    providers: List = field(default_factory=lambda: [
        CoinDCXClient(timeout=8, max_retries=1, cache_ttl_seconds=5.0),
        WazirXClient(timeout=8, max_retries=1, cache_ttl_seconds=5.0),
    ])
    # Whether to skip CoinDCX entirely (saves ~40ms of failed-404 time
    # per request). Default False so we still try CoinDCX in case their
    # API is restored — set True for faster warm-path on WazirX.
    skip_coindcx: bool = False

    def _ordered_providers(self) -> List:
        if self.skip_coindcx:
            return [p for p in self.providers if not isinstance(p, CoinDCXClient)]
        return self.providers

    # ------------------------------------------------------------------ pairs
    def list_exchange_pairs(self) -> Tuple[List[PairInfo], DataProvenance]:
        last_error: Optional[Exception] = None
        for provider in self._ordered_providers():
            try:
                pairs, prov = provider.list_exchange_pairs()
                if pairs:
                    return pairs, prov
            except CoinDCXAPIError as e:
                log.info("provider %s failed list_exchange_pairs: %s",
                         type(provider).__name__, e)
                last_error = e
            except Exception as e:
                log.warning("provider %s raised in list_exchange_pairs: %s",
                            type(provider).__name__, e)
                last_error = e
        raise CoinDCXAPIError(
            f"All providers failed list_exchange_pairs; last error: {last_error}")

    def list_inr_spot_pairs(self) -> Tuple[List[PairInfo], DataProvenance]:
        last_error: Optional[Exception] = None
        for provider in self._ordered_providers():
            try:
                pairs, prov = provider.list_inr_spot_pairs()
                if pairs:
                    return pairs, prov
            except CoinDCXAPIError as e:
                log.info("provider %s failed list_inr_spot_pairs: %s",
                         type(provider).__name__, e)
                last_error = e
            except Exception as e:
                log.warning("provider %s raised in list_inr_spot_pairs: %s",
                            type(provider).__name__, e)
                last_error = e
        raise CoinDCXAPIError(
            f"All providers failed list_inr_spot_pairs; last error: {last_error}")

    # ------------------------------------------------------------------ prices
    def current_prices(self) -> Tuple[Dict[str, dict], DataProvenance]:
        last_error: Optional[Exception] = None
        for provider in self._ordered_providers():
            try:
                prices, prov = provider.current_prices()
                if prices:
                    return prices, prov
            except CoinDCXAPIError as e:
                log.info("provider %s failed current_prices: %s",
                         type(provider).__name__, e)
                last_error = e
            except Exception as e:
                log.warning("provider %s raised in current_prices: %s",
                            type(provider).__name__, e)
                last_error = e
        raise CoinDCXAPIError(
            f"All providers failed current_prices; last error: {last_error}")

    # ------------------------------------------------------------------ candles
    def candles(self, pair: str, interval: str,
                limit: int = 200,
                end_time_ms: Optional[int] = None
                ) -> Tuple[List[Candle], DataProvenance]:
        last_error: Optional[Exception] = None
        for provider in self._ordered_providers():
            try:
                candles, prov = provider.candles(
                    pair=pair, interval=interval, limit=limit,
                    end_time_ms=end_time_ms)
                if candles:
                    return candles, prov
            except CoinDCXAPIError as e:
                log.info("provider %s failed candles(pair=%s): %s",
                         type(provider).__name__, pair, e)
                last_error = e
            except ValueError as e:
                # Invalid pair/interval — propagate immediately, no fallback
                raise
            except Exception as e:
                log.warning("provider %s raised in candles(pair=%s): %s",
                            type(provider).__name__, pair, e)
                last_error = e
        raise CoinDCXAPIError(
            f"All providers failed candles(pair={pair}, interval={interval}); "
            f"last error: {last_error}")
