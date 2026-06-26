-- ============================================================================
-- schema.sql — Neon Postgres schema for the Live Strategy Alert Engine
-- ============================================================================
-- This is the FIRST database in the whole stack (the existing TradeKit app in
-- Part I of webinfo.txt has no database). It is the single durable store shared
-- by every component of the new engine:
--   * Engine (Python)      writes candles + signals,   reads candles + bias + trend
--   * Render Node backend  writes bias
--   * FastAPI service      writes trend, clears signals.telegram_pending,
--                          reads signals (history/fallback queries)
--
-- DESIGN RULE (webinfo.txt 1.2 / 8): every durable write is idempotent so any
-- component can be restarted, redeployed, or temporarily unavailable without
-- losing or duplicating data. Re-running this whole file is also safe
-- (CREATE ... IF NOT EXISTS everywhere).
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. candles — rolling live-candle buffer
-- ----------------------------------------------------------------------------
-- Filled by the Engine's Data Manager: REST backfill on startup, then one
-- closed Binance candle at a time (only k.x == true ticks). The buffer is
-- trimmed to the newest N rows per symbol+interval on each closed candle, so
-- this table stays small.
--
-- IDEMPOTENT KEY: a candle is uniquely identified by (symbol, interval,
-- open_time). Re-inserting the same closed candle after a WebSocket reconnect /
-- gap backfill must NOT create a duplicate — so this triple is the PRIMARY KEY.
-- Insert with:  INSERT ... ON CONFLICT (symbol, interval, open_time) DO UPDATE
-- (Resilience summary, webinfo.txt 8: "candles keyed by symbol+interval+open_time".)
CREATE TABLE IF NOT EXISTS candles (
    symbol      TEXT        NOT NULL,                 -- e.g. 'BTCUSDT'
    interval    TEXT        NOT NULL,                 -- e.g. '1h', '15m', '1d'
    open_time   BIGINT      NOT NULL,                 -- candle open, epoch milliseconds (UTC) — matches Binance kline
    open        NUMERIC     NOT NULL,
    high        NUMERIC     NOT NULL,
    low         NUMERIC     NOT NULL,
    close       NUMERIC     NOT NULL,
    volume      NUMERIC     NOT NULL,
    close_time  BIGINT      NOT NULL,                 -- candle close, epoch milliseconds (UTC)
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),   -- bookkeeping: when this row was written
    PRIMARY KEY (symbol, interval, open_time)
);

-- Fast "latest N candles per symbol+interval" reads the Runner does on every
-- closed candle (newest first).
CREATE INDEX IF NOT EXISTS idx_candles_symbol_interval_time
    ON candles (symbol, interval, open_time DESC);


-- ----------------------------------------------------------------------------
-- 2. signals — every detected strategy setup
-- ----------------------------------------------------------------------------
-- Written DIRECTLY by the Engine's Runner the instant a strategy's on_candle()
-- returns a Signal. This write is unconditional and never depends on the
-- FastAPI call succeeding (webinfo.txt 2.3 / 4). FastAPI never CREATES rows
-- here — its only write to this table is flipping telegram_pending to false
-- after a successful Telegram send.
--
-- telegram_pending: TRUE when the alert has not yet been confirmed delivered to
-- Telegram. The Engine sweeps for telegram_pending = true rows (periodically and
-- on startup) and re-attempts delivery, so an alert missed because FastAPI was
-- down at firing time is retried, not lost (webinfo.txt 4.2 telegram-retry queue).
CREATE TABLE IF NOT EXISTS signals (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy         TEXT        NOT NULL,            -- strategy name that fired
    symbol           TEXT        NOT NULL,            -- e.g. 'ETHUSDT'
    side             TEXT        NOT NULL,            -- trade direction, e.g. 'long' / 'short'
    entry            NUMERIC     NOT NULL,            -- entry price
    stop_loss        NUMERIC     NOT NULL,            -- stop-loss price
    take_profit      NUMERIC     NOT NULL,            -- take-profit price
    reason           TEXT,                            -- human-readable explanation from the strategy
    telegram_pending BOOLEAN     NOT NULL DEFAULT true, -- retry flag; FastAPI sets false on confirmed Telegram send
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()  -- when the signal fired / was written
);

