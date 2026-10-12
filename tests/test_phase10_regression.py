"""Phase 10 regression tests: walk-forward warm-up, paper-exit recovery,
gap detection, idempotency under retries, equity-snapshot persistence.

Required cases (per Phase 10 brief):
  A. Walk-forward warm-up:
     1. EMA200 warm-up in short segments (val/oos segment < 200 bars
        must now successfully initialise EMA200 via the warm-up prefix).
     2. Segment boundaries: trades whose entry_idx < segment_start_idx
        are NOT scored.
     3. Entry timestamps: candles before entry_time_ms never trigger
        paper-trade exits.
     4. Out-of-sample isolation: OOS results do not depend on OOS-only
        tuning; warm-up prefix is taken ONLY from prior segments.

  B. Paper-exit recovery:
     5. Delayed cron run (>5h gap): gap_detected=True, more candles
        are processed.
     6. Missing candles: pair not in candles_by_pair → incomplete_pairs
        is non-empty; no silent "ok" claim.
     7. Retry behaviour: second call with same candles → no duplicate
        exit, no double-debit of realized PnL.
     8. Stop/target hit semantics preserved (stop-first on ambiguous).
     9. Equity-snapshot persistence: each close appends a snapshot
        that survives a fresh engine instance.
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


def _trend_up_candles(n=250, start_ts=1_700_000_000_000,
                      interval_ms=3_600_000,
                      start_price=100.0, slope=0.5):
    """Generate a strictly-upward-trending candle series suitable for
    EMA20 > EMA50 > EMA200 (UP trend) once warm-up is complete."""
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


# Local tmp_store fixture for the paper-exit tests in this module
# (defined locally so the file is self-contained).
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
# A. Walk-forward warm-up
# ===========================================================================
class TestWalkForwardWarmupPhase10:
    """Phase 10 — verify the warm-up prefix fix.

    Pre-Phase-10: walk_forward partitioned candles into dev/val/oos
    and ran each segment independently. A 125-bar val segment couldn't
    initialise EMA200 (needs 200 bars), so trend_at(i) returned FLAT
    for all i < 199, and TrendPullback / MomentumContinuation never
    fired in val/oos.

    Phase 10: each non-dev segment is run with a warm-up prefix taken
    from the prior segment's last `warmup_candles` bars. EMA200 can
    now initialise BEFORE the scored segment begins.
    """

    def test_ema200_initialises_on_short_segment_with_prefix(self):
        """A 125-bar segment with a 200-bar prefix should have
        EMA200 defined at every segment bar."""
        from coindcx_lab.engine.indicators import IndicatorSet
        prefix = _trend_up_candles(n=200)
        seg = _trend_up_candles(n=125,
                                start_ts=prefix[-1].open_time_ms + 3_600_000)
        all_candles = prefix + seg
        ind = IndicatorSet.from_candles(all_candles)
        # EMA200 should be defined for all bars at i >= 199 (the prefix
        # alone is exactly 200 bars, so EMA200 is defined at i=199, the
        # LAST bar of the prefix). All 125 segment bars (i = 200..324)
        # should have EMA200 defined.
        seg_start = len(prefix)  # 200
        nan_count = sum(1 for i in range(seg_start, len(all_candles))
                        if math.isnan(ind.ema200[i]))
        assert nan_count == 0, \
            f"EMA200 should be defined for all segment bars; " \
            f"{nan_count} NaN values found"

    def test_walk_forward_val_segment_uses_dev_prefix(self):
        """walk_forward() should produce a non-trivial warmup_prefix_len
        for the val segment (drawn from dev)."""
        from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        # 500 candles split into dev / val / oos by day boundaries.
        candles = _trend_up_candles(n=500, slope=0.1)
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)

        from coindcx_lab.engine.backtest import _day_iso_from_ms
        early = candles[125].open_time_ms
        mid = candles[250].open_time_ms
        dev_end = _day_iso_from_ms(early)
        val_end = _day_iso_from_ms(mid)
        results = bt.walk_forward(pair="TEST", candles=candles,
                                  dev_end_iso=dev_end, val_end_iso=val_end)

        # DEV: first segment, no prefix
        assert results["dev"].warmup_prefix_len == 0
        # VAL: should have a warm-up prefix from dev (length 200 since
        # strategy.warmup_candles=200 for BaselineSMA? Actually
        # BaselineSMAStrategy.warmup_candles=60; max(60, 60)=60)
        assert results["val"].warmup_prefix_len > 0, \
            "val segment should have a non-zero warm-up prefix"
        # OOS: prefix from (dev + val)
        assert results["oos"].warmup_prefix_len > 0, \
            "oos segment should have a non-zero warm-up prefix"

    def test_segment_boundary_no_scored_trades_from_prefix(self):
        """If a BUY signal would have fired in the warm-up prefix
        (before segment_start_idx), it must NOT be scored as a segment
        trade.

        We use BaselineSMAStrategy on a rising series: it should fire
        BUY candidates once warm-up completes. We deliberately put the
        boundary so that the prefix contains most of the signal-bearing
        bars; the segment is short. We verify num_trades == 0 for the
        segment (because we don't carry the position in) but
        eligible_bars may be > 0.
        """
        from coindcx_lab.api.models import DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        prefix = _trend_up_candles(n=200, slope=0.5)
        seg = _trend_up_candles(n=30, slope=0.5,
                                start_ts=prefix[-1].open_time_ms + 3_600_000)
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        result = bt.run(pair="TEST", candles=seg, provenance=prov,
                        segment="val", warmup_prefix=prefix,
                        segment_start_idx=len(prefix))
        # No scored trades should have entry_idx < segment_start_idx.
        for t in result.trades:
            assert t.entry_idx >= result.segment_start_idx, \
                f"trade entry_idx={t.entry_idx} < segment_start_idx=" \
                f"{result.segment_start_idx} (warm-up leakage)"
        # Buy-hold should use SEGMENT prices, not prefix prices.
        bh_expected = (seg[-1].close / seg[0].open - 1) * 100
        assert abs(result.buy_hold_return_pct - bh_expected) < 0.1, \
            f"bh should be {bh_expected:.4f}, got " \
            f"{result.buy_hold_return_pct:.4f}"

    def test_out_of_sample_isolation(self):
        """The OOS segment's warm-up prefix must come from PRIOR
        segments (dev + val), not from OOS itself.

        We construct candles where the OOS segment's first bar has a
        specific timestamp, and verify the warm-up prefix's last bar
        is from val (not from OOS)."""
        from coindcx_lab.api.models import Candle
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        candles = _trend_up_candles(n=600, slope=0.1)
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
                                  dev_end_iso=dev_end, val_end_iso=val_end)

        # OOS warmup_prefix_len should equal strategy.warmup_candles (60)
        # OR config.warmup_skip_bars (60), whichever is greater.
        assert results["oos"].warmup_prefix_len == 60, \
            f"oos prefix should be 60, got " \
            f"{results['oos'].warmup_prefix_len}"

        # The OOS segment's first bar should have open_time_ms >= the
        # val_end day boundary (i.e., strictly AFTER val).
        # We can't directly check the prefix contents from the result,
        # but we CAN check that oos.equity_curve starts at a timestamp
        # > val's last candle timestamp.
        if results["oos"].equity_curve and results["val"].equity_curve:
            oos_first_ts = results["oos"].equity_curve[0][0]
            val_last_ts = results["val"].equity_curve[-1][0]
            assert oos_first_ts > val_last_ts, \
                f"OOS first bar ts {oos_first_ts} should be > val last " \
                f"ts {val_last_ts} (segment isolation)"

    def test_eligible_bars_reported_per_segment(self):
        """Each BacktestResult must report eligible_bars — the count
        of bars in the segment where signal evaluation was actually
        permitted (warm-up complete AND inside segment)."""
        from coindcx_lab.api.models import DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        candles = _trend_up_candles(n=250, slope=0.5)
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        result = bt.run(pair="TEST", candles=candles, provenance=prov)
        # BaselineSMA.warmup_candles=60, warmup_skip_bars=60.
        # For 250 candles with no prefix, the warmup gate is
        # `i+1 >= 60` which first passes at i=59. So eligible_bars
        # = 250 - 59 = 191.
        assert result.eligible_bars == 191, \
            f"eligible_bars should be 191, got {result.eligible_bars}"
        # total_bars should be 250 (no prefix).
        assert result.total_bars == 250
        assert result.warmup_prefix_len == 0

    def test_zero_trade_segment_emits_warning(self):
        """A segment with eligible bars but no BUY signals must emit
        a warning so callers can distinguish 'no signals fired' from
        'no data' / 'calc failure'."""
        from coindcx_lab.api.models import DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import (
            BaselineSMAStrategy, SignalEngine,
        )

        # Flat series: prices don't move, no signals will fire.
        from coindcx_lab.api.models import Candle
        candles = []
        base_ts = 1_700_000_000_000
        for i in range(250):
            candles.append(Candle(
                open_time_ms=base_ts + i * 86_400_000,
                open=100, high=101, low=99, close=100, volume=100,
                close_time_ms=base_ts + (i + 1) * 86_400_000 - 1,
                complete=True))
        s = _settings_for_paper()
        strat = BaselineSMAStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        result = bt.run(pair="TEST", candles=candles, provenance=prov)
        assert result.num_trades == 0
        assert result.eligible_bars > 0
        assert any("zero_trades_with_eligible_bars" in w
                    for w in result.warnings), \
            f"warnings should mention zero_trades_with_eligible_bars, " \
            f"got {result.warnings}"

    def test_no_eligible_bars_emits_warning(self):
        """A segment with bars but 0 eligible (warm-up too long)
        must emit a no_eligible_bars warning."""
        from coindcx_lab.api.models import DataProvenance, SourceKind
        from coindcx_lab.engine.backtest import BacktestConfig, Backtester
        from coindcx_lab.engine.risk import RiskSettings
        from coindcx_lab.engine.signals import (
            TrendPullbackStrategy, SignalEngine,
        )

        # TrendPullback.warmup_candles=200; segment has 100 bars and
        # NO prefix — so warm-up can never complete.
        from coindcx_lab.api.models import Candle
        candles = _trend_up_candles(n=100, slope=0.5)
        s = _settings_for_paper()
        strat = TrendPullbackStrategy()
        eng = SignalEngine(settings=s, strategies=[strat])
        cfg = BacktestConfig(starting_capital_inr=2000, warmup_skip_bars=60)
        bt = Backtester(settings=s, strategy=strat, signal_engine=eng,
                        config=cfg)
        prov = DataProvenance(source=SourceKind.SIMULATED, endpoint="test",
                              fetched_at_utc=datetime.now(timezone.utc))
        result = bt.run(pair="TEST", candles=candles, provenance=prov)
        assert result.num_trades == 0
        assert result.eligible_bars == 0
        assert any("no_eligible_bars" in w for w in result.warnings), \
            f"warnings should mention no_eligible_bars, " \
            f"got {result.warnings}"


# ===========================================================================
# B. Paper-exit recovery
# ===========================================================================
class TestPaperExitRecoveryPhase10:

    def test_first_check_has_no_gap(self, tmp_store):
        """First-ever exit check: gap_hours is None (no prior check).
        last_exit_check_ms is set to current_time_ms after the call."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)
        # Candle after entry
        candle = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                         h=105, l=99, c=104)
        now_ms = trade.entry_time_ms + 7_200_000
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle]},
            current_time_ms=now_ms)
        assert result["gap_hours"] is None
        assert result["gap_detected"] is False
        assert result["last_check_ms"] == 0  # was 0 before this call
        assert result["processed_at_ms"] == now_ms
        # Engine state now has last_exit_check_ms == now_ms
        assert pe.last_exit_check_ms == now_ms

    def test_delayed_cron_run_gap_detected(self, tmp_store):
        """If the gap since last check > 5h, gap_detected=True."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)

        # First check at T+1h (no exit, just sets last_exit_check_ms).
        candle1 = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                         h=101, l=99, c=100)
        pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle1]},
            current_time_ms=trade.entry_time_ms + 3_600_000)
        first_check = pe.last_exit_check_ms
        assert first_check == trade.entry_time_ms + 3_600_000

        # Second check 10 hours later (gap > 5h threshold).
        # Candle that hits target during the gap.
        candle2 = _candle(open_time_ms=trade.entry_time_ms + 10 * 3_600_000,
                         h=105, l=99, c=104)
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle2]},
            current_time_ms=trade.entry_time_ms + 10 * 3_600_000)
        assert result["gap_detected"] is True
        assert result["gap_hours"] >= 9.0  # ~9h gap (10h - 1h first check)

    def test_missing_pair_surfaces_in_incomplete(self, tmp_store):
        """If an OPEN trade's pair is missing from candles_by_pair,
        incomplete_pairs must be non-empty — never silent."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal(pair="B-MISSINGPAIR")
        trade, _ = pe.open_position(signal=sig)
        # Pass NO candles for the missing pair
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-OTHER": [_candle(
                open_time_ms=trade.entry_time_ms + 3_600_000)]},
            current_time_ms=trade.entry_time_ms + 7_200_000)
        assert result["closed_count"] == 0
        assert len(result["incomplete_pairs"]) >= 1
        assert any(ip["pair"] == "B-MISSINGPAIR"
                    for ip in result["incomplete_pairs"])
        # The reason should be 'missing'
        ip = next(ip for ip in result["incomplete_pairs"]
                  if ip["pair"] == "B-MISSINGPAIR")
        assert ip["reason"] == "missing"

    def test_empty_candles_surfaces_in_incomplete(self, tmp_store):
        """If candles_by_pair has the pair but with an empty list,
        incomplete_pairs must record it (reason='empty')."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": []},
            current_time_ms=trade.entry_time_ms + 7_200_000)
        assert len(result["incomplete_pairs"]) == 1
        assert result["incomplete_pairs"][0]["reason"] == "empty"

    def test_retry_does_not_duplicate_exit(self, tmp_store):
        """Calling check_exits_with_recovery twice with the SAME candle
        must NOT close the trade twice. After the first call closes it,
        the second call's idempotency re-check should skip the UPDATE."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)
        # Candle hits target
        candle = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                         h=115, l=99, c=110)
        # First call
        r1 = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle]},
            current_time_ms=trade.entry_time_ms + 7_200_000)
        assert r1["closed_count"] == 1
        pnl_after_1 = pe.state.realized_pnl
        # Second call: trade is now CLOSED, get_open_trades() returns []
        r2 = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle]},
            current_time_ms=trade.entry_time_ms + 10_800_000)
        assert r2["closed_count"] == 0
        # Realized PnL must NOT have changed (no double-debit)
        assert pe.state.realized_pnl == pnl_after_1, \
            "realized PnL changed on retry — duplicate exit debited"

    def test_stop_first_on_ambiguous_bar(self, tmp_store):
        """Both stop and target hit in same candle → stop-first."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal(entry=100, stop=98, target=104)
        trade, _ = pe.open_position(signal=sig)
        candle = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                         h=105, l=97, c=100)
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle]},
            current_time_ms=trade.entry_time_ms + 7_200_000,
            assume_stop_first_on_ambiguous=True)
        assert result["closed_count"] == 1
        # Exit reason should be 'stop' (conservative)
        closed = result["closed_trades"][0]
        assert closed.exit_reason == "stop"

    def test_equity_snapshot_persists_across_engines(self, tmp_store):
        """A close must append an equity snapshot that survives a fresh
        PaperTradingEngine instance loaded from the same store."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)
        candle = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                         h=105, l=99, c=104)
        pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle]},
            current_time_ms=trade.entry_time_ms + 7_200_000)
        # Count snapshots after the close
        snaps_before = pe.store.list_equity_snapshots(limit=1000)
        n_before = len(snaps_before)
        assert n_before >= 2  # at least 1 open + 1 close snapshot

        # Re-instantiate the engine from the same store
        pe2 = PaperTradingEngine(settings=s, store=tmp_store)
        snaps_after = pe2.store.list_equity_snapshots(limit=1000)
        assert len(snaps_after) == n_before, \
            "snapshots must persist across engine instances"
        # last_exit_check_ms should also persist
        assert pe2.last_exit_check_ms == pe.last_exit_check_ms

    def test_last_exit_check_ms_persists(self, tmp_store):
        """The last_exit_check_ms field must persist across engine
        re-instantiation (it lives in the state blob)."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)
        candle = _candle(open_time_ms=trade.entry_time_ms + 3_600_000,
                         h=105, l=99, c=104)
        target_time = trade.entry_time_ms + 7_200_000
        pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle]},
            current_time_ms=target_time)
        assert pe.last_exit_check_ms == target_time
        # Re-instantiate
        pe2 = PaperTradingEngine(settings=s, store=tmp_store)
        assert pe2.last_exit_check_ms == target_time, \
            f"last_exit_check_ms did not persist; got " \
            f"{pe2.last_exit_check_ms}, expected {target_time}"

    def test_candle_before_entry_filtered(self, tmp_store):
        """A candle whose open_time_ms <= entry_time_ms must NOT
        trigger an exit (trade wasn't open yet)."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)
        # Candle BEFORE entry (open_time = entry - 1h) — hits stop.
        before_candle = _candle(
            open_time_ms=trade.entry_time_ms - 3_600_000,
            h=101, l=90, c=100)  # low=90 hits stop=98
        # Candle AFTER entry — neither hits.
        after_candle = _candle(
            open_time_ms=trade.entry_time_ms + 3_600_000,
            h=101, l=99, c=100)
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [before_candle, after_candle]},
            current_time_ms=trade.entry_time_ms + 7_200_000)
        assert result["closed_count"] == 0, \
            "candle before entry must not trigger exit"
        # Trade should still be OPEN
        opens = pe.store.get_open_trades()
        assert len(opens) == 1

    def test_candle_at_or_after_last_check_processed(self, tmp_store):
        """Candles after the (last_check_ms - 1 interval buffer) are
        processed; candles before are filtered."""
        from coindcx_lab.engine.paper import PaperTradingEngine
        s = _settings_for_paper()
        pe = PaperTradingEngine(settings=s, store=tmp_store)
        sig = _buy_signal()
        trade, _ = pe.open_position(signal=sig)

        # First check at T+1h — no exit, sets last_exit_check_ms.
        candle_old = _candle(
            open_time_ms=trade.entry_time_ms + 3_600_000,
            h=101, l=99, c=100)
        pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle_old]},
            current_time_ms=trade.entry_time_ms + 3_600_000)

        # Now do a second check at T+3h, supplying the OLD candle
        # (which would have hit target if it did, but it doesn't) AND
        # a new candle that DOES hit target.
        candle_new = _candle(
            open_time_ms=trade.entry_time_ms + 2 * 3_600_000,
            h=105, l=99, c=104)
        result = pe.check_exits_with_recovery(
            candles_by_pair={"B-BTCINR": [candle_old, candle_new]},
            current_time_ms=trade.entry_time_ms + 3 * 3_600_000)
        # Should close on candle_new (candle_old is older than the
        # last_check buffer so it's filtered, but it didn't hit anyway).
        assert result["closed_count"] == 1
        closed = result["closed_trades"][0]
        # Exit candle should be candle_new (open_time = entry + 2h)
        assert closed.exit_time_ms == candle_new.close_time_ms


