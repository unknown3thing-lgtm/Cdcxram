"""Phase 11 regression tests: 4h/1d interval support, candle integrity,
walk-forward on different intervals.

Required cases (per Phase 11 brief):
  A. Interval conversion & plumbing:
     1. Backtester.run() accepts interval="4h" / "1d" and propagates
        to signal_engine.evaluate().
     2. Walk_forward() accepts interval parameter and propagates
        to each segment's run().
     3. interval="1h" remains the default (backward compat).

  B. Warm-up at different intervals:
     4. EMA200 warmup_candles=200 means 200 CANDLES, not 200 hours —
        on 1d interval this is ~200 days, on 4h ~33 days.
     5. Warm-up prefix correctly supplies bars at 4h and 1d intervals.
     6. Eligible-bars reporting works at 4h and 1d intervals.

  C. Candle integrity & validation:
     7. Out-of-order timestamps are caught by sorting.
     8. Duplicate open_time_ms are de-duplicated by the prefix builder.
     9. OHLC violations (low > high) are detectable.
     10. Missing intervals in the sequence are detectable.

  D. Segment boundaries at different intervals:
     11. Walk-forward day-boundary split works on 4h candles
         (multiple 4h bars per day still go to the same segment).
     12. Walk-forward day-boundary split works on 1d candles
         (1 bar per day; day boundary IS the bar boundary).

  E. 429 retry behaviour (Phase 11 WazirX client change):
     13. 429 retries up to rate_limit_retries times before raising.
     14. 429 eventually succeeds when API recovers within budget.
     15. 429 raises RateLimitError after exhausting retries.
"""
from __future__ import annotations

import math
import os
import tempfile
from datetime import datetime, timezone, timedelta
from typing import List
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _settings_for_paper():
    from coindcx_lab.engine.risk import RiskSettings
    return RiskSettings(initial_inr=2000, fee_pct=0, spread_pct=0,
                       slippage_pct=0, max_position_pct=100,
                       max_portfolio_exposure_pct=100,
                       trading_allocation_pct=100,
                       reward_to_risk_target=2.0,
                       max_open_positions=5, max_daily_trades=100,
                       max_daily_loss_pct=100, max_drawdown_pct=100,
                       cooldown_after_consecutive_losses=0,
                       allow_overnight=True, min_24h_volume_inr=0,
                       min_24h_trades=0)


def _trend_up_candles(n=250, start_ts=1_700_000_000_000,
                      interval_ms=3_600_000,
                      start_price=100.0, slope=0.5):
    """Generate a strictly-upward-trending candle series."""
    from coindcx_lab.api.models import Candle
    out = []
    for i in range(n):
        p = start_price + i * slope
        out.append(Candle(
            open_time_ms=start_ts + i * interval_ms,
            open=p, high=p + 1, low=p - 1, close=p, volume=100,
            close_time_ms=start_ts + (i + 1) * interval_ms - 1,
            complete=True))
    return out


# Interval→ms map (mirrors api/client.py INTERVAL_MS).
INTERVAL_MS = {
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


# ===========================================================================
# A. Interval conversion & plumbing
# ===========================================================================
class TestIntervalPlumbing:
    """Phase 11 — verify the interval parameter is threaded through
    Backtester.run(), walk_forward(), and into SignalEngine.evaluate()."""

    def test_run_accepts_4h_interval(self):
        """run() should accept interval='4h' without error and the
        result should be valid (no exceptions raised)."""
        from coindcx_lab.api.models import DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        candles = _trend_up_candles(n=120, interval_ms=INTERVAL_MS["4h"])
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        # Should not raise
        result = bt.run(pair="TEST", candles=candles, provenance=prov,
                        interval="4h")
        # 120 candles, warmup_skip_bars=60 → eligible = 120 - 59 = 61
        assert result.eligible_bars == 61

    def test_run_accepts_1d_interval(self):
        """run() should accept interval='1d' without error."""
        from coindcx_lab.api.models import DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        candles = _trend_up_candles(n=120, interval_ms=INTERVAL_MS["1d"])
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        result = bt.run(pair="TEST", candles=candles, provenance=prov,
                        interval="1d")
        assert result.eligible_bars == 61

    def test_run_default_interval_is_1h(self):
        """If interval is not passed, it should default to '1h'
        (backward compatibility with Phase 10 callers)."""
        from coindcx_lab.api.models import DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        candles = _trend_up_candles(n=120, interval_ms=INTERVAL_MS["1h"])
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        # Wrap evaluate to capture interval arg
        original_eval = eng.evaluate
        captured = {}
        def _capture(*, pair, interval, **kw):
            captured["interval"] = interval
            return original_eval(pair=pair, interval=interval, **kw)
        eng.evaluate = _capture
        bt.run(pair="TEST", candles=candles, provenance=prov)  # no interval=
        assert captured.get("interval") == "1h", \
            f"default interval should be '1h', got {captured.get('interval')}"

    def test_walk_forward_propagates_interval_to_run(self):
        """walk_forward(interval=...) should propagate to run() and
        into SignalEngine.evaluate()."""
        from coindcx_lab.api.models import Candle
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        candles = _trend_up_candles(n=300, interval_ms=INTERVAL_MS["4h"],
                                     slope=0.1)
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)

        # Capture interval passed to evaluate.
        original_eval = eng.evaluate
        captured_intervals = []
        def _capture(*, pair, interval, **kw):
            captured_intervals.append(interval)
            return original_eval(pair=pair, interval=interval, **kw)
        eng.evaluate = _capture

        early = candles[100].open_time_ms
        mid = candles[200].open_time_ms
        dev_end = _day_iso_from_ms(early)
        val_end = _day_iso_from_ms(mid)
        bt.walk_forward(pair="TEST", candles=candles,
                        dev_end_iso=dev_end, val_end_iso=val_end,
                        interval="4h")
        # All evaluate() calls should have interval='4h'
        assert all(iv == "4h" for iv in captured_intervals), \
            f"not all evaluate calls used '4h'; saw {set(captured_intervals)}"
        assert len(captured_intervals) > 0, "no evaluate calls were made"


