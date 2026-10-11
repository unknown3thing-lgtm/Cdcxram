-- =====================================================================
-- CoinDCX Spot Lab -- Anon-user RLS policies
-- =====================================================================
-- Migration 0003: enable public access to the shared anonymous user's
-- rows. This lets the MVP run without Supabase Auth signup: every
-- browser request uses the publishable (anon) key + the shared anon
-- user_id = '00000000-0000-0000-0000-000000000000' (defined in
-- SupabaseStore.DEFAULT_USER_ID).
--
-- Idempotent: uses DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL.
-- No DROP statements.
--
-- When real Supabase Auth is layered on later, DROP these policies
-- (they only allow access when user_id = the anon UUID, so they're
-- safe to keep even after auth is added — they just become inert).
-- =====================================================================

-- The shared anonymous user UUID.
-- This MUST match SupabaseStore.DEFAULT_USER_ID in
-- src/coindcx_lab/db/supabase_store.py.
-- If you ever change one, change the other to match.
DO $$ BEGIN
    CREATE POLICY user_settings_anon_full
        ON public.user_settings
        FOR ALL
        USING (user_id = '00000000-0000-0000-0000-000000000000'::uuid)
        WITH CHECK (user_id = '00000000-0000-0000-0000-000000000000'::uuid);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_state_anon_full
        ON public.paper_state
        FOR ALL
        USING (user_id = '00000000-0000-0000-0000-000000000000'::uuid)
        WITH CHECK (user_id = '00000000-0000-0000-0000-000000000000'::uuid);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_trades_anon_full
        ON public.paper_trades
        FOR ALL
        USING (user_id = '00000000-0000-0000-0000-000000000000'::uuid)
        WITH CHECK (user_id = '00000000-0000-0000-0000-000000000000'::uuid);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_signals_anon_full
        ON public.paper_signals
        FOR ALL
        USING (user_id = '00000000-0000-0000-0000-000000000000'::uuid)
        WITH CHECK (user_id = '00000000-0000-0000-0000-000000000000'::uuid);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY equity_snapshots_anon_full
        ON public.equity_snapshots
        FOR ALL
        USING (user_id = '00000000-0000-0000-0000-000000000000'::uuid)
        WITH CHECK (user_id = '00000000-0000-0000-0000-000000000000'::uuid);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
