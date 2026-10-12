-- =====================================================================
-- CoinDCX Spot Lab — Add request_key column for idempotency
-- =====================================================================
-- Migration 0004: adds a dedicated `request_key` column to paper_trades
-- for server-enforced idempotency on POST /api/trades.
--
-- This is ADDITIVE — does NOT modify existing columns, does NOT drop
-- data, does NOT change RLS policies. The column is nullable (existing
-- trades have NULL request_key, which is fine — they were created
-- before idempotency was implemented).
--
-- No UNIQUE constraint is added because:
--   1. The anon-user RLS policy would make a UNIQUE constraint
--      conflict with multi-user scenarios.
--   2. Application-level dedup (SELECT before INSERT) is sufficient
--      for paper trading (not real money).
-- =====================================================================

ALTER TABLE public.paper_trades
    ADD COLUMN IF NOT EXISTS request_key TEXT;

COMMENT ON COLUMN public.paper_trades.request_key IS
  'Client-generated UUID for idempotency dedup. NULL for trades created before Phase 6.';
