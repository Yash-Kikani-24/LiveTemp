"""
strategies/rsi_4h.py — live port of the RSI(14) + EMA(45)/SMA(9)-on-RSI
crossover strategy (backtest/RSI.py) on the 4H timeframe.

Detection (evaluated on each CLOSED 4H candle):
  1. RSI(14) (Wilder) on close.
  2. EMA(45) and SMA(9) computed ON THE RSI LINE.
  3. diff = rsi_sma - rsi_ema
       - diff crosses DOWN->UP  (<=0 then >0)  -> LONG
       - diff crosses UP->DOWN  (>=0 then <0)  -> SHORT

Trade plan (adaptive ATR stop, fixed 1:6 RR — same as the backtest):
  entry     = close of the just-closed (signal) candle
  sl_dist   = ATR_MULT × ATR(14), clamped to [SL_MIN_PCT, SL_MAX_PCT] of entry
  SL        = entry ∓ sl_dist
  TP        = entry ± sl_dist × RR_TARGET      (RR_TARGET = 6)

The backtest's trade-management rules (opposite-signal early exit, max-hold time
exit, one-trade-at-a-time) are NOT ported: the live engine only ALERTS on entry
setups — position management happens downstream. So this file emits one Signal
per fresh crossover, exactly like the other drop-in strategies.

Every valid setup fires regardless of trend alignment; the manual trend is only
reported in the reason string.

Hard gates (still block the signal):
  * jp-risk: net R:R after fees > RSI_MIN_NET_RR; max position ≤ MAX_POS_AFTER_FEES.

Indicators are reimplemented in pure Python (the live app has no pandas/numpy)
and mirror the pandas semantics used in backtest/RSI.py line-for-line.
"""

from __future__ import annotations

from .base import Signal, Strategy
from ._shared import (
    POSITION, FEE_PCT, ALLOWED_RISK, LEVERAGE,
    MAX_POS_AFTER_FEES,
    _norm, _ctx_get, _norm_dir, calc_jp_risk,
    fmt_indicators, fmt_setup_time, fmt_trend, trend_state,
)

# ============================================================================
# RSI-specific constants (mirror backtest/RSI.py)
# ============================================================================

RSI_PERIOD    = 14
EMA_PERIOD    = 45          # EMA span, computed on the RSI line
SMA_PERIOD    = 9           # SMA window, computed on the RSI line
ATR_PERIOD    = 14          # ATR lookback (Wilder)
ATR_MULT      = 1.0         # stop distance = ATR_MULT × ATR
SL_MIN_PCT    = 0.01        # stop floor: never tighter than 1.0% of entry
SL_MAX_PCT    = 0.03        # stop ceiling: never wider than 3.0% of entry
RR_TARGET     = 6           # 1:6 -> TP = RR_TARGET × stop distance (fixed, not from context)
RSI_MIN_NET_RR = 1.8        # hard gate: net RR after fees must exceed this (backtest value)


# ============================================================================
# Pure-Python indicators (mirror the pandas math in backtest/RSI.py)
# ============================================================================

