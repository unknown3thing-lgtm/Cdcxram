"""Phase 10 walk-forward backtest runner.

Runs all 4 existing strategies across the existing supported pairs and
segments using the CORRECTED Phase 10 walk-forward engine (with warm-up
prefix from prior segments). Records before/after metrics for the final
report.

Output: writes a JSON results file to
    /home/z/my-project/download/phase10_walk_forward_results.json

Usage:
    python /home/z/my-project/scripts/run_phase10_backtest.py

This script DOES NOT:
    - place real orders
    - modify production trades/portfolio
    - tune strategy parameters
    - reset any data

It only READS public WazirX candle data and writes a JSON summary.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import List, Tuple

# Make src importable
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from coindcx_lab.api.wazirx_client import WazirXClient
from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
from coindcx_lab.engine.risk import RiskSettings
from coindcx_lab.engine.signals import (
    TrendPullbackStrategy,
    BreakoutRetestStrategy,
    MomentumContinuationStrategy,
    BaselineSMAStrategy,
    SignalEngine,
)


# Existing supported pairs (matches the existing scanner / paper-trade
# universe — kept stable per Phase 10 requirement: "existing strategy
# parameters unchanged").
DEFAULT_PAIRS = [
    "B-BTCINR",    # BTC/INR — highest turnover
    "B-ETHINR",    # ETH/INR
    "B-SOLINR",    # SOL/INR
    "B-XRPINR",    # XRP/INR — had +168% OOS rally per Phase 9 notes
    "B-MATICINR",  # MATIC/INR
]

# Existing strategies (Phase 10: parameters preserved).
ALL_STRATEGIES = [
    ("trend_pullback", TrendPullbackStrategy()),
    ("breakout_retest", BreakoutRetestStrategy()),
    ("momentum_continuation", MomentumContinuationStrategy()),
    ("baseline_sma", BaselineSMAStrategy()),
]


def fetch_candles(pair: str, interval: str = "1h",
                  limit: int = 500) -> Tuple[List[Candle], DataProvenance]:
    """Fetch historical candles from WazirX public API."""
    client = WazirXClient()
    return client.candles(pair=pair, interval=interval, limit=limit)


def split_segments(candles: List[Candle]):
    """Split candles into dev/val/oos by chronological thirds."""
    if not candles:
        return [], [], []
    n = len(candles)
    # Roughly equal thirds. The dev_end / val_end ISO strings are
    # derived from the FIRST candle of the next segment (so the
    # boundary is inclusive of the dev/val day).
    third = n // 3
    dev_end_iso = _day_iso_from_ms(candles[third].open_time_ms)
    val_end_iso = _day_iso_from_ms(candles[2 * third].open_time_ms)
    return dev_end_iso, val_end_iso


def settings_for_backtest() -> RiskSettings:
    """Risk settings for paper-trading context. Matches the production
    defaults (fees/slippage preserved per Phase 10)."""
    return RiskSettings(
        initial_inr=2000.0,
        risk_per_trade_pct=1.0,
        max_position_pct=25.0,
        max_portfolio_exposure_pct=80.0,
        max_daily_loss_pct=5.0,
        max_daily_loss_abs_inr=0.0,
        max_drawdown_pct=15.0,
        max_open_positions=5,
        max_daily_trades=10,
        reward_to_risk_target=2.0,
        cooldown_after_consecutive_losses=3,
        allow_overnight=True,
        fee_pct=0.1,
        spread_pct=0.05,
        slippage_pct=0.05,
        min_24h_volume_inr=0,
        min_24h_trades=0,
        max_spread_pct=1.0,
        trading_allocation_pct=100.0,
    )


def run_strategy_on_pair(strategy_name: str, strategy, pair: str,
                          candles: List[Candle]) -> dict:
    """Run a single (strategy, pair) walk-forward and return summary."""
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
                                  val_end_iso=val_end_iso)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}",
                "pair": pair, "strategy": strategy_name,
                "n_candles": len(candles)}
    out = {"pair": pair, "strategy": strategy_name,
           "n_candles": len(candles),
           "dev_end_iso": dev_end_iso, "val_end_iso": val_end_iso,
           "segments": {}}
    for seg_name in ("dev", "val", "oos"):
        r = results[seg_name]
        out["segments"][seg_name] = r.to_summary()
    return out


def main():
    print(f"Phase 10 walk-forward backtest runner")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print()

    all_results = {
        "phase": 10,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine_version": "phase10_corrected_walk_forward",
        "strategies": [s[0] for s in ALL_STRATEGIES],
        "pairs": DEFAULT_PAIRS,
        "data_source": "WazirX public klines API",
        "interval": "1h",
        "candle_limit_per_fetch": 500,
        "notes": [
            "Walk-forward now passes a warm-up prefix from prior segments",
            "so EMA200 can initialise on short (125-bar) val/oos windows.",
            "Trades whose entry_idx < segment_start_idx are NOT scored.",
            "Buy-and-hold uses segment-only first/last prices.",
            "Eligible bars reported per segment.",
            "Strategy parameters preserved from prior phases — no tuning.",
        ],
        "results": [],
        "fetch_errors": [],
    }

    # Cache candles per pair (don't refetch on every strategy).
    candles_cache = {}
    for pair in DEFAULT_PAIRS:
        try:
            print(f"  Fetching {pair} 1h candles (limit=500)...")
            candles, prov = fetch_candles(pair=pair, interval="1h", limit=500)
            candles_cache[pair] = (candles, prov)
            print(f"    -> got {len(candles)} candles "
                  f"from {datetime.fromtimestamp(candles[0].open_time_ms/1000, tz=timezone.utc).date()}"
                  f" to {datetime.fromtimestamp(candles[-1].open_time_ms/1000, tz=timezone.utc).date()}"
                  f"  source={prov.source.value}")
            # Be polite to the API
            time.sleep(0.5)
        except Exception as e:
            err = {"pair": pair, "error": f"{type(e).__name__}: {e}"}
            all_results["fetch_errors"].append(err)
            print(f"    !! fetch failed: {e}")

    # Now run all (strategy, pair) combinations.
    for strat_name, strat in ALL_STRATEGIES:
        for pair in DEFAULT_PAIRS:
            if pair not in candles_cache:
                continue
            candles, prov = candles_cache[pair]
            print(f"  Running {strat_name} on {pair}...")
            res = run_strategy_on_pair(strat_name, strat, pair, candles)
            all_results["results"].append(res)
            # Quick summary line
            for seg_name in ("dev", "val", "oos"):
                if seg_name in res.get("segments", {}):
                    seg = res["segments"][seg_name]
                    print(f"    {seg_name}: trades={seg['num_trades']} "
                          f"ret={seg['total_return_pct']:.2f}% "
                          f"bh={seg['buy_hold_return_pct']:.2f}% "
                          f"dd={seg['max_drawdown_pct']:.2f}% "
                          f"elig={seg['eligible_bars']} "
                          f"pref={seg['warmup_prefix_len']}")
            # Be polite to the engine — short sleep
            time.sleep(0.1)

    all_results["ended_at_utc"] = datetime.now(timezone.utc).isoformat()

    # Write to download/
    out_path = "/home/z/my-project/download/phase10_walk_forward_results.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print()
    print(f"Results written to: {out_path}")
    print(f"Total (strategy,pair) runs: {len(all_results['results'])}")
    print(f"Fetch errors: {len(all_results['fetch_errors'])}")


if __name__ == "__main__":
    main()