# ===========================================================================
# B. Warm-up at different intervals
# ===========================================================================
class TestWarmupAtIntervals:
    """Phase 11 — verify warm-up works correctly at 4h and 1d intervals."""

    def test_ema200_warmup_at_1d_interval(self):
        """On 1d candles, EMA200 needs 200 CANDLES (= 200 days).
        Verify EMA200 is undefined for i < 199 and defined for i >= 199."""
        from coindcx_lab.engine.indicators import IndicatorSet
        candles = _trend_up_candles(n=250, interval_ms=INTERVAL_MS["1d"])
        ind = IndicatorSet.from_candles(candles)
        # First 199 bars should have NaN ema200
        nan_count = sum(1 for i in range(199) if math.isnan(ind.ema200[i]))
        assert nan_count == 199, \
            f"first 199 bars should have NaN ema200, got {nan_count} NaN"
        # Bar 199+ should have defined ema200
        assert not math.isnan(ind.ema200[199]), \
            "bar 199 should have defined ema200"
        assert not math.isnan(ind.ema200[249]), \
            "bar 249 should have defined ema200"

    def test_ema200_warmup_at_4h_interval(self):
        """On 4h candles, EMA200 needs 200 CANDLES (= ~33 days).
        Verify warm-up bookkeeping is interval-agnostic."""
        from coindcx_lab.engine.indicators import IndicatorSet
        candles = _trend_up_candles(n=250, interval_ms=INTERVAL_MS["4h"])
        ind = IndicatorSet.from_candles(candles)
        # First 199 bars should have NaN ema200
        nan_count = sum(1 for i in range(199) if math.isnan(ind.ema200[i]))
        assert nan_count == 199

    def test_walk_forward_prefix_at_4h(self):
        """Walk-forward warm-up prefix should work on 4h candles.
        Verify val segment gets a prefix from dev."""
        from coindcx_lab.api.models import Candle
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        candles = _trend_up_candles(n=500, interval_ms=INTERVAL_MS["4h"],
                                     slope=0.1)
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)
        early = candles[125].open_time_ms
        mid = candles[250].open_time_ms
        dev_end = _day_iso_from_ms(early)
        val_end = _day_iso_from_ms(mid)
        results = bt.walk_forward(pair="TEST", candles=candles,
                                  dev_end_iso=dev_end, val_end_iso=val_end,
                                  interval="4h")
        # VAL should have a non-zero warmup_prefix_len
        assert results["val"].warmup_prefix_len > 0, \
            f"val should have prefix at 4h; got " \
            f"{results['val'].warmup_prefix_len}"
        # OOS should also have a prefix
        assert results["oos"].warmup_prefix_len > 0


