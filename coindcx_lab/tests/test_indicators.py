"""Tests: indicator calculations (EMA, RSI, ATR, MACD, swings, etc.).

Hand-verified on small fixtures so the assertions are exact, not
statistical.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from coindcx_lab.api.models import Candle
from coindcx_lab.engine.indicators import (
    atr, breakout_retest, ema, ema_close, macd,
    relative_volume, rolling_volatility, rsi, support_resistance,
    swing_highs_lows, IndicatorSet, TrendDirection,
)


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------
class TestEMA:
    def test_first_n_minus_1_are_nan(self):
        v = np.arange(1, 21, dtype=float)
        out = ema(v, period=5)
        assert np.isnan(out[:4]).all()
        assert not np.isnan(out[4:]).any()

    def test_seed_is_sma_of_first_period(self):
        v = np.arange(1, 11, dtype=float)  # 1..10
        out = ema(v, period=5)
        # SMA of first 5 = (1+2+3+4+5)/5 = 3.0
        assert math.isclose(out[4], 3.0, rel_tol=1e-9)

    def test_recurse_correctly(self):
        v = np.array([10.0, 10.0, 10.0, 10.0, 10.0, 20.0], dtype=float)
        out = ema(v, period=5)
        alpha = 2 / 6
        expected_first = 10.0   # SMA of first 5 = 10
        # Next value = alpha*20 + (1-alpha)*10 = 0.333*20 + 0.666*10
        expected_next = alpha * 20 + (1 - alpha) * 10
        assert math.isclose(out[4], expected_first, rel_tol=1e-9)
        assert math.isclose(out[5], expected_next, rel_tol=1e-9)

    def test_short_input_returns_all_nan(self):
        out = ema(np.array([1.0, 2.0]), period=5)
        assert np.isnan(out).all()

    def test_period_zero_raises(self):
        with pytest.raises(ValueError):
            ema(np.array([1.0]), period=0)


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------
class TestRSI:
    def test_first_period_values_are_nan(self):
        # Build 30 candles with constant close so RSI = 100 (no losses).
        cs = [Candle(open_time_ms=i*3_600_000, open=1, high=1, low=1, close=1,
                     volume=1, close_time_ms=(i+1)*3_600_000-1)
              for i in range(30)]
        out = rsi(cs, period=14)
        assert np.isnan(out[:14]).all()
        # After warm-up, all up moves (no, actually constant -> no move at
        # all -> avg_gain = 0, avg_loss = 0). Wilder's RSI = 100 when
        # avg_loss == 0.
        assert out[14] == 100.0

    def test_all_down_moves_rsi_zero(self):
        cs = [Candle(open_time_ms=i*3_600_000, open=10 - i*0.1,
                     high=10 - i*0.1, low=10 - i*0.1 - 0.1,
                     close=10 - i*0.1 - 0.1, volume=1,
                     close_time_ms=(i+1)*3_600_000-1)
              for i in range(30)]
        out = rsi(cs, period=14)
        # avg_gain = 0, avg_loss > 0 => RS = 0 => RSI = 0
        assert out[14] == 0.0

    def test_alternating_moves_rsi_around_50(self):
        # Alternating up/down of equal magnitude => RSI ~ 50
        prices = []
        for i in range(30):
            prices.append(100 if i % 2 == 0 else 101)
        cs = [Candle(open_time_ms=i*3_600_000, open=prices[i],
                     high=prices[i] + 1, low=prices[i] - 1,
                     close=prices[i], volume=1,
                     close_time_ms=(i+1)*3_600_000-1)
              for i in range(30)]
        out = rsi(cs, period=14)
        # Should be near 50 (alternating up/down of equal size => avg_gain
        # ~ avg_loss)
        assert 30 < out[14] < 70


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------
class TestATR:
    def test_first_period_nan(self):
        cs = [Candle(open_time_ms=i*3_600_000, open=10, high=11, low=9,
                     close=10, volume=1, close_time_ms=(i+1)*3_600_000-1)
              for i in range(30)]
        out = atr(cs, period=14)
        assert np.isnan(out[:14]).all()
        # All TR = 2 (since high - low = 2, no gap), so ATR = 2.
        assert math.isclose(out[14], 2.0, rel_tol=1e-9)
        assert math.isclose(out[15], 2.0, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------
class TestMACD:
    def test_lengths_match_input(self, clean_candles):
        m, s, h = macd(clean_candles)
        assert len(m) == len(clean_candles)
        assert len(s) == len(clean_candles)
        assert len(h) == len(clean_candles)

    def test_warmup_nans(self, clean_candles):
        m, s, h = macd(clean_candles)
        # MACD line = EMA12 - EMA26; warm-up = 25 NaN
        assert np.isnan(m[:25]).all()
        # Signal = EMA9 of macd_line; additional warm-up
        assert np.isnan(s[:33]).all()


# ---------------------------------------------------------------------------
# Swing highs/lows
# ---------------------------------------------------------------------------
class TestSwings:
    def test_detects_simple_swing_high(self):
        # Make a series with a clear peak at i=2 (lookback=1 => window [1..3])
        cs = []
        for i in range(5):
            close = 10
            if i == 2:
                close = 20
            cs.append(Candle(open_time_ms=i*3_600_000, open=close,
                             high=close, low=close - 1,
                             close=close, volume=1,
                             close_time_ms=(i+1)*3_600_000-1))
        sh_p, sl_p, sh_i, sl_i = swing_highs_lows(cs, lookback=1)
        assert sh_p[2] == 20

    def test_no_swing_at_boundaries(self):
        cs = [Candle(open_time_ms=i*3_600_000, open=i, high=i, low=i, close=i,
                     volume=1, close_time_ms=(i+1)*3_600_000-1)
              for i in range(5)]
        sh_p, sl_p, _, _ = swing_highs_lows(cs, lookback=2)
        # At boundaries (i=0 and i=4) we cannot have a swing
        assert all(s is None for s in sh_p[:2])
        assert all(s is None for s in sh_p[3:])


# ---------------------------------------------------------------------------
# Support/Resistance
# ---------------------------------------------------------------------------
class TestSupportResistance:
    def test_returns_levels(self, clean_candles):
        res, sup = support_resistance(clean_candles, lookback=10)
        assert isinstance(res, list)
        assert isinstance(sup, list)
        assert len(res) <= 10
        assert len(sup) <= 10


# ---------------------------------------------------------------------------
# Relative volume & rolling volatility
# ---------------------------------------------------------------------------
class TestRelVol:
    def test_first_period_nan(self, clean_candles):
        out = relative_volume(clean_candles, period=20)
        assert np.isnan(out[:20]).all()
        # The 21st bar's relative volume = v[20]/mean(v[0..19])
        vols = [c.volume for c in clean_candles]
        expected = vols[20] / (sum(vols[0:20]) / 20)
        assert math.isclose(out[20], expected, rel_tol=1e-9)


class TestRollingVol:
    def test_first_period_nan(self, clean_candles):
        out = rolling_volatility(clean_candles, period=20)
        assert np.isnan(out[:20]).all()
        # 21st value should be a positive finite number
        assert math.isfinite(out[20]) and out[20] >= 0


# ---------------------------------------------------------------------------
# Breakout & retest
# ---------------------------------------------------------------------------
class TestBreakoutRetest:
    def test_detects_simple_breakout(self):
        # Build 30 bars: first 20 around 100, last 10 push to 110.
        cs = []
        for i in range(20):
            cs.append(Candle(open_time_ms=i*3_600_000, open=100, high=101,
                             low=99, close=100, volume=10,
                             close_time_ms=(i+1)*3_600_000-1))
        for j in range(10):
            cs.append(Candle(open_time_ms=(20+j)*3_600_000, open=100+j,
                             high=101+j, low=99+j, close=100+j+1, volume=15,
                             close_time_ms=(21+j)*3_600_000-1))
        sig = breakout_retest(cs, lookback=20)
        assert sig.has_breakout
        assert sig.direction == "up"


# ---------------------------------------------------------------------------
# IndicatorSet
# ---------------------------------------------------------------------------
class TestIndicatorSet:
    def test_trend_up(self, up_trend_candles):
        ind = IndicatorSet.from_candles(up_trend_candles)
        i = ind.last_idx
        assert ind.trend_at(i) is TrendDirection.UP

    def test_trend_flat_on_flat_series(self, flat_candles):
        ind = IndicatorSet.from_candles(flat_candles)
        i = ind.last_idx
        # All EMAs equal => neither UP nor DOWN strict alignment => FLAT
        assert ind.trend_at(i) is TrendDirection.FLAT

    def test_momentum_neutral_on_flat_series(self, flat_candles):
        ind = IndicatorSet.from_candles(flat_candles)
        i = ind.last_idx
        assert ind.momentum_at(i) == "neutral"
