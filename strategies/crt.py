"""
strategies/crt.py — live, drop-in port of the offline CRT backtest.

Source of truth for the trading logic: ../stratergy/crt_final_stratergy.py
(the "STAGE 1 engine, no v2/v3 filter" backtest). This file wraps that exact
logic behind the webinfo.txt 3.1 interface (name / symbols / interval / lookback
+ on_candle(symbol, candles, context) -> Signal | None).

WHAT IS IDENTICAL TO THE BACKTEST (copied verbatim below):
  * detect()          — the 2-candle CRT pattern vs the previous IST-day H/L.
  * structure_trade()  — fixed-stop plan (per-coin FIXED_STOP_POINTS, TP = 2.5R).
  * the jp-risk maths  — calc_jp_risk() is a line-for-line port of the frozen
                         backend/src/tools/jpRisk.js, so net R:R and
                         max-position-after-fees are computed the SAME way the
                         backtest got them from POST /api/tools/jp-risk.
  * every tunable constant (POSITION, FEE_PCT, ALLOWED_RISK, LEVERAGE,
    FIXED_TARGET_RR, ENGINE_MIN_RR, MAX_POS_AFTER_FEES, the 07:00–18:00 IST
    window, the IST day-boundary maths, and the per-coin fixed-stop points).

WHAT CHANGED (wrapping / data source only — see the walkthrough in chat):
  1. Data source: CSV files -> the live CLOSED-candle lookback window. We read
     the two most recent CLOSED 1h candles as (c1, c2) and the previous IST day's
     H/L straight out of `candles`. No in-progress candle is ever used.
  2. Bias source (BOTH backtest gates preserved):
       GATE 1 — backtest DATE_BIAS_RANGES (the manual regime windows) -> the MANUAL
         trend from Neon's `trend` table (context.trend), set from the dashboard.
         EXACT match: long requires BULLISH, short requires BEARISH; NEUTRAL/unset
         -> no trade.
       GATE 2 — backtest server dailyClose bias -> fetched LIVE from the Node backend
         (GET /api/bias/dailyClose) the moment a setup is found and passes GATE 1,
         exactly like the backtest called the bias server. AGREE-OR-NEUTRAL: long
         needs BULLISH or NEUTRAL, short needs BEARISH or NEUTRAL; anything else
         (incl. a failed fetch after retries = the backtest's 'N/A') -> no trade.
     There is NO bias table in Neon. on_candle is async so it can await the bias
     HTTP call without blocking the engine event loop. NODE_BACKEND_URL (default
     http://localhost:3001) selects the backend.
  3. Per-symbol: the backtest pins one SYMBOL; this serves all three, so the
     per-coin fixed-stop distance is looked up by the incoming `symbol`
     (identical values, just parameterised instead of a module global).
  4. Fill/resolution: the backtest's 15m find_fill / fill-time-window /
     2-hour-staleness / resolve(TP/SL/EOD) steps are NOT reproduced — a 1h
     closed-candle alert engine has no 15m feed and does not execute trades. We
     emit the Signal at pattern close (entry = c2 open, fixed SL, 2.5R TP); the
     execution layer owns the fill. Every gate decidable AT candle close is kept.
"""

from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime, timezone

import httpx

from .base import Signal, Strategy

# ============================================================================
# Node backend bias API (GATE 2 — the computed dailyClose bias, fetched LIVE).
# Base URL defaults to the local dev backend; set NODE_BACKEND_URL to the Render
# URL in production. Same method the backtest used (BIAS_METHOD = 'dailyClose').
# ============================================================================
NODE_BACKEND_URL = os.environ.get("NODE_BACKEND_URL", "http://localhost:3001").rstrip("/")
BIAS_METHOD = "dailyClose"
NODE_BIAS_TIMEOUT = 5.0       # seconds per attempt
NODE_BIAS_RETRIES = 3         # total attempts before giving up
NODE_BIAS_RETRY_DELAY = 0.5   # seconds between attempts

# ============================================================================
# ENGINE settings — copied verbatim from crt_final_stratergy.py
# ============================================================================

# --- DYNAMIC STRUCTURE-BASED RISK (only used by STRATEGY_MODE == 'structure') ---
MIN_RISK_PCT = 500.0 / 70000.0   # ~= 0.714%
MAX_RISK_PCT = 700.0 / 70000.0   # ~= 1.000%
TARGET_RR    = 2.5               # reward distance = TARGET_RR x risk

