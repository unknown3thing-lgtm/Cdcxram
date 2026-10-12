"""Phase 14 — regime-aware strategy candidates.

Each candidate fires ONLY in its suitable regime, and uses wider
stops (2 ATR instead of 1) so the post-cost R/R is more favorable
than the existing strategies' 1-ATR stops.

Candidates:
  A. RegimeTrendPullback
     - Regime: TRENDING_UP
     - Entry: pullback to [EMA50, EMA20] on the LTF
     - Stop: 2 ATR below entry (wider than 1 ATR)
     - Target: 2R

  B. RegimeBreakoutRetest
     - Regime: TRENDING_UP or UNCERTAIN (transitioning into trend)
     - Entry: recent UP breakout + retest (uses Phase 12 fixed indicator)
     - Stop: below the breakout level * 0.998
     - Target: 2R

  C. RegimeMeanReversion
     - Regime: RANGE_BOUND (only — avoid trading mean-reversion in trends)
     - Entry: RSI(14) crossed from < 35 (prev bar) to > 35 (current bar)
              (relaxed from 30 to 35 to capture more setups)
     - Stop: 2 ATR below entry (NOT 10-bar low — ATR-based is more
              adaptive to current volatility)
     - Target: 2R

  D. RegimeMultiTimeframe
     - Regime: TRENDING_UP on BOTH HTF (4h) AND LTF (1h)
     - Entry: LTF pullback zone [EMA50, EMA20]
     - Stop: 2 ATR below entry
     - Target: 2R

The wider 2-ATR stop geometry is the key Phase 14 insight: with
fees (0.1% per side) + slippage (0.05% per side) ≈ 0.3% round-trip
cost, a 1-ATR stop is so tight that the post-cost R/R falls below
the 2.0 target. A 2-ATR stop has roughly half the cost-as-fraction-
of-risk, making post-cost R/R ~1.8+ (acceptable under the existing
reward_to_risk_target=2.0 OR the relaxed target of 1.5).

Production safeguards preserved:
  - Long-only (paper-trading is long-only).
  - Risk engine integration (fees, slippage, R/R after cost).
  - warmup_candles threshold.
  - No look-ahead (regime uses only candles[0..i]).
"""
from __future__ import annotations

import math
from typing import List, Optional

import numpy as np

from .indicators import IndicatorSet, TrendDirection, ema
from .regime import RegimeClassifier, MarketRegime, RegimeSnapshot
from .risk import RiskEngine, RiskState
from .signals import (
    Strategy, Signal, SignalState, SignalReason, TrendPullbackStrategy,
    BreakoutRetestStrategy,
)
from .phase13_candidates import (
    MeanReversionRsi as Phase13MeanReversion,
    MultiTimeframeTrendPullback as Phase13MTF,
)
from ..api.models import Candle, DataProvenance


# ===========================================================================
# Candidate A: RegimeTrendPullback
# ===========================================================================
class RegimeTrendPullback(TrendPullbackStrategy):
    """Trend pullback that fires ONLY in a TRENDING_UP regime.

    Differences from production TrendPullbackStrategy:
      1. Requires RegimeClassifier to classify the bar as
         MarketRegime.TRENDING_UP (not just EMA stack — also requires
         EMA50 slope > 0 and ATR/price < 2.5%).
      2. Uses 2-ATR stop instead of swing-low-based stop. This gives
         a wider stop that makes post-cost R/R more favorable.
      3. RSI range is RELAXED to [35, 60] (from [40, 55]) to capture
         more pullback opportunities within the trending regime.
    """
    name = "regime_trend_pullback"
    warmup_candles = 200  # needs EMA200

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete (needs 200 bars for EMA200)"
            return sig

        i = indicators.last_idx
        # --- Regime filter ---
        snap = RegimeClassifier.classify_at(candles, indicators, i)
        if snap.regime is not MarketRegime.TRENDING_UP:
            sig.state = SignalState.WAIT
            sig.explanation = (f"regime {snap.regime.value}; "
                               "need TRENDING_UP")
            return sig
        sig.supporting.append(SignalReason(
            text=f"regime TRENDING_UP (ATR%={snap.atr_pct:.2f}, "
                 f"EMA spread={snap.ema_spread_pct:.2f}%)"))

        # --- LTF pullback zone ---
        ema20 = indicators.ema20[i]
        ema50 = indicators.ema50[i]
        rsi = indicators.rsi14[i]
        last = candles[i]
        if not (ema50 <= last.close <= ema20):
            sig.state = SignalState.WAIT
            sig.explanation = (f"price {last.close:.2f} not in pullback "
                               f"zone [EMA50={ema50:.2f}, EMA20={ema20:.2f}]")
            return sig
        sig.supporting.append(SignalReason(
            text=f"price {last.close:.2f} in pullback zone"))

        # --- RSI in [35, 60] (relaxed from [40, 55]) ---
        if not (35 <= rsi <= 60):
            sig.conflicting.append(SignalReason(
                text=f"RSI {rsi:.1f} outside [35,60] (relaxed range)",
                is_supporting=False))
        else:
            sig.supporting.append(SignalReason(
                text=f"RSI {rsi:.1f} within [35,60]"))

        # --- Stop: 2 ATR below entry (wider than production 1.5 ATR) ---
        atr_val = indicators.atr14[i]
        if math.isnan(atr_val) or atr_val <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "ATR not available / non-positive"
            return sig
        entry = last.close
        stop = entry - 2.0 * atr_val   # 2 ATR stop (wider)
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry; invalid geometry"
            return sig
        risk_per_unit = entry - stop
        target = entry + 2 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"stop = entry - 2*ATR({atr_val:.2f}) = {stop:.2f}; "
                 f"target = 2R = {target:.2f}"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# ===========================================================================
