"""CoinDCX Spot Lab — research-only crypto analysis & paper-trading platform.

This package provides:
    - A faithful CoinDCX public market-data client (REST, no auth).
    - OHLCV validation & freshness checks.
    - A clearly-labelled synthetic OHLCV generator (only used when live data
      is unavailable, never silently substituted for live prices).
    - Indicator engine (EMA, RSI, ATR, MACD, swings, support/resistance).
    - Rule-based signal engine (trend pullback, breakout/retest, momentum,
      baseline SMA) with no-trade filters.
    - Market scanner.
    - Risk / position-sizing engine with daily-loss & drawdown limits.
    - Event-driven backtester (no look-ahead).
    - Paper-trading engine with SQLite persistence.
    - Streamlit dashboard.

NO REAL-MONEY TRADING. NO AUTHENTICATED API CALLS. NO LEVERAGE.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
