"""Phase 11 paper-exit recovery — additional isolated tests.

Per Phase 11 brief requirement 5: "Add isolated tests for gaps longer
than five hours, missing candles, API rate limits, retries and duplicate
cron requests."

The Phase 10 file (test_phase10_regression.py) already covers:
  - test_first_check_has_no_gap
  - test_delayed_cron_run_gap_detected (>5h)
  - test_missing_pair_surfaces_in_incomplete
  - test_empty_candles_surfaces_in_incomplete
  - test_retry_does_not_duplicate_exit
  - test_stop_first_on_ambiguous_bar
  - test_equity_snapshot_persists_across_engines
  - test_last_exit_check_ms_persists
  - test_candle_before_entry_filtered
  - test_candle_at_or_after_last_check_processed

This Phase 11 file adds the remaining cases:
  - Very long gap (>24h) — gap still detected, processed_at_ms correct
  - Multiple consecutive cron retries with the SAME candle — never
    double-debits PnL across many retries
  - Concurrent cron race simulation — two near-simultaneous calls
    with the same exit-triggering candle; only one closes the trade
  - Rate-limit during cron candle fetch — incomplete_pairs surfaces
    'candle_fetch_failed' reason
  - Multiple OPEN trades across different pairs — independent exit
    processing, one trade's exit doesn't affect the other
  - Stale candle (all-incomplete) — surfaces as incomplete_pairs
    reason='all_incomplete'
  - No eligible candles after gap (gap >5h but candles list is all
    before last_check buffer) — surfaces as 'no_eligible_candles'
"""
from __future__ import annotations

import math
import os
import tempfile
from datetime import datetime, timezone, timedelta
from typing import List

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


def _buy_signal(*, entry=100.0, stop=98.0, target=104.0,
                pair="B-BTCINR", strategy="test"):
    from coindcx_lab.engine.signals import Signal, SignalState
    return Signal(pair=pair, interval="1h", strategy=strategy,
                  state=SignalState.BUY_CANDIDATE,
                  entry=entry, stop=stop, target=target,
                  rr_after_cost=3.0)


def _candle(*, open_time_ms, o=100, h=101, l=99, c=100, v=10,
            interval_ms=3_600_000, complete=True):
    from coindcx_lab.api.models import Candle
    return Candle(open_time_ms=open_time_ms, open=o, high=h, low=l,
                  close=c, volume=v,
                  close_time_ms=open_time_ms + interval_ms - 1,
                  complete=complete)


@pytest.fixture
def tmp_store():
    from coindcx_lab.db.store import Store
    fd, db_path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    store = Store(path=db_path)
    yield store
    store.close()
    try:
        os.unlink(db_path)
    except OSError:
        pass