# Candidate B: RegimeBreakoutRetest
# ===========================================================================
class RegimeBreakoutRetest(BreakoutRetestStrategy):
    """Breakout-retest that fires in TRENDING_UP or UNCERTAIN regime
    (NOT in HIGH_VOLATILITY — extreme volatility whipsaws breakouts).

    Inherits the Phase 12 fixed breakout_retest indicator (finds the
    most-recent UP breakout). Adds a regime filter and a wider stop.
    """
    name = "regime_breakout_retest"
    warmup_candles = 200  # needs EMA200 for regime classification

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        # --- Regime filter: TRENDING_UP or UNCERTAIN ---
        snap = RegimeClassifier.classify_at(candles, indicators, i)
        if snap.regime not in (MarketRegime.TRENDING_UP,
                                MarketRegime.UNCERTAIN):
            sig.state = SignalState.WAIT
            sig.explanation = (f"regime {snap.regime.value}; "
                               "need TRENDING_UP or UNCERTAIN")
            return sig
        sig.supporting.append(SignalReason(
            text=f"regime {snap.regime.value}"))

        # --- Breakout + retest (uses Phase 12 fixed indicator) ---
        br = indicators.breakout_retest_signal
        if not br.has_breakout or br.direction != "up":
            sig.state = SignalState.WAIT
            sig.explanation = "no upside breakout detected in recent window"
            return sig
        if not br.has_retest:
            sig.state = SignalState.WAIT
            sig.explanation = "breakout not yet retested"
            return sig
        sig.supporting.append(SignalReason(
            text=f"breakout UP above {br.breakout_level:.2f} @bar#{br.breakout_idx}; "
                 f"retest @bar#{br.retest_idx}"))

        # --- Stop and target ---
        # Use 2 ATR stop OR breakout_level - 0.5%, whichever is wider
        # (more conservative).
        entry = candles[-1].close
        atr_val = indicators.atr14[i]
        if math.isnan(atr_val) or atr_val <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "ATR not available"
            return sig
        stop_breakout = br.breakout_level * (1 - 0.005)
        stop_atr = entry - 2.0 * atr_val
        # Take the LOWER (wider) stop — more conservative.
        stop = min(stop_breakout, stop_atr)
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry; invalid geometry"
            return sig

        risk_per_unit = entry - stop
        target = entry + 2 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"stop = min(breakout-0.5%, 2*ATR) = {stop:.2f}; "
                 f"target = 2R = {target:.2f}"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# ===========================================================================
