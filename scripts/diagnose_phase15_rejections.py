"""Phase 15 — strategy rejection-reason diagnostic.

Instruments each Phase 14 regime candidate's evaluate() call to
record the specific rejection reason at each bar. Categorizes
rejections per the Phase 15 brief:
  - regime_not_eligible
  - entry_trigger_absent
  - insufficient_history
  - invalid_stop_or_target
  - rr_below_threshold
  - excessive_spread_or_missing_data
  - other (uncategorized)

Output: /home/z/my-project/download/phase15_rejection_diagnostic.json
"""
from __future__ import annotations

import json
import os
import re
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
from coindcx_lab.engine.regime_strategies import (
    RegimeTrendPullback, RegimeBreakoutRetest, RegimeMeanReversion,
    RegimeMultiTimeframe,
)


VETTED_PAIRS = [
    "B-BTCINR", "B-ETHINR", "B-SOLINR", "B-XRPINR", "B-TRXINR",
    "B-PEPEINR", "B-SHIBINR",
]
INTERVAL = "1h"
CANDLE_LIMIT = 1000


def categorize_rejection(explanation: str, state: str) -> str:
    """Map a strategy's explanation string to a Phase 15 category."""
    if not explanation:
        return "other"
    e = explanation.lower()
    # Check regime filter rejections
    if "regime" in e or "trending_up" in e or "range_bound" in e or \
       "high_volatility" in e or "trending_down" in e or "uncertain" in e:
        return "regime_not_eligible"
    # Insufficient history
    if "warm-up" in e or "warmup" in e or "warm up" in e:
        return "insufficient_history"
    # RSI / trigger absences
    if "rsi" in e or "cross" in e or "macd" in e or "relvol" in e or \
       "not in pullback" in e or "no upside breakout" in e or \
       "breakout not yet retested" in e or "no recent swing" in e or \
       "atr not available" in e or "no breakout" in e:
        return "entry_trigger_absent"
    # Invalid stop or target geometry
    if "stop" in e and ("entry" in e or "invalid" in e or "geometry" in e):
        return "invalid_stop_or_target"
    # Risk-gate rejections (NO_TRADE state with risk reason)
    if state == "NO_TRADE" and ("risk" in e or "r/r" in e or "rejected" in e):
        return "rr_below_threshold"
    return "other"


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


def diagnose_strategy(strategy, pair: str, candles: List[Candle]) -> dict:
    """Slide a window through candles; at each eligible bar, evaluate
    the strategy and record the rejection category."""
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
        note="phase15 rejection diagnostic")
    reason_counter = Counter()
    n_evaluated = 0
    n_buy_candidates = 0

    for i in range(strategy.warmup_candles, len(candles)):
        prefix = candles[:i + 1]
        indicators = IndicatorSet.from_candles(prefix)
        sig = strategy.evaluate(
            pair=pair, interval=INTERVAL, candles=prefix,
            indicators=indicators, risk_engine=risk_engine,
            risk_state=state, provenance=prov)
        n_evaluated += 1
        if sig.state.value == "BUY_CANDIDATE":
            n_buy_candidates += 1
        # Categorize the rejection (or the supporting reason if BUY)
        cat = categorize_rejection(sig.explanation, sig.state.value)
        reason_counter[cat] += 1

    return {
        "pair": pair,
        "strategy": strategy.name,
        "n_candles": len(candles),
        "n_evaluated": n_evaluated,
        "n_buy_candidates": n_buy_candidates,
        "rejection_counts": dict(reason_counter),
    }


def main():
    print(f"Phase 15 — strategy rejection-reason diagnostic")
    print(f"Started at: {datetime.now(timezone.utc).isoformat()}")
    print(f"Interval: {INTERVAL}  Candle limit: {CANDLE_LIMIT}")
    print()

    client = WazirXClient()
    candles_cache = {}
    htf_cache = {}
    for pair in VETTED_PAIRS:
        try:
            print(f"  Fetch {pair} {INTERVAL} limit={CANDLE_LIMIT}...", end="", flush=True)
            candles, _ = client.candles(pair=pair, interval=INTERVAL,
                                          limit=CANDLE_LIMIT)
            candles_cache[pair] = candles
            print(f" {len(candles)}")
            time.sleep(0.3)
        except Exception as e:
            print(f" failed: {e}")
        try:
            print(f"  Fetch {pair} 4h limit={CANDLE_LIMIT}...", end="", flush=True)
            candles, _ = client.candles(pair=pair, interval="4h",
                                          limit=CANDLE_LIMIT)
            htf_cache[pair] = candles
            print(f" {len(candles)}")
            time.sleep(0.3)
        except Exception as e:
            print(f" failed: {e}")

    strategies = [
        RegimeTrendPullback(),
        RegimeBreakoutRetest(),
        RegimeMeanReversion(),
    ]
    all_diags = []
    for strat in strategies:
        for pair in VETTED_PAIRS:
            if pair not in candles_cache: continue
            print(f"  Diagnosing {strat.name} on {pair}...")
            d = diagnose_strategy(strat, pair, candles_cache[pair])
            all_diags.append(d)
            print(f"    evaluated={d['n_evaluated']} "
                  f"buy={d['n_buy_candidates']} "
                  f"rejections={d['rejection_counts']}")

    # MTF strategy needs HTF candles per pair
    for pair in VETTED_PAIRS:
        if pair not in candles_cache or pair not in htf_cache: continue
        print(f"  Diagnosing regime_mtf_trend_pullback on {pair}...")
        strat = RegimeMultiTimeframe(htf_candles=htf_cache[pair])
        d = diagnose_strategy(strat, pair, candles_cache[pair])
        all_diags.append(d)
        print(f"    evaluated={d['n_evaluated']} "
              f"buy={d['n_buy_candidates']} "
              f"rejections={d['rejection_counts']}")

    # Aggregate by strategy
    by_strategy = {}
    for d in all_diags:
        s = d["strategy"]
        if s not in by_strategy:
            by_strategy[s] = Counter()
        for k, v in d["rejection_counts"].items():
            by_strategy[s][k] += v

    out = {
        "phase": 15,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "interval": INTERVAL,
        "candle_limit": CANDLE_LIMIT,
        "strategies_diagnosed": list(by_strategy.keys()),
        "diagnostics": all_diags,
        "aggregate_by_strategy": {k: dict(v) for k, v in by_strategy.items()},
    }
    out_path = "/home/z/my-project/download/phase15_rejection_diagnostic.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print()
    print("Aggregate rejection counts by strategy:")
    for s, counts in by_strategy.items():
        print(f"  {s}:")
        for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"    {cat}: {n}")
    print()
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
