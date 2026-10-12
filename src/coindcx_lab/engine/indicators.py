"""Transparent, tested technical indicators.

All functions accept a list of Candle objects (or a pandas Series / DataFrame
adapter) and return numpy arrays aligned to the input. Each indicator is
implemented from first principles (no external TA library) so that:

    - the calculation is auditable
    - the test suite can verify correctness on hand-checked fixtures
    - warm-up periods are explicit (NaN for the first N-1 bars)
    - there is zero look-ahead: every output at index i uses only candles
      with index <= i

Indicators implemented:
    - ema(candles, period)
    - rsi(candles, period=14)
    - atr(candles, period=14)
    - macd(candles, fast=12, slow=26, signal=9)
    - swing_highs_lows(candles, lookback=2)
    - support_resistance(candles, lookback=20)
    - relative_volume(candles, period=20)
    - rolling_volatility(candles, period=20)
    - breakout_retest(candles, sr_levels, lookback=20)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

import numpy as np

from ..api.models import Candle


class TrendDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------
def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average. Returns array of same length; first
    `period-1` entries are NaN (warm-up)."""
    if period < 1:
        raise ValueError("EMA period must be >= 1")
    n = len(values)
    out = np.full(n, np.nan, dtype=float)
    if n == 0:
        return out
    alpha = 2.0 / (period + 1)
    # Seed with SMA of first `period` values (standard convention).
    if n < period:
        return out
    seed = float(np.mean(values[:period]))
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = alpha * float(values[i]) + (1 - alpha) * out[i - 1]
    return out


def ema_close(candles: List[Candle], period: int) -> np.ndarray:
    closes = np.array([c.close for c in candles], dtype=float)
    return ema(closes, period)


# ---------------------------------------------------------------------------
# RSI (Wilder's smoothing)
# ---------------------------------------------------------------------------
def rsi(candles: List[Candle], period: int = 14) -> np.ndarray:
    """Wilder's RSI. NaN for first `period` entries."""
    n = len(candles)
    out = np.full(n, np.nan, dtype=float)
    if n <= period:
        return out
    deltas = np.diff([c.close for c in candles])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    # Wilder smoothing: first avg = simple mean of first `period` changes.
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, n):
        g = float(gains[i - 1])
        l = float(losses[i - 1])
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


