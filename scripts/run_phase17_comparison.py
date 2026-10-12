"""Phase 17 — controlled classifier comparison.

Runs the 5 Phase 15 candidates with TWO classifiers:
  A. RegimeClassifier (existing, EMA-stack-based)
  B. DirectionalAdxClassifier (Phase 16, ADX + +DI/-DI)

Uses identical candles, pairs, strategies, fees, slippage. Reports
regime counts, rejection reasons, trades, returns for each classifier.

Output: /home/z/my-project/download/phase17_classifier_comparison.json
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
from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
from coindcx_lab.engine.risk import RiskSettings
from coindcx_lab.engine.signals import SignalEngine
from coindcx_lab.engine.indicators import IndicatorSet
from coindcx_lab.engine.regime import RegimeClassifier, MarketRegime, RegimeSnapshot
from coindcx_lab.engine.phase15_candidates import (
    PHASE15_CANDIDATES, DirectionalAdxClassifier,
    TrendPullbackWiderZone, TrendPullbackPriceReversal,
    MeanReversionBB, MeanReversionDecliningCloses,
    BreakoutRetestLevelStop,
)


SLIM_PAIRS = ["B-BTCINR", "B-ETHINR", "B-WRXINR", "B-TRXINR", "B-PEPEINR"]
INTERVAL = "1h"
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
    return (_day_iso_from_ms(candles[third].open_time_ms),
            _day_iso_from_ms(candles[2 * third].open_time_ms))


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
                "pair": pair, "strategy": strategy_name}
    out = {"pair": pair, "strategy": strategy_name,
           "n_candles": len(candles), "segments": {}}
    for seg_name in ("dev", "val", "oos"):
        r = results[seg_name]
        seg = r.to_summary()
        seg["trade_details"] = [
            {"entry": t.entry, "exit_price": t.exit_price,
             "pnl_net": t.pnl_net, "exit_reason": t.exit_reason}
            for t in r.trades
        ]
        out["segments"][seg_name] = seg
    return out


def classify_regimes(candles, classifier_cls):
    """Classify each bar using the given classifier. Return regime counts."""
    full = IndicatorSet.from_candles(candles)
    counts = {r: 0 for r in MarketRegime}
    for i in range(len(candles)):
        snap = classifier_cls.classify_at(candles, full, i)
        counts[snap.regime] += 1
    return counts


def main():
    print(f"Phase 17 — controlled classifier comparison")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print()

    client = WazirXClient()
    ltf_cache = {}
    for pair in SLIM_PAIRS:
        try:
            print(f"  Fetch {pair} {INTERVAL} limit={CANDLE_LIMIT}...", end="", flush=True)
            candles, _ = client.candles(pair=pair, interval=INTERVAL,
                                          limit=CANDLE_LIMIT)
            ltf_cache[pair] = candles
            print(f" {len(candles)}")
            time.sleep(0.3)
        except Exception as e:
            print(f" failed: {e}")

    # --- Regime distribution comparison ---
    print("\n--- Regime distribution comparison ---")
    regime_comparison = {}
    for pair in SLIM_PAIRS:
        if pair not in ltf_cache: continue
        candles = ltf_cache[pair]
        rc_counts = classify_regimes(candles, RegimeClassifier)
        adx_counts = classify_regimes(candles, DirectionalAdxClassifier)
        total = len(candles)
        regime_comparison[pair] = {
            "n_candles": total,
            "regime_classifier": {r.value: rc_counts[r] for r in MarketRegime},
            "directional_adx": {r.value: adx_counts[r] for r in MarketRegime},
        }
        print(f"  {pair}:")
        for r in MarketRegime:
            rc_pct = rc_counts[r] / total * 100 if total > 0 else 0
            adx_pct = adx_counts[r] / total * 100 if total > 0 else 0
            print(f"    {r.value:20s}  RC={rc_pct:5.1f}%  ADX={adx_pct:5.1f}%")

    # --- Backtest comparison ---
    all_results = {
        "phase": 17,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "regime_comparison": regime_comparison,
        "backtest_results": [],
    }

    # Run each candidate with EACH classifier
    candidate_classes = [
        ("phase15_trend_pullback_wider_zone", TrendPullbackWiderZone),
        ("phase15_trend_pullback_price_reversal", TrendPullbackPriceReversal),
        ("phase15_mean_reversion_bb", MeanReversionBB),
        ("phase15_mean_reversion_declining", MeanReversionDecliningCloses),
        ("phase15_breakout_retest_level_stop", BreakoutRetestLevelStop),
    ]

    for strat_name, strat_cls in candidate_classes:
        for pair in SLIM_PAIRS:
            if pair not in ltf_cache: continue
            # With RegimeClassifier (default)
            strat_rc = strat_cls()  # default: regime_classifier=RegimeClassifier
            print(f"  Run {strat_name} [RC] on {pair}...", end="", flush=True)
            r_rc = run_strategy(strat_name, strat_rc, pair, INTERVAL, ltf_cache[pair])
            r_rc["classifier"] = "RegimeClassifier"
            r_rc["strategy_type"] = "phase15_candidate"
            if "segments" in r_rc and "oos" in r_rc["segments"]:
                r_rc["segments"]["oos"]["preserved"] = True
            all_results["backtest_results"].append(r_rc)
            print(f" -> dev:t={r_rc['segments']['dev']['num_trades']} "
                  f"val:t={r_rc['segments']['val']['num_trades']}")

            # With DirectionalAdxClassifier
            strat_adx = strat_cls(regime_classifier=DirectionalAdxClassifier)
            print(f"  Run {strat_name} [ADX] on {pair}...", end="", flush=True)
            r_adx = run_strategy(strat_name, strat_adx, pair, INTERVAL, ltf_cache[pair])
            r_adx["classifier"] = "DirectionalAdxClassifier"
            r_adx["strategy_type"] = "phase15_candidate"
            if "segments" in r_adx and "oos" in r_adx["segments"]:
                r_adx["segments"]["oos"]["preserved"] = True
            all_results["backtest_results"].append(r_adx)
            print(f" -> dev:t={r_adx['segments']['dev']['num_trades']} "
                  f"val:t={r_adx['segments']['val']['num_trades']}")

    # --- Aggregate ---
    print("\n--- Aggregated results ---")
    for classifier_name in ("RegimeClassifier", "DirectionalAdxClassifier"):
        runs = [r for r in all_results["backtest_results"]
                if r.get("classifier") == classifier_name]
        total_trades = 0
        total_wins = 0
        total_losses = 0
        total_pnl = 0.0
        for r in runs:
            if "error" in r: continue
            for seg in ("dev", "val"):
                if seg not in r.get("segments", {}): continue
                s = r["segments"][seg]
                if s.get("preserved"): continue
                for t in s.get("trade_details", []):
                    pnl = t.get("pnl_net", 0)
                    total_trades += 1
                    total_pnl += pnl
                    if pnl > 0: total_wins += 1
                    else: total_losses += 1
        print(f"\n  {classifier_name} (dev+val):")
        print(f"    trades={total_trades}  wins={total_wins}  losses={total_losses}")
        print(f"    net_pnl=₹{total_pnl:+.2f}")
        if total_trades > 0:
            print(f"    win_rate={total_wins/total_trades*100:.1f}%")
            print(f"    expectancy=₹{total_pnl/total_trades:+.2f}/trade")

    all_results["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    out_path = "/home/z/my-project/download/phase17_classifier_comparison.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print()
    print(f"Written: {out_path}")
    print(f"Total backtest runs: {len(all_results['backtest_results'])}")


if __name__ == "__main__":
    main()
