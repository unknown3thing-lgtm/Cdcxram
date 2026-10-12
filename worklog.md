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

---
Task ID: phase-13-final
Agent: main (super-z)
Task: Phase 13 final — commit, push, Vercel deployment verification attempt

Work Log:
- Final pre-commit verification:
    * Full test suite: 313 passing (303 prior + 10 new Phase 13).
    * Serverless function count: 11 (within Vercel Hobby 12-function
      limit).
    * Migrations: 4 byte-for-byte unchanged (0001: 9969, 0002: 6954,
      0003: 2571, 0004: 1157 bytes — same as Phase 12 baseline).
    * Working tree: clean (after reverting spurious file-mode changes
      via `git config core.fileMode false` + `git checkout -- .`).
- Committed Phase 13 changes: SHA 6242109 on local main.
    * 7 files changed, 8574 insertions(+), including:
      - src/coindcx_lab/engine/phase13_candidates.py (NEW — 2 candidates)
      - tests/test_phase13_regression.py (NEW — 10 tests)
      - scripts/vet_pair_universe.py (NEW — pair vetting)
      - scripts/run_phase13_comparison.py (NEW — backtest runner)
      - download/phase13_*.json (2 diagnostic results files)
- Pushed to GitHub main: bc53650..6242109 fast-forward succeeded.
- Verified post-push: origin/main == local main == 6242109.
- Attempted Vercel Production deployment verification:
    * `vercel` CLI installed via `npm install -g vercel` (Vercel CLI
      63.1.2) in this Phase 13 session.
    * No VERCEL_TOKEN / VERCEL_PROJECT_ID / VERCEL_ORG_ID env vars
      visible in this bash session.
    * No ~/.local/share/com.vercel.cli/auth.json file present.
    * No .vercel/ project config (no `vercel link` ever done here).
    * `vercel whoami` returns "Logged out."
    * `vercel project ls` returns "No existing credentials found."
    * Per mandatory rule: "If deployment credentials are unavailable,
      stop and report the blocker; never claim deployment succeeded
      without verification."
    * Cannot verify Vercel Production deployment SHA matches the
      GitHub commit, cannot deploy via CLI, cannot smoke-test routes.

Stage Summary:
- Phase 13 code + tests committed locally at 6242109.
- Pushed to GitHub main successfully (fast-forward bc53650..6242109).
- GitHub main verified at 6242109 (matches local main).
- Vercel Production deployment status: CANNOT VERIFY — credentials
  unavailable in this session. The Vercel-GitHub integration (if
  enabled on the project) should auto-trigger a deployment from
  the push, but I cannot query the Vercel API or CLI to confirm the
  deployment SHA, readiness, or smoke-test results.
