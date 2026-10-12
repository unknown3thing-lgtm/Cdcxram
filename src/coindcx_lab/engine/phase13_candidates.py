"""Phase 13 — new candidate strategy designs.

Two new candidate strategies for Phase 13 evaluation:

  1. MultiTimeframeTrendPullback — uses higher-timeframe (4h) trend
     confirmation + lower-timeframe (1h) pullback entry. Production
     defaults remain unchanged; this is a separate candidate evaluated
     on dev/val only (OOS preserved per Phase 13 brief req 3).

  2. MeanReversionRsi — explicit mean-reversion rules:
     - Entry: RSI(14) < 30 (oversold) on a 1h bar where close > previous
       close (initial recovery signal).
     - Stop: below the lowest low of the last 10 bars.
     - Target: 2R (risk-defined) — NOT a price-target mean-reversion
       to EMA50, because such targets vary and the risk engine needs
       a fixed geometric target to compute R/R after cost.
     - Exit rules: stop-loss, target, or RSI > 55 (exit signal).

Both candidates inherit from the existing Strategy base class so they
plug into the existing SignalEngine and Backtester unchanged.

Production safeguards preserved:
  - Long-only (paper-trading is long-only by design).
  - Risk engine integration (fees, slippage, R/R after cost, daily
    loss / drawdown / max-positions limits).
  - warmup_candles threshold (prevents evaluation before indicators
    are seeded).
"""
from __future__ import annotations

import math
from typing import List, Optional

import numpy as np

from .indicators import IndicatorSet, TrendDirection, ema
from .risk import RiskEngine, RiskState
from .signals import (
    Strategy, Signal, SignalState, SignalReason, TrendPullbackStrategy,
)
from ..api.models import Candle, DataProvenance


# ===========================================================================
# Candidate 1: MultiTimeframeTrendPullback
# ===========================================================================
class MultiTimeframeTrendPullback(Strategy):
    """Multi-timeframe candidate: 4h trend filter + 1h pullback entry.

    Rules:
      * HTF (4h) trend filter: EMA20 > EMA50 > EMA200 on the 4h series
        at the most-recent-completed 4h bar.
      * LTF (1h) entry: when HTF trend is UP, look for a pullback on
        the 1h series — price in [EMA50_1h, EMA20_1h].
      * Stop: below the most-recent 1h swing low * 0.998 (small buffer).
      * Target: 2R (entry + 2 * (entry - stop)).
      * Exit: stop-loss, target, or end-of-data (force-close).

    The HTF (4h) candle series is supplied at construction time. The
    strategy uses it ONLY for trend filtering — never for entry timing
    or trade scoring. This prevents look-ahead bias because at any
    LTF bar i, the HTF trend is computed using only HTF bars whose
    close_time <= LTF bar i's open_time.
    """
    name = "mtf_trend_pullback"
    warmup_candles = 200  # LTF (1h) warmup

    def __init__(self, *, htf_candles: List[Candle], htf_interval_ms: int = 14_400_000):
        """Construct with the higher-timeframe candle series.

        Args:
            htf_candles: full 4h candle series (used for HTF trend
                filtering). The strategy uses the prefix up to the
                most-recent-completed 4h bar at each LTF evaluation.
            htf_interval_ms: 4h interval in ms (default 14_400_000).
        """
        self.htf_candles = sorted(htf_candles, key=lambda c: c.open_time_ms)
        self.htf_interval_ms = htf_interval_ms
        # Pre-compute EMA20, EMA50, EMA200 on the full HTF series.
        # These arrays are indexed by HTF bar index.
        htf_closes = np.array([c.close for c in self.htf_candles], dtype=float)
        self._htf_ema20 = ema(htf_closes, 20)
        self._htf_ema50 = ema(htf_closes, 50)
        self._htf_ema200 = ema(htf_closes, 200)
        self._htf_open_times = [c.open_time_ms for c in self.htf_candles]

    def _htf_trend_at(self, ltf_open_time_ms: int) -> TrendDirection:
        """Get the HTF trend at the most-recent-completed HTF bar
        whose close_time <= ltf_open_time_ms.

        Returns TrendDirection.UP only if EMA20 > EMA50 > EMA200 are
        ALL defined at that HTF bar (no NaN). This prevents using
        HTF data that hasn't been completed yet (no look-ahead).
        """
        # Find the largest HTF bar index where close_time <= ltf_open_time_ms.
        # close_time = open_time + interval - 1.
        threshold = ltf_open_time_ms
        # Binary search: find rightmost HTF bar with close_time <= threshold
        lo, hi = 0, len(self._htf_open_times) - 1
        result_idx = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            close_time = (self._htf_open_times[mid]
                            + self.htf_interval_ms - 1)
            if close_time <= threshold:
                result_idx = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if result_idx < 0:
            return TrendDirection.FLAT
        i = result_idx
        if i < 199:  # need at least 200 bars to seed EMA200
            return TrendDirection.FLAT
        try:
            if (np.isnan(self._htf_ema20[i]) or np.isnan(self._htf_ema50[i])
                    or np.isnan(self._htf_ema200[i])):
                return TrendDirection.FLAT
        except (IndexError, TypeError):
            return TrendDirection.FLAT
        if (self._htf_ema20[i] > self._htf_ema50[i]
                > self._htf_ema200[i]):
            return TrendDirection.UP
        return TrendDirection.FLAT

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete (1h needs 200 bars)"
            return sig

        i = indicators.last_idx
        ltf_open_time_ms = candles[i].open_time_ms

        # --- HTF trend filter ---
        htf_trend = self._htf_trend_at(ltf_open_time_ms)
        if htf_trend is not TrendDirection.UP:
            sig.state = SignalState.WAIT
            sig.explanation = f"HTF trend {htf_trend.value}; need UP"
            return sig
        sig.supporting.append(SignalReason(
            text=f"HTF (4h) trend UP at LTF bar open_time={ltf_open_time_ms}"))

        # --- LTF (1h) pullback zone ---
        ema20 = indicators.ema20[i]
        ema50 = indicators.ema50[i]
        last = candles[i]
        if not (ema50 <= last.close <= ema20):
            sig.state = SignalState.WAIT
            sig.explanation = (f"price {last.close:.2f} not in LTF pullback "
                               f"zone [EMA50={ema50:.2f}, EMA20={ema20:.2f}]")
            return sig
        sig.supporting.append(SignalReason(
            text=f"price {last.close:.2f} in LTF pullback zone"))

        # --- Stop and target ---
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
            text=f"target {target:.2f} = 2R; stop {stop:.2f}"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# ===========================================================================
