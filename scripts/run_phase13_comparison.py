"""Phase 13 — production + new candidates backtest runner.

Runs the existing 4 production strategies AND the 2 new Phase 13
candidate strategies (MultiTimeframeTrendPullback, MeanReversionRsi)
across the 11 vetted pairs (spread < 2.0%).

Production strategies: full dev/val/oos evaluation.
New candidates: dev/val only (OOS preserved per Phase 13 brief req 3).

Output: /home/z/my-project/download/phase13_strategy_comparison.json
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
from coindcx_lab.api.client import RateLimitError, CoinDCXAPIError
from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
from coindcx_lab.engine.risk import RiskSettings
from coindcx_lab.engine.signals import (
    TrendPullbackStrategy, BreakoutRetestStrategy,
    MomentumContinuationStrategy, BaselineSMAStrategy, SignalEngine,
)
from coindcx_lab.engine.phase13_candidates import (
    MultiTimeframeTrendPullback, MeanReversionRsi,
)


# 11 vetted pairs (spread < 2.0%, fresh data, 500+ history).
VETTED_PAIRS = [
    "B-USDTINR", "B-BTCINR", "B-WRXINR", "B-ETHINR", "B-TRXINR",
    "B-PEPEINR", "B-SHIBINR", "B-COTIINR", "B-CELRINR", "B-LINKINR",
    "B-XRPINR",
]
LTF_INTERVAL = "1h"
HTF_INTERVAL = "4h"
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
    print(f"Phase 13 — production + new candidates backtest")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print(f"LTF: {LTF_INTERVAL}  HTF: {HTF_INTERVAL}  Candle limit: {CANDLE_LIMIT}")
    print(f"Pairs ({len(VETTED_PAIRS)}): {VETTED_PAIRS}")
    print()

    client = WazirXClient()
    # Cache LTF (1h) and HTF (4h) candles per pair.
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
        "phase": 13,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "ltf_interval": LTF_INTERVAL,
        "htf_interval": HTF_INTERVAL,
        "candle_limit": CANDLE_LIMIT,
        "production_strategies": [s[0] for s in PRODUCTION_STRATEGIES],
        "candidate_strategies": ["mtf_trend_pullback", "mean_reversion_rsi"],
        "pairs": VETTED_PAIRS,
        "results": [],
    }

    # --- Production strategies (full dev/val/oos) ---
    print("\n--- Production strategies (full dev/val/oos) ---")
    for strat_name, strat in PRODUCTION_STRATEGIES:
        for pair in VETTED_PAIRS:
            if pair not in ltf_cache: continue
            print(f"  Run {strat_name} on {pair}...", end="", flush=True)
            r = run_strategy(strat_name, strat, pair, LTF_INTERVAL, ltf_cache[pair])
            r["strategy_type"] = "production"
            all_results["results"].append(r)
            if "error" in r:
                print(f" error: {r['error']}")
            else:
                line = ""
                for seg in ("dev", "val", "oos"):
                    s = r["segments"][seg]
                    line += f" {seg}:t={s['num_trades']}"
                print(f" ->{line}")

    # --- New candidates (dev/val only — OOS preserved per Phase 13 brief req 3) ---
    print("\n--- New candidates (dev/val only — OOS preserved) ---")
    # MeanReversionRsi: same instance works for all pairs (uses only LTF candles).
    mr_strat = MeanReversionRsi()
    for pair in VETTED_PAIRS:
        if pair not in ltf_cache: continue
        print(f"  Run mean_reversion_rsi on {pair}...", end="", flush=True)
        r = run_strategy("mean_reversion_rsi", mr_strat, pair, LTF_INTERVAL, ltf_cache[pair])
        r["strategy_type"] = "candidate_variant"
        # Mark OOS as preserved
        if "segments" in r and "oos" in r["segments"]:
            r["segments"]["oos"]["preserved"] = True
            r["segments"]["oos"]["note"] = (
                "OOS NOT evaluated for candidate variants per Phase 13 "
                "brief req 3. Variants are selected using dev/val only.")
        all_results["results"].append(r)
        print(f" -> dev:t={r['segments']['dev']['num_trades']} "
              f"val:t={r['segments']['val']['num_trades']}")

    # MultiTimeframeTrendPullback: needs HTF candles per pair (separate
    # instance per pair).
    for pair in VETTED_PAIRS:
        if pair not in ltf_cache or pair not in htf_cache: continue
        print(f"  Run mtf_trend_pullback on {pair}...", end="", flush=True)
        mtf_strat = MultiTimeframeTrendPullback(htf_candles=htf_cache[pair])
        r = run_strategy("mtf_trend_pullback", mtf_strat, pair, LTF_INTERVAL, ltf_cache[pair])
        r["strategy_type"] = "candidate_variant"
        r["pair_specific_strategy"] = True  # MTF strategy is pair-specific
        if "segments" in r and "oos" in r["segments"]:
            r["segments"]["oos"]["preserved"] = True
            r["segments"]["oos"]["note"] = (
                "OOS NOT evaluated for candidate variants per Phase 13 "
                "brief req 3.")
        all_results["results"].append(r)
        print(f" -> dev:t={r['segments']['dev']['num_trades']} "
              f"val:t={r['segments']['val']['num_trades']}")

    all_results["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    out_path = "/home/z/my-project/download/phase13_strategy_comparison.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print()
    print(f"Written: {out_path}")
    print(f"Total runs: {len(all_results['results'])}")


if __name__ == "__main__":
    main()