def _wilder_rsi(closes, period=RSI_PERIOD):
    """Wilder RSI. Returns a list aligned with `closes`; entries are None until
    `period` deltas have been seen (== pandas min_periods=period)."""
    n = len(closes)
    rsi = [None] * n
    if n < 2:
        return rsi
    alpha = 1.0 / period
    avg_gain = avg_loss = None
    count = 0
    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        if avg_gain is None:                       # seed from first delta (adjust=False)
            avg_gain, avg_loss = gain, loss
        else:
            avg_gain = avg_gain * (1 - alpha) + gain * alpha
            avg_loss = avg_loss * (1 - alpha) + loss * alpha
        count += 1
        if count >= period:
            if avg_loss == 0:                      # no losses -> RSI 100
                rsi[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _ema(values, span):
    """EMA with adjust=False, seeded from the first non-None value; leading Nones
    stay None (== pandas ewm(span=…, adjust=False).mean() over a NaN-prefixed series)."""
    n = len(values)
    out = [None] * n
    alpha = 2.0 / (span + 1.0)
    prev = None
    for i, v in enumerate(values):
        if v is None:
            continue
        prev = v if prev is None else prev * (1 - alpha) + v * alpha
        out[i] = prev
    return out


def _sma(values, window):
    """Simple moving average; None unless the trailing `window` values are all
    non-None (== pandas rolling(window).mean())."""
    n = len(values)
    out = [None] * n
    for i in range(window - 1, n):
        w = values[i - window + 1:i + 1]
        if any(x is None for x in w):
            continue
        out[i] = sum(w) / window
    return out


def _wilder_atr(highs, lows, closes, period=ATR_PERIOD):
    """Wilder ATR (adjust=False, seeded from TR[0]); mirrors backtest TR/ATR math."""
    n = len(closes)
    if n == 0:
        return []
    alpha = 1.0 / period
    atr = [None] * n
    prev_atr = None
    for i in range(n):
        hl = highs[i] - lows[i]
        if i == 0:
            tr = hl                                # prev close unavailable -> just H-L
        else:
            tr = max(hl, abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        prev_atr = tr if prev_atr is None else prev_atr * (1 - alpha) + tr * alpha
        atr[i] = prev_atr
    return atr


# ============================================================================
# The drop-in strategy
# ============================================================================

class RSI4HStrategy(Strategy):
    name     = "rsi_4h"
    label    = "RSI 4H"
    symbols  = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    interval = "4h"
    # EMA(45)-on-RSI needs a long warm-up to converge; keep a generous buffer.
    lookback = 300

    def on_candle(self, symbol, candles, context) -> "Signal | None":
        # --- normalise candles oldest -> newest -----------------------------
        cs = sorted((_norm(c) for c in candles), key=lambda x: x["t"])
        # Need enough bars for the RSI + EMA-on-RSI warm-up plus a prior diff.
        if len(cs) < RSI_PERIOD + EMA_PERIOD + 2:
            return None

        closes = [c["c"] for c in cs]
        highs  = [c["h"] for c in cs]
        lows   = [c["l"] for c in cs]

        # --- indicators on the RSI line -------------------------------------
        rsi     = _wilder_rsi(closes, RSI_PERIOD)
        rsi_ema = _ema(rsi, EMA_PERIOD)
        rsi_sma = _sma(rsi, SMA_PERIOD)

        # diff = SMA - EMA (positive => SMA above EMA), last two closed candles.
        e_now, s_now = rsi_ema[-1], rsi_sma[-1]
        e_prev, s_prev = rsi_ema[-2], rsi_sma[-2]
        if None in (e_now, s_now, e_prev, s_prev):
            return None
        d_prev = s_prev - e_prev
        d_now  = s_now - e_now

        # --- crossover detection on the just-closed candle ------------------
        long_signal  = d_prev <= 0 and d_now > 0
        short_signal = d_prev >= 0 and d_now < 0
        if not (long_signal or short_signal):
            return None
        direction = "long" if long_signal else "short"

        # --- adaptive ATR stop + fixed 1:6 TP -------------------------------
        atr = _wilder_atr(highs, lows, closes, ATR_PERIOD)
        atr_now = atr[-1]
        if atr_now is None:
            return None
        entry = closes[-1]                          # signal candle's close
        sl_dist = ATR_MULT * atr_now
        sl_dist = min(max(sl_dist, entry * SL_MIN_PCT), entry * SL_MAX_PCT)
        if direction == "long":
            sl = entry - sl_dist
            tp = entry + sl_dist * RR_TARGET
        else:
            sl = entry + sl_dist
            tp = entry - sl_dist * RR_TARGET

        # --- jp-risk gates (hard — these block) -----------------------------
        jp = calc_jp_risk(POSITION, direction, entry, sl, tp, LEVERAGE, FEE_PCT,
                          ALLOWED_RISK)
        if jp is None:
            return None
        net_rr = round(jp["rrAfterFees"], 3)
        if jp["maxPositionAfterFees"] > MAX_POS_AFTER_FEES:
            return None
        if net_rr <= RSI_MIN_NET_RR:
            return None

        # --- manual trend alignment (informational — does not block) --------
        manual_trend = _norm_dir(_ctx_get(context, "trend"))
        want = "BULLISH" if direction == "long" else "BEARISH"
        # reason: trend indicator (line 0) + the two indicator readings that
        # produced the crossover on the signal candle — the SMA(9) and EMA(45)
        # computed ON the RSI line (0–100 scale) — plus the setup candle's time.
        # The dashboard card shows only the readings (its parser ignores the
        # setup-time line); the Telegram alert prints the setup time too, so we
        # still know exactly which candle produced the setup.
        reason = (
            f"{fmt_trend(trend_state(manual_trend, want))}\n"
            f"{fmt_indicators('SMA/EMA on RSI (signal candle)', ('SMA 9', f'{s_now:.2f}'), ('EMA 45', f'{e_now:.2f}'))}\n"
            f"{fmt_setup_time(cs[-1]['t'])}"
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
