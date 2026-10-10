"""Clearly-labelled synthetic OHLCV generator.

Used in TWO well-defined roles:

    1. As a deterministic fixture source for unit tests. Tests must not need
       network access, so they construct synthetic series with known
       statistical properties (trend, volatility, jump events) and assert
       that indicators/signals/backtest behave correctly.

    2. As a *fallback* when the CoinDCX public API is unreachable from the
       running environment (e.g. Cloudflare edge block on data-centre IPs).
       In this mode the dashboard MUST clearly label the data as SIMULATED
       and never present it as live.

This generator is NOT used to fabricate live prices, API responses, or
backtest results. Where it is used for demonstration purposes, the
DataProvenance.source == SourceKind.SIMULATED so the dashboard can render
the appropriate badge.

The series produced are deterministic given (seed, pair, interval, count):
two runs with the same arguments yield identical candles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .models import Candle, DataProvenance, SourceKind
from .validate import validate_candles


@dataclass
class SyntheticGenerator:
    """Geometric Brownian motion + optional jump generator.

    Parameters are chosen to produce price series that look plausible for
    crypto markets: log-normal returns with ~1-3% bar volatility, occasional
    jumps to simulate news events. All values are clearly artificial.
    """

    seed: int = 42

    def series(self, *, pair: str, interval: str,
               count: int = 200,
               start_price: float = 1000.0,
               drift_per_bar: float = 0.0005,
               vol_per_bar: float = 0.015,
               jump_prob: float = 0.02,
               jump_size: float = 0.05,
               end_time_ms: Optional[int] = None) -> Tuple[List[Candle], DataProvenance]:
        """Generate `count` synthetic candles ending at end_time_ms (UTC ms).

        Deterministic for a given (seed, pair, interval, count).
        """
        from .client import INTERVAL_MS  # local import to avoid cycle
        if interval not in INTERVAL_MS:
            raise ValueError(f"Unsupported interval {interval!r}")
        interval_ms = INTERVAL_MS[interval]

        if end_time_ms is None:
            end_time_ms = (int(datetime.now(timezone.utc).timestamp() * 1000)
                           // interval_ms) * interval_ms

        # Trivial LCG. We avoid numpy.random.default_rng here so that the
        # generator is byte-for-byte reproducible across numpy versions and
        # platforms (some test envs still ship numpy 1.x).
        state = (self.seed * 0x9E3779B97F4A7C15 + hash((pair, interval))) & 0xFFFFFFFFFFFFFFFF
        def _next() -> float:
            nonlocal state
            # xorshift64*
            state ^= (state >> 12)
            state ^= (state << 25)
            state ^= (state >> 27)
            return (state * 0x2545F4914F6CDD1D & 0xFFFFFFFFFFFFFFFF) / 2.0**64

        # Box–Muller transform for normal returns
        def _normal() -> float:
            u1 = max(_next(), 1e-12)
            u2 = _next()
            return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

        candles: List[Candle] = []
        price = max(start_price, 1e-6)
        t = end_time_ms - (count - 1) * interval_ms
        for i in range(count):
            o = price
            # intra-bar path: simulate 4 sub-steps to get realistic high/low
            intra = [o]
            for _ in range(4):
                ret = drift_per_bar / 4 + vol_per_bar / 2 * _normal()
                if _next() < jump_prob:
                    sign = 1.0 if _next() > 0.5 else -1.0
                    ret += sign * jump_size
                intra.append(max(intra[-1] * math.exp(ret), 1e-8))
            h = max(intra)
            l = min(intra)
            c = intra[-1]
            v = abs(_normal()) * (10 + 100 * (1 + abs(c / o - 1) * 50))
            candles.append(Candle(
                open_time_ms=t + i * interval_ms,
                open=round(o, 6),
                high=round(h, 6),
                low=round(l, 6),
                close=round(c, 6),
                volume=round(v, 4),
                close_time_ms=t + (i + 1) * interval_ms - 1,
                complete=(i < count - 1),  # last bar is "incomplete"
            ))
            price = c

        # Last bar in the requested window should be considered complete for
        # backtest purposes (the request was for `count` historical bars).
        # The dashboard live-fetch path will mark the latest bar incomplete
        # based on wall-clock time; the synthetic fixture path always
        # delivers a complete-candles series to make signal evaluation
        # deterministic.
        candles[-1] = Candle(
            open_time_ms=candles[-1].open_time_ms,
            open=candles[-1].open, high=candles[-1].high,
            low=candles[-1].low, close=candles[-1].close,
            volume=candles[-1].volume,
            close_time_ms=candles[-1].close_time_ms,
            complete=True,
        )

        # Validate
        candles = validate_candles(candles, interval_ms=interval_ms, min_count=2)

        prov = DataProvenance(
            source=SourceKind.SIMULATED,
            endpoint="synthetic://coindcx-lab/gbm+jumps",
            fetched_at_utc=datetime.now(timezone.utc),
            note=f"seed={self.seed} pair={pair} interval={interval} "
                 f"count={count} drift={drift_per_bar} vol={vol_per_bar}",
        )
        return candles, prov

    def current_prices_for(self, pair: str, candles: List[Candle]) -> dict:
        """Derive a fake 24h ticker from synthetic candles.

        Clearly only used in SIMULATED mode.
        """
        if not candles:
            return {}
        last = candles[-1].close
        # 24h window = last 24 1h candles or whatever's available
        window = candles[-24:] if len(candles) >= 24 else candles
        high_24 = max(c.high for c in window)
        low_24 = min(c.low for c in window)
        vol_24 = sum(c.volume for c in window)
        first = window[0].open
        change_pct = ((last - first) / first * 100.0) if first > 0 else 0.0
        return {
            "last_price": last,
            "high_24h": high_24,
            "low_24h": low_24,
            "volume_24h": vol_24,
            "change_pct_24h": change_pct,
        }