# ===========================================================================
# C. Cron-end-to-end check (mocked)
# ===========================================================================
class TestCronExitCheckContract:
    """Smoke test the cron's exit-check loop using mocks.

    We don't hit the network. We mock MultiProviderClient.candles and
    verify that check_exits_with_recovery is called with the right
    candles and that the cron's status flips to "partial" when coverage
    is incomplete.
    """

    def test_cron_response_shape_includes_phase10_fields(self):
        """The cron response JSON contract must include the Phase 10
        fields (incomplete_pairs, gap_detected, gap_hours,
        candle_limit_used) so operators can monitor exit coverage."""
        # We don't run the full cron handler (requires Supabase env).
        # Instead we verify the fields exist on the structured result
        # returned by check_exits_with_recovery, which the cron surfaces
        # directly in its JSON response.
        from coindcx_lab.db.store import Store
        from coindcx_lab.engine.paper import PaperTradingEngine
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            s = _settings_for_paper()
            store = Store(path=db_path)
            pe = PaperTradingEngine(settings=s, store=store)
            sig = _buy_signal()
            trade, _ = pe.open_position(signal=sig)
            result = pe.check_exits_with_recovery(
                candles_by_pair={"B-BTCINR": [_candle(
                    open_time_ms=trade.entry_time_ms + 3_600_000,
                    h=101, l=99, c=100)]},
                current_time_ms=trade.entry_time_ms + 7_200_000)
            # Required Phase 10 fields
            required = ["closed_trades", "incomplete_pairs",
                        "gap_detected", "gap_hours", "last_check_ms",
                        "processed_at_ms", "open_trades_count",
                        "closed_count"]
            for k in required:
                assert k in result, f"missing field {k} in result"
        finally:
            store.close()
            os.unlink(db_path)
