#!/usr/bin/env python3
"""
MACD(12/26/9) crossover backtester -- BALANCED preset, 1-HOUR timeframe
=======================================================================

This is the "balanced" preset (SL 4%, RR 1:6, fresh-momentum zero-line filter)
run on 1h BTC data instead of 4h. Same engine as MACD.py / macd_max.py.

    SL            = 4% fixed
    RR            = 1:6            (TP = 6 x stop distance)
    macd-max-dist = 0.5%          (fresh-momentum zero-line filter, on)
    trend-ema     = 800           (only long above / short below the slow EMA)
    max-hold      = 672 bars      (== 28 days on 1h, so 1:6 winners can run)

The EMA800 higher-timeframe trend filter is the 1h-specific improvement found
by feature-mining the trades: taking crossovers only in the direction of the
slow trend fixed the one losing year (2021) and pushed the backtest to 7 of 7
profitable years with a higher win rate. An optional --atr-max volatility gate
is also available (off by default). See --trend-ema 0 to revert to raw signals.

NOTE ON THE TIMEFRAME
---------------------
The MACD periods (12/26/9) are bar counts, so on 1h the indicator reacts ~4x
faster (and noisier) than on 4h -- this is a genuinely different, higher-
frequency character, not just "the same strategy on more data". The max-hold
default (672 bars) is scaled to preserve the balanced preset's ~28-day wall-
clock hold; on 4h that was 168 bars. Everything is tunable via flags.

STRATEGY
--------
1. Compute the MACD indicator on close:
     macd   = EMA(fast) - EMA(slow)          (fast=12, slow=26 by default)
     signal = EMA(macd, signal_period)       (signal=9 by default)
2. Signal (evaluated on the just-CLOSED candle), with a 0-line filter:
     - MACD crosses signal DOWN->UP  AND both lines > 0  -> LONG
     - MACD crosses signal UP->DOWN  AND both lines < 0  -> SHORT
   PLUS a "fresh momentum" filter: the crossover is only taken when the MACD
   line is still within --macd-max-dist % of price from 0 (i.e. the cross
   happened soon after clearing zero, not deep in extended territory).
3. One trade at a time. New signals ignored while a trade is open
   EXCEPT an OPPOSITE signal, which closes the open trade early at
   that candle's close (= partial profit / partial loss).
4. SL is a FIXED % from entry (4% by default). TP at 1:6 RR.
   Time exit after --max-hold bars (default 672 = 28 days on 1h).
5. Risk sizing mirrors the JP/AMD calculator:
     - allowed risk per trade = 1% of capital ($1,000 on $100k)
     - position sized so loss-AFTER-FEES <= allowed risk
     - hard gates: net RR after fees > 1.8  AND  position <= 2,50,000
     - 0.05% taker fee on entry and exit
6. Intrabar: if a candle hits BOTH SL and TP, SL counts first.

CAPITAL  = $100,000
FEE      = 0.05% taker (each side)
RISK/TRD = $1,000 (1%)

USAGE
-----
    python macd_1h.py /path/to/BTC_1h.csv
    python macd_1h.py                        # auto-detects BTCUSDT_1h_all.csv
    python macd_1h.py --rr 8 --max-hold 0    # push toward "max return"
    python macd_1h.py --macd-max-dist 0      # disable the zero-line filter

OUTPUTS
-------
    trades_1h.csv    - one row per setup/trade
    summary_1h.csv   - per-year aggregate stats
"""

import argparse
import os
import sys
import numpy as np
import pandas as pd

# ----------------------------------------------------------------------
# FILE DISCOVERY  (1h flavour)
# ----------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
SYMBOL = "BTCUSDT"
DATA_DIRS = [HERE, os.path.normpath(os.path.join(HERE, "..", "yash", "backtest"))]


def _data(name):
    for d in DATA_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(HERE, name)


