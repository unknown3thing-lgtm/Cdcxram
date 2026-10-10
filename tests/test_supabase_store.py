"""Tests: SupabaseStore (REST-backed) — mocked HTTP, no network.

The SupabaseStore adapter must mirror the SQLite Store interface so
the PaperTradingEngine can use either backend. These tests verify the
adapter:
    - issues the right HTTP methods + paths + headers
    - correctly parses Supabase responses
    - raises StoreError on HTTP errors
    - never uses the publishable key for server-side writes
    - uses the service-role key (and bypasses RLS) when available
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest


def _make_config(service_key: str = None) -> "SupabaseConfig":
    from coindcx_lab.api.env import SupabaseConfig
    return SupabaseConfig(
        url="https://x.supabase.co",
        publishable_key="pub",
        service_key=service_key,
        allow_simulated=True,
    )


def _mock_response(status_code: int, body=None, *, headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.reason = "OK" if status_code < 400 else "ERR"
    r.text = json.dumps(body) if isinstance(body, (dict, list)) else (body or "")
    r.headers = headers or {}
    r.json.return_value = body if body is not None else []
    return r


class TestIsServerSide:
    def test_no_service_key_is_client(self):
        cfg = _make_config(service_key=None)
        assert cfg.is_server_side() is False

    def test_with_service_key_is_server(self):
        cfg = _make_config(service_key="svc")
        assert cfg.is_server_side() is True


class TestSaveSettings:
    def test_save_settings_requires_service_key(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        from coindcx_lab.db.store import StoreError
        cfg = _make_config(service_key=None)
        store = SupabaseStore(config=cfg)
        with pytest.raises(StoreError) as e:
            store.save_settings("risk", {"initial_inr": 5000})
        assert "service-role" in str(e.value)

    def test_save_settings_with_service_key_posts(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = _make_config(service_key="svc")
        store = SupabaseStore(config=cfg)
        body = {"initial_inr": 5000, "risk_per_trade_pct": 2.0}
        with patch("coindcx_lab.db.supabase_store.requests.post") as mock_post:
            mock_post.return_value = _mock_response(
                201, [{"user_id": "00000000-0000-0000-0000-000000000000"}])
            store.save_settings("risk", body)
            # Verify the call
            args, kwargs = mock_post.call_args
            url = args[0]
            assert "/rest/v1/user_settings" in url
            assert "on_conflict" in kwargs["params"]
            assert kwargs["params"]["on_conflict"] == "user_id"
            assert kwargs["headers"]["apikey"] == "svc"
            assert kwargs["headers"]["Prefer"] == \
                "resolution=merge-duplicates, return=representation"
            assert kwargs["json"]["initial_inr"] == 5000
            assert kwargs["json"]["user_id"] == \
                "00000000-0000-0000-0000-000000000000"

    def test_save_settings_only_risk_key_persisted(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = _make_config(service_key="svc")
        store = SupabaseStore(config=cfg)
        # Other keys are silently ignored (no HTTP call made).
        with patch("coindcx_lab.db.supabase_store.requests.post") as mock_post:
            store.save_settings("other_key", {"foo": 1})
            assert mock_post.call_count == 0


class TestLoadSettings:
    def test_load_settings_uses_publishable_key(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = _make_config(service_key=None)
        store = SupabaseStore(config=cfg)
        with patch("coindcx_lab.db.supabase_store.requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, [])
            store.load_settings("risk")
            args, kwargs = mock_get.call_args
            assert kwargs["headers"]["apikey"] == "pub"

    def test_load_settings_real(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = _make_config(service_key=None)
        store = SupabaseStore(config=cfg)
        with patch("coindcx_lab.db.supabase_store.requests.get") as mock_get:
            mock_get.return_value = _mock_response(
                200, [{"initial_inr": "5000.00",
                       "risk_per_trade_pct": "2.00",
                       "fee_pct": "0.100"}])
            result = store.load_settings("risk")
            assert result is not None
            assert result["initial_inr"] == 5000.0
            assert result["risk_per_trade_pct"] == 2.0
            args, kwargs = mock_get.call_args
            # PostgREST filter form is `eq.<value>` (no dot between eq and value).
            params_str = str(kwargs["params"])
            assert "user_id" in params_str
            assert "eq." in params_str
            assert "00000000-0000-0000-0000-000000000000" in params_str
            assert kwargs["headers"]["apikey"] == "pub"

    def test_load_settings_returns_default_when_empty(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = _make_config(service_key=None)
        store = SupabaseStore(config=cfg)
        with patch("coindcx_lab.db.supabase_store.requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, [])
            result = store.load_settings("risk", default={"default": True})
            assert result == {"default": True}


class TestSaveState:
    def test_save_state_requires_service_key(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        from coindcx_lab.db.store import StoreError
        cfg = _make_config(service_key=None)
        store = SupabaseStore(config=cfg)
        with pytest.raises(StoreError):
            store.save_state({"initial_inr": 2000})

    def test_save_state_with_service_key(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = _make_config(service_key="svc")
        store = SupabaseStore(config=cfg)
        with patch("coindcx_lab.db.supabase_store.requests.post") as mock_post:
            mock_post.return_value = _mock_response(201, [])
            store.save_state({"initial_inr": 2000, "realized_pnl": 50})
            args, kwargs = mock_post.call_args
            assert "/rest/v1/paper_state" in args[0]


class TestInsertTrade:
    def test_insert_trade_returns_id(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = _make_config(service_key="svc")
        store = SupabaseStore(config=cfg)
        with patch("coindcx_lab.db.supabase_store.requests.post") as mock_post:
            mock_post.return_value = _mock_response(
                201, [{"id": 42}])
            trade_id = store.insert_trade({
                "pair": "B-BTCINR", "strategy": "x", "side": "long",
                "status": "OPEN", "qty": 1.0, "entry": 100, "stop": 98,
                "target": 104, "entry_time_ms": 1700000000000,
                "exit_time_ms": None, "exit_price": None,
                "pnl_net": None, "exit_reason": None,
                "fee_inr": 0, "slippage_inr": 0,
                "signal_json": "", "created_at_ms": 1700000000000,
            })
            assert trade_id == 42

    def test_insert_trade_requires_service_key(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        from coindcx_lab.db.store import StoreError
        cfg = _make_config(service_key=None)
        store = SupabaseStore(config=cfg)
        with pytest.raises(StoreError):
            store.insert_trade({})


class TestListTrades:
    def test_list_trades_returns_dicts(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = _make_config(service_key=None)
        store = SupabaseStore(config=cfg)
        with patch("coindcx_lab.db.supabase_store.requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, [
                {"id": 1, "pair": "B-BTCINR", "qty": "1.5",
                 "entry": "100.0", "status": "OPEN",
                 "entry_time_ms": 1700000000000},
            ])
            trades = store.list_trades()
            assert len(trades) == 1
            assert trades[0]["pair"] == "B-BTCINR"
            assert trades[0]["qty"] == 1.5
            assert trades[0]["entry"] == 100.0


class TestResetAllTrades:
    def test_reset_requires_service_key(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        from coindcx_lab.db.store import StoreError
        cfg = _make_config(service_key=None)
        store = SupabaseStore(config=cfg)
        with pytest.raises(StoreError):
            store.reset_all_trades()

    def test_reset_filters_by_user_id(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        cfg = _make_config(service_key="svc")
        store = SupabaseStore(config=cfg)
        with patch("coindcx_lab.db.supabase_store.requests.delete") as mock_del:
            mock_del.return_value = _mock_response(204, [])
            store.reset_all_trades()
            # 3 DELETE calls: paper_trades, equity_snapshots, paper_state
            assert mock_del.call_count == 3
            # Each must filter by user_id
            for call in mock_del.call_args_list:
                args, kwargs = call
                params = kwargs["params"]
                params_str = str(params)
                assert "user_id" in params_str
                assert "eq." in params_str
                assert "00000000-0000-0000-0000-000000000000" in params_str


class TestErrorHandling:
    def test_http_error_raises_store_error(self):
        from coindcx_lab.db.supabase_store import SupabaseStore
        from coindcx_lab.db.store import StoreError
        cfg = _make_config(service_key=None)
        store = SupabaseStore(config=cfg)
        with patch("coindcx_lab.db.supabase_store.requests.get") as mock_get:
            mock_get.return_value = _mock_response(
                401, {"error": "Unauthorized"}, headers={"Content-Type": "application/json"})
            with pytest.raises(StoreError) as e:
                store.list_trades()
            assert "401" in str(e.value)


class TestDefaultUserId:
    def test_default_user_id_is_anon_uuid(self):
        from coindcx_lab.db.supabase_store import DEFAULT_USER_ID
        assert DEFAULT_USER_ID == "00000000-0000-0000-0000-000000000000"
        # Must be a valid UUID format
        import uuid
        uuid.UUID(DEFAULT_USER_ID)
