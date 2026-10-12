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

---
Task ID: phase-10-final
Agent: main (super-z)
Task: Commit, verify production safeguards, finalize Phase 10 report

Work Log:
- Verified env var names unchanged (ENV_SUPABASE_URL, ENV_SUPABASE_PUBLISHABLE_KEY,
  ENV_SUPABASE_SERVICE_KEY, ENV_CRON_SECRET, ENV_API_ALLOWED_ORIGIN,
  ENV_COINDCX_BASE_URL, ENV_COINDCX_TIMEOUT, ENV_COINDCX_MAX_RETRIES,
  ENV_ALLOW_SIMULATED — all preserved).
- Verified 11 deployed serverless functions (api/settings.py and
  api/portfolio_reset.py remain UNTRACKED — vercel.json rewrites already
  route /api/settings → /api/health?action=settings and
  /api/portfolio_reset → /api/portfolio?action=reset, so these files
  are bypassed and would be redundant if deployed).
- Verified 4 migrations unchanged (0001_initial_schema.sql,
  0002_rls_policies.sql, 0003_anon_user_policies.sql,
  0004_add_request_key_column.sql). Migration 0004 file-mode change
  (644 → 755) is a pre-existing artifact from prior phases, not a
  content change.
- Verified RLS policies unchanged (migration 0002 untouched).
- Verified strategy parameters unchanged (TrendPullback.warmup_candles=200,
  BreakoutRetest.warmup_candles=60, MomentumContinuation.warmup_candles=60,
  BaselineSMAStrategy.warmup_candles=60 — all preserved from prior phases).
- Verified RiskSettings defaults preserved (initial_inr=2000,
  risk_per_trade_pct=1.0, fee_pct=0.1, spread_pct=0.05, slippage_pct=0.05,
  max_drawdown_pct=15.0, etc.).
- Final test suite result: 262 passing (244 baseline + 18 new Phase 10).
- Committed Phase 10 changes: SHA 4defbf9 on main.

Final Report Sections (per Phase 10 brief, 9 required sections):
  1. Backtest results before and after the Phase 9 accounting fix
  2. Verified warm-up behavior and eligible bars per segment
  3. New tests and total suite results
  4. Paper-exit gap detection and recovery behavior
  5. Data coverage, missing candles and rate-limit failures
  6. Whether the strategies have enough evidence for further evaluation
  7. Commit SHA, deployment status and function count
  8. What was verified locally versus live
  9. Remaining limitations and next recommended phase

Stage Summary:
- Phase 10 implementation complete and committed (SHA 4defbf9).
- Strategy profitability remains UNVALIDATED — 6 trades total across
  16 walk-forward runs (BreakoutRetest only), all losing. Insufficient
  evidence for further evaluation.
- Production safeguards fully preserved — no real orders, no leverage,
  no migration/RLS/env var changes, no portfolio resets.
- Deployment: not deployed live in this session; verified locally only.

---
Task ID: phase-11-start
Agent: main (super-z)
Task: Phase 11 kickoff — inspect commits, remove redundant files, plan work

Work Log:
- Found an automated commit 367a0e8 (UUID message) on top of Phase 10
  that committed previously-untracked api/settings.py,
  api/portfolio_reset.py, and a junk tool-results/ text file. This
  would have pushed the deployed Vercel function count to 13 (over
  Hobby 12-function limit).
- Inspected api/portfolio.py — already handles action=reset (line 37)
  via vercel.json rewrite /api/portfolio_reset → /api/portfolio?action=reset.
- Inspected api/health.py — already handles action=settings (line 37)
  via vercel.json rewrite /api/settings → /api/health?action=settings.
- Confirmed api/settings.py and api/portfolio_reset.py are REDUNDANT
  duplicates of logic already present in health.py / portfolio.py.
