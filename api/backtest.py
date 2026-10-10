"""POST /api/backtest

Body: {"pair": "B-BTCINR", "interval": "1h", "strategy": "baseline_sma",
       "starting_capital_inr": 2000, "walk_forward": true}

Runs an event-driven backtest. Returns full result: total return,
max drawdown, win rate, profit factor, expectancy, equity curve,
trade ledger.

No look-ahead bias. Conservative intrabar stop-first handling.
"""

import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from http.server import BaseHTTPRequestHandler

from coindcx_lab.server.common import (
    handle_preflight, read_json_body, send_error, send_json,
)


class handler(BaseHTTPRequestHandler):

    def _handle(self, method: str) -> None:
        if handle_preflight(method, self):
            return
        if method != "POST":
            send_error(self, 405, "method_not_allowed", "Use POST")
            return
        try:
            body, err = read_json_body(self)
            if err:
                send_error(self, 400, "bad_request", err)
                return
            if body is None:
                body = {}
            pair = body.get("pair", "B-BTCINR")
            interval = body.get("interval", "1h")
            strategy_name = body.get("strategy", "baseline_sma")
            starting_capital = float(body.get("starting_capital_inr", 2000.0))
            walk_forward = bool(body.get("walk_forward", True))

            from coindcx_lab.api.client import CoinDCXClient, CoinDCXAPIError
            from coindcx_lab.api.models import DataProvenance, SourceKind
            from coindcx_lab.api.synthetic import SyntheticGenerator
            from coindcx_lab.engine.backtest import (
                BacktestConfig, Backtester, _day_iso_from_ms,
            )
            from coindcx_lab.engine.risk import RiskSettings
            from coindcx_lab.engine.signals import (
                BaselineSMAStrategy, BreakoutRetestStrategy,
                MomentumContinuationStrategy, SignalEngine,
                TrendPullbackStrategy,
            )
            from datetime import datetime, timezone, timedelta
            from coindcx_lab.api.validate import validate_candles

            strategies = {
                "trend_pullback": TrendPullbackStrategy(),
                "breakout_retest": BreakoutRetestStrategy(),
                "momentum_continuation": MomentumContinuationStrategy(),
                "baseline_sma": BaselineSMAStrategy(),
            }
            if strategy_name not in strategies:
                send_error(self, 400, "bad_request",
                           f"unknown strategy {strategy_name!r}; "
                           f"choose from {list(strategies)}")
                return
            strat = strategies[strategy_name]

            # Fetch candles
            client = CoinDCXClient(timeout=8, max_retries=1, cache_ttl_seconds=0)
            try:
                candles, prov = client.candles(pair=pair, interval=interval,
                                                   limit=800)
            except CoinDCXAPIError:
                base_price = {
                    "B-BTCINR": 7_000_000, "B-ETHINR": 350_000,
                    "B-SOLINR": 18_000, "B-MATICINR": 90,
                    "B-DOTINR": 700, "B-LTCINR": 8_000,
                    "B-XRPINR": 65, "B-LINKINR": 1_800,
                    "B-AVAXINR": 4_000,
                }.get(pair, 1000)
                synth = SyntheticGenerator(seed=42)
                candles, prov = synth.series(pair=pair, interval=interval,
                                               count=800, start_price=base_price)

            # Settings: defaults (backtest uses its own capital param)
            settings = RiskSettings(initial_inr=starting_capital,
                                       fee_pct=0.1, spread_pct=0.05,
                                       slippage_pct=0.05)
            signal_engine = SignalEngine(settings=settings, strategies=[strat])
            cfg = BacktestConfig(starting_capital_inr=starting_capital)
            bt = Backtester(settings=settings, strategy=strat,
                             signal_engine=signal_engine, config=cfg)

            if walk_forward and len(candles) >= 90:
                mid = candles[len(candles)//2].open_time_ms
                early = candles[len(candles)//4].open_time_ms
                dev_end = _day_iso_from_ms(early)
                val_end = _day_iso_from_ms(mid)
                results = bt.walk_forward(pair=pair, candles=candles,
                                             dev_end_iso=dev_end,
                                             val_end_iso=val_end)
                segs = list(results.keys())
            else:
                results = {"full": bt.run(pair=pair, candles=candles,
                                              segment="full",
                                              provenance=prov)}
                segs = ["full"]

            send_json(self, 200, {
                "pair": pair, "interval": interval,
                "strategy": strategy_name,
                "starting_capital": starting_capital,
                "provenance": {
                    "source": prov.source.value,
                    "endpoint": prov.endpoint,
                    "fetched_at_utc": prov.fetched_at_utc.isoformat(),
                    "label": prov.label(),
                    "note": prov.note,
                },
                "segments": [
                    {"name": seg,
                     "summary": results[seg].to_summary(),
                     "num_trades": results[seg].num_trades,
                     "equity_curve": [
                         {"t_ms": t, "equity": e}
                         for t, e in results[seg].equity_curve
                     ],
                     "trades": [
                         {"entry_idx": t.entry_idx,
                          "exit_idx": t.exit_idx,
                          "entry": t.entry, "exit_price": t.exit_price,
                          "qty": t.qty, "pnl_net": t.pnl_net,
                          "exit_reason": t.exit_reason,
                          "strategy": t.strategy}
                         for t in results[seg].trades
                     ]}
                    for seg in segs
                ],
            })
        except ValueError as e:
            send_error(self, 400, "bad_request", str(e))
        except Exception as e:
            send_error(self, 500, "internal_error", f"{type(e).__name__}: {e}")

    def log_message(self, *args, **kwargs):
        pass

    def do_POST(self):  self._handle("POST")
    def do_OPTIONS(self): self._handle("OPTIONS")
