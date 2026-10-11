"""Database layer for CoinDCX Spot Lab.

Two interchangeable backends, both implementing the same `Store`
protocol so the rest of the engine is backend-agnostic:

    - Store            (SQLite, file-backed; used for local dev + tests)
    - SupabaseStore    (Supabase REST API; used by Vercel functions)

The engine (PaperTradingEngine, etc.) never knows which backend is in
use — it just calls methods on the Store interface.
"""

from .store import Store, StoreError  # noqa: F401
from .supabase_store import (  # noqa: F401
    SupabaseStore, DEFAULT_USER_ID,
)

__all__ = [
    "Store",
    "StoreError",
    "SupabaseStore",
    "DEFAULT_USER_ID",
]
