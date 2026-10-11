"""Market scanner & ranking.

Scans a set of CoinDCX INR spot pairs and ranks them by an explainable
score composed of:

    - liquidity (24h quote volume in INR)
    - trend strength (EMA alignment)
    - momentum (RSI in healthy zone + MACD alignment)
    - volatility (rolling_volatility, normalised)
    - signal quality (best BUY_CANDIDATE signal's R-R, or 0 if NO_TRADE)
    - data freshness (penalise stale batches)
    - estimated transaction cost (penalise high-cost markets)

Does NOT rank a market highly just because its price is rising fast.
Each criterion is capped at a fixed contribution so a single runaway
factor cannot dominate.

Also collects exclusion reasons so the dashboard can show WHY a market
was filtered out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ..api.models import Candle, DataProvenance, PairInfo, SourceKind
from .indicators import IndicatorSet, TrendDirection
from .risk import RiskSettings, RiskState
from .signals import Signal, SignalEngine, SignalState


@dataclass
class ScanResult:
    pair: str
    base: str
    quote: str
    last_price: Optional[float]
    change_pct_24h: Optional[float]
    volume_24h_inr: Optional[float]
    trend: str
    volatility: Optional[float]
    signal_state: str
    best_strategy: Optional[str]
    rr_after_cost: Optional[float]
    est_cost_inr: Optional[float]
    score: float
    rank: Optional[int] = None
    excluded: bool = False
    exclusion_reason: str = ""
    provenance_label: str = ""

    def to_dict(self) -> dict:
        return {
            "pair": self.pair, "base": self.base, "quote": self.quote,
            "last_price": self.last_price,
            "change_pct_24h": self.change_pct_24h,
            "volume_24h_inr": self.volume_24h_inr,
            "trend": self.trend,
            "volatility": self.volatility,
            "signal_state": self.signal_state,
            "best_strategy": self.best_strategy,
            "rr_after_cost": self.rr_after_cost,
            "est_cost_inr": self.est_cost_inr,
            "score": self.score, "rank": self.rank,
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
            "provenance_label": self.provenance_label,
        }


@dataclass
class Scanner:
    settings: RiskSettings
    signal_engine: SignalEngine

    # ---- score weights (sum to 1.0)
    w_liquidity: float = 0.20
    w_trend: float = 0.20
    w_momentum: float = 0.15
    w_volatility: float = 0.10
    w_signal: float = 0.25
    w_freshness: float = 0.05
    w_cost: float = 0.05

    def scan(self, *,
             pairs: List[PairInfo],
             candles_by_pair: Dict[str, Tuple[List[Candle], DataProvenance]],
             tickers_by_pair: Optional[Dict[str, dict]] = None,
             risk_state: RiskState,
             interval: str = "1h") -> List[ScanResult]:
        """Scan all pairs and return ranked ScanResult list.

        candles_by_pair: { pair_code -> (candles, provenance) }
        tickers_by_pair: { pair_code -> {"last_price", "high_24h", ...} }
                          (optional, from current_prices endpoint)
        """
        tickers_by_pair = tickers_by_pair or {}
        results: List[ScanResult] = []
        for p in pairs:
            r = self._scan_one(p, candles_by_pair, tickers_by_pair,
                               risk_state, interval)
            results.append(r)

        # Exclude filtered-out
        ranked_active = [r for r in results if not r.excluded]
        ranked_active.sort(key=lambda r: -r.score)
        for i, r in enumerate(ranked_active, 1):
            r.rank = i

        # Excluded ones get no rank
        for r in results:
            if r.excluded:
                r.rank = None

        return results

    # ------------------------------------------------------------------
    def _scan_one(self, p: PairInfo,
                  cbp: Dict[str, Tuple[List[Candle], DataProvenance]],
                  tbp: Dict[str, dict],
                  risk_state: RiskState,
                  interval: str) -> ScanResult:
        if p.pair not in cbp:
            return ScanResult(
                pair=p.pair, base=p.base, quote=p.quote,
                last_price=None, change_pct_24h=None, volume_24h_inr=None,
                trend="unknown", volatility=None,
                signal_state=SignalState.NO_TRADE.value,
                best_strategy=None, rr_after_cost=None,
                est_cost_inr=None, score=0.0,
                excluded=True, exclusion_reason="no candles available",
            )
        candles, prov = cbp[p.pair]
        ticker = tbp.get(p.pair, {})

        # Liquidity gate
        vol24 = ticker.get("volume_24h")
        if vol24 is None or vol24 != vol24:
            # try to derive from candle volume if missing
            window = candles[-24:] if len(candles) >= 24 else candles
            try:
                vol24 = sum(c.volume * c.close for c in window) if window else 0.0
            except Exception:
                vol24 = 0.0

        if vol24 is None or vol24 < self.settings.min_24h_volume_inr:
            return ScanResult(
                pair=p.pair, base=p.base, quote=p.quote,
                last_price=ticker.get("last_price"),
                change_pct_24h=ticker.get("change_pct_24h"),
                volume_24h_inr=vol24,
                trend="unknown", volatility=None,
                signal_state=SignalState.NO_TRADE.value,
                best_strategy=None, rr_after_cost=None,
                est_cost_inr=None, score=0.0,
                excluded=True,
                exclusion_reason=(
                    f"24h volume {vol24:.0f} < min {self.settings.min_24h_volume_inr}"
                    if vol24 is not None else "24h volume missing"),
                provenance_label=prov.label(),
            )

        if prov.source is SourceKind.UNAVAILABLE:
            return ScanResult(
                pair=p.pair, base=p.base, quote=p.quote,
                last_price=ticker.get("last_price"),
                change_pct_24h=ticker.get("change_pct_24h"),
                volume_24h_inr=vol24,
                trend="unknown", volatility=None,
                signal_state=SignalState.NO_TRADE.value,
                best_strategy=None, rr_after_cost=None,
                est_cost_inr=None, score=0.0,
                excluded=True, exclusion_reason="data UNAVAILABLE",
                provenance_label=prov.label(),
            )

        # Indicators
        ind = IndicatorSet.from_candles(candles)
        i = ind.last_idx
        trend = ind.trend_at(i).value
        vol = float(ind.vol20[i]) if not math.isnan(ind.vol20[i]) else None
        rsi = float(ind.rsi14[i]) if not math.isnan(ind.rsi14[i]) else None

        # Signals
        sigs = self.signal_engine.evaluate(
            pair=p.pair, interval=interval, candles=candles,
            provenance=prov, risk_state=risk_state)

        # Pick best signal: BUY_CANDIDATE with highest rr_after_cost
        candidates = [s for s in sigs if s.state is SignalState.BUY_CANDIDATE]
        best_sig: Optional[Signal] = None
        if candidates:
            best_sig = max(candidates,
                           key=lambda s: s.rr_after_cost or 0.0)

        # ---- score components (each capped to [0, 1]) --------------
        # Liquidity: log-scaled vs min threshold
        s_liq = _cap(_log_score(vol24, lo=self.settings.min_24h_volume_inr,
                                hi=self.settings.min_24h_volume_inr * 50.0))

        # Trend: 1 if UP, 0.5 if FLAT, 0 if DOWN (slightly biased UP since
        # paper-trading is long-only)
        s_trend = {"up": 1.0, "flat": 0.5, "down": 0.0}.get(trend, 0.5)

        # Momentum: RSI in [50, 70] is best
        s_mom = 0.5
        if rsi is not None:
            if 50 <= rsi <= 70: s_mom = 1.0
            elif 45 <= rsi <= 75: s_mom = 0.7
            elif rsi > 75: s_mom = 0.2     # overbought
            elif rsi < 30: s_mom = 0.2     # oversold (NOT a long candidate here)

        # Volatility: 0.4-1.0/yr is "healthy"; outside that, penalise
        s_vol = 0.5
        if vol is not None:
            if 0.4 <= vol <= 1.2: s_vol = 1.0
            elif 0.2 <= vol <= 1.8: s_vol = 0.7
            elif vol > 2.0: s_vol = 0.2     # too wild
            else: s_vol = 0.3              # too flat

        # Signal: best BUY_CANDIDATE R-R normalised
        s_sig = 0.0
        rr = best_sig.rr_after_cost if best_sig else None
        if rr is not None and rr > 0:
            s_sig = _cap(rr / max(self.settings.reward_to_risk_target * 2, 1e-6))

        # Freshness: 1.0 if LIVE, 0.7 CACHED, 0.3 SIMULATED, 0.0 STALE/UNAVAILABLE
        src_map = {
            SourceKind.LIVE: 1.0,
            SourceKind.CACHED: 0.7,
            SourceKind.SIMULATED: 0.3,
            SourceKind.STALE: 0.1,
            SourceKind.UNAVAILABLE: 0.0,
        }
        s_fresh = src_map.get(prov.source, 0.0)

        # Cost: lower is better
        if best_sig is not None:
            cost_inr = (best_sig.est_entry_cost_inr or 0) + \
                       (best_sig.est_exit_cost_inr or 0)
            # normalise by 2% of equity (cost shouldn't dominate ranking
            # unless it's egregious)
            denom = max(risk_state.current_equity * 0.02, 1e-6)
            s_cost = _cap(1.0 - cost_inr / denom)
        else:
            # Without a concrete signal, use the cost model on a hypothetical
            # position = 1 * last_price
            last = (ticker.get("last_price") or
                    (candles[-1].close if candles else 0.0))
            cost_inr = last * (self.settings.fee_pct * 2
                              + self.settings.spread_pct
                              + self.settings.slippage_pct * 2) / 100.0
            denom = max(risk_state.current_equity * 0.02, 1e-6)
            s_cost = _cap(1.0 - cost_inr / denom)

        score = (
            self.w_liquidity * s_liq +
            self.w_trend * s_trend +
            self.w_momentum * s_mom +
            self.w_volatility * s_vol +
            self.w_signal * s_sig +
            self.w_freshness * s_fresh +
            self.w_cost * s_cost
        ) * 100.0

        return ScanResult(
            pair=p.pair, base=p.base, quote=p.quote,
            last_price=ticker.get("last_price"),
            change_pct_24h=ticker.get("change_pct_24h"),
            volume_24h_inr=vol24,
            trend=trend, volatility=vol,
            signal_state=(best_sig.state.value if best_sig else
                          (sigs[0].state.value if sigs else SignalState.NO_TRADE.value)),
            best_strategy=(best_sig.strategy if best_sig else None),
            rr_after_cost=rr,
            est_cost_inr=cost_inr if best_sig else None,
            score=score,
            excluded=False,
            exclusion_reason="",
            provenance_label=prov.label(),
        )


# ---------------------------------------------------------------------------
def _cap(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if x != x:
        return 0.0
    return max(lo, min(hi, x))


def _log_score(x: float, lo: float, hi: float) -> float:
    """Log-scaled score: 0 at lo, 1 at hi, >1 capped later."""
    if x <= 0 or lo <= 0:
        return 0.0
    import math as _m
    return (_m.log10(x) - _m.log10(lo)) / max(_m.log10(hi) - _m.log10(lo), 1e-9)
