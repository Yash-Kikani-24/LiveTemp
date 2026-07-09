"""
strategies/sweep_1h.py — live port of the 1H Sweep Swing strategy
(backtest: filterscript_1hr.py).

Detection (per consecutive 1H candle pair C1, C2), entry always at C2 open:
  LONG :  C1 and C2 both GREEN, C2.low  < C1.low  (sweep),
          C2.open >= C1.close - OPEN_CLOSE_TOLERANCE
  SHORT:  C1 and C2 both RED,   C2.high > C1.high (sweep),
          C2.open <= C1.close + OPEN_CLOSE_TOLERANCE

Unlike sweep_4h there is NO "C2 close beyond C1 extreme" requirement — the 1H
backtest only checks colour, sweep and the open-vs-close continuity (with a
small tolerance, since 24/7 crypto opens can differ from the prior close by
pennies).

Both directions are checked every candle (they are mutually exclusive by
colour). Every valid setup fires regardless of trend alignment; the manual
trend is only reported in the reason string.

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
    _norm,
    _ctx_get,
    _norm_dir,
    trend_state,
    calc_jp_risk,
    fmt_setup_candles,
    fmt_trend,
)

# Per-coin minimum stop distance (in points) — backtest SETTINGS (4b).
MIN_STOP_POINTS_BY_SYMBOL = {
    "BTCUSDT": 300.0,
    "ETHUSDT": 16.0,
    "SOLUSDT": 1.5,
}

# Tolerance (price points) for the "C2 open vs C1 close" check — backtest (6).
OPEN_CLOSE_TOLERANCE = 1.0


class Sweep1HStrategy(Strategy):
    name = "sweep_1h"
    label = "1H Sweep"
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    interval = "1h"
    lookback = 6

    def on_candle(self, symbol, candles, context) -> "Signal | None":
        min_stop = MIN_STOP_POINTS_BY_SYMBOL.get(symbol)
        if min_stop is None:
            return None

        # Normalize + sort candles oldest..newest by open time.
        cs = sorted((_norm(c) for c in candles), key=lambda x: x["t"])

        # Keep only fully-closed candles (open time + 1h must be in the past).
        now_ms = time.time() * 1000
        closed = [c for c in cs if c["t"] + ONE_HOUR_MS <= now_ms]
        if len(closed) < 2:
            return None
        c1, c2 = closed[-2], closed[-1]

        # Candle colors.
        c1_green = c1["c"] > c1["o"]
        c2_green = c2["c"] > c2["o"]
        c1_red = c1["c"] < c1["o"]
        c2_red = c2["c"] < c2["o"]

        # Sweep detection (long/short are mutually exclusive by colour).
        long_fired = (
            c1_green
            and c2_green
            and c2["l"] < c1["l"]
            and c2["o"] >= c1["c"] - OPEN_CLOSE_TOLERANCE
        )
        short_fired = (
            c1_red
            and c2_red
            and c2["h"] > c1["h"]
            and c2["o"] <= c1["c"] + OPEN_CLOSE_TOLERANCE
        )

        if not (long_fired or short_fired):
            return None
        direction = "long" if long_fired else "short"

        # Dedupe: fire at most once per C2 candle, per symbol.
        fired = getattr(self, "_fired_c2", None)
        if fired is None:
            fired = self._fired_c2 = {}
        if fired.get(symbol) == c2["t"]:
            return None

        manual_trend = _norm_dir(_ctx_get(context, "trend"))
        want = "BULLISH" if direction == "long" else "BEARISH"
        trend_alignment = trend_state(manual_trend, want)

        # Entry / stop-loss (entry always at C2 open).
        entry = c2["o"]
        sl = c2["l"] if direction == "long" else c2["h"]

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