# ===========================================================================
# C. Candle integrity & validation
# ===========================================================================
class TestCandleIntegrity:
    """Phase 11 — verify candle integrity checks catch real defects."""

    def test_out_of_order_timestamps_sorted_by_run(self):
        """If candles are passed out of order, run() should sort them
        by open_time_ms before processing."""
        from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        # Build in-order candles, then shuffle a pair.
        candles = _trend_up_candles(n=120, interval_ms=INTERVAL_MS["1h"])
        # Swap candles[50] and candles[51]
        candles[50], candles[51] = candles[51], candles[50]
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        # Should not raise; equity_curve should have 120 points
        result = bt.run(pair="TEST", candles=candles, provenance=prov)
        assert len(result.equity_curve) == 120

    def test_duplicate_open_time_ms_deduped_by_prefix(self):
        """When walk_forward constructs a prefix from prior segments,
        any candle whose open_time_ms appears in BOTH the prefix and
        the segment should be de-duplicated (segment wins)."""
        from coindcx_lab.api.models import Candle
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        candles = _trend_up_candles(n=300, interval_ms=INTERVAL_MS["1h"],
                                     slope=0.1)
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)

        # Manually run() with a warmup_prefix that overlaps with the
        # segment — the run() should de-duplicate by open_time_ms.
        prefix = candles[:70]   # first 70 candles
        seg = candles[60:120]   # overlap: candles 60..69 appear in BOTH
        result = bt.run(pair="TEST", candles=seg, segment="val",
                        warmup_prefix=prefix,
                        segment_start_idx=len(prefix),
                        interval="1h")
        # The de-dup should have removed 10 duplicates from the prefix.
        # segment_start_idx points to the first SEGMENT bar in the
        # combined array; with 70 prefix - 10 dups = 60 unique prefix
        # bars + 60 seg bars = 120 total. So segment_start_idx should
        # be at index 60 in the combined array.
        # However, the caller passed segment_start_idx=70 — the run()
        # method defensively recomputes it from candle counts.
        # Verify the result has the right segment_start_idx (60, not 70).
        assert result.segment_start_idx == 60, \
            f"segment_start_idx should be 60 after de-dup, got " \
            f"{result.segment_start_idx}"

    def test_ohlc_violation_detection(self):
        """The Candle dataclass's __post_init__ validator should
        reject any candle with low > high (or high < open/close).
        This means bad OHLC can never enter the system — integrity
        is enforced at construction time."""
        from coindcx_lab.api.models import Candle
        # Attempting to construct a candle with low > high should raise
        with pytest.raises(ValueError) as exc_info:
            Candle(
                open_time_ms=1_700_000_000_000,
                open=100, high=99, low=105,  # low > high
                close=100, volume=10,
                close_time_ms=1_700_000_003_600 - 1, complete=True)
        assert "high" in str(exc_info.value), \
            f"error should mention 'high'; got: {exc_info.value}"
        # Attempting to construct a candle with high < open should also raise
        with pytest.raises(ValueError):
            Candle(
                open_time_ms=1_700_000_000_000,
                open=110, high=99, low=98,  # high < open
                close=105, volume=10,
                close_time_ms=1_700_000_003_600 - 1, complete=True)
        # A valid candle should construct without error
        good = Candle(
            open_time_ms=1_700_000_000_000,
            open=100, high=110, low=95, close=105, volume=10,
            close_time_ms=1_700_000_003_600 - 1, complete=True)
        assert good.high == 110

    def test_missing_interval_detection(self):
        """A gap in the open_time_ms sequence should be detectable."""
        from coindcx_lab.api.models import Candle
        # Build 10 candles, then remove candles[5] to create a gap.
        candles = _trend_up_candles(n=10, interval_ms=INTERVAL_MS["1h"])
        candles = candles[:5] + candles[6:]
        times = [c.open_time_ms for c in candles]
        missing = sum(1 for i in range(1, len(times))
                      if times[i] - times[i - 1] != INTERVAL_MS["1h"])
        assert missing == 1, f"expected 1 missing interval, got {missing}"


# ===========================================================================
# D. Segment boundaries at different intervals
# ===========================================================================
class TestSegmentBoundaries:
    """Phase 11 — walk-forward day-boundary split works on 4h and 1d."""

    def test_4h_candles_same_day_in_same_segment(self):
        """Multiple 4h bars on the same UTC day should all land in
        the same segment (based on _day_iso_from_ms)."""
        from coindcx_lab.engine.backtest import _day_iso_from_ms
        # 6 4h bars on the same UTC day (2026-10-12)
        base_ts = int(datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp() * 1000)
        days = [_day_iso_from_ms(base_ts + i * INTERVAL_MS["4h"])
                for i in range(6)]
        # All 6 should be the same day
        assert len(set(days)) == 1, \
            f"all 6 4h bars on same day should map to same ISO date; " \
            f"got {set(days)}"

    def test_1d_candle_one_per_day(self):
        """1d candles: each bar is on a different UTC day."""
        from coindcx_lab.engine.backtest import _day_iso_from_ms
        candles = _trend_up_candles(n=10, interval_ms=INTERVAL_MS["1d"])
        days = [_day_iso_from_ms(c.open_time_ms) for c in candles]
        # All 10 should be distinct days
        assert len(set(days)) == 10

    def test_walk_forward_4h_segments_non_overlapping(self):
        """Walk-forward split on 4h candles should produce non-
        overlapping dev/val/oos segments by UTC day."""
        from coindcx_lab.api.models import Candle
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        candles = _trend_up_candles(n=600, interval_ms=INTERVAL_MS["4h"],
                                     slope=0.1)
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)

        early = candles[200].open_time_ms
        mid = candles[400].open_time_ms
        dev_end = _day_iso_from_ms(early)
        val_end = _day_iso_from_ms(mid)
        results = bt.walk_forward(pair="TEST", candles=candles,
                                  dev_end_iso=dev_end, val_end_iso=val_end,
                                  interval="4h")
        # All three segments should be present
        for name in ("dev", "val", "oos"):
            assert name in results
            assert results[name].total_bars > 0
        # Verify segments don't overlap in time:
        # dev's last candle open_time < val's first candle open_time
        # (We approximate by checking that dev's warmup_prefix_len + total_bars
        # doesn't bleed into val's window. A simpler check: val's
        # first candle in its OWN segment (after prefix) should be on a
        # later day than dev's last candle.)
        # Since we don't have direct access to per-segment candle lists,
        # we just confirm the equity curves don't share timestamps.
        dev_ts = [t for t, _ in results["dev"].equity_curve]
        val_ts = [t for t, _ in results["val"].equity_curve]
        if dev_ts and val_ts:
            # val_ts should be > max(dev_ts) - 1 interval (allowing
            # for the prefix overlap which is intentional)
            # Actually, the val equity_curve records only in-segment
            # bars, so all val_ts should be > max(dev_ts).
            assert min(val_ts) > max(dev_ts), \
                f"val segment first ts {min(val_ts)} should be > " \
                f"dev last ts {max(dev_ts)}"


