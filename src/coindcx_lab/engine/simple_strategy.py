"""Phase 18 — simple baseline strategy for honest profitability validation.

Rules (documented precisely before running the experiment):

  Trend filter:
    - close > EMA50 (price is above the medium-term moving average).
    - NO regime filter, NO RSI filter, NO pullback zone — minimal
      filtering to maximize trade generation and diagnose whether
      the entry hypothesis produces enough trades to evaluate.

  Entry trigger:
    - close > max(high of last N bars) where N=20 (resistance breakout).
    - This is the simplest possible breakout entry: price closes above
      the highest high of the last 20 bars.
    - No retest requirement (unlike breakout_retest which requires
      both breakout AND retest — too restrictive).

  Stop-loss:
    - entry - 2 * ATR(14) (2 ATR below entry — same as Phase 14/15
      regime-aware candidates, wide enough for post-cost R/R to be
      favorable).

  Profit target:
    - entry + 3 * (entry - stop) = entry + 6 * ATR (3R target where
      R = 2*ATR). With 3R target, the post-cost R/R is:
        (3R - cost) / (R + cost) which is >= 2.0 when R >= ~1% of price.
      This is the MINIMUM target that passes the 2.0 reward_to_risk_target
      gate with realistic costs (0.3% round-trip). A 2R target is
      mathematically impossible to achieve R/R >= 2.0 after positive costs
      (since (2R-cost)/(R+cost) < 2 for any cost > 0).

  Invalidation:
    - Stop hit OR target hit OR end-of-data force-close.

  Spread/fee/slippage:
    - Handled by the risk engine's _finalise_with_costs() which
      computes est_entry_cost, est_exit_cost, est_slippage_cost and
      checks R/R after cost against the reward_to_risk_target.
    - Settings: fee_pct=0.1, spread_pct=0.05, slippage_pct=0.05.

  Warmup:
    - 50 bars (needs EMA50 + ATR(14) + 20-bar high lookback).

Production safeguards preserved:
  - Long-only (paper-trading is long-only by design).
  - Risk engine integration (fees, slippage, R/R after cost, daily
    loss / drawdown / max-positions limits).
  - Research candidate — NOT connected to production /api/signals.
  - Production strategy defaults UNCHANGED.
"""
from __future__ import annotations

import math
from typing import List, Optional

import numpy as np

from .indicators import IndicatorSet, TrendDirection, ema
from .risk import RiskEngine, RiskState
from .signals import (
    Strategy, Signal, SignalState, SignalReason, TrendPullbackStrategy,
    BreakoutRetestStrategy,
)
from ..api.models import Candle, DataProvenance


class SimpleBreakoutStrategy(Strategy):
    """Phase 18 — simplest possible trend-following breakout strategy.

    Entry: close > EMA50 (trend filter) AND close > max(high of last 20 bars)
           (resistance breakout).
    Stop: entry - 2 * ATR(14).
    Target: entry + 2 * (entry - stop) = 2R.
    Warmup: 50 bars (needs EMA50 + ATR14 + 20-bar lookback).

    This is a RESEARCH-ONLY strategy — NOT connected to production
    /api/signals. Used to test whether a minimal-filter approach
    produces enough trades to evaluate profitability honestly.
    """
    name = "simple_breakout"
    warmup_candles = 50  # needs EMA50 + ATR(14) + 20-bar high

    # Lookback for the resistance breakout detection.
    RESISTANCE_LOOKBACK = 20

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete (needs 50 bars)"
            return sig

        i = indicators.last_idx
        ema50 = indicators.ema50[i]
        atr_val = indicators.atr14[i]
        last = candles[i]

        if math.isnan(ema50) or math.isnan(atr_val) or atr_val <= 0:
            sig.explanation = "EMA50 or ATR not yet initialized"
            return sig

        # --- Trend filter: close > EMA50 ---
        if last.close <= ema50:
            sig.state = SignalState.WAIT
            sig.explanation = (f"close {last.close:.2f} <= EMA50 {ema50:.2f} "
                               "(trend filter: need close > EMA50)")
            return sig
        sig.supporting.append(SignalReason(
            text=f"close {last.close:.2f} > EMA50 {ema50:.2f} (trend filter)"))

        # --- Entry trigger: close > max(high of last N bars) ---
        # Exclude the current bar from the lookback (we compare current
        # close to the PREVIOUS N bars' highs).
        lookback = self.RESISTANCE_LOOKBACK
        if i < lookback:
            sig.state = SignalState.WAIT
            sig.explanation = f"need at least {lookback} bars for resistance lookback"
            return sig
        recent_highs = [candles[j].high for j in range(i - lookback, i)]
        resistance = max(recent_highs)
        if last.close <= resistance:
            sig.state = SignalState.WAIT
            sig.explanation = (f"close {last.close:.2f} <= resistance "
                               f"{resistance:.2f} (max high of last {lookback} bars)")
            return sig
        sig.supporting.append(SignalReason(
            text=f"close {last.close:.2f} > resistance {resistance:.2f} "
                 f"(max high of last {lookback} bars)"))

        # --- Stop-loss: entry - 2 * ATR ---
        entry = last.close
        stop = entry - 2.0 * atr_val
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry; invalid geometry"
            return sig

        # --- Target: 3R (minimum to pass the 2.0 R/R-after-cost gate
        # with positive fees+slippage; 2R is mathematically impossible
        # since (2R-cost)/(R+cost) < 2 for any cost > 0) ---
        risk_per_unit = entry - stop
        target = entry + 3 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"stop = entry - 2*ATR({atr_val:.2f}) = {stop:.2f}; "
                 f"target = 3R = {target:.2f}"))

        # --- Finalise with costs (fees, slippage, R/R check) ---
        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)