def find_1h_csv():
    """Auto-discover the BTC 1h CSV."""
    p = _data(f"{SYMBOL}_1h_all.csv")
    if os.path.exists(p):
        print(f"Auto-detected: {os.path.basename(p)}")
        return p
    raise SystemExit(
        f"Could not find {SYMBOL}_1h_all.csv in {DATA_DIRS}.\n"
        f"Pass the CSV path explicitly: python macd_1h.py /path/to/BTC_1h.csv"
    )

# ----------------------------------------------------------------------
# CONFIG / CONSTANTS   (BALANCED preset, 1h)
# ----------------------------------------------------------------------
MACD_FAST     = 12          # fast EMA span
MACD_SLOW     = 26          # slow EMA span
MACD_SIGNAL   = 9           # signal line = EMA(macd, this span)
SL_PCT        = 0.04        # fixed stop: 4.0% from entry
RR_TARGET     = 6.0         # 1:6 -> TP = 6 x stop distance
MAX_HOLD_BARS = 672         # force-close after 672 bars (28 days on 1h). None=off.
FEE_PCT       = 0.0005      # 0.05% taker, each side
MIN_NET_RR    = 1.8         # hard gate: net RR after fees must exceed this
MAX_POSITION  = 250_000     # hard gate: position size cap ($)
# "fresh momentum" filter: only take a crossover when the MACD line is still
# within this fraction of price from the 0 line (i.e. the cross happened soon
# after clearing zero, not deep in extended territory). None = disabled.
MACD_MAX_DIST = 0.005       # 0.5% of price (fresh-momentum crossovers only)
# HIGHER-TIMEFRAME TREND filter (the 1h improvement): only take LONGs when
# price is above a slow EMA and SHORTs when below it -- trade with the trend,
# not against it. On 1h an 800-EMA ~ the 200-EMA on 4h. This lifted the 1h
# backtest to 7/7 profitable years (it fixed the 2021 whipsaw year). None/0=off.
TREND_EMA     = 800         # slow-trend EMA span for the direction filter
# Optional volatility gate: skip entries when ATR% exceeds this. None = off.
ATR_MAX       = None        # e.g. 0.015 == only enter when ATR% < 1.5%
ATR_SPAN      = 14          # ATR EMA span (used only when ATR_MAX is set)


# ----------------------------------------------------------------------
# CSV LOADING  (auto-detects common column names)
# ----------------------------------------------------------------------
def load_csv(path):
    df = pd.read_csv(path)
    # normalise column names
    cols = {c.lower().strip(): c for c in df.columns}

    def pick(*cands):
        for cand in cands:
            if cand in cols:
                return cols[cand]
        return None

    date_c  = pick("date", "open_time", "open time", "time", "timestamp", "datetime")
    close_t = pick("close_time", "close time")
    open_c  = pick("open", "o")
    high_c  = pick("high", "h")
    low_c   = pick("low", "l")
    close_c = pick("close", "c", "close*", "adj close")

    missing = [n for n, c in
               [("date", date_c), ("open", open_c), ("high", high_c),
                ("low", low_c), ("close", close_c)] if c is None]
    if missing:
        sys.exit(f"ERROR: could not find columns {missing}. "
                 f"Found columns: {list(df.columns)}")

    out = pd.DataFrame({
        "date":  df[date_c],
        "open":  pd.to_numeric(df[open_c],  errors="coerce"),
        "high":  pd.to_numeric(df[high_c],  errors="coerce"),
        "low":   pd.to_numeric(df[low_c],   errors="coerce"),
        "close": pd.to_numeric(df[close_c], errors="coerce"),
    })
    if close_t is not None:
        out["close_time"] = df[close_t]

    out["date"] = _parse_dt(out["date"])
    if "close_time" in out.columns:
        out["close_time"] = _parse_dt(out["close_time"])
    else:
        out["close_time"] = out["date"]   # fallback = bar open time

    out = out.dropna(subset=["open", "high", "low", "close", "date"]).reset_index(drop=True)
    out = out.sort_values("date").reset_index(drop=True)
    return out


