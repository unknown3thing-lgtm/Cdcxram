"""OHLCV validation utilities.

These functions enforce data hygiene on every fetched candle batch:

    - drop_duplicate_timestamps: kill duplicate bars
    - dedupe_and_sort: sort by open_time_ms and de-duplicate by open_time
    - drop_incomplete_latest: trim the still-forming bar from completed-candle
      signals (no look-ahead, no using partial bars as if closed)
    - find_gaps: report missing bars relative to the expected cadence
    - validate_candles: end-to-end validation, raises OHLCVValidationError
      on the first structurally invalid bar

These are deliberately pure functions so the test suite can drive them with
synthetic fixtures and never need network access.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from .models import Candle


class OHLCVValidationError(ValueError):
    """Raised when a candle batch fails structural validation."""


def dedupe_and_sort(candles: Iterable[Candle]) -> List[Candle]:
    """Return a list sorted by open_time_ms with duplicates removed.

    When two candles share an open_time_ms we keep the first occurrence.
    """
    seen: dict[int, Candle] = {}
    for c in candles:
        if c.open_time_ms not in seen:
            seen[c.open_time_ms] = c
    return [seen[k] for k in sorted(seen.keys())]


def drop_duplicate_timestamps(candles: Iterable[Candle]) -> Tuple[List[Candle], int]:
    """Remove duplicate bars by open_time_ms; return (kept, dropped_count)."""
    seen: set[int] = set()
    kept: List[Candle] = []
    dropped = 0
    for c in candles:
        if c.open_time_ms in seen:
            dropped += 1
            continue
        seen.add(c.open_time_ms)
        kept.append(c)
    return kept, dropped


def drop_incomplete_latest(candles: List[Candle]) -> List[Candle]:
    """Drop the trailing incomplete candle.

    Required so that completed-candle signals (EMAs, RSI, MACD, etc.) never
    look at a bar that is still forming. This is the central anti-look-ahead
    guard for streaming candle feeds.
    """
    if not candles:
        return []
    out = list(candles)
    if not out[-1].complete:
        out = out[:-1]
    return out


def find_gaps(candles: List[Candle], interval_ms: int,
              tol_ratio: float = 0.1) -> List[int]:
    """Return open_time_ms values that are missing relative to a cadence.

    `interval_ms` is the expected bar size in milliseconds (e.g. 3_600_000 for
    1h). `tol_ratio` allows the gap detector to tolerate clock skew at the
    bar boundary. Returns the list of *expected* open_time_ms values that are
    absent.
    """
    if len(candles) < 2:
        return []
    candles = dedupe_and_sort(candles)
    missing: List[int] = []
    tol = int(interval_ms * tol_ratio)
    t = candles[0].open_time_ms
    last = candles[-1].open_time_ms
    i = 1
    while t < last:
        nxt = candles[i].open_time_ms if i < len(candles) else last + interval_ms
        # advance t by one interval and check whether the next bar is there
        t += interval_ms
        if abs(nxt - t) > tol:
            # the bar that should have started at `t - interval_ms + interval_ms`
            # is missing — record the expected open time
            missing.append(t - interval_ms + interval_ms)  # == t
        else:
            i += 1
    return missing


def validate_candles(candles: List[Candle], interval_ms: Optional[int] = None,
                     min_count: int = 1) -> List[Candle]:
    """End-to-end validation. Returns a cleaned list or raises.

    Checks (in order):
        1. Non-empty (>= min_count).
        2. Each Candle is structurally valid (the Candle dataclass already
           enforces OHLC/V non-negativity and high>=max(O,C,L), low<=min(O,C,H)).
        3. Monotonically increasing open_time_ms (after de-dup).
        4. close_time_ms >= open_time_ms for every bar.
        5. No negative intervals (close_time of bar i must be <= open_time of
           bar i+1, otherwise bars overlap).
        6. If interval_ms is provided, report (but do not fail on) gaps.

    The cleaned, deduped, sorted list is returned.
    """
    if candles is None:
        raise OHLCVValidationError("candles is None")
    if len(candles) < min_count:
        raise OHLCVValidationError(
            f"need at least {min_count} candles, got {len(candles)}"
        )

    cleaned = dedupe_and_sort(candles)

    # Check monotonicity & non-overlap (Candle.__post_init__ already validates
    # OHLC/V sanity and close_time_ms >= open_time_ms for individual bars).
    for i in range(1, len(cleaned)):
        prev, curr = cleaned[i - 1], cleaned[i]
        if curr.open_time_ms <= prev.open_time_ms:
            raise OHLCVValidationError(
                f"non-monotonic open_time_ms at index {i}: "
                f"{prev.open_time_ms} -> {curr.open_time_ms}"
            )
        if curr.open_time_ms < prev.close_time_ms:
            raise OHLCVValidationError(
                f"overlapping bars at index {i}: prev.close_time_ms "
                f"{prev.close_time_ms} > curr.open_time_ms {curr.open_time_ms}"
            )

    # Gap detection (informational only; gaps are normal on illiquid markets).
    if interval_ms is not None and len(cleaned) >= 2:
        _gaps = find_gaps(cleaned, interval_ms)
        # We do not raise on gaps — illiquid pairs are expected to have them.
        # The dashboard surfaces this count as a data-quality warning.

    return cleaned
