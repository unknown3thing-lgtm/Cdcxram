"""Phase 14 — market-regime classifier.

Classifies each candle into one of:
  - TRENDING_UP    : EMA20 > EMA50 > EMA200 AND EMA50 rising
  - TRENDING_DOWN  : EMA20 < EMA50 < EMA200 AND EMA50 falling
  - RANGE_BOUND    : EMAs roughly equal (within 1% of each other)
                     AND price oscillates around EMA50
  - HIGH_VOLATILITY: ATR/price > 2.5% (high intraday range)
  - UNCERTAIN      : anything else (default — most choppy periods)

The classifier uses ONLY completed candles (no look-ahead). It is
conservative — when in doubt, classify as UNCERTAIN. Strategies can
choose which regimes they're willing to trade in.

The classifier is designed to be MEASURABLE and TESTABLE: each rule
has explicit thresholds documented in the class docstring.

Phase 14 brief req 2: "Avoid excessive filters that leave almost no
eligible trades. Also avoid forcing trades when the data does not
support a setup." The HIGH_VOLATILITY regime is NOT a hard no-trade
filter — it's a classification that strategies can use to adjust
behavior (e.g., wider stops, smaller position sizing).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

import numpy as np

from .indicators import IndicatorSet, TrendDirection
from ..api.models import Candle


class MarketRegime(str, Enum):
    """Market regime classification."""
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE_BOUND = "range_bound"
    HIGH_VOLATILITY = "high_volatility"
    UNCERTAIN = "uncertain"


@dataclass
class RegimeSnapshot:
    """A point-in-time regime classification for a single bar."""
    regime: MarketRegime
    # Supporting diagnostics (for tests + explanations)
    ema20: float
    ema50: float
    ema200: float
    ema50_slope: float        # ema50[i] - ema50[i-1]
    atr14: float
    close: float
    atr_pct: float            # atr14 / close * 100
    ema_spread_pct: float     # |ema20 - ema50| / ema50 * 100
    ema50_to_200_pct: float   # |ema50 - ema200| / ema200 * 100
    # The bar index at which this classification was made.
    bar_idx: int


class RegimeClassifier:
    """Classifies a candle series into regimes at each bar.

    Classification rules (all use ONLY data available at bar i,
    computed from candles[0..i]):

      TRENDING_UP:
        * EMA20 > EMA50 > EMA200 (strict UP stack) AND all defined.
        * EMA50 slope > 0 (rising over the last bar).
        * ATR/price < 2.5% (not in high-volatility regime).

      TRENDING_DOWN:
        * EMA20 < EMA50 < EMA200 (strict DOWN stack) AND all defined.
        * EMA50 slope < 0 (falling over the last bar).
        * ATR/price < 2.5%.

      RANGE_BOUND:
        * EMA spread < 1%: |EMA20 - EMA50| / EMA50 < 1% AND
          |EMA50 - EMA200| / EMA200 < 1%.
        * ATR/price < 2.5%.
        * If the EMAs are NOT in a strict UP or DOWN stack and the
          spread is small, this is range-bound.

      HIGH_VOLATILITY:
        * ATR/price >= 2.5% (high intraday range relative to price).
        * This regime is informational — strategies may still trade
          but should use wider stops.

      UNCERTAIN:
        * Default — anything not matching the above. Most choppy
          periods where EMAs are partially ordered but not strictly
          stacked will fall here.

    The HIGH_VOLATILITY regime takes priority over the trend regimes
    (a trending market with extreme volatility is classified as
    HIGH_VOLATILITY, not TRENDING_UP). This is intentional — extreme
    volatility warrants extra caution regardless of trend.

    Thresholds are intentionally SIMPLE and MEASURABLE per Phase 14
    brief req 2. They're documented here so they can be tested and
    tuned (in dev/val only — never on OOS) in future phases.
    """

    # Tunable thresholds (kept as class attributes for easy testing).
    ATR_PCT_HIGH_VOL_THRESHOLD = 2.5     # % — ATR/price >= this → HIGH_VOL
    EMA_SPREAD_RANGE_THRESHOLD = 1.0     # % — |EMA20-EMA50|/EMA50 < this → range
    EMA50_TO_200_RANGE_THRESHOLD = 1.0   # % — |EMA50-EMA200|/EMA200 < this → range
    EMA50_SLOPE_MIN = 0.0                # > 0 → rising, < 0 → falling

    @classmethod
    def classify_at(cls, candles: List[Candle],
                     indicators: IndicatorSet,
                     i: int) -> RegimeSnapshot:
        """Classify the regime at bar i using only candles[0..i].

        Returns a RegimeSnapshot with the classification and all
        supporting diagnostics. If indicators aren't yet initialized
        at bar i (e.g., EMA200 needs 200 bars), returns UNCERTAIN.
        """
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
        close = candles[i].close

        # If EMA200 isn't initialized yet (< 200 bars), classify as UNCERTAIN.
        # (Trend classification needs the slowest EMA.)
        if any(math.isnan(x) for x in (ema20, ema50, ema200, atr14, close)):
            return RegimeSnapshot(
                regime=MarketRegime.UNCERTAIN,
                ema20=ema20, ema50=ema50, ema200=ema200,
                ema50_slope=float("nan"), atr14=atr14, close=close,
                atr_pct=float("nan") if math.isnan(atr14) or close == 0 else atr14 / close * 100,
                ema_spread_pct=float("nan"),
                ema50_to_200_pct=float("nan"),
                bar_idx=i)

        # EMA50 slope (last bar's change)
        ema50_prev = indicators.ema50[i - 1] if i >= 1 else ema50
        if math.isnan(ema50_prev):
            ema50_slope = 0.0
        else:
            ema50_slope = ema50 - ema50_prev

        # Diagnostic metrics
        atr_pct = (atr14 / close) * 100.0 if close > 0 else 0.0
        ema_spread_pct = (abs(ema20 - ema50) / ema50) * 100.0 if ema50 > 0 else 0.0
        ema50_to_200_pct = (abs(ema50 - ema200) / ema200) * 100.0 if ema200 > 0 else 0.0

        # --- Classification logic ---
        # 1. HIGH_VOLATILITY takes priority (extreme vol = caution).
        if atr_pct >= cls.ATR_PCT_HIGH_VOL_THRESHOLD:
            regime = MarketRegime.HIGH_VOLATILITY
        # 2. TRENDING_UP: strict stack + rising EMA50.
        elif (ema20 > ema50 > ema200
              and ema50_slope > cls.EMA50_SLOPE_MIN):
            regime = MarketRegime.TRENDING_UP
        # 3. TRENDING_DOWN: strict inverse stack + falling EMA50.
        elif (ema20 < ema50 < ema200
              and ema50_slope < cls.EMA50_SLOPE_MIN):
            regime = MarketRegime.TRENDING_DOWN
        # 4. RANGE_BOUND: EMAs roughly equal (small spread).
        elif (ema_spread_pct < cls.EMA_SPREAD_RANGE_THRESHOLD
              and ema50_to_200_pct < cls.EMA50_TO_200_RANGE_THRESHOLD):
            regime = MarketRegime.RANGE_BOUND
        # 5. UNCERTAIN: default.
        else:
            regime = MarketRegime.UNCERTAIN

        return RegimeSnapshot(
            regime=regime,
            ema20=ema20, ema50=ema50, ema200=ema200,
            ema50_slope=ema50_slope, atr14=atr14, close=close,
            atr_pct=atr_pct, ema_spread_pct=ema_spread_pct,
            ema50_to_200_pct=ema50_to_200_pct, bar_idx=i)

    @classmethod
    def classify_series(cls, candles: List[Candle],
                        indicators: IndicatorSet) -> List[RegimeSnapshot]:
        """Classify the regime at every bar in the series."""
        return [cls.classify_at(candles, indicators, i)
                for i in range(len(candles))]

    @classmethod
    def regime_counts(cls, snapshots: List[RegimeSnapshot]) -> dict:
        """Count occurrences of each regime. Useful for reporting
        what fraction of bars fall into each classification."""
        counts = {r: 0 for r in MarketRegime}
        for s in snapshots:
            counts[s.regime] += 1
        return counts