def _parse_dt(raw):
    """Parse a date column: handles epoch ms/s and tz-aware ISO strings.
    Returns tz-naive (UTC) datetimes for clean display."""
    if pd.api.types.is_numeric_dtype(raw):
        mx = float(raw.max())
        unit = "ms" if mx > 1e12 else "s"
        dt = pd.to_datetime(raw, unit=unit, utc=True)
    else:
        dt = pd.to_datetime(raw, errors="coerce", utc=True)
    # drop tz info so output reads "2020-10-01 00:00:00" not "...+00:00"
    try:
        dt = dt.dt.tz_localize(None)
    except (TypeError, AttributeError):
        pass
    return dt


_IST_OFFSET = pd.Timedelta(hours=5, minutes=30)


def _to_ist(ts):
    """Convert a tz-naive UTC Timestamp to an IST-formatted string."""
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return ""
    try:
        if pd.isna(ts):
            return ""
    except (TypeError, ValueError):
        pass
    return (pd.Timestamp(ts) + _IST_OFFSET).strftime("%Y-%m-%d %H:%M IST")


# ----------------------------------------------------------------------
# INDICATORS
# ----------------------------------------------------------------------
def add_indicators(df, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    df = df.copy()
    ema_fast     = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow     = df["close"].ewm(span=slow, adjust=False).mean()
    df["macd"]   = ema_fast - ema_slow                                # MACD line
    df["signal"] = df["macd"].ewm(span=signal, adjust=False).mean()   # signal line
    df["diff"]   = df["macd"] - df["signal"]   # MACD above signal -> positive
    return df


# ----------------------------------------------------------------------
# RISK SIZING  (JP/AMD calculator logic, after-fees)
# ----------------------------------------------------------------------
def size_position(entry, sl, tp, allowed_risk, direction):
    """
    Returns dict with sizing + net RR after fees, or None if rejected.
    Position is sized so loss-after-fees == allowed_risk, then capped.
    """
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    if sl_dist == 0:
        return None

    # per-$ of position notional: loss/profit fractions incl fees
    # coins = pos/entry ; rawLoss = sl_dist*coins = pos*sl_dist/entry
    loss_frac = sl_dist / entry
    prof_frac = tp_dist / entry
    # fees: entry fee on notional + exit fee on exit notional (~ same scale)
    entry_fee_frac = FEE_PCT
    exit_fee_sl_frac = FEE_PCT * (sl / entry)
    exit_fee_tp_frac = FEE_PCT * (tp / entry)

    loss_after_frac = loss_frac + entry_fee_frac + exit_fee_sl_frac
    prof_after_frac = prof_frac - entry_fee_frac - exit_fee_tp_frac

    # position size so that pos * loss_after_frac == allowed_risk
    pos = allowed_risk / loss_after_frac
    pos = min(pos, MAX_POSITION)

    loss_after = pos * loss_after_frac
    prof_after = pos * prof_after_frac
    net_rr = prof_after / loss_after if loss_after > 0 else 0.0

    rejected = (net_rr <= MIN_NET_RR) or (pos > MAX_POSITION + 1e-6)

    return {
        "position": pos,
        "coins": pos / entry,
        "loss_after": loss_after,
        "prof_after": prof_after,
        "net_rr": net_rr,
        "rejected": rejected,
    }


# ----------------------------------------------------------------------
# BACKTEST
# ----------------------------------------------------------------------
def backtest(df, capital, risk_pct, sl_pct, rr, start_ts=None, end_ts=None,
             macd_max_dist=MACD_MAX_DIST, max_hold_bars=MAX_HOLD_BARS,
             trend_ema=TREND_EMA, atr_max=ATR_MAX):
    """Indicators are computed on the FULL series (so the MACD warm-up stays
    correct); start_ts/end_ts only restrict which bars may OPEN a new trade.
    A trade opened inside the window is allowed to run to its SL/TP even if
    that happens after end_ts.

    trend_ema / atr_max are ENTRY-quality gates: they can block a new trade from
    OPENING, but they never interfere with exits -- an open trade is still
    closed by its SL/TP, an opposite crossover, or the max-hold timer."""
    allowed_risk = capital * risk_pct / 100.0
    equity = capital
    trades = []

    # precompute the entry-filter series (vectorised) when the gates are active
    trend = None
    if trend_ema:
        trend = df["close"].ewm(span=trend_ema, adjust=False).mean()
    atr_pct = None
    if atr_max is not None:
        hi_, lo_, cl_ = df["high"], df["low"], df["close"]
        pc = cl_.shift(1)
        tr = pd.concat([(hi_ - lo_), (hi_ - pc).abs(), (lo_ - pc).abs()],
                       axis=1).max(axis=1)
        atr_pct = tr.ewm(span=ATR_SPAN, adjust=False).mean() / df["close"]

    in_trade = False
    pos = None  # open trade dict

    # warm-up: need macd & signal converged
    start = MACD_SLOW + MACD_SIGNAL + 1

    for i in range(start, len(df)):
        prev = df.iloc[i - 1]
        cur  = df.iloc[i]

        # ---- is this bar inside the requested analysis window? ----
        cur_ts = cur["date"]
        in_window = ((start_ts is None or cur_ts >= start_ts) and
                     (end_ts is None or cur_ts <= end_ts))

        # ---- detect cross on the just-CLOSED candle (i-1 -> i) ----
        d_prev = df["diff"].iloc[i - 1]
        d_now  = df["diff"].iloc[i]
        macd_now   = df["macd"].iloc[i]
        signal_now = df["signal"].iloc[i]
        # zero-line filter: only take the trade when BOTH lines sit on the
        # correct side of the 0 threshold at the crossover candle.
        #   LONG  -> cross up   AND both lines > 0  (upper half of indicator)
        #   SHORT -> cross down AND both lines < 0  (lower half of indicator)
        long_signal  = ((d_prev <= 0) and (d_now > 0)
                        and (macd_now > 0) and (signal_now > 0))
        short_signal = ((d_prev >= 0) and (d_now < 0)
                        and (macd_now < 0) and (signal_now < 0))

        # "fresh momentum" filter: skip crossovers that fire when the MACD line
        # is already far from 0 (late / extended entries mean-revert into the SL).
        if macd_max_dist is not None:
            price = cur["close"]
            too_far = price > 0 and (abs(macd_now) / price) > macd_max_dist
            if too_far:
                long_signal = False
                short_signal = False

        # ============ MANAGE OPEN TRADE FIRST ============
        if in_trade:
            hi, lo = cur["high"], cur["low"]
            closed = False
            note = ""
            result = ""
            exit_price = None

            if pos["dir"] == "long":
                hit_sl = lo <= pos["sl"]
                hit_tp = hi >= pos["tp"]
            else:
                hit_sl = hi >= pos["sl"]
                hit_tp = lo <= pos["tp"]

            # SL takes priority if both hit
            if hit_sl:
                exit_price = pos["sl"]; result = "LOSS"; note = "SL hit"; closed = True
            elif hit_tp:
                exit_price = pos["tp"]; result = "PROFIT"; note = "TP hit"; closed = True
            # opposite signal -> early exit at current close (partial)
            elif (pos["dir"] == "long" and short_signal) or \
                 (pos["dir"] == "short" and long_signal):
                exit_price = cur["close"]; closed = True
                gross = ((exit_price - pos["entry"]) if pos["dir"] == "long"
                         else (pos["entry"] - exit_price))
                result = "PARTIAL_PROFIT" if gross > 0 else "PARTIAL_LOSS"
                note = "Opposite signal early exit"
            elif max_hold_bars is not None and (i - pos["entry_i"]) >= max_hold_bars:
                exit_price = cur["close"]; closed = True
                gross = ((exit_price - pos["entry"]) if pos["dir"] == "long"
                         else (pos["entry"] - exit_price))
                result = "PARTIAL_PROFIT" if gross > 0 else "PARTIAL_LOSS"
                note = f"Max-hold ({max_hold_bars} bars) time exit"

            if closed:
                pnl = _realized_pnl(pos, exit_price)
                equity += pnl
                trades[pos["row"]].update({
                    "Final Value": round(equity, 2),
                    "Profit/Loss": round(pnl, 2),
                    "Trade Close Time": _to_ist(cur["close_time"]),
                    "result": result,
                    "NOTE": note,
                })
                in_trade = False
                pos = None
                # fall through: an opposite signal that closed the trade
                # may also OPEN the new opposite trade this same candle.

        # ============ OPEN NEW TRADE ============
        if not in_trade and in_window and (long_signal or short_signal):
            direction = "long" if long_signal else "short"
            entry = cur["close"]

            # ---- entry-quality gates (block opening only; exits unaffected) ----
            entry_ok = True
            if trend is not None:
                tv = trend.iloc[i]
                if direction == "long"  and not (entry > tv): entry_ok = False
                if direction == "short" and not (entry < tv): entry_ok = False
            if entry_ok and atr_pct is not None:
                if not (atr_pct.iloc[i] < atr_max): entry_ok = False
            if not entry_ok:
                continue   # skip this crossover -- wrong side of trend / too volatile

            sl_dist_abs = entry * sl_pct
            if direction == "long":
                sl = entry - sl_dist_abs
                tp = entry + sl_dist_abs * rr
            else:
                sl = entry + sl_dist_abs
                tp = entry - sl_dist_abs * rr

            sizing = size_position(entry, sl, tp, allowed_risk, direction)

            row = {
                "date": cur["date"].date(),
                "long/short": direction,
                "Setup Found at time": _to_ist(prev["close_time"]),
                "trade taken": "NO",
                "Entry Price": round(entry, 2),
                "Stop loss": round(sl, 2),
                "Take Profit": round(tp, 2),
                "Time": _to_ist(cur["close_time"]),
                "Net R:R": round(sizing["net_rr"], 3) if sizing else 0,
                "Final Value": "",
                "Profit/Loss": "",
                "Trade Close Time": "",
                "trend": "",
                "NOTE": "",
                "result": "NO_TRADE",
            }

            if sizing is None or sizing["rejected"]:
                reason = ("net RR<=1.8" if sizing and sizing["net_rr"] <= MIN_NET_RR
                          else "position cap" if sizing else "invalid")
                row["NOTE"] = f"Setup skipped ({reason})"
                trades.append(row)
            else:
                row["trade taken"] = "YES"
                trades.append(row)
                pos = {
                    "dir": direction,
                    "entry": entry, "sl": sl, "tp": tp,
                    "coins": sizing["coins"],
                    "position": sizing["position"],
                    "row": len(trades) - 1,
                    "entry_i": i,
                }
                in_trade = True

    # close any trade still open at the end of data (mark-to-market on last close)
    if in_trade and pos is not None:
        last = df.iloc[-1]
        pnl = _realized_pnl(pos, last["close"])
        equity += pnl
        gross = ((last["close"] - pos["entry"]) if pos["dir"] == "long"
                 else (pos["entry"] - last["close"]))
        trades[pos["row"]].update({
            "Final Value": round(equity, 2),
            "Profit/Loss": round(pnl, 2),
            "Trade Close Time": _to_ist(last["close_time"]),
            "result": "PARTIAL_PROFIT" if gross > 0 else "PARTIAL_LOSS",
            "NOTE": "Open at data end - closed at last close",
        })

    return pd.DataFrame(trades), equity, capital


def _realized_pnl(pos, exit_price):
    """PnL in $ including entry+exit taker fees, based on coin qty."""
    coins = pos["coins"]
    if pos["dir"] == "long":
        gross = (exit_price - pos["entry"]) * coins
    else:
        gross = (pos["entry"] - exit_price) * coins
    entry_fee = pos["entry"] * coins * FEE_PCT
    exit_fee  = exit_price   * coins * FEE_PCT
    return gross - entry_fee - exit_fee


# ----------------------------------------------------------------------
# SUMMARY
# ----------------------------------------------------------------------
def build_summary(trades_df, df, risk_unit=1000.0):
    if trades_df.empty:
        return pd.DataFrame()

    taken = trades_df[trades_df["trade taken"] == "YES"].copy()
    taken["year"] = taken["Time"].str[:4].astype(int)

    # days per year present in the price data
    dft = df.copy()
    dft["year"] = pd.to_datetime(dft["date"]).dt.year
    days_per_year = dft.groupby("year")["date"].apply(
        lambda s: pd.to_datetime(s).dt.date.nunique())

    rows = []
    for year, g in taken.groupby("year"):
        prof   = (g["result"] == "PROFIT").sum()
        loss   = (g["result"] == "LOSS").sum()
        pprof  = (g["result"] == "PARTIAL_PROFIT").sum()
        ploss  = (g["result"] == "PARTIAL_LOSS").sum()
        total  = len(g)
        wins   = prof + pprof
        net_pl = pd.to_numeric(g["Profit/Loss"], errors="coerce").sum()
        win_rate = (wins / total * 100) if total else 0.0
        # net_R = sum of R-multiples
        pl = pd.to_numeric(g["Profit/Loss"], errors="coerce")
        net_R = (pl / risk_unit).sum()

        rows.append({
            "year": int(year),
            "total_days": int(days_per_year.get(year, 0)),
            "total_setups": total,
            "total_no_of_profit_setups": int(prof),
            "total_no_of_loss_setups": int(loss),
            "total_no_of_partialprofit_setups": int(pprof),
            "total_no_of_partialloss_setups": int(ploss),
            "net_R": round(net_R, 3),
            "win_rate": round(win_rate, 2),
            "net_PL_$": round(net_pl, 2),
        })
    return pd.DataFrame(rows).sort_values("year").reset_index(drop=True)


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="MACD crossover backtester -- BALANCED preset on 1h data "
                    "(SL 4%, RR 1:6, 0.5% zero-line filter, 672-bar/28-day hold)")
    ap.add_argument("csv", nargs="?", default=None,
                    help="path to BTC 1h OHLC csv (auto-detected if omitted)")
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--sl-pct", type=float, default=SL_PCT * 100,
                    help="fixed stop-loss as %% of entry (default 4.0)")
    ap.add_argument("--rr", type=float, default=RR_TARGET,
                    help="risk:reward target (default 6.0)")
    ap.add_argument("--fast", type=int, default=MACD_FAST,
                    help="MACD fast EMA span (default 12)")
    ap.add_argument("--slow", type=int, default=MACD_SLOW,
                    help="MACD slow EMA span (default 26)")
    ap.add_argument("--signal", type=int, default=MACD_SIGNAL,
                    help="MACD signal EMA span (default 9)")
    ap.add_argument("--macd-max-dist", type=float,
                    default=(MACD_MAX_DIST * 100 if MACD_MAX_DIST else None),
                    help="fresh-momentum filter: only take crossovers where the "
                         "MACD line is within this %% of price from 0 "
                         f"(default {MACD_MAX_DIST*100 if MACD_MAX_DIST else 'off'}). "
                         "Pass 0 to disable.")
    ap.add_argument("--max-hold", type=int, default=MAX_HOLD_BARS,
                    help=f"force-close a trade after this many bars "
                         f"(default {MAX_HOLD_BARS} = 28 days on 1h; "
                         f"0 or negative = no time exit)")
    ap.add_argument("--trend-ema", type=int, default=TREND_EMA,
                    help=f"higher-timeframe trend filter: only long above this "
                         f"EMA, only short below it (default {TREND_EMA}; "
                         f"0 = disable)")
    ap.add_argument("--atr-max", type=float,
                    default=(ATR_MAX * 100 if ATR_MAX else None),
                    help="optional volatility gate: only enter when ATR%% is "
                         "below this %% of price (e.g. 1.5). Omit/0 to disable.")
    ap.add_argument("--start", default=None,
                    help="analyse trades from this date (UTC, e.g. 2021-01-01)")
    ap.add_argument("--end", default=None,
                    help="analyse trades up to this date (UTC, inclusive, e.g. 2021-12-31)")
    ap.add_argument("--out-trades", default="trades_1h.csv")
    ap.add_argument("--out-summary", default="summary_1h.csv")
    args = ap.parse_args()

    # parse the analysis window (data is UTC). A bare date for --end is made
    # inclusive of the whole day.
    start_ts = pd.to_datetime(args.start) if args.start else None
    end_ts   = pd.to_datetime(args.end)   if args.end   else None
    if end_ts is not None and end_ts == end_ts.normalize():
        end_ts = end_ts + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        sys.exit(f"ERROR: --start ({args.start}) is after --end ({args.end}).")

    sl_pct = args.sl_pct / 100.0

    csv_path = args.csv if args.csv else find_1h_csv()
    df = load_csv(csv_path)
    df = add_indicators(df, args.fast, args.slow, args.signal)

    macd_max_dist = (args.macd_max_dist / 100.0) if args.macd_max_dist else None
    max_hold = args.max_hold if args.max_hold and args.max_hold > 0 else None
    trend_ema = args.trend_ema if args.trend_ema and args.trend_ema > 0 else None
    atr_max = (args.atr_max / 100.0) if args.atr_max else None
    trades_df, final_equity, capital = backtest(
        df, args.capital, args.risk_pct, sl_pct, args.rr, start_ts, end_ts,
        macd_max_dist=macd_max_dist, max_hold_bars=max_hold,
        trend_ema=trend_ema, atr_max=atr_max)

    # final output column order (drop internal 'result')
    out_cols = ["date", "long/short", "Setup Found at time", "trade taken",
                "Entry Price", "Stop loss", "Take Profit", "Time", "Net R:R",
                "Final Value", "Profit/Loss", "Trade Close Time", "trend", "NOTE"]
    export = trades_df.copy()
    for c in out_cols:
        if c not in export.columns:
            export[c] = ""
    export[out_cols].to_csv(args.out_trades, index=False)

    # count days only within the requested window for the per-year summary
    df_summary = df
    if start_ts is not None:
        df_summary = df_summary[df_summary["date"] >= start_ts]
    if end_ts is not None:
        df_summary = df_summary[df_summary["date"] <= end_ts]
    summary = build_summary(trades_df, df_summary,
                            risk_unit=args.capital * args.risk_pct / 100.0)
    summary.to_csv(args.out_summary, index=False)

    # console recap
    taken = (trades_df["trade taken"] == "YES").sum()
    skipped = (trades_df["trade taken"] == "NO").sum()
    hold_txt = "off (unlimited)" if max_hold is None else f"{max_hold} bars"
    dist_txt = "off" if macd_max_dist is None else f"{macd_max_dist*100:.2f}% of price"
    trend_txt = "off" if trend_ema is None else f"EMA{trend_ema} (long above / short below)"
    atr_txt = "off" if atr_max is None else f"ATR% < {atr_max*100:.2f}%"
    win = "full data" if (start_ts is None and end_ts is None) else (
        f"{args.start or 'start'} -> {args.end or 'end'} (UTC)")
    print("=== MACD BALANCED preset (1h timeframe) ===")
    print(f"MACD params         : fast={args.fast} slow={args.slow} signal={args.signal}")
    print(f"Stop / RR           : {args.sl_pct:.2f}% fixed  |  1:{args.rr}")
    print(f"Zero-line filter    : {dist_txt}")
    print(f"Trend filter        : {trend_txt}")
    print(f"Volatility gate     : {atr_txt}")
    print(f"Max hold            : {hold_txt}")
    print(f"Analysis window     : {win}")
    print(f"Bars processed      : {len(df)}")
    print(f"Setups detected     : {len(trades_df)}")
    print(f"Trades taken        : {taken}")
    print(f"Setups skipped      : {skipped}")
    print(f"Start capital       : ${capital:,.2f}")
    print(f"Final equity        : ${final_equity:,.2f}")
    print(f"Net P/L             : ${final_equity - capital:,.2f}  "
          f"({(final_equity/capital - 1)*100:.2f}%)")
    print(f"\nWrote: {args.out_trades}  +  {args.out_summary}")


if __name__ == "__main__":
    main()