# Candidate 2: MeanReversionRsi
# ===========================================================================
class MeanReversionRsi(Strategy):
    """Mean-reversion candidate: RSI(14) < 30 (oversold) with
    initial-recovery trigger.

    Rules:
      * Entry: RSI(14) on the 1h series was < 30 on the previous bar
        AND is now > 30 on the current bar (recovery trigger).
        AND price closed above previous bar's close (initial bounce).
      * Stop: below the lowest low of the last 10 bars.
      * Target: 2R (entry + 2 * (entry - stop)).
      * Exit: stop-loss, target, or RSI > 55 (mean-reversion complete).
      * If both stop and target fall in the same bar, conservative
        stop-first (handled by backtester, not the strategy).

    This is a true mean-reversion design (not just a trend-following
    strategy with relaxed RSI). The entry is anchored on the
    oversold/recovery crossover, not on a trend filter.
    """
    name = "mean_reversion_rsi"
    warmup_candles = 60  # needs RSI(14) + 10-bar low lookback

    def evaluate(self, *, pair, interval, candles, indicators,
                 risk_engine, risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        if i < 1:
            sig.explanation = "need at least 2 bars for recovery trigger"
            return sig
        rsi_now = indicators.rsi14[i]
        rsi_prev = indicators.rsi14[i - 1]
        if math.isnan(rsi_now) or math.isnan(rsi_prev):
            sig.explanation = "RSI warm-up incomplete"
            return sig

        # Entry condition: RSI crossed from below 30 to above 30
        # (recovery trigger). Plus close > prev close (initial bounce).
        prev_close = candles[i - 1].close
        last_close = candles[i].close

        if not (rsi_prev < 30 and rsi_now > 30):
            sig.state = SignalState.WAIT
            sig.explanation = (f"RSI did not cross 30 (prev={rsi_prev:.1f}, "
                               f"now={rsi_now:.1f})"
                               if not (rsi_prev < 30 and rsi_now > 30)
                               else "filtered")
            return sig
        sig.supporting.append(SignalReason(
            text=f"RSI crossed above 30 (prev={rsi_prev:.1f}, now={rsi_now:.1f})"))

        if last_close <= prev_close:
            sig.state = SignalState.WAIT
            sig.explanation = (f"close {last_close:.2f} not above prev "
                               f"{prev_close:.2f}")
            return sig
        sig.supporting.append(SignalReason(
            text=f"close {last_close:.2f} > prev {prev_close:.2f}"))

        # Stop: below the lowest low of the last 10 bars.
        lookback = 10
        if i < lookback:
            sig.state = SignalState.WAIT
            sig.explanation = "not enough bars for 10-bar low stop"
            return sig
        recent_lows = [candles[j].low for j in range(i - lookback + 1, i + 1)]
        lowest_low = min(recent_lows)
        stop = lowest_low * 0.998  # small buffer below
        entry = last_close
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop >= entry; invalid geometry"
            return sig

        # Target: 2R
        risk_per_unit = entry - stop
        target = entry + 2 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"target {target:.2f} = 2R; stop {stop:.2f} (10-bar low)"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                          target=target,
                                          risk_engine=risk_engine,
                                          risk_state=risk_state)


# Convenience list for the Phase 13 backtest runner.
def make_phase13_candidates(htf_candles_by_pair):
    """Factory: returns a list of (name, strategy) tuples for the
    Phase 13 candidates. The MTF strategy needs HTF candles per pair,
    so the factory takes a pair -> HTF candles dict."""
    out = []
    for pair, htf_candles in htf_candles_by_pair.items():
        out.append((
            f"mtf_trend_pullback__{pair}",
            MultiTimeframeTrendPullback(htf_candles=htf_candles),
        ))
    # MeanReversionRsi doesn't need HTF candles — same instance works
    # for all pairs (it uses only LTF candles passed at evaluate()).
    out.append(("mean_reversion_rsi", MeanReversionRsi()))
    return out
