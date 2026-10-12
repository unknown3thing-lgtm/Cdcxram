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

---
Task ID: phase-11-github-sync
Agent: main (super-z)
Task: Resolve GitHub–Vercel sync issue — push Phase 10/11 commits to GitHub main

Work Log:
- Inspected git state per user request:
    * git status: working tree clean
    * git log: local main at fffbacf (Phase 11 final)
    * git branch -vv: local main had no upstream tracking set,
      but origin/main ref existed at 5f55a5b (Phase 9)
- Verified Phase 10/11 commits exist locally and are ancestors
  of local main:
    * 4defbf9 (Phase 10 feat) ✓
    * 367a0e8 (automated UUID commit) ✓
    * ce9c3c4 (Phase 11 cleanup) ✓
    * 66370cd (Phase 11 feat) ✓
    * fffbacf (Phase 11 worklog docs) ✓
- Verified local main is 5 commits AHEAD of origin/main, 0 commits
  BEHIND (no diverge). git merge-base --is-ancestor origin/main main
  returned YES — confirming linear fast-forward history.
- Pre-push safety checks (all passed):
    * Full test suite: 289 passing (no regressions)
    * Serverless function count: 11 (within Vercel Hobby 12-function limit)
    * vercel.json: unchanged, rewrites /api/settings -> /api/health
      and /api/portfolio_reset -> /api/portfolio (no new functions added)
    * Redundant api/settings.py and api/portfolio_reset.py correctly
      absent (removed in ce9c3c4)
- Pushed to GitHub main via `git push origin main` — fast-forward
  from 5f55a5b..fffbacf succeeded.
- Verified post-push: fetched origin, confirmed origin/main ==
  main == fffbacf. Git log now shows "fffbacf (HEAD -> main, origin/main)".

Stage Summary:
- GitHub main is now at fffbacf (Phase 11 final), matching local.
- Vercel's auto-deploy (if enabled on the project) will pick up the
  new commit; the user did not request a manual `vercel --prod`
  deploy, so no deploy was performed.
- All Phase 10 + Phase 11 code, tests, and cleanup preserved.
- No reset, rebase, force-push, or destructive operation performed.
- No duplicate API files created.
- No secrets or env var values printed.
- No production portfolio data, migrations, or RLS policies modified.

---
Task ID: phase-12-start
Agent: main (super-z)
Task: Phase 12 kickoff — baseline check, deployment capability check

Work Log:
- Verified local main at bca5781 (HEAD -> main, origin/main) — same as
  GitHub main after the Phase 11 sync push.
- Confirmed baseline test suite: 289 passing in 24.91s (no regressions
  from Phase 11 baseline).
- Confirmed 4 migrations unchanged (0001-0004; no content changes,
  file-mode preserved from prior phases).
- Confirmed 11 serverless functions (within Vercel Hobby 12-function
  limit).
- Checked Vercel deployment capability:
    * `vercel` CLI is NOT installed in this environment.
    * However VERCEL_TOKEN, VERCEL_ORG_ID, VERCEL_PROJECT_ID env vars
      ARE set (values not printed per rule 6).
    * Plan: push to GitHub main → triggers auto-deploy via Vercel-
      GitHub integration → verify via Vercel REST API using curl +
      VERCEL_TOKEN (no CLI needed).

Stage Summary:
- Phase 12 work plan ready.
- Strategy investigation is the next major step (trace each strategy's
  entry conditions to identify blocking rules).
- Vercel deployment verification will use the REST API after the
  GitHub push triggers auto-deploy.

---
Task ID: phase-12-progress
Agent: main (super-z)
Task: Phase 12 strategy investigation + pair universe scan

Work Log:
- Wrote scripts/diagnose_strategy_entries.py to trace each strategy's
  entry conditions against historical candles.
- Pre-fix diagnostic showed breakout_retest produced 0 BUY_CANDIDATE
  signals across 940 evaluations on BTC 1h (full 1000-candle series).
- Investigated breakout_retest indicator (src/coindcx_lab/engine/
  indicators.py). CONFIRMED DEFECT: the function used `break` +
  `breakout_idx is None` check to find only the FIRST breakout in
  the entire series. If the first breakout was DOWN (as on BTC 1h
  where the first breakout at bar 41 was a down-move), the long-only
  strategy could NEVER fire for the rest of the series.
  This explained the Phase 11 paradox: my diagnostic on the full
  series showed 0 BUY_CANDIDATE, but Phase 11's per-segment runs
  produced 20 trades because each segment's prefix+segment series
  happened to start with an up-move.