# Candidate C: RegimeMeanReversion
# ===========================================================================
class RegimeMeanReversion(Phase13MeanReversion):
    """Mean reversion that fires ONLY in RANGE_BOUND regime.

    Key differences from Phase 13 MeanReversionRsi:
      1. Requires regime = RANGE_BOUND (prevents trading against
         strong trends — the Phase 13 version had no regime filter).
      2. RSI threshold relaxed from 30 to 35 (captures more setups
         while still requiring true oversold conditions).
      3. Stop is 2 ATR below entry (not 10-bar low) — adaptive to
         current volatility regime.
    """
    name = "regime_mean_reversion"
    warmup_candles = 200  # needs EMA200 for regime classification

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        # --- Regime filter: RANGE_BOUND only ---
        snap = RegimeClassifier.classify_at(candles, indicators, i)
        if snap.regime is not MarketRegime.RANGE_BOUND:
            sig.state = SignalState.WAIT
            sig.explanation = (f"regime {snap.regime.value}; "
                               "need RANGE_BOUND")
            return sig
        sig.supporting.append(SignalReason(
            text=f"regime RANGE_BOUND (ATR%={snap.atr_pct:.2f}%, "
                 f"EMA spread={snap.ema_spread_pct:.2f}%)"))

        # --- RSI recovery trigger (relaxed from 30 to 35) ---
        if i < 1:
            sig.explanation = "need at least 2 bars for recovery trigger"
            return sig
        rsi_now = indicators.rsi14[i]
        rsi_prev = indicators.rsi14[i - 1]
        if math.isnan(rsi_now) or math.isnan(rsi_prev):
            sig.explanation = "RSI warm-up incomplete"
            return sig

        # Relaxed threshold: prev < 35, now > 35
        if not (rsi_prev < 35 and rsi_now > 35):
            sig.state = SignalState.WAIT
            sig.explanation = (f"RSI did not cross 35 (prev={rsi_prev:.1f}, "
                               f"now={rsi_now:.1f})")
            return sig
        sig.supporting.append(SignalReason(
            text=f"RSI crossed above 35 (prev={rsi_prev:.1f}, "
                 f"now={rsi_now:.1f})"))

        # --- Close > prev close (initial bounce) ---
        prev_close = candles[i - 1].close
        last_close = candles[i].close
        if last_close <= prev_close:
            sig.state = SignalState.WAIT
            sig.explanation = (f"close {last_close:.2f} not above prev "
                               f"{prev_close:.2f}")
            return sig
        sig.supporting.append(SignalReason(
            text=f"close {last_close:.2f} > prev {prev_close:.2f}"))

        # --- Stop: 2 ATR below entry ---
        atr_val = indicators.atr14[i]
        if math.isnan(atr_val) or atr_val <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "ATR not available"
            return sig
        entry = last_close
        stop = entry - 2.0 * atr_val
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry; invalid geometry"
            return sig

        risk_per_unit = entry - stop
        target = entry + 2 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"target {target:.2f} = 2R; stop {stop:.2f} (2*ATR)"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# ===========================================================================
# Candidate D: RegimeMultiTimeframe
# ===========================================================================
class RegimeMultiTimeframe(Phase13MTF):
    """Multi-timeframe strategy that fires ONLY when BOTH the HTF (4h)
    AND LTF (1h) classify as TRENDING_UP.

    Inherits the Phase 13 MultiTimeframeTrendPullback (binary-search
    HTF trend lookup, no look-ahead) and adds:
      1. LTF regime classification (must be TRENDING_UP).
      2. 2-ATR stop instead of swing-low-based stop.
    """
    name = "regime_mtf_trend_pullback"
    warmup_candles = 200

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        ltf_open_time_ms = candles[i].open_time_ms

        # --- HTF trend filter (uses parent's binary search) ---
        htf_trend = self._htf_trend_at(ltf_open_time_ms)
        if htf_trend is not TrendDirection.UP:
            sig.state = SignalState.WAIT
            sig.explanation = f"HTF (4h) trend {htf_trend.value}; need UP"
            return sig
        sig.supporting.append(SignalReason(
            text="HTF (4h) trend UP"))

        # --- LTF regime filter: must be TRENDING_UP ---
        snap = RegimeClassifier.classify_at(candles, indicators, i)
        if snap.regime is not MarketRegime.TRENDING_UP:
            sig.state = SignalState.WAIT
            sig.explanation = (f"LTF regime {snap.regime.value}; "
                               "need TRENDING_UP")
            return sig
        sig.supporting.append(SignalReason(
            text=f"LTF regime TRENDING_UP"))

        # --- LTF pullback zone ---
        ema20 = indicators.ema20[i]
        ema50 = indicators.ema50[i]
        last = candles[i]
        if not (ema50 <= last.close <= ema20):
            sig.state = SignalState.WAIT
            sig.explanation = (f"price {last.close:.2f} not in pullback "
                               f"zone [EMA50={ema50:.2f}, EMA20={ema20:.2f}]")
            return sig
        sig.supporting.append(SignalReason(
            text=f"price {last.close:.2f} in LTF pullback zone"))

        # --- Stop: 2 ATR below entry ---
        atr_val = indicators.atr14[i]
        if math.isnan(atr_val) or atr_val <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "ATR not available"
            return sig
        entry = last.close
        stop = entry - 2.0 * atr_val
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry; invalid geometry"
            return sig

        risk_per_unit = entry - stop
        target = entry + 2 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"stop = entry - 2*ATR({atr_val:.2f}) = {stop:.2f}; "
                 f"target = 2R = {target:.2f}"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# Convenience list for the Phase 14 backtest runner.
# The MTF strategy needs HTF candles, so it's not in this list —
# the runner constructs it per-pair.
REGIME_CANDIDATES_NO_HTF = [
    ("regime_trend_pullback", RegimeTrendPullback()),
    ("regime_breakout_retest", RegimeBreakoutRetest()),
    ("regime_mean_reversion", RegimeMeanReversion()),
]
