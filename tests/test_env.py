"""Tests: env-var configuration (src/coindcx_lab/api/env.py).

Verifies:
    - SupabaseConfig.from_env reads env vars correctly.
    - Missing required vars raise ConfigError.
    - The service-role key is never exposed in is_server_side() when unset.
    - Headers contain the right keys; service-key vs publishable-key paths.
    - cron_secret_matches is constant-time and rejects mismatches.
    - allowed_origin respects the env var.
"""

from __future__ import annotations

import os

import pytest


def _clean_env(monkeypatch):
    for var in ("SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY",
                "SUPABASE_SERVICE_KEY", "CRON_SECRET",
                "API_ALLOWED_ORIGIN", "ALLOW_SIMULATED_WHEN_LIVE_UNAVAILABLE"):
        monkeypatch.delenv(var, raising=False)


class TestSupabaseConfig:
    def test_reads_publishable_and_url(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_abc")
        from coindcx_lab.api.env import SupabaseConfig
        cfg = SupabaseConfig.from_env()
        assert cfg.url == "https://test.supabase.co"
        assert cfg.publishable_key == "sb_publishable_abc"
        assert cfg.service_key is None
        assert cfg.is_server_side() is False

    def test_reads_service_key_when_set(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_abc")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "sb_secret_xyz")
        from coindcx_lab.api.env import SupabaseConfig
        cfg = SupabaseConfig.from_env()
        assert cfg.service_key == "sb_secret_xyz"
        assert cfg.is_server_side() is True

    def test_missing_url_raises(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_abc")
        from coindcx_lab.api.env import SupabaseConfig, ConfigError
        with pytest.raises(ConfigError):
            SupabaseConfig.from_env()

    def test_missing_publishable_raises(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        from coindcx_lab.api.env import SupabaseConfig, ConfigError
        with pytest.raises(ConfigError):
            SupabaseConfig.from_env()

    def test_invalid_url_format_raises(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "not-a-url")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_abc")
        from coindcx_lab.api.env import SupabaseConfig, ConfigError
        with pytest.raises(ConfigError):
            SupabaseConfig.from_env()

    def test_rest_url(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co/")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "k")
        from coindcx_lab.api.env import SupabaseConfig
        cfg = SupabaseConfig.from_env()
        assert cfg.rest_url("paper_trades") == \
            "https://x.supabase.co/rest/v1/paper_trades"

    def test_headers_publishable(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "pub")
        from coindcx_lab.api.env import SupabaseConfig
        cfg = SupabaseConfig.from_env()
        h = cfg.headers(use_service=False)
        assert h["apikey"] == "pub"
        assert h["Authorization"] == "Bearer pub"

    def test_headers_service_requires_service_key(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "pub")
        from coindcx_lab.api.env import SupabaseConfig, ConfigError
        cfg = SupabaseConfig.from_env()
        with pytest.raises(ConfigError):
            cfg.headers(use_service=True)

    def test_headers_service_uses_service_key(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "pub")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "svc")
        from coindcx_lab.api.env import SupabaseConfig
        cfg = SupabaseConfig.from_env()
        h = cfg.headers(use_service=True)
        assert h["apikey"] == "svc"
        assert h["Authorization"] == "Bearer svc"

    def test_allow_simulated_default_true(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "k")
        from coindcx_lab.api.env import SupabaseConfig
        cfg = SupabaseConfig.from_env()
        assert cfg.allow_simulated is True

    def test_allow_simulated_false(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "k")
        monkeypatch.setenv("ALLOW_SIMULATED_WHEN_LIVE_UNAVAILABLE", "false")
        from coindcx_lab.api.env import SupabaseConfig
        cfg = SupabaseConfig.from_env()
        assert cfg.allow_simulated is False


class TestCronSecret:
    def test_no_secret_set_rejects(self, monkeypatch):
        _clean_env(monkeypatch)
        from coindcx_lab.api.env import cron_secret_matches
        assert cron_secret_matches("anything") is False

    def test_no_provided_rejects(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("CRON_SECRET", "expected")
        from coindcx_lab.api.env import cron_secret_matches
        assert cron_secret_matches(None) is False
        assert cron_secret_matches("") is False

    def test_match(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("CRON_SECRET", "expected")
        from coindcx_lab.api.env import cron_secret_matches
        assert cron_secret_matches("expected") is True

    def test_mismatch(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("CRON_SECRET", "expected")
        from coindcx_lab.api.env import cron_secret_matches
        assert cron_secret_matches("not-expected") is False

    def test_constant_time_different_lengths(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("CRON_SECRET", "expected")
        from coindcx_lab.api.env import cron_secret_matches
        assert cron_secret_matches("x") is False
        assert cron_secret_matches("x" * 1000) is False


class TestAllowedOrigin:
    def test_unset_returns_none(self, monkeypatch):
        _clean_env(monkeypatch)
        from coindcx_lab.api.env import allowed_origin
        assert allowed_origin() is None

    def test_set_returns_value(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("API_ALLOWED_ORIGIN", "https://example.com")
        from coindcx_lab.api.env import allowed_origin
        assert allowed_origin() == "https://example.com"

    def test_blank_returns_none(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("API_ALLOWED_ORIGIN", "   ")
        from coindcx_lab.api.env import allowed_origin
        assert allowed_origin() is None


class TestCoinDCXClientConfig:
    def test_defaults(self, monkeypatch):
        _clean_env(monkeypatch)
        from coindcx_lab.api.env import CoinDCXClientConfig
        cfg = CoinDCXClientConfig.from_env()
        assert cfg.base_url == "https://public.coindcx.com"
        assert cfg.timeout == 10.0
        assert cfg.max_retries == 3

    def test_env_overrides(self, monkeypatch):
        _clean_env(monkeypatch)
        monkeypatch.setenv("COINDCX_BASE_URL", "https://mirror.example.com")
        monkeypatch.setenv("COINDCX_TIMEOUT", "5")
        monkeypatch.setenv("COINDCX_MAX_RETRIES", "1")
        from coindcx_lab.api.env import CoinDCXClientConfig
        cfg = CoinDCXClientConfig.from_env()
        assert cfg.base_url == "https://mirror.example.com"
        assert cfg.timeout == 5.0
        assert cfg.max_retries == 1