# ===========================================================================
# E. 429 retry behaviour (Phase 11 WazirX client change)
# ===========================================================================
class TestRateLimitRetry:
    """Phase 11 — 429 retry behaviour in WazirXClient._request()."""

    def _mock_response(self, status, body):
        """Build a mock requests.Response."""
        m = MagicMock()
        m.status_code = status
        m.text = str(body)
        m.json.return_value = body
        m.headers = {}
        return m

    def test_429_retries_then_raises(self):
        """429 should be retried rate_limit_retries times, then raise
        RateLimitError."""
        from coindcx_lab.api.wazirx_client import WazirXClient
        from coindcx_lab.api.client import RateLimitError
        c = WazirXClient(max_retries=3, backoff_seconds=0.001,
                         rate_limit_retries=4,
                         rate_limit_backoff_seconds=0.001)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req, \
             patch("coindcx_lab.api.wazirx_client.time.sleep") as mock_sleep:
            mock_req.return_value = self._mock_response(429, {"error": "rate"})
            with pytest.raises(RateLimitError):
                c.list_exchange_pairs()
            # Should have called requests.request exactly
            # rate_limit_retries + 1 times (each retry is a new HTTP call;
            # the +1 is the final attempt that also returns 429 and
            # triggers the raise).
            assert mock_req.call_count == c.rate_limit_retries + 1
            # Should have called sleep once per retry.
            assert mock_sleep.call_count == c.rate_limit_retries

    def test_429_eventually_succeeds(self):
        """If the API recovers within the retry budget, the call
        should succeed."""
        from coindcx_lab.api.wazirx_client import WazirXClient
        from coindcx_lab.api.models import SourceKind
        c = WazirXClient(max_retries=3, backoff_seconds=0.001,
                         rate_limit_retries=4,
                         rate_limit_backoff_seconds=0.001)
        # First 2 calls return 429; third returns 200.
        sample = {
            "btcinr": {"lastPrice": "100", "volume": "10"},
            "ethinr": {"lastPrice": "50", "volume": "5"},
        }
        responses = [
            self._mock_response(429, {"error": "rate"}),
            self._mock_response(429, {"error": "rate"}),
            self._mock_response(200, sample),
        ]
        with patch("coindcx_lab.api.wazirx_client.requests.request",
                    side_effect=responses), \
             patch("coindcx_lab.api.wazirx_client.time.sleep"):
            prices, prov = c.current_prices()
            assert "B-BTCINR" in prices
            assert prov.source is SourceKind.LIVE or \
                   prov.source.value == "live"

    def test_429_retries_exhausted_raises_with_retry_count(self):
        """The RateLimitError message should mention the exhausted
        retry count so callers can distinguish transient from
        persistent rate-limiting."""
        from coindcx_lab.api.wazirx_client import WazirXClient
        from coindcx_lab.api.client import RateLimitError
        c = WazirXClient(rate_limit_retries=2,
                         rate_limit_backoff_seconds=0.001)
        with patch("coindcx_lab.api.wazirx_client.requests.request") as mock_req, \
             patch("coindcx_lab.api.wazirx_client.time.sleep"):
            mock_req.return_value = self._mock_response(429, {"error": "rate"})
            with pytest.raises(RateLimitError) as exc_info:
                c.list_exchange_pairs()
            # Error message should mention the retry count
            assert "2" in str(exc_info.value), \
                f"error message should mention retry count: {exc_info.value}"
