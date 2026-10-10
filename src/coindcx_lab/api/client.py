"""CoinDCX public market-data client.

Only uses endpoints documented at https://docs.coindcx.com/ for unauthenticated
public market data. Never authenticates, never sends real orders.

Design:
    - Bounded retries with exponential backoff.
    - Request timeouts.
    - Tiny in-memory TTL cache for current_prices (avoids hammering the API).
    - Honest failure handling: on 403/timeout/HTTPError we raise a typed
      exception. The caller (dashboard, scanner, backtest) decides whether
      to surface UNAVAILABLE or fall back to a clearly-labelled synthetic
      series via SyntheticGenerator.

We NEVER silently fabricate live prices, candle batches, or API responses.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from .models import Candle, DataProvenance, PairInfo, SourceKind
from .validate import validate_candles

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public CoinDCX REST endpoints (documented, unauthenticated, public only).
# ---------------------------------------------------------------------------
PUBLIC_BASE = "https://public.coindcx.com"
EP_CURRENT_PRICES = "/market_data/v3/current_prices/all_pairs"
EP_EXCHANGE_PAIRS = "/market_data/v3/exchange_pairs"
EP_CANDLES         = "/market_data/v3/candles"   # POST with JSON body

# Map our interval strings to CoinDCX interval codes.
INTERVAL_MAP: Dict[str, str] = {
    "1m":  "1m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1d",
}
INTERVAL_MS: Dict[str, int] = {
    "1m":  60_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "4h":  14_400_000,
    "1d":  86_400_000,
}

DEFAULT_HEADERS = {
    "User-Agent": "coindcx-lab/0.1 (+research; no-auth)",
    "Accept": "application/json",
    "Accept-Language": "en-IN,en;q=0.9",
}


class CoinDCXAPIError(RuntimeError):
    """Raised when the CoinDCX public API returns a hard failure
    (HTTP 4xx/5xx, malformed JSON, empty response, or network error after
    exhausting retries)."""


class RateLimitError(CoinDCXAPIError):
    """Raised specifically when the API responds 429."""


@dataclass
class _CacheEntry:
    payload: Any
    cached_at: float
    ttl: float


@dataclass
class CoinDCXClient:
    """Thin, retrying, caching REST client for CoinDCX public market data.

    All methods return a tuple of (data, DataProvenance). The provenance
    tells the dashboard exactly where the data came from so that we never
    silently mislabel simulated data as live.
    """

    base_url: str = PUBLIC_BASE
    timeout: float = 10.0
    max_retries: int = 3
    backoff_seconds: float = 0.6
    cache_ttl_seconds: float = 5.0
    user_agent: str = "coindcx-lab/0.1 (+research; no-auth)"

    _cache: Dict[str, _CacheEntry] = field(default_factory=dict)
    _cache_lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ HTTP
    def _headers(self) -> Dict[str, str]:
        h = dict(DEFAULT_HEADERS)
        h["User-Agent"] = self.user_agent
        return h

    def _request(self, method: str, path: str, *,
                body: Optional[dict] = None,
                params: Optional[dict] = None,
                skip_cache: bool = False) -> Tuple[Any, requests.Response]:
        url = self.base_url + path
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.request(
                    method, url,
                    json=body if method.upper() == "POST" else None,
                    params=params if method.upper() == "GET" else None,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                if resp.status_code == 429:
                    # Rate-limited — non-retryable for now (would need a
                    # Retry-After header to do this safely).
                    raise RateLimitError(f"429 Too Many Requests for {url}")
                if resp.status_code == 403:
                    # Cloudflare edge block (data-centre IP filter). This is
                    # the situation we currently see from this environment.
                    # Non-retryable: retrying 403 from a blocked IP just
                    # wastes time.
                    raise CoinDCXAPIError(
                        f"403 Forbidden at {url} — Cloudflare edge block. "
                        "The CoinDCX public API is unreachable from this IP range."
                    )
                if resp.status_code >= 500:
                    # 5xx is retryable.
                    last_exc = CoinDCXAPIError(
                        f"HTTP {resp.status_code} server error at {url}"
                    )
                    log.warning("attempt %d/%d got 5xx at %s",
                                attempt, self.max_retries, url)
                    if attempt < self.max_retries:
                        time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    raise last_exc
                if resp.status_code >= 400:
                    # 4xx (other than 403/429) — non-retryable.
                    raise CoinDCXAPIError(
                        f"HTTP {resp.status_code} client error at {url}: "
                        f"{resp.text[:200]}"
                    )
                # OK
                try:
                    return resp.json(), resp
                except json.JSONDecodeError as e:
                    raise CoinDCXAPIError(
                        f"Malformed JSON from {url}: {e}; body[:200]={resp.text[:200]}"
                    )
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                log.warning("attempt %d/%d failed at %s: %s",
                            attempt, self.max_retries, url, e)
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                    continue
                raise CoinDCXAPIError(
                    f"Exhausted {self.max_retries} retries for {url}: {last_exc}"
                )
            except CoinDCXAPIError as e:
                # Non-retryable errors (403, 4xx) — propagate immediately.
                raise

        # Should be unreachable but kept for safety.
        raise CoinDCXAPIError(
            f"Exhausted {self.max_retries} retries for {url}: {last_exc}"
        )

    # ---------------------------------------------------------------- cache
    def _cache_get(self, key: str) -> Optional[Any]:
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if time.time() - entry.cached_at > entry.ttl:
                del self._cache[key]
                return None
            return entry.payload
        return None  # unreachable

    def _cache_put(self, key: str, payload: Any, ttl: Optional[float] = None) -> None:
        with self._cache_lock:
            self._cache[key] = _CacheEntry(
                payload=payload, cached_at=time.time(),
                ttl=ttl if ttl is not None else self.cache_ttl_seconds,
            )

    # ----------------------------------------------------------- public API
    def list_exchange_pairs(self) -> Tuple[List[PairInfo], DataProvenance]:
        """Discover tradable pairs on CoinDCX spot.

        Returns PairInfo list filtered to spot (B-* pair code prefix is the
        convention CoinDCX uses for spot pairs).
        """
        body, resp = self._request("GET", EP_EXCHANGE_PAIRS)
        pairs = self._parse_pairs(body)
        provenance = DataProvenance(
            source=SourceKind.LIVE,
            endpoint=EP_EXCHANGE_PAIRS,
            fetched_at_utc=datetime.now(timezone.utc),
            server_time_utc=_server_time(resp),
        )
        return pairs, provenance

    def list_inr_spot_pairs(self) -> Tuple[List[PairInfo], DataProvenance]:
        """Convenience wrapper — only INR-quoted spot pairs."""
        pairs, prov = self.list_exchange_pairs()
        inr = [p for p in pairs if p.is_inr_quote]
        return inr, prov

    def current_prices(self) -> Tuple[Dict[str, dict], DataProvenance]:
        """GET /market_data/v3/current_prices/all_pairs.

        Returns map of pair_code -> {"last_price": float, ...} (only the
        fields CoinDCX exposes). Cached for cache_ttl_seconds.
        """
        cache_key = "current_prices"
        cached = self._cache_get(cache_key)
        if cached is not None:
            prov = DataProvenance(
                source=SourceKind.CACHED,
                endpoint=EP_CURRENT_PRICES,
                fetched_at_utc=datetime.now(timezone.utc),
                cache_ttl_seconds=int(self.cache_ttl_seconds),
            )
            return cached, prov

        body, resp = self._request("GET", EP_CURRENT_PRICES)
        # CoinDCX returns {"B-BTCINR": {"last_price": "3500000.0", "h": ..., "l": ...}, ...}
        cleaned: Dict[str, dict] = {}
        if isinstance(body, dict):
            for k, v in body.items():
                if not isinstance(v, dict):
                    continue
                try:
                    cleaned[k] = {
                        "last_price": float(v.get("last_price", "nan")),
                        "high_24h":  float(v.get("h", v.get("high_24h", "nan"))),
                        "low_24h":   float(v.get("l", v.get("low_24h", "nan"))),
                        "volume_24h":float(v.get("v", v.get("volume", "nan"))),
                        "change_pct_24h": float(v.get("c", v.get("change_pct", "0")) or 0),
                    }
                except (TypeError, ValueError):
                    continue
        self._cache_put(cache_key, cleaned)
        prov = DataProvenance(
            source=SourceKind.LIVE,
            endpoint=EP_CURRENT_PRICES,
            fetched_at_utc=datetime.now(timezone.utc),
            server_time_utc=_server_time(resp),
        )
        return cleaned, prov

    def candles(self, pair: str, interval: str,
                limit: int = 200,
                end_time_ms: Optional[int] = None) -> Tuple[List[Candle], DataProvenance]:
        """Fetch historical OHLCV candles for a pair.

        POST /market_data/v3/candles with JSON body:
            {"pair": "B-BTCINR", "interval": "1h", "limit": 200}
        """
        if interval not in INTERVAL_MAP:
            raise ValueError(f"Unsupported interval {interval!r}; "
                             f"supported: {list(INTERVAL_MAP)}")
        body = {"pair": pair, "interval": INTERVAL_MAP[interval], "limit": int(limit)}
        if end_time_ms is not None:
            body["endTime"] = int(end_time_ms)

        payload, resp = self._request("POST", EP_CANDLES, body=body)
        candles = self._parse_candles(payload, pair, interval)
        # Validate end-to-end; raises OHLCVValidationError on first bad bar.
        candles = validate_candles(
            candles,
            interval_ms=INTERVAL_MS.get(interval),
            min_count=1,
        )
        prov = DataProvenance(
            source=SourceKind.LIVE,
            endpoint=EP_CANDLES,
            fetched_at_utc=datetime.now(timezone.utc),
            server_time_utc=_server_time(resp),
            note=f"pair={pair} interval={interval} limit={limit}",
        )
        return candles, prov

    # ----------------------------------------------------------- parse helpers
    @staticmethod
    def _parse_pairs(body: Any) -> List[PairInfo]:
        """Parse /market_data/v3/exchange_pairs response into PairInfo list."""
        out: List[PairInfo] = []
        if isinstance(body, dict) and "products" in body:
            # Some CoinDCX responses nest under "products"
            body = body["products"]
        if not isinstance(body, list):
            return out
        for item in body:
            if not isinstance(item, dict):
                continue
            pair = str(item.get("pair", item.get("symbol", "")))
            if not pair:
                continue
            base = str(item.get("target_currency",
                                 item.get("base_currency", "")))
            quote = str(item.get("base_currency",
                                  item.get("quote_currency", "")))
            # In CoinDCX spot, the "pair" code starts with "B-" and the quote
            # is the trailing currency code (e.g. "B-BTCINR" -> BTC/INR).
            if not base or not quote:
                if pair.startswith("B-"):
                    inner = pair[2:]
                    # Heuristic: split on INR/USDT/BTC if no metadata provided
                    for q in ("INR", "USDT", "BTC"):
                        if inner.endswith(q):
                            base, quote = inner[:-len(q)], q
                            break
                    else:
                        base, quote = inner, "INR"
            out.append(PairInfo(
                pair=pair,
                base=base,
                quote=quote,
                status=str(item.get("status", "active")),
                is_inr_quote=(quote.upper() == "INR"),
                base_min_size=_fnum(item.get("target_currency_ask_price_min",
                                              item.get("min_size"))),
                base_max_size=_fnum(item.get("target_currency_ask_price_max",
                                              item.get("max_size"))),
                quote_min_size=_fnum(item.get("base_currency_bid_price_min",
                                              item.get("quote_min_size"))),
                quote_precision=_int(item.get("base_currency_precision",
                                                item.get("precision"))),
                base_precision=_int(item.get("target_currency_precision")),
                extra={k: v for k, v in item.items()
                       if k not in {"pair", "status"}},
            ))
        return out

    @staticmethod
    def _parse_candles(payload: Any, pair: str, interval: str) -> List[Candle]:
        """Parse the candles payload. CoinDCX returns either:
            [{"open":..., "high":..., "low":..., "close":..., "volume":...,
              "time": <ms>}, ...]  (v3 layout)
        or  a list of arrays [open, high, low, close, volume, time].
        """
        out: List[Candle] = []
        if not isinstance(payload, list):
            return out
        now_ms = int(time.time() * 1000)
        interval_ms = INTERVAL_MS.get(interval, 0)
        for item in payload:
            try:
                if isinstance(item, dict):
                    o = float(item["open"]); h = float(item["high"])
                    l = float(item["low"]);  c = float(item["close"])
                    v = float(item.get("volume", 0) or 0)
                    t_ms = int(item.get("time", item.get("open_time",
                                item.get("timestamp", 0))) or 0)
                elif isinstance(item, (list, tuple)) and len(item) >= 6:
                    o = float(item[0]); h = float(item[1]); l = float(item[2])
                    c = float(item[3]); v = float(item[4]); t_ms = int(item[5])
                else:
                    continue
                if t_ms <= 0:
                    continue
                close_t = t_ms + interval_ms - 1
                complete = close_t < now_ms
                out.append(Candle(
                    open_time_ms=t_ms, open=o, high=h, low=l,
                    close=c, volume=v, close_time_ms=close_t,
                    complete=complete,
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return out


# ---------- small utilities
def _server_time(resp: Optional[requests.Response]) -> Optional[datetime]:
    if resp is None:
        return None
    raw = resp.headers.get("Date") or resp.headers.get("date")
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _fnum(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x: Any) -> Optional[int]:
    if x is None or x == "":
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        try:
            return int(float(x))
        except (TypeError, ValueError):
            return None
