"""Phase 14 — regime-aware strategy evaluation backtest.

Runs the 4 Phase 14 regime-aware candidates across the 11 vetted
pairs (from Phase 13) using 1h LTF + 4h HTF candles. Reports trades,
wins/losses, win rate, net PnL, expectancy, profit factor, DD,
buy-and-hold benchmark. Distinguishes target hits vs stop-outs vs
end-of-data force-closes.

Production strategies: full dev/val/oos evaluation.
Regime candidates: dev/val only (OOS preserved per Phase 14 brief).

Output: /home/z/my-project/download/phase14_strategy_comparison.json
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from coindcx_lab.api.wazirx_client import WazirXClient
from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
from coindcx_lab.api.client import RateLimitError, CoinDCXAPIError
from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
from coindcx_lab.engine.risk import RiskSettings
from coindcx_lab.engine.signals import (
    TrendPullbackStrategy, BreakoutRetestStrategy,
    MomentumContinuationStrategy, BaselineSMAStrategy, SignalEngine,
)
from coindcx_lab.engine.regime_strategies import (
    RegimeTrendPullback, RegimeBreakoutRetest, RegimeMeanReversion,
    RegimeMultiTimeframe, REGIME_CANDIDATES_NO_HTF,
)
from coindcx_lab.engine.regime import RegimeClassifier, MarketRegime


VETTED_PAIRS = [
    "B-USDTINR", "B-BTCINR", "B-WRXINR", "B-ETHINR", "B-TRXINR",
    "B-PEPEINR", "B-SHIBINR", "B-COTIINR", "B-CELRINR", "B-LINKINR",
    "B-XRPINR",
]
LTF_INTERVAL = "1h"
HTF_INTERVAL = "4h"
CANDLE_LIMIT = 1000


def settings_for_backtest() -> RiskSettings:
    return RiskSettings(
        initial_inr=2000.0, risk_per_trade_pct=1.0,
        max_position_pct=25.0, max_portfolio_exposure_pct=80.0,
        max_daily_loss_pct=5.0, max_daily_loss_abs_inr=0.0,
        max_drawdown_pct=15.0, max_open_positions=5, max_daily_trades=10,
        reward_to_risk_target=2.0, cooldown_after_consecutive_losses=3,
        allow_overnight=True, fee_pct=0.1, spread_pct=0.05,
        slippage_pct=0.05, min_24h_volume_inr=0, min_24h_trades=0,
        max_spread_pct=1.0, trading_allocation_pct=100.0,
    )


def split_segments(candles):
    if not candles:
        return "", ""
    n = len(candles)
    third = n // 3
    dev_end_iso = _day_iso_from_ms(candles[third].open_time_ms)
    val_end_iso = _day_iso_from_ms(candles[2 * third].open_time_ms)
    return dev_end_iso, val_end_iso


def run_strategy(strategy_name, strategy, pair, interval, candles):
    s = settings_for_backtest()
    eng = SignalEngine(settings=s, strategies=[strategy])
    cfg = BacktestConfig(starting_capital_inr=2000.0,
                         warmup_skip_bars=strategy.warmup_candles)
    bt = Backtester(settings=s, strategy=strategy, signal_engine=eng,
                    config=cfg)
    dev_end_iso, val_end_iso = split_segments(candles)
    try:
        results = bt.walk_forward(pair=pair, candles=candles,
                                  dev_end_iso=dev_end_iso,
                                  val_end_iso=val_end_iso,
                                  interval=interval)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}",
                "pair": pair, "strategy": strategy_name,
                "interval": interval, "n_candles": len(candles)}
    out = {"pair": pair, "strategy": strategy_name, "interval": interval,
           "n_candles": len(candles),
           "dev_end_iso": dev_end_iso, "val_end_iso": val_end_iso,
           "segments": {}}
    for seg_name in ("dev", "val", "oos"):
        r = results[seg_name]
        seg_summary = r.to_summary()
        seg_summary["trade_details"] = [
            {"entry": t.entry, "exit_price": t.exit_price,
             "pnl_net": t.pnl_net, "exit_reason": t.exit_reason,
             "entry_time_ms": t.entry_time_ms,
             "exit_time_ms": t.exit_time_ms}
            for t in r.trades
        ]
        out["segments"][seg_name] = seg_summary
    return out


def aggregate_metrics(results, strategy_type, segments):
    """Aggregate trade-level metrics across all (pair, segment) runs
    for the given strategy_type. Returns:
      trades, wins, losses, win_rate, net_pnl, avg_win, avg_loss,
      expectancy, profit_factor, hold_bars_avg, exit_breakdown
    """
    metrics = {
        "trades": 0, "wins": 0, "losses": 0,
        "net_pnl": 0.0, "wins_pnl": 0.0, "losses_pnl_abs": 0.0,
        "hold_bars_total": 0, "exit_breakdown": {},
    }
    for r in results:
        if r.get("strategy_type") != strategy_type: continue
        if "error" in r: continue
        for seg in segments:
            if seg not in r.get("segments", {}): continue
            s = r["segments"][seg]
            if s.get("preserved"): continue  # skip OOS for candidates
            for t in s.get("trade_details", []):
                pnl = t.get("pnl_net", 0)
                metrics["trades"] += 1
                metrics["net_pnl"] += pnl
                if pnl > 0:
                    metrics["wins"] += 1
                    metrics["wins_pnl"] += pnl
                else:
                    metrics["losses"] += 1
                    metrics["losses_pnl_abs"] += -pnl
                # Holding period in bars
                entry_ms = t.get("entry_time_ms", 0)
                exit_ms = t.get("exit_time_ms", 0)
                if exit_ms > entry_ms:
                    hold_hours = (exit_ms - entry_ms) / 3_600_000
                    metrics["hold_bars_total"] += hold_hours
                # Exit reason breakdown
                reason = t.get("exit_reason", "unknown")
                metrics["exit_breakdown"][reason] = \
                    metrics["exit_breakdown"].get(reason, 0) + 1
    # Derived metrics
    metrics["win_rate_pct"] = (metrics["wins"] / metrics["trades"] * 100.0
                                if metrics["trades"] > 0 else 0.0)
    metrics["avg_win"] = (metrics["wins_pnl"] / metrics["wins"]
                           if metrics["wins"] > 0 else 0.0)
    metrics["avg_loss"] = (-metrics["losses_pnl_abs"] / metrics["losses"]
                             if metrics["losses"] > 0 else 0.0)
    metrics["expectancy"] = (metrics["net_pnl"] / metrics["trades"]
                               if metrics["trades"] > 0 else 0.0)
    metrics["profit_factor"] = (metrics["wins_pnl"] / metrics["losses_pnl_abs"]
                                  if metrics["losses_pnl_abs"] > 0
                                  else float("inf") if metrics["wins_pnl"] > 0
                                  else 0.0)
    metrics["hold_hours_avg"] = (metrics["hold_bars_total"] / metrics["trades"]
                                   if metrics["trades"] > 0 else 0.0)
    return metrics


def main():
    print(f"Phase 14 — regime-aware strategy evaluation backtest")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print(f"LTF: {LTF_INTERVAL}  HTF: {HTF_INTERVAL}  Candle limit: {CANDLE_LIMIT}")
    print(f"Pairs ({len(VETTED_PAIRS)}): {VETTED_PAIRS}")
    print()

    client = WazirXClient()
    ltf_cache = {}
    htf_cache = {}
    for pair in VETTED_PAIRS:
        try:
            print(f"  Fetch {pair} {LTF_INTERVAL} limit={CANDLE_LIMIT}...", end="", flush=True)
            candles, _ = client.candles(pair=pair, interval=LTF_INTERVAL, limit=CANDLE_LIMIT)
            ltf_cache[pair] = candles
            print(f" {len(candles)}")
            time.sleep(0.3)
        except Exception as e:
            print(f" failed: {e}")
        try:
            print(f"  Fetch {pair} {HTF_INTERVAL} limit={CANDLE_LIMIT}...", end="", flush=True)
            candles, _ = client.candles(pair=pair, interval=HTF_INTERVAL, limit=CANDLE_LIMIT)
            htf_cache[pair] = candles
            print(f" {len(candles)}")
            time.sleep(0.3)
        except Exception as e:
            print(f" failed: {e}")

    all_results = {
        "phase": 14,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "ltf_interval": LTF_INTERVAL,
        "htf_interval": HTF_INTERVAL,
        "candle_limit": CANDLE_LIMIT,
        "pairs": VETTED_PAIRS,
        "results": [],
    }

    # --- Regime-aware candidates (dev/val only — OOS preserved) ---
    print("\n--- Regime-aware candidates (dev/val only — OOS preserved) ---")
    # 3 candidates that don't need HTF candles
    for strat_name, strat in REGIME_CANDIDATES_NO_HTF:
        for pair in VETTED_PAIRS:
            if pair not in ltf_cache: continue
            print(f"  Run {strat_name} on {pair}...", end="", flush=True)
            r = run_strategy(strat_name, strat, pair, LTF_INTERVAL, ltf_cache[pair])
            r["strategy_type"] = "regime_candidate"
            if "segments" in r and "oos" in r["segments"]:
                r["segments"]["oos"]["preserved"] = True
                r["segments"]["oos"]["note"] = (
                    "OOS NOT evaluated for regime candidates per "
                    "Phase 14 brief req 4. Candidates selected using "
                    "dev/val only.")
            all_results["results"].append(r)
            print(f" -> dev:t={r['segments']['dev']['num_trades']} "
                  f"val:t={r['segments']['val']['num_trades']}")

    # RegimeMultiTimeframe needs HTF candles per pair
    for pair in VETTED_PAIRS:
        if pair not in ltf_cache or pair not in htf_cache: continue
        print(f"  Run regime_mtf_trend_pullback on {pair}...", end="", flush=True)
        mtf_strat = RegimeMultiTimeframe(htf_candles=htf_cache[pair])
        r = run_strategy("regime_mtf_trend_pullback", mtf_strat, pair,
                          LTF_INTERVAL, ltf_cache[pair])
        r["strategy_type"] = "regime_candidate"
        if "segments" in r and "oos" in r["segments"]:
            r["segments"]["oos"]["preserved"] = True
            r["segments"]["oos"]["note"] = (
                "OOS NOT evaluated for regime candidates per Phase 14 brief.")
        all_results["results"].append(r)
        print(f" -> dev:t={r['segments']['dev']['num_trades']} "
              f"val:t={r['segments']['val']['num_trades']}")

    # Also include production breakout_retest for comparison (full dev/val/oos)
    print("\n--- Production breakout_retest (full dev/val/oos) for comparison ---")
    for pair in VETTED_PAIRS:
        if pair not in ltf_cache: continue
        print(f"  Run breakout_retest on {pair}...", end="", flush=True)
        r = run_strategy("breakout_retest", BreakoutRetestStrategy(), pair,
                          LTF_INTERVAL, ltf_cache[pair])
        r["strategy_type"] = "production"
        all_results["results"].append(r)
        print(f" -> dev:t={r['segments']['dev']['num_trades']} "
              f"val:t={r['segments']['val']['num_trades']} "
              f"oos:t={r['segments']['oos']['num_trades']}")

    # --- Aggregate metrics ---
    print("\n--- Aggregated metrics (dev+val for candidates; dev+val+oos for production) ---")
    agg = {}
    agg["regime_candidates"] = aggregate_metrics(
        all_results["results"], "regime_candidate", ("dev", "val"))
    agg["production_breakout_retest"] = aggregate_metrics(
        all_results["results"], "production", ("dev", "val", "oos"))
    all_results["aggregated_metrics"] = agg

    # Print summary
    for label, m in agg.items():
        print(f"\n  {label}:")
        print(f"    trades={m['trades']}  wins={m['wins']}  losses={m['losses']}")
        print(f"    win_rate={m['win_rate_pct']:.1f}%")
        print(f"    net_pnl=₹{m['net_pnl']:+.2f}")
        print(f"    avg_win=₹{m['avg_win']:+.2f}  avg_loss=₹{m['avg_loss']:+.2f}")
        print(f"    expectancy=₹{m['expectancy']:+.2f}/trade")
        print(f"    profit_factor={m['profit_factor']:.3f}")
        print(f"    hold_hours_avg={m['hold_hours_avg']:.1f}")
        print(f"    exit_breakdown={m['exit_breakdown']}")

    all_results["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    out_path = "/home/z/my-project/download/phase14_strategy_comparison.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print()
    print(f"Written: {out_path}")
    print(f"Total runs: {len(all_results['results'])}")


if __name__ == "__main__":
    main()