# --- PDH/PDL CONTINUATION vs FADE ZONES (scale with entry price) ---
CONTINUATION_ZONE_PCT = 0.0035   # ~245 pts @ 70k
FADE_MIN_DISTANCE_PCT = 0.017    # ~1190 pts @ 70k

# --- STRATEGY MODE ---
STRATEGY_MODE   = "fixed"
LONGS_ONLY      = True            # blanket long-only filter (date-bias off only)
FIXED_STOP_PCT  = 0.020           # 2.0% stop (unused while FIXED_STOP_POINTS is set)
FIXED_TARGET_RR = 2.5             # reward = FIXED_TARGET_RR x risk

# ENABLE_DATE_BIAS is kept True ONLY so structure_trade() stays byte-identical to
# the backtest: with it True the LONGS_ONLY-blocks-shorts branch is dormant
# exactly as in the backtest (direction was decided by the bias windows there,
# and by the Neon bias gate here). The hardcoded DATE_BIAS_RANGES windows
# themselves are intentionally NOT ported — direction now comes from Neon bias.
ENABLE_DATE_BIAS = True

# Per-coin fixed-stop distance (points). Same values as COIN_CONFIG in the
# backtest; parameterised by symbol because this strategy serves all three.
FIXED_STOP_POINTS_BY_SYMBOL = {
    "BTCUSDT": 600.0,
    "ETHUSDT": 37.0,
    "SOLUSDT": 1.85,
}

# --- account / fee model (verbatim) ---
POSITION = 100000.0
FEE_PCT = 0.05                  # 0.05% per side
ALLOWED_RISK = 1000.0
LEVERAGE = 100

# --- trade-taken gates (verbatim) ---
IST_OFFSET_MIN = 330            # IST = UTC + 5:30
TRADE_START_H = 7              # >= 7am IST
TRADE_END_H = 18              # < 6pm IST
MAX_SETUP_TO_ENTRY_H = 2       # (kept for reference; staleness gate needs 15m, see header)
MAX_POS_AFTER_FEES = 250000.0
ENGINE_MIN_RR = 2             # require net (post-fee) R:R > this to take a trade

ONE_HOUR_MS = 3600_000
ONE_DAY_MS = 24 * ONE_HOUR_MS

DAY_OFFSET_MIN = 330
DAY_OFFSET_MS = DAY_OFFSET_MIN * 60_000


# ============================================================================
# Time helpers — copied verbatim from crt_final_stratergy.py
# ============================================================================
def utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def ist(ms):
    return utc(ms + IST_OFFSET_MIN * 60_000)


