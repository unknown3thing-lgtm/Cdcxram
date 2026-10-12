"""Phase 12 — candidate strategy variants for research.

These are EXPERIMENTAL variations on the existing production strategies,
intended ONLY for dev/val evaluation. The existing production strategies
(in src/coindcx_lab/engine/signals.py) remain unchanged.

Variants are documented with their rationale. Successful variants (if
any) may be promoted to production in a future phase, but ONLY after
out-of-sample validation. Phase 12 explicitly evaluates on dev/val
only — OOS is preserved.

Variants:
  1. TrendPullbackRelaxedRSI — RSI range [35, 60] instead of [40, 55].
     Rationale: Phase 12 diagnostic showed trend_pullback produces 0
     BUY_CANDIDATE signals. The strict RSI [40, 55] rarely aligns with
     the strict UP-trend filter. Widening to [35, 60] may capture
     more pullback opportunities without fundamentally changing the
     strategy's pullback-entry thesis.

  2. TrendPullbackWiderPullbackZone — price in [EMA100, EMA20] instead
     of [EMA50, EMA20]. Rationale: the existing [EMA50, EMA20] pullback
     zone is shallow; widening to [EMA100, EMA20] captures deeper
     pullbacks in trending markets.

  3. MomentumContinuationRelaxed — RSI > 50 (instead of > 55) and
     relative_volume > 1.0 (instead of > 1.2). Rationale: the existing
     entry conditions are too restrictive for INR-spot volatility;
     slight relaxation may produce signals while preserving the
     strategy's momentum-continuation thesis.

All variants preserve:
  * Long-only behavior (no leverage, no futures).
  * The risk engine integration (fees, slippage, R/R after cost).
  * The warmup_candles requirement (200 for trend variants, 60 for
    momentum variants).
  * The 2R target and stop-loss semantics.
"""
from __future__ import annotations

import math
from typing import Optional

from .indicators import IndicatorSet, TrendDirection
from .risk import RiskEngine, RiskState
from .signals import (
    Strategy, Signal, SignalState, SignalReason, SignalEngine,
    TrendPullbackStrategy, MomentumContinuationStrategy,
)
from ..api.models import Candle, DataProvenance


# ===========================================================================
# Variant 1: TrendPullbackRelaxedRSI
# ===========================================================================
class TrendPullbackRelaxedRSI(TrendPullbackStrategy):
    """Variant: RSI range [35, 60] instead of [40, 55].

    Inherits everything from TrendPullbackStrategy (trend filter,
    pullback zone, stop/target, risk integration) but widens the RSI
    acceptance range. This is the MINIMAL change that might allow the
    strategy to actually fire on INR-spot candles.
    """
    name = "trend_pullback_relaxed_rsi"
    warmup_candles = 200  # same as parent

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        trend = indicators.trend_at(i)
        ema20 = indicators.ema20[i]
        ema50 = indicators.ema50[i]
        rsi = indicators.rsi14[i]
        last = candles[i]

        if trend is not TrendDirection.UP:
            sig.state = SignalState.WAIT
            sig.explanation = f"trend {trend.value}; need UP (EMA20>EMA50>EMA200)"
            return sig
        sig.supporting.append(SignalReason(
            text=f"trend UP (EMA20={ema20:.2f}, EMA50={ema50:.2f})"))

        if not (ema50 <= last.close <= ema20):
            sig.state = SignalState.WAIT
            sig.explanation = (f"price {last.close:.2f} not in pullback zone "
                               f"[EMA50={ema50:.2f}, EMA20={ema20:.2f}]")
            return sig
        sig.supporting.append(SignalReason(
            text=f"price {last.close:.2f} in pullback zone"))

        # VARIANT: RSI in [35, 60] instead of [40, 55]
        if not (35 <= rsi <= 60):
            sig.conflicting.append(SignalReason(
                text=f"RSI {rsi:.1f} outside [35,60] (variant relaxed range)",
                is_supporting=False))
        else:
            sig.supporting.append(SignalReason(
                text=f"RSI {rsi:.1f} within [35,60] (variant)"))

        # Stop / target — same as parent
        entry = last.close
        recent_lows = [sl for sl in indicators.swing_lows if sl is not None][-3:]
        atr_val = indicators.atr14[i]
        if recent_lows:
            stop = min(recent_lows) * 0.998
        elif not math.isnan(atr_val) and atr_val > 0:
            stop = entry - 1.5 * atr_val
        else:
            sig.state = SignalState.WAIT
            sig.explanation = "no recent swing low and ATR unavailable"
            return sig
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry; invalid geometry"
            return sig

        risk_per_unit = entry - stop
        target = entry + 2 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"target {target:.2f} = 2R"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# ===========================================================================
