"""Phase 15 — entry-hypothesis candidates.

Each candidate tests ONE specific alternative to the existing
Phase 14 regime-aware strategies. Per Phase 15 brief req 3:
"Compare the existing rules with a small, predefined set of
alternatives" and "Do not loosen every condition simultaneously."

Candidates (dev/val only — OOS preserved per brief req 4):

  A. TrendPullbackWiderZone
     - Same regime filter (TRENDING_UP) as RegimeTrendPullback.
     - DIFFERENCE: pullback zone is [EMA50, EMA100] (deeper pullback)
       instead of [EMA50, EMA20] (shallow pullback).
     - Tests whether the entry hypothesis (shallow pullback) or the
       zone definition is the blocker.

  B. TrendPullbackPriceReversal
     - Same regime filter (TRENDING_UP) and pullback zone [EMA50, EMA20].
     - DIFFERENCE: requires 3+ consecutive declining closes BEFORE the
       current bar, AND current bar's close > prev close (reversal
       trigger). Tests whether a confirmed price-reversal trigger
       (instead of just RSI in range) unlocks signal generation.

  C. MeanReversionBB
     - Same regime filter (RANGE_BOUND) as RegimeMeanReversion.
     - DIFFERENCE: uses Bollinger Band touch (close <= BB_lower on
       prev bar) AND close back inside (close > BB_lower on current
       bar) as the entry trigger, instead of RSI crossing 35.
     - Tests whether Bollinger Band touches produce more setups than
       RSI crosses.

  D. MeanReversionDecliningCloses
     - Same regime filter (RANGE_BOUND).
     - DIFFERENCE: requires 3+ consecutive declining closes BEFORE
       the current bar, AND current bar's close > prev close (first
       up-close after the decline = initial reversal).
     - Tests whether a price-reversal trigger after declining closes
       produces more setups than RSI-based mean reversion.

  E. BreakoutRetestLevelStop
     - Same regime filter (TRENDING_UP or UNCERTAIN) as
       RegimeBreakoutRetest.
     - DIFFERENCE: stop is exactly at breakout_level * (1 - 0.005)
       (NOT min(breakout_level * 0.995, 2*ATR)). The tighter stop
       gives a HIGHER R/R after cost (potentially > 2.0), unlocking
       the risk gate that blocked RegimeBreakoutRetest.
     - Tests whether the risk geometry (wide stop) or the entry
       hypothesis is the blocker for breakout_retest.

  F. AdxRegimeClassifier
     - Alternative regime classifier using ADX (Phase 15 brief req 3D).
     - TRENDING_UP requires: ADX > 25 AND +DI > -DI AND EMA20 > EMA50.
     - RANGE_BOUND requires: ADX < 20.
     - This is wrapped as a Mixin that the A/B/C/D/E candidates can
       opt into, to test whether ADX-based classification produces
       more eligible bars than the EMA-stack classifier.
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
from .phase13_candidates import MeanReversionRsi as Phase13MeanReversion
from .regime_strategies import (
    RegimeTrendPullback, RegimeBreakoutRetest, RegimeMeanReversion,
)
from ..api.models import Candle, DataProvenance


# ===========================================================================
# A. TrendPullbackWiderZone
# ===========================================================================
class TrendPullbackWiderZone(RegimeTrendPullback):
    """Phase 15 candidate A: trend pullback with wider (deeper)
    pullback zone [EMA50, EMA100] instead of [EMA50, EMA20].

    Phase 16: now accepts a ``regime_classifier`` via constructor.
    Defaults to ``RegimeClassifier`` for backward compat.
    """
    name = "phase15_trend_pullback_wider_zone"
    warmup_candles = 200

    def __init__(self, *, regime_classifier=None):
        self._regime_classifier = regime_classifier or RegimeClassifier

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        snap = self._regime_classifier.classify_at(candles, indicators, i)
        if snap.regime is not MarketRegime.TRENDING_UP:
            sig.state = SignalState.WAIT
            sig.explanation = f"regime {snap.regime.value}; need TRENDING_UP"
            return sig
        sig.supporting.append(SignalReason(
            text=f"regime TRENDING_UP"))

        ema20 = indicators.ema20[i]
        ema50 = indicators.ema50[i]
        ema100 = indicators.ema100[i]
        last = candles[i]
        # WIDER pullback zone: price in [EMA50, EMA100] (deeper pullback)
        # Note: in an uptrend, EMA20 > EMA50 > EMA100, so the deeper
        # zone is [EMA100, EMA50] — price pulled back below EMA50 but
        # stayed above EMA100.
        if math.isnan(ema100):
            sig.state = SignalState.WAIT
            sig.explanation = "EMA100 not yet initialized"
            return sig
        if not (ema100 <= last.close <= ema50):
            sig.state = SignalState.WAIT
            sig.explanation = (f"price {last.close:.2f} not in wider "
                               f"pullback zone [EMA100={ema100:.2f}, "
                               f"EMA50={ema50:.2f}]")
            return sig
        sig.supporting.append(SignalReason(
            text=f"price {last.close:.2f} in wider pullback zone "
                 f"[EMA100={ema100:.2f}, EMA50={ema50:.2f}]"))

        # 2-ATR stop (same as RegimeTrendPullback)
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
            text=f"stop = entry - 2*ATR = {stop:.2f}; target = 2R = {target:.2f}"))
        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# ===========================================================================
# B. TrendPullbackPriceReversal
# ===========================================================================
class TrendPullbackPriceReversal(RegimeTrendPullback):
    """Phase 15 candidate B: trend pullback with explicit price-
    reversal trigger (3+ declining closes + first up-close).

    Phase 16: now accepts a ``regime_classifier`` via constructor.
    """
    name = "phase15_trend_pullback_price_reversal"
    warmup_candles = 200

    def __init__(self, *, regime_classifier=None):
        self._regime_classifier = regime_classifier or RegimeClassifier

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        snap = self._regime_classifier.classify_at(candles, indicators, i)
        if snap.regime is not MarketRegime.TRENDING_UP:
            sig.state = SignalState.WAIT
            sig.explanation = f"regime {snap.regime.value}; need TRENDING_UP"
            return sig
        sig.supporting.append(SignalReason(text="regime TRENDING_UP"))

        # Original shallow pullback zone [EMA50, EMA20]
        ema20 = indicators.ema20[i]
        ema50 = indicators.ema50[i]
        last = candles[i]
        if not (ema50 <= last.close <= ema20):
            sig.state = SignalState.WAIT
            sig.explanation = (f"price {last.close:.2f} not in pullback "
                               f"zone [EMA50={ema50:.2f}, EMA20={ema20:.2f}]")
            return sig
        sig.supporting.append(SignalReason(
            text=f"price {last.close:.2f} in pullback zone"))

        # Price-reversal trigger: 3+ consecutive declining closes BEFORE
        # the current bar, AND current close > prev close.
        if i < 4:
            sig.state = SignalState.WAIT
            sig.explanation = "need at least 4 bars for reversal trigger"
            return sig
        declining_count = 0
        for j in range(i - 1, max(i - 5, 0) - 1, -1):  # look back up to 4 bars
            if candles[j].close < candles[j - 1].close:
                declining_count += 1
            else:
                break
        if declining_count < 3:
            sig.state = SignalState.WAIT
            sig.explanation = (f"only {declining_count} consecutive declining "
                               "closes (need >= 3)")
            return sig
        if last.close <= candles[i - 1].close:
            sig.state = SignalState.WAIT
            sig.explanation = (f"close {last.close:.2f} not above prev "
                               f"{candles[i - 1].close:.2f}")
            return sig
        sig.supporting.append(SignalReason(
            text=f"price reversal: {declining_count} declining closes "
                 f"then up-close"))

        # 2-ATR stop
        atr_val = indicators.atr14[i]
        if math.isnan(atr_val) or atr_val <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "ATR not available"
            return sig
        entry = last.close
        stop = entry - 2.0 * atr_val
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry"
            return sig
        risk_per_unit = entry - stop
        target = entry + 2 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"stop = entry - 2*ATR = {stop:.2f}; target = 2R = {target:.2f}"))
        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# ===========================================================================
# C. MeanReversionBB
# ===========================================================================
class MeanReversionBB(RegimeMeanReversion):
    """Phase 15 candidate C: mean reversion using Bollinger Band
    touches (close <= BB_lower on prev bar) AND close back inside
    (close > BB_lower on current bar).

    Phase 16: now accepts a ``regime_classifier`` via constructor.
    """
    name = "phase15_mean_reversion_bb"
    warmup_candles = 200

    def __init__(self, *, regime_classifier=None):
        self._regime_classifier = regime_classifier or RegimeClassifier

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        snap = self._regime_classifier.classify_at(candles, indicators, i)
        if snap.regime is not MarketRegime.RANGE_BOUND:
            sig.state = SignalState.WAIT
            sig.explanation = f"regime {snap.regime.value}; need RANGE_BOUND"
            return sig
        sig.supporting.append(SignalReason(text="regime RANGE_BOUND"))

        if i < 1:
            sig.explanation = "need at least 2 bars"
            return sig
        bb_lower = indicators.bb_lower
        bb_upper = indicators.bb_upper
        bb_mid = indicators.bb_middle
        if math.isnan(bb_lower[i]) or math.isnan(bb_lower[i - 1]):
            sig.explanation = "Bollinger Bands warm-up incomplete"
            return sig

        # Trigger: prev close <= BB_lower (touch), current close > BB_lower (back inside)
        prev_close = candles[i - 1].close
        last_close = candles[i].close
        if prev_close > bb_lower[i - 1]:
            sig.state = SignalState.WAIT
            sig.explanation = (f"prev close {prev_close:.2f} did not touch "
                               f"BB_lower {bb_lower[i-1]:.2f}")
            return sig
        if last_close <= bb_lower[i]:
            sig.state = SignalState.WAIT
            sig.explanation = (f"close {last_close:.2f} still below BB_lower "
                               f"{bb_lower[i]:.2f} (not back inside)")
            return sig
        sig.supporting.append(SignalReason(
            text=f"BB touch (prev close {prev_close:.2f} <= BB_lower "
                 f"{bb_lower[i-1]:.2f}) and back inside (close "
                 f"{last_close:.2f} > BB_lower {bb_lower[i]:.2f})"))

        # 2-ATR stop
        atr_val = indicators.atr14[i]
        if math.isnan(atr_val) or atr_val <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "ATR not available"
            return sig
        entry = last_close
        stop = entry - 2.0 * atr_val
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry"
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
# D. MeanReversionDecliningCloses
# ===========================================================================
class MeanReversionDecliningCloses(RegimeMeanReversion):
    """Phase 15 candidate D: mean reversion using a price-reversal
    trigger after 3+ consecutive declining closes.

    Phase 16: now accepts a ``regime_classifier`` via constructor.
    """
    name = "phase15_mean_reversion_declining"
    warmup_candles = 200

    def __init__(self, *, regime_classifier=None):
        self._regime_classifier = regime_classifier or RegimeClassifier

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        snap = self._regime_classifier.classify_at(candles, indicators, i)
        if snap.regime is not MarketRegime.RANGE_BOUND:
            sig.state = SignalState.WAIT
            sig.explanation = f"regime {snap.regime.value}; need RANGE_BOUND"
            return sig
        sig.supporting.append(SignalReason(text="regime RANGE_BOUND"))

        if i < 4:
            sig.explanation = "need at least 4 bars"
            return sig
        # Count consecutive declining closes BEFORE the current bar
        declining_count = 0
        for j in range(i - 1, max(i - 5, 0) - 1, -1):
            if candles[j].close < candles[j - 1].close:
                declining_count += 1
            else:
                break
        if declining_count < 3:
            sig.state = SignalState.WAIT
            sig.explanation = (f"only {declining_count} consecutive declining "
                               "closes (need >= 3)")
            return sig

        # First up-close after the decline
        last_close = candles[i].close
        prev_close = candles[i - 1].close
        if last_close <= prev_close:
            sig.state = SignalState.WAIT
            sig.explanation = (f"close {last_close:.2f} not above prev "
                               f"{prev_close:.2f}")
            return sig
        sig.supporting.append(SignalReason(
            text=f"price reversal after {declining_count} declining closes"))

        # 2-ATR stop
        atr_val = indicators.atr14[i]
        if math.isnan(atr_val) or atr_val <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "ATR not available"
            return sig
        entry = last_close
        stop = entry - 2.0 * atr_val
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry"
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
# E. BreakoutRetestLevelStop
# ===========================================================================
class BreakoutRetestLevelStop(RegimeBreakoutRetest):
    """Phase 15 candidate E: breakout-retest with TIGHT breakout-level
    stop (0.5% below breakout level) instead of the wider
    min(breakout_level * 0.995, 2*ATR) stop.

    Phase 16: now accepts a ``regime_classifier`` via constructor.
    """
    name = "phase15_breakout_retest_level_stop"
    warmup_candles = 200

    def __init__(self, *, regime_classifier=None):
        self._regime_classifier = regime_classifier or RegimeClassifier

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        snap = self._regime_classifier.classify_at(candles, indicators, i)
        if snap.regime not in (MarketRegime.TRENDING_UP,
                                MarketRegime.UNCERTAIN):
            sig.state = SignalState.WAIT
            sig.explanation = f"regime {snap.regime.value}; need TRENDING_UP or UNCERTAIN"
            return sig
        sig.supporting.append(SignalReason(
            text=f"regime {snap.regime.value}"))

        br = indicators.breakout_retest_signal
        if not br.has_breakout or br.direction != "up":
            sig.state = SignalState.WAIT
            sig.explanation = "no upside breakout detected"
            return sig
        if not br.has_retest:
            sig.state = SignalState.WAIT
            sig.explanation = "breakout not yet retested"
            return sig
        sig.supporting.append(SignalReason(
            text=f"breakout UP above {br.breakout_level:.2f} @bar#{br.breakout_idx}; "
                 f"retest @bar#{br.retest_idx}"))

        entry = candles[-1].close
        # TIGHT stop: exactly at breakout_level * (1 - 0.005)
        # (no min() with 2*ATR — pure breakout-level stop)
        stop = br.breakout_level * (1 - 0.005)
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = (f"stop {stop:.2f} >= entry {entry:.2f}; "
                               "price already far above breakout level")
            return sig

        risk_per_unit = entry - stop
        target = entry + 2 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"stop = breakout_level * 0.995 = {stop:.2f}; "
                 f"target = 2R = {target:.2f}"))
        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# ===========================================================================
# F. DirectionalAdxClassifier (Phase 16 — proper ADX + DI classifier)
# ===========================================================================
class DirectionalAdxClassifier:
    """Phase 16 — ADX-based regime classifier that uses +DI vs -DI
    for direction (NOT EMA stack, NOT ADX alone).

    Phase 16 brief req 3: "Build a separate ADX-based classifier that
    considers: Trend strength using ADX; Direction using +DI versus -DI;
    Appropriate price or moving-average confirmation where justified;
    Sideways conditions; High-volatility conditions; Insufficient data
    and uncertain conditions."

    And: "ADX alone must never be treated as bullish or bearish direction."

    Classification rules (all use ONLY data available at bar i):
      TRENDING_UP    : ADX > 25 AND +DI > -DI (strong trend, direction up)
      TRENDING_DOWN  : ADX > 25 AND -DI > +DI (strong trend, direction down)
      RANGE_BOUND    : ADX < 20 (weak/no trend — sideways)
      HIGH_VOLATILITY: ATR/price >= 2.5% (unchanged threshold)
      UNCERTAIN      : default (e.g., 20 <= ADX <= 25, or +DI == -DI)

    The +DI/-DI directional indicators are now exposed from the ADX
    calculation (Phase 16: adx_with_di() + IndicatorSet.plus_di14/
    minus_di14 fields). This classifier reads them from the IndicatorSet.

    This classifier is SEPARATE from the existing RegimeClassifier
    (which uses EMA stack + ATR/price). Both are preserved — research
    candidates can be configured to use either one via the
    ``regime_classifier`` constructor parameter.
    """

    ADX_STRONG_THRESHOLD = 25.0    # ADX > this → strong trend
    ADX_WEAK_THRESHOLD = 20.0      # ADX < this → range-bound
    ATR_PCT_HIGH_VOL_THRESHOLD = 2.5
    # Minimum +DI/-DI difference to call direction (avoids noise when
    # +DI ≈ -DI).
    DI_MIN_DIFFERENCE = 1.0

    @classmethod
    def classify_at(cls, candles: List[Candle],
                     indicators: IndicatorSet,
                     i: int) -> RegimeSnapshot:
        """Classify using ADX (strength) + +DI/-DI (direction).
        Returns a RegimeSnapshot with the SAME structure as
        RegimeClassifier.classify_at so the same strategies can
        consume it."""
        if i < 0 or i >= len(candles):
            return RegimeSnapshot(
                regime=MarketRegime.UNCERTAIN,
                ema20=float("nan"), ema50=float("nan"),
                ema200=float("nan"), ema50_slope=float("nan"),
                atr14=float("nan"), close=candles[i].close if 0 <= i < len(candles) else 0.0,
                atr_pct=float("nan"), ema_spread_pct=float("nan"),
                ema50_to_200_pct=float("nan"), bar_idx=i)

        ema20 = indicators.ema20[i]
        ema50 = indicators.ema50[i]
        ema200 = indicators.ema200[i]
        atr14 = indicators.atr14[i]
        adx_val = indicators.adx14[i]
        plus_di = indicators.plus_di14[i]
        minus_di = indicators.minus_di14[i]
        close = candles[i].close

        # Compute ATR/price first — needed for HIGH_VOLATILITY which
        # only requires ATR + close (not ADX/DI/EMA200).
        atr_pct = (atr14 / close) * 100.0 if (close > 0 and not math.isnan(atr14)) else float("nan")
        ema50_prev = indicators.ema50[i - 1] if i >= 1 else ema50
        ema50_slope = (ema50 - ema50_prev) if not math.isnan(ema50_prev) else 0.0
        ema_spread_pct = (abs(ema20 - ema50) / ema50) * 100.0 if (ema50 > 0 and not math.isnan(ema20) and not math.isnan(ema50)) else float("nan")
        ema50_to_200_pct = (abs(ema50 - ema200) / ema200) * 100.0 if (ema200 > 0 and not math.isnan(ema50) and not math.isnan(ema200)) else float("nan")

        # 1. HIGH_VOLATILITY takes priority — needs ONLY ATR + close.
        # This check happens BEFORE the full indicator-availability
        # check so that high-volatility regimes are detected even
        # when ADX/+DI/-DI haven't initialized yet.
        if (not math.isnan(atr_pct)
                and atr_pct >= cls.ATR_PCT_HIGH_VOL_THRESHOLD):
            regime = MarketRegime.HIGH_VOLATILITY
        # 2. For trend/range classification, need ALL indicators.
        elif any(math.isnan(x) for x in
                  (ema20, ema50, ema200, atr14, close, adx_val,
                   plus_di, minus_di)):
            regime = MarketRegime.UNCERTAIN
        # 3. Strong trend (ADX > 25): use +DI vs -DI for direction.
        elif adx_val > cls.ADX_STRONG_THRESHOLD:
            if (plus_di > minus_di + cls.DI_MIN_DIFFERENCE
                    and plus_di > minus_di):
                regime = MarketRegime.TRENDING_UP
            elif (minus_di > plus_di + cls.DI_MIN_DIFFERENCE
                    and minus_di > plus_di):
                regime = MarketRegime.TRENDING_DOWN
            else:
                regime = MarketRegime.UNCERTAIN
        # 4. Weak trend (ADX < 20) → range-bound.
        elif adx_val < cls.ADX_WEAK_THRESHOLD:
            regime = MarketRegime.RANGE_BOUND
        # 5. 20 <= ADX <= 25 → uncertain (developing trend).
        else:
            regime = MarketRegime.UNCERTAIN

        return RegimeSnapshot(
            regime=regime,
            ema20=ema20, ema50=ema50, ema200=ema200,
            ema50_slope=ema50_slope, atr14=atr14, close=close,
            atr_pct=atr_pct, ema_spread_pct=ema_spread_pct,
            ema50_to_200_pct=ema50_to_200_pct, bar_idx=i)


# Backward-compat alias: the Phase 15 AdxRegimeClassifier used EMA stack
# for direction. Phase 16 replaces it with DirectionalAdxClassifier which
# uses +DI/-DI. The old name is kept as an alias so existing test imports
# don't break, but the implementation is now the Phase 16 version.
AdxRegimeClassifier = DirectionalAdxClassifier


# Convenience list for the Phase 15 backtest runner.
PHASE15_CANDIDATES = [
    ("phase15_trend_pullback_wider_zone", TrendPullbackWiderZone()),
    ("phase15_trend_pullback_price_reversal", TrendPullbackPriceReversal()),
    ("phase15_mean_reversion_bb", MeanReversionBB()),
    ("phase15_mean_reversion_declining", MeanReversionDecliningCloses()),
    ("phase15_breakout_retest_level_stop", BreakoutRetestLevelStop()),
]
