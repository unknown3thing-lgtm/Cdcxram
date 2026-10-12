"""Event-driven, no-look-ahead backtester.

Design rules:
    - Strategy is evaluated at bar CLOSE using ONLY completed bars. The
      current bar's high/low are not visible to the signal evaluator.
    - Entry fills at the next bar's OPEN, with entry slippage applied
      adversely. This means we never enter on the same bar we decided.
    - While in a position, each subsequent bar is checked for stop / target
      hits using the bar's low/high. When BOTH the stop and target fall
      inside the same bar's range, we conservatively assume the stop is
      hit first (pessimistic).
    - Exit slippage and exit fee are applied adversely on the exit fill.
    - Cash accounting: cash = initial + realized pnl - committed open
      position value. We refuse to enter if cash < required entry cost.
    - Daily loss limit, max drawdown, max open positions, max daily trades
      are all enforced.
    - No indicator warm-up: signals are not evaluated until the strategy's
      warmup_candles threshold has been seen.
    - No duplicate entries: a new BUY is ignored while the same pair is
      already in a position.
    - No future data: the strategy sees candles[:i+1] at bar i.

Walk-forward split:
    The candle series is split chronologically into dev / val / oos slices
    by date boundaries provided by the caller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..api.models import Candle, DataProvenance, SourceKind
from .indicators import IndicatorSet
from .risk import (RiskDecision, RiskEngine, RiskSettings, RiskState,
                   DailyLossState)
from .signals import Signal, SignalEngine, SignalState, Strategy
from ..api.validate import drop_incomplete_latest


# ---------------------------------------------------------------------------
@dataclass
class BacktestConfig:
    starting_capital_inr: float = 2000.0
    warmup_skip_bars: int = 200
    # Conservative: assume stop hit first if both stop & target fall inside
    # the same bar's range.
    assume_stop_first_on_ambiguous_bar: bool = True
    # Whether to enforce daily loss / drawdown / max position limits.
    enforce_risk_limits: bool = True
    # Maximum number of simultaneously open positions (single-pair backtest
    # only opens 1 at a time; multi-pair mode tracks N).
    max_concurrent_positions: int = 1
    # Split boundaries (ISO date strings in display TZ) for walk-forward.
    dev_end_iso: Optional[str] = None
    val_end_iso: Optional[str] = None
    # oos_end defaults to last candle.


@dataclass
class Trade:
    pair: str
    strategy: str
    entry_idx: int
    exit_idx: int
    entry_time_ms: int
    exit_time_ms: int
    entry: float
    stop: float
    target: float
    exit_price: float
    qty: float
    position_value: float
    pnl_gross: float
    entry_fee: float
    exit_fee: float
    slippage_cost: float
    pnl_net: float
    exit_reason: str           # "target", "stop", "signal_exit", "end_of_data"

    @property
    def holding_bars(self) -> int:
        return self.exit_idx - self.entry_idx

    @property
    def is_win(self) -> bool:
        return self.pnl_net > 0


@dataclass
class BacktestResult:
    config: BacktestConfig
    segment: str               # "dev", "val", "oos", or "full"
    starting_capital: float
    ending_equity: float
    total_return_inr: float
    total_return_pct: float
    net_pnl_after_costs: float
    max_drawdown_pct: float
    win_rate_pct: float
    avg_win_inr: float
    avg_loss_inr: float
    profit_factor: float
    expectancy_inr: float
    num_trades: int
    exposure_pct: float
    turnover_inr: float
    est_fees_inr: float
    est_slippage_inr: float
    buy_hold_return_pct: float
    trades: List[Trade]
    equity_curve: List[Tuple[int, float]]   # [(close_time_ms, equity)]
    # Phase 10: warm-up bookkeeping. Bars at i < segment_start_idx are
    # warm-up prefix bars (used for indicator init only). Only bars at
    # i >= segment_start_idx are eligible for signal evaluation / trade
    # scoring / equity recording.
    segment_start_idx: int = 0
    total_bars: int = 0          # bars in the segment window (excluding prefix)
    eligible_bars: int = 0       # bars where signal eval was actually permitted
                                 # (= bars where i >= segment_start_idx AND
                                 #  warm-up gates passed)
    warmup_prefix_len: int = 0   # length of the warm-up prefix (0 for full)
    warnings: List[str] = field(default_factory=list)

    def to_summary(self) -> dict:
        return {
            "segment": self.segment,
            "starting_capital": self.starting_capital,
            "ending_equity": self.ending_equity,
            "total_return_inr": self.total_return_inr,
            "total_return_pct": self.total_return_pct,
            "net_pnl_after_costs": self.net_pnl_after_costs,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate_pct": self.win_rate_pct,
            "avg_win_inr": self.avg_win_inr,
            "avg_loss_inr": self.avg_loss_inr,
            "profit_factor": self.profit_factor,
            "expectancy_inr": self.expectancy_inr,
            "num_trades": self.num_trades,
            "exposure_pct": self.exposure_pct,
            "turnover_inr": self.turnover_inr,
            "est_fees_inr": self.est_fees_inr,
            "est_slippage_inr": self.est_slippage_inr,
            "buy_hold_return_pct": self.buy_hold_return_pct,
            "segment_start_idx": self.segment_start_idx,
            "total_bars": self.total_bars,
            "eligible_bars": self.eligible_bars,
            "warmup_prefix_len": self.warmup_prefix_len,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
class Backtester:
    """Single-pair, single-strategy event-driven backtester."""

    def __init__(self, *,
                 settings: RiskSettings,
                 strategy: Strategy,
                 signal_engine: SignalEngine,
                 config: Optional[BacktestConfig] = None):
        self.settings = settings
        self.strategy = strategy
        self.signal_engine = signal_engine
        self.config = config or BacktestConfig(
            starting_capital_inr=settings.initial_inr)

    # ------------------------------------------------------------------ run
    def run(self, *, pair: str, candles: List[Candle],
            provenance: Optional[DataProvenance] = None,
            segment: str = "full",
            warmup_prefix: Optional[List[Candle]] = None,
            segment_start_idx: int = 0) -> BacktestResult:
        """Run a backtest over `candles` (chronologically ordered).

        Phase 10 — warm-up prefix support
        --------------------------------
        If `warmup_prefix` is provided (a list of OLDER candles from the
        previous segment), the engine PREPENDS it to `candles` for
        indicator initialization. `segment_start_idx` MUST equal
        `len(warmup_prefix)` and marks the first bar of the actual
        scored segment.

        Behaviour:
          * Indicators at bar `i` see candles[0..i] (prefix + segment).
            No future-data leakage.
          * Signal evaluation is permitted only when:
                i + 1 >= cfg.warmup_skip_bars
              AND i + 1 >= strategy.warmup_candles
            AND i >= segment_start_idx      (the bar is INSIDE the segment)
          * Trades whose entry_idx < segment_start_idx are NEVER scored
            (warm-up leakage prevention). Because the loop starts with
            `pos = None`, no carried-in position exists.
          * Equity curve records only bars at i >= segment_start_idx.
          * Buy-and-hold uses the segment's first/last prices
            (candles[segment_start_idx].open and candles[-1].close),
            so the benchmark reflects only the segment.
        """
        if provenance is None:
            provenance = DataProvenance(
                source=SourceKind.SIMULATED,
                endpoint="backtest",
                fetched_at_utc=datetime.now(timezone.utc),
                note="backtest fixture",
            )

        # ----- assemble (warmup_prefix + candles) -----
        # Both lists are sorted by open_time_ms. We must NOT duplicate
        # any candle that appears in both (e.g., the boundary candle).
        if warmup_prefix:
            # Defensive copies & sort
            prefix_sorted = sorted(warmup_prefix, key=lambda c: c.open_time_ms)
            seg_sorted = sorted(candles, key=lambda c: c.open_time_ms)
            # De-duplicate by open_time_ms (segment wins over prefix if overlap)
            prefix_times = {c.open_time_ms for c in seg_sorted}
            prefix_clean = [c for c in prefix_sorted
                            if c.open_time_ms not in prefix_times]
            all_candles = prefix_clean + seg_sorted
            # Verify segment_start_idx points at the first SEGMENT candle.
            # We rely on the caller to pass the correct value, but we
            # also defensively re-derive it from timestamps if there's
            # a mismatch.
            actual_seg_start = len(all_candles) - len(seg_sorted)
            if segment_start_idx <= 0:
                segment_start_idx = actual_seg_start
        else:
            all_candles = sorted(candles, key=lambda c: c.open_time_ms)
            if segment_start_idx < 0:
                segment_start_idx = 0

        # Drop incomplete latest (still-forming bar). Only drops at most
        # one bar at the END of the series (whether it's a segment or
        # prefix+segment).
        all_candles = drop_incomplete_latest(all_candles) \
            if all_candles and not all_candles[-1].complete else all_candles

        cfg = self.config
        s = self.settings
        risk_engine = self.risk_engine_for(cfg.starting_capital_inr)
        state = RiskState(initial_inr=cfg.starting_capital_inr)
        state.daily = DailyLossState(
            day_iso=_day_iso_from_ms(all_candles[0].open_time_ms)
            if all_candles else "")

        trades: List[Trade] = []
        equity_curve: List[Tuple[int, float]] = []
        eligible_bars = 0   # bars where signal eval was permitted (segment only)

        # Position state — always None at segment start (no carried-in pos).
        pos = None
        # Risk gating flags
        paused_today = False
        paused_entirely = False

        for i in range(len(all_candles)):
            # New day reset?
            today_iso = _day_iso_from_ms(all_candles[i].open_time_ms)
            if state.daily.day_iso != today_iso:
                state.daily.reset(today_iso)
                paused_today = False

            # If in a position, check stop/target on THIS bar.
            if pos is not None:
                exit_price, exit_reason = self._check_exit(
                    pos, all_candles[i], cfg)
                if exit_reason is not None:
                    exit_idx = i
                    pnl = risk_engine.on_trade_closed(
                        entry=pos["entry"], stop=pos["stop"],
                        target=pos["target"], qty=pos["qty"],
                        exit_price=exit_price, state=state,
                        day_iso=today_iso)
                    entry_fee = pos["entry"] * pos["qty"] * s.fee_pct / 100.0
                    exit_fee  = exit_price * pos["qty"] * s.fee_pct / 100.0
                    slip      = (pos["entry"] * pos["qty"] * s.slippage_pct / 100.0
                                 + exit_price * pos["qty"] * s.slippage_pct / 100.0)
                    pnl_gross = (exit_price - pos["entry"]) * pos["qty"]
                    trades.append(Trade(
                        pair=pair, strategy=pos["strategy"],
                        entry_idx=pos["entry_idx"], exit_idx=exit_idx,
                        entry_time_ms=pos["entry_time_ms"],
                        exit_time_ms=all_candles[i].close_time_ms,
                        entry=pos["entry"], stop=pos["stop"],
                        target=pos["target"], exit_price=exit_price,
                        qty=pos["qty"], position_value=pos["position_value"],
                        pnl_gross=pnl_gross,
                        entry_fee=entry_fee, exit_fee=exit_fee,
                        slippage_cost=slip, pnl_net=pnl,
                        exit_reason=exit_reason,
                    ))
                    pos = None
                    if cfg.enforce_risk_limits:
                        day_loss_pct = (state.daily.realized_pnl_day /
                                        max(state.peak_equity, 1.0)) * 100.0
                        if day_loss_pct <= -s.max_daily_loss_pct:
                            paused_today = True
                        if state.drawdown_pct >= s.max_drawdown_pct:
                            paused_entirely = True

            # Evaluate signal at bar i's close (using only candles[:i+1]).
            # Phase 10: signals are evaluated ONLY inside the segment
            # (i >= segment_start_idx). The warm-up prefix supplies
            # indicator history but cannot generate scored trades.
            in_segment = (i >= segment_start_idx)
            warmup_ok = (i + 1 >= cfg.warmup_skip_bars
                         and i + 1 >= self.strategy.warmup_candles)
            if in_segment and warmup_ok:
                eligible_bars += 1

            if (pos is None and not paused_today and not paused_entirely
                    and in_segment and warmup_ok):
                prefix = all_candles[:i + 1]
                sigs = self.signal_engine.evaluate(
                    pair=pair, interval="1h",
                    candles=prefix, provenance=provenance,
                    risk_state=state)
                buy_sigs = [s for s in sigs
                            if s.state is SignalState.BUY_CANDIDATE]
                if buy_sigs:
                    best = max(buy_sigs,
                               key=lambda x: x.rr_after_cost or 0.0)
                    if i + 1 < len(all_candles):
                        entry = all_candles[i + 1].open
                        entry_adjusted = entry * (1 + s.slippage_pct / 100.0)
                        stop = best.stop
                        target = best.target
                        if stop >= entry_adjusted or target <= entry_adjusted:
                            pass
                        else:
                            plan = risk_engine.plan_position(
                                pair=pair, entry=entry_adjusted,
                                stop=stop, target=target, state=state)
                            if plan.decision is RiskDecision.ACCEPTED:
                                required = (plan.position_value
                                            + plan.est_entry_cost
                                            + plan.est_slippage_cost / 2)
                                if state.available_cash < required:
                                    pass
                                else:
                                    risk_engine.on_trade_opened(
                                        entry=entry_adjusted, stop=stop,
                                        qty=plan.qty, state=state)
                                    pos = {
                                        "entry": entry_adjusted,
                                        "stop": stop, "target": target,
                                        "qty": plan.qty,
                                        "entry_idx": i + 1,
                                        "entry_time_ms": all_candles[i + 1].open_time_ms,
                                        "strategy": best.strategy,
                                        "position_value": plan.position_value,
                                    }

            # End-of-bar equity mark — recorded ONLY for segment bars.
            if in_segment:
                if pos is not None:
                    cur_equity = (state.available_cash
                                  + pos["qty"] * all_candles[i].close)
                else:
                    cur_equity = state.current_equity
                equity_curve.append(
                    (all_candles[i].close_time_ms, cur_equity))

        # End of data: force-close any open position at last close.
        if pos is not None and all_candles:
            exit_price = all_candles[-1].close
            exit_idx = len(all_candles) - 1
            pnl = risk_engine.on_trade_closed(
                entry=pos["entry"], stop=pos["stop"], target=pos["target"],
                qty=pos["qty"], exit_price=exit_price, state=state,
                day_iso=_day_iso_from_ms(all_candles[-1].open_time_ms))
            entry_fee = pos["entry"] * pos["qty"] * s.fee_pct / 100.0
            exit_fee  = exit_price * pos["qty"] * s.fee_pct / 100.0
            slip      = (pos["entry"] * pos["qty"] * s.slippage_pct / 100.0
                         + exit_price * pos["qty"] * s.slippage_pct / 100.0)
            pnl_gross = (exit_price - pos["entry"]) * pos["qty"]
            trades.append(Trade(
                pair=pair, strategy=pos["strategy"],
                entry_idx=pos["entry_idx"], exit_idx=exit_idx,
                entry_time_ms=pos["entry_time_ms"],
                exit_time_ms=all_candles[-1].close_time_ms,
                entry=pos["entry"], stop=pos["stop"],
                target=pos["target"], exit_price=exit_price,
                qty=pos["qty"], position_value=pos["position_value"],
                pnl_gross=pnl_gross,
                entry_fee=entry_fee, exit_fee=exit_fee,
                slippage_cost=slip, pnl_net=pnl,
                exit_reason="end_of_data",
            ))
            pos = None
            # Equity snapshot for the final bar already recorded above
            # (in-segment branch), but the force-close changed realized
            # PnL AFTER the snapshot was taken. Patch the last equity
            # point to reflect post-close equity.
            if equity_curve:
                last_ts = equity_curve[-1][0]
                equity_curve[-1] = (last_ts, state.current_equity)

        return self._summarise(cfg, segment, pair, all_candles, trades,
                               equity_curve, state,
                               segment_start_idx=segment_start_idx,
                               warmup_prefix_len=(segment_start_idx
                                                  if warmup_prefix else 0),
                               eligible_bars=eligible_bars)

    # ------------------------------------------------------------------
    def _check_exit(self, pos, bar: Candle, cfg: BacktestConfig
                    ) -> Tuple[Optional[float], Optional[str]]:
        """Return (exit_price, exit_reason) if a stop or target is hit
        on this bar, else (None, None)."""
        stop = pos["stop"]; target = pos["target"]
        hit_stop   = bar.low <= stop
        hit_target = bar.high >= target
        if hit_stop and hit_target:
            if cfg.assume_stop_first_on_ambiguous_bar:
                # Pessimistic: assume the stop was hit first.
                return stop, "stop"
            else:
                return target, "target"
        if hit_stop:
            return stop, "stop"
        if hit_target:
            return target, "target"
        return None, None

    def risk_engine_for(self, starting_capital: float) -> RiskEngine:
        s = RiskSettings(
            initial_inr=starting_capital,
            risk_per_trade_pct=self.settings.risk_per_trade_pct,
            max_position_pct=self.settings.max_position_pct,
            max_portfolio_exposure_pct=self.settings.max_portfolio_exposure_pct,
            max_daily_loss_pct=self.settings.max_daily_loss_pct,
            max_daily_loss_abs_inr=self.settings.max_daily_loss_abs_inr,
            max_drawdown_pct=self.settings.max_drawdown_pct,
            max_open_positions=self.settings.max_open_positions,
            max_daily_trades=self.settings.max_daily_trades,
            reward_to_risk_target=self.settings.reward_to_risk_target,
            cooldown_after_consecutive_losses=self.settings.cooldown_after_consecutive_losses,
            allow_overnight=self.settings.allow_overnight,
            fee_pct=self.settings.fee_pct,
            spread_pct=self.settings.spread_pct,
            slippage_pct=self.settings.slippage_pct,
            min_24h_volume_inr=self.settings.min_24h_volume_inr,
            min_24h_trades=self.settings.min_24h_trades,
            trading_allocation_pct=self.settings.trading_allocation_pct,
        )
        return RiskEngine(s)

    # ------------------------------------------------------------------
    def _summarise(self, cfg: BacktestConfig, segment: str, pair: str,
                   candles: List[Candle], trades: List[Trade],
                   equity_curve: List[Tuple[int, float]],
                   state: RiskState,
                   *, segment_start_idx: int = 0,
                   warmup_prefix_len: int = 0,
                   eligible_bars: int = 0) -> BacktestResult:
        starting = cfg.starting_capital_inr
        ending = state.current_equity
        if equity_curve:
            # Equity at end-of-data — but if we force-closed, the state's
            # realized PnL already includes that close. Use equity_curve[-1].
            ending = equity_curve[-1][1]
        total_inr = ending - starting
        total_pct = (total_inr / starting) * 100.0 if starting > 0 else 0.0

        # Buy-and-hold benchmark: segment-only first/last prices.
        # Phase 10: previously the function used candles[0]/candles[-1],
        # which contaminated the benchmark with warm-up prefix data.
        # Now uses candles[segment_start_idx]/candles[-1] so the benchmark
        # reflects only the segment being evaluated.
        seg_candles = candles[segment_start_idx:] if segment_start_idx > 0 \
            else candles
        if seg_candles:
            bh_entry = seg_candles[0].open
            bh_exit  = seg_candles[-1].close
            units = starting / bh_entry if bh_entry > 0 else 0.0
            bh_ending = units * bh_exit
            bh_pct = ((bh_ending - starting) / starting) * 100.0 \
                if starting > 0 else 0.0
        else:
            bh_pct = 0.0

        # Drawdown
        peak = -math.inf
        max_dd = 0.0
        for _, eq in equity_curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak * 100.0
                if dd > max_dd:
                    max_dd = dd

        # Win rate, avg win/loss, profit factor, expectancy
        wins = [t for t in trades if t.is_win]
        losses = [t for t in trades if not t.is_win]
        win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
        avg_win = (sum(t.pnl_net for t in wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(t.pnl_net for t in losses) / len(losses)) if losses else 0.0
        gross_profit = sum(t.pnl_net for t in wins)
        gross_loss_abs = -sum(t.pnl_net for t in losses)
        profit_factor = (gross_profit / gross_loss_abs) \
            if gross_loss_abs > 0 else (float("inf")
                                         if gross_profit > 0 else 0.0)
        expectancy = (sum(t.pnl_net for t in trades) / len(trades)) \
            if trades else 0.0

        # Exposure (sum of holding bars / total bars) — segment bars only.
        total_holding_bars = sum(t.holding_bars for t in trades)
        total_bars = max(len(seg_candles), 1)
        exposure_pct = (total_holding_bars / total_bars) * 100.0

        # Turnover
        turnover_inr = sum(t.position_value for t in trades)
        est_fees = sum(t.entry_fee + t.exit_fee for t in trades)
        est_slip = sum(t.slippage_cost for t in trades)

        warnings = []
        # Distinguish zero-trade outcome from missing-data / calc failure.
        if not seg_candles:
            warnings.append("empty_segment: no candles in segment window")
        elif eligible_bars == 0 and not trades:
            warnings.append(
                f"no_eligible_bars: segment had {len(seg_candles)} bars "
                f"but 0 were eligible for signal evaluation "
                f"(warmup_skip_bars={cfg.warmup_skip_bars}, "
                f"strategy.warmup_candles={self.strategy.warmup_candles})")
        elif not trades and eligible_bars > 0:
            # Eligible bars existed but no BUY signals fired — record
            # this so callers can distinguish "no signals" from "no data".
            warnings.append(
                f"zero_trades_with_eligible_bars: {eligible_bars} eligible "
                f"bars evaluated but no BUY signals fired")

        return BacktestResult(
            config=cfg, segment=segment,
            starting_capital=starting, ending_equity=ending,
            total_return_inr=total_inr, total_return_pct=total_pct,
            net_pnl_after_costs=state.realized_pnl,
            max_drawdown_pct=max_dd,
            win_rate_pct=win_rate, avg_win_inr=avg_win,
            avg_loss_inr=avg_loss,
            profit_factor=profit_factor,
            expectancy_inr=expectancy,
            num_trades=len(trades),
            exposure_pct=exposure_pct,
            turnover_inr=turnover_inr,
            est_fees_inr=est_fees, est_slippage_inr=est_slip,
            buy_hold_return_pct=bh_pct,
            trades=trades,
            equity_curve=equity_curve,
            segment_start_idx=segment_start_idx,
            total_bars=len(seg_candles),
            eligible_bars=eligible_bars,
            warmup_prefix_len=warmup_prefix_len,
            warnings=warnings,
        )

    # ---------------------------------------------------- walk-forward split
    def walk_forward(self, *, pair: str, candles: List[Candle],
                     dev_end_iso: str, val_end_iso: str) -> Dict[str, BacktestResult]:
        """Run dev/val/oos splits by ISO date boundaries.

        Phase 10 — warm-up prefix
        -------------------------
        Each non-dev segment is run with a warm-up prefix taken from
        the END of the immediately-preceding segment, of length
        `strategy.warmup_candles`. This allows EMA200 (which needs 200
        bars to seed its SMA) to initialise BEFORE the scored segment
        begins, so trend-following strategies can actually fire on
        short segments (e.g., a 125-bar val window).

        Guarantees:
          * Indicators at bar i in segment N see only bars 0..i (prefix
            from segment N-1 + segment N). No future-data leakage.
          * Trades whose entry_idx < segment_start_idx are NOT scored
            (the run() loop enforces this via the `in_segment` gate).
          * Each segment starts with `pos = None` (no carried-in
            position). This means a trade that would have straddled
            a segment boundary is not represented — instead the
            segment evaluates signals on its own bars only.
          * Buy-and-hold uses each segment's own first/last prices.
          * Equity curve records only bars inside the segment.

        If the dev segment is itself shorter than warmup_candles,
        no prefix is available for val/oos — in that case we fall
        back to running the segment without a prefix and emit a
        warning (the segment will report 0 eligible bars).
        """
        # Partition candles chronologically by date boundary.
        dev, val, oos = [], [], []
        for c in candles:
            day = _day_iso_from_ms(c.open_time_ms)
            if day <= dev_end_iso:
                dev.append(c)
            elif day <= val_end_iso:
                val.append(c)
            else:
                oos.append(c)

        # Required warm-up length per strategy.
        warmup_n = max(self.strategy.warmup_candles,
                       self.config.warmup_skip_bars)

        def _prefix_for(prior_segs: List[List[Candle]]) -> List[Candle]:
            """Take the last `warmup_n` candles from prior segments
            (in chronological order). Returns [] if not enough data."""
            combined: List[Candle] = []
            for seg in prior_segs:
                combined.extend(seg)
            combined.sort(key=lambda c: c.open_time_ms)
            if len(combined) < warmup_n:
                return []  # signal: no usable prefix
            return combined[-warmup_n:]

        out: Dict[str, BacktestResult] = {}
        # DEV: first segment — no prefix available by definition.
        out["dev"] = self._run_segment(pair=pair, seg=dev, segment="dev",
                                        warmup_prefix=[], prior_segs=[])
        # VAL: prefix = last warmup_n of dev.
        out["val"] = self._run_segment(pair=pair, seg=val, segment="val",
                                       warmup_prefix=_prefix_for([dev]),
                                       prior_segs=[dev])
        # OOS: prefix = last warmup_n of (dev + val).
        out["oos"] = self._run_segment(pair=pair, seg=oos, segment="oos",
                                       warmup_prefix=_prefix_for([dev, val]),
                                       prior_segs=[dev, val])
        return out

    def _run_segment(self, *, pair: str, seg: List[Candle],
                     segment: str, warmup_prefix: List[Candle],
                     prior_segs: List[List[Candle]]) -> BacktestResult:
        """Run one walk-forward segment. If the segment itself is too
        short to produce ANY scored bars (with or without prefix), emit
        an explicit empty BacktestResult with a warning."""
        warmup_n = max(self.strategy.warmup_candles,
                       self.config.warmup_skip_bars)
        # Minimum segment size for any scoring: must have at least 1 bar.
        if not seg:
            return BacktestResult(
                config=self.config, segment=segment,
                starting_capital=self.config.starting_capital_inr,
                ending_equity=self.config.starting_capital_inr,
                total_return_inr=0.0, total_return_pct=0.0,
                net_pnl_after_costs=0.0, max_drawdown_pct=0.0,
                win_rate_pct=0.0, avg_win_inr=0.0, avg_loss_inr=0.0,
                profit_factor=0.0, expectancy_inr=0.0,
                num_trades=0, exposure_pct=0.0, turnover_inr=0.0,
                est_fees_inr=0.0, est_slippage_inr=0.0,
                buy_hold_return_pct=0.0,
                trades=[], equity_curve=[],
                segment_start_idx=0, total_bars=0,
                eligible_bars=0, warmup_prefix_len=len(warmup_prefix),
                warnings=[f"segment {segment} empty: 0 bars"])
        # If no prefix is available AND the segment is shorter than
        # warmup, signal eval will yield 0 eligible bars — still run
        # (so the equity curve and buy-hold are reported) but the
        # warning will be emitted by _summarise.
        result = self.run(pair=pair, candles=seg, segment=segment,
                          warmup_prefix=warmup_prefix or None,
                          segment_start_idx=len(warmup_prefix))
        return result


def _day_iso_from_ms(ms: int) -> str:
    """Return YYYY-MM-DD in UTC (used for daily-loss reset)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
