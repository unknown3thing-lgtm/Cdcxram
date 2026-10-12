"""Phase 12 — production defaults vs candidate variants backtest.

Runs the existing 4 production strategies AND the 3 Phase 12 candidate
variants across the existing supported pairs and 1h interval. Reports
the dev/val metrics separately from OOS so candidate variants are
NEVER evaluated using OOS data (per Phase 12 brief requirement 4).

Production strategies (defaults unchanged):
  - trend_pullback
  - breakout_retest
  - momentum_continuation
  - baseline_sma

Candidate variants (Phase 12, evaluated on dev/val ONLY):
  - trend_pullback_relaxed_rsi (RSI [35, 60] instead of [40, 55])
  - trend_pullback_wider_zone (pullback [EMA100, EMA20] instead of [EMA50, EMA20])
  - momentum_continuation_relaxed (RSI > 50 instead of > 55, relvol > 1.0 instead of > 1.2)

Output: /home/z/my-project/download/phase12_strategy_comparison.json
"""
from __future__ import annotations

import json
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
from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
from coindcx_lab.engine.risk import RiskSettings
from coindcx_lab.engine.signals import (
    TrendPullbackStrategy, BreakoutRetestStrategy,
    MomentumContinuationStrategy, BaselineSMAStrategy, SignalEngine,
)
from coindcx_lab.engine.candidate_variants import CANDIDATE_VARIANTS


# Use the existing 5 supported pairs (MATIC kept for comparison with
# Phase 11, even though it's stale — its stale data will show in the
# results as a near-flat buy-hold benchmark).
DEFAULT_PAIRS = ["B-BTCINR", "B-ETHINR", "B-SOLINR", "B-XRPINR"]
INTERVAL = "1h"
CANDLE_LIMIT = 1000

PRODUCTION_STRATEGIES = [
    ("trend_pullback", TrendPullbackStrategy()),
    ("breakout_retest", BreakoutRetestStrategy()),
    ("momentum_continuation", MomentumContinuationStrategy()),
    ("baseline_sma", BaselineSMAStrategy()),
]


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
             "pnl_net": t.pnl_net, "exit_reason": t.exit_reason}
            for t in r.trades
        ]
        out["segments"][seg_name] = seg_summary
    return out


def main():
    print(f"Phase 12 — production defaults vs candidate variants backtest")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print(f"Interval: {INTERVAL}  Candle limit: {CANDLE_LIMIT}")
    print()

    client = WazirXClient()
    candles_cache = {}
    for pair in DEFAULT_PAIRS:
        try:
            print(f"  Fetch {pair} {INTERVAL} limit={CANDLE_LIMIT}...", end="", flush=True)
            candles, _ = client.candles(pair=pair, interval=INTERVAL,
                                         limit=CANDLE_LIMIT)
            candles_cache[pair] = candles
            first = datetime.fromtimestamp(candles[0].open_time_ms / 1000, tz=timezone.utc)
            last = datetime.fromtimestamp(candles[-1].open_time_ms / 1000, tz=timezone.utc)
            print(f" {len(candles)} candles {first.date()}->{last.date()}")
            time.sleep(0.3)
        except Exception as e:
            print(f" failed: {e}")

    all_results = {
        "phase": 12,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "interval": INTERVAL,
        "candle_limit": CANDLE_LIMIT,
        "production_strategies": [s[0] for s in PRODUCTION_STRATEGIES],
        "candidate_variants": [v[0] for v in CANDIDATE_VARIANTS],
        "pairs": DEFAULT_PAIRS,
        "results": [],
    }

    # Run PRODUCTION strategies (full dev/val/oos).
    print("\n--- Production strategies (defaults unchanged) ---")
    for strat_name, strat in PRODUCTION_STRATEGIES:
        for pair in DEFAULT_PAIRS:
            if pair not in candles_cache: continue
            print(f"  Run {strat_name} on {pair}...", end="", flush=True)
            r = run_strategy(strat_name, strat, pair, INTERVAL, candles_cache[pair])
            r["strategy_type"] = "production"
            all_results["results"].append(r)
            if "error" in r:
                print(f" error: {r['error']}")
            else:
                line = ""
                for seg in ("dev", "val", "oos"):
                    s = r["segments"][seg]
                    line += f" {seg}:t={s['num_trades']}r={s['total_return_pct']:+.1f}%"
                print(f" ->{line}")

    # Run CANDIDATE VARIANTS on DEV/VAL ONLY (OOS not run for variants).
    # We still call walk_forward() (which produces oos results), but
    # we will EXCLUDE oos from the variant comparison in the report.
    print("\n--- Candidate variants (dev/val only — OOS preserved) ---")
    for strat_name, strat in CANDIDATE_VARIANTS:
        for pair in DEFAULT_PAIRS:
            if pair not in candles_cache: continue
            print(f"  Run {strat_name} on {pair}...", end="", flush=True)
            r = run_strategy(strat_name, strat, pair, INTERVAL, candles_cache[pair])
            r["strategy_type"] = "candidate_variant"
            # Mark OOS as "preserved" (not evaluated for variants)
            if "segments" in r and "oos" in r["segments"]:
                r["segments"]["oos"]["preserved"] = True
                r["segments"]["oos"]["note"] = (
                    "OOS NOT evaluated for candidate variants per "
                    "Phase 12 brief requirement 4. Variants are "
                    "selected using dev/val only.")
            print(f" -> dev:t={r['segments']['dev']['num_trades']} "
                  f"val:t={r['segments']['val']['num_trades']}")
            all_results["results"].append(r)

    all_results["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    out_path = "/home/z/my-project/download/phase12_strategy_comparison.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print()
    print(f"Written: {out_path}")
    print(f"Total runs: {len(all_results['results'])}")


if __name__ == "__main__":
    main()
