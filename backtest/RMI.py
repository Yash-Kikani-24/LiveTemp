#!/usr/bin/env python3
"""
RSI(14) + EMA(45)/SMA(9)-on-RSI crossover backtester
=====================================================

STRATEGY
--------
1. Compute RSI(14) on close.
2. Compute EMA(45) and SMA(9) ON THE RSI LINE.
3. Signal (evaluated on CLOSED candle):
     - SMA crosses EMA  DOWN->UP  -> LONG
     - SMA crosses EMA  UP->DOWN   -> SHORT
4. One trade at a time. New signals ignored while a trade is open
   EXCEPT an OPPOSITE signal, which closes the open trade early at
   that candle's close (= partial profit / partial loss, option A).
5. SL fixed 2% from entry. TP at 1:2 RR (4% from entry) -> raw RR 2.0.
6. Risk sizing mirrors the JP/AMD calculator:
     - allowed risk per trade = 1% of capital ($1,000 on $100k)
     - position sized so loss-AFTER-FEES <= allowed risk
     - hard gates: net RR after fees > 1.8  AND  position <= 2,50,000
     - 0.05% taker fee on entry and exit
7. Intrabar: if a candle hits BOTH SL and TP, SL counts first.

CAPITAL  = $100,000
FEE      = 0.05% taker (each side)
RISK/TRD = $1,000 (1%)

USAGE
-----
    python strategy.py /path/to/BTC_4h.csv
    python strategy.py /path/to/BTC_4h.csv --capital 100000 --risk-pct 1

OUTPUTS
-------
    trades.csv    - one row per setup/trade
    summary.csv   - per-year aggregate stats
"""

import argparse
import os
import sys
import numpy as np
import pandas as pd

# ----------------------------------------------------------------------
# FILE DISCOVERY  (mirrors rsi_ema_sma_strategy.py)
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


def find_4h_csv():
    """Auto-discover the BTC 4h CSV (same convention as rsi_ema_sma_strategy.py)."""
    p = _data(f"{SYMBOL}_4h_all.csv")
    if os.path.exists(p):
        print(f"Auto-detected: {os.path.basename(p)}")
        return p
    raise SystemExit(
        f"Could not find {SYMBOL}_4h_all.csv in {DATA_DIRS}.\n"
        f"Pass the CSV path explicitly: python strategy.py /path/to/BTC_4h.csv"
    )

# ----------------------------------------------------------------------
# CONFIG / CONSTANTS
# ----------------------------------------------------------------------
RSI_PERIOD   = 14
EMA_PERIOD   = 45
SMA_PERIOD   = 9
ATR_PERIOD   = 14           # ATR lookback (Wilder)
ATR_MULT     = 1.0          # stop distance = 1.0 x ATR (adaptive; median ~1.5%)
SL_MIN_PCT   = 0.01         # stop floor: never tighter than 1.0% (noise/slippage guard)
SL_MAX_PCT   = 0.03         # stop ceiling: never wider than 3.0% (keeps TP reachable)
RR_TARGET    = 6            # 1:6 -> TP = 6 x stop distance
MAX_HOLD_BARS = 42          # force-close after 42 bars (7 days on 4h). None=off.
FEE_PCT      = 0.0005        # 0.05% taker, each side
MIN_NET_RR   = 1.8           # hard gate: net RR after fees must exceed this
MAX_POSITION = 250_000       # hard gate: position size cap ($)


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
def wilder_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)   # no losses -> RSI 100
    return rsi


def add_indicators(df):
    df = df.copy()
    df["rsi"]     = wilder_rsi(df["close"], RSI_PERIOD)
    df["rsi_ema"] = df["rsi"].ewm(span=EMA_PERIOD, adjust=False).mean()
    df["rsi_sma"] = df["rsi"].rolling(SMA_PERIOD).mean()
    df["diff"]    = df["rsi_sma"] - df["rsi_ema"]   # SMA above EMA -> positive
    # ATR (Wilder) for adaptive stop sizing
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"]  - df["close"].shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/ATR_PERIOD, adjust=False).mean()
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
def backtest(df, capital, risk_pct, start_ts=None, end_ts=None):
    """Indicators are computed on the FULL series (so the EMA warm-up stays
    correct); start_ts/end_ts only restrict which bars may OPEN a new trade.
    A trade opened inside the window is allowed to run to its SL/TP even if
    that happens after end_ts."""
    allowed_risk = capital * risk_pct / 100.0
    equity = capital
    trades = []

    in_trade = False
    pos = None  # open trade dict

    # warm-up: need rsi_ema & rsi_sma valid
    start = max(RSI_PERIOD + EMA_PERIOD, RSI_PERIOD + SMA_PERIOD) + 1

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
        long_signal  = (d_prev <= 0) and (d_now > 0)   # SMA crosses up through EMA
        short_signal = (d_prev >= 0) and (d_now < 0)   # SMA crosses down through EMA

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
            elif MAX_HOLD_BARS is not None and (i - pos["entry_i"]) >= MAX_HOLD_BARS:
                exit_price = cur["close"]; closed = True
                gross = ((exit_price - pos["entry"]) if pos["dir"] == "long"
                         else (pos["entry"] - exit_price))
                result = "PARTIAL_PROFIT" if gross > 0 else "PARTIAL_LOSS"
                note = f"Max-hold ({MAX_HOLD_BARS} bars) time exit"

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
            if direction == "long":
                sl_dist_abs = ATR_MULT * cur["atr"]
                sl_dist_abs = min(max(sl_dist_abs, entry * SL_MIN_PCT), entry * SL_MAX_PCT)
                sl = entry - sl_dist_abs
                tp = entry + sl_dist_abs * RR_TARGET
            else:
                sl_dist_abs = ATR_MULT * cur["atr"]
                sl_dist_abs = min(max(sl_dist_abs, entry * SL_MIN_PCT), entry * SL_MAX_PCT)
                sl = entry + sl_dist_abs
                tp = entry - sl_dist_abs * RR_TARGET

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
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="?", default=None,
                    help="path to BTC 4h OHLC csv (auto-detected if omitted)")
    ap.add_argument("--capital", type=float, default=100_000)
    ap.add_argument("--risk-pct", type=float, default=1.0)
    ap.add_argument("--start", default=None,
                    help="analyse trades from this date (UTC, e.g. 2021-01-01)")
    ap.add_argument("--end", default=None,
                    help="analyse trades up to this date (UTC, inclusive, e.g. 2021-12-31)")
    ap.add_argument("--out-trades", default="trades.csv")
    ap.add_argument("--out-summary", default="summary.csv")
    args = ap.parse_args()

    # parse the analysis window (data is UTC). A bare date for --end is made
    # inclusive of the whole day.
    start_ts = pd.to_datetime(args.start) if args.start else None
    end_ts   = pd.to_datetime(args.end)   if args.end   else None
    if end_ts is not None and end_ts == end_ts.normalize():
        end_ts = end_ts + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        sys.exit(f"ERROR: --start ({args.start}) is after --end ({args.end}).")

    csv_path = args.csv if args.csv else find_4h_csv()
    df = load_csv(csv_path)
    df = add_indicators(df)

    trades_df, final_equity, capital = backtest(
        df, args.capital, args.risk_pct, start_ts, end_ts)

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
    win = "full data" if (start_ts is None and end_ts is None) else (
        f"{args.start or 'start'} -> {args.end or 'end'} (UTC)")
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
