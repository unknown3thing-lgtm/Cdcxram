"""Phase 11 walk-forward backtest runner — slim version.

Slimmer than run_phase11_backtest.py: focuses on 5 high-priority pairs
across 3 intervals. Fetches candles lazily per (pair, interval) to
avoid hitting rate limits with a big upfront burst.

Output: /home/z/my-project/download/phase11_walk_forward_results.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
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
    TrendPullbackStrategy,
    BreakoutRetestStrategy,
    MomentumContinuationStrategy,
    BaselineSMAStrategy,
    SignalEngine,
)


# Slim pair list — 5 high-priority INR pairs.
DEFAULT_PAIRS = [
    "B-BTCINR", "B-ETHINR", "B-SOLINR", "B-XRPINR", "B-MATICINR",
]
INTERVALS = ["1h", "4h", "1d"]
CANDLE_LIMIT = 1000

ALL_STRATEGIES = [
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


def split_segments(candles: List[Candle]):
    if not candles:
        return "", ""
    n = len(candles)
    third = n // 3
    dev_end_iso = _day_iso_from_ms(candles[third].open_time_ms)
    val_end_iso = _day_iso_from_ms(candles[2 * third].open_time_ms)
    return dev_end_iso, val_end_iso


def run_one(strategy_name, strategy, pair, interval, candles):
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
        # Phase 11: also serialize trade-level details so the report can
        # show win/loss breakdown per trade. to_summary() intentionally
        # omits the trades list (it's verbose); we add a compact version.
        seg_summary["trade_details"] = [
            {"entry": t.entry, "exit_price": t.exit_price,
             "pnl_net": t.pnl_net, "exit_reason": t.exit_reason,
             "entry_time_ms": t.entry_time_ms,
             "exit_time_ms": t.exit_time_ms}
            for t in r.trades
        ]
        out["segments"][seg_name] = seg_summary
    return out


def main():
    print(f"Phase 11 walk-forward (slim) — started {datetime.now(timezone.utc).isoformat()}")
    print(f"Candle limit: {CANDLE_LIMIT}; intervals: {INTERVALS}")
    print()
    all_results = {
        "phase": 11,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine_version": "phase11_interval_support_slim",
        "strategies": [s[0] for s in ALL_STRATEGIES],
        "pairs": DEFAULT_PAIRS,
        "intervals": INTERVALS,
        "candle_limit": CANDLE_LIMIT,
        "data_source": "WazirX public klines API",
        "results": [],
        "fetch_errors": [],
    }

    client = WazirXClient()
    # Cache per (pair, interval) — fetch lazily.
    cache = {}
    total_runs = 0
    for strat_name, strat in ALL_STRATEGIES:
        for pair in DEFAULT_PAIRS:
            for interval in INTERVALS:
                key = (pair, interval)
                if key not in cache:
                    try:
                        print(f"  Fetch {pair} {interval} limit={CANDLE_LIMIT}...", end="", flush=True)
                        candles, prov = client.candles(
                            pair=pair, interval=interval, limit=CANDLE_LIMIT)
                        cache[key] = candles
                        first = datetime.fromtimestamp(
                            candles[0].open_time_ms / 1000, tz=timezone.utc)
                        last = datetime.fromtimestamp(
                            candles[-1].open_time_ms / 1000, tz=timezone.utc)
                        print(f" {len(candles)} candles {first.date()}->{last.date()}")
                        time.sleep(0.3)
                    except (RateLimitError, CoinDCXAPIError) as e:
                        err = {"pair": pair, "interval": interval,
                                "error": f"{type(e).__name__}: {e}"}
                        all_results["fetch_errors"].append(err)
                        cache[key] = None
                        print(f" failed: {e}")
                        continue
                candles = cache[key]
                if candles is None:
                    continue
                # Run backtest
                print(f"  Run {strat_name} {pair} {interval}", end="", flush=True)
                r = run_one(strat_name, strat, pair, interval, candles)
                all_results["results"].append(r)
                total_runs += 1
                if "error" in r:
                    print(f" -> error: {r['error']}")
                else:
                    line = ""
                    for seg in ("dev", "val", "oos"):
                        if seg in r.get("segments", {}):
                            s = r["segments"][seg]
                            line += f" {seg}:t={s['num_trades']}r={s['total_return_pct']:+.1f}%bh={s['buy_hold_return_pct']:+.1f}%elig={s['eligible_bars']}"
                    print(f" ->{line}")

    all_results["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    all_results["total_runs"] = total_runs
    out_path = "/home/z/my-project/download/phase11_walk_forward_results.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print()
    print(f"Written: {out_path}")
    print(f"Total runs: {total_runs}")
    print(f"Fetch errors: {len(all_results['fetch_errors'])}")


if __name__ == "__main__":
    main()
