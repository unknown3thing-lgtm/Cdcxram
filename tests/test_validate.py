"""Tests: OHLCV validation, missing/duplicate/incomplete candles, gaps."""

from __future__ import annotations

import pytest

from coindcx_lab.api.models import Candle
from coindcx_lab.api.validate import (
    OHLCVValidationError,
    dedupe_and_sort,
    drop_duplicate_timestamps,
    drop_incomplete_latest,
    find_gaps,
    validate_candles,
)


# ---------------------------------------------------------------------------
# Candle dataclass invariants
# ---------------------------------------------------------------------------
class TestCandleInvariants:
    def test_negative_price_rejected(self):
        with pytest.raises(ValueError):
            Candle(open_time_ms=1, open=-1, high=2, low=0.5, close=1.5,
                   volume=10, close_time_ms=2)

    def test_high_below_close_rejected(self):
        with pytest.raises(ValueError):
            Candle(open_time_ms=1, open=10, high=5, low=9, close=11,
                   volume=10, close_time_ms=2)

    def test_low_above_close_rejected(self):
        with pytest.raises(ValueError):
            Candle(open_time_ms=1, open=10, high=11, low=15, close=11,
                   volume=10, close_time_ms=2)

    def test_close_before_open_rejected(self):
        with pytest.raises(ValueError):
            Candle(open_time_ms=10, open=10, high=11, low=9, close=10.5,
                   volume=10, close_time_ms=5)

    def test_nan_price_rejected(self):
        with pytest.raises(ValueError):
            Candle(open_time_ms=1, open=float("nan"), high=2, low=0.5,
                   close=1.5, volume=10, close_time_ms=2)


# ---------------------------------------------------------------------------
# Duplicate timestamps
# ---------------------------------------------------------------------------
class TestDuplicateTimestamps:
    def test_drop_duplicates_keeps_first(self):
        c1 = Candle(open_time_ms=1, open=10, high=11, low=9, close=10.5, volume=1, close_time_ms=2)
        c2 = Candle(open_time_ms=1, open=11, high=12, low=10, close=11.5, volume=1, close_time_ms=2)
        c3 = Candle(open_time_ms=2, open=12, high=13, low=11, close=12.5, volume=1, close_time_ms=3)
        kept, dropped = drop_duplicate_timestamps([c1, c2, c3])
        assert len(kept) == 2
        assert dropped == 1
        assert kept[0].open == 10     # first wins

    def test_dedupe_and_sort_orders_by_open_time(self):
        c1 = Candle(open_time_ms=3, open=10, high=11, low=9, close=10.5, volume=1, close_time_ms=4)
        c2 = Candle(open_time_ms=1, open=11, high=12, low=10, close=11.5, volume=1, close_time_ms=2)
        c3 = Candle(open_time_ms=2, open=12, high=13, low=11, close=12.5, volume=1, close_time_ms=3)
        out = dedupe_and_sort([c1, c2, c3])
        assert [c.open_time_ms for c in out] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Incomplete latest candle
# ---------------------------------------------------------------------------
class TestIncompleteLatest:
    def test_drops_incomplete_latest(self):
        complete = Candle(open_time_ms=1, open=10, high=11, low=9, close=10.5,
                          volume=1, close_time_ms=2, complete=True)
        incomplete = Candle(open_time_ms=2, open=10, high=11, low=9, close=10.5,
                            volume=1, close_time_ms=3, complete=False)
        out = drop_incomplete_latest([complete, incomplete])
        assert out == [complete]

    def test_keeps_complete_when_last_is_complete(self):
        complete = Candle(open_time_ms=1, open=10, high=11, low=9, close=10.5,
                          volume=1, close_time_ms=2, complete=True)
        out = drop_incomplete_latest([complete])
        assert out == [complete]

    def test_empty_input(self):
        assert drop_incomplete_latest([]) == []


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------
class TestGaps:
    def test_no_gaps_in_contiguous_series(self):
        interval = 3_600_000
        cs = [
            Candle(open_time_ms=i*interval, open=1, high=1, low=1, close=1,
                   volume=1, close_time_ms=(i+1)*interval - 1)
            for i in range(10)
        ]
        assert find_gaps(cs, interval) == []

    def test_detects_a_gap(self):
        interval = 3_600_000
        cs = [
            Candle(open_time_ms=0,  open=1, high=1, low=1, close=1, volume=1, close_time_ms=interval-1),
            Candle(open_time_ms=interval, open=1, high=1, low=1, close=1, volume=1, close_time_ms=2*interval-1),
            # skip the 3rd bar
            Candle(open_time_ms=3*interval, open=1, high=1, low=1, close=1, volume=1, close_time_ms=4*interval-1),
        ]
        gaps = find_gaps(cs, interval)
        assert len(gaps) == 1
        assert gaps[0] == 2 * interval


# ---------------------------------------------------------------------------
# End-to-end validate
# ---------------------------------------------------------------------------
class TestValidateCandles:
    def test_clean_series_passes(self, clean_candles):
        out = validate_candles(clean_candles, interval_ms=3_600_000, min_count=2)
        assert len(out) == len(clean_candles)

    def test_too_few_candles_raises(self):
        with pytest.raises(OHLCVValidationError):
            validate_candles([], min_count=5)

    def test_none_input_raises(self):
        with pytest.raises(OHLCVValidationError):
            validate_candles(None)  # type: ignore[arg-type]

    def test_non_monotonic_input_is_sorted_not_raised(self):
        """dedupe_and_sort inside validate_candles sorts the input; an
        out-of-order input is therefore not an error — it's normalised.
        Overlapping bars, however, ARE an error."""
        interval = 3_600_000
        cs_out_of_order = [
            Candle(open_time_ms=interval, open=1, high=1, low=1, close=1, volume=1, close_time_ms=2*interval-1),
            Candle(open_time_ms=0, open=1, high=1, low=1, close=1, volume=1, close_time_ms=interval-1),
        ]
        # Should sort and pass.
        out = validate_candles(cs_out_of_order)
        assert [c.open_time_ms for c in out] == [0, interval]

    def test_overlapping_bars_raise(self):
        """Real overlap: bar i's close_time extends into bar i+1's open."""
        interval = 3_600_000
        cs_overlapping = [
            Candle(open_time_ms=0, open=1, high=1, low=1, close=1, volume=1,
                   close_time_ms=2*interval - 1),   # extends into next bar's open
            Candle(open_time_ms=interval, open=1, high=1, low=1, close=1, volume=1,
                   close_time_ms=2*interval - 1),
        ]
        with pytest.raises(OHLCVValidationError):
            validate_candles(cs_overlapping)

    def test_dedupes_on_validate(self):
        interval = 3_600_000
        c1 = Candle(open_time_ms=0, open=1, high=1, low=1, close=1, volume=1, close_time_ms=interval-1)
        c2 = Candle(open_time_ms=0, open=1, high=1, low=1, close=1, volume=1, close_time_ms=interval-1)
        c3 = Candle(open_time_ms=interval, open=1, high=1, low=1, close=1, volume=1, close_time_ms=2*interval-1)
        out = validate_candles([c1, c2, c3])
        assert len(out) == 2