- Fixed breakout_retest to scan ALL bars and track the MOST RECENT
  UP breakout (matching the existing docstring which already said
  "most recent bar"). The long-only strategy only cares about up-
  breakouts, so down-breakouts are no longer tracked. The previous
  behavior of returning direction="down" is removed — the strategy
  never used it (it always returned WAIT on direction != "up"), so
  no behavior change for the strategy.
- Wrote tests/test_phase12_regression.py with 14 tests covering:
  A. breakout_retest defect fix (4 tests):
     - no_down_breakout_blocks_up_breakout_detection
     - no_up_breakout_returns_has_breakout_false
     - most_recent_up_breakout_preferred_over_earlier
     - strategy_can_fire_after_early_down_breakout (integration)
  B. Fee/slippage/net-PnL calculation (3 tests):
     - test_entry_fee_applied_on_open
     - test_exit_fee_added_on_close
     - test_net_pnl_includes_fees_and_slippage
  C. Equity snapshot persistence (2 tests):
     - test_multiple_closes_append_multiple_snapshots
     - test_snapshots_survive_engine_reinstantiation
  D. Portfolio reconciliation (1 test):
     - test_reconcile_fixes_open_positions_value
  E. Provenance status reporting (4 tests):
     - LIVE / CACHED / SIMULATED / UNAVAILABLE labels verified
- Post-fix diagnostic shows breakout_retest now produces:
    BTC: 11 BUY_CANDIDATE (was 0)
    ETH: 15 BUY_CANDIDATE (was 0)
    SOL: 10 BUY_CANDIDATE (was 0)
    XRP: 32 BUY_CANDIDATE (was 0)
  Total: 68 BUY_CANDIDATE signals (was 0).
- Other 3 strategies (trend_pullback, momentum_continuation,
  baseline_sma) still produce 0 BUY_CANDIDATE — but these are
  NOT bugs. Their entry conditions are genuinely restrictive:
    * trend_pullback: requires strict UP trend (EMA20>EMA50>EMA200)
      AND price in pullback zone [EMA50, EMA20] AND RSI in [40, 55].
      The 32 NO_TRADE cases on BTC had BLOCK reason "R/R after cost
      1.42" — setups were found but the risk gate rejected them
      because R/R after costs (1.42) was below the 2.0 target.
    * momentum_continuation: 274 trend-UP supporting reasons but
      82 NO_TRADE (risk gate rejected).
    * baseline_sma: 478 NO_TRADE out of 940 (risk gate rejected).
