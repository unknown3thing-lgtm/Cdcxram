"""Phase 6 regression tests: idempotency for POST /api/trades.

Covers:
  1. Same request_key → returns original trade (not a duplicate)
  2. Different request_key → creates a new trade
  3. No request_key → backward compatible (creates trade, no dedup)
  4. SQLite Store find_trade_by_request_key works correctly
  5. SQLite Store update_trade_signal_json works correctly

Uses temporary SQLite (not Supabase) so tests run without network.
"""

from __future__ import annotations

import os
import tempfile
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Store-level idempotency (SQLite)
# ---------------------------------------------------------------------------
class TestStoreIdempotency:
    """Test the Store's find_trade_by_request_key and
    update_trade_signal_json methods (SQLite backend)."""

    def test_find_trade_by_request_key_finds_existing(self):
        from coindcx_lab.db.store import Store
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            store = Store(path=db_path)
            # Insert a trade with a request_key in signal_json
            rk = str(uuid.uuid4())
            store.insert_trade({
                "pair": "B-BTCINR", "strategy": "test", "side": "long",
                "status": "OPEN", "qty": 1.0, "entry": 100, "stop": 98,
                "target": 110, "entry_time_ms": 1700000000000,
                "exit_time_ms": None, "exit_price": None,
                "pnl_net": None, "exit_reason": None,
                "fee_inr": 0.1, "slippage_inr": 0.05,
                "signal_json": json.dumps({"request_key": rk}),
                "created_at_ms": 1700000000000,
            })
            found = store.find_trade_by_request_key(rk)
            assert found is not None
            assert found["pair"] == "B-BTCINR"
            assert json.loads(found["signal_json"])["request_key"] == rk
        finally:
            store.close()
            os.unlink(db_path)

    def test_find_trade_by_request_key_returns_none_for_missing(self):
        from coindcx_lab.db.store import Store
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            store = Store(path=db_path)
            found = store.find_trade_by_request_key("nonexistent-key")
            assert found is None
        finally:
            store.close()
            os.unlink(db_path)

    def test_find_trade_by_request_key_empty_key_returns_none(self):
        from coindcx_lab.db.store import Store
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            store = Store(path=db_path)
            found = store.find_trade_by_request_key("")
            assert found is None
            found = store.find_trade_by_request_key(None)
            assert found is None
        finally:
            store.close()
            os.unlink(db_path)

    def test_update_trade_signal_json(self):
        from coindcx_lab.db.store import Store
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            store = Store(path=db_path)
            # Insert a trade
            trade_id = store.insert_trade({
                "pair": "B-BTCINR", "strategy": "test", "side": "long",
                "status": "OPEN", "qty": 1.0, "entry": 100, "stop": 98,
                "target": 110, "entry_time_ms": 1700000000000,
                "exit_time_ms": None, "exit_price": None,
                "pnl_net": None, "exit_reason": None,
                "fee_inr": 0.1, "slippage_inr": 0.05,
                "signal_json": "{}",
                "created_at_ms": 1700000000000,
            })
            # Update signal_json with a request_key
            rk = str(uuid.uuid4())
            new_json = json.dumps({"request_key": rk, "strategy": "baseline"})
            store.update_trade_signal_json(trade_id, new_json)
            # Verify it was updated
            trades = store.list_trades(limit=1)
            assert trades[0]["signal_json"] == new_json
            # And find_trade_by_request_key can find it
            found = store.find_trade_by_request_key(rk)
            assert found is not None
            assert found["id"] == trade_id
        finally:
            store.close()
            os.unlink(db_path)

    def test_different_request_keys_create_different_trades(self):
        from coindcx_lab.db.store import Store
        fd, db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        try:
            store = Store(path=db_path)
            rk1 = str(uuid.uuid4())
            rk2 = str(uuid.uuid4())
            # Insert two trades with different request_keys
            for rk in (rk1, rk2):
                store.insert_trade({
                    "pair": "B-BTCINR", "strategy": "test", "side": "long",
                    "status": "OPEN", "qty": 1.0, "entry": 100, "stop": 98,
                    "target": 110, "entry_time_ms": 1700000000000,
                    "exit_time_ms": None, "exit_price": None,
                    "pnl_net": None, "exit_reason": None,
                    "fee_inr": 0.1, "slippage_inr": 0.05,
                    "signal_json": json.dumps({"request_key": rk}),
                    "created_at_ms": 1700000000000,
                })
            # find by rk1 → should find the first trade
            found1 = store.find_trade_by_request_key(rk1)
            assert found1 is not None
            # find by rk2 → should find the second trade
            found2 = store.find_trade_by_request_key(rk2)
            assert found2 is not None
            # They should be different trades
            assert found1["id"] != found2["id"]
        finally:
            store.close()
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# API-level idempotency (via local HTTP server)
# ---------------------------------------------------------------------------
class TestApiIdempotency:
    """Test that POST /api/trades with the same request_key returns the
    original trade instead of creating a duplicate."""

    def test_same_request_key_returns_original_trade(self):
        """Send POST /api/trades twice with the same request_key.
        The second request should return the original trade, not create
        a new one."""
        from api.trades import handler
        import socketserver, threading, time
        import requests as _rq

        rk = str(uuid.uuid4())
        body = {
            "pair": "B-BTCINR", "entry": 100, "stop": 95, "target": 120,
            "strategy": "idempotency_test", "state": "BUY_CANDIDATE",
            "rr_after_cost": 5.0, "current_price": 100,
            "request_key": rk,
        }

        # Mock the paper engine to avoid needing real Supabase
        mock_pe = MagicMock()
        mock_trade = MagicMock()
        mock_trade.id = 42
        mock_trade.to_dict.return_value = {
            "id": 42, "pair": "B-BTCINR", "status": "OPEN",
            "entry": 100.05, "stop": 95, "target": 120, "qty": 3.96,
        }
        mock_trade.signal_json = "{}"
        mock_pe.open_position.return_value = (mock_trade, "ok")
        mock_pe.store.find_trade_by_request_key.return_value = None  # first call: not found
        # After first INSERT, update the mock to return the trade on second call
        mock_pe.store.update_trade_signal_json = MagicMock()

        with patch("coindcx_lab.server.paper_common.build_paper_engine",
                    return_value=mock_pe):
            class _S(socketserver.TCPServer):
                allow_reuse_address = True
            srv = _S(("127.0.0.1", 0), handler)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            time.sleep(0.05)
            try:
                # First request — should create the trade
                r1 = _rq.post(f"http://127.0.0.1:{port}/api/trades",
                              json=body, timeout=10)
                assert r1.status_code == 200
                d1 = r1.json()
                assert d1["accepted"] is True
                assert d1["trade"]["id"] == 42
                assert not d1.get("idempotent")

                # Second request with SAME request_key — should return original
                # Update mock: now find_trade_by_request_key returns the existing trade
                mock_pe.store.find_trade_by_request_key.return_value = {
                    "id": 42, "pair": "B-BTCINR", "status": "OPEN",
                    "entry": 100.05, "stop": 95, "target": 120,
                    "qty": 3.96, "strategy": "idempotency_test",
                    "side": "long", "entry_time_ms": 1700000000000,
                    "exit_time_ms": None, "exit_price": None,
                    "pnl_net": None, "exit_reason": None,
                    "fee_inr": 0.1, "slippage_inr": 0.05,
                    "signal_json": json.dumps({"request_key": rk}),
                    "created_at_ms": 1700000000000,
                }
                r2 = _rq.post(f"http://127.0.0.1:{port}/api/trades",
                              json=body, timeout=10)
                assert r2.status_code == 200
                d2 = r2.json()
                assert d2["accepted"] is True
                assert d2["trade"]["id"] == 42  # same trade, not a new one
                assert d2.get("idempotent") is True
                assert "idempotent" in d2["reason"]
                # open_position should NOT have been called for the second request
                mock_pe.open_position.assert_called_once()
            finally:
                srv.shutdown()

    def test_different_request_key_creates_new_trade(self):
        """Two requests with DIFFERENT request_keys should create two
        separate trades."""
        from api.trades import handler
        import socketserver, threading, time
        import requests as _rq

        mock_pe = MagicMock()
        mock_trade1 = MagicMock()
        mock_trade1.id = 1
        mock_trade1.to_dict.return_value = {"id": 1, "pair": "B-BTCINR"}
        mock_trade1.signal_json = "{}"
        mock_trade2 = MagicMock()
        mock_trade2.id = 2
        mock_trade2.to_dict.return_value = {"id": 2, "pair": "B-BTCINR"}
        mock_trade2.signal_json = "{}"
        mock_pe.open_position.side_effect = [(mock_trade1, "ok"), (mock_trade2, "ok")]
        mock_pe.store.find_trade_by_request_key.return_value = None
        mock_pe.store.update_trade_signal_json = MagicMock()

        with patch("coindcx_lab.server.paper_common.build_paper_engine",
                    return_value=mock_pe):
            class _S(socketserver.TCPServer):
                allow_reuse_address = True
            srv = _S(("127.0.0.1", 0), handler)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            time.sleep(0.05)
            try:
                body = {
                    "pair": "B-BTCINR", "entry": 100, "stop": 95,
                    "target": 120, "strategy": "test", "state": "BUY_CANDIDATE",
                    "rr_after_cost": 5.0, "current_price": 100,
                }
                # First request with request_key A
                body["request_key"] = str(uuid.uuid4())
                r1 = _rq.post(f"http://127.0.0.1:{port}/api/trades",
                              json=body, timeout=10)
                d1 = r1.json()
                assert d1["trade"]["id"] == 1

                # Second request with DIFFERENT request_key B
                body["request_key"] = str(uuid.uuid4())
                r2 = _rq.post(f"http://127.0.0.1:{port}/api/trades",
                              json=body, timeout=10)
                d2 = r2.json()
                assert d2["trade"]["id"] == 2  # different trade

                # open_position should have been called twice
                assert mock_pe.open_position.call_count == 2
            finally:
                srv.shutdown()

    def test_no_request_key_backward_compatible(self):
        """POST without request_key should still work (backward compatible).
        No idempotency check is performed."""
        from api.trades import handler
        import socketserver, threading, time
        import requests as _rq

        mock_pe = MagicMock()
        mock_trade = MagicMock()
        mock_trade.id = 1
        mock_trade.to_dict.return_value = {"id": 1, "pair": "B-BTCINR"}
        mock_trade.signal_json = "{}"
        mock_pe.open_position.return_value = (mock_trade, "ok")

        with patch("coindcx_lab.server.paper_common.build_paper_engine",
                    return_value=mock_pe):
            class _S(socketserver.TCPServer):
                allow_reuse_address = True
            srv = _S(("127.0.0.1", 0), handler)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            time.sleep(0.05)
            try:
                body = {
                    "pair": "B-BTCINR", "entry": 100, "stop": 95,
                    "target": 120, "strategy": "test", "state": "BUY_CANDIDATE",
                    "rr_after_cost": 5.0, "current_price": 100,
                    # NO request_key
                }
                r = _rq.post(f"http://127.0.0.1:{port}/api/trades",
                             json=body, timeout=10)
                assert r.status_code == 200
                d = r.json()
                assert d["accepted"] is True
                # find_trade_by_request_key should NOT have been called
                # (no request_key to check)
                mock_pe.store.find_trade_by_request_key.assert_not_called()
            finally:
                srv.shutdown()