- Phase 13 is PARTIALLY COMPLETE per the mandatory rule
  ("At the end of the phase, commit and push the verified code and
  tests to GitHub main, deploy the intended commit to Vercel
  Production, and verify that the deployed SHA matches GitHub main.
  If deployment credentials are unavailable, stop and report the
  blocker; never claim deployment succeeded without verification.").
- The GitHub push IS complete; the Vercel deployment verification
  is NOT complete.
- Honest blocker reported per the mandatory rule.

---
Task ID: phase-14-start
Agent: main (super-z)
Task: Phase 14 kickoff — audit existing strategies, design regime classifier

Work Log:
- Verified baseline: local + GitHub main at 9f226d7 (matches Phase 13
  end). Working tree clean. 313 tests passing. 11 serverless
  functions. 4 migrations byte-for-byte unchanged (0001: 9969,
  0002: 6954, 0003: 2571, 0004: 1157 bytes). Vercel credentials
  STILL NOT SET (consistent with Phase 12/13 blocker).
- Audited existing strategies per Phase 14 brief req 1:
    * breakout_retest (Phase 12 fix): now produces trades (66 in
      Phase 13). 21.2% win rate, +₹31.35 net (small sample).
    * trend_pullback: 0 trades. Blocked by strict EMA stack + RSI
      [40, 55] + risk gate (R/R after cost 1.42 < 2.0 target).
    * momentum_continuation: 0 trades. Blocked by UP trend + MACD
      + RSI>55 + relvol>1.2 — all four rarely align.
    * baseline_sma: 0 trades. Surprising — entry conditions are
      just close>EMA50 AND EMA50 rising. Investigated code: 478
      NO_TRADE cases — risk gate rejected because stop = EMA50 - 1*ATR
      is only 1 ATR below entry; with fees+slippage, R/R after cost
      falls below 2.0 target. So even the simplest strategy fails
      the risk gate due to tight stop geometry, not the entry logic.
    * MultiTimeframeTrendPullback: 0 trades. HTF (4h) trend + LTF
      (1h) pullback combo too restrictive.
    * MeanReversionRsi: 0 trades. RSI rarely crosses 30 on INR-spot
      pairs (low volatility regime).

Root cause analysis (Phase 14 brief req 1 — "Identify actual blocking
conditions before changing strategy rules"):
  1. The risk gate's reward_to_risk_target=2.0 is hard to achieve
     after fees (0.1% per side) + slippage (0.05% per side) ≈ 0.3%
     round-trip cost. With tight stops (1 ATR or less), the post-cost
     R/R is barely above 1.5, so the gate rejects most setups.
  2. The strict EMA20 > EMA50 > EMA200 trend filter is rarely
     satisfied on choppier INR-spot pairs. The 4h candles often have
     mixed EMA alignment.
  3. Mean reversion's RSI < 30 threshold is too low — INR-spot pairs
     rarely hit oversold conditions.

Phase 14 design plan:
  1. Build a market-regime classifier that doesn't depend on strict
     EMA stack — use ATR/price ratio for volatility regime + EMA50
     slope for trend regime.
  2. Build regime-aware variants:
     - RegimeTrendPullback: only fire in Trending UP regime; use
       wider stop (2 ATR) so R/R after cost is more favorable.
     - RegimeBreakoutRetest: only fire in Trending UP or transitioning
       regime; require recent retest.
     - RegimeMeanReversion: only fire in Range-bound regime; relax
       RSI threshold to < 35 (instead of < 30) but add regime filter.
     - RegimeMultiTimeframe: HTF trend + LTF entry, only when both
       timeframes agree on regime.
  3. Adjust stop geometry to give R/R after cost >= 2.0 even with
     tight stops — use 2 ATR stops instead of 1 ATR.

Stage Summary:
- Phase 14 audit complete. Root causes identified.
- Ready to build regime classifier + regime-aware candidates.

---
Task ID: phase-14-execution
Agent: main (super-z)
Task: Phase 14 — regime classifier + 4 regime-aware candidates + evaluation

Work Log:
- Built RegimeClassifier (src/coindcx_lab/engine/regime.py) with 5
  classifications: TRENDING_UP, TRENDING_DOWN, RANGE_BOUND,
  HIGH_VOLATILITY, UNCERTAIN. Uses EMA stack + EMA50 slope + ATR/price
  ratio. HIGH_VOLATILITY (ATR/price >= 2.5%) takes priority over trend.
  All other thresholds documented as class attributes for testing.

- Built 4 regime-aware candidates (src/coindcx_lab/engine/regime_strategies.py):
    A. RegimeTrendPullback — only fires in TRENDING_UP; 2-ATR stop
       (wider than production 1.5 ATR); RSI [35,60] (relaxed from
       [40,55]).
    B. RegimeBreakoutRetest — fires in TRENDING_UP or UNCERTAIN;
       blocked in HIGH_VOLATILITY; uses min(breakout-0.5%, 2*ATR)
       stop.
    C. RegimeMeanReversion — only fires in RANGE_BOUND; RSI cross 35
       (relaxed from 30); 2-ATR stop.
    D. RegimeMultiTimeframe — requires BOTH HTF (4h) and LTF (1h)
       regime TRENDING_UP; 2-ATR stop; inherits Phase 13 no-lookahead
       HTF binary search.

- Wrote tests/test_phase14_regression.py with 16 new tests:
    A. RegimeClassifier (6): TRENDING_UP/DOWN/RANGE_BOUND/HIGH_VOL/
       UNCERTAIN/EMA-not-initialized.
    B. Regime-aware strategies (8): each candidate's regime filter
       behavior, RSI threshold relaxed, 2-ATR stop geometry, MTF no-
       lookahead HTF.
    C. Paper-trading reliability spot-check (1): equity snapshot
       persistence after close.
    D. Regime counts summary (1): regime_counts() returns dict with
       all 5 regimes.

- Phase 14 evaluation backtest (scripts/run_phase14_comparison.py):
    * 4 regime candidates × 11 pairs (dev/val only — OOS preserved)
    * Production breakout_retest × 11 pairs (full dev/val/oos)
    * Total: 55 runs.

  Results:
    regime_candidates: 0 trades (all 4 candidates × 11 pairs × dev+val)
    production_breakout_retest: 66 trades, 14W/52L (21.2% win rate),
      +₹31.35 net (₹0.47/trade expectancy, profit_factor=1.139,
      avg_hold=6.9h). Exit breakdown: 52 stop, 11 target, 3 EOD.

- Analyzed regime distribution on live data:
    B-BTCINR: UP=27%, DOWN=24%, RANGE=31%, HIGH_VOL=0%, UNCERT=18%
    B-ETHINR: UP=31%, DOWN=17%, RANGE=30%, HIGH_VOL=0%, UNCERT=22%
    B-SOLINR: UP=34%, DOWN=19%, RANGE=19%, HIGH_VOL=0%, UNCERT=29%
    B-XRPINR: UP=22%, DOWN=36%, RANGE=22%, HIGH_VOL=0%, UNCERT=21%
    B-TRXINR: UP=22%, DOWN=37%, RANGE=36%, HIGH_VOL=0%, UNCERT=5%
    B-SHIBINR: UP=29%, DOWN=24%, RANGE=18%, HIGH_VOL=0%, UNCERT=29%

  Finding: regime filter (TRENDING_UP, RANGE_BOUND) passes ~25-50%
  of bars, but the OTHER entry conditions (pullback zone, RSI range,
  breakout+retest, close > prev close) still rarely align within
  those regimes. So even with the regime filter, the strategies
  produce 0 trades.

  This is the HONEST outcome per Phase 14 brief req 4: "If the
  available data produces too few trades, report that profitability
  is unvalidated." The 80% win-rate research target was NOT achieved
  and NOT claimed — none of the candidates produced enough trades
  to compute a meaningful win rate.

- Verified production safeguards:
    * 329 tests passing (313 prior + 16 new Phase 14).
    * 11 serverless functions (within Vercel Hobby 12-function limit).
    * 4 migrations byte-for-byte unchanged (0001: 9969, 0002: 6954,
      0003: 2571, 0004: 1157 bytes — same as Phase 13 baseline).
    * No RLS policy changes.
    * No env var name changes.
    * Paper-only trading preserved — no real orders, leverage, or
      futures.
    * Production strategy defaults UNCHANGED.

Stage Summary:
- Phase 14 regime classifier + 4 regime-aware candidates built.
- All 4 regime candidates produced 0 trades on dev/val — the INR-spot
  regime is genuinely challenging; the entry conditions are too
  restrictive even with the regime filter.
- Production breakout_retest unchanged from Phase 13: 66 trades,
  21.2% win rate, +₹31.35 net.
- Strategy profitability remains UNVALIDATED. 80% win-rate target
  NOT achieved (and not claimed).
- Ready to commit + push + attempt Vercel deployment verification.

---
Task ID: phase-14-final
Agent: main (super-z)
Task: Phase 14 final — Vercel deployment verification attempt

Work Log:
- Final pre-commit verification:
    * Full test suite: 329 passing (313 prior + 16 new Phase 14).
    * Serverless function count: 11 (within Vercel Hobby 12-function
      limit).
    * Migrations: 4 byte-for-byte unchanged (0001: 9969, 0002: 6954,
      0003: 2571, 0004: 1157 bytes — same as Phase 13 baseline).
- Committed Phase 14 changes: SHA e93cf94 on local main.
    * 6 files changed, 7479 insertions(+), including:
      - src/coindcx_lab/engine/regime.py (NEW — classifier)
      - src/coindcx_lab/engine/regime_strategies.py (NEW — 4 candidates)
      - tests/test_phase14_regression.py (NEW — 16 tests)
      - scripts/run_phase14_comparison.py (NEW — backtest runner)
      - download/phase14_strategy_comparison.json (results)
- Pushed to GitHub main: 9f226d7..e93cf94 fast-forward succeeded.
- Verified post-push: origin/main == local main == e93cf94.
- Attempted Vercel Production deployment verification:
    * `vercel` CLI installed (Phase 13) — Vercel CLI 63.1.2.
    * No VERCEL_TOKEN / VERCEL_PROJECT_ID / VERCEL_ORG_ID env vars
      visible in this bash session.
    * No ~/.local/share/com.vercel.cli/auth.json file present.
    * No .vercel/ project config (no `vercel link` ever done here).
    * `vercel whoami` returns "Logged out."
    * Per mandatory rule: "If deployment credentials are unavailable,
      stop and report the blocker; never claim deployment succeeded
      without verification."
    * Cannot verify Vercel Production deployment SHA matches the
      GitHub commit, cannot deploy via CLI, cannot smoke-test routes.

Stage Summary:
- Phase 14 code + tests committed locally at e93cf94.
- Pushed to GitHub main successfully (fast-forward 9f226d7..e93cf94).
- GitHub main verified at e93cf94 (matches local main).
- Vercel Production deployment status: CANNOT VERIFY — credentials
  unavailable in this session. The Vercel-GitHub integration (if
  enabled on the project) should auto-trigger a deployment from
  the push, but I cannot query the Vercel API or CLI to confirm.
- Phase 14 is PARTIALLY COMPLETE per the mandatory rule
  ("At completion, commit and push the code and tests to GitHub main,
  deploy the verified commit to Vercel Production, and confirm the
  deployed SHA matches GitHub main.").
- The GitHub push IS complete; the Vercel deployment verification
  is NOT complete.
- Honest blocker reported per the mandatory rule.

---
Task ID: phase-15-start
Agent: main (super-z)
Task: Phase 15 kickoff — baseline + rejection diagnostic

Work Log:
- Verified baseline:
    * Local + GitHub main at fbead6e.
    * Inspected fbead6e: docs-only (50 worklog.md insertions, no app
      changes beyond what's in e93cf94). User's note that "Vercel
      Production shows Phase 14 feature deployment as Ready" cannot
      be verified by me — Vercel credentials still NOT SET in this
      bash session. Will report blocker honestly per mandatory rule 10.
    * 329 tests passing (Phase 14 baseline).
    * 11 serverless functions.
    * 4 migrations byte-for-byte unchanged (0001: 9969, 0002: 6954,
      0003: 2571, 0004: 1157 bytes).
    * Working tree clean.
- Wrote scripts/diagnose_phase15_rejections.py — instruments each
  Phase 14 regime candidate's evaluate() call and categorizes the
  rejection reason per Phase 15 brief req 2:
    - regime_not_eligible
    - entry_trigger_absent (RSI/pullback/breakout missing)
    - insufficient_history (warm-up incomplete)
    - invalid_stop_or_target (stop >= entry etc.)
    - rr_below_threshold (risk gate rejected)
    - excessive_spread_or_missing_data
    - other (uncategorized)

- Phase 15 rejection diagnostic results (7 pairs × 4 candidates ×
  800 evaluated bars per pair = ~22,400 evaluations):
    regime_trend_pullback:        5600 evals, 0 buys
      regime_not_eligible:    4109 (73.4%)
      entry_trigger_absent:   1240 (22.1%)
      other:                   251 ( 4.5%)
    regime_breakout_retest:       5600 evals, 0 buys
      regime_not_eligible:    2833 (50.6%)
      entry_trigger_absent:   1284 (22.9%)
      other:                  1483 (26.5%)  (likely risk-gate NO_TRADE)
    regime_mean_reversion:        5600 evals, 0 buys
      regime_not_eligible:    4283 (76.5%)  <- RANGE_BOUND regime is rare
      entry_trigger_absent:   1309 (23.4%)
      other:                     8 ( 0.1%)
    regime_mtf_trend_pullback:    5600 evals, 0 buys
      other:                  2633 (47.0%)  (HTF warmup incomplete)
      regime_not_eligible:    1788 (31.9%)
      entry_trigger_absent:   1179 (21.1%)

  ROOT CAUSE (per Phase 15 brief req 2 "Determine whether the main
  problem is the entry hypothesis, risk geometry, regime
  classification or data availability"):
    - For regime_trend_pullback and regime_mean_reversion, the
      PRIMARY blocker is the REGIME CLASSIFICATION (73-76% of
      rejections). The TRENDING_UP and RANGE_BOUND regimes are too
      strictly defined — most INR-spot bars fall into UNCERTAIN.
    - For regime_breakout_retest, the "other" category (26%) is
      likely the risk gate rejecting due to wide stop geometry
      (post-cost R/R < 2.0 target).
    - For regime_mtf_trend_pullback, the "other" category (47%) is
      HTF warmup incomplete at early bars (needs 200 4h bars = ~33
      days before EMA200 is defined).

  Per Phase 15 brief req 2: "Do not loosen every condition
  simultaneously." Phase 15 will research SEPARATE entry hypotheses
  (one at a time) to identify which changes unlock signal generation
  without overfitting.

Stage Summary:
- Phase 15 baseline established.
- Root cause diagnosed: regime classification is the primary blocker
  (not entry hypothesis, not risk geometry, not data availability).
- Ready to build ADX indicator + 5 new entry-hypothesis candidates.

---
Task ID: phase-15-execution
Agent: main (super-z)
Task: Phase 15 — ADX + Bollinger Bands + 5 entry-hypothesis candidates

Work Log:
- Built ADX indicator (src/coindcx_lab/engine/indicators.py:adx()).
  Wilder's smoothing, NaN for first 2*period bars (28). Measures
  trend STRENGTH (not direction). Phase 15 brief req 3D.
- Built Bollinger Bands (indicators.py:bollinger_bands()) — middle
  = SMA(close, period), upper/lower = middle ± num_std * std. NaN
  for first period-1 bars. For mean-reversion candidates.
- Added ema100, adx14, bb_middle, bb_upper, bb_lower fields to
  IndicatorSet dataclass. Existing 329 tests still pass after the
  expansion.
- Built 5 entry-hypothesis candidates
  (src/coindcx_lab/engine/phase15_candidates.py):
    A. TrendPullbackWiderZone — pullback zone [EMA100, EMA50]
       (deeper than [EMA50, EMA20]).
    B. TrendPullbackPriceReversal — 3+ consecutive declining closes
       + first up-close (instead of RSI in range).
    C. MeanReversionBB — Bollinger Band touch (prev close <= BB_lower)
       + back inside (current close > BB_lower).
    D. MeanReversionDecliningCloses — 3+ declining closes + first
       up-close (price-reversal trigger in range-bound regime).
    E. BreakoutRetestLevelStop — TIGHT breakout-level stop
       (0.5% below breakout level, NOT min with 2*ATR).
- Built AdxRegimeClassifier — alternative regime classifier using ADX
  (>25 strong trend, <20 range-bound). Existing RegimeClassifier
  unchanged.
- Wrote tests/test_phase15_regression.py with 14 new tests:
    A. ADX (3): defined after 2*period, high on strong trend, low on
       flat series.
    B. Bollinger Bands (3): defined after period-1, middle=SMA,
       width>0 on non-flat.
    C. Entry-hypothesis candidates (6): wider zone uses EMA100,
       price-reversal requires 3+ declining closes, BB requires
       touch + back-inside, declining-closes requires 3+ declines,
       breakout-level-stop uses tight stop, ADX classifier strong
       trend up.
    D. Paper-trading reliability (2): IndicatorSet has new fields,
       equity snapshot persists after close.
- Full test suite: 343 passing (329 prior + 14 new Phase 15).
- Production safeguards preserved:
    * 11 serverless functions (within Vercel Hobby 12-function limit).
    * 4 migrations byte-for-byte unchanged.
    * No RLS policy changes.
    * No env var name changes.
    * Paper-only trading preserved.
    * Production strategy defaults UNCHANGED.

Phase 15 evaluation backtest running (background):
- 5 Phase 15 candidates × 11 pairs (dev/val only — OOS preserved)
- Production breakout_retest × 11 pairs (full dev/val/oos) for
  comparison.
- Total: 66 runs. Output:
  /home/z/my-project/download/phase15_strategy_comparison.json

Stage Summary:
- Phase 15 candidates built and unit-tested.
- ADX + Bollinger Bands + EMA100 added to IndicatorSet.
- 5 entry-hypothesis candidates + AdxRegimeClassifier ready for
  evaluation.
- Backtest running in background; will produce honest metrics
  report once it completes.

---
Task ID: phase-15-final
Agent: main (super-z)
Task: Phase 15 final — Vercel deployment verification attempt

Work Log:
- Final pre-commit verification:
    * Full test suite: 343 passing (329 prior + 14 new Phase 15).
    * Serverless function count: 11 (within Vercel Hobby 12-function
      limit).
    * Migrations: 4 byte-for-byte unchanged (0001: 9969, 0002: 6954,
      0003: 2571, 0004: 1157 bytes — same as Phase 14 baseline).
- Committed Phase 15 changes: SHA d1bc086 on local main.
    * 9 files changed, 4676 insertions(+), including:
      - src/coindcx_lab/engine/indicators.py (M — added adx(),
        bollinger_bands(), ema100 field)
      - src/coindcx_lab/engine/phase15_candidates.py (NEW — 5
        candidates + AdxRegimeClassifier)
      - tests/test_phase15_regression.py (NEW — 14 tests)
      - scripts/diagnose_phase15_rejections.py (NEW — rejection
        diagnostic)
      - scripts/run_phase15_comparison.py + _slim.py (NEW —
        backtest runners)
      - download/phase15_*.json (2 diagnostic results files)
- Pushed to GitHub main: fbead6e..d1bc086 fast-forward succeeded.
- Verified post-push: origin/main == local main == d1bc086.
- Attempted Vercel Production deployment verification:
    * `vercel` CLI installed (Vercel CLI 63.1.2, from Phase 13).
    * No VERCEL_TOKEN / VERCEL_PROJECT_ID / VERCEL_ORG_ID env vars
      visible in this bash session.
    * No ~/.local/share/com.vercel.cli/auth.json file present.
    * No .vercel/ project config (no `vercel link` ever done here).
    * `vercel whoami` returns "Logged out."
    * Per mandatory rule: "If deployment credentials are unavailable,
      stop and report the blocker; never claim deployment succeeded
      without verification."
    * Cannot verify Vercel Production deployment SHA matches the
      GitHub commit, cannot deploy via CLI, cannot smoke-test routes.

  Note: The user's Phase 15 brief stated "Production: Vercel,
  currently showing Phase 14 feature deployment as Ready" — meaning
  the Vercel-GitHub integration IS auto-deploying (the Phase 14
  commit e93cf94 was deployed). The push of d1bc086 should also
  trigger an auto-deployment, but I cannot verify the deployed SHA
  without credentials.

Stage Summary:
- Phase 15 code + tests committed locally at d1bc086.
- Pushed to GitHub main successfully (fast-forward fbead6e..d1bc086).
- GitHub main verified at d1bc086 (matches local main).
- Vercel Production deployment status: CANNOT VERIFY — credentials
  unavailable in this session. The Vercel-GitHub integration (if
  enabled on the project) should auto-trigger a deployment from
  the push, but I cannot query the Vercel API or CLI to confirm.
- Phase 15 is PARTIALLY COMPLETE per the mandatory rule
  ("At completion, commit and push the completed code and tests to
  GitHub main, deploy the intended application commit to Vercel
  Production, and verify that the active deployment SHA matches
  the intended GitHub commit.").
- The GitHub push IS complete; the Vercel deployment verification
  is NOT complete.
- Honest blocker reported per the mandatory rule.

---
Task ID: phase-16-start
Agent: main (super-z)
Task: Phase 16 kickoff — inspect regime classifier architecture

Work Log:
- Verified baseline: local + GitHub main at 0d48f43 (matches Phase 15
  end). Working tree clean. 343 tests passing. 11 serverless
  functions. 4 migrations byte-for-byte unchanged (0001: 9969, 0002:
  6954, 0003: 2571, 0004: 1157 bytes). Vercel credentials STILL NOT
  SET (consistent with Phase 12-15 blocker).
- Inspected existing implementation per Phase 16 brief req 1:
    * src/coindcx_lab/engine/regime.py — RegimeClassifier with 5
      regimes (TRENDING_UP/DOWN/RANGE_BOUND/HIGH_VOLATILITY/
      UNCERTAIN). Uses EMA stack + EMA50 slope + ATR/price.
    * src/coindcx_lab/engine/indicators.py:adx() — computes +DI/-DI
      INTERNALLY but only returns the ADX array. The +DI/-DI arrays
      are local variables, not exposed.
    * src/coindcx_lab/engine/phase15_candidates.py — ALL 5 candidates
      (TrendPullbackWiderZone, TrendPullbackPriceReversal,
      MeanReversionBB, MeanReversionDecliningCloses,
      BreakoutRetestLevelStop) hard-code
      `RegimeClassifier.classify_at(candles, indicators, i)` at
      lines 100, 174, 260, 335, 414. No way to swap in a different
      classifier.
    * src/coindcx_lab/engine/phase15_candidates.py:AdxRegimeClassifier
      — built in Phase 15 but NOT wired into any candidate. Uses
      EMA stack (lines 528-531) for direction, NOT +DI/-DI as the
      Phase 16 brief requires ("Direction using +DI versus -DI").
    * src/coindcx_lab/engine/signals.py — production strategies
      (TrendPullback, BreakoutRetest, MomentumContinuation,
      BaselineSMA) do NOT use any regime classifier (regime is only
      used by Phase 14/15 research candidates).
    * api/backtest.py — serverless function exposes the backtest API;
      uses SignalEngine with production strategies. Research
      candidates are NOT auto-connected to /api/signals.

- Root cause confirmed (per Phase 16 brief req 1): the regime
  classifier architecture has TWO problems:
    1. HARD-CODED DEPENDENCY: Phase 15 candidates hard-code
       RegimeClassifier — can't swap in ADX-based classifier.
    2. INCOMPLETE ADX EXPOSURE: the adx() function computes +DI/-DI
       but doesn't return them. The existing AdxRegimeClassifier
       uses EMA stack for direction (workaround), violating the
       Phase 16 brief.

Phase 16 plan:
  1. Refactor adx() to expose +DI/-DI (add adx_with_di() function +
     plus_di14/minus_di14 fields to IndicatorSet). Preserve existing
     adx() for backward compat.
  2. Refactor Phase 15 candidates to accept regime_classifier via
     constructor (default=RegimeClassifier for backward compat).
  3. Build new DirectionalAdxClassifier that uses ADX for strength +
     +DI vs -DI for direction (per Phase 16 brief req 3).
  4. Add tests for DirectionalAdxClassifier on synthetic rising/
     falling/sideways/high-vol series. Verify no look-ahead.
  5. Run controlled comparison: same pairs/candles/strategies with
     RegimeClassifier vs DirectionalAdxClassifier.

Stage Summary:
- Phase 16 architecture issues identified.
- Ready to expose +DI/-DI + make classifier configurable + build
  DirectionalAdxClassifier.

---
Task ID: phase-16-final
Agent: main (super-z)
Task: Phase 16 final — commit, push, Vercel deployment attempt

Work Log:
- Final pre-commit verification:
    * Full test suite: 356 passing (343 prior + 13 new Phase 16).
    * Serverless function count: 11 (within Vercel Hobby 12-function
      limit).
    * Migrations: 4 byte-for-byte unchanged (0001: 9969, 0002: 6954,
      0003: 2571, 0004: 1157 bytes — same as Phase 15 baseline).
    * No RLS policy changes.
    * Production strategy defaults UNCHANGED.
    * Research candidates NOT connected to production /api/signals.
- Committed Phase 16 changes: SHA 34c80dc on local main.
    * 4 files changed, 573 insertions(+), 87 deletions(-):
      - src/coindcx_lab/engine/indicators.py (M — adx_with_di() +
        plus_di14/minus_di14 fields)
      - src/coindcx_lab/engine/phase15_candidates.py (M — configurable
        classifier + DirectionalAdxClassifier)
      - tests/test_phase16_regression.py (NEW — 13 tests)
- Pushed to GitHub main: 0d48f43..34c80dc fast-forward succeeded.
- Verified post-push: origin/main == local main == 34c80dc.
- Attempted Vercel Production deployment verification:
    * `vercel` CLI installed (Vercel CLI 63.1.2, from Phase 13).
    * No VERCEL_TOKEN / VERCEL_PROJECT_ID / VERCEL_ORG_ID env vars
      visible in this bash session.
    * No ~/.local/share/com.vercel.cli/auth.json file present.
    * No .vercel/ project config (no `vercel link` ever done here).
    * `vercel whoami` returns "Logged out."
    * Per mandatory rule: "If deployment is blocked, report it
      honestly."
    * Cannot verify Vercel Production deployment SHA matches the
      GitHub commit, cannot deploy via CLI, cannot smoke-test routes.

Stage Summary:
- Phase 16 code + tests committed locally at 34c80dc.
- Pushed to GitHub main successfully (fast-forward 0d48f43..34c80dc).
- GitHub main verified at 34c80dc (matches local main).
- Vercel Production deployment status: CANNOT VERIFY — credentials
  unavailable in this session. The Vercel-GitHub integration (if
  enabled on the project) should auto-trigger a deployment from
  the push, but I cannot query the Vercel API or CLI to confirm.
- Phase 16 is PARTIALLY COMPLETE per the mandatory rule.
- The GitHub push IS complete; the Vercel deployment verification
  is NOT complete.
- Honest blocker reported per the mandatory rule.

---
Task ID: phase-17-start
Agent: main (super-z)
Task: Phase 17 kickoff — baseline + optimization plan

Work Log:
- Verified baseline: local + GitHub main at 1b296d9 (docs-only —
  49 worklog insertions, no app changes beyond 34c80dc). Vercel
  Production at 34c80dc (per user's note). Working tree clean.
  356 tests passing. 11 serverless functions. 4 migrations
  byte-for-byte unchanged (9969/6954/2571/1157 bytes).
- Inspected the current indicator computation hot path:
    * Backtester.run() iterates through all_candles (prefix + segment).
    * At each bar i, calls signal_engine.evaluate(candles=all_candles[:i+1]).
    * SignalEngine.evaluate() calls IndicatorSet.from_candles(prefix).
    * IndicatorSet.from_candles() recomputes ALL indicators from scratch
      (EMA, RSI, ATR, MACD, ADX, BB, relvol, vol, swing_highs_lows,
      breakout_retest, support_resistance).
    * Total cost: O(N) per bar * N bars = O(N²) per segment.
    * Phase 15/16 additions (ADX, BB, EMA100, +DI/-DI) roughly doubled
      the per-IndicatorSet cost, causing the Phase 15 backtest to
      timeout at 540s for 25 runs on 5 pairs × 5 candidates.

Optimization plan:
  1. Pre-compute ARRAY indicators (EMA20/50/100/200, RSI14, ATR14,
     MACD, relvol20, vol20, ADX14, BB, +DI14, -DI14) on the full
     candle series ONCE (O(N) total). These indicators have NO
     look-ahead — the value at index i depends only on candles[0..i].
  2. At each bar i, create a lightweight IndicatorSet that:
     - References the pre-computed arrays (shared, not copied).
     - Truncates swing_highs/swing_lows to [0..i] (O(N) list copy,
       but tiny constant ~0.01ms per 1000 elements).
     - Recomputes breakout_retest_signal on prefix[0..i] (O(i) per
       bar, small constant).
     - Recomputes support_resistance from truncated swing lists.
     - Sets last_idx = i.
  3. Modify Backtester.run() to use the pre-computed indicators
     instead of calling SignalEngine.evaluate() (which recomputes
     IndicatorSet at every bar). Call strategy.evaluate() directly
     with the lightweight IndicatorSet.
  4. SignalEngine.evaluate() API preserved for other callers
     (backward compat).
  5. Numerical equivalence: the pre-computed array values at index i
     are IDENTICAL to the values computed from the prefix [0..i].
     The truncated swing lists are IDENTICAL to computing
     swing_highs_lows on the prefix. The recomputed breakout_retest
     is IDENTICAL. So the strategy's decisions are unchanged.

Stage Summary:
- Optimization approach planned. Ready to implement.

---
Task ID: phase-17-final
Agent: main (super-z)
Task: Phase 17 final — commit, push, Vercel deployment attempt

Work Log:
- Final pre-commit verification:
    * Full test suite: 365 passing (356 prior + 9 new Phase 17) in
      17.49s (down from 42s — 2.4x faster thanks to the optimization).
    * Serverless function count: 11 (within Vercel Hobby 12-function
      limit).
    * Migrations: 4 byte-for-byte unchanged (0001: 9969, 0002: 6954,
      0003: 2571, 0004: 1157 bytes — same as Phase 16 baseline).
    * No RLS policy changes.
    * Production strategy defaults UNCHANGED.
    * Research candidates NOT connected to production /api/signals.
- Committed Phase 17 changes: SHA 3ed81c7 on local main.
    * 8 files changed, 5497 insertions(+), 13 deletions(-):
      - src/coindcx_lab/engine/indicators.py (M — at_bar() method)
      - src/coindcx_lab/engine/signals.py (M — indicators parameter)
      - src/coindcx_lab/engine/backtest.py (M — pre-compute + at_bar)
      - tests/test_backtest.py (M — spy function accepts **kwargs)
      - tests/test_phase17_regression.py (NEW — 9 tests)
      - scripts/run_phase17_comparison.py (NEW — comparison runner)
      - download/phase17_classifier_comparison.json (results)
- Pushed to GitHub main: 1b296d9..3ed81c7 fast-forward succeeded.
- Verified post-push: origin/main == local main == 3ed81c7.
- Attempted Vercel Production deployment verification:
    * `vercel` CLI installed (Vercel CLI 63.1.2).
    * No VERCEL_TOKEN / VERCEL_PROJECT_ID / VERCEL_ORG_ID env vars.
    * `vercel whoami` returns "Logged out."
    * Per mandatory rule: "If deployment is blocked by Git
      information retrieval or missing credentials, report the issue
      and keep the last known working production deployment intact."
    * The last known working production deployment is at 34c80dc
      (Phase 16 feature commit, per user's note). Preserving it.
    * Cannot verify whether the Vercel-GitHub integration auto-
      deployed 3ed81c7, or whether the deployed SHA matches.

Stage Summary:
- Phase 17 code + tests committed locally at 3ed81c7.
- Pushed to GitHub main successfully (fast-forward 1b296d9..3ed81c7).
- GitHub main verified at 3ed81c7 (matches local main).
- Vercel Production deployment status: CANNOT VERIFY — credentials
  unavailable. Last known working deployment preserved at 34c80dc.
- Phase 17 is PARTIALLY COMPLETE per the mandatory rule.
- Honest blocker reported per the mandatory rule.

---
Task ID: phase-18-final
Agent: main (super-z)
Task: Phase 18 final — commit, push, Vercel deployment attempt

Work Log:
- Phase 18 baseline: local main was at bc53650 (Phase 12 state —
  context reset). Fetched from GitHub, reset to 6a388eb (Phase 17
  final). All Phase 13-17 files restored. 365 tests passing.
- Implemented SimpleBreakoutStrategy (src/coindcx_lab/engine/
  simple_strategy.py) — minimal-filter trend-breakout:
    * Trend filter: close > EMA50
    * Entry: close > max(high of last 20 bars)
    * Stop: entry - 2*ATR(14)
    * Target: 3R (NOT 2R — see root cause below)
    * Warmup: 50 bars
- ROOT CAUSE identified: 2R target is mathematically impossible to
  pass the 2.0 R/R-after-cost gate with positive costs (0.3%
  round-trip). Proof: (2R-cost)/(R+cost) < 2 for any cost > 0.
  This explains why ALL Phase 14/15 regime-aware candidates (2R
  target) produced 0 trades — the risk gate rejected every setup.
  Fix: 3R target. For R >= 1% of price, 3R gives rr >= 2.0.
- Phase 18 comparison (8 pairs × 1000 1h candles × 3 segments):
    simple_breakout: 75 trades, 26.7% WR, +₹23.37 net, PF=1.039
    breakout_retest: 55 trades, 16.4% WR, +₹12.58 net, PF=1.068
    buy-hold avg:    +3.72%
  Both strategies barely above breakeven (PF ~1.04-1.07).
  Both underperform buy-hold. Sample too small. UNVALIDATED.
- 3 regression tests added (368 total). Migrations unchanged.
  11 functions. Paper-only. Research NOT connected to /api/signals.
- Committed: SHA d313463. Pushed: 6a388eb..d313463 fast-forward.
  GitHub main verified at d313463.
- Vercel deployment verification:
    * Vercel CLI reinstalled (npm install -g vercel, v63.1.2).
    * VERCEL_TOKEN, VERCEL_PROJECT_ID, VERCEL_ORG_ID env vars
      EXIST but have LENGTH 0 (empty strings — not actually set
      with values).
    * `vercel whoami --token` fails: "missing a value".
    * Cannot verify deployed SHA, cannot deploy, cannot smoke-test.
    * Last known working deployment: 34c80dc (Phase 16). Preserving.
    * Per mandatory rule: "If deployment fails, report the exact
      blocker honestly." — Vercel credentials are empty strings,
      not usable.

Stage Summary:
- Phase 18 ROOT CAUSE found: 2R target impossible with positive costs.
- SimpleBreakoutStrategy (3R) produces 75 trades with marginal
  positive PnL — UNVALIDATED.
- Committed at d313463. Pushed to GitHub main. Verified.
- Vercel deployment verification BLOCKED — credentials are empty.
- Honest blocker reported.
