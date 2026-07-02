"""
strategies/_shared.py — utilities shared by every drop-in strategy.

Centralising these means a new strategy file only needs to define what makes it
different: its detection logic and its constants. The main.py discovery loop skips
modules whose names start with '_', so this file is never treated as a strategy.

Contents:
  * Account / fee model constants  (POSITION, FEE_PCT, etc.)
  * Time helpers                   (utc, ist)
  * Candle / context normalisation (_field, _norm, _ctx_get, _norm_dir)
  * jp-risk calculator             (calc_jp_risk) — line-for-line port of
                                   backend/src/tools/jpRisk.js (frozen v1.0)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

# ============================================================================
# Shared account / fee model
# All strategies share the same account model; override locally if needed.
# ============================================================================

IST_OFFSET_MIN     = 330            # IST = UTC + 5:30
ONE_HOUR_MS        = 3_600_000

POSITION           = 100_000.0      # notional position size ($)
FEE_PCT            = 0.05           # 0.05% taker fee per side
ALLOWED_RISK       = 1_000.0        # max dollar loss per trade
LEVERAGE           = 100
ENGINE_MIN_RR      = 2              # net (post-fee) R:R floor; skip trades below
MAX_POS_AFTER_FEES = 250_000.0      # max notional after fee adjustment
DEFAULT_TARGET_RR  = 2.5           # R:R when context carries none


# ============================================================================
# Time helpers
# ============================================================================

def utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def ist(ms: int) -> datetime:
    """Convert epoch-ms to an IST-equivalent UTC datetime (for display only)."""
    return utc(ms + IST_OFFSET_MIN * 60_000)


# ============================================================================
# Candle / context normalisation
# ============================================================================

def _field(c, key):
    """Read a field from a candle row that may be a dict or an asyncpg Record."""
    try:
        return c[key]
    except (TypeError, KeyError, IndexError):
        return getattr(c, key)


def _norm(c) -> dict:
    """Normalise a live candle row into {t, o, h, l, c} with float values.

    NUMERIC columns come back as Decimal from Neon; cast to float exactly as
    the backtests' CSV load() did. open_time is epoch-ms (BIGINT)."""
    return {
        "t": int(_field(c, "open_time")),
        "o": float(_field(c, "open")),
        "h": float(_field(c, "high")),
        "l": float(_field(c, "low")),
        "c": float(_field(c, "close")),
    }


def _ctx_get(context, key):
    """Read context[key] whether context is a dict or an object. Tolerates the
    value being a plain string OR a row/dict carrying a 'direction'/'trend'
    sub-field."""
    v = context.get(key) if isinstance(context, dict) else getattr(context, key, None)
    if isinstance(v, dict):
        v = v.get("direction") or v.get("trend")
    elif v is not None and not isinstance(v, str):
        v = getattr(v, "direction", getattr(v, "trend", v))
    return v


def _norm_dir(raw) -> str | None:
    """Normalise a direction-ish value to 'BULLISH'/'BEARISH'/'NEUTRAL' (or None).
    Accepts either vocabulary: bull/bear or bullish/bearish."""
    if raw is None:
        return None
    v = str(raw).strip().upper()
    if v in ("BULL", "BULLISH"):
        return "BULLISH"
    if v in ("BEAR", "BEARISH"):
        return "BEARISH"
    if v in ("NEUTRAL", "FLAT"):
        return "NEUTRAL"
    return v or None


def fmt_setup_candles(c1_ms, c2_ms) -> str:
    """Two-line 'Setup candles' block, appended to a Signal.reason as extra lines.

    A pattern-based strategy (CRT / Sweep) forms its setup from two candles C1 and
    C2; this renders their OPEN times in IST so the Telegram alert can show exactly
    which candles produced the signal. The Telegram formatter treats reason line 0
    as the inline trend indicator and everything below it as a details block, so
    this block appears beneath the price lines. Kept here (not in each strategy) so
    all pattern strategies share one consistent, easy-to-read layout."""
    c1 = ist(int(c1_ms)).strftime("%a %d %b · %H:%M")
    c2 = ist(int(c2_ms)).strftime("%a %d %b · %H:%M")
    return f"🕯 Setup candles (IST)\n   C1  →  {c1}\n   C2  →  {c2}"


# ============================================================================
# jp-risk calculator — line-for-line port of backend/src/tools/jpRisk.js.
# Every strategy that needs post-fee R:R or position sizing calls this.
# The backtest called POST /api/tools/jp-risk; this reproduces the same numbers
# locally so no HTTP round-trip is needed.
# ============================================================================

def calc_jp_risk(position, direction, entry, sl, tp, leverage, fee,
                 allowed_risk: float = ALLOWED_RISK) -> dict | None:
    pos     = float(position)
    e       = float(entry)
    s       = float(sl)
    t       = float(tp)
    lev     = float(leverage)
    fee_pct = float(fee) / 100.0

    if not pos or not e or not s:
        return None

    direction_invalid = False
    if direction == "long":
        if t and t <= e:
            direction_invalid = True
        if s >= e:
            direction_invalid = True
    else:
        if t and t >= e:
            direction_invalid = True
        if s <= e:
            direction_invalid = True

    effective_entry   = e * (1 + fee_pct) if direction == "long" else e * (1 - fee_pct)
    coins             = pos / e
    sl_distance       = abs(e - s)
    tp_distance       = abs(t - e) if t else 0.0
    raw_loss          = sl_distance * coins
    raw_profit        = tp_distance * coins
    raw_rr            = raw_profit / raw_loss if raw_profit > 0 else 0.0
    entry_fee         = pos * fee_pct
    exit_fee_sl       = coins * s * fee_pct
    exit_fee_tp       = coins * t * fee_pct if t else 0.0
    loss_after_fees   = raw_loss   + entry_fee + exit_fee_sl
    profit_after_fees = raw_profit - entry_fee - exit_fee_tp
    rr_after_fees     = profit_after_fees / loss_after_fees if profit_after_fees > 0 else 0.0
    allowed           = float(allowed_risk)
    margin            = pos / lev
    liquidation_price = (effective_entry * (1 - 1 / lev) if direction == "long"
                         else effective_entry * (1 + 1 / lev))

    reject_rr        = rr_after_fees < 1 - 1e-6
    reject_risk      = loss_after_fees > allowed + 0.01
    reject_direction = direction_invalid
    reject           = reject_rr or reject_risk or reject_direction

    max_position_raw        = math.floor((allowed / raw_loss) * pos) if raw_loss else pos
    max_position_after_fees = (math.floor((allowed / loss_after_fees) * pos)
                               if loss_after_fees else pos)

    return {
        "liquidationPrice":     liquidation_price,
        "coins":                coins,
        "slDistance":           sl_distance,
        "tpDistance":           tp_distance,
        "rawLoss":              raw_loss,
        "rawProfit":            raw_profit,
        "rawRR":                raw_rr,
        "lossAfterFees":        loss_after_fees,
        "profitAfterFees":      profit_after_fees,
        "rrAfterFees":          rr_after_fees,
        "margin":               margin,
        "reject":               reject,
        "rejectRR":             reject_rr,
        "rejectRisk":           reject_risk,
        "rejectDirection":      direction_invalid,
        "maxPositionRaw":       max_position_raw,
        "maxPositionAfterFees": max_position_after_fees,
        "lev":                  lev,
    }
