"""Phase 12 — strategy entry-condition diagnostic.

Traces each strategy's actual entry conditions against historical
candles to identify which conditions block entries and how frequently
each condition fires / fails.

This is READ-ONLY: fetches public WazirX candles, runs the existing
strategy evaluate() method on a sliding window, and counts how often
each supporting/conflicting reason fires.

Output: /home/z/my-project/download/phase12_strategy_diagnostic.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from coindcx_lab.api.wazirx_client import WazirXClient
from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
from coindcx_lab.engine.indicators import IndicatorSet
from coindcx_lab.engine.risk import RiskSettings, RiskState, RiskEngine, DailyLossState
from coindcx_lab.engine.signals import (
    TrendPullbackStrategy, BreakoutRetestStrategy,
    MomentumContinuationStrategy, BaselineSMAStrategy,
)


# Use 1h interval with 1000-candle history — same as Phase 11 evaluation.
DEFAULT_PAIRS = ["B-BTCINR", "B-ETHINR", "B-SOLINR", "B-XRPINR"]
INTERVAL = "1h"
CANDLE_LIMIT = 1000


def settings_for_diagnostic() -> RiskSettings:
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


def trace_strategy(strategy, pair: str, candles: List[Candle]) -> dict:
    """Slide a window through the candles; at each completed bar i
    (where i >= strategy.warmup_candles), call strategy.evaluate()
    and collect the supporting / conflicting reasons.

    Returns a Counter of how often each reason text appears.
    """
    s = settings_for_diagnostic()
    risk_engine = RiskEngine(s)
    state = RiskState(initial_inr=2000.0)
    state.daily = DailyLossState(
        day_iso=datetime.fromtimestamp(
            candles[0].open_time_ms / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d"))
    prov = DataProvenance(
        source=SourceKind.SIMULATED, endpoint="diagnostic",
        fetched_at_utc=datetime.now(timezone.utc),
        note="phase12 diagnostic fixture")

    reason_counter = Counter()
    state_counter = Counter()
    n_evaluated = 0
    n_buy_candidates = 0
    last_idx = -1

    for i in range(strategy.warmup_candles, len(candles)):
        prefix = candles[:i + 1]
        # Build indicators from prefix (matches what the strategy
        # does internally via SignalEngine).
        indicators = IndicatorSet.from_candles(prefix)
        sig = strategy.evaluate(
            pair=pair, interval=INTERVAL, candles=prefix,
            indicators=indicators, risk_engine=risk_engine,
            risk_state=state, provenance=prov)
        n_evaluated += 1
        state_counter[sig.state.value] += 1
        if sig.state.value == "BUY_CANDIDATE":
            n_buy_candidates += 1
        for reason in sig.supporting:
            # Group by the first few words (the reason text varies
            # slightly with parameter values; we want category counts).
            key = " ".join(reason.text.split()[:4]) if reason.text else "(empty)"
            reason_counter[f"SUPPORT: {key}"] += 1
        for reason in sig.conflicting:
            key = " ".join(reason.text.split()[:4]) if reason.text else "(empty)"
            reason_counter[f"BLOCK: {key}"] += 1
        last_idx = i

    return {
        "pair": pair,
        "strategy": strategy.name,
        "n_candles": len(candles),
        "n_evaluated": n_evaluated,
        "n_buy_candidates": n_buy_candidates,
        "state_counts": dict(state_counter),
        "top_reasons": dict(reason_counter.most_common(15)),
    }


def main():
    print(f"Phase 12 — strategy entry-condition diagnostic")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print(f"Interval: {INTERVAL}  Candle limit: {CANDLE_LIMIT}")
    print()

    client = WazirXClient()
    candles_cache = {}
    for pair in DEFAULT_PAIRS:
        try:
            print(f"  Fetch {pair} {INTERVAL} limit={CANDLE_LIMIT}...", end="", flush=True)
            candles, prov = client.candles(pair=pair, interval=INTERVAL,
                                            limit=CANDLE_LIMIT)
            candles_cache[pair] = candles
            print(f" {len(candles)} candles")
            time.sleep(0.4)
        except Exception as e:
            print(f" failed: {e}")

    strategies = [
        TrendPullbackStrategy(),
        BreakoutRetestStrategy(),
        MomentumContinuationStrategy(),
        BaselineSMAStrategy(),
    ]
    all_results = {
        "phase": 12,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "interval": INTERVAL,
        "candle_limit": CANDLE_LIMIT,
        "strategies": [s.name for s in strategies],
        "pairs": DEFAULT_PAIRS,
        "diagnostics": [],
    }

    for strat in strategies:
        for pair in DEFAULT_PAIRS:
            if pair not in candles_cache:
                continue
            print(f"  Tracing {strat.name} on {pair}...")
            d = trace_strategy(strat, pair, candles_cache[pair])
            all_results["diagnostics"].append(d)
            print(f"    evaluated={d['n_evaluated']} "
                  f"buy_candidates={d['n_buy_candidates']} "
                  f"states={d['state_counts']}")

    all_results["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    out_path = "/home/z/my-project/download/phase12_strategy_diagnostic.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print()
    print(f"Written: {out_path}")
    print(f"Total diagnostics: {len(all_results['diagnostics'])}")


if __name__ == "__main__":
    main()
