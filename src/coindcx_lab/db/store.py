"""SQLite-backed persistent store for paper trades, settings, and portfolio
state.

Schema:
    settings   (id, key, value_json)        -- key/value blob for risk settings
    state      (id, blob_json)               -- single-row portfolio state
    trades     (id, pair, strategy, side, status, qty, entry, stop, target,
                entry_time_ms, exit_time_ms, exit_price, pnl_net, exit_reason,
                fee_inr, slippage_inr, signal_json, created_at_ms)
    equity_snapshots (id, time_ms, equity, cash, open_positions_value, peak_equity)

All writes are explicit; the dashboard never silently overwrites history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class StoreError(RuntimeError):
    """Raised on store-level invariant violations."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    value_json TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    blob_json TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    qty REAL NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    entry_time_ms INTEGER NOT NULL,
    exit_time_ms INTEGER,
    exit_price REAL,
    pnl_net REAL,
    exit_reason TEXT,
    fee_inr REAL,
    slippage_inr REAL,
    signal_json TEXT,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_pair ON trades(pair);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time_ms);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time_ms INTEGER NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    open_positions_value REAL NOT NULL,
    peak_equity REAL NOT NULL,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_equity_time ON equity_snapshots(time_ms);
"""


@dataclass
class Store:
    """Thread-safe wrapper around a single SQLite file."""

    path: str
    _lock: threading.Lock = threading.Lock()
    _conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------ init
    def __post_init__(self) -> None:
        # Connect with check_same_thread=False and serialise writes via
        # the lock. SQLite handles its own file locking.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.commit()
                self._conn.close()
                self._conn = None

    # --------------------------------------------------------------- settings
    def save_settings(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value_json, updated_at_ms) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value_json=excluded.value_json, "
                "updated_at_ms=excluded.updated_at_ms",
                (key, json.dumps(value, default=str),
                 int(datetime.now(timezone.utc).timestamp() * 1000)))
            self._conn.commit()

    def load_settings(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (json.JSONDecodeError, TypeError):
            return default

    # ------------------------------------------------------------------ state
    def save_state(self, blob: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO state(id, blob_json, updated_at_ms) "
                "VALUES(1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "blob_json=excluded.blob_json, "
                "updated_at_ms=excluded.updated_at_ms",
                (json.dumps(blob, default=str),
                 int(datetime.now(timezone.utc).timestamp() * 1000)))
            self._conn.commit()

    def load_state(self) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT blob_json FROM state WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["blob_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    # ------------------------------------------------------------------ trades
    def insert_trade(self, trade: dict) -> int:
        cols = ("pair", "strategy", "side", "status", "qty", "entry", "stop",
                "target", "entry_time_ms", "exit_time_ms", "exit_price",
                "pnl_net", "exit_reason", "fee_inr", "slippage_inr",
                "signal_json", "created_at_ms")
        values = [trade.get(c) for c in cols]
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO trades ({','.join(cols)}) "
                f"VALUES ({','.join(['?']*len(cols))})",
                values,
            )
            self._conn.commit()
            return cur.lastrowid

    def update_trade_exit(self, trade_id: int, *,
                          exit_time_ms: int, exit_price: float,
                          pnl_net: float, exit_reason: str,
                          fee_inr: float, slippage_inr: float,
                          status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET "
                " exit_time_ms = ?, exit_price = ?, pnl_net = ?, "
                " exit_reason = ?, fee_inr = ?, slippage_inr = ?, status = ? "
                "WHERE id = ?",
                (exit_time_ms, exit_price, pnl_net, exit_reason,
                 fee_inr, slippage_inr, status, trade_id)
            )
            self._conn.commit()

    def list_trades(self, *, status: Optional[str] = None,
                    limit: int = 500) -> List[dict]:
        sql = "SELECT * FROM trades"
        args: List[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY entry_time_ms DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def get_open_trades(self) -> List[dict]:
        return self.list_trades(status="OPEN")

    def count_trades_today(self, day_iso: str) -> int:
        # Convert ISO date to ms range
        import time, calendar
        from datetime import datetime as _dt
        dt = _dt.fromisoformat(day_iso).replace(tzinfo=timezone.utc)
        start_ms = int(dt.timestamp() * 1000)
        end_ms = start_ms + 86_400_000
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM trades "
                "WHERE entry_time_ms >= ? AND entry_time_ms < ?",
                (start_ms, end_ms),
            ).fetchone()
        return int(row["c"] if row else 0)

    # ------------------------------------------------------- equity snapshots
    def append_equity_snapshot(self, *, time_ms: int, equity: float,
                               cash: float, open_positions_value: float,
                               peak_equity: float,
                               note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO equity_snapshots"
                "(time_ms, equity, cash, open_positions_value, peak_equity, note) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (time_ms, equity, cash, open_positions_value, peak_equity, note)
            )
            self._conn.commit()

    def list_equity_snapshots(self, limit: int = 1000) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM equity_snapshots ORDER BY time_ms ASC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- destructive
    def reset_all_trades(self) -> None:
        """DANGEROUS: truncate trades, equity_snapshots, state.

        Only callable from PaperTradingEngine.reset() and only after
        explicit user confirmation. Never invoked automatically.
        """
        with self._lock:
            self._conn.execute("DELETE FROM trades")
            self._conn.execute("DELETE FROM equity_snapshots")
            self._conn.execute("DELETE FROM state")
            self._conn.commit()