def day_bucket(ms):
    return ((ms + DAY_OFFSET_MS) // ONE_DAY_MS) * ONE_DAY_MS - DAY_OFFSET_MS


# ============================================================================
# Pattern + plan — copied verbatim from crt_final_stratergy.py
# (structure_trade only changed to take fixed_stop_points as an argument so the
#  per-coin value can be selected by symbol; the maths is unchanged.)
# ============================================================================
def detect(cands, pdh, pdl):
    out = []
    for i in range(len(cands) - 1):
        c1, c2 = cands[i], cands[i + 1]
        if (c1["c"] < c1["o"] and c2["l"] < c1["l"] and c2["c"] > c2["o"]
                and c2["h"] <= c1["h"]):
            out.append(("Bullish", c1, c2))
        if (c1["c"] > c1["o"] and c2["h"] > c1["h"] and c2["c"] < c2["o"]
                and c2["l"] >= c1["l"]):
            out.append(("Bearish", c1, c2))
    return out


def structure_trade(direction, entry, pdh, pdl, fixed_stop_points):
    """Decide the trade plan. Returns (ok, sl, tp, label, note).

    In STRATEGY_MODE == 'fixed' (the default): fixed-% / fixed-point stop,
    TP = FIXED_TARGET_RR x risk, every zone allowed, optionally longs-only.
    In 'structure' mode: PDH/PDL-anchored stops with a risk band.
    """
    # ---- FIXED-STOP MODE ----------------------------------------------------
    if STRATEGY_MODE == "fixed":
        if LONGS_ONLY and not ENABLE_DATE_BIAS and direction != "long":
            return (False, None, None, "shorts disabled",
                    "Not taken: LONGS_ONLY mode (short rejected)")
        risk = fixed_stop_points if fixed_stop_points > 0 else entry * FIXED_STOP_PCT
        if direction == "long":
            sl = entry - risk
            tp = entry + risk * FIXED_TARGET_RR
        else:
            sl = entry + risk
            tp = entry - risk * FIXED_TARGET_RR
        return (True, sl, tp, "fixed-stop", "fixed-stop")

    continuation_distance = entry * CONTINUATION_ZONE_PCT
    fade_distance = entry * FADE_MIN_DISTANCE_PCT

    sl = tp = None
    label = None

    if entry > pdh:
        gap = entry - pdh
        if gap <= continuation_distance and direction == "long":
            label = "above PDH continuation"
            sl = pdh
            risk = entry - pdh
            tp = entry + risk * TARGET_RR
        elif gap >= fade_distance and direction == "short":
            label = "above PDH fade"
            tp = pdh
            tp_dist = entry - pdh
            risk = tp_dist / TARGET_RR
            sl = entry + risk
        else:
            return (False, None, None, "above pdh",
                    "Outside PDH/PDL but neither continuation nor fade setup.")

    elif entry < pdl:
        gap = pdl - entry
        if gap <= continuation_distance and direction == "short":
            label = "below PDL continuation"
            sl = pdl
            risk = pdl - entry
            tp = entry - risk * TARGET_RR
        elif gap >= fade_distance and direction == "long":
            label = "below PDL fade"
            tp = pdl
            tp_dist = pdl - entry
            risk = tp_dist / TARGET_RR
            sl = entry - risk
        else:
            return (False, None, None, "below pdl",
                    "Outside PDH/PDL but neither continuation nor fade setup.")

    else:
        label = "inside PDH/PDL"
        if direction == "long":
            sl = pdl
            risk = entry - pdl
            tp = entry + risk * TARGET_RR
        else:
            sl = pdh
            risk = pdh - entry
            tp = entry - risk * TARGET_RR

    if risk <= 0:
        return (False, None, None, label,
                "Rejected: stop on wrong side of entry (non-positive risk)")

    risk_pct = risk / entry
    if risk_pct < MIN_RISK_PCT:
        return (False, None, None, label,
                "Rejected: stop distance below minimum structure risk")
    if risk_pct > MAX_RISK_PCT:
        return (False, None, None, label,
                "Rejected: stop distance above maximum structure risk")

    return (True, sl, tp, label, label)


# ============================================================================
# jp-risk — line-for-line port of backend/src/tools/jpRisk.js (frozen v1.0).
# The backtest got net R:R and max-position-after-fees from POST /api/tools/
# jp-risk; this reproduces the SAME numbers locally (no server call).
# ============================================================================
def calc_jp_risk(position, direction, entry, sl, tp, leverage, fee, allowed_risk=1000.0):
    pos = float(position)
    e = float(entry)
    s = float(sl)
    t = float(tp)
    lev = float(leverage)
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

    effective_entry = e * (1 + fee_pct) if direction == "long" else e * (1 - fee_pct)
    coins = pos / e

    sl_distance = abs(e - s)
    tp_distance = abs(t - e) if t else 0.0

    raw_loss = sl_distance * coins
    raw_profit = tp_distance * coins
    raw_rr = raw_profit / raw_loss if raw_profit > 0 else 0.0

    entry_fee = pos * fee_pct
    exit_fee_sl = coins * s * fee_pct
    exit_fee_tp = coins * t * fee_pct if t else 0.0

    loss_after_fees = raw_loss + entry_fee + exit_fee_sl
    profit_after_fees = raw_profit - entry_fee - exit_fee_tp
    rr_after_fees = profit_after_fees / loss_after_fees if profit_after_fees > 0 else 0.0

    allowed = float(allowed_risk)
    margin = pos / lev

    liquidation_price = (effective_entry * (1 - 1 / lev) if direction == "long"
                         else effective_entry * (1 + 1 / lev))

    epsilon = 0.01
    reject_rr = rr_after_fees < 1 - 1e-6
    reject_risk = loss_after_fees > allowed + epsilon
    reject_direction = direction_invalid
    reject = reject_rr or reject_risk or reject_direction

    max_position_raw = math.floor((allowed / raw_loss) * pos) if raw_loss else pos
    max_position_after_fees = (math.floor((allowed / loss_after_fees) * pos)
                               if loss_after_fees else pos)

    return {
        "liquidationPrice": liquidation_price,
        "coins": coins,
        "slDistance": sl_distance,
        "tpDistance": tp_distance,
        "rawLoss": raw_loss,
        "rawProfit": raw_profit,
        "rawRR": raw_rr,
        "lossAfterFees": loss_after_fees,
        "profitAfterFees": profit_after_fees,
        "rrAfterFees": rr_after_fees,
        "margin": margin,
        "reject": reject,
        "rejectRR": reject_rr,
        "rejectRisk": reject_risk,
        "rejectDirection": reject_direction,
        "maxPositionRaw": max_position_raw,
        "maxPositionAfterFees": max_position_after_fees,
        "lev": lev,
    }


# ============================================================================
# Live-wrapping helpers (the ONLY genuinely new code — reading live candles).
# ============================================================================
def _field(c, key):
    """Read a field from a candle row that may be a dict or an asyncpg Record."""
    try:
        return c[key]
    except (TypeError, KeyError, IndexError):
        return getattr(c, key)


def _norm(c):
    """Normalise a live candle row into the backtest's internal {t,o,h,l,c} dict.

    NUMERIC columns come back as Decimal from Neon; cast to float exactly as the
    backtest's load() did. open_time is epoch-ms (BIGINT), matching the backtest's
    `t`. We use OPEN time for the IST-day bucket, identical to the backtest."""
    return {
        "t": int(_field(c, "open_time")),
        "o": float(_field(c, "open")),
        "h": float(_field(c, "high")),
        "l": float(_field(c, "low")),
        "c": float(_field(c, "close")),
    }


def _ctx_get(context, key):
    """Read context[key] (context may be a dict or object). Tolerates the value
    being a plain string OR a row/dict carrying a 'direction'/'trend' sub-field."""
    v = context.get(key) if isinstance(context, dict) else getattr(context, key, None)
    if isinstance(v, dict):
        v = v.get("direction") or v.get("trend")
    elif v is not None and not isinstance(v, str):
        v = getattr(v, "direction", getattr(v, "trend", v))
    return v


def _norm_dir(raw):
    """Normalise a direction-ish value to 'BULLISH'/'BEARISH'/'NEUTRAL' (or None).
    Accepts either vocabulary (bull/bear or bullish/bearish)."""
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


async def fetch_node_bias(symbol):
    """GATE 2 source: fetch the Node backend's computed bias for `symbol` LIVE via
    GET /api/bias/<method> and return its signal normalised to
    'BULLISH'/'BEARISH'/'NEUTRAL'. Retries up to NODE_BIAS_RETRIES times; returns
    None if every attempt fails (the caller treats None as a rejected trade, exactly
    like the backtest's unreachable-server -> 'N/A' -> blocked behaviour)."""
    url = f"{NODE_BACKEND_URL}/api/bias/{BIAS_METHOD}"
    params = {"symbol": symbol}
    last_err = None
    for attempt in range(1, NODE_BIAS_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=NODE_BIAS_TIMEOUT) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            return _norm_dir((data.get("result") or {}).get("signal"))
        except Exception as exc:                       # noqa: BLE001
            last_err = exc
            if attempt < NODE_BIAS_RETRIES:
                await asyncio.sleep(NODE_BIAS_RETRY_DELAY)
    print(f"[crt] node bias fetch failed for {symbol} after "
          f"{NODE_BIAS_RETRIES} attempts: {last_err!r}")
    return None


def _prev_day_hl(cs, cur_day):
    """Previous IST-day high/low aggregated from the lookback candles — the
    backtest's prev = daily.get(d - ONE_DAY_MS). Returns (pdh, pdl) or None if
    the previous day is not present in the window."""
    prev_bucket = cur_day - ONE_DAY_MS
    highs, lows = [], []
    for x in cs:
        if day_bucket(x["t"]) == prev_bucket:
            highs.append(x["h"])
            lows.append(x["l"])
    if not highs:
        return None
    return max(highs), min(lows)


# ============================================================================
# The drop-in strategy
# ============================================================================
class CRTStrategy(Strategy):
    name = "crt"
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    interval = "1h"
    # Enough closed 1h candles to always cover the full previous IST day plus the
    # current day up to the candle that just closed (max 24 + 24 = 48); 72 keeps a
    # safe margin so PDH/PDL is always complete.
    lookback = 72

    async def on_candle(self, symbol, candles, context) -> Signal | None:
        fixed_stop_points = FIXED_STOP_POINTS_BY_SYMBOL.get(symbol)
        if fixed_stop_points is None:
            return None

        # --- normalise the CLOSED-candle lookback window (oldest..newest) -----
        cs = sorted((_norm(c) for c in candles), key=lambda x: x["t"])
        if len(cs) < 2:
            return None

        # The two most recent CLOSED candles ARE the backtest's (c1, c2) pair.
        c1, c2 = cs[-2], cs[-1]

        # The backtest only pairs candles WITHIN the same IST day (detect runs over
        # by_day[d]); it never pairs the first candle of a day with the previous
        # day's last candle. Reproduce that boundary exactly.
        cur_day = day_bucket(c2["t"])
        if day_bucket(c1["t"]) != cur_day:
            return None

        # Previous IST-day H/L (PDH/PDL) — backtest requires the prev day to exist.
        prev = _prev_day_hl(cs, cur_day)
        if prev is None:
            return None
        pdh, pdl = prev

        # --- detect() verbatim on the newest pair ----------------------------
        setups = detect([c1, c2], pdh, pdl)
        if not setups:
            return None
        kind, dc1, dc2 = setups[0]          # at most one (bull/bear are exclusive)
        direction = "long" if kind == "Bullish" else "short"
        entry = c2["o"]

        # --- DUAL bias gate — both gates, exactly as the backtest run_engine() ---
        # GATE 1 — DATE-RANGE BIAS in the backtest (date_bias windows), now the
        #   MANUAL trend from Neon's `trend` table (dashboard). EXACT match:
        #   long <- BULLISH ("bull"), short <- BEARISH ("bear"); NEUTRAL/unset skips.
        manual_trend = _norm_dir(_ctx_get(context, "trend"))
        want = "BULLISH" if direction == "long" else "BEARISH"
        if manual_trend != want:
            return None

        # GATE 2 — SERVER DAILY BIAS in the backtest (computed dailyClose). Now that
        #   a setup has been FOUND and passed GATE 1, fetch the Node backend's bias
        #   LIVE (with retries) and apply AGREE-OR-NEUTRAL: long needs BULLISH or
        #   NEUTRAL, short needs BEARISH or NEUTRAL. Anything else skips — INCLUDING a
        #   failed/None fetch, mirroring the backtest where a missing/'N/A' server
        #   bias is not in the allowed set and blocks the trade.
        node_bias = await fetch_node_bias(symbol)
        agree_ok = {"long": ("BULLISH", "NEUTRAL"),
                    "short": ("BEARISH", "NEUTRAL")}[direction]
        if node_bias not in agree_ok:
            return None

        # cosmetic with/counter-trend label from the node bias (backtest 'trend' col).
        if node_bias == "BULLISH":
            trend_label = "with trend" if direction == "long" else "counter trend"
        elif node_bias == "BEARISH":
            trend_label = "with trend" if direction == "short" else "counter trend"
        else:
            trend_label = "neutral"

        # --- structure_trade() verbatim --------------------------------------
        ok, sl, tp, zone_label, zone_note = structure_trade(
            direction, entry, pdh, pdl, fixed_stop_points)
        if not ok:
            return None

        # --- jp-risk maths verbatim ------------------------------------------
        jp = calc_jp_risk(POSITION, direction, entry, sl, tp, LEVERAGE, FEE_PCT,
                          ALLOWED_RISK)
        if jp is None:
            return None
        net_rr = round(jp["rrAfterFees"], 3)

        # --- gates decidable AT candle close (backtest blockers we can keep) ---
        # setup_found = c2 close time = c2 open + 1h, exactly as the backtest.
        setup_found = c2["t"] + ONE_HOUR_MS
        sh = ist(setup_found).hour
        if not (TRADE_START_H <= sh < TRADE_END_H):
            return None
        if jp["maxPositionAfterFees"] > MAX_POS_AFTER_FEES:
            return None
        if net_rr <= ENGINE_MIN_RR:
            return None

        # (The backtest's remaining blockers — entry-fill existence, entry-time
        #  window, and <=2h staleness — depend on the 15m fill and so belong to
        #  execution, not detection. See the module header, point 4.)

        reason = (
            f"CRT {direction} ({zone_label}) vs prev-day H/L "
            f"{pdh:.4f}/{pdl:.4f}; entry {entry:.4f}, SL {sl:.4f}, TP {tp:.4f} "
            f"(fixed {fixed_stop_points} pts, {FIXED_TARGET_RR}R); "
            f"net R:R {net_rr}; trend(manual) {manual_trend}, bias(node) "
            f"{node_bias or 'n/a'} ({trend_label}); setup candle closed "
            f"{ist(setup_found):%Y-%m-%d %H:%M} IST"
        )

        return Signal(
            strategy=self.name,
            symbol=symbol,
            side=direction,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            reason=reason,
        )