# Variant 2: TrendPullbackWiderPullbackZone
# ===========================================================================
class TrendPullbackWiderPullbackZone(TrendPullbackStrategy):
    """Variant: pullback zone is [EMA100, EMA20] instead of [EMA50, EMA20].

    Inherits everything from TrendPullbackStrategy but accepts deeper
    pullbacks (down to EMA100) as valid entry zones.
    """
    name = "trend_pullback_wider_zone"
    warmup_candles = 200

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        trend = indicators.trend_at(i)
        ema20 = indicators.ema20[i]
        ema50 = indicators.ema50[i]
        ema100 = indicators.ema200[i]  # EMA200 is the slowest EMA available
        rsi = indicators.rsi14[i]
        last = candles[i]

        if trend is not TrendDirection.UP:
            sig.state = SignalState.WAIT
            sig.explanation = f"trend {trend.value}; need UP"
            return sig
        sig.supporting.append(SignalReason(text=f"trend UP"))

        # VARIANT: pullback zone is [EMA100, EMA20] (deeper pullback OK)
        if math.isnan(ema100) or ema100 <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "EMA100 (EMA200) not yet initialized"
            return sig
        if not (ema100 <= last.close <= ema20):
            sig.state = SignalState.WAIT
            sig.explanation = (f"price {last.close:.2f} not in wider pullback "
                               f"zone [EMA100={ema100:.2f}, EMA20={ema20:.2f}]")
            return sig
        sig.supporting.append(SignalReason(
            text=f"price {last.close:.2f} in wider pullback zone "
                 f"[{ema100:.2f}, {ema20:.2f}]"))

        if not (40 <= rsi <= 55):
            sig.conflicting.append(SignalReason(
                text=f"RSI {rsi:.1f} outside [40,55]",
                is_supporting=False))
        else:
            sig.supporting.append(SignalReason(
                text=f"RSI {rsi:.1f} within [40,55]"))

        entry = last.close
        recent_lows = [sl for sl in indicators.swing_lows if sl is not None][-3:]
        atr_val = indicators.atr14[i]
        if recent_lows:
            stop = min(recent_lows) * 0.998
        elif not math.isnan(atr_val) and atr_val > 0:
            stop = entry - 1.5 * atr_val
        else:
            sig.state = SignalState.WAIT
            sig.explanation = "no recent swing low and ATR unavailable"
            return sig
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry; invalid geometry"
            return sig

        risk_per_unit = entry - stop
        target = entry + 2 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"target {target:.2f} = 2R"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# ===========================================================================
# Variant 3: MomentumContinuationRelaxed
# ===========================================================================
class MomentumContinuationRelaxed(MomentumContinuationStrategy):
    """Variant: RSI > 50 (instead of > 55) and relvol > 1.0 (instead of > 1.2).

    Slightly relaxes the momentum confirmation thresholds.
    """
    name = "momentum_continuation_relaxed"
    warmup_candles = 60

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        hist = indicators.macd_hist[i]
        rsi = indicators.rsi14[i]
        rv = indicators.relvol20[i]
        trend = indicators.trend_at(i)

        if math.isnan(hist) or math.isnan(rsi) or math.isnan(rv):
            sig.explanation = "indicator warm-up incomplete"
            return sig

        if trend is not TrendDirection.UP:
            sig.state = SignalState.WAIT
            sig.explanation = "trend not UP"
            return sig
        sig.supporting.append(SignalReason(text="trend UP"))

        # VARIANT: hist > 0 and RSI > 50 (was > 55)
        if not (hist > 0 and rsi > 50):
            sig.state = SignalState.WAIT
            sig.explanation = (f"MACD hist {hist:.3f} or RSI {rsi:.1f} "
                               "not in variant continuation zone")
            return sig
        sig.supporting.append(SignalReason(
            text=f"MACD hist {hist:.3f} > 0 and RSI {rsi:.1f} > 50 (variant)"))

        # VARIANT: relvol > 1.0 (was > 1.2)
        if rv < 1.0:
            sig.conflicting.append(SignalReason(
                text=f"relative volume {rv:.2f} < 1.0 (variant threshold)",
                is_supporting=False))
        else:
            sig.supporting.append(SignalReason(
                text=f"relative volume {rv:.2f} > 1.0 (variant)"))

        entry = candles[-1].close
        atr_val = indicators.atr14[i]
        if math.isnan(atr_val) or atr_val <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "ATR not available / non-positive"
            return sig
        stop = entry - 1.5 * atr_val
        target = entry + 2 * 1.5 * atr_val
        sig.supporting.append(SignalReason(
            text=f"stop = entry - 1.5*ATR({atr_val:.2f}) = {stop:.2f}; "
                 f"target = 2R = {target:.2f}"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# Convenience list for the Phase 12 backtest runner.
CANDIDATE_VARIANTS = [
    ("trend_pullback_relaxed_rsi", TrendPullbackRelaxedRSI()),
    ("trend_pullback_wider_zone", TrendPullbackWiderPullbackZone()),
    ("momentum_continuation_relaxed", MomentumContinuationRelaxed()),
]
