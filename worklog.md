# CoinDCX Spot Lab — Multi-Agent Worklog

This file is the single shared work log for all agents working on the
CoinDCX Spot Lab project. New sections MUST be appended at the end.
Each new section MUST start with a `---` separator line.

---
Task ID: phase-10-context
Agent: main (super-z)
Task: Phase 10 kickoff — read prior state and plan work

Work Log:
- Read worklog.md (not present — fresh continuation after context reset)
- Inspected git log: latest commit 5f55a5b on main; 4 modified files uncommitted:
  supabase/migrations/0004_add_request_key_column.sql,
  tests/test_phase6_idempotency.py, tests/test_phase7_regression.py,
  tests/test_phase9_regression.py
- Read engine/backtest.py — confirmed walk_forward partitions candles
  into dev/val/oos and runs each segment INDEPENDENTLY with NO warm-up
  prefix. This is the core Phase 10 defect: a 125-bar val segment cannot
  initialize EMA200, so trend_at(i) returns FLAT for all i < 199 and
  TrendPullback / MomentumContinuation cannot fire.
- Read engine/paper.py — confirmed check_exits iterates OPEN trades only
  (CLOSED trades are skipped, providing some idempotency) but has no
  "last successful exit check" tracking. Candles before trade entry are
  NOT filtered (test_candles_before_entry_ignored acknowledges this).
- Read api/cron.py — confirmed cron fetches limit=5 candles per open
  pair. No gap detection: if cron is down >5h, exits are missed silently.
  Candle-fetch failures swallowed by bare `except Exception: pass`.
- Read tests/test_phase9_regression.py — confirmed existing tests cover
  equity, drawdown, buy-hold, paper-exit (CLOSED skip, stop-first,
  incomplete-candle skip), and a single warm-up test that only checks
  entry_idx >= warmup_skip_bars inside one full run.
- Read engine/indicators.py — confirmed ema() requires `period` bars
  (SMA seed) so ema200 needs >=200 bars; trend_at returns FLAT for
  i < 199.

Stage Summary:
- Baseline commit confirmed: 5f55a5b
- Core defect: walk_forward lacks warm-up prefix → EMA200 cannot
  initialize on short segments → trend strategies never fire in val/oos.
- Paper-exit defect: cron fetches only 5 candles, no gap detection,
  silent failures on candle fetch.
- Phase 10 work plan ready.

---
Task ID: phase-10-execution
Agent: main (super-z)
Task: Execute Phase 10 — walk-forward fix, paper-exit recovery, tests, backtests

Work Log:
- Confirmed Phase 9 accounting fix correctly applied in risk.py
  (current_equity = initial + realized_pnl, excludes open_positions_value
  to avoid double-counting; available_cash = initial + realized -
  open_positions_value; backtester mtm_equity = available_cash + qty*close).
