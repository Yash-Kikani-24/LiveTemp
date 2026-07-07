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


def trend_state(manual_trend, want) -> str:
    """Classify the user's manual trend against the setup's required direction into
    one canonical state: 'WITH-TREND', 'COUNTER-TREND', or 'NO-TREND-SET'.

    `want` is the direction ('BULLISH'/'BEARISH') the trend must be for the setup to
    count as with-trend. Kept here so every strategy classifies identically."""
    if manual_trend == want:
        return "WITH-TREND"
    if manual_trend in ("BULLISH", "BEARISH"):
        return "COUNTER-TREND"
    return "NO-TREND-SET"


_TREND_INDICATOR = {
    "WITH-TREND":    "✅ With Trend",
    "COUNTER-TREND": "⚠️ Counter-Trend",
    "NO-TREND-SET":  "➖ No Trend Set",
}


def fmt_trend(alignment) -> str:
    """Line-0 trend indicator for a Signal.reason — the string the dashboard's pill
    and the Telegram alert read. Three distinct states so the UI can tell
    counter-trend apart from 'no manual trend set' (both used to collapse to '❌')."""
    return _TREND_INDICATOR.get(alignment, "➖ No Trend Set")


def fmt_setup_candles(*candle_ms) -> str:
    """'Setup candles' block appended to a Signal.reason as extra lines.

    Pass the OPEN time (epoch ms) of EACH candle that formed the setup, oldest
    first: a pattern strategy (CRT / Sweep) passes two (→ C1, C2); a single-candle
    strategy (RSI crossover) passes one (→ C1). Rendered in IST so the dashboard and
    Telegram show exactly which candle(s) produced the signal — one 'Cn → …' line
    each. The Telegram formatter treats reason line 0 as the inline trend indicator
    and everything below it as this details block."""
    if not candle_ms:
        return ""
    lines = ["🕯 Setup candles (IST)"]
    for i, ms in enumerate(candle_ms, start=1):
        t = ist(int(ms)).strftime("%a %d %b · %H:%M")
        lines.append(f"   C{i}  →  {t}")
    return "\n".join(lines)


def fmt_setup_time(ms) -> str:
    """Single 'setup candle' time line (IST) for a Signal.reason.

    Used by single-candle strategies (e.g. RSI) that surface indicator readings
    instead of a full setup-candles block, but still want the setup candle's time
    shown in the Telegram alert. Rendered like fmt_setup_candles' timestamps. It
    carries no 'Cn →' / 'SMA…' token, so the dashboard's reason parser ignores it
    (the card keeps showing just the indicator readings) while Telegram prints it
    in the details block."""
    t = ist(int(ms)).strftime("%a %d %b · %H:%M")
    return f"🕯 Setup candle (IST):  {t}"


def fmt_indicators(title, *pairs) -> str:
    """Indicator-readings block appended to a Signal.reason as extra lines.

    Pass a short `title` (e.g. 'SMA/EMA on RSI (signal candle)') and then any
    number of (label, value) pairs — each becomes a 'label → value' line. Used by
    indicator strategies (e.g. RSI) to surface the key readings ON the signal
    candle in the dashboard and Telegram, in place of setup-candle times. The
    dashboard parser keys off the 'SMA…'/'EMA…' labels, so keep them stable.

    Formatted to match fmt_setup_candles: the Telegram formatter treats reason
    line 0 as the trend indicator and everything below it as the details block,
    so this renders there automatically too."""
    if not pairs:
        return ""
    lines = [f"📊 {title}"]
    for label, value in pairs:
        lines.append(f"   {label}  →  {value}")
    return "\n".join(lines)


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