- Wrote scripts/scan_pair_universe.py to scan WazirX all-tickers.
  Found 216 pairs total, 96 active (positive 24h INR turnover).
  Top 20 active pairs include the existing BTC/ETH/SOL/LINK plus
  new candidates: USDT, WRX, INJ, PEPE, DENT, CKB, TRX, SHIB, NEAR,
  FET, SKY, BAT, HNT, CELR, COTI, ZEC.
  Note: staleness cannot be reliably detected from the ticker alone
  (WazirX doesn't return last_trade_at in the normalized fields).
  The MATIC pair had bh=0% in Phase 11 because its latest candle is
  from 2025-10-25 — pair was delisted from WazirX around Oct 2025.

Stage Summary:
- Phase 12 confirmed defect (breakout_retest first-breakout bug) FIXED.
- Phase 12 regression tests (14 new) all pass.
- Full test suite: 303 passing (289 prior + 14 new).
- Strategy investigation complete — root cause of Phase 11's 0-trade
  result for breakout_retest on the full series is fixed.
- Pair universe expanded to 96 active pairs (from 5 in Phase 11).
- Ready to proceed to: candidate variants research + Phase 12
  backtest runner + commit/push/deploy.

---
Task ID: phase-12-final
Agent: main (super-z)
Task: Phase 12 commit, push, deployment attempt, honest reporting

Work Log:
- Final pre-commit verification:
    * Full test suite: 303 passing (289 prior + 14 new Phase 12).
    * Serverless function count: 11 (within Vercel Hobby 12-function
      limit).
    * Migrations: 4 unchanged (0001-0004 untouched).
    * Working tree: clean except for the new Phase 12 files.
    * src/coindcx_lab/engine/indicators.py change scoped to
      breakout_retest function only.
- Committed Phase 12 changes: SHA 6f54bef on local main.
    * 9 files changed, including:
      - src/coindcx_lab/engine/indicators.py (breakout_retest fix)
      - src/coindcx_lab/engine/candidate_variants.py (NEW, 3 variants)
      - tests/test_phase12_regression.py (NEW, 14 tests)
      - scripts/diagnose_strategy_entries.py (NEW)
      - scripts/scan_pair_universe.py (NEW)
      - scripts/run_phase12_comparison.py (NEW)
      - download/*.json (3 diagnostic results files)
- Pushed to GitHub main: bca5781..6f54bef fast-forward succeeded.
- Verified post-push: origin/main == local main == 6f54bef.
- Attempted Vercel Production deployment verification:
    * `vercel` CLI is NOT installed in this environment.
    * VERCEL_TOKEN, VERCEL_PROJECT_ID, VERCEL_ORG_ID env vars are
      NOT available in subsequent bash calls (they were visible only
      in the very first bash check at the start of Phase 12, before
      any other work was done).
    * curl call to https://api.vercel.com/v9/projects/$VERCEL_PROJECT_ID
      returned 403 forbidden: "The request is missing an authentication
      token."
    * Per Phase 12 mandatory rule 9: "If credentials are unavailable,
      stop and report the blocker honestly."
    * Cannot verify Vercel Production deployment SHA matches the
      GitHub commit.

Stage Summary:
- Phase 12 code + tests committed locally at 6f54bef.
- Pushed to GitHub main successfully (fast-forward bca5781..6f54bef).
- GitHub main verified at 6f54bef (matches local main).
- Vercel Production deployment status: CANNOT VERIFY — credentials
  unavailable in this session. The Vercel-GitHub integration (if
  enabled on the project) should auto-trigger a deployment from
  the push, but I cannot query the Vercel API to confirm the
  deployment SHA, readiness, or smoke-test results.
- Phase 12 is PARTIALLY COMPLETE per the mandatory rule 8
  ("A local commit alone is not completion"). The GitHub push IS
  complete; the Vercel deployment verification is NOT complete.
- Honest blocker reported per mandatory rule 9.

---
Task ID: phase-13-start
Agent: main (super-z)
Task: Phase 13 kickoff — baseline verification + credential check

Work Log:
- Verified local main at bc53650 (HEAD -> main, origin/main) — same as
  Phase 12 baseline.
- Found 19 spurious file-mode changes (100644 -> 100755, no content
  change) on Phase 11/12 files. Set `git config core.fileMode false`
  and `git checkout -- .` to revert. Working tree is now clean.
- Confirmed baseline test suite: 303 passing in 28.34s.
- Confirmed 4 migrations byte-for-byte unchanged (0001: 9969 bytes,
  0002: 6954 bytes, 0003: 2571 bytes, 0004: 1157 bytes).
- Confirmed 11 serverless functions (within Vercel Hobby 12-function
  limit).
- Attempted Vercel deployment verification:
    * `vercel` CLI was NOT installed; installed via `npm install -g
      vercel` (Vercel CLI 63.1.2).
    * No VERCEL_TOKEN / VERCEL_PROJECT_ID / VERCEL_ORG_ID env vars
      visible in this bash session.
    * No ~/.local/share/com.vercel.cli/auth.json file present.
    * No .vercel/ project config (no `vercel link` ever done here).
    * `vercel whoami` requires a token; cannot authenticate.
- Per mandatory rule: "If deployment credentials are unavailable,
  stop and report the blocker; never claim deployment succeeded
  without verification." Will proceed with strategy/pair-universe
  research work (which doesn't require deployment), then attempt
  Vercel verification at end and report honestly.

Stage Summary:
- Phase 13 baseline confirmed.
- Vercel deployment verification BLOCKED (credentials unavailable).
- Will report the blocker honestly at the end per the mandatory rule.
- Proceeding with strategy research + pair-universe expansion.

---
Task ID: phase-13-execution
Agent: main (super-z)
Task: Phase 13 — pair universe vetting, candidate strategies, evaluation, tests

Work Log:
- Wrote scripts/vet_pair_universe.py — vets 26 candidate pairs by:
    * Ticker turnover + spread
    * Candle freshness (latest candle < 6h old)
    * 500-candle history verification
  Found 11 pairs with spread < 2.0% (data-quality threshold):
    B-USDTINR, B-BTCINR, B-WRXINR, B-ETHINR, B-TRXINR, B-PEPEINR,
    B-SHIBINR, B-COTIINR, B-CELRINR, B-LINKINR, B-XRPINR.
  MATIC correctly excluded as stale (latest candle 8453.5h old).

- Designed and implemented 2 new candidate strategies in
  src/coindcx_lab/engine/phase13_candidates.py:
    1. MultiTimeframeTrendPullback — uses 4h (HTF) EMA20>EMA50>EMA200
       trend filter + 1h (LTF) pullback zone entry. HTF candles
       supplied at construction; strategy uses binary search to find
       the most-recent-completed HTF bar at each LTF evaluation
       (prevents look-ahead bias).
    2. MeanReversionRsi — RSI(14) crosses from <30 to >30 (recovery
       trigger) + close > prev_close. Stop: below 10-bar low.
       Target: 2R. Independent of trend filter.

- Wrote tests/test_phase13_regression.py with 10 new tests:
    A. MultiTimeframeTrendPullback (4 tests):
       - htf_trend_filter_blocks_when_flat
       - no_lookahead_htf_bar_at_or_after_ltf_open_not_used
       - ltf_pullback_zone_enforced
       - warmup_incomplete_returns_wait (skeleton default NO_TRADE)
    B. MeanReversionRsi (3 tests):
       - no_rsi_cross_returns_wait
       - warmup_incomplete_returns_wait
       - close_below_prev_close_blocks_entry
    C. Paper-trading reliability (Phase 13 spot-check) (3 tests):
       - stop_loss_with_adverse_slippage
       - target_with_adverse_slippage
       - missing_pair_surfaces_in_incomplete

- Wrote scripts/run_phase13_comparison.py — runs:
    * 4 production strategies × 11 pairs × 1h interval = 44 runs
    * 2 candidate strategies × 11 pairs = 22 runs (dev/val only for
      candidates; OOS preserved per Phase 13 brief req 3)
    Total: 66 runs.

- Phase 13 backtest results (1h interval, 1000 candles per pair):
  PRODUCTION strategies (dev+val+oos across 11 pairs):
    breakout_retest:       66 trades, 14 wins, 52 losses, +₹31.35 net
    trend_pullback:         0 trades (entry conditions too restrictive)
    momentum_continuation: 0 trades (entry conditions too restrictive)
    baseline_sma:           0 trades (entry conditions too restrictive)
  CANDIDATE strategies (dev+val only across 11 pairs):
    mean_reversion_rsi:     0 trades (RSI rarely crosses 30 on INR-spot)
    mtf_trend_pullback:     0 trades (HTF trend + LTF pullback combo
                          too restrictive)

- Notable: breakout_retest with Phase 12 fix + 11 vetted pairs now
  produces the FIRST positive net PnL across all phases (+₹31.35 INR
  = +1.57% return). However:
    * Win rate is 21.2% (14/66), below breakeven for a 2R target
      (needs 33%+ to break even after costs).
    * Some wins are end_of_data closes (force-close at last bar)
      rather than target hits — making the positive return less
      meaningful.
    * Sample is small (66 trades across 11 pairs × 3 segments) and
      trades within a segment are correlated.
    * Strategy profitability remains UNVALIDATED per Phase 13 brief
      req 4: "Do not interpret a small sample as evidence of
      profitability."

- Verified production safeguards:
    * 313 tests passing (303 prior + 10 new Phase 13).
    * 11 serverless functions (within Vercel Hobby 12-function limit).
    * 4 migrations byte-for-byte unchanged (0001: 9969, 0002: 6954,
      0003: 2571, 0004: 1157 bytes — same as Phase 12 baseline).
    * No RLS policy changes.
    * No env var name changes.
    * Paper-only trading preserved — no real orders, leverage, or
      futures.

Stage Summary:
- Phase 13 strategy investigation complete.
- 2 new candidate strategies designed and tested.
- 11 pairs vetted with transparent data-quality criteria.
- 66 backtest runs completed; breakout_retest shows small positive
  net PnL but sample is too small to validate profitability.
- 3 new candidate strategies (mtf_trend_pullback, mean_reversion_rsi,
  and 3 Phase 12 variants) all produced 0 trades — confirming that
  the INR-spot regime is genuinely challenging for trend-following
  and mean-reversion strategies with the current parameter sets.
- Ready to commit + push + attempt Vercel deployment verification.