-- Required by webinfo.txt: fast history queries.
-- Serves GET /signals/history (paginated, indexed on created_at) and
-- GET /signals?since= (dashboard catch-up / fallback). Newest-first.
CREATE INDEX IF NOT EXISTS idx_signals_created_at
    ON signals (created_at DESC);

-- Helps the Engine's retry sweep find unsent alerts cheaply (partial index:
-- only the rows that still need sending).
CREATE INDEX IF NOT EXISTS idx_signals_telegram_pending
    ON signals (created_at)
    WHERE telegram_pending = true;


-- ----------------------------------------------------------------------------
-- 3. (removed) bias table
-- ----------------------------------------------------------------------------
-- The per-symbol computed bias is NO LONGER stored here. Instead, when a strategy
-- detects a setup it calls the Node backend's bias API live (GET /api/bias/<method>)
-- and applies the agree-or-neutral gate inline. So there is no bias table and the
-- Node backend does not write bias into Neon.
DROP TABLE IF EXISTS bias;


-- ----------------------------------------------------------------------------
-- 4. trend — manual per-symbol / per-strategy trend input
-- ----------------------------------------------------------------------------
-- The user's manual trend input, written via FastAPI (POST /trend) and read back
-- via GET /trend. The Engine's Runner also reads it and bundles it (alongside
-- bias) into the strategy context object.
--
-- IDEMPOTENT: one current trend per (symbol, strategy). FastAPI upserts:
--   INSERT ... ON CONFLICT (symbol, strategy) DO UPDATE SET trend = ..., updated_at = now()
CREATE TABLE IF NOT EXISTS trend (
    symbol     TEXT        NOT NULL,                  -- e.g. 'BTCUSDT'
    strategy   TEXT        NOT NULL,                  -- which strategy this trend applies to
    trend      TEXT        NOT NULL,                  -- user-set trend, e.g. 'bull' / 'bear' / 'neutral'
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),    -- when the user last set it
    PRIMARY KEY (symbol, strategy)
);


-- ----------------------------------------------------------------------------
-- 5. last_alert — dedupe record (prevents sending the same alert twice)
-- ----------------------------------------------------------------------------
-- Before pushing/storing a freshly-fired signal, the Runner checks this record
-- so the same setup is not alerted twice (webinfo.txt 4.1 step 4: "deduped
-- against the last-alert record"; 8: "alerts deduped by record").
--
-- IDEMPOTENT: one last-alert row per (strategy, symbol). The Runner upserts the
-- latest fired signal's fingerprint:
--   INSERT ... ON CONFLICT (strategy, symbol) DO UPDATE SET ...
CREATE TABLE IF NOT EXISTS last_alert (
    strategy     TEXT        NOT NULL,                -- strategy that produced the alert
    symbol       TEXT        NOT NULL,                -- coin the alert was for
    side         TEXT,                                -- last alerted direction
    signal_id    BIGINT REFERENCES signals (id),      -- the signal row this dedupe record points at
    alert_key    TEXT,                                -- optional fingerprint (e.g. candle open_time / setup id) for dedupe comparison
    last_sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- when the last alert for this key was sent
    PRIMARY KEY (strategy, symbol)
);

-- Migration: add rr column to an existing trend table (safe to re-run).
ALTER TABLE trend ADD COLUMN IF NOT EXISTS rr NUMERIC DEFAULT 2.5;


-- ----------------------------------------------------------------------------
-- 6. strategy_config — per-strategy on/off switch
-- ----------------------------------------------------------------------------
-- The user can pause a strategy from the dashboard without removing its file.
-- When a row is missing the engine defaults to enabled = true.
-- FastAPI writes via POST /strategy-config; the Engine reads before each run.
-- IDEMPOTENT: one row per strategy name.
CREATE TABLE IF NOT EXISTS strategy_config (
    strategy   TEXT        NOT NULL PRIMARY KEY,
    enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