- Removed them via git rm and committed as ce9c3c4 "chore(phase11):
  remove redundant api/settings.py and api/portfolio_reset.py".
  Also removed the tool-results/*.txt junk file.
- Verified 11 deployed functions: backtest, candles, cron, health,
  markets, portfolio, prices, risk, scanner, signals, trades.
- Inspected commits 45b03aa (Phase 9 equity fix), 5f55a5b (Phase 9
  cash-cap test update), 4defbf9 (Phase 10 walk-forward + paper-exit).
- Ran full test suite: 262 passing (no regressions).
- Ran API endpoint + idempotency + Phase 7 regression tests: 32 passing.

Stage Summary:
- Pre-Phase-11 cleanup done (commit ce9c3c4) — restored 11-function
  count.
- Phase 10 verified locally; not deployed live in this session.
- Ready to begin Phase 11 work: historical data expansion, 4h/daily
  interval support, strategy evaluation.

---
Task ID: phase-11-final
Agent: main (super-z)
Task: Phase 11 execution — interval support, history expansion, paper-exit tests, final report

Work Log:
- Pre-flight cleanup (commit ce9c3c4): removed redundant api/settings.py
  and api/portfolio_reset.py (accidentally committed in 367a0e8) plus
  junk tool-results/ text file. Restored deployed function count to 11.
- Inspected commits 45b03aa, 5f55a5b, 4defbf9 per Phase 11 brief req 1.
  All three contain the intended changes:
    * 45b03aa: Phase 9 equity fix (current_equity excludes
      open_positions_value to avoid double-counting).
    * 5f55a5b: Phase 9 test update (cash-cap test reflects corrected
      equity formula).
    * 4defbf9: Phase 10 walk-forward warm-up prefix + paper-exit gap
      recovery + 18 regression tests.
- Ran full test suite (262 passing at start of Phase 11).
- Ran API endpoint + idempotency + Phase 7 regression tests: 32 passing.
- Phase 11 fix #1 (429 retry):
    * Added rate_limit_retries=4 and rate_limit_backoff_seconds=2.0
      to WazirXClient.
    * Rewrote _request() as a single while-loop with separate budgets
      for 5xx/network errors (max_retries) and 429 (rate_limit_retries).
    * Updated test_429_raises_rate_limit -> test_429_retried_then_raises
      _rate_limit; added test_429_eventually_succeeds_after_retries.
- Phase 11 fix #2 (4h/1d interval support):
    * Added 'interval' parameter to Backtester.run() (default '1h').
    * Added 'interval' parameter to walk_forward() (default '1h').
    * Interval is propagated to signal_engine.evaluate() (was hard-
      coded to '1h').
    * Walk-forward split remains UTC-day-boundary based; works at any
      interval.
    * Fixed segment_start_idx recompute bug: always use post-de-dup
      prefix length, not the caller-passed value.
- Phase 11 fix #3 (tests):
    * tests/test_phase11_regression.py (NEW, 17 tests): interval
      plumbing, warm-up at 4h/1d, candle integrity (sorting, dedup,
      OHLC, missing intervals), segment boundaries at 4h/1d, 429 retry.
    * tests/test_phase11_paper_exit.py (NEW, 9 tests): very long gap,
      concurrent cron race, multiple retries, rate-limit failure,
      multiple open trades, all-incomplete candles, no eligible
      candles, processed_at_ms, zero open trades.
- Phase 11 fix #4 (data expansion probe):
    * scripts/probe_wazirx_history.py: tests 10 pairs × 4 limit values
      (200/500/750/1000) on 1h interval. All 40 probes succeeded.
    * Data integrity verified: sorted=True, duplicates=0, missing=0,
      OHLC violations=0 across all probes.
    * WazirX reliably supports limit=1000 (gives ~42 days history on
      1h, ~167 days on 4h, ~1000 days on 1d).
    * 429 retry logic recovered from rate-limit hits without fail.
    * MATIC has stale data: latest candle 2025-10-25 (delisted ~Oct 2025).
- Phase 11 fix #5 (strategy evaluation):
    * scripts/run_phase11_backtest_slim.py: 5 pairs × 3 intervals × 4
      strategies = 60 walk-forward runs. CANDLE_LIMIT=1000.
    * Total trades: 20 (all from breakout_retest; trend_pullback,
      momentum_continuation, baseline_sma produced 0 trades each).
    * Win rate: 10% (2 wins, 18 losses).
    * Net PnL: -120.11 INR on 2000 INR starting (-6.0% return).
    * Both wins suspect: 1 end_of_data force-close (SOL 1d dev, +14.13
      INR); 1 actual target hit (XRP 4h dev, +27.39 INR).
    * All 18 losing trades exited via stop-loss.
    * Strategy profitability remains UNVALIDATED. Sample too small.
- Phase 11 fix #6 (production safeguards):
    * 11 serverless functions preserved.
    * 4 migrations unchanged (0001-0004 untouched).
    * 9 env var names unchanged.
    * No real-money / leverage / futures code added.
    * No RLS policy changes.
    * Strategy parameters unchanged.
- Final test suite result: 289 passing (263 prior + 26 new Phase 11).
- Committed Phase 11 changes: SHA 66370cd on main.

Stage Summary:
- Phase 11 implementation complete and committed (SHA 66370cd).
- Strategy profitability remains UNVALIDATED — 20 trades across 60
  walk-forward runs, 10% win rate, -6.0% net return. All 3 trend
  strategies (trend_pullback, momentum_continuation, baseline_sma)
  produced ZERO trades across all 60 runs — their entry conditions
  are too restrictive for the INR-spot regime.
- Phase 10 fixes (warm-up prefix, paper-exit gap recovery) verified
  working at all 3 intervals (1h, 4h, 1d).
- WazirX reliably supports limit=1000 candles per pair (no truncation,
  full data integrity).
- 429 rate-limit retry logic working in production-ready form.
- Paper-exit recovery tested with 9 additional isolated cases beyond
  Phase 10's coverage.
- Production safeguards fully preserved.
