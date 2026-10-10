-- =====================================================================
-- CoinDCX Spot Lab -- Row Level Security policies
-- =====================================================================
-- Migration 0002: enable RLS on every user-scoped table and grant
-- owner-scoped access only. Public read on shared tables
-- (market_data_cache, job_log) -- these contain no user-identifying data.
--
-- Idempotent: uses DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL
-- on every policy so re-applying is safe. No DROP statements anywhere.
--
-- Server-side service role bypasses RLS -- that is the ONLY way cron
-- writes reach market_data_cache and job_log. Browser/public calls
-- can only ever SELECT (and on user-scoped tables, only their own rows).
-- =====================================================================

-- ---------------------------------------------------------------------
-- Enable RLS on every table
-- ---------------------------------------------------------------------
ALTER TABLE public.user_settings      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.paper_state        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.paper_trades       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.paper_signals      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.equity_snapshots   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_data_cache  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.job_log            ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------
-- user_settings -- owner-scoped CRUD
-- ---------------------------------------------------------------------
DO $$ BEGIN
    CREATE POLICY user_settings_owner_select
        ON public.user_settings FOR SELECT
        USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY user_settings_owner_insert
        ON public.user_settings FOR INSERT
        WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY user_settings_owner_update
        ON public.user_settings FOR UPDATE
        USING (auth.uid() = user_id)
        WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------
-- paper_state -- owner-scoped CRUD
-- ---------------------------------------------------------------------
DO $$ BEGIN
    CREATE POLICY paper_state_owner_select
        ON public.paper_state FOR SELECT
        USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_state_owner_insert
        ON public.paper_state FOR INSERT
        WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_state_owner_update
        ON public.paper_state FOR UPDATE
        USING (auth.uid() = user_id)
        WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_state_owner_delete
        ON public.paper_state FOR DELETE
        USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------
-- paper_trades -- owner-scoped CRUD
-- ---------------------------------------------------------------------
DO $$ BEGIN
    CREATE POLICY paper_trades_owner_select
        ON public.paper_trades FOR SELECT
        USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_trades_owner_insert
        ON public.paper_trades FOR INSERT
        WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_trades_owner_update
        ON public.paper_trades FOR UPDATE
        USING (auth.uid() = user_id)
        WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_trades_owner_delete
        ON public.paper_trades FOR DELETE
        USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------
-- paper_signals -- owner-scoped CRUD
-- ---------------------------------------------------------------------
DO $$ BEGIN
    CREATE POLICY paper_signals_owner_select
        ON public.paper_signals FOR SELECT
        USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_signals_owner_insert
        ON public.paper_signals FOR INSERT
        WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_signals_owner_update
        ON public.paper_signals FOR UPDATE
        USING (auth.uid() = user_id)
        WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY paper_signals_owner_delete
        ON public.paper_signals FOR DELETE
        USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------
-- equity_snapshots -- owner-scoped CRUD
-- ---------------------------------------------------------------------
DO $$ BEGIN
    CREATE POLICY equity_snapshots_owner_select
        ON public.equity_snapshots FOR SELECT
        USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY equity_snapshots_owner_insert
        ON public.equity_snapshots FOR INSERT
        WITH CHECK (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE POLICY equity_snapshots_owner_delete
        ON public.equity_snapshots FOR DELETE
        USING (auth.uid() = user_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------
-- market_data_cache -- public read, NO public write (cron-only writes)
-- ---------------------------------------------------------------------
DO $$ BEGIN
    CREATE POLICY market_data_cache_read_all
        ON public.market_data_cache FOR SELECT
        USING (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- No INSERT/UPDATE/DELETE policy: only the service-role key (which
-- bypasses RLS) can write. Browser anon key is read-only here.

-- ---------------------------------------------------------------------
-- job_log -- public read (sanity monitoring), NO public write
-- ---------------------------------------------------------------------
DO $$ BEGIN
    CREATE POLICY job_log_read_all
        ON public.job_log FOR SELECT
        USING (TRUE);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
-- No INSERT/UPDATE/DELETE policy: only the service-role key writes.
