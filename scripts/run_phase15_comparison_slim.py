"""Phase 15 — slim evaluation backtest (5 pairs, 5 candidates only).

Slimmer than run_phase15_comparison.py to fit within bash timeout.
Skips the production breakout_retest comparison (those numbers are
already in Phase 13/14 results).

Output: /home/z/my-project/download/phase15_strategy_comparison.json
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from coindcx_lab.api.wazirx_client import WazirXClient
from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
from coindcx_lab.api.client import RateLimitError, CoinDCXAPIError
from coindcx_lab.engine.backtest import BacktestConfig, Backtester, _day_iso_from_ms
from coindcx_lab.engine.risk import RiskSettings
from coindcx_lab.engine.signals import SignalEngine
from coindcx_lab.engine.phase15_candidates import PHASE15_CANDIDATES


# Top 5 pairs by turnover (keeps runtime manageable).
SLIM_PAIRS = ["B-USDTINR", "B-BTCINR", "B-ETHINR", "B-WRXINR", "B-TRXINR"]
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
           "n_candles": len(candles),
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


def aggregate_metrics(results, segments):
    m = {"trades": 0, "wins": 0, "losses": 0,
         "net_pnl": 0.0, "wins_pnl": 0.0, "losses_pnl_abs": 0.0,
         "hold_bars_total": 0, "exit_breakdown": {},
         "max_dd_pct": 0.0, "bh_total_pct": 0.0, "bh_count": 0}
    for r in results:
        if "error" in r: continue
        for seg in segments:
            if seg not in r.get("segments", {}): continue
            s = r["segments"][seg]
            if s.get("preserved"): continue
            for t in s.get("trade_details", []):
                pnl = t.get("pnl_net", 0)
                m["trades"] += 1
                m["net_pnl"] += pnl
                if pnl > 0:
                    m["wins"] += 1
                    m["wins_pnl"] += pnl
                else:
                    m["losses"] += 1
                    m["losses_pnl_abs"] += -pnl
                entry_ms = t.get("entry_time_ms", 0)
                exit_ms = t.get("exit_time_ms", 0)
                if exit_ms > entry_ms:
                    m["hold_bars_total"] += (exit_ms - entry_ms) / 3_600_000
                reason = t.get("exit_reason", "unknown")
                m["exit_breakdown"][reason] = m["exit_breakdown"].get(reason, 0) + 1
            if "max_drawdown_pct" in s:
                m["max_dd_pct"] = max(m["max_dd_pct"], s["max_drawdown_pct"])
            if "buy_hold_return_pct" in s:
                m["bh_total_pct"] += s["buy_hold_return_pct"]
                m["bh_count"] += 1
    m["win_rate_pct"] = (m["wins"] / m["trades"] * 100.0
                          if m["trades"] > 0 else 0.0)
    m["avg_win"] = (m["wins_pnl"] / m["wins"] if m["wins"] > 0 else 0.0)
    m["avg_loss"] = (-m["losses_pnl_abs"] / m["losses"]
                       if m["losses"] > 0 else 0.0)
    m["expectancy"] = (m["net_pnl"] / m["trades"]
                         if m["trades"] > 0 else 0.0)
    m["profit_factor"] = (m["wins_pnl"] / m["losses_pnl_abs"]
                            if m["losses_pnl_abs"] > 0
                            else float("inf") if m["wins_pnl"] > 0 else 0.0)
    m["hold_hours_avg"] = (m["hold_bars_total"] / m["trades"]
                             if m["trades"] > 0 else 0.0)
    m["bh_avg_pct"] = (m["bh_total_pct"] / m["bh_count"]
                         if m["bh_count"] > 0 else 0.0)
    return m


def main():
    print(f"Phase 15 slim — 5 pairs × 5 candidates (dev/val only)")
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

    all_results = {
        "phase": 15,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "interval": INTERVAL,
        "candle_limit": CANDLE_LIMIT,
        "pairs": SLIM_PAIRS,
        "results": [],
    }

    for strat_name, strat in PHASE15_CANDIDATES:
        for pair in SLIM_PAIRS:
            if pair not in ltf_cache: continue
            print(f"  Run {strat_name} on {pair}...", end="", flush=True)
            r = run_strategy(strat_name, strat, pair, INTERVAL, ltf_cache[pair])
            r["strategy_type"] = "phase15_candidate"
            if "segments" in r and "oos" in r["segments"]:
                r["segments"]["oos"]["preserved"] = True
                r["segments"]["oos"]["note"] = (
                    "OOS NOT evaluated for Phase 15 candidates per brief req 4.")
            all_results["results"].append(r)
            print(f" -> dev:t={r['segments']['dev']['num_trades']} "
                  f"val:t={r['segments']['val']['num_trades']}")

    all_results["aggregated_metrics"] = {
        "phase15_candidates_dev_val": aggregate_metrics(
            all_results["results"], ("dev", "val")),
    }
    agg = all_results["aggregated_metrics"]["phase15_candidates_dev_val"]
    print()
    print("Phase 15 candidates (dev+val only, 5 pairs):")
    print(f"  trades={agg['trades']}  wins={agg['wins']}  losses={agg['losses']}")
    print(f"  win_rate={agg['win_rate_pct']:.1f}%")
    print(f"  net_pnl=₹{agg['net_pnl']:+.2f}")
    print(f"  expectancy=₹{agg['expectancy']:+.2f}/trade")
    print(f"  profit_factor={agg['profit_factor']:.3f}")
    print(f"  exit_breakdown={agg['exit_breakdown']}")

    all_results["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    out_path = "/home/z/my-project/download/phase15_strategy_comparison.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print()
    print(f"Written: {out_path}")
    print(f"Total runs: {len(all_results['results'])}")


if __name__ == "__main__":
    main()
