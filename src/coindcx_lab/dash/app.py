"""CoinDCX Spot Lab — Streamlit dashboard.

Run with:
    streamlit run src/coindcx_lab/dash/app.py

Sections:
    1. Market overview (with provenance badge)
    2. INR spot scanner (ranked table)
    3. Interactive candlestick chart with EMA overlays + indicators
    4. Multi-timeframe analysis (15m / 1h / 4h alignment)
    5. Candidate signal explanations (per strategy)
    6. Position-sizing calculator (live)
    7. Settings panel (capital & risk)
    8. Backtesting controls (walk-forward)
    9. Equity curve & drawdown
    10. Trade history
    11. Paper portfolio summary
    12. Cost breakdown
    13. System health & data freshness

The dashboard clearly labels SIMULATED data. No live orders. No real money.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import streamlit as st

# Ensure the package is importable when running this file directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from coindcx_lab.api import (  # noqa: E402
    Candle, CoinDCXAPIError, DataProvenance, PairInfo,
    SourceKind, SyntheticGenerator,
)
from coindcx_lab.api.multi_provider import MultiProviderClient  # noqa: E402
from coindcx_lab.engine import (  # noqa: E402
    BacktestConfig, Backtester, IndicatorSet, PaperTrade, PaperTradingEngine,
    RiskSettings, RiskState, Scanner, ScanResult, Signal, SignalEngine,
    SignalState, TrendDirection,
    TrendPullbackStrategy, BreakoutRetestStrategy,
    MomentumContinuationStrategy, BaselineSMAStrategy,
)
from coindcx_lab.engine.risk import RiskEngine, RiskDecision  # noqa: E402
from coindcx_lab.db.store import Store  # noqa: E402

# Try to load default settings from TOML if available; fall back to code defaults.
try:
    import tomllib  # py3.11+
    _CFG_PATH = os.path.abspath(os.path.join(_PKG_ROOT, "..", "config",
                                              "default_settings.toml"))
    if os.path.exists(_CFG_PATH):
        with open(_CFG_PATH, "rb") as f:
            _TOML = tomllib.load(f)
    else:
        _TOML = {}
except Exception:
    _TOML = {}


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_PAIRS = ["B-BTCINR", "B-ETHINR", "B-SOLINR", "B-MATICINR", "B-ADA INR".replace(" ", ""),
                 "B-DOTINR", "B-LTCINR", "B-XRPINR", "B-LINKINR", "B-AVAXINR"]
DEFAULT_PAIRS = [p for p in DEFAULT_PAIRS if " " not in p]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Session state setup
# ---------------------------------------------------------------------------
def _init_session_state() -> None:
    if "client" not in st.session_state:
        st.session_state.client = MultiProviderClient()
    if "synth" not in st.session_state:
        st.session_state.synth = SyntheticGenerator(seed=42)
    if "store" not in st.session_state:
        store_path = os.path.abspath(os.path.join(_PKG_ROOT, "..", "data",
                                                   "paper_trading.sqlite"))
        os.makedirs(os.path.dirname(store_path), exist_ok=True)
        st.session_state.store = Store(path=store_path)
    if "settings" not in st.session_state:
        st.session_state.settings = _default_settings_from_toml()
    if "paper_engine" not in st.session_state:
        st.session_state.paper_engine = PaperTradingEngine(
            settings=st.session_state.settings,
            store=st.session_state.store,
        )
    if "cache_candles" not in st.session_state:
        # cache_candles[pair][interval] = (List[Candle], DataProvenance)
        st.session_state.cache_candles: Dict[str, Dict[str, Tuple[List[Candle], DataProvenance]]] = {}
    if "cache_pairs" not in st.session_state:
        st.session_state.cache_pairs: Optional[Tuple[List[PairInfo], DataProvenance]] = None
    if "cache_prices" not in st.session_state:
        st.session_state.cache_prices: Optional[Tuple[dict, DataProvenance]] = None
    if "last_fetch_ms" not in st.session_state:
        st.session_state.last_fetch_ms = 0


def _default_settings_from_toml() -> RiskSettings:
    cap = _TOML.get("capital", {})
    rsk = _TOML.get("risk", {})
    cst = _TOML.get("costs", {})
    liq = _TOML.get("liquidity", {})
    sysm = _TOML.get("system", {})
    return RiskSettings(
        initial_inr=float(cap.get("initial_inr", 2000)),
        risk_per_trade_pct=float(rsk.get("risk_per_trade_pct", 1.0)),
        max_position_pct=float(rsk.get("max_position_pct", 25.0)),
        max_portfolio_exposure_pct=float(rsk.get("max_portfolio_exposure_pct", 80.0)),
        max_daily_loss_pct=float(rsk.get("max_daily_loss_pct", 5.0)),
        max_daily_loss_abs_inr=float(rsk.get("max_daily_loss_abs_inr", 0.0)),
        max_drawdown_pct=float(rsk.get("max_drawdown_pct", 15.0)),
        max_open_positions=int(rsk.get("max_open_positions", 5)),
        max_daily_trades=int(rsk.get("max_daily_trades", 10)),
        reward_to_risk_target=float(rsk.get("reward_to_risk_target", 2.0)),
        cooldown_after_consecutive_losses=int(rsk.get("cooldown_after_consecutive_losses", 3)),
        allow_overnight=bool(rsk.get("allow_overnight", True)),
        fee_pct=float(cst.get("fee_pct", 0.1)),
        spread_pct=float(cst.get("spread_pct", 0.05)),
        slippage_pct=float(cst.get("slippage_pct", 0.05)),
        min_24h_volume_inr=float(liq.get("min_24h_volume_inr", 5_000_000)),
        min_24h_trades=int(liq.get("min_24h_trades", 1000)),
        trading_allocation_pct=float(rsk.get("trading_allocation_pct",
                                              cap.get("trading_allocation_pct", 100.0))),
    )


# ---------------------------------------------------------------------------
# Data fetchers (live-with-fallback)
# ---------------------------------------------------------------------------
def _live_pairs_or_sim() -> Tuple[List[PairInfo], DataProvenance, str]:
    """Try to fetch pairs list live; fall back to a clearly-labelled
    SIMULATED pairs list if the CoinDCX public API is unreachable.
    """
    client: MultiProviderClient = st.session_state.client
    try:
        pairs, prov = client.list_inr_spot_pairs()
        if pairs:
            return pairs, prov, "live"
    except CoinDCXAPIError as e:
        st.session_state.live_unavailable_reason = str(e)
    except Exception as e:
        st.session_state.live_unavailable_reason = f"{type(e).__name__}: {e}"

    # SIMULATED fallback. Each default pair gets a PairInfo with the
    # is_inr_quote flag set so downstream code behaves identically.
    synth_pairs = [
        PairInfo(pair=p, base=p[2:].replace("INR", ""), quote="INR",
                 status="active", is_inr_quote=True)
        for p in DEFAULT_PAIRS
    ]
    prov = DataProvenance(
        source=SourceKind.SIMULATED,
        endpoint="synthetic://pairs",
        fetched_at_utc=datetime.now(UTC),
        note="CoinDCX public API unreachable from this host; using the "
             "documented default INR spot pair list as SIMULATED.",
    )
    return synth_pairs, prov, "simulated"


def _live_candles_or_sim(pair: str, interval: str,
                         limit: int = 250) -> Tuple[List[Candle], DataProvenance, str]:
    """Try to fetch live candles; fall back to a SIMULATED series."""
    cache: Dict[str, Dict[str, Tuple[List[Candle], DataProvenance]]] = \
        st.session_state.cache_candles
    if pair in cache and interval in cache[pair]:
        candles, prov = cache[pair][interval]
        if (datetime.now(UTC) - prov.fetched_at_utc.replace(tzinfo=UTC)
                ).total_seconds() < 60:
            return candles, prov, "cached"

    client: MultiProviderClient = st.session_state.client
    try:
        candles, prov = client.candles(pair=pair, interval=interval, limit=limit)
        if candles:
            cache.setdefault(pair, {})[interval] = (candles, prov)
            return candles, prov, "live"
    except CoinDCXAPIError as e:
        st.session_state.live_unavailable_reason = str(e)
    except Exception as e:
        st.session_state.live_unavailable_reason = f"{type(e).__name__}: {e}"

    # SIMULATED fallback (clearly labelled, deterministic)
    synth = st.session_state.synth
    # Choose a plausible start price per pair so simulated series look
    # different across pairs.
    base_price = {
        "B-BTCINR":   7_000_000,
        "B-ETHINR":   350_000,
        "B-SOLINR":   18_000,
        "B-MATICINR": 90,
        "B-ADA INR".replace(" ", ""): 65,
        "B-DOTINR":   700,
        "B-LTCINR":   8_000,
        "B-XRPINR":   65,
        "B-LINKINR":  1_800,
        "B-AVAXINR": 4_000,
    }.get(pair, 1000)
    candles, prov = synth.series(pair=pair, interval=interval, count=limit,
                                  start_price=base_price,
                                  drift_per_bar=0.0002,
                                  vol_per_bar=0.012,
                                  jump_prob=0.02,
                                  jump_size=0.04)
    cache.setdefault(pair, {})[interval] = (candles, prov)
    return candles, prov, "simulated"


def _live_prices_or_sim() -> Tuple[dict, DataProvenance, str]:
    client: MultiProviderClient = st.session_state.client
    try:
        prices, prov = client.current_prices()
        if prices:
            return prices, prov, "live"
    except CoinDCXAPIError as e:
        st.session_state.live_unavailable_reason = str(e)
    except Exception as e:
        st.session_state.live_unavailable_reason = f"{type(e).__name__}: {e}"

    # SIMULATED prices derived from cached candles (or fresh synth ones)
    out: dict = {}
    for pair in DEFAULT_PAIRS:
        candles, _, _ = _live_candles_or_sim(pair, "1h", limit=48)
        out[pair] = st.session_state.synth.current_prices_for(pair, candles)
    prov = DataProvenance(
        source=SourceKind.SIMULATED,
        endpoint="synthetic://prices",
        fetched_at_utc=datetime.now(UTC),
        note="CoinDCX public API unreachable; SIMULATED prices derived from "
             "deterministic synthetic candles.",
    )
    return out, prov, "simulated"


# ---------------------------------------------------------------------------
# UI components
# ---------------------------------------------------------------------------
def _badge(label: str, kind: str = "info") -> str:
    color_map = {
        "info": "#1f77b4",
        "ok": "#2ca02c",
        "warn": "#ff7f0e",
        "err": "#d62728",
        "sim": "#9467bd",
    }
    color = color_map.get(kind, color_map["info"])
    return (f'<span style="background:{color};color:white;padding:'
            f'2px 8px;border-radius:6px;font-size:0.78em;font-weight:600">'
            f'{label}</span>')


def _render_provenance_badge(prov: DataProvenance) -> None:
    label = prov.label()
    kind = {"live": "ok", "cached": "info", "stale": "warn",
            "simulated": "sim", "unavailable": "err"}.get(prov.source.value, "info")
    st.markdown(_badge(label, kind=kind), unsafe_allow_html=True)
    if prov.server_time_utc:
        st.caption(f"Server time: {prov.server_time_utc.astimezone(IST).isoformat()}")


def _sidebar_settings() -> None:
    st.sidebar.title("⚙️ Settings")
    s = st.session_state.settings

    st.sidebar.markdown("### Capital (INR)")
    presets = _TOML.get("capital", {}).get("presets_inr",
                                            [2000, 5000, 10000, 25000, 50000])
    preset_choice = st.sidebar.selectbox(
        "Capital preset", ["Custom"] + [f"₹{p:,}" for p in presets],
        index=0,
    )
    if preset_choice == "Custom":
        new_capital = st.sidebar.number_input(
            "Custom initial capital (INR)", min_value=1.0, max_value=10_000_000.0,
            value=float(s.initial_inr), step=100.0,
        )
    else:
        new_capital = float(preset_choice.replace("₹", "").replace(",", ""))

    st.sidebar.markdown("### Risk")
    new_risk_pct = st.sidebar.slider("Risk per trade (%)",
                                     min_value=0.1, max_value=5.0,
                                     value=float(s.risk_per_trade_pct), step=0.1)
    new_max_pos = st.sidebar.slider("Max position size (% of equity)",
                                     min_value=5.0, max_value=100.0,
                                     value=float(s.max_position_pct), step=1.0)
    new_max_exp = st.sidebar.slider("Max portfolio exposure (%)",
                                     min_value=10.0, max_value=100.0,
                                     value=float(s.max_portfolio_exposure_pct), step=5.0)
    new_daily_loss = st.sidebar.slider("Max daily loss (%)",
                                        min_value=1.0, max_value=20.0,
                                        value=float(s.max_daily_loss_pct), step=0.5)
    new_max_dd = st.sidebar.slider("Max drawdown (%)",
                                    min_value=5.0, max_value=50.0,
                                    value=float(s.max_drawdown_pct), step=1.0)
    new_max_open = st.sidebar.slider("Max open positions",
                                       min_value=1, max_value=10,
                                       value=int(s.max_open_positions), step=1)
    new_max_daily_trades = st.sidebar.slider("Max daily trades",
                                              min_value=1, max_value=50,
                                              value=int(s.max_daily_trades), step=1)
    new_rr_target = st.sidebar.slider("Reward-to-risk target",
                                       min_value=1.0, max_value=5.0,
                                       value=float(s.reward_to_risk_target), step=0.25)
    new_cooldown = st.sidebar.slider("Cooldown after N consecutive losses",
                                       min_value=0, max_value=10,
                                       value=int(s.cooldown_after_consecutive_losses), step=1)
    new_overnight = st.sidebar.checkbox("Allow overnight positions",
                                          value=bool(s.allow_overnight))

    st.sidebar.markdown("### Costs (estimated)")
    new_fee = st.sidebar.slider("Fee per side (%)", min_value=0.0, max_value=1.0,
                                  value=float(s.fee_pct), step=0.01)
    new_spread = st.sidebar.slider("Spread (%)", min_value=0.0, max_value=0.5,
                                     value=float(s.spread_pct), step=0.01)
    new_slip = st.sidebar.slider("Slippage (%)", min_value=0.0, max_value=0.5,
                                   value=float(s.slippage_pct), step=0.01)

    st.sidebar.markdown("### Liquidity gates")
    new_min_vol = st.sidebar.number_input(
        "Min 24h volume (INR)", min_value=0.0, value=float(s.min_24h_volume_inr),
        step=1_000_000.0, format="%f",
    )
    new_min_trades = st.sidebar.number_input(
        "Min 24h trades", min_value=0, value=int(s.min_24h_trades), step=100,
    )

    st.sidebar.markdown("### Strategies")
    en_tp = st.sidebar.checkbox("Trend pullback", value=True)
    en_br = st.sidebar.checkbox("Breakout & retest", value=True)
    en_mc = st.sidebar.checkbox("Momentum continuation", value=True)
    en_bs = st.sidebar.checkbox("Baseline SMA", value=True)

    if st.sidebar.button("Apply settings", type="primary"):
        new_settings = RiskSettings(
            initial_inr=new_capital,
            risk_per_trade_pct=new_risk_pct,
            max_position_pct=new_max_pos,
            max_portfolio_exposure_pct=new_max_exp,
            max_daily_loss_pct=new_daily_loss,
            max_daily_loss_abs_inr=s.max_daily_loss_abs_inr,
            max_drawdown_pct=new_max_dd,
            max_open_positions=new_max_open,
            max_daily_trades=new_max_daily_trades,
            reward_to_risk_target=new_rr_target,
            cooldown_after_consecutive_losses=new_cooldown,
            allow_overnight=new_overnight,
            fee_pct=new_fee,
            spread_pct=new_spread,
            slippage_pct=new_slip,
            min_24h_volume_inr=new_min_vol,
            min_24h_trades=int(new_min_trades),
            trading_allocation_pct=s.trading_allocation_pct,
        )
        try:
            new_settings.validate()
            st.session_state.settings = new_settings
            # Update paper engine for FUTURE trades. Preserves history.
            st.session_state.paper_engine.update_settings(new_settings)
            st.sidebar.success("Settings applied (future trades only).")
        except Exception as e:
            st.sidebar.error(f"Invalid settings: {e}")

    st.sidebar.markdown("---")
    st.sidebar.markdown("**Disclaimer**")
    st.sidebar.caption(
        "Research & education only. No live orders, no real money, "
        "no leverage. Live execution can produce losses greater than "
        "the planned stop risk due to gaps, slippage, or unavailable "
        "liquidity."
    )


def _render_market_overview() -> Tuple[List[PairInfo], DataProvenance, dict, DataProvenance]:
    st.header("1) Market Overview")
    pairs, pairs_prov, mode_pairs = _live_pairs_or_sim()
    prices, prices_prov, mode_prices = _live_prices_or_sim()
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("**Pairs source**")
        _render_provenance_badge(pairs_prov)
        st.metric("Pairs discovered", len(pairs))
    with col2:
        st.markdown("**Prices source**")
        _render_provenance_badge(prices_prov)
        st.metric("Pairs with prices", len(prices))
    with col3:
        st.markdown("**System health**")
        st.metric("Last fetch (UTC)",
                  datetime.now(UTC).strftime("%H:%M:%S"))
        if "live_unavailable_reason" in st.session_state:
            st.warning(f"Live data unavailable: "
                       f"{st.session_state.live_unavailable_reason[:200]}")
        else:
            st.success("Live CoinDCX API reachable")
    if mode_pairs == "simulated" or mode_prices == "simulated":
        st.info("ℹ️ **SIMULATED data is being shown.** Live CoinDCX public API "
                "is unreachable from this host (Cloudflare edge block). "
                "Synthetic series are deterministic and clearly labelled per "
                "chart. No live prices are fabricated.")
    return pairs, pairs_prov, prices, prices_prov


def _render_scanner(pairs: List[PairInfo], prices: dict,
                    prices_prov: DataProvenance) -> List[ScanResult]:
    st.header("2) INR Spot Market Scanner")
    settings = st.session_state.settings
    interval = st.selectbox("Scanner interval",
                            ["15m", "1h", "4h", "1d"], index=1)
    if st.button("Run scan", type="primary"):
        with st.spinner("Fetching candles for all pairs…"):
            cbp: Dict[str, Tuple[List[Candle], DataProvenance]] = {}
            for p in pairs:
                candles, prov, _ = _live_candles_or_sim(p.pair, interval, limit=300)
                cbp[p.pair] = (candles, prov)
            signal_engine = SignalEngine(settings=settings)
            scanner = Scanner(settings=settings, signal_engine=signal_engine)
            paper_engine: PaperTradingEngine = st.session_state.paper_engine
            results = scanner.scan(
                pairs=pairs, candles_by_pair=cbp,
                tickers_by_pair=prices, risk_state=paper_engine.state,
                interval=interval,
            )
            st.session_state.scan_results = results
            st.session_state.scan_interval = interval
            st.session_state.scan_candles = cbp
    if "scan_results" in st.session_state:
        results: List[ScanResult] = st.session_state.scan_results
        df = pd.DataFrame([r.to_dict() for r in results])
        df = df.sort_values("score", ascending=False)
        st.markdown("**Ranked scan results** (score 0-100, higher = better)")
        st.dataframe(df, use_container_width=True, height=400)
        excluded = [r for r in results if r.excluded]
        if excluded:
            with st.expander(f"Excluded markets ({len(excluded)})"):
                ex_df = pd.DataFrame([
                    {"pair": r.pair, "reason": r.exclusion_reason,
                     "volume_24h_inr": r.volume_24h_inr}
                    for r in excluded
                ])
                st.dataframe(ex_df, use_container_width=True)
        return results
    st.caption("Click 'Run scan' to evaluate all visible pairs.")
    return []


def _render_charts(pairs: List[PairInfo]) -> None:
    st.header("3) Candlestick Chart with Indicator Overlays")
    pair_options = {p.pair: p for p in pairs}
    pair = st.selectbox("Symbol", list(pair_options.keys()),
                        index=0,
                        format_func=lambda p: pair_options[p].display_symbol
                            if p in pair_options else p)
    interval = st.selectbox("Interval", ["15m", "1h", "4h", "1d"], index=1)
    candles, prov, _ = _live_candles_or_sim(pair, interval, limit=300)
    ind = IndicatorSet.from_candles(candles)
    st.markdown("**Data source**")
    _render_provenance_badge(prov)
    df = pd.DataFrame([
        {"time": datetime.fromtimestamp(c.open_time_ms / 1000, tz=IST),
         "open": c.open, "high": c.high, "low": c.low, "close": c.close,
         "volume": c.volume}
        for c in candles
    ]).set_index("time")
    # Candlestick + EMA overlays + volume
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.08, row_heights=[0.7, 0.3],
                        subplot_titles=("OHLC + EMAs", "Volume"))
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"],
        low=df["low"], close=df["close"], name="OHLC"),
        row=1, col=1)
    if len(candles) >= 20:
        fig.add_trace(go.Scatter(x=df.index, y=ind.ema20, name="EMA20",
                                  line=dict(width=1.2, color="#1f77b4")),
                      row=1, col=1)
    if len(candles) >= 50:
        fig.add_trace(go.Scatter(x=df.index, y=ind.ema50, name="EMA50",
                                  line=dict(width=1.2, color="#ff7f0e")),
                      row=1, col=1)
    if len(candles) >= 200:
        fig.add_trace(go.Scatter(x=df.index, y=ind.ema200, name="EMA200",
                                  line=dict(width=1.5, color="#2ca02c")),
                      row=1, col=1)
    fig.add_trace(go.Bar(x=df.index, y=df["volume"], name="Volume",
                          marker_color="#888"), row=2, col=1)
    fig.update_layout(height=560, xaxis_rangeslider_visible=False,
                      margin=dict(l=10, r=10, t=40, b=10))
    fig.update_yaxes(title_text="Price (INR)", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    st.plotly_chart(fig, use_container_width=True)

    # Indicator strip: RSI
    if len(candles) >= 15:
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=df.index, y=ind.rsi14, name="RSI14",
                                    line=dict(color="#9467bd", width=1.5)))
        fig2.add_hline(y=70, line_dash="dash", line_color="red")
        fig2.add_hline(y=30, line_dash="dash", line_color="green")
        fig2.update_layout(height=240,
                            title="RSI (14)",
                            margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig2, use_container_width=True)

    # MACD
    if len(candles) >= 35:
        fig3 = make_subplots(rows=2, cols=1, shared_xaxes=True,
                              vertical_spacing=0.05,
                              row_heights=[0.6, 0.4],
                              subplot_titles=("MACD line + signal", "Histogram"))
        fig3.add_trace(go.Scatter(x=df.index, y=ind.macd_line, name="MACD",
                                    line=dict(color="#1f77b4", width=1.5)),
                        row=1, col=1)
        fig3.add_trace(go.Scatter(x=df.index, y=ind.macd_signal, name="Signal",
                                    line=dict(color="#ff7f0e", width=1.2)),
                        row=1, col=1)
        colors = ["#2ca02c" if v >= 0 else "#d62728" for v in ind.macd_hist]
        fig3.add_trace(go.Bar(x=df.index, y=ind.macd_hist, name="Hist",
                                marker_color=colors), row=2, col=1)
        fig3.update_layout(height=320, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig3, use_container_width=True)


def _render_multi_timeframe(pairs: List[PairInfo]) -> None:
    st.header("4) Multi-Timeframe Analysis")
    pair = st.selectbox("Symbol (MTF)", [p.pair for p in pairs], index=0)
    intervals = ["15m", "1h", "4h"]
    cols = st.columns(len(intervals))
    settings = st.session_state.settings
    signal_engine = SignalEngine(settings=settings)
    paper_engine: PaperTradingEngine = st.session_state.paper_engine
    for i, iv in enumerate(intervals):
        with cols[i]:
            st.subheader(iv)
            candles, prov, _ = _live_candles_or_sim(pair, iv, limit=300)
            _render_provenance_badge(prov)
            ind = IndicatorSet.from_candles(candles)
            last_i = ind.last_idx
            if last_i >= 0:
                td = ind.trend_at(last_i).value
                mom = ind.momentum_at(last_i)
                rsi = float(ind.rsi14[last_i]) if not np.isnan(ind.rsi14[last_i]) else None
                st.metric("Trend", td)
                st.metric("Momentum", mom)
                st.metric("RSI14", f"{rsi:.1f}" if rsi is not None else "—")
            sigs = signal_engine.evaluate(
                pair=pair, interval=iv, candles=candles,
                provenance=prov, risk_state=paper_engine.state)
            for s in sigs:
                with st.expander(f"{s.strategy} — {s.state.value}"):
                    st.caption(f"Entry: {s.entry} | Stop: {s.stop} | "
                                f"Target: {s.target} | "
                                f"R/R after cost: {s.rr_after_cost}")
                    for r in s.supporting:
                        st.markdown(f"+ {r.text}")
                    for r in s.conflicting:
                        st.markdown(f"- {r.text}")
                    if s.data_quality_warnings:
                        st.warning(" | ".join(s.data_quality_warnings))


def _render_position_calculator() -> None:
    st.header("5) Position-Sizing Calculator")
    settings = st.session_state.settings
    paper_engine: PaperTradingEngine = st.session_state.paper_engine
    col1, col2, col3 = st.columns(3)
    with col1:
        entry = st.number_input("Entry (INR)", min_value=1.0, value=1000.0, step=1.0)
    with col2:
        stop = st.number_input("Stop (INR)", min_value=0.01, value=950.0, step=1.0)
    with col3:
        target = st.number_input("Target (INR)", min_value=1.0, value=1100.0, step=1.0)
    try:
        plan = paper_engine.risk_engine.plan_position(
            pair="CALC", entry=float(entry), stop=float(stop),
            target=float(target), state=paper_engine.state)
        st.markdown(f"**Decision:** {_badge(plan.decision.value, 'ok' if plan.decision is RiskDecision.ACCEPTED else 'err')}",
                    unsafe_allow_html=True)
        if plan.reason:
            st.caption(f"Reason: {plan.reason}")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Position value (INR)", f"{plan.position_value:.2f}")
        c2.metric("Quantity", f"{plan.qty:.6f}")
        c3.metric("Risk budget (INR)", f"{plan.risk_budget:.2f}")
        c4.metric("Cap reason", plan.cap_reason)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Potential loss (net)", f"{plan.potential_loss_net:.2f}")
        c2.metric("Potential gain (net)", f"{plan.potential_gain_net:.2f}")
        c3.metric("R/R after cost",
                  f"{plan.rr_after_cost:.2f}" if plan.rr_after_cost else "—")
        c4.metric("Est. costs (entry+exit)",
                  f"{plan.est_entry_cost + plan.est_exit_cost:.2f}")
    except Exception as e:
        st.error(f"Invalid input: {e}")


def _render_paper_portfolio() -> None:
    st.header("6) Paper Portfolio")
    pe: PaperTradingEngine = st.session_state.paper_engine
    summ = pe.portfolio_summary()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Initial equity (INR)", f"{summ['initial_equity']:.2f}")
    c2.metric("Current equity (INR)", f"{summ['current_equity']:.2f}")
    c3.metric("Available cash (INR)", f"{summ['available_cash']:.2f}")
    c4.metric("Open positions", f"{summ['open_positions_count']} / "
                                    f"{summ['max_open_positions']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open positions value", f"{summ['open_positions_value']:.2f}")
    c2.metric("Realized P&L", f"{summ['realized_pnl']:.2f}")
    c3.metric("Unrealized P&L", f"{summ['unrealized_pnl']:.2f}")
    c4.metric("Drawdown %", f"{summ['drawdown_pct']:.2f}%")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Risk budget/trade (INR)", f"{summ['risk_budget_per_trade']:.2f}")
    c2.metric("Daily realized P&L", f"{summ['daily_realized_pnl']:.2f}")
    c3.metric("Daily trades",
              f"{summ['daily_trades']} / {summ['max_daily_trades']}")
    c4.metric("Consecutive losses", summ["consecutive_losses"])
    if summ["paused"]:
        st.warning("Engine PAUSED. New entries blocked.")
    if summ["cooldown_until"]:
        st.warning(f"Cooldown until {summ['cooldown_until']}")

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("Pause", disabled=summ["paused"]):
            pe.pause()
            st.rerun()
    with col2:
        if st.button("Resume", disabled=not summ["paused"]):
            pe.resume()
            st.rerun()
    with col3:
        _confirm = st.checkbox("I confirm — wipe ALL paper history")
        if st.button("🚨 Reset (DANGER)", disabled=not _confirm,
                      type="secondary"):
            ok = pe.reset(require_confirmation=True,
                           confirmation_token="RESET-ALL-CONFIRMED" if _confirm else None)
            if ok:
                st.success("Reset complete.")
                st.rerun()
            else:
                st.error("Reset requires confirmation.")


def _render_trade_history() -> None:
    st.header("7) Trade History")
    pe: PaperTradingEngine = st.session_state.paper_engine
    trades = pe.list_trades(limit=200)
    if not trades:
        st.caption("No paper trades yet.")
        return
    df = pd.DataFrame([t.to_dict() for t in trades])
    df["entry_time"] = pd.to_datetime(df["entry_time_ms"], unit="ms",
                                       utc=True).dt.tz_convert(IST)
    if "exit_time_ms" in df:
        df["exit_time"] = pd.to_datetime(df["exit_time_ms"], unit="ms",
                                          utc=True, errors="coerce").dt.tz_convert(IST)
    df = df.sort_values("entry_time", ascending=False)
    st.dataframe(df, use_container_width=True, height=360)


def _render_equity_curve() -> None:
    st.header("8) Equity Curve & Drawdown")
    pe: PaperTradingEngine = st.session_state.paper_engine
    snaps = pe.store.list_equity_snapshots(limit=2000)
    if not snaps:
        st.caption("No equity snapshots yet — open or close a paper trade.")
        return
    df = pd.DataFrame(snaps)
    df["time"] = pd.to_datetime(df["time_ms"], unit="ms",
                                  utc=True).dt.tz_convert(IST)
    df = df.sort_values("time")
    # Drawdown series
    df["peak"] = df["equity"].cummax()
    df["dd_pct"] = (df["peak"] - df["equity"]) / df["peak"].replace(0, np.nan) * 100
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.08, row_heights=[0.7, 0.3],
                        subplot_titles=("Equity (INR)", "Drawdown %"))
    fig.add_trace(go.Scatter(x=df["time"], y=df["equity"], name="Equity",
                              line=dict(color="#1f77b4", width=2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df["time"], y=df["peak"], name="Peak",
                              line=dict(color="#2ca02c", dash="dot", width=1)),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=df["time"], y=df["dd_pct"], name="Drawdown %",
                              fill="tozeroy",
                              line=dict(color="#d62728", width=1.5)),
                  row=2, col=1)
    fig.update_layout(height=480, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)


def _render_backtest(pairs: List[PairInfo]) -> None:
    st.header("9) Backtesting")
    pair = st.selectbox("Symbol (backtest)",
                        [p.pair for p in pairs], index=0)
    interval = st.selectbox("Interval (backtest)",
                             ["15m", "1h", "4h", "1d"], index=1)
    strategy_name = st.selectbox("Strategy",
                                   ["trend_pullback", "breakout_retest",
                                    "momentum_continuation", "baseline_sma"])
    starting_capital = st.number_input("Starting capital (INR)",
                                        min_value=100.0, value=2000.0, step=100.0)
    do_walk = st.checkbox("Walk-forward (dev/val/oos split)", value=True)

    if st.button("Run backtest", type="primary"):
        with st.spinner("Fetching candles and running event-driven backtest…"):
            candles, prov, _ = _live_candles_or_sim(pair, interval, limit=800)
            settings = st.session_state.settings
            strategies = {
                "trend_pullback": TrendPullbackStrategy(),
                "breakout_retest": BreakoutRetestStrategy(),
                "momentum_continuation": MomentumContinuationStrategy(),
                "baseline_sma": BaselineSMAStrategy(),
            }
            strat = strategies[strategy_name]
            signal_engine = SignalEngine(settings=settings,
                                          strategies=[strat])
            cfg = BacktestConfig(starting_capital_inr=float(starting_capital))
            bt = Backtester(settings=settings, strategy=strat,
                            signal_engine=signal_engine, config=cfg)
            if do_walk:
                # Use the candle time span to derive sensible splits
                if len(candles) >= 90:
                    mid = candles[len(candles)//2].open_time_ms
                    early = candles[len(candles)//4].open_time_ms
                    from datetime import datetime as _dt
                    dev_end = _dt.fromtimestamp(early / 1000, tz=UTC).date().isoformat()
                    val_end = _dt.fromtimestamp(mid / 1000, tz=UTC).date().isoformat()
                    results = bt.walk_forward(pair=pair, candles=candles,
                                              dev_end_iso=dev_end,
                                              val_end_iso=val_end)
                    segs = ["dev", "val", "oos"]
                else:
                    results = {"full": bt.run(pair=pair, candles=candles)}
                    segs = ["full"]
            else:
                results = {"full": bt.run(pair=pair, candles=candles)}
                segs = ["full"]
            st.session_state.bt_results = results
            st.session_state.bt_pair = pair
            st.session_state.bt_candles = candles
            st.session_state.bt_provenance = prov

    if "bt_results" in st.session_state:
        results = st.session_state.bt_results
        prov: DataProvenance = st.session_state.bt_provenance
        st.markdown("**Backtest data source**")
        _render_provenance_badge(prov)
        for seg, r in results.items():
            st.subheader(f"Segment: {seg}")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total return", f"{r.total_return_pct:.2f}% "
                                        f"(₹{r.total_return_inr:.2f})")
            c2.metric("Max drawdown", f"{r.max_drawdown_pct:.2f}%")
            c3.metric("Win rate", f"{r.win_rate_pct:.1f}% ({r.num_trades} trades)")
            c4.metric("Profit factor",
                      f"{r.profit_factor:.2f}" if math.isfinite(r.profit_factor) else "—")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Avg win (₹)", f"{r.avg_win_inr:.2f}")
            c2.metric("Avg loss (₹)", f"{r.avg_loss_inr:.2f}")
            c3.metric("Expectancy (₹)", f"{r.expectancy_inr:.2f}")
            c4.metric("Buy&hold", f"{r.buy_hold_return_pct:.2f}%")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Exposure %", f"{r.exposure_pct:.1f}%")
            c2.metric("Turnover (₹)", f"{r.turnover_inr:.0f}")
            c3.metric("Est. fees (₹)", f"{r.est_fees_inr:.2f}")
            c4.metric("Est. slippage (₹)", f"{r.est_slippage_inr:.2f}")
            if r.warnings:
                st.warning(" | ".join(r.warnings))
            if r.equity_curve:
                eq_df = pd.DataFrame(r.equity_curve,
                                      columns=["time_ms", "equity"])
                eq_df["time"] = pd.to_datetime(eq_df["time_ms"], unit="ms",
                                                utc=True).dt.tz_convert(IST)
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=eq_df["time"], y=eq_df["equity"],
                                          name="Equity",
                                          line=dict(color="#1f77b4", width=2)))
                fig.update_layout(title=f"Equity curve ({seg})",
                                   height=320,
                                   margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig, use_container_width=True)
            if r.trades:
                trades_df = pd.DataFrame([
                    {"entry_idx": t.entry_idx, "exit_idx": t.exit_idx,
                     "entry": t.entry, "exit_price": t.exit_price,
                     "qty": t.qty, "pnl_net": t.pnl_net,
                     "exit_reason": t.exit_reason, "strategy": t.strategy}
                    for t in r.trades
                ])
                with st.expander(f"Trade ledger ({len(r.trades)} trades)"):
                    st.dataframe(trades_df, use_container_width=True)


def _render_system_health() -> None:
    st.header("10) System Health & Data Freshness")
    pe: PaperTradingEngine = st.session_state.paper_engine
    s = pe.settings
    c1, c2, c3 = st.columns(3)
    c1.metric("Python", "3.12")
    c2.metric("Streamlit", "1.65")
    c3.metric("Timezone", "Asia/Kolkata")
    st.markdown("**API endpoints (documented, public, no-auth):**")
    st.code(
        "GET  https://public.coindcx.com/market_data/v3/current_prices/all_pairs\n"
        "GET  https://public.coindcx.com/market_data/v3/exchange_pairs\n"
        "POST https://public.coindcx.com/market_data/v3/candles\n"
        "    body: {\"pair\": \"B-BTCINR\", \"interval\": \"1h\", \"limit\": 200}",
        language="text",
    )
    if "live_unavailable_reason" in st.session_state:
        st.error(f"Live CoinDCX API error: "
                 f"{st.session_state.live_unavailable_reason[:300]}")
    else:
        st.success("Live CoinDCX API reachable")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="CoinDCX Spot Lab",
                       page_icon="📊",
                       layout="wide",
                       initial_sidebar_state="expanded")
    st.title("📊 CoinDCX Spot Lab — Advanced Crypto Analysis & Paper Trading")
    st.caption("Research & education only. No live orders. No real money. "
                "No leverage. No API keys.")
    _init_session_state()
    _sidebar_settings()
    pairs, pairs_prov, prices, prices_prov = _render_market_overview()
    _render_scanner(pairs, prices, prices_prov)
    _render_charts(pairs)
    _render_multi_timeframe(pairs)
    _render_position_calculator()
    _render_paper_portfolio()
    _render_trade_history()
    _render_equity_curve()
    _render_backtest(pairs)
    _render_system_health()


if __name__ == "__main__":
    main()
