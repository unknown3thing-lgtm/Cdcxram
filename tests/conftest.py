"""Shared pytest fixtures for CoinDCX Spot Lab tests.

All fixtures are pure functions over synthetic Candle lists — no network
access required.
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timezone
from typing import List

import pytest

# Make the package importable when running pytest from the repo root.
_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from coindcx_lab.api.models import Candle, DataProvenance, SourceKind
from coindcx_lab.api.synthetic import SyntheticGenerator
from coindcx_lab.api.validate import validate_candles
from coindcx_lab.engine.risk import RiskSettings, RiskState, DailyLossState


@pytest.fixture
def synth() -> SyntheticGenerator:
    return SyntheticGenerator(seed=42)


@pytest.fixture
def clean_candles(synth) -> List[Candle]:
    """A clean synthetic OHLCV series of 250 1h candles, all complete."""
    candles, _ = synth.series(pair="B-BTCINR", interval="1h", count=250,
                                start_price=1000.0)
    return candles


@pytest.fixture
def up_trend_candles() -> List[Candle]:
    """A synthetic up-trending series with predictable structure:
    each bar closes higher than the previous, low = open, high = close + 1,
    so EMA20 > EMA50 > EMA200 is guaranteed after enough bars.
    """
    base_ts = 1_700_000_000_000   # fixed anchor
    interval_ms = 3_600_000
    candles: List[Candle] = []
    price = 100.0
    for i in range(220):
        o = price
        c = price * 1.0015
        h = c + 0.5
        lo = o - 0.5
        v = 1000.0 + i * 0.1
        candles.append(Candle(
            open_time_ms=base_ts + i * interval_ms,
            open=o, high=h, low=lo, close=c, volume=v,
            close_time_ms=base_ts + (i + 1) * interval_ms - 1,
            complete=True,
        ))
        price = c
    return candles


@pytest.fixture
def flat_candles() -> List[Candle]:
    """Flat series at 100 INR with small noise. Useful for testing RSI
    equilibrium, ATR ~0, EMA convergence."""
    base_ts = 1_700_000_000_000
    interval_ms = 3_600_000
    candles: List[Candle] = []
    for i in range(220):
        candles.append(Candle(
            open_time_ms=base_ts + i * interval_ms,
            open=100.0, high=100.5, low=99.5, close=100.0,
            volume=1000.0,
            close_time_ms=base_ts + (i + 1) * interval_ms - 1,
            complete=True,
        ))
    return candles


@pytest.fixture
def default_settings() -> RiskSettings:
    return RiskSettings(
        initial_inr=2000.0,
        risk_per_trade_pct=1.0,
        max_position_pct=25.0,
        max_portfolio_exposure_pct=80.0,
        max_daily_loss_pct=5.0,
        max_daily_loss_abs_inr=0.0,
        max_drawdown_pct=15.0,
        max_open_positions=5,
        max_daily_trades=10,
        reward_to_risk_target=2.0,
        cooldown_after_consecutive_losses=3,
        allow_overnight=True,
        fee_pct=0.1,
        spread_pct=0.05,
        slippage_pct=0.05,
        min_24h_volume_inr=5_000_000,
        min_24h_trades=1000,
        trading_allocation_pct=100.0,
    )


@pytest.fixture
def fresh_state(default_settings) -> RiskState:
    s = default_settings
    st = RiskState(initial_inr=s.initial_inr)
    st.daily = DailyLossState(day_iso="2024-01-01")
    return st


@pytest.fixture
def provenance_live() -> DataProvenance:
    return DataProvenance(
        source=SourceKind.LIVE,
        endpoint="https://public.coindcx.com/market_data/v3/candles",
        fetched_at_utc=datetime.now(timezone.utc),
    )


@pytest.fixture
def provenance_sim() -> DataProvenance:
    return DataProvenance(
        source=SourceKind.SIMULATED,
        endpoint="synthetic://test",
        fetched_at_utc=datetime.now(timezone.utc),
    )
