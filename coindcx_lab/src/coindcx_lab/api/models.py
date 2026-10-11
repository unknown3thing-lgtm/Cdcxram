"""Lightweight data models for the API layer.

These are intentionally simple frozen dataclasses so they are easy to
serialise, test, and inspect in the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class SourceKind(str, Enum):
    """Where a particular piece of market data came from.

    The dashboard uses this to render honest source labels.
    """

    LIVE = "live"               # fetched from CoinDCX public API successfully
    CACHED = "cached"            # served from local cache within TTL
    STALE = "stale"              # live fetch failed, served expired cache
    SIMULATED = "simulated"      # deterministic synthetic series (no live prices)
    UNAVAILABLE = "unavailable"  # no data available at all


@dataclass(frozen=True)
class DataProvenance:
    """Provenance metadata attached to every fetched batch."""

    source: SourceKind
    endpoint: str
    fetched_at_utc: datetime
    server_time_utc: Optional[datetime] = None
    cache_ttl_seconds: int = 0
    note: str = ""

    def label(self) -> str:
        """Human-readable short label suitable for dashboard badges."""
        if self.source is SourceKind.LIVE:
            return f"LIVE · {self.endpoint}"
        if self.source is SourceKind.CACHED:
            return f"CACHED · TTL {self.cache_ttl_seconds}s"
        if self.source is SourceKind.STALE:
            return "STALE · live fetch failed, expired cache served"
        if self.source is SourceKind.SIMULATED:
            return "SIMULATED · no live prices (deterministic fixture)"
        return "UNAVAILABLE · live fetch failed"


@dataclass(frozen=True)
class Candle:
    """A single OHLCV bar.

    All timestamps are Unix epoch milliseconds (UTC). `open_time_ms` is the
    candle's start; `close_time_ms` is the candle's end (exclusive of the next
    bar). `complete` is False for the still-forming current candle.
    """

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int
    complete: bool = True

    def __post_init__(self) -> None:
        # Sanity checks: prices & volume must be non-negative, and the
        # OHLC relationships must be internally consistent. These raise
        # immediately so callers cannot accidentally persist bad data.
        for name, v in (("open", self.open), ("high", self.high),
                        ("low", self.low), ("close", self.close),
                        ("volume", self.volume)):
            if not isinstance(v, (int, float)):
                raise ValueError(f"Candle.{name} must be numeric, got {type(v)}")
            if v != v:  # NaN check
                raise ValueError(f"Candle.{name} is NaN")
            if v < 0:
                raise ValueError(f"Candle.{name} must be >= 0, got {v}")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError(
                f"high {self.high} must be >= all of O/C/L "
                f"(o={self.open}, c={self.close}, l={self.low})"
            )
        if self.low > min(self.open, self.close, self.high):
            raise ValueError(
                f"low {self.low} must be <= all of O/C/H "
                f"(o={self.open}, c={self.close}, h={self.high})"
            )
        if self.close_time_ms < self.open_time_ms:
            raise ValueError(
                f"close_time_ms {self.close_time_ms} < open_time_ms {self.open_time_ms}"
            )

    def to_dict(self) -> dict:
        return {
            "open_time_ms": self.open_time_ms,
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "volume": self.volume,
            "close_time_ms": self.close_time_ms,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class PairInfo:
    """Static info about a tradable pair on CoinDCX spot."""

    pair: str              # e.g. "B-BTCINR"  (CoinDCX's pair code for BTC/INR spot)
    base: str              # e.g. "BTC"
    quote: str             # e.g. "INR"
    status: str            # e.g. "active"
    is_inr_quote: bool
    base_min_size: Optional[float] = None
    base_max_size: Optional[float] = None
    quote_min_size: Optional[float] = None
    quote_precision: Optional[int] = None
    base_precision: Optional[int] = None
    extra: dict = field(default_factory=dict)

    @property
    def display_symbol(self) -> str:
        return f"{self.base}/{self.quote}"
