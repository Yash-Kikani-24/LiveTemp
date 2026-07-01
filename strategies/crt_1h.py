"""
strategies/crt_1h.py — live CRT strategy on 1H timeframe (fixed-stop mode).

Detection: 2-candle CRT pattern on 1H candles.
  Bullish: C1 bearish, C2 sweeps below C1 low, C2 closes bullish inside C1 range.
  Bearish: C1 bullish, C2 sweeps above C1 high, C2 closes bearish inside C1 range.

Trade plan (fixed-stop):
  entry      = C2 open
  SL         = entry ∓ fixed_stop_points  (per-coin)
  TP         = entry ± fixed_stop_points × target_rr
  target_rr  = from context (dashboard), default 2.5

Every valid setup fires a signal regardless of trend alignment. The manual trend
and node bias are reported in the reason string so you can see at a glance in
Telegram whether the setup is with-trend, counter-trend, or no-trend-set.

Hard gates (still block the signal):
  * jp-risk: net R:R after fees > ENGINE_MIN_RR; max position ≤ MAX_POS_AFTER_FEES.
  * IST time window: setup candle must close between 07:00 and 18:00 IST.
"""

from __future__ import annotations

from .base import Signal, Strategy
from ._shared import (
    ONE_HOUR_MS,
    POSITION, FEE_PCT, ALLOWED_RISK, LEVERAGE,
    ENGINE_MIN_RR, MAX_POS_AFTER_FEES, DEFAULT_TARGET_RR,
    ist, _norm, _ctx_get, _norm_dir, calc_jp_risk,
)

# ============================================================================
# CRT-1H-specific constants
# ============================================================================

FIXED_STOP_POINTS_BY_SYMBOL = {
    "BTCUSDT": 600.0,
    "ETHUSDT": 37.0,
    "SOLUSDT": 1.85,
}

FIXED_TARGET_RR = DEFAULT_TARGET_RR


# ============================================================================
# Pattern detection
# ============================================================================

def detect(cands):
    """2-candle CRT pattern. Returns list of ('Bullish'|'Bearish', c1, c2)."""
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


# ============================================================================
# The drop-in strategy
# ============================================================================

class CRT1HStrategy(Strategy):
    name     = "crt_1h"
    symbols  = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    interval = "1h"
    lookback = 6

    async def on_candle(self, symbol, candles, context) -> Signal | None:
        fixed_stop_points = FIXED_STOP_POINTS_BY_SYMBOL.get(symbol)
        if fixed_stop_points is None:
            return None

        # --- normalise candles oldest → newest ------------------------------
        cs = sorted((_norm(c) for c in candles), key=lambda x: x["t"])
        if len(cs) < 2:
            return None
        c1, c2 = cs[-2], cs[-1]

        # --- pattern detection ----------------------------------------------
        setups = detect([c1, c2])
        if not setups:
            return None
        kind      = setups[0][0]
        direction = "long" if kind == "Bullish" else "short"
        entry     = c2["o"]

        # --- manual trend alignment (informational — does not block) ---------
        manual_trend = _norm_dir(_ctx_get(context, "trend"))
        want = "BULLISH" if direction == "long" else "BEARISH"
        if manual_trend == want:
            trend_alignment = "WITH-TREND"
        elif manual_trend in ("BULLISH", "BEARISH"):
            trend_alignment = "COUNTER-TREND"
        else:
            trend_alignment = "NO-TREND-SET"

        # --- R:R from context (dashboard) or fallback -----------------------
        ctx_rr    = context.get("rr") if isinstance(context, dict) else getattr(context, "rr", None)
        target_rr = float(ctx_rr) if ctx_rr is not None else FIXED_TARGET_RR

        # --- fixed-stop trade plan ------------------------------------------
        risk = fixed_stop_points
        if direction == "long":
            sl = entry - risk
            tp = entry + risk * target_rr
        else:
            sl = entry + risk
            tp = entry - risk * target_rr

        # --- jp-risk gates (hard — these still block) -----------------------
        jp = calc_jp_risk(POSITION, direction, entry, sl, tp, LEVERAGE, FEE_PCT,
                          ALLOWED_RISK)
        if jp is None:
            return None
        net_rr = round(jp["rrAfterFees"], 3)

        # setup available after C2 closes (1H after C2 open)
        setup_found = c2["t"] + ONE_HOUR_MS
        if not (7 <= ist(setup_found).hour < 18):
            return None
        if jp["maxPositionAfterFees"] > MAX_POS_AFTER_FEES:
            return None
        if net_rr <= ENGINE_MIN_RR:
            return None

        # --- reason string: trend indicator only (with-trend vs not) --------
        reason = "✅ With Trend" if trend_alignment == "WITH-TREND" else "❌ Not With Trend"

        return Signal(
            strategy=self.name,
            symbol=symbol,
            side=direction,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            reason=reason,
        )
