"""Phase 5 regression test: SupabaseStore must NOT send `created_at_ms`
to the paper_trades table (the Supabase schema has `created_at
TIMESTAMPTZ DEFAULT now()`, not `created_at_ms BIGINT`).

Defect: POST /api/trades returned HTTP 500 with:
  PGRST204: Could not find the 'created_at_ms' column of 'paper_trades'

Fix: _trade_to_row no longer includes created_at_ms; _row_to_trade
converts the Supabase `created_at` (ISO timestamp) back to ms.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


class TestSupabaseStoreCreated_at_ms:
    """Regression: paper trade INSERT must not include created_at_ms."""

    def test_trade_to_row_omits_created_at_ms(self):
        """_trade_to_row must NOT include 'created_at_ms' key — the
        Supabase paper_trades table has 'created_at TIMESTAMPTZ DEFAULT
        now()', not 'created_at_ms BIGINT'."""
        from coindcx_lab.db.supabase_store import _trade_to_row
        trade = {
            "pair": "B-BTCINR", "strategy": "test", "side": "long",
            "status": "OPEN", "qty": 1.0, "entry": 100, "stop": 98,
            "target": 110, "entry_time_ms": 1700000000000,
            "exit_time_ms": None, "exit_price": None,
            "pnl_net": None, "exit_reason": None,
            "fee_inr": 0.1, "slippage_inr": 0.05,
            "signal_json": "{}", "created_at_ms": 1700000000000,
        }
        row = _trade_to_row(trade, "00000000-0000-0000-0000-000000000000")
        assert "created_at_ms" not in row, \
            "created_at_ms must NOT be in the Supabase row (causes PGRST204)"
        # All other fields should be present
        for k in ("pair", "strategy", "side", "status", "qty", "entry",
                  "stop", "target", "entry_time_ms", "fee_inr",
                  "slippage_inr", "signal_json"):
            assert k in row, f"missing field {k}"

    def test_row_to_trade_converts_created_at_to_ms(self):
        """_row_to_trade should convert the Supabase `created_at` ISO
        timestamp to `created_at_ms` (epoch milliseconds) for
        compatibility with the Store interface."""
        from coindcx_lab.db.supabase_store import _row_to_trade
        row = {
            "id": 1, "pair": "B-BTCINR", "strategy": "test",
            "side": "long", "status": "OPEN", "qty": "1.0",
            "entry": "100.0", "stop": "98.0", "target": "110.0",
            "entry_time_ms": 1700000000000, "exit_time_ms": None,
            "exit_price": None, "pnl_net": None, "exit_reason": None,
            "fee_inr": "0.1", "slippage_inr": "0.05",
            "signal_json": "{}",
            # Supabase returns created_at as ISO timestamp string
            "created_at": "2026-10-11T08:00:00+00:00",
        }
        trade = _row_to_trade(row)
        # created_at_ms should be the epoch milliseconds of the ISO timestamp
        expected_ms = int(datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)
                          .timestamp() * 1000)
        assert trade["created_at_ms"] == expected_ms, \
            f"expected {expected_ms}, got {trade['created_at_ms']}"

    def test_row_to_trade_handles_missing_created_at(self):
        """If created_at is missing (e.g., old rows), created_at_ms=0."""
        from coindcx_lab.db.supabase_store import _row_to_trade
        row = {
            "id": 1, "pair": "B-BTCINR", "strategy": "test",
            "side": "long", "status": "OPEN", "qty": "1.0",
            "entry": "100.0", "stop": "98.0", "target": "110.0",
            "entry_time_ms": 1700000000000,
        }
        trade = _row_to_trade(row)
        assert trade["created_at_ms"] == 0

    def test_insert_trade_does_not_send_created_at_ms(self):
        """End-to-end: the POST body sent to Supabase must NOT contain
        created_at_ms."""
        from coindcx_lab.db.supabase_store import SupabaseStore
        from coindcx_lab.api.env import SupabaseConfig
        cfg = SupabaseConfig(
            url="https://test.supabase.co",
            publishable_key="pub",
            service_key="svc",
        )
        store = SupabaseStore(config=cfg)
        trade = {
            "pair": "B-BTCINR", "strategy": "test", "side": "long",
            "status": "OPEN", "qty": 1.0, "entry": 100, "stop": 98,
            "target": 110, "entry_time_ms": 1700000000000,
            "exit_time_ms": None, "exit_price": None,
            "pnl_net": None, "exit_reason": None,
            "fee_inr": 0.1, "slippage_inr": 0.05,
            "signal_json": "{}", "created_at_ms": 1700000000000,
        }
        with patch("coindcx_lab.db.supabase_store.requests.post") as mock_post:
            mock_post.return_value = MagicMock(
                status_code=201, headers={},
                text='[{"id": 1}]',
            )
            mock_post.return_value.json.return_value = [{"id": 1}]
            store.insert_trade(trade)
            # Verify the POST body does NOT contain created_at_ms
            args, kwargs = mock_post.call_args
            body = kwargs["json"]
            assert "created_at_ms" not in body, \
                "POST body must not contain created_at_ms"
            # But all other fields should be there
            assert "pair" in body
            assert "entry_time_ms" in body