- Confirmed baseline test count: 244 passing.
- Phase 10 fix #1 (walk-forward warm-up):
    * Added `warmup_prefix` and `segment_start_idx` parameters to
      Backtester.run().
    * Walk_forward now constructs prefix from prior segments'
      last `max(strategy.warmup_candles, cfg.warmup_skip_bars)` candles.
    * Signal evaluation gated by `i >= segment_start_idx` (in-segment).
    * Equity curve records only in-segment bars.
    * Buy-and-hold uses seg_candles[0].open / seg_candles[-1].close
      (NOT prefix+seg first/last) — segment-only benchmark.
    * Added BacktestResult fields: segment_start_idx, total_bars,
      eligible_bars, warmup_prefix_len.
    * Added warnings distinguishing:
        - empty_segment (no candles in segment)
        - no_eligible_bars (warm-up gate never passed)
        - zero_trades_with_eligible_bars (signals didn't fire)
- Phase 10 fix #2 (paper-exit recovery):
    * Added `last_exit_check_ms` to PaperTradingEngine (persisted in
      state blob as top-level key, NOT inside RiskState).
    * Added `check_exits_with_recovery()` method returning structured
      dict: closed_trades, incomplete_pairs, gap_detected, gap_hours,
      last_check_ms, processed_at_ms, open_trades_count, closed_count.
    * check_exits() is now a backward-compat wrapper that delegates.
    * Filters eligible candles by:
        - c.complete (skip still-forming)
        - c.open_time_ms > entry_time_ms (Phase 10 fix — was a known
          limitation in Phase 9 test_candles_before_entry_ignored)
        - c.open_time_ms >= (last_check_ms - interval_ms) buffer
    * Idempotency: re-reads trade status before UPDATE; skips if
      trade is no longer OPEN (concurrent-cron race protection).
    * Updated api/cron.py:
        - Computes per-pair candle_limit based on gap (5 → 200 cap).
        - Surfaces incomplete_pairs in response JSON (no silent skip).
        - cron status="partial" when incomplete_pairs is non-empty
          (vs "ok" when coverage is complete).
        - Job_log detail includes gap_detected, gap_hours,
          candle_limit_used, incomplete_pairs.
- Phase 10 fix #3 (tests):
    * Updated tests/test_paper.py 4 tests (test_check_exits_closes_on_target,
      _on_stop, _conservative_when_both_touched, _no_action_on_incomplete_latest)
      to use candle.open_time_ms > entry_time_ms (matches production cron
      behaviour — cron always fetches recent candles).
    * Updated tests/test_phase9_regression.py test_candles_before_entry_ignored
      to assert the new correct behaviour (candle before entry MUST NOT
      trigger exit).
    * Created tests/test_phase10_regression.py with 18 new tests:
        A. Walk-forward warm-up (6 tests): EMA200 init on short segment,
           walk_forward val prefix from dev, segment boundary no leakage,
           OOS isolation, eligible_bars reporting, zero/no-eligible warnings.
        B. Paper-exit recovery (10 tests): first check no gap, delayed
           cron gap detection, missing pair surfaces incomplete, empty
           candles surfaces incomplete, retry no duplicate exit, stop-first
           on ambiguous, equity-snapshot persistence, last_check_ms
           persistence, candle before entry filtered, candle at/after
           last check processed.
        C. Cron-end-to-end contract (1 test): response shape includes
           Phase 10 fields.
    * Suite result: 262 passing (244 baseline + 18 new). NO regressions.
- Phase 10 fix #4 (re-run backtests):
    * Wrote scripts/run_phase10_backtest.py — fetches WazirX 1h
      candles (limit=500) for 5 existing supported pairs (BTC, ETH,
      SOL, XRP, MATIC), runs all 4 strategies through corrected
      walk-forward, writes JSON results.
    * Live data fetched for 4 of 5 pairs (MATIC rate-limited at 429).
    * CRITICAL VERIFICATION: trend_pullback now reports
      `oos: elig=151, pref=200` — the OOS segment successfully receives
      a 200-bar warm-up prefix from (dev+val), giving 151 eligible bars
      where before Phase 10 it would have 0 (warm-up never completed).
      This is the core Phase 10 fix in action.
    * Total trades across 16 (strategy, pair) runs: 6 trades
      (3 BTC val BreakoutRetest, 3 XRP dev BreakoutRetest), all
      losing. TrendPullback, MomentumContinuation, BaselineSMA: 0 trades.
    * Strategy profitability: NOT VALIDATED. Insufficient sample (6
      trades, all losses, no winning reference).

Stage Summary:
- Phase 10 fixes implemented and tested:
    1. Walk-forward warm-up prefix (eliminates EMA200 cold-start on
       short segments; OOS now has 151 eligible bars vs 0 pre-Phase-10).
    2. Paper-exit recovery with gap detection + incomplete-pair flagging
       + idempotency against retries.
    3. 18 new Phase 10 regression tests + 5 existing tests updated.
    4. Test suite: 262 passing (no regressions).
    5. Backtest results JSON written to
       /home/z/my-project/download/phase10_walk_forward_results.json
- Strategy profitability remains UNVALIDATED (6 trades total, all
  losing, across 16 walk-forward runs). Insufficient evidence for
  further evaluation.
- Production safeguards preserved: no real orders, no leverage, no
  schema/migration/RLS changes, no portfolio resets, no strategy
  parameter tuning, env var names unchanged.
