"""Supabase-backed persistent store for paper-trading.

Mirrors the interface of `coindcx_lab.db.store.Store` so the existing
PaperTradingEngine can use either backend without modification:

    local dev (SQLite):     Store(path=...)
    Vercel (Supabase):      SupabaseStore(config=SupabaseConfig.from_env())

Implementation uses the Supabase REST API (PostgREST) over HTTPS so we
do not add psycopg2 / asyncpg to the serverless bundle. All writes
use the service-role key (server-side only) so RLS is bypassed for
trusted operations; reads may use either the publishable key (browser)
or the service key (server).

Tables (created by supabase/migrations/0001_initial_schema.sql):
    user_settings      — one row per user
    paper_state        — one row per user (account state)
    paper_trades       — full lifecycle of each trade
    paper_signals      — persisted signal snapshots
    market_data_cache  — latest ticker per pair
    job_log            — scheduled job execution log
    equity_snapshots   — equity curve snapshots
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from ..api.env import SupabaseConfig
from .store import Store, StoreError


# ---------------------------------------------------------------------------
# Anonymous user identity
# ---------------------------------------------------------------------------
# For the research/paper-trading MVP we use a single shared anonymous
# user_id. Auth integration (Supabase Auth) can later replace this
# constant with the per-request authenticated user's UUID.
DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000000"


class SupabaseStore(Store):
    """REST-based implementation of the Store protocol against Supabase.

    Constructor: pass a SupabaseConfig (which holds URL + keys). Reads
    the publishable key for safe browser-side calls; uses the service
    key for trusted writes when available.

    All operations are idempotent and bounded by a 6s HTTP timeout to
    stay within Vercel's serverless function execution limits.
    """

    # Match SQLite store defaults for parity.
    HTTP_TIMEOUT = 6.0

    def __init__(self, config: SupabaseConfig,
                 user_id: str = DEFAULT_USER_ID):
        # NOTE: deliberately do NOT call super().__init__(); we don't
        # want to open a SQLite connection. We override every method.
        self.config = config
        self.user_id = user_id
        # No _lock needed — every call is a fresh HTTPS request.

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        # Nothing to close.
        pass

    # ---------------------------------------------------------------- settings
    def save_settings(self, key: str, value: Any) -> None:
        # Persist as a per-user row in user_settings. The "key" param
        # in the Store interface is preserved as the JSON key within the
        # settings blob — we just store the whole RiskSettings object.
        if key != "risk":
            # Only "risk" settings are persisted by the engine today.
            return
        if not self.config.is_server_side():
            raise StoreError("save_settings requires the service-role key "
                             "(server-side only).")
        # Upsert via PostgREST: POST + Prefer: resolution=merge-duplicates
        # with on_conflict query param to scope the conflict detection.
        body = _settings_to_row(value)
        body["user_id"] = self.user_id
        body["updated_at"] = _now_iso()
        url = self.config.rest_url("user_settings")
        r = requests.post(
            url,
            headers={**self.config.headers(use_service=True),
                      "Prefer": "resolution=merge-duplicates, return=representation"},
            json=body,
            params={"on_conflict": "user_id"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "save_settings")

    def load_settings(self, key: str, default: Any = None) -> Any:
        if key != "risk":
            return default
        url = self.config.rest_url("user_settings")
        r = requests.get(
            url,
            headers=self.config.headers(use_service=False),
            params={"user_id": f"eq.{self.user_id}", "limit": "1"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "load_settings")
        rows = r.json()
        if not rows:
            return default
        return _row_to_settings(rows[0])

    # ---------------------------------------------------------------- state
    def save_state(self, blob: dict) -> None:
        if not self.config.is_server_side():
            raise StoreError("save_state requires the service-role key.")
        body = _state_to_row(blob)
        body["user_id"] = self.user_id
        body["updated_at"] = _now_iso()
        url = self.config.rest_url("paper_state")
        r = requests.post(
            url,
            headers={**self.config.headers(use_service=True),
                      "Prefer": "resolution=merge-duplicates, return=representation"},
            json=body,
            params={"on_conflict": "user_id"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "save_state")

    def load_state(self) -> Optional[dict]:
        url = self.config.rest_url("paper_state")
        r = requests.get(
            url,
            headers=self.config.headers(use_service=False),
            params={"user_id": f"eq.{self.user_id}", "limit": "1"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "load_state")
        rows = r.json()
        if not rows:
            return None
        return _row_to_state(rows[0])

    # ---------------------------------------------------------------- trades
    def find_trade_by_request_key(self, request_key: str) -> Optional[dict]:
        """Find an existing trade by its idempotency request_key.

        The request_key is stored inside the signal_json JSONB field.
        PostgREST supports JSONB path operators for filtering:
        signal_json->>request_key=eq.<value>

        Returns the trade dict if found, None otherwise.
        """
        if not request_key:
            return None
        url = self.config.rest_url("paper_trades")
        # Use PostgREST's JSONB path operator to filter on the request_key
        # inside signal_json. The ->> operator extracts as text.
        r = requests.get(
            url,
            headers=self.config.headers(use_service=True),
            params={
                "user_id": f"eq.{self.user_id}",
                "signal_json->>request_key": f"eq.{request_key}",
                "limit": "1",
            },
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "find_trade_by_request_key")
        rows = r.json()
        if rows and isinstance(rows, list) and len(rows) > 0:
            return _row_to_trade(rows[0])
        return None

    def insert_trade(self, trade: dict) -> int:
        if not self.config.is_server_side():
            raise StoreError("insert_trade requires the service-role key.")
        body = _trade_to_row(trade, self.user_id)
        url = self.config.rest_url("paper_trades")
        r = requests.post(
            url,
            headers={**self.config.headers(use_service=True),
                      "Prefer": "return=representation"},
            json=body,
            params={"select": "id"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "insert_trade")
        # PostgREST may return an empty body even with return=representation
        # (depends on RLS and column visibility). Handle gracefully — the
        # INSERT succeeded if _check passed (HTTP 201), we just may not
        # have the generated ID.
        try:
            rows = r.json()
            if rows and isinstance(rows, list) and rows[0].get("id"):
                return int(rows[0]["id"])
        except (ValueError, json.JSONDecodeError, IndexError, TypeError):
            pass
        return 0

    def update_trade_exit(self, trade_id: int, *,
                          exit_time_ms: int, exit_price: float,
                          pnl_net: float, exit_reason: str,
                          fee_inr: float, slippage_inr: float,
                          status: str) -> None:
        if not self.config.is_server_side():
            raise StoreError("update_trade_exit requires the service-role key.")
        url = self.config.rest_url("paper_trades")
        body = {
            "exit_time_ms": exit_time_ms,
            "exit_price": exit_price,
            "pnl_net": pnl_net,
            "exit_reason": exit_reason,
            "fee_inr": fee_inr,
            "slippage_inr": slippage_inr,
            "status": status,
        }
        r = requests.patch(
            url,
            headers=self.config.headers(use_service=True),
            json=body,
            params={"id": f"eq.{trade_id}"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "update_trade_exit")

    def update_trade_signal_json(self, trade_id: int, signal_json: str) -> None:
        """Update the signal_json field of an existing trade (for
        idempotency key embedding)."""
        if not self.config.is_server_side():
            raise StoreError("update_trade_signal_json requires the service-role key.")
        url = self.config.rest_url("paper_trades")
        r = requests.patch(
            url,
            headers=self.config.headers(use_service=True),
            json={"signal_json": signal_json},
            params={"id": f"eq.{trade_id}"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "update_trade_signal_json")

    def list_trades(self, *, status: Optional[str] = None,
                    limit: int = 500) -> List[dict]:
        params = {
            "user_id": f"eq.{self.user_id}",
            "order": "entry_time_ms.desc",
            "limit": str(limit),
        }
        if status is not None:
            params["status"] = f"eq.{status}"
        url = self.config.rest_url("paper_trades")
        r = requests.get(
            url,
            headers=self.config.headers(use_service=False),
            params=params,
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "list_trades")
        return [_row_to_trade(r) for r in r.json()]

    def get_open_trades(self) -> List[dict]:
        return self.list_trades(status="OPEN", limit=100)

    def count_trades_today(self, day_iso: str) -> int:
        from datetime import datetime as _dt
        try:
            dt = _dt.fromisoformat(day_iso).replace(tzinfo=timezone.utc)
        except ValueError:
            return 0
        start_ms = int(dt.timestamp() * 1000)
        end_ms = start_ms + 86_400_000
        url = self.config.rest_url("paper_trades")
        # PostgREST range filter uses comma-separated ops on one key.
        params = {
            "user_id": f"eq.{self.user_id}",
            "entry_time_ms": f"gte.{start_ms},lt.{end_ms}",
            "select": "id",
        }
        r = requests.get(
            url,
            headers=self.config.headers(use_service=False),
            params=params,
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "count_trades_today")
        # Prefer the "Content-Range" header for a count when possible.
        count_header = r.headers.get("Content-Range", "")
        if count_header.startswith("*/"):
            try:
                return int(count_header[2:])
            except ValueError:
                pass
        return len(r.json())

    # ----------------------------------------------------- equity snapshots
    def append_equity_snapshot(self, *, time_ms: int, equity: float,
                               cash: float, open_positions_value: float,
                               peak_equity: float,
                               note: str = "") -> None:
        if not self.config.is_server_side():
            raise StoreError("append_equity_snapshot requires the service-role key.")
        url = self.config.rest_url("equity_snapshots")
        body = {
            "user_id": self.user_id,
            "time_ms": time_ms,
            "equity": equity, "cash": cash,
            "open_positions_value": open_positions_value,
            "peak_equity": peak_equity,
            "note": note,
        }
        r = requests.post(
            url,
            headers=self.config.headers(use_service=True),
            json=body,
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "append_equity_snapshot")

    def list_equity_snapshots(self, limit: int = 1000) -> List[dict]:
        url = self.config.rest_url("equity_snapshots")
        params = {
            "user_id": f"eq.{self.user_id}",
            "order": "time_ms.asc",
            "limit": str(limit),
        }
        r = requests.get(
            url,
            headers=self.config.headers(use_service=False),
            params=params,
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "list_equity_snapshots")
        return [_row_to_equity(r) for r in r.json()]

    # ----------------------------------------------------------- destructive
    def reset_all_trades(self) -> None:
        """DANGEROUS: wipe this user's trades, equity_snapshots, state.
        Only callable from PaperTradingEngine.reset() and only after
        explicit user confirmation."""
        if not self.config.is_server_side():
            raise StoreError("reset_all_trades requires the service-role key.")
        # IMPORTANT: filter by user_id so we never touch other users' data.
        for table in ("paper_trades", "equity_snapshots"):
            url = self.config.rest_url(table)
            r = requests.delete(
                url,
                headers=self.config.headers(use_service=True),
                params={"user_id": f"eq.{self.user_id}"},
                timeout=self.HTTP_TIMEOUT,
            )
            self._check(r, f"reset_all_trades/{table}")
        # Reset state row to defaults
        url = self.config.rest_url("paper_state")
        r = requests.delete(
            url,
            headers=self.config.headers(use_service=True),
            params={"user_id": f"eq.{self.user_id}"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "reset_all_trades/state")

    # ------------------------------------------------------------------ market_data_cache
    def upsert_market_data(self, pair: str, ticker: dict,
                           provenance_note: str) -> None:
        """Persist the latest ticker for a pair (used by the cron job)."""
        if not self.config.is_server_side():
            raise StoreError("upsert_market_data requires the service-role key.")
        url = self.config.rest_url("market_data_cache")
        body = {
            "pair": pair,
            "last_price": ticker.get("last_price"),
            "high_24h": ticker.get("high_24h"),
            "low_24h": ticker.get("low_24h"),
            "volume_24h": ticker.get("volume_24h"),
            "change_pct_24h": ticker.get("change_pct_24h"),
            "source": provenance_note,
            "endpoint": "coindcx_public",
            "fetched_at": _now_iso(),
            "note": provenance_note,
        }
        r = requests.post(
            url,
            headers={**self.config.headers(use_service=True),
                      "Prefer": "resolution=merge-duplicates, return=representation"},
            json=body,
            params={"on_conflict": "pair"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "upsert_market_data")

    def upsert_market_data_batch(self, items: dict, provenance_note: str,
                                  endpoint: str = "wazirx_public") -> int:
        """Batch upsert many tickers in a single Supabase POST.

        `items` is a dict of {pair_code: ticker_dict}. Returns the number
        of rows actually upserted.

        PostgREST supports bulk insert by POSTing an array of objects
        with `Prefer: resolution=merge-duplicates` — much faster than
        216 individual POSTs (which was taking 110s; this brings it
        down to ~1-2s).
        """
        if not self.config.is_server_side():
            raise StoreError("upsert_market_data_batch requires the service-role key.")
        if not items:
            return 0
        now_iso = _now_iso()
        body = [
            {
                "pair": pair,
                "last_price": t.get("last_price"),
                "high_24h": t.get("high_24h"),
                "low_24h": t.get("low_24h"),
                "volume_24h": t.get("volume_24h"),
                "change_pct_24h": t.get("change_pct_24h"),
                "source": provenance_note,
                "endpoint": endpoint,
                "fetched_at": now_iso,
                "note": provenance_note,
            }
            for pair, t in items.items()
        ]
        url = self.config.rest_url("market_data_cache")
        r = requests.post(
            url,
            headers={**self.config.headers(use_service=True),
                      "Prefer": "resolution=merge-duplicates"},
            json=body,
            params={"on_conflict": "pair"},
            timeout=max(self.HTTP_TIMEOUT, 15.0),  # longer timeout for batch
        )
        self._check(r, "upsert_market_data_batch")
        # PostgREST returns the inserted rows (or empty if return=representation
        # was not requested). Count rows from response if available.
        try:
            return len(r.json()) if r.json() else len(body)
        except (ValueError, json.JSONDecodeError):
            return len(body)

    def list_market_data(self) -> List[dict]:
        url = self.config.rest_url("market_data_cache")
        r = requests.get(
            url,
            headers=self.config.headers(use_service=False),
            params={"order": "pair.asc"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "list_market_data")
        return r.json()

    # ------------------------------------------------------------------ job_log
    def log_job(self, *, job_name: str, status: str,
                started_at: str, ended_at: Optional[str],
                duration_ms: Optional[int],
                error: Optional[str] = None,
                detail: Optional[dict] = None) -> None:
        if not self.config.is_server_side():
            raise StoreError("log_job requires the service-role key.")
        url = self.config.rest_url("job_log")
        body = {
            "job_name": job_name,
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "error": error,
            "detail": json.dumps(detail) if detail else None,
        }
        r = requests.post(
            url,
            headers=self.config.headers(use_service=True),
            json=body,
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "log_job")

    def latest_job(self, job_name: str) -> Optional[dict]:
        url = self.config.rest_url("job_log")
        r = requests.get(
            url,
            headers=self.config.headers(use_service=False),
            params={"job_name": f"eq.{job_name}",
                    "order": "started_at.desc",
                    "limit": "1"},
            timeout=self.HTTP_TIMEOUT,
        )
        self._check(r, "latest_job")
        rows = r.json()
        return rows[0] if rows else None

    # ------------------------------------------------------------------ helpers
    def _check(self, r: requests.Response, op: str) -> None:
        if r.status_code >= 400:
            raise StoreError(
                f"{op} failed: HTTP {r.status_code} {r.reason} "
                f"body[:200]={r.text[:200]}"
            )


# ---------------------------------------------------------------------------
# Row <-> dict mappers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _settings_to_row(value: dict) -> dict:
    return {
        "initial_inr": value.get("initial_inr", 2000.0),
        "risk_per_trade_pct": value.get("risk_per_trade_pct", 1.0),
        "max_position_pct": value.get("max_position_pct", 25.0),
        "max_portfolio_exposure_pct": value.get("max_portfolio_exposure_pct", 80.0),
        "max_daily_loss_pct": value.get("max_daily_loss_pct", 5.0),
        "max_daily_loss_abs_inr": value.get("max_daily_loss_abs_inr", 0.0),
        "max_drawdown_pct": value.get("max_drawdown_pct", 15.0),
        "max_open_positions": value.get("max_open_positions", 5),
        "max_daily_trades": value.get("max_daily_trades", 10),
        "reward_to_risk_target": value.get("reward_to_risk_target", 2.0),
        "cooldown_after_consecutive_losses": value.get("cooldown_after_consecutive_losses", 3),
        "allow_overnight": value.get("allow_overnight", True),
        "fee_pct": value.get("fee_pct", 0.1),
        "spread_pct": value.get("spread_pct", 0.05),
        "slippage_pct": value.get("slippage_pct", 0.05),
        "min_24h_volume_inr": value.get("min_24h_volume_inr", 5_000_000.0),
        "min_24h_trades": value.get("min_24h_trades", 1000),
        "trading_allocation_pct": value.get("trading_allocation_pct", 100.0),
    }


def _row_to_settings(row: dict) -> dict:
    return {
        "initial_inr": float(row.get("initial_inr", 2000.0)),
        "risk_per_trade_pct": float(row.get("risk_per_trade_pct", 1.0)),
        "max_position_pct": float(row.get("max_position_pct", 25.0)),
        "max_portfolio_exposure_pct": float(row.get("max_portfolio_exposure_pct", 80.0)),
        "max_daily_loss_pct": float(row.get("max_daily_loss_pct", 5.0)),
        "max_daily_loss_abs_inr": float(row.get("max_daily_loss_abs_inr", 0.0)),
        "max_drawdown_pct": float(row.get("max_drawdown_pct", 15.0)),
        "max_open_positions": int(row.get("max_open_positions", 5)),
        "max_daily_trades": int(row.get("max_daily_trades", 10)),
        "reward_to_risk_target": float(row.get("reward_to_risk_target", 2.0)),
        "cooldown_after_consecutive_losses": int(row.get("cooldown_after_consecutive_losses", 3)),
        "allow_overnight": bool(row.get("allow_overnight", True)),
        "fee_pct": float(row.get("fee_pct", 0.1)),
        "spread_pct": float(row.get("spread_pct", 0.05)),
        "slippage_pct": float(row.get("slippage_pct", 0.05)),
        "min_24h_volume_inr": float(row.get("min_24h_volume_inr", 5_000_000.0)),
        "min_24h_trades": int(row.get("min_24h_trades", 1000)),
        "trading_allocation_pct": float(row.get("trading_allocation_pct", 100.0)),
    }


def _state_to_row(blob: dict) -> dict:
    return {
        "initial_inr": blob.get("initial_inr", 2000.0),
        "realized_pnl": blob.get("realized_pnl", 0.0),
        "open_positions_value": blob.get("open_positions_value", 0.0),
        "open_positions_risk": blob.get("open_positions_risk", 0.0),
        "peak_equity": blob.get("peak_equity", 2000.0),
        "consecutive_losses": blob.get("consecutive_losses", 0),
        "cooldown_until_iso": blob.get("cooldown_until_iso"),
        "paused": blob.get("paused", False),
        "daily_day_iso": blob.get("daily_day_iso"),
        "daily_realized_pnl_day": blob.get("daily_realized_pnl_day", 0.0),
        "daily_trades_today": blob.get("daily_trades_today", 0),
    }


def _row_to_state(row: dict) -> dict:
    return {
        "initial_inr": float(row.get("initial_inr", 2000.0)),
        "realized_pnl": float(row.get("realized_pnl", 0.0)),
        "open_positions_value": float(row.get("open_positions_value", 0.0)),
        "open_positions_risk": float(row.get("open_positions_risk", 0.0)),
        "peak_equity": float(row.get("peak_equity", 2000.0)),
        "consecutive_losses": int(row.get("consecutive_losses", 0)),
        "cooldown_until_iso": row.get("cooldown_until_iso"),
        "paused": bool(row.get("paused", False)),
        "daily_day_iso": row.get("daily_day_iso"),
        "daily_realized_pnl_day": float(row.get("daily_realized_pnl_day", 0.0)),
        "daily_trades_today": int(row.get("daily_trades_today", 0)),
    }


def _trade_to_row(trade: dict, user_id: str) -> dict:
    # NOTE: the Supabase paper_trades table has `created_at TIMESTAMPTZ
    # DEFAULT now()` — it does NOT have a `created_at_ms` column (unlike
    # the SQLite schema). We omit `created_at_ms` here and let Postgres
    # auto-populate `created_at` with the current timestamp on INSERT.
    return {
        "user_id": user_id,
        "pair": trade.get("pair"),
        "strategy": trade.get("strategy"),
        "side": trade.get("side"),
        "status": trade.get("status"),
        "qty": trade.get("qty"),
        "entry": trade.get("entry"),
        "stop": trade.get("stop"),
        "target": trade.get("target"),
        "entry_time_ms": trade.get("entry_time_ms"),
        "exit_time_ms": trade.get("exit_time_ms"),
        "exit_price": trade.get("exit_price"),
        "pnl_net": trade.get("pnl_net"),
        "exit_reason": trade.get("exit_reason"),
        "fee_inr": trade.get("fee_inr"),
        "slippage_inr": trade.get("slippage_inr"),
        "signal_json": trade.get("signal_json"),
        # created_at_ms NOT included — Supabase auto-populates created_at
    }


def _row_to_trade(row: dict) -> dict:
    # Convert Supabase `created_at` (ISO timestamp) to `created_at_ms`
    # (epoch milliseconds) for compatibility with the Store interface.
    created_at = row.get("created_at")
    created_at_ms = 0
    if created_at:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            created_at_ms = int(dt.timestamp() * 1000)
        except Exception:
            created_at_ms = 0
    return {
        "id": row.get("id"),
        "pair": row.get("pair"),
        "strategy": row.get("strategy"),
        "side": row.get("side"),
        "status": row.get("status"),
        "qty": float(row.get("qty", 0)) if row.get("qty") is not None else None,
        "entry": float(row.get("entry", 0)) if row.get("entry") is not None else None,
        "stop": float(row.get("stop", 0)) if row.get("stop") is not None else None,
        "target": float(row.get("target", 0)) if row.get("target") is not None else None,
        "entry_time_ms": int(row.get("entry_time_ms", 0)),
        "exit_time_ms": int(row.get("exit_time_ms")) if row.get("exit_time_ms") is not None else None,
        "exit_price": float(row.get("exit_price")) if row.get("exit_price") is not None else None,
        "pnl_net": float(row.get("pnl_net")) if row.get("pnl_net") is not None else None,
        "exit_reason": row.get("exit_reason"),
        "fee_inr": float(row.get("fee_inr", 0)) if row.get("fee_inr") is not None else 0,
        "slippage_inr": float(row.get("slippage_inr", 0)) if row.get("slippage_inr") is not None else 0,
        "signal_json": row.get("signal_json") or "",
        "created_at_ms": created_at_ms,
    }


def _row_to_equity(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "time_ms": int(row.get("time_ms", 0)),
        "equity": float(row.get("equity", 0)),
        "cash": float(row.get("cash", 0)),
        "open_positions_value": float(row.get("open_positions_value", 0)),
        "peak_equity": float(row.get("peak_equity", 0)),
        "note": row.get("note", ""),
    }
