"""Engine layer: indicators, signals, scanner, risk, backtest, paper-trading.
"""

from .indicators import (  # noqa: F401
    IndicatorSet,
    ema, rsi, atr, macd, swing_highs_lows, support_resistance,
    relative_volume, rolling_volatility, breakout_retest,
    TrendDirection,
)
from .risk import (  # noqa: F401
    RiskSettings, RiskState, PositionPlan, RiskEngine,
    RiskError, DailyLossState,
)
from .signals import (  # noqa: F401
    SignalEngine, Signal, SignalState, SignalReason,
    TrendPullbackStrategy, BreakoutRetestStrategy,
    MomentumContinuationStrategy, BaselineSMAStrategy,
    DEFAULT_STRATEGIES,
)
from .scanner import Scanner, ScanResult  # noqa: F401
from .backtest import Backtester, BacktestConfig, BacktestResult  # noqa: F401
from .paper import PaperTradingEngine, PaperTrade, PaperTradeStatus  # noqa: F401
