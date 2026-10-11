"""Shared factory for the Supabase-backed PaperTradingEngine."""

from __future__ import annotations

from typing import Optional


def build_paper_engine(request):
    """Build a Supabase-backed PaperTradingEngine.

    Returns the engine, or None if an error response has already been
    sent (in which case the caller should return immediately).
    """
    from .common import current_user_id, send_error
    from coindcx_lab.api.env import SupabaseConfig, ConfigError
    from coindcx_lab.db.supabase_store import SupabaseStore
    from coindcx_lab.engine.paper import PaperTradingEngine
    from coindcx_lab.engine.risk import RiskSettings

    try:
        cfg = SupabaseConfig.from_env()
        store = SupabaseStore(config=cfg, user_id=current_user_id(request))
    except ConfigError as e:
        send_error(request, 500, "config_error",
                   f"Supabase env not configured: {e}")
        return None
    loaded = store.load_settings("risk")
    settings = RiskSettings(**loaded) if loaded else RiskSettings()
    pe = PaperTradingEngine(settings=settings, store=store)
    # Auto-reconcile: fix any state inconsistencies caused by external
    # DB modifications (e.g., direct trade deletes that bypass the engine).
    # This is safe — it only recalculates derived values, never deletes
    # trades or changes settings.
    pe.reconcile_state()
    return pe