# ---------------------------------------------------------------------------
# ATR (Wilder)
# ---------------------------------------------------------------------------
def atr(candles: List[Candle], period: int = 14) -> np.ndarray:
    """Average True Range (Wilder). NaN for first `period` entries."""
    n = len(candles)
    out = np.full(n, np.nan, dtype=float)
    if n <= period:
        return out
    tr = np.zeros(n)
    tr[0] = candles[0].high - candles[0].low
    for i in range(1, n):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    # First ATR is simple mean of TR[1..period]
    first_atr = float(np.mean(tr[1:period + 1]))
    out[period] = first_atr
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------
def macd(candles: List[Candle], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns (macd_line, signal_line, histogram). All aligned to input."""
    closes = np.array([c.close for c in candles], dtype=float)
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = ema_fast - ema_slow
    # signal = EMA of macd_line, but only where macd_line is defined
    valid = ~np.isnan(macd_line)
    sig = np.full(len(closes), np.nan, dtype=float)
    if valid.any():
        sig_valid = ema(macd_line[valid], signal)
        sig[valid] = sig_valid
    hist = macd_line - sig
    return macd_line, sig, hist


# ---------------------------------------------------------------------------
# Swing highs/lows
# ---------------------------------------------------------------------------
def swing_highs_lows(candles: List[Candle], lookback: int = 2
                     ) -> Tuple[List[Optional[float]], List[Optional[float]],
                                List[Optional[int]], List[Optional[int]]]:
    """Detect swing highs/lows using a pivot of `lookback` bars on each side.

    Returns four parallel arrays of length len(candles):
        swing_high_prices, swing_low_prices, swing_high_idx, swing_low_idx

    A swing high at index i means candles[i].high is the strict maximum of
    candles[i-lookback .. i+lookback] (and there is at least one bar on
    each side, i.e. lookback <= i < n-lookback).
    """
    n = len(candles)
    sh_p = [None] * n
    sl_p = [None] * n
    sh_i = [None] * n
    sl_i = [None] * n
    if lookback < 1:
        return sh_p, sl_p, sh_i, sl_i
    for i in range(lookback, n - lookback):
        window = candles[i - lookback: i + lookback + 1]
        highs = [w.high for w in window]
        lows = [w.low for w in window]
        if max(highs) == candles[i].high and highs.count(candles[i].high) == 1:
            sh_p[i] = candles[i].high
            sh_i[i] = i
        if min(lows) == candles[i].low and lows.count(candles[i].low) == 1:
            sl_p[i] = candles[i].low
            sl_i[i] = i
    return sh_p, sl_p, sh_i, sl_i


# ---------------------------------------------------------------------------
# Support/resistance levels (recent extreme pivot levels)
# ---------------------------------------------------------------------------
def support_resistance(candles: List[Candle], lookback: int = 20
                       ) -> Tuple[List[float], List[float]]:
    """Return (resistance_levels, support_levels) from the last `lookback`
    pivots. Levels are sorted by recency.

    Implementation: gather swing_highs and swing_lows over the whole series,
    keep the most recent `lookback` of each.
    """
    sh, sl, shi, sli = swing_highs_lows(candles, lookback=2)
    highs = [(idx, p) for idx, p in zip(shi, sh) if p is not None]
    lows = [(idx, p) for idx, p in zip(sli, sl) if p is not None]
    highs_sorted = sorted(highs, key=lambda x: -x[0])[:lookback]
    lows_sorted = sorted(lows, key=lambda x: -x[0])[:lookback]
    return [p for _, p in highs_sorted], [p for _, p in lows_sorted]


# ---------------------------------------------------------------------------
# Relative volume (current bar volume / average of last N)
# ---------------------------------------------------------------------------
def relative_volume(candles: List[Candle], period: int = 20) -> np.ndarray:
    n = len(candles)
    out = np.full(n, np.nan, dtype=float)
    if n < period + 1:
        return out
    vols = np.array([c.volume for c in candles], dtype=float)
    for i in range(period, n):
        avg = float(np.mean(vols[i - period:i]))
        if avg > 0:
            out[i] = vols[i] / avg
    return out


# ---------------------------------------------------------------------------
# Rolling volatility (std of log-returns, annualized by sqrt of bars per year)
# ---------------------------------------------------------------------------
def rolling_volatility(candles: List[Candle], period: int = 20,
                       bars_per_year: int = 24 * 365) -> np.ndarray:
    n = len(candles)
    out = np.full(n, np.nan, dtype=float)
    if n < period + 1:
        return out
    log_rets = np.zeros(n)
    log_rets[0] = 0.0
    for i in range(1, n):
        if candles[i - 1].close > 0:
            log_rets[i] = math.log(candles[i].close / candles[i - 1].close)
    for i in range(period, n):
        out[i] = float(np.std(log_rets[i - period + 1:i + 1], ddof=1)) \
                 * math.sqrt(bars_per_year)
    return out


# ---------------------------------------------------------------------------
# Breakout & retest detection
# ---------------------------------------------------------------------------
@dataclass
class BreakoutRetestSignal:
    has_breakout: bool
    has_retest: bool
    breakout_level: Optional[float] = None
    breakout_idx: Optional[int] = None
    retest_idx: Optional[int] = None
    direction: str = "none"   # "up" / "down" / "none"


def breakout_retest(candles: List[Candle], lookback: int = 20
                    ) -> BreakoutRetestSignal:
    """Detect a recent upside breakout and a subsequent retest.

    Phase 12 BUG FIX: the previous implementation found only the FIRST
    breakout in the entire series (whether up or down). If the first
    breakout was DOWN, the long-only strategy could NEVER fire for the
    rest of the series — even when later UP breakouts occurred.

    The docstring already said "most recent bar" but the code used
    `break` + `breakout_idx is None`, capturing only the earliest
    breakout. This was a confirmed defect traced in Phase 12 via the
    strategy diagnostic script: breakout_retest on a 1000-candle BTC
    1h series returned direction="down" (first breakout at bar 41 was
    a down-move), causing 940/940 evaluations to return WAIT — yet
    Phase 11's per-segment runs DID produce 20 trades because each
    segment's prefix+segment series happened to start with an up-move.

    Phase 12 fix: find the MOST RECENT UP breakout in the series
    (long-only strategy only cares about up-breakouts). If a retest
    follows that most-recent up-breakout, return has_retest=True.

    Backward compatibility:
      * If at least one UP breakout exists, returns has_breakout=True,
        direction="up", with the most-recent up-breakout's idx/level.
      * If no UP breakout exists, returns has_breakout=False (so the
        long-only strategy returns WAIT, same as before).
      * The previous behavior of returning direction="down" is removed
        — the strategy never used it (it always returned WAIT on
        direction != "up"), so no behavior change for the strategy.
    """
    n = len(candles)
    if n < lookback + 3:
        return BreakoutRetestSignal(False, False)
    # Phase 12: scan ALL bars (no `break`), tracking the most recent
    # UP breakout. We no longer track down-breakouts because the
    # long-only strategy ignores them.
    most_recent_up_breakout_idx: Optional[int] = None
    most_recent_up_breakout_level: Optional[float] = None
    for i in range(lookback, n):
        window_high = max(c.high for c in candles[i - lookback:i])
        if candles[i].close > window_high:
            # Found an UP breakout at bar i. Keep scanning — we want
            # the MOST RECENT one, not the first.
            most_recent_up_breakout_idx = i
            most_recent_up_breakout_level = window_high
    if most_recent_up_breakout_idx is None:
        # No UP breakout anywhere in the series.
        return BreakoutRetestSignal(False, False, direction="none")
    # Look for retest in the bars AFTER the most-recent up-breakout.
    breakout_idx = most_recent_up_breakout_idx
    breakout_level = most_recent_up_breakout_level
    direction = "up"
    retest_idx: Optional[int] = None
    if breakout_idx < n - 1:
        for j in range(breakout_idx + 1, n):
            c = candles[j]
            # price comes back near the level without breaking below
            if abs(c.low - breakout_level) / breakout_level < 0.005 \
                    and c.close > breakout_level:
                retest_idx = j
                break
    return BreakoutRetestSignal(
        has_breakout=True,
        has_retest=(retest_idx is not None),
        breakout_level=breakout_level,
        breakout_idx=breakout_idx,
        retest_idx=retest_idx,
        direction=direction,
    )


# ---------------------------------------------------------------------------
# Composite indicator bundle
# ---------------------------------------------------------------------------
@dataclass
class IndicatorSet:
    """All indicators computed on one candle series, with explicit warm-up
    bookkeeping. Every array has the same length as the input candles."""

    ema20: np.ndarray
    ema50: np.ndarray
    ema200: np.ndarray
    rsi14: np.ndarray
    atr14: np.ndarray
    macd_line: np.ndarray
    macd_signal: np.ndarray
    macd_hist: np.ndarray
    relvol20: np.ndarray
    vol20: np.ndarray
    swing_highs: List[Optional[float]]
    swing_lows: List[Optional[float]]
    resistance_levels: List[float]
    support_levels: List[float]
    breakout_retest_signal: BreakoutRetestSignal
    last_close: float
    last_idx: int

    @classmethod
    def from_candles(cls, candles: List[Candle]) -> "IndicatorSet":
        closes = np.array([c.close for c in candles], dtype=float)
        macd_line, macd_sig, macd_hist = macd(candles)
        sh_p, sl_p, sh_i, sl_i = swing_highs_lows(candles, lookback=2)
        res, sup = support_resistance(candles, lookback=20)
        br = breakout_retest(candles, lookback=20)
        last_idx = len(candles) - 1
        return cls(
            ema20=ema(closes, 20),
            ema50=ema(closes, 50),
            ema200=ema(closes, 200),
            rsi14=rsi(candles, 14),
            atr14=atr(candles, 14),
            macd_line=macd_line,
            macd_signal=macd_sig,
            macd_hist=macd_hist,
            relvol20=relative_volume(candles, 20),
            vol20=rolling_volatility(candles, 20),
            swing_highs=sh_p,
            swing_lows=sl_p,
            resistance_levels=res,
            support_levels=sup,
            breakout_retest_signal=br,
            last_close=float(closes[-1]) if len(closes) else float("nan"),
            last_idx=last_idx,
        )

    def trend_at(self, i: int) -> TrendDirection:
        """Trend direction derived from EMA20 vs EMA50 vs EMA200."""
        # Use np.isnan rather than math.isnan — math.isnan can raise
        # TypeError on some numpy scalar types.
        if i < 199:
            return TrendDirection.FLAT
        try:
            if (np.isnan(self.ema20[i]) or np.isnan(self.ema50[i])
                    or np.isnan(self.ema200[i])):
                return TrendDirection.FLAT
        except (IndexError, TypeError):
            return TrendDirection.FLAT
        if self.ema20[i] > self.ema50[i] > self.ema200[i]:
            return TrendDirection.UP
        if self.ema20[i] < self.ema50[i] < self.ema200[i]:
            return TrendDirection.DOWN
        return TrendDirection.FLAT

    def momentum_at(self, i: int) -> str:
        """Momentum read from MACD histogram and RSI."""
        if i < 1 or math.isnan(self.macd_hist[i]) or math.isnan(self.rsi14[i]):
            return "neutral"
        if self.macd_hist[i] > 0 and self.rsi14[i] > 55:
            return "bullish"
        if self.macd_hist[i] < 0 and self.rsi14[i] < 45:
            return "bearish"
        return "neutral"
