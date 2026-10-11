"""WazirX public market-data client.

WazirX is India's largest crypto exchange by INR volume. Their public
market-data endpoints are unauthenticated and support INR spot pairs
natively — which CoinDCX's public endpoints no longer do (they return
HTTP 404 "Link not found" from Vercel as of 2026-10-11).

This client mirrors the CoinDCXClient interface so the
MultiProviderClient can use either interchangeably. The analysis
engine (indicators, signals, scanner, backtest, paper trading) is
unchanged.

Verified endpoints (2026-10-11 from this host + from Vercel):
    GET https://api.wazirx.com/api/v2/tickers
        Returns 480 pairs as a dict: {"btcinr": {...}, "ethinr": {...}, ...}
        Each ticker has: base_unit, quote_unit, low, high, last, type, open,
                         volume, sell, buy, at (unix seconds), name
        200 OK, real live data.

    GET https://api.wazirx.com/sapi/v1/klines?symbol=btcinr&interval=1h&limit=3
        Returns an array of candles: [[open_time_sec, open, high, low, close, volume], ...]
        Note: open_time is in SECONDS (not milliseconds like CoinDCX/Binance).
        200 OK, real live data.

    GET https://api.wazirx.com/api/v1/status
        Returns 403 from this host — not used.

Pair format conversion (WazirX ↔ CoinDCX):
    WazirX:   "btcinr"  (lowercase, no separator)
    CoinDCX:  "B-BTCINR" (uppercase, "B-" prefix)
    We convert to CoinDCX format on output so the rest of the engine
    (which expects CoinDCX pair codes) doesn't need to change.

Interval mapping (our internal → WazirX):
    All our intervals (1m, 5m, 15m, 30m, 1h, 4h, 1d) are supported
    verbatim by WazirX.

Never authenticates, never sends real orders, never bypasses any
access control. Only documented public market-data endpoints.
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

from .client import (  # reuse error types + cache entry from coindcx client
    CoinDCXAPIError, RateLimitError, _CacheEntry, DEFAULT_HEADERS,
)
from .models import Candle, DataProvenance, PairInfo, SourceKind
from .validate import validate_candles

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# WazirX public REST endpoints (verified 2026-10-11)
# ---------------------------------------------------------------------------
WAZIRX_BASE = "https://api.wazirx.com"
EP_ALL_TICKERS = "/api/v2/tickers"           # GET — all 480 pairs
EP_KLINES       = "/sapi/v1/klines"           # GET — historical OHLCV

# Map our interval strings to WazirX interval codes (verbatim — WazirX
# uses the same codes for 1m, 5m, 15m, 30m, 1h, 4h, 1d).
WAZIRX_INTERVAL_MAP: Dict[str, str] = {
    "1m":  "1m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
    "2h":  "2h",
    "4h":  "4h",
    "6h":  "6h",
    "12h": "12h",
    "1d":  "1d",
    "3d":  "3d",
    "1w":  "1w",
}

# WazirX candle interval in milliseconds (open_time is in seconds).
# Reuse CoinDCX's INTERVAL_MS for the common intervals.
from .client import INTERVAL_MS  # noqa: E402


# ---------------------------------------------------------------------------
# Pair-code conversion helpers (WazirX ↔ CoinDCX format)
# ---------------------------------------------------------------------------
def wazirx_pair_to_coindcx(wazirx_pair: str) -> Optional[str]:
    """Convert a WazirX pair code (e.g. "btcinr") to CoinDCX format ("B-BTCINR").

    Returns None if the input is empty/invalid.
    """
    if not wazirx_pair or not isinstance(wazirx_pair, str):
        return None
    return "B-" + wazirx_pair.upper()


def coindcx_pair_to_wazirx(coindcx_pair: str) -> Optional[str]:
    """Convert a CoinDCX pair code (e.g. "B-BTCINR") to WazirX format ("btcinr").

    Returns None if the input doesn't start with "B-".
    """
    if not coindcx_pair or not isinstance(coindcx_pair, str):
        return None
    if not coindcx_pair.startswith("B-"):
        return None
    return coindcx_pair[2:].lower()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
@dataclass
class WazirXClient:
    """Thin, retrying, caching REST client for WazirX public market data.

    Mirrors the CoinDCXClient interface so MultiProviderClient can use
    either backend without code changes to the analysis engine.

    All methods return a tuple of (data, DataProvenance). The provenance
    always says source=LIVE with endpoint=wazirx-public when the request
    succeeds, so the dashboard can distinguish it from CoinDCX live data.
    """

    base_url: str = WAZIRX_BASE
    timeout: float = 10.0
    max_retries: int = 3
    backoff_seconds: float = 0.6
    cache_ttl_seconds: float = 5.0
    user_agent: str = "coindcx-lab/0.1 (+research; wazirx-fallback)"

    _cache: Dict[str, _CacheEntry] = field(default_factory=dict)
    _cache_lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ HTTP
    def _headers(self) -> Dict[str, str]:
        h = dict(DEFAULT_HEADERS)
        h["User-Agent"] = self.user_agent
        return h

    def _request(self, method: str, path: str, *,
                params: Optional[dict] = None) -> Tuple[Any, requests.Response]:
        url = self.base_url + path
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.request(
                    method, url,
                    params=params,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                if resp.status_code == 429:
                    raise RateLimitError(f"429 Too Many Requests for {url}")
                if resp.status_code == 403:
                    # WazirX sometimes returns 403 (openresty) for specific
                    # endpoints (e.g. /api/v1/status). Non-retryable for
                    # that endpoint, but for /api/v2/tickers and /sapi/v1/
                    # klines it's the data-centre IP filter. Either way,
                    # surface the failure so MultiProviderClient can fall
                    # back to the next provider or SIMULATED.
                    raise CoinDCXAPIError(
                        f"403 Forbidden at {url} — WazirX edge block "
                        "or endpoint not exposed publicly.")
                if resp.status_code >= 500:
                    last_exc = CoinDCXAPIError(
                        f"HTTP {resp.status_code} server error at {url}")
                    log.warning("attempt %d/%d got 5xx at %s",
                                attempt, self.max_retries, url)
                    if attempt < self.max_retries:
                        time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                        continue
                    raise last_exc
                if resp.status_code >= 400:
                    raise CoinDCXAPIError(
                        f"HTTP {resp.status_code} client error at {url}: "
                        f"{resp.text[:200]}")
                try:
                    return resp.json(), resp
                except json.JSONDecodeError as e:
                    raise CoinDCXAPIError(
                        f"Malformed JSON from {url}: {e}; "
                        f"body[:200]={resp.text[:200]}")
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                log.warning("attempt %d/%d failed at %s: %s",
                            attempt, self.max_retries, url, e)
                if attempt < self.max_retries:
                    time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                    continue
                raise CoinDCXAPIError(
                    f"Exhausted {self.max_retries} retries for {url}: {last_exc}")
            except CoinDCXAPIError as e:
                # Non-retryable errors (403, 4xx) — propagate immediately.
                raise

        raise CoinDCXAPIError(
            f"Exhausted {self.max_retries} retries for {url}: {last_exc}")

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

    def _cache_put(self, key: str, payload: Any, ttl: Optional[float] = None) -> None:
        with self._cache_lock:
            self._cache[key] = _CacheEntry(
                payload=payload, cached_at=time.time(),
                ttl=ttl if ttl is not None else self.cache_ttl_seconds,
            )

    # ----------------------------------------------------------- public API
    def list_exchange_pairs(self) -> Tuple[List[PairInfo], DataProvenance]:
        """Discover tradable INR spot pairs on WazirX.

        WazirX doesn't have a dedicated "list pairs" endpoint, so we
        derive the pair list from the all-tickers response: every key
        in the response that ends with "inr" is an INR-quoted spot pair.
        """
        tickers, prov = self._all_tickers()
        pairs: List[PairInfo] = []
        for wazirx_pair, ticker in tickers.items():
            # Only INR-quoted spot pairs
            if not wazirx_pair.endswith("inr"):
                continue
            if not isinstance(ticker, dict):
                continue
            if str(ticker.get("type", "")).upper() != "SPOT":
                continue
            cdx_pair = wazirx_pair_to_coindcx(wazirx_pair) or ""
            if not cdx_pair:
                continue
            base = str(ticker.get("base_unit", "")).upper()
            quote = str(ticker.get("quote_unit", "")).upper() or "INR"
            pairs.append(PairInfo(
                pair=cdx_pair, base=base, quote=quote,
                status="active", is_inr_quote=(quote == "INR"),
            ))
        return pairs, prov

    def list_inr_spot_pairs(self) -> Tuple[List[PairInfo], DataProvenance]:
        """Convenience wrapper — same as list_exchange_pairs (already INR-only)."""
        return self.list_exchange_pairs()

    def current_prices(self) -> Tuple[Dict[str, dict], DataProvenance]:
        """GET /api/v2/tickers → map of CoinDCX pair code → ticker dict.

        The ticker dict has the same shape CoinDCXClient produces:
            {"last_price": float, "high_24h": float, "low_24h": float,
             "volume_24h": float,        # INR turnover (last * base_volume)
             "volume_24h_base": float,  # base-asset volume (BTC, ETH, etc.)
             "change_pct_24h": float}

        IMPORTANT: WazirX's raw `volume` field is the BASE-ASSET volume
        (e.g. 0.07033 BTC) — NOT INR turnover. The scanner's
        `min_24h_volume_inr` filter expects INR turnover, so we
        compute `volume_24h = last * base_volume` here.
        """
        cache_key = "wazirx_current_prices"
        cached = self._cache_get(cache_key)
        if cached is not None:
            prov = DataProvenance(
                source=SourceKind.CACHED,
                endpoint=WAZIRX_BASE + EP_ALL_TICKERS,
                fetched_at_utc=datetime.now(timezone.utc),
                cache_ttl_seconds=int(self.cache_ttl_seconds),
            )
            return cached, prov

        tickers, prov = self._all_tickers()
        cleaned: Dict[str, dict] = {}
        for wazirx_pair, ticker in tickers.items():
            if not wazirx_pair.endswith("inr"):
                continue
            if not isinstance(ticker, dict):
                continue
            cdx_pair = wazirx_pair_to_coindcx(wazirx_pair)
            if not cdx_pair:
                continue
            try:
                last = float(ticker.get("last", "nan"))
                open_ = float(ticker.get("open", "nan"))
                high = float(ticker.get("high", "nan"))
                low = float(ticker.get("low", "nan"))
                # WazirX `volume` is in BASE asset (e.g. BTC). Convert to
                # INR turnover for the scanner's min_24h_volume_inr filter.
                base_volume = float(ticker.get("volume", "nan"))
                volume_inr = last * base_volume if last and base_volume else 0.0
                change_pct = ((last - open_) / open_ * 100.0) if open_ > 0 else 0.0
                cleaned[cdx_pair] = {
                    "last_price": last,
                    "high_24h": high,
                    "low_24h": low,
                    "volume_24h": volume_inr,         # INR turnover
                    "volume_24h_base": base_volume,    # base-asset volume (BTC)
                    "change_pct_24h": change_pct,
                }
            except (TypeError, ValueError):
                continue
        self._cache_put(cache_key, cleaned)
        return cleaned, prov

    def candles(self, pair: str, interval: str,
                limit: int = 200,
                end_time_ms: Optional[int] = None) -> Tuple[List[Candle], DataProvenance]:
        """Fetch historical OHLCV candles for a pair.

        GET /sapi/v1/klines?symbol=<wazirx_pair>&interval=<interval>&limit=<n>

        Returns a list of validated Candle objects (with open_time_ms in
        MILLISECONDS, matching CoinDCX/Binance convention).
        """
        if interval not in WAZIRX_INTERVAL_MAP:
            raise ValueError(f"Unsupported interval {interval!r}; "
                             f"supported: {list(WAZIRX_INTERVAL_MAP)}")
        wazirx_pair = coindcx_pair_to_wazirx(pair)
        if not wazirx_pair:
            raise ValueError(f"pair {pair!r} is not a valid CoinDCX pair code "
                             "(expected format like 'B-BTCINR')")

        params = {
            "symbol": wazirx_pair,
            "interval": WAZIRX_INTERVAL_MAP[interval],
            "limit": int(limit),
        }
        if end_time_ms is not None:
            # WazirX expects endTime in MILLISECONDS (verified by sending
            # both seconds and ms — only ms is accepted).
            params["endTime"] = int(end_time_ms)

        payload, resp = self._request("GET", EP_KLINES, params=params)
        candles = self._parse_klines(payload, pair, interval)
        candles = validate_candles(
            candles,
            interval_ms=INTERVAL_MS.get(interval),
            min_count=1,
        )
        prov = DataProvenance(
            source=SourceKind.LIVE,
            endpoint=WAZIRX_BASE + EP_KLINES,
            fetched_at_utc=datetime.now(timezone.utc),
            server_time_utc=_server_time(resp),
            note=f"wazirx pair={wazirx_pair} interval={interval} limit={limit}",
        )
        return candles, prov

    # ----------------------------------------------------------- parse helpers
    def _all_tickers(self) -> Tuple[dict, DataProvenance]:
        """Fetch and cache the all-tickers response."""
        cache_key = "wazirx_all_tickers"
        cached = self._cache_get(cache_key)
        if cached is not None:
            prov = DataProvenance(
                source=SourceKind.CACHED,
                endpoint=WAZIRX_BASE + EP_ALL_TICKERS,
                fetched_at_utc=datetime.now(timezone.utc),
                cache_ttl_seconds=int(self.cache_ttl_seconds),
            )
            return cached, prov

        payload, resp = self._request("GET", EP_ALL_TICKERS)
        if not isinstance(payload, dict):
            raise CoinDCXAPIError(
                f"WazirX /api/v2/tickers returned {type(payload).__name__}, "
                "expected dict")
        self._cache_put(cache_key, payload)
        prov = DataProvenance(
            source=SourceKind.LIVE,
            endpoint=WAZIRX_BASE + EP_ALL_TICKERS,
            fetched_at_utc=datetime.now(timezone.utc),
            server_time_utc=_server_time(resp),
            note="wazirx all-tickers (INR spot pairs)",
        )
        return payload, prov

    @staticmethod
    def _parse_klines(payload: Any, pair: str, interval: str) -> List[Candle]:
        """Parse WazirX klines payload.

        WazirX returns an array of arrays:
            [[open_time_seconds, open, high, low, close, volume], ...]

        Convert to our Candle dataclass (open_time_ms in milliseconds).
        """
        out: List[Candle] = []
        if not isinstance(payload, list):
            return out
        interval_ms = INTERVAL_MS.get(interval, 0)
        now_ms = int(time.time() * 1000)
        for item in payload:
            if not isinstance(item, (list, tuple)) or len(item) < 6:
                continue
            try:
                # WazirX returns open_time in SECONDS — convert to ms.
                t_sec = int(item[0])
                o = float(item[1]); h = float(item[2]); l = float(item[3])
                c = float(item[4]); v = float(item[5])
                t_ms = t_sec * 1000
                if t_ms <= 0:
                    continue
                close_t = t_ms + interval_ms - 1
                complete = close_t < now_ms
                out.append(Candle(
                    open_time_ms=t_ms, open=o, high=h, low=l,
                    close=c, volume=v,
                    close_time_ms=close_t, complete=complete,
                ))
            except (TypeError, ValueError, IndexError):
                continue
        return out


# ---------- server-time helper (mirrors CoinDCX client)
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