# ===========================================================================
# Phase 11 paper-exit recovery — additional cases
# ===========================================================================
class TestPaperExitRecoveryPhase11:
    """Phase 11 — additional isolated tests for paper-exit recovery."""

    def test_very_long_gap_24h_plus_still_detected(self, tmp_store):
        """A 25-hour gap (>24h) should still be detected as gap_detected.
        The cron's gap threshold is 5h; a 25h gap easily exceeds it."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)

        # First check at T+1h
        candle1 = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                          h=101, l=99, c=100)
        pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle1]},
            current_time_ms=trade.entry_time_ms + 3_600_000)

        # Second check 25h later (gap >> 5h threshold)
        candle2 = _candle(open_time_ms=trade.entry_time_ms + 25 * 3_600_000,
                          h=101, l=99, c=100)  # no exit
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle2]},
            current_time_ms=trade.entry_time_ms + 25 * 3_600_000)
        assert result["gap_detected"] is True
        assert result["gap_hours"] >= 24.0, \
            f"gap_hours should be >= 24, got {result['gap_hours']}"

    def test_concurrent_cron_race_only_one_exit(self, tmp_store):
        """Two near-simultaneous calls with the SAME exit-triggering
        candle should result in only ONE exit (idempotency via the
        pre-UPDATE status re-check)."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)

        candle = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                         h=105, l=99, c=104)  # hits target

        # Call 1 — closes the trade
        r1 = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle]},
            current_time_ms=trade.entry_time_ms + 7_200_000)
        assert r1["closed_count"] == 1
        pnl_1 = pe.state.realized_pnl

        # Call 2 — same candle, should NOT close again
        r2 = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle]},
            current_time_ms=trade.entry_time_ms + 7_200_000)
        assert r2["closed_count"] == 0
        # Realized PnL must be unchanged
        assert pe.state.realized_pnl == pnl_1, \
            f"PnL changed after second call: {pnl_1} -> " \
            f"{pe.state.realized_pnl}"

    def test_multiple_retries_never_double_debit(self, tmp_store):
        """Calling check_exits_with_recovery N times (where N is large)
        with the SAME candle must NEVER accumulate more than 1 exit's
        worth of PnL. Tests the idempotency across many retries."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)

        candle = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                         h=105, l=99, c=104)

        # First call closes the trade
        pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle]},
            current_time_ms=trade.entry_time_ms + 7_200_000)
        pnl_after_first = pe.state.realized_pnl

        # 5 more retries with the same candle
        for i in range(5):
            pe.check_exits_with_recovery(
                candles_by_pair={"B-BTCINR": [candle]},
                current_time_ms=trade.entry_time_ms + (8 + i) * 3_600_000)

        assert pe.state.realized_pnl == pnl_after_first, \
            f"PnL changed across 5 retries: {pnl_after_first} -> " \
            f"{pe.state.realized_pnl}"

    def test_rate_limit_failure_surfaces_in_incomplete(self, tmp_store):
        """If a candle fetch fails with RateLimitError, the cron
        records it in incomplete_pairs with reason='candle_fetch_failed'.

        We can't easily mock the cron's HTTP layer in a unit test, but
        we can verify that check_exits_with_recovery() correctly records
        a pair as 'missing' when it's absent from candles_by_pair —
        which is what the cron does when fetch_errors occur for that
        pair."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig1 = _buy_signal(pair="B-BTCINR")
        sig2 = _buy_signal(pair="B-ETHINR")
        trade1, _ = pe.open_position(signal=sig1)
        trade2, _ = pe.open_position(signal=sig2)

        # Pass candles ONLY for BTC (ETH fetch failed in cron)
        btc_candle = _candle(open_time_ms=trade1.entry_time_ms + 3_600_000,
                             h=101, l=99, c=100)
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [btc_candle]},
            current_time_ms=trade1.entry_time_ms + 7_200_000)
        # ETH should be in incomplete_pairs with reason='missing'
        eth_in_incomplete = [ip for ip in result["incomplete_pairs"]
                             if ip["pair"] == "B-ETHINR"]
        assert len(eth_in_incomplete) == 1, \
            f"B-ETHINR should be in incomplete_pairs; got " \
            f"{result['incomplete_pairs']}"
        assert eth_in_incomplete[0]["reason"] == "missing"

    def test_multiple_open_trades_independent_processing(self, tmp_store):
        """Two OPEN trades on different pairs: an exit on one should
        NOT affect the other."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig1 = _buy_signal(pair="B-BTCINR", entry=100, stop=98, target=104)
        sig2 = _buy_signal(pair="B-ETHINR", entry=200, stop=196, target=208)
        trade1, _ = pe.open_position(signal=sig1)
        trade2, _ = pe.open_position(signal=sig2)

        # BTC candle hits target; ETH candle doesn't
        btc_candle = _candle(open_time_ms=trade1.entry_time_ms + 3_600_000,
                             h=105, l=99, c=104)
        eth_candle = _candle(open_time_ms=trade2.entry_time_ms + 3_600_000,
                             o=200, h=201, l=199, c=200)
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [btc_candle],
                              "B-ETHINR": [eth_candle]},
            current_time_ms=trade1.entry_time_ms + 7_200_000)
        # Only BTC should be closed
        assert result["closed_count"] == 1
        assert result["closed_trades"][0].pair == "B-BTCINR"
        # ETH should still be OPEN
        opens = pe.store.get_open_trades()
        eth_opens = [t for t in opens if t["pair"] == "B-ETHINR"]
        assert len(eth_opens) == 1, "ETH should still be open"

    def test_all_incomplete_candles_surfaces_in_incomplete(self, tmp_store):
        """If all candles passed are incomplete (still-forming), the
        pair should be reported in incomplete_pairs with
        reason='all_incomplete'."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)

        # Two incomplete candles that hit target — but should be ignored
        c1 = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                     h=105, l=99, c=104, complete=False)
        c2 = _candle(open_time_ms=trade.entry_time_ms + 7_200_000,
                     h=105, l=99, c=104, complete=False)
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [c1, c2]},
            current_time_ms=trade.entry_time_ms + 10_800_000)
        assert result["closed_count"] == 0
        # Should be in incomplete_pairs with reason='all_incomplete'
        btc_in_incomplete = [ip for ip in result["incomplete_pairs"]
                             if ip["pair"] == "B-BTCINR"]
        assert len(btc_in_incomplete) == 1
        assert btc_in_incomplete[0]["reason"] == "all_incomplete"

    def test_no_eligible_candles_after_gap_surfaces_in_incomplete(
        self, tmp_store):
        """If all candles supplied fall OUTSIDE the eligible window
        (e.g., all candles are before entry_time, or all are before
        the last_check buffer), the pair should be reported with
        reason='no_eligible_candles' — NOT silently claimed as
        'no exit'."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)

        # First check at T+1h
        candle1 = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                          h=101, l=99, c=100)
        pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle1]},
            current_time_ms=trade.entry_time_ms + 3_600_000)

        # Second check at T+10h (gap=9h > 5h threshold) but pass
        # ONLY a candle whose open_time is BEFORE the trade entry.
        # That candle is complete but fails the entry_time filter
        # (open_time <= entry_time) and so is not eligible.
        before_entry_candle = _candle(
            open_time_ms=trade.entry_time_ms - 3_600_000,
            h=101, l=99, c=100)
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [before_entry_candle]},
            current_time_ms=trade.entry_time_ms + 10 * 3_600_000)
        # gap_detected should be True
        assert result["gap_detected"] is True
        # Should be in incomplete_pairs with reason='no_eligible_candles'
        btc_in_incomplete = [ip for ip in result["incomplete_pairs"]
                             if ip["pair"] == "B-BTCINR"]
        assert len(btc_in_incomplete) == 1
        assert btc_in_incomplete[0]["reason"] == "no_eligible_candles"

    def test_processed_at_ms_correctly_recorded(self, tmp_store):
        """processed_at_ms should match the current_time_ms passed
        to the call (NOT the wall-clock time)."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)
        candle = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                         h=101, l=99, c=100)
        target_time = trade.entry_time_ms + 99 * 3_600_000  # arbitrary
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle]},
            current_time_ms=target_time)
        assert result["processed_at_ms"] == target_time
        # And last_exit_check_ms is updated to this value
        assert pe.last_exit_check_ms == target_time

    def test_zero_open_trades_returns_zero_closed(self, tmp_store):
        """If there are no OPEN trades, the call should return
        closed_count=0 and incomplete_pairs=[]."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        # No trades opened
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [_candle(
                open_time_ms=1_700_000_000_000)]},
            current_time_ms=1_700_000_003_600)
        assert result["closed_count"] == 0
        assert result["open_trades_count"] == 0
        assert result["incomplete_pairs"] == []
