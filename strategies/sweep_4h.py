"""
strategies/sweep_4h.py — live port of the 4H Sweep Swing strategy.

Detection (per consecutive 4H candle pair C1, C2):
  Bullish sweep (LONG):  C2.low  < C1.low  AND C2.close > C1.low
  Bearish sweep (SHORT): C2.high > C1.high AND C2.close < C1.high

Both directions are always checked independently. Every valid setup fires a
signal regardless of trend alignment. The manual trend is reported in the
reason string so you can see in Telegram whether the setup is with-trend,
counter-trend, or no-trend-set.

If both a long and short sweep fire on the same candle pair (outside bar), the
direction that matches the manual trend is preferred; if no trend is set, long
is taken.

Hard gates (still block the signal):
  * MIN_STOP: risk (entry→SL) must meet the per-coin minimum.
  * jp-risk: net R:R after fees > ENGINE_MIN_RR; max position ≤ MAX_POS_AFTER_FEES.
"""

from __future__ import annotations

from .base import Signal, Strategy
from ._shared import (
    POSITION, FEE_PCT, ALLOWED_RISK, LEVERAGE,
    ENGINE_MIN_RR, MAX_POS_AFTER_FEES, DEFAULT_TARGET_RR,
    ONE_HOUR_MS,
    ist, _norm, _ctx_get, _norm_dir, calc_jp_risk,
)

FOUR_HOUR_MS = 4 * ONE_HOUR_MS

MIN_STOP_POINTS_BY_SYMBOL = {
    "BTCUSDT": 300.0,
    "ETHUSDT": 16.0,
    "SOLUSDT": 1.6,
}


class Sweep4HStrategy(Strategy):
    name     = "sweep_4h"
    symbols  = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    interval = "4h"
    lookback = 6

    def on_candle(self, symbol, candles, context) -> Signal | None:
        min_stop = MIN_STOP_POINTS_BY_SYMBOL.get(symbol)
        if min_stop is None:
            return None

        # --- normalise candles oldest → newest ------------------------------
        cs = sorted((_norm(c) for c in candles), key=lambda x: x["t"])
        if len(cs) < 2:
            return None
        c1, c2 = cs[-2], cs[-1]

        # --- detect both sweep directions independently ----------------------
        long_fired  = c2["l"] < c1["l"] and c2["c"] > c1["l"]
        short_fired = c2["h"] > c1["h"] and c2["c"] < c1["h"]

        if not long_fired and not short_fired:
            return None

        # --- manual trend (informational — does not block) ------------------
        manual_trend = _norm_dir(_ctx_get(context, "trend"))

        # pick direction: prefer trend-matching; if both fire and no trend, prefer long
        if long_fired and short_fired:
            direction = "long" if manual_trend == "BULLISH" else "short"
        elif long_fired:
            direction = "long"
        else:
            direction = "short"

        # alignment label
        want = "BULLISH" if direction == "long" else "BEARISH"
        if manual_trend == want:
            trend_alignment = "WITH-TREND"
        elif manual_trend in ("BULLISH", "BEARISH"):
            trend_alignment = "COUNTER-TREND"
        else:
            trend_alignment = "NO-TREND-SET"

        # --- trade plan -----------------------------------------------------
        if direction == "long":
            entry = c2["c"]
            sl    = c2["l"]
        else:
            entry = c2["c"]
            sl    = c2["h"]

        risk = abs(entry - sl)

        # --- MIN_STOP gate (hard — keeps noise trades out) ------------------
        if risk < min_stop:
            return None

        # --- R:R from context (dashboard) or fallback -----------------------
        ctx_rr    = context.get("rr") if isinstance(context, dict) else getattr(context, "rr", None)
        target_rr = float(ctx_rr) if ctx_rr is not None else DEFAULT_TARGET_RR

        tp = (entry + risk * target_rr) if direction == "long" else (entry - risk * target_rr)

        # --- jp-risk gates (hard — these still block) -----------------------
        jp = calc_jp_risk(POSITION, direction, entry, sl, tp, LEVERAGE, FEE_PCT,
                          ALLOWED_RISK)
        if jp is None:
            return None
        net_rr = round(jp["rrAfterFees"], 3)
        if jp["maxPositionAfterFees"] > MAX_POS_AFTER_FEES:
            return None
        if net_rr <= ENGINE_MIN_RR:
            return None

        # --- reason string --------------------------------------------------
        sweep_detail = (
            f"C2L {c2['l']:.4f} < C1L {c1['l']:.4f}" if direction == "long"
            else f"C2H {c2['h']:.4f} > C1H {c1['h']:.4f}"
        )
        reason = (
            f"{trend_alignment}  ·  manual: {manual_trend or 'none'}  →  setup: {direction}\n"
            f"Sweep  :  {sweep_detail}  ·  Net R:R  :  {net_rr}\n"
            f"Risk   :  {risk:.4f} pts  ·  {target_rr}R  ·  {ist(c2['t']):%d %b %Y  %H:%M} IST"
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
