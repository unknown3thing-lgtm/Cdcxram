-- =====================================================================
-- CoinDCX Spot Lab -- Supabase schema (initial, additive)
-- =====================================================================
-- Migration 0001: create the initial schema.
-- All CREATE statements use IF NOT EXISTS so re-applying this migration
-- is idempotent and never destroys existing data.
--
-- Tables created:
--   user_settings       -- one row per user (risk / cost / capital config)
--   paper_state         -- one row per user (account state, daily loss)
--   paper_trades        -- full lifecycle of each paper trade
--   paper_signals      -- persisted signal snapshots (latest per pair)
--   market_data_cache   -- latest ticker per pair (from cron refresh)
--   equity_snapshots    -- equity curve points (one per open/close)
--   job_log             -- scheduled job execution log
--
-- RLS policies live in 0002_rls_policies.sql.
--
-- NOTE: user_id is a UUID WITHOUT a FK to auth.users. This lets the
-- MVP run in "shared anonymous user" mode (user_id = the anon UUID
-- defined in SupabaseStore.DEFAULT_USER_ID) without requiring Supabase
-- Auth signup. When real auth is layered on later, add the FK then.
-- =====================================================================

-- ---------------------------------------------------------------------
-- user_settings
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_settings (
    user_id                                 UUID        PRIMARY KEY,
    initial_inr                             NUMERIC(12,2) NOT NULL DEFAULT 2000.0,
    risk_per_trade_pct                      NUMERIC(5,2)  NOT NULL DEFAULT 1.0,
    max_position_pct                        NUMERIC(5,2)  NOT NULL DEFAULT 25.0,
    max_portfolio_exposure_pct              NUMERIC(5,2)  NOT NULL DEFAULT 80.0,
    max_daily_loss_pct                      NUMERIC(5,2)  NOT NULL DEFAULT 5.0,
    max_daily_loss_abs_inr                  NUMERIC(12,2) NOT NULL DEFAULT 0.0,
    max_drawdown_pct                        NUMERIC(5,2)  NOT NULL DEFAULT 15.0,
    max_open_positions                      INT          NOT NULL DEFAULT 5,
    max_daily_trades                        INT          NOT NULL DEFAULT 10,
    reward_to_risk_target                   NUMERIC(5,2)  NOT NULL DEFAULT 2.0,
    cooldown_after_consecutive_losses       INT          NOT NULL DEFAULT 3,
    allow_overnight                         BOOLEAN      NOT NULL DEFAULT TRUE,
    fee_pct                                 NUMERIC(5,3)  NOT NULL DEFAULT 0.1,
    spread_pct                              NUMERIC(5,3)  NOT NULL DEFAULT 0.05,
    slippage_pct                            NUMERIC(5,3)  NOT NULL DEFAULT 0.05,
    min_24h_volume_inr                      NUMERIC(15,2) NOT NULL DEFAULT 5000000.0,
    min_24h_trades                          INT          NOT NULL DEFAULT 1000,
    trading_allocation_pct                  NUMERIC(5,2)  NOT NULL DEFAULT 100.0,
    strategies_enabled                      JSONB        NOT NULL DEFAULT '{"trend_pullback":true,"breakout_retest":true,"momentum_continuation":true,"baseline_sma":true}'::jsonb,
    updated_at                              TIMESTAMPTZ  NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.user_settings IS
  'Per-user paper-trading risk/cost/capital configuration. Defaults match the project spec (Rs 2,000 initial, 1% risk/trade, etc.).';

-- ---------------------------------------------------------------------
-- paper_state
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.paper_state (
    user_id                  UUID         PRIMARY KEY,
    initial_inr              NUMERIC(12,2) NOT NULL DEFAULT 2000.0,
    realized_pnl             NUMERIC(12,2) NOT NULL DEFAULT 0.0,
    open_positions_value     NUMERIC(12,2) NOT NULL DEFAULT 0.0,
    open_positions_risk      NUMERIC(12,2) NOT NULL DEFAULT 0.0,
    peak_equity              NUMERIC(12,2) NOT NULL DEFAULT 2000.0,
    consecutive_losses       INT          NOT NULL DEFAULT 0,
    cooldown_until_iso       TEXT,
    paused                   BOOLEAN      NOT NULL DEFAULT FALSE,
    daily_day_iso            TEXT,
    daily_realized_pnl_day   NUMERIC(12,2) NOT NULL DEFAULT 0.0,
    daily_trades_today       INT          NOT NULL DEFAULT 0,
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT now()
);
COMMENT ON TABLE public.paper_state IS
  'Per-user account state (equity, cash, open positions value, daily loss tracker).';

-- ---------------------------------------------------------------------
-- paper_trades
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.paper_trades (
    id              BIGSERIAL    PRIMARY KEY,
    user_id         UUID         NOT NULL,
    pair            TEXT         NOT NULL,
    strategy        TEXT         NOT NULL,
    side            TEXT         NOT NULL,
    status          TEXT         NOT NULL,
    qty             NUMERIC(18,8) NOT NULL,
    entry           NUMERIC(18,8) NOT NULL,
    stop            NUMERIC(18,8) NOT NULL,
    target          NUMERIC(18,8) NOT NULL,
    entry_time_ms   BIGINT       NOT NULL,
    exit_time_ms    BIGINT,
    exit_price      NUMERIC(18,8),
    pnl_net         NUMERIC(18,8),
    exit_reason     TEXT,
    fee_inr         NUMERIC(12,4),
    slippage_inr    NUMERIC(12,4),
    signal_json     JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_user_pair
    ON public.paper_trades(user_id, pair);
CREATE INDEX IF NOT EXISTS idx_paper_trades_user_status
    ON public.paper_trades(user_id, status);
CREATE INDEX IF NOT EXISTS idx_paper_trades_user_entry_time
    ON public.paper_trades(user_id, entry_time_ms DESC);
COMMENT ON TABLE public.paper_trades IS
  'Per-user paper-trade lifecycle. status IN (OPEN, CLOSED).';

-- ---------------------------------------------------------------------
-- paper_signals
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.paper_signals (
    id                       BIGSERIAL     PRIMARY KEY,
    user_id                  UUID          NOT NULL,
    pair                     TEXT          NOT NULL,
    interval                 TEXT          NOT NULL,
    strategy                 TEXT          NOT NULL,
    state                    TEXT          NOT NULL,
    entry                    NUMERIC(18,8),
    stop                     NUMERIC(18,8),
    target                   NUMERIC(18,8),
    rr_after_cost            NUMERIC(10,4),
    est_entry_cost_inr       NUMERIC(12,4),
    est_exit_cost_inr        NUMERIC(12,4),
    supporting               JSONB         NOT NULL DEFAULT '[]'::jsonb,
    conflicting              JSONB         NOT NULL DEFAULT '[]'::jsonb,
    data_quality_warnings    JSONB         NOT NULL DEFAULT '[]'::jsonb,
    explanation              TEXT,
    candles_provenance       TEXT          NOT NULL,
    candles_fetched_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at               TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_paper_signals_user_pair
    ON public.paper_signals(user_id, pair, interval);
CREATE INDEX IF NOT EXISTS idx_paper_signals_user_created
    ON public.paper_signals(user_id, created_at DESC);
COMMENT ON TABLE public.paper_signals IS
  'Per-user signal snapshots written by the cron refresh job and by on-demand evaluations.';

-- ---------------------------------------------------------------------
-- market_data_cache
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.market_data_cache (
    pair              TEXT          PRIMARY KEY,
    last_price        NUMERIC(18,8),
    high_24h          NUMERIC(18,8),
    low_24h           NUMERIC(18,8),
    volume_24h        NUMERIC(18,8),
    change_pct_24h    NUMERIC(10,4),
    source            TEXT          NOT NULL,
    endpoint          TEXT          NOT NULL,
    fetched_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    note              TEXT
);
COMMENT ON TABLE public.market_data_cache IS
  'Latest ticker per pair (no user_id) - written by the cron job (server-side service key), read by everyone.';

-- ---------------------------------------------------------------------
-- equity_snapshots
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.equity_snapshots (
    id                       BIGSERIAL    PRIMARY KEY,
    user_id                  UUID         NOT NULL,
    time_ms                  BIGINT       NOT NULL,
    equity                   NUMERIC(12,2) NOT NULL,
    cash                     NUMERIC(12,2) NOT NULL,
    open_positions_value     NUMERIC(12,2) NOT NULL,
    peak_equity              NUMERIC(12,2) NOT NULL,
    note                     TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_snapshots_user_time
    ON public.equity_snapshots(user_id, time_ms ASC);
COMMENT ON TABLE public.equity_snapshots IS
  'Per-user equity curve points (one per trade open/close).';

-- ---------------------------------------------------------------------
-- job_log
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.job_log (
    id            BIGSERIAL    PRIMARY KEY,
    job_name      TEXT         NOT NULL,
    status        TEXT         NOT NULL,
    started_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ,
    duration_ms   INT,
    error         TEXT,
    detail        JSONB
);
CREATE INDEX IF NOT EXISTS idx_job_log_started_at
    ON public.job_log(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_log_name_started_at
    ON public.job_log(job_name, started_at DESC);
COMMENT ON TABLE public.job_log IS
  'Cron job execution log (one row per job invocation).';
