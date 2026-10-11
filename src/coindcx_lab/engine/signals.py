"""Rule-based, explainable signal engine.

Each strategy emits a Signal with:
    - trend direction
    - supporting & conflicting indicator reasons
    - entry, invalidation, stop, target
    - estimated fees / slippage / R-R (when computable)
    - data-quality warnings
    - a one-line human-readable explanation

Strategies are deliberately conservative. They never claim a probability of
profit; the score they produce is a heuristic ranking signal, not a forecast.

Implemented strategies (all long-only — paper-trading is long-only by design):
    - TrendPullbackStrategy
    - BreakoutRetestStrategy
    - MomentumContinuationStrategy
    - BaselineSMAStrategy (simple baseline for comparison)

No-trade filters (any of these flips a candidate to NO_TRADE):
    - data provenance not LIVE / CACHED / SIMULATED labelled acceptable
    - data stale or incomplete
    - fewer candles than the strategy's warm-up requirement
    - cost-adjusted R-R below target
    - daily-loss or drawdown limits reached
    - signal state explicitly WAIT (setup incomplete) rather than BUY
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from ..api.models import Candle, DataProvenance, SourceKind
from .indicators import IndicatorSet, TrendDirection
from .risk import RiskSettings, RiskState, RiskEngine, RiskDecision


class SignalState(str, Enum):
    BUY_CANDIDATE = "BUY_CANDIDATE"
    WAIT = "WAIT"
    EXIT = "EXIT"
    NO_TRADE = "NO_TRADE"


@dataclass
class SignalReason:
    text: str
    is_supporting: bool = True    # True=supports, False=conflicts


@dataclass
class Signal:
    pair: str
    interval: str
    strategy: str
    state: SignalState
    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    invalidation: Optional[float] = None
    supporting: List[SignalReason] = field(default_factory=list)
    conflicting: List[SignalReason] = field(default_factory=list)
    data_quality_warnings: List[str] = field(default_factory=list)
    rr_after_cost: Optional[float] = None
    est_entry_cost_inr: Optional[float] = None
    est_exit_cost_inr: Optional[float] = None
    explanation: str = ""
    request_key: Optional[str] = None  # for idempotency dedup

    def to_dict(self) -> dict:
        d = {
            "pair": self.pair, "interval": self.interval,
            "strategy": self.strategy, "state": self.state.value,
            "entry": self.entry, "stop": self.stop, "target": self.target,
            "invalidation": self.invalidation,
            "supporting": [r.text for r in self.supporting],
            "conflicting": [r.text for r in self.conflicting],
            "data_quality_warnings": list(self.data_quality_warnings),
            "rr_after_cost": self.rr_after_cost,
            "est_entry_cost_inr": self.est_entry_cost_inr,
            "est_exit_cost_inr": self.est_exit_cost_inr,
            "explanation": self.explanation,
        }
        if self.request_key:
            d["request_key"] = self.request_key
        return d


# ---------------------------------------------------------------------------
# Strategy interface
# ---------------------------------------------------------------------------
class Strategy:
    name: str = "base"
    warmup_candles: int = 200

    enabled: bool = True

    def evaluate(self, *, pair: str, interval: str,
                 candles: List[Candle],
                 indicators: IndicatorSet,
                 risk_engine: RiskEngine,
                 risk_state: RiskState,
                 provenance: DataProvenance,
                 current_price: Optional[float] = None) -> Optional[Signal]:
        raise NotImplementedError

    # ---- shared helpers
    @staticmethod
    def _check_data_quality(candles: List[Candle],
                            provenance: DataProvenance,
                            warmup: int) -> List[str]:
        warnings: List[str] = []
        if len(candles) < warmup:
            warnings.append(f"fewer than {warmup} candles ({len(candles)}); "
                            "indicators in warm-up phase")
        if not candles:
            warnings.append("no candles available")
            return warnings
        if provenance.source is SourceKind.UNAVAILABLE:
            warnings.append("data source UNAVAILABLE")
        if not candles[-1].complete:
            warnings.append("latest candle incomplete; signals use only "
                            "completed candles")
        # Stale-data check: if last close time older than 30 min relative to
        # now, surface a stale-data warning.
        import time
        now_ms = int(time.time() * 1000)
        if candles[-1].close_time_ms < now_ms - 30 * 60_000:
            warnings.append("latest candle close is older than 30 minutes "
                            "relative to wall-clock; data may be stale")
        return warnings

    def _build_signal_skeleton(self, pair, interval, candles, provenance
                               ) -> Signal:
        return Signal(
            pair=pair, interval=interval, strategy=self.name,
            state=SignalState.NO_TRADE,
            data_quality_warnings=self._check_data_quality(candles, provenance,
                                                            self.warmup_candles),
        )

    def _finalise_with_costs(self, sig: Signal, *,
                             entry: float, stop: float, target: float,
                             risk_engine: RiskEngine,
                             risk_state: RiskState) -> Signal:
        """If a strategy produced entry/stop/target, run the cost check and
        update the signal with R/R and cost estimates. The risk engine's
        plan_position is the single source of truth for cost-adjusted R/R,
        so we reuse it."""
        try:
            plan = risk_engine.plan_position(
                pair=sig.pair, entry=entry, stop=stop, target=target,
                state=risk_state)
        except Exception as e:
            sig.state = SignalState.NO_TRADE
            sig.explanation = f"plan_position failed: {e}"
            return sig
        sig.entry = entry; sig.stop = stop; sig.target = target
        sig.invalidation = stop
        sig.rr_after_cost = plan.rr_after_cost
        sig.est_entry_cost_inr = plan.est_entry_cost
        sig.est_exit_cost_inr = plan.est_exit_cost
        if plan.decision is RiskDecision.ACCEPTED:
            sig.state = SignalState.BUY_CANDIDATE
        elif plan.decision is RiskDecision.REJECTED_RR:
            sig.state = SignalState.NO_TRADE
            sig.conflicting.append(SignalReason(
                text=f"R/R after cost {plan.rr_after_cost:.2f} below target",
                is_supporting=False))
        else:
            sig.state = SignalState.NO_TRADE
            sig.conflicting.append(SignalReason(
                text=f"risk gate rejected: {plan.decision.value} ({plan.reason})",
                is_supporting=False))
        return sig


# ---------------------------------------------------------------------------
# 1) Trend pullback
# ---------------------------------------------------------------------------
class TrendPullbackStrategy(Strategy):
    name = "trend_pullback"
    warmup_candles = 200

    def evaluate(self, *, pair, interval, candles, indicators, risk_engine,
                 risk_state, provenance, current_price=None):
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

        # Trend must be UP
        if trend is not TrendDirection.UP:
            sig.state = SignalState.WAIT
            sig.explanation = f"trend {trend.value}; need UP (EMA20>EMA50>EMA200)"
            return sig
        sig.supporting.append(SignalReason(
            text=f"trend UP (EMA20={ema20:.2f}, EMA50={ema50:.2f}, "
                 f"EMA200={indicators.ema200[i]:.2f})"))

        # Pullback: close between EMA20 and EMA50 (closer to EMA20)
        if not (ema50 <= last.close <= ema20):
            sig.state = SignalState.WAIT
            sig.explanation = (f"price {last.close:.2f} not in pullback zone "
                               f"[EMA50={ema50:.2f}, EMA20={ema20:.2f}]")
            return sig
        sig.supporting.append(SignalReason(
            text=f"price {last.close:.2f} pulled back into "
                 f"[{ema50:.2f}, {ema20:.2f}]"))

        # RSI in [40, 55] — pullback, not yet overheated
        if not (40 <= rsi <= 55):
            sig.conflicting.append(SignalReason(
                text=f"RSI {rsi:.1f} outside [40,55] pullback range",
                is_supporting=False))
        else:
            sig.supporting.append(SignalReason(
                text=f"RSI {rsi:.1f} within [40,55]"))

        # Look for the most recent swing low as the invalidation/stop.
        entry = last.close
        recent_lows = [sl for sl in indicators.swing_lows if sl is not None][-3:]
        atr_val = indicators.atr14[i]
        if recent_lows:
            stop = min(recent_lows) * 0.998   # small buffer below the swing low
        elif not math.isnan(atr_val) and atr_val > 0:
            # Fallback: ATR-based stop when there are no recent pivot lows
            # (common in a strictly trending series where every bar's low
            # is rising). 1.5 ATR below the entry.
            stop = entry - 1.5 * atr_val
            sig.supporting.append(SignalReason(
                text=f"no recent swing low; using ATR-based stop = "
                     f"entry - 1.5*ATR({atr_val:.2f}) = {stop:.2f}"))
        else:
            sig.state = SignalState.WAIT
            sig.explanation = "no recent swing low to anchor stop and ATR unavailable"
            return sig
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = (f"stop {stop:.2f} >= entry {entry:.2f}; "
                               "invalid setup geometry")
            return sig

        # Target: 2x risk (gross) — risk engine will cost-adjust
        risk_per_unit = entry - stop
        target = entry + 2 * risk_per_unit
        sig.supporting.append(SignalReason(
            text=f"target {target:.2f} = 2R from entry"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                         target=target,
                                         risk_engine=risk_engine,
                                         risk_state=risk_state)


# ---------------------------------------------------------------------------
# 2) Breakout & retest
# ---------------------------------------------------------------------------
class BreakoutRetestStrategy(Strategy):
    name = "breakout_retest"
    warmup_candles = 60    # only needs ~20+ for the breakout window

    def evaluate(self, *, pair, interval, candles, indicators, risk_engine,
                 risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        br = indicators.breakout_retest_signal
        if not br.has_breakout or br.direction != "up":
            sig.state = SignalState.WAIT
            sig.explanation = "no upside breakout detected in recent window"
            return sig
        sig.supporting.append(SignalReason(
            text=f"breakout UP above {br.breakout_level:.2f} "
                 f"@bar#{br.breakout_idx}"))

        if not br.has_retest:
            sig.state = SignalState.WAIT
            sig.explanation = "breakout not yet retested; waiting for retest"
            return sig
        sig.supporting.append(SignalReason(
            text=f"retest confirmed @bar#{br.retest_idx}"))

        entry = candles[-1].close
        stop = br.breakout_level * (1 - 0.005)   # 0.5% below breakout level
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = (f"stop {stop:.2f} >= entry {entry:.2f}; "
                               "price already far above breakout level")
            return sig

        # Target: recent swing high (or 2R minimum)
        recent_highs = [h for h in indicators.swing_highs if h is not None][-3:]
        target_swing = max(recent_highs) if recent_highs else entry * 1.04
        target_2r = entry + 2 * (entry - stop)
        target = max(target_swing, target_2r)
        sig.supporting.append(SignalReason(
            text=f"target {target:.2f} (max of recent swing high {target_swing:.2f} "
                 f"and 2R {target_2r:.2f})"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                         target=target,
                                         risk_engine=risk_engine,
                                         risk_state=risk_state)


# ---------------------------------------------------------------------------
# 3) Momentum continuation
# ---------------------------------------------------------------------------
class MomentumContinuationStrategy(Strategy):
    name = "momentum_continuation"
    warmup_candles = 60

    def evaluate(self, *, pair, interval, candles, indicators, risk_engine,
                 risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        hist = indicators.macd_hist[i]
        rsi  = indicators.rsi14[i]
        rv   = indicators.relvol20[i]
        trend = indicators.trend_at(i)

        if math.isnan(hist) or math.isnan(rsi) or math.isnan(rv):
            sig.explanation = "indicator warm-up incomplete"
            return sig

        if trend is not TrendDirection.UP:
            sig.state = SignalState.WAIT
            sig.explanation = "trend not UP; momentum continuation requires UP trend"
            return sig
        sig.supporting.append(SignalReason(text=f"trend UP"))

        if not (hist > 0 and rsi > 55):
            sig.state = SignalState.WAIT
            sig.explanation = (f"MACD hist {hist:.3f} or RSI {rsi:.1f} "
                               "not in continuation zone")
            return sig
        sig.supporting.append(SignalReason(
            text=f"MACD hist {hist:.3f} > 0 and RSI {rsi:.1f} > 55"))

        if rv < 1.2:
            sig.conflicting.append(SignalReason(
                text=f"relative volume {rv:.2f} < 1.2 (weak participation)",
                is_supporting=False))
        else:
            sig.supporting.append(SignalReason(
                text=f"relative volume {rv:.2f} > 1.2"))

        entry = candles[-1].close
        # Stop: 1.5 ATR below entry
        atr_val = indicators.atr14[i]
        if math.isnan(atr_val) or atr_val <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "ATR not available / non-positive"
            return sig
        stop = entry - 1.5 * atr_val
        target = entry + 2 * 1.5 * atr_val   # 2R
        sig.supporting.append(SignalReason(
            text=f"stop = entry - 1.5*ATR({atr_val:.2f}) = {stop:.2f}; "
                 f"target = 2R = {target:.2f}"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                         target=target,
                                         risk_engine=risk_engine,
                                         risk_state=risk_state)


# ---------------------------------------------------------------------------
# 4) Baseline SMA
# ---------------------------------------------------------------------------
class BaselineSMAStrategy(Strategy):
    """A deliberately simple baseline: long when close > EMA50 and EMA50 is
    rising. Used as a benchmark for the other strategies."""
    name = "baseline_sma"
    warmup_candles = 60

    def evaluate(self, *, pair, interval, candles, indicators, risk_engine,
                 risk_state, provenance, current_price=None):
        sig = self._build_signal_skeleton(pair, interval, candles, provenance)
        if len(candles) < self.warmup_candles:
            sig.explanation = "warm-up incomplete"
            return sig

        i = indicators.last_idx
        ema50 = indicators.ema50[i]
        ema50_prev = indicators.ema50[i - 1]
        last = candles[i]
        if math.isnan(ema50) or math.isnan(ema50_prev):
            sig.explanation = "EMA50 warm-up incomplete"
            return sig

        if last.close <= ema50:
            sig.state = SignalState.WAIT
            sig.explanation = (f"close {last.close:.2f} <= EMA50 {ema50:.2f}")
            return sig
        sig.supporting.append(SignalReason(
            text=f"close {last.close:.2f} > EMA50 {ema50:.2f}"))

        if ema50 <= ema50_prev:
            sig.state = SignalState.WAIT
            sig.explanation = (f"EMA50 not rising ({ema50_prev:.2f} -> {ema50:.2f})")
            return sig
        sig.supporting.append(SignalReason(
            text=f"EMA50 rising ({ema50_prev:.2f} -> {ema50:.2f})"))

        atr_val = indicators.atr14[i]
        if math.isnan(atr_val) or atr_val <= 0:
            sig.state = SignalState.WAIT
            sig.explanation = "ATR not available"
            return sig
        entry = last.close
        stop = ema50 - 1.0 * atr_val
        if stop >= entry:
            sig.state = SignalState.WAIT
            sig.explanation = "stop would not be below entry"
            return sig
        target = entry + 2 * (entry - stop)
        sig.supporting.append(SignalReason(
            text=f"stop = EMA50 - 1*ATR = {stop:.2f}; target = 2R = {target:.2f}"))

        return self._finalise_with_costs(sig, entry=entry, stop=stop,
                                         target=target,
                                         risk_engine=risk_engine,
                                         risk_state=risk_state)


DEFAULT_STRATEGIES = [
    TrendPullbackStrategy(),
    BreakoutRetestStrategy(),
    MomentumContinuationStrategy(),
    BaselineSMAStrategy(),
]


# ---------------------------------------------------------------------------
# Signal engine
# ---------------------------------------------------------------------------
@dataclass
class SignalEngineConfig:
    enabled: Dict[str, bool] = field(default_factory=lambda: {
        "trend_pullback": True,
        "breakout_retest": True,
        "momentum_continuation": True,
        "baseline_sma": True,
    })


class SignalEngine:
    """Composes strategies and applies no-trade filters."""

    def __init__(self, *,
                 settings: RiskSettings,
                 strategies: Optional[List[Strategy]] = None,
                 config: Optional[SignalEngineConfig] = None):
        self.settings = settings
        self.risk_engine = RiskEngine(settings)
        self.strategies = strategies or DEFAULT_STRATEGIES
        self.config = config or SignalEngineConfig()

    def evaluate(self, *, pair: str, interval: str,
                 candles: List[Candle],
                 provenance: DataProvenance,
                 risk_state: RiskState,
                 current_price: Optional[float] = None) -> List[Signal]:
        """Run every enabled strategy on the given candles."""
        if not candles:
            return [Signal(
                pair=pair, interval=interval, strategy="none",
                state=SignalState.NO_TRADE,
                data_quality_warnings=["no candles available"],
                explanation="no candles available",
            )]
        # Compute indicators ONCE per evaluation (candles are a single
        # interval's worth — the multi-timeframe orchestration lives in
        # the scanner/dashboard).
        indicators = IndicatorSet.from_candles(candles)
        out: List[Signal] = []
        for strat in self.strategies:
            if not self.config.enabled.get(strat.name, True):
                continue
            try:
                sig = strat.evaluate(
                    pair=pair, interval=interval, candles=candles,
                    indicators=indicators, risk_engine=self.risk_engine,
                    risk_state=risk_state, provenance=provenance,
                    current_price=current_price)
            except Exception as e:
                sig = Signal(
                    pair=pair, interval=interval, strategy=strat.name,
                    state=SignalState.NO_TRADE,
                    data_quality_warnings=[f"strategy raised: {e}"],
                    explanation=f"strategy {strat.name} raised: {e}",
                )
            if sig is not None:
                out.append(sig)
        return out
