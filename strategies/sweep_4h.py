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

import time

from .base import Signal, Strategy
from ._shared import (
    POSITION,
    FEE_PCT,
    ALLOWED_RISK,
    LEVERAGE,
    ENGINE_MIN_RR,
    MAX_POS_AFTER_FEES,
    DEFAULT_TARGET_RR,
    ONE_HOUR_MS,
    ist,
    _norm,
    _ctx_get,
    _norm_dir,
    calc_jp_risk,
    fmt_setup_candles,
    fmt_trend,
)

FOUR_HOUR_MS = 4 * ONE_HOUR_MS

# Per-coin minimum stop distance (in points).
MIN_STOP_POINTS_BY_SYMBOL = {
    "BTCUSDT": 300.0,
    "ETHUSDT": 16.0,
    "SOLUSDT": 1.6,
}


class Sweep4HStrategy(Strategy):
    name = "sweep_4h"
    label = "4H Sweep"
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    interval = "4h"
    lookback = 6

    def on_candle(self, symbol, candles, context) -> "Signal | None":
        min_stop = MIN_STOP_POINTS_BY_SYMBOL.get(symbol)
        if min_stop is None:
            return None

        # Normalize + sort candles oldest..newest by open time.
        cs = sorted((_norm(c) for c in candles), key=lambda x: x["t"])

        # Keep only fully-closed candles (open time + 4h must be in the past).
        now_ms = time.time() * 1000
        closed = [c for c in cs if c["t"] + FOUR_HOUR_MS <= now_ms]
        if len(closed) < 2:
            return None
        c1, c2 = closed[-2], closed[-1]

        # Candle colors.
        c1_green = c1["c"] > c1["o"]
        c2_green = c2["c"] > c2["o"]
        c1_red = c1["c"] < c1["o"]
        c2_red = c2["c"] < c2["o"]

        # Sweep detection.
        long_fired = (
            c1_green
            and c2_green
            and c2["l"] < c1["l"]
            and c2["o"] >= c1["c"]
            and c2["c"] > c1["h"]
        )
        short_fired = (
            c1_red
            and c2_red
            and c2["h"] > c1["h"]
            and c2["o"] <= c1["c"]
            and c2["c"] < c1["l"]
        )

        if not (long_fired or short_fired):
            return None

        # Dedupe: fire at most once per C2 candle, per symbol.
        fired = getattr(self, "_fired_c2", None)
        if fired is None:
            fired = self._fired_c2 = {}
        if fired.get(symbol) == c2["t"]:
            return None

        manual_trend = _norm_dir(_ctx_get(context, "trend"))

        # Resolve direction (prefer trend-aligned on an outside bar).
        if long_fired and short_fired:
            direction = "long" if manual_trend == "BULLISH" else "short"
        elif long_fired:
            direction = "long"
        else:
            direction = "short"

        want = "BULLISH" if direction == "long" else "BEARISH"
        if manual_trend == want:
            trend_alignment = "WITH-TREND"
        elif manual_trend in ("BULLISH", "BEARISH"):
            trend_alignment = "COUNTER-TREND"
        else:
            trend_alignment = "NO-TREND-SET"

        # Entry / stop-loss.
        if direction == "long":
            entry = c2["o"]
            sl = c2["l"]
        else:
            entry = c2["o"]
            sl = c2["h"]

        risk = abs(entry - sl)

        # Min-stop gate.
        if risk < min_stop:
            return None

        ctx_rr = context.get("rr") if isinstance(context, dict) else getattr(context, "rr", None)
        target_rr = float(ctx_rr) if ctx_rr is not None else DEFAULT_TARGET_RR

        tp = entry + risk * target_rr if direction == "long" else entry - risk * target_rr

        jp = calc_jp_risk(POSITION, direction, entry, sl, tp, LEVERAGE, FEE_PCT,
                          ALLOWED_RISK)
        if jp is None:
            return None
        net_rr = round(jp["rrAfterFees"], 3)
        if jp["maxPositionAfterFees"] > MAX_POS_AFTER_FEES:
            return None
        if net_rr <= ENGINE_MIN_RR:
            return None

        # Reason: trend indicator (line 0) + C1/C2 setup-candle times below it.
        reason = f"{fmt_trend(trend_alignment)}\n{fmt_setup_candles(c1['t'], c2['t'])}"

        # Record the fired C2 and return the signal.
        fired[symbol] = c2["t"]
        return Signal(
            strategy=self.name,
            symbol=symbol,
            side=direction,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            reason=reason,
        )
