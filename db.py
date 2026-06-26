"""
db.py — shared async database access to Neon Postgres.

Imported by BOTH processes:
  * main.py  (Engine)   — writes candles + signals, reads candles + bias + trend,
                          runs the telegram_pending retry sweep.
  * api.py   (FastAPI)  — reads/writes trend, clears signals.telegram_pending,
                          serves signal history/fallback queries.

Backed by a single asyncpg connection pool. All writes are idempotent (upserts
keyed per schema.sql) so any component can restart without duplicating data.

NOTE: logic is intentionally stubbed — only the structure/signatures are here.
"""

from __future__ import annotations

import os
from decimal import Decimal

import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

# Module-level pool, created once at startup via init_pool().
_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    """Create (once) and return the shared asyncpg connection pool."""
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set (see .env / .env.example).")
        # Small pool: the Engine is a single process doing light, bursty writes.
        # sslmode=require travels in the DSN — asyncpg honours it for Neon.
        _pool = await asyncpg.create_pool(
            dsn=DATABASE_URL, min_size=1, max_size=5, command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    """Close the shared pool on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the already-initialized pool (raises if init_pool() wasn't called)."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first.")
    return _pool


def _num(v) -> Decimal:
    """Coerce a price/volume to Decimal — asyncpg binds NUMERIC columns as Decimal,
    not float. str() first so we don't inherit binary float noise."""
    return v if isinstance(v, Decimal) else Decimal(str(v))


# --- candles ----------------------------------------------------------------
_UPSERT_CANDLE_SQL = """
    INSERT INTO candles
        (symbol, interval, open_time, open, high, low, close, volume, close_time)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (symbol, interval, open_time) DO UPDATE SET
        open        = EXCLUDED.open,
        high        = EXCLUDED.high,
        low         = EXCLUDED.low,
        close       = EXCLUDED.close,
        volume      = EXCLUDED.volume,
        close_time  = EXCLUDED.close_time,
        inserted_at = now()
"""


async def upsert_candle(symbol, interval, candle) -> None:
    """Insert/replace one closed candle (ON CONFLICT symbol+interval+open_time).

    `candle` is a dict: open_time, open, high, low, close, volume, close_time.
    Idempotent — re-inserting the same candle after a reconnect/gap-fill is a no-op
    on the key columns."""
    await get_pool().execute(
        _UPSERT_CANDLE_SQL,
        symbol, interval,
        int(candle["open_time"]),
        _num(candle["open"]), _num(candle["high"]), _num(candle["low"]),
        _num(candle["close"]), _num(candle["volume"]),
        int(candle["close_time"]),
    )


async def trim_candles(symbol, interval, keep_n) -> None:
    """Trim the rolling buffer to the newest keep_n rows for symbol+interval."""
    await get_pool().execute(
        """
        DELETE FROM candles
        WHERE symbol = $1 AND interval = $2 AND open_time < (
            SELECT MIN(open_time) FROM (
                SELECT open_time FROM candles
                WHERE symbol = $1 AND interval = $2
                ORDER BY open_time DESC
                LIMIT $3
            ) keep
        )
        """,
        symbol, interval, int(keep_n),
    )


async def get_recent_candles(symbol, interval, limit):
    """Return the latest `limit` candles (newest-first) for a symbol+interval."""
    return await get_pool().fetch(
        """
        SELECT symbol, interval, open_time, open, high, low, close, volume, close_time
        FROM candles
        WHERE symbol = $1 AND interval = $2
        ORDER BY open_time DESC
        LIMIT $3
        """,
        symbol, interval, int(limit),
    )


# --- signals ----------------------------------------------------------------
async def insert_signal(signal) -> int:
    """Write a fired signal (telegram_pending defaults true). Returns new id.

    `signal` is a strategies.base.Signal dataclass. This write is UNCONDITIONAL in
    the Runner pipeline — it never depends on the FastAPI call (webinfo.txt 2.3/4).
    NUMERIC prices are bound as Decimal."""
    row = await get_pool().fetchrow(
        """
        INSERT INTO signals
            (strategy, symbol, side, entry, stop_loss, take_profit, reason)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        signal.strategy, signal.symbol, signal.side,
        _num(signal.entry), _num(signal.stop_loss), _num(signal.take_profit),
        signal.reason,
    )
    return row["id"]


async def get_pending_telegram_signals():
    """Return signals with telegram_pending = true (for the retry sweep), oldest
    first so they are re-delivered in the order they fired."""
    return await get_pool().fetch(
        """
        SELECT id, strategy, symbol, side, entry, stop_loss, take_profit,
               reason, created_at
        FROM signals
        WHERE telegram_pending = true
        ORDER BY created_at ASC
        """
    )


async def clear_telegram_pending(signal_id) -> None:
    """Set telegram_pending = false after a confirmed Telegram send."""
    await get_pool().execute(
        "UPDATE signals SET telegram_pending = false WHERE id = $1", int(signal_id)
    )


_SIGNAL_COLS = ("id, strategy, symbol, side, entry, stop_loss, take_profit, "
                "reason, telegram_pending, created_at")


async def get_signals_since(since):
    """Recent signals for dashboard catch-up / fallback (GET /signals?since=).

    With `since` (an ISO-8601 timestamp string): signals created strictly after it.
    Without it: the most recent 50. Newest-first either way."""
    if since:
        return await get_pool().fetch(
            f"SELECT {_SIGNAL_COLS} FROM signals "
            "WHERE created_at > $1::timestamptz ORDER BY created_at DESC",
            since,
        )
    return await get_pool().fetch(
        f"SELECT {_SIGNAL_COLS} FROM signals ORDER BY created_at DESC LIMIT 50"
    )


async def get_signals_history(limit, offset):
    """Paginated historical signals (GET /signals/history), newest-first."""
    return await get_pool().fetch(
        f"SELECT {_SIGNAL_COLS} FROM signals "
        "ORDER BY created_at DESC LIMIT $1 OFFSET $2",
        int(limit), int(offset),
    )


# --- bias --------------------------------------------------------------------
# (removed) Bias is no longer stored in Neon. Strategies call the Node backend's
# bias API live when a setup is found — see strategies/crt.py.


# --- trend ------------------------------------------------------------------
async def upsert_trend(symbol, strategy, trend, rr=None) -> None:
    """Set the user's manual trend + optional R:R (ON CONFLICT symbol+strategy)."""
    rr_val = _num(rr) if rr is not None else _num(2.5)
    await get_pool().execute(
        """
        INSERT INTO trend (symbol, strategy, trend, rr, updated_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (symbol, strategy)
        DO UPDATE SET trend = EXCLUDED.trend, rr = EXCLUDED.rr, updated_at = now()
        """,
        symbol, strategy, trend, rr_val,
    )


async def get_trend(symbol, strategy):
    """Read the user's manual trend row (Runner bundles it into context).
    Returns the row (with .trend and .rr) or None."""
    return await get_pool().fetchrow(
        "SELECT symbol, strategy, trend, rr, updated_at "
        "FROM trend WHERE symbol = $1 AND strategy = $2",
        symbol, strategy,
    )


# --- last_alert (dedupe) ----------------------------------------------------
async def get_last_alert(strategy, symbol):
    """Read the dedupe fingerprint row for strategy+symbol (or None)."""
    return await get_pool().fetchrow(
        "SELECT strategy, symbol, side, signal_id, alert_key, last_sent_at "
        "FROM last_alert WHERE strategy = $1 AND symbol = $2",
        strategy, symbol,
    )


# --- strategy_config (on/off switch) ----------------------------------------
async def get_strategy_enabled(strategy: str) -> bool:
    """Return True if the strategy is enabled. Defaults to True when no row exists."""
    row = await get_pool().fetchrow(
        "SELECT enabled FROM strategy_config WHERE strategy = $1", strategy
    )
    return bool(row["enabled"]) if row else True


async def set_strategy_enabled(strategy: str, enabled: bool) -> None:
    """Upsert the enabled flag for a strategy."""
    await get_pool().execute(
        """
        INSERT INTO strategy_config (strategy, enabled, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (strategy) DO UPDATE SET
            enabled    = EXCLUDED.enabled,
            updated_at = now()
        """,
        strategy, enabled,
    )


async def get_all_strategy_configs() -> list:
    """Return all strategy_config rows (strategy, enabled, updated_at)."""
    return await get_pool().fetch(
        "SELECT strategy, enabled, updated_at FROM strategy_config ORDER BY strategy"
    )


# --- last_alert (dedupe) ----------------------------------------------------
async def upsert_last_alert(strategy, symbol, side, signal_id, alert_key) -> None:
    """Record the latest alert fingerprint (ON CONFLICT strategy+symbol)."""
    await get_pool().execute(
        """
        INSERT INTO last_alert (strategy, symbol, side, signal_id, alert_key, last_sent_at)
        VALUES ($1, $2, $3, $4, $5, now())
        ON CONFLICT (strategy, symbol) DO UPDATE SET
            side         = EXCLUDED.side,
            signal_id    = EXCLUDED.signal_id,
            alert_key    = EXCLUDED.alert_key,
            last_sent_at = now()
        """,
        strategy, symbol, side, int(signal_id), alert_key,
    )
