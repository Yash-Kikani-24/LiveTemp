#!/usr/bin/env python3
"""
crt_without_filter.py
=====================
STANDALONE CRT backtest -- the engine ONLY, with NO v2/v3 filter stage.

This is crt_combined_v6fixed.py's "STAGE 1 engine" carved out into its own
self-contained file. It detects CRT setups, applies the trade-taken gates and
the date-bias window gate, resolves TP/SL/EOD, and summarizes the resulting
engine trades per year. There is NO v2 candle-shape filter and NO v3 PDH/PDL
fib / time-block filter -- the numbers here equal the "PRE-FILTER (STAGE 1
engine)" table from the combined script.

It does NOT import the combined script; every tunable is duplicated below so you
can retune this strategy independently.

Outputs:
    crt_trades_final.csv            <- engine trade-level rows
    crt_without_filter_summary.csv  <- per-year summary table + TOTAL row

Run it with one command:

    python crt_without_filter.py

Notes
-----
* Needs the jp-risk server on :3001 (or a cached bias_dailyclose_3y.json) exactly
  like the combined script. Nothing about the trade maths changed.
"""

import csv
import json
import os
import urllib.request
from bisect import bisect_left
from datetime import datetime, timedelta, timezone

# ============================================================================
# ENGINE settings
# ============================================================================
HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# WHICH COIN TO BACKTEST
# ----------------------------------------------------------------------------
# Change ONLY this line to switch coins. Everything price-sensitive (data files,
# date range, fixed-stop distance, bias cache, and the date-bias windows) is
# pulled from COIN_CONFIG / DATE_BIAS_RANGES_BY_SYMBOL below, so each coin keeps
# its own independently-tuned parameters.
SYMBOL = "SOLUSDT"
SERVER = "http://localhost:3001"

# ----------------------------------------------------------------------------
# PER-COIN PARAMETERS
# ----------------------------------------------------------------------------
# fixed_stop_points was tuned per coin to the SAME volatility multiple as BTC's
# original 600 pts (= 1.77 x the median 1h candle range over 2021-01-01..2026):
#     BTC  median 1h range 339.6 -> 600   (1.21% of median price)
#     ETH  median 1h range  20.97 ->  37  (1.56% of median price)
#     SOL  median 1h range   1.05 ->  1.85 (2.00% of median price)
# Scaling by candle range (not raw price) keeps the stop the same distance in
# "normal noise" units on every coin, so setups survive ordinary wicks equally.
COIN_CONFIG = {
    "BTCUSDT": {
        "start_date": "2021-01-01",
        "end_date":   "2026-06-06",
        "fixed_stop_points": 600.0,
        "bias_cache": "bias_dailyclose_3y.json",
    },
    "ETHUSDT": {
        "start_date": "2021-01-01",
        "end_date":   "2026-06-10",
        "fixed_stop_points": 37.0,
        "bias_cache": "bias_dailyclose_3y_ETHUSDT.json",
    },
    "SOLUSDT": {
        "start_date": "2021-01-01",
        "end_date":   "2026-06-10",
        "fixed_stop_points": 1.85,
        "bias_cache": "bias_dailyclose_3y_SOLUSDT.json",
    },
}

if SYMBOL not in COIN_CONFIG:
    raise SystemExit(f"SYMBOL {SYMBOL!r} has no entry in COIN_CONFIG "
                     f"(known: {', '.join(COIN_CONFIG)})")
_CFG = COIN_CONFIG[SYMBOL]
START_DATE = _CFG["start_date"]
END_DATE = _CFG["end_date"]

# The big market-data CSVs ({SYMBOL}_*.csv) are shared and live in yash/backtest.
DATA_DIRS = [HERE, os.path.normpath(os.path.join(HERE, "..", "yash", "backtest"))]


def _data(name):
    for d in DATA_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return os.path.join(HERE, name)


# --- DYNAMIC STRUCTURE-BASED RISK (only used by STRATEGY_MODE == 'structure') ---
MIN_RISK_PCT = 500.0 / 70000.0   # ~= 0.714%
MAX_RISK_PCT = 700.0 / 70000.0   # ~= 1.000%
TARGET_RR    = 2.5               # reward distance = TARGET_RR x risk

# --- PDH/PDL CONTINUATION vs FADE ZONES (scale with entry price) ---
CONTINUATION_ZONE_PCT = 0.0035   # ~245 pts @ 70k
FADE_MIN_DISTANCE_PCT = 0.017    # ~1190 pts @ 70k

# ============================================================================
# STRATEGY MODE
# ============================================================================
#   'structure' -> PDH/PDL structure stop + risk band.
#   'fixed'     -> fixed-% (or fixed-point) stop, TP = FIXED_TARGET_RR x risk.
STRATEGY_MODE   = "fixed"
# LONGS_ONLY is only honored when ENABLE_DATE_BIAS is False. With the date-bias
# windows active, direction follows each window's tag instead: "bull" -> longs,
# "bear" -> shorts (see DATE_BIAS_RANGES below).
LONGS_ONLY      = True            # blanket long-only filter (date-bias off only)
FIXED_STOP_PCT  = 0.020           # 2.0% stop (unused while FIXED_STOP_POINTS is set)
FIXED_STOP_POINTS = _CFG["fixed_stop_points"]  # per-coin SL distance in points (see COIN_CONFIG)
FIXED_TARGET_RR = 2.5             # reward = FIXED_TARGET_RR x risk

# ============================================================================
# DATE-RANGE BIAS OVERRIDE  --  only trade dates you explicitly tag
# ============================================================================
# Inclusive ("YYYY-MM-DD", "YYYY-MM-DD") windows -> "bull" or "bear". The tag
# decides which direction is traded over that window:
#   "bull" -> take LONG setups only,
#   "bear" -> take SHORT setups only.
# Any date NOT covered by a window is skipped entirely (no trade, emitted as a
# non-taken audit row). Set ENABLE_DATE_BIAS = False to disable the windows and
# fall back to LONGS_ONLY over every date.
ENABLE_DATE_BIAS = True

# Per-coin date-bias windows. Each coin has its OWN market structure, so its
# bull/bear windows differ -- fill ETHUSDT and SOLUSDT in manually (same
# ("YYYY-MM-DD", "YYYY-MM-DD"): "bull"/"bear" format as BTCUSDT below). The
# active window set is selected by SYMBOL via DATE_BIAS_RANGES below.
DATE_BIAS_RANGES_BY_SYMBOL = {
    "BTCUSDT": {

        ("2021-01-01", "2021-04-14"): "bull",
        ("2021-04-15", "2021-06-22"): "bear",

        ("2021-06-22", "2021-11-12"): "bull",
        ("2021-11-12", "2022-12-19"): "bear",

        ("2022-12-19", "2024-03-21"): "bull",
        # ("2024-03-21", "2024-09-22"): "bear",

        ("2024-09-22", "2025-01-21"): "bull",
        ("2025-01-21", "2025-04-09"): "bear",

        ("2025-04-09", "2025-10-06"): "bull",
        ("2025-10-06", "2026-06-20"): "bear",
    },

    "ETHUSDT": {
        ("2021-01-01", "2021-05-12"): "bull",
        ("2021-05-12", "2021-07-20"): "bear",

        ("2021-07-20", "2021-09-04"): "bull",
        ("2021-09-04", "2021-09-29"): "bear",

        ("2021-09-29", "2021-11-10"): "bull",
        ("2021-11-10", "2022-06-18"): "bear",

        ("2022-06-18", "2022-08-14"): "bull",
        ("2022-08-14", "2022-11-21"): "bear",

        ("2022-11-21", "2023-04-14"): "bull",
        ("2023-04-14", "2023-10-12"): "bear",

        ("2023-10-12", "2024-03-12"): "bull",
        ("2024-03-12", "2024-05-14"): "bear",

        ("2024-05-14", "2024-05-29"): "bull",
        ("2024-05-29", "2024-07-07"): "bear",

        ("2024-07-07", "2024-07-24"): "bull",
        ("2024-07-24", "2024-08-07"): "bear",

        ("2024-08-07", "2024-08-24"): "bull",
        ("2024-08-24", "2024-09-05"): "bear",

        ("2024-09-05", "2024-12-16"): "bull",
        ("2024-12-16", "2025-04-08"): "bear",

        ("2025-04-08", "2025-08-25"): "bull",
        ("2025-08-25", "2026-06-20"): "bear",
    },

    "SOLUSDT": {
        ("2021-01-01", "2021-05-18"): "bull",
        ("2021-05-18", "2021-07-21"): "bear",

        ("2021-07-21", "2021-11-07"): "bull",
        ("2021-11-07", "2022-02-21"): "bear",

        ("2022-02-21", "2022-04-02"): "bull",
        ("2022-04-02", "2022-06-14"): "bear",

        ("2022-06-14", "2022-08-14"): "bull",
        ("2022-08-14", "2022-12-31"): "bear",

        ("2022-12-31", "2023-12-24"): "bull",
        ("2023-12-24", "2024-01-25"): "bear",

        ("2024-01-25", "2024-03-18"): "bull",
        ("2024-03-18", "2024-08-04"): "bear",

        ("2024-08-04", "2024-11-22"): "bull",
        ("2024-11-22", "2025-01-13"): "bear",

        ("2025-01-13", "2025-01-18"): "bull",
        ("2025-01-18", "2025-04-06"): "bear",

        ("2025-04-06", "2025-05-13"): "bull",
        ("2025-05-13", "2025-06-23"): "bear",

        ("2025-06-23", "2025-09-19"): "bull",
        ("2025-09-19", "2026-02-05"): "bear",

        ("2026-02-05", "2025-05-11"): "bull",
        ("2025-05-11", "2026-06-20"): "bear",
    },
}

DATE_BIAS_RANGES = DATE_BIAS_RANGES_BY_SYMBOL.get(SYMBOL, {})


def date_bias(date_str):
    """Return the configured bias ('bull'/'bear') for date_str ('YYYY-MM-DD'),
    or None if the date falls outside every window. ISO date strings compare
    lexically, so plain string comparison gives the correct chronological order."""
    for (start, end), bias in DATE_BIAS_RANGES.items():
        if start <= date_str <= end:
            return str(bias).strip().lower()
    return None


POSITION = 100000.0
FEE_PCT = 0.05                  # 0.05% per side
ALLOWED_RISK = 1000.0
LEVERAGE = 100

# trade-taken gates
IST_OFFSET_MIN = 330            # IST = UTC + 5:30
TRADE_START_H = 7              # >= 7am IST
TRADE_END_H = 18              # < 6pm IST
MAX_SETUP_TO_ENTRY_H = 2
MAX_POS_AFTER_FEES = 250000.0
ENGINE_MIN_RR = 2             # require net (post-fee) R:R > this to take a trade

ONE_HOUR_MS = 3600_000
ONE_DAY_MS = 24 * ONE_HOUR_MS

DAY_OFFSET_MIN = 330
DAY_OFFSET_MS = DAY_OFFSET_MIN * 60_000

OUT_CSV = os.path.join(HERE, "crt_trades_final.csv")
BIAS_METHOD = "dailyClose"
BIAS_CACHE = os.path.join(HERE, _CFG["bias_cache"])  # per-coin (server bias differs by symbol)
BIAS_MAX_RANGE_DAYS = 1825

ENGINE_COLUMNS = ["date", "bias", "long/short", "Setup Found at (C1,C2)", "trade taken",
                  "Entry Price", "Stop loss", "Take Profit", "Entry Trigger Time",
                  "Net R:R", "Final Value", "Profit/Loss", "Trade Close Time", "trend", "NOTE"]

# column names used by the summary
COL_DATE, COL_TAKEN = "date", "trade taken"
COL_RR, COL_FINAL, COL_PL = "Net R:R", "Final Value", "Profit/Loss"
TAKEN_YES = "Yes"

# ============================================================================
# SUMMARY settings
# ============================================================================
SUMMARY_YEARS = [2021, 2022, 2023, 2024, 2025, 2026]
SUMMARY_CSV   = os.path.join(HERE, "crt_without_filter_summary.csv")

SUMMARY_HEADERS = [
    "year", "total_days", "total_setups",
    "total_no_of_profit_setups", "total_no_of_loss_setups",
    "total_no_of_partialprofit_setups", "total_no_of_partialloss_setups",
    "net RR", "total_setup(with bias)", "net RR(with bias)",
    "total_taken", "win_rate", "net_PL_$",
]


# ============================================================================
# Shared time / io helpers
# ============================================================================
def parse_ms(s):
    dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def day_ms(date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def ist(ms):
    return utc(ms + IST_OFFSET_MIN * 60_000)


def day_bucket(ms):
    return ((ms + DAY_OFFSET_MS) // ONE_DAY_MS) * ONE_DAY_MS - DAY_OFFSET_MS


def load(path):
    out = []
    with open(path, newline="") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            out.append({"t": parse_ms(row[0]), "o": float(row[1]),
                        "h": float(row[2]), "l": float(row[3]), "c": float(row[4])})
    out.sort(key=lambda x: x["t"])
    return out


class Series:
    def __init__(self, c):
        self.c = c
        self.t = [x["t"] for x in c]

    def after(self, lo, hi):
        i = bisect_left(self.t, lo)
        j = bisect_left(self.t, hi)
        return self.c[i:j]


def fnum(v):
    if v in ("", None):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ============================================================================
# ENGINE logic
# ============================================================================
def _fetch_bias_window(start, end):
    """Fetch one bias window [start, end] (both 'YYYY-MM-DD') from the server.
    Returns {date: signal}. The window MUST be <= BIAS_MAX_RANGE_DAYS or the
    server replies 400."""
    url = f"{SERVER}/api/bias/range?symbol={SYMBOL}&start={start}&end={end}"
    with urllib.request.urlopen(url, timeout=900) as resp:
        data = json.load(resp)
    return {d["date"]: ((d.get("results") or {}).get(BIAS_METHOD) or {})
            .get("signal", "N/A") for d in data["days"]}


def fetch_bias():
    """Historical daily bias (reference column only; never filters direction).

    Prefer the cached BIAS_CACHE json. Otherwise call GET /api/bias/range in
    sub-windows of at most BIAS_MAX_RANGE_DAYS days and merge them. If the
    server is unreachable, degrade gracefully to no bias (every day -> 'N/A')."""
    if os.path.exists(BIAS_CACHE):
        with open(BIAS_CACHE) as f:
            return json.load(f)
    try:
        bias = {}
        cur = datetime.strptime(START_DATE, "%Y-%m-%d")
        hard_end = datetime.strptime(END_DATE, "%Y-%m-%d")
        step = timedelta(days=BIAS_MAX_RANGE_DAYS - 1)
        while cur <= hard_end:
            win_end = min(cur + step, hard_end)
            bias.update(_fetch_bias_window(cur.strftime("%Y-%m-%d"),
                                           win_end.strftime("%Y-%m-%d")))
            cur = win_end + timedelta(days=1)
        with open(BIAS_CACHE, "w") as f:
            json.dump(bias, f)
        return bias
    except Exception as e:
        print(f"!! bias unavailable ({e}); continuing with no bias (N/A)")
        return {}


_JP = {}
def jp_risk(direction, entry, sl, tp):
    key = (direction, round(entry, 2), round(sl, 2), round(tp, 2))
    if key in _JP:
        return _JP[key]
    body = json.dumps({"position": POSITION, "direction": direction, "entry": entry,
                       "sl": sl, "tp": tp, "leverage": LEVERAGE, "fee": FEE_PCT,
                       "allowedRisk": ALLOWED_RISK}).encode()
    req = urllib.request.Request(f"{SERVER}/api/tools/jp-risk", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        res = json.load(resp)["result"]
    _JP[key] = res
    return res


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


def find_fill(m15, direction, entry, start_ms, day_end):
    for k in m15.after(start_ms, day_end):
        if direction == "long" and k["l"] <= entry:
            return k["t"]
        if direction == "short" and k["h"] >= entry:
            return k["t"]
    return None


def resolve(m15, direction, sl, tp, fill_ms, day_end):
    for k in m15.after(fill_ms, day_end):
        if direction == "long":
            if k["l"] <= sl and k["h"] >= tp:
                return "SL", k["t"]
            if k["l"] <= sl:
                return "SL", k["t"]
            if k["h"] >= tp:
                return "TP", k["t"]
        else:
            if k["h"] >= sl and k["l"] <= tp:
                return "SL", k["t"]
            if k["h"] >= sl:
                return "SL", k["t"]
            if k["l"] <= tp:
                return "TP", k["t"]
    return "EOD", day_end


def manual_pnl(direction, entry, exit_px):
    coins = POSITION / entry
    fee = FEE_PCT / 100.0
    entry_fee = POSITION * fee
    exit_fee = coins * exit_px * fee
    gross = (exit_px - entry) * coins if direction == "long" else (entry - exit_px) * coins
    return gross - entry_fee - exit_fee


def structure_trade(direction, entry, pdh, pdl):
    """Decide the trade plan. Returns (ok, sl, tp, label, note).

    In STRATEGY_MODE == 'fixed' (the default): fixed-% / fixed-point stop,
    TP = FIXED_TARGET_RR x risk, every zone allowed, optionally longs-only.
    In 'structure' mode: PDH/PDL-anchored stops with a risk band.
    """
    # ---- FIXED-STOP MODE ----------------------------------------------------
    if STRATEGY_MODE == "fixed":
        # Direction control: when the date-bias windows are active they decide
        # direction (bull window -> take longs, bear window -> take shorts), so
        # LONGS_ONLY is bypassed and shorts ARE taken inside bear windows.
        # LONGS_ONLY only acts as a blanket long-only filter when
        # ENABLE_DATE_BIAS is False.
        if LONGS_ONLY and not ENABLE_DATE_BIAS and direction != "long":
            return (False, None, None, "shorts disabled",
                    "Not taken: LONGS_ONLY mode (short rejected)")
        risk = FIXED_STOP_POINTS if FIXED_STOP_POINTS > 0 else entry * FIXED_STOP_PCT
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


def run_engine():
    """Detect + gate + resolve every setup, write crt_trades_final.csv, and
    return (rows, daily_by_date, m15, h1_candles_by_ms)."""
    h1 = load(_data(f"{SYMBOL}_1h_all.csv"))
    by_day, daily = {}, {}
    for c in h1:
        b = day_bucket(c["t"])
        by_day.setdefault(b, []).append(c)
        sd = daily.get(b)
        if sd is None:
            daily[b] = {"t": b, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]}
        else:
            sd["h"] = max(sd["h"], c["h"])
            sd["l"] = min(sd["l"], c["l"])
            sd["c"] = c["c"]
    m15 = Series(load(_data(f"{SYMBOL}_15m_all.csv")))
    bias_map = fetch_bias()

    rows = []
    st = {"setups": 0, "taken": 0, "tp": 0, "sl": 0, "pp": 0, "pl": 0,
          "no_trigger": 0, "blk_time": 0, "blk_stale": 0, "blk_pos": 0,
          "blk_rr": 0, "blk_date": 0, "blk_server_bias": 0}

    d, end = day_bucket(day_ms(START_DATE)), day_bucket(day_ms(END_DATE))
    while d <= end:
        prev = daily.get(d - ONE_DAY_MS)
        cands = by_day.get(d, [])
        date_str = ist(d).strftime("%Y-%m-%d")
        bias = bias_map.get(date_str, "N/A")
        if prev and len(cands) >= 2:
            day_end = d + ONE_DAY_MS
            day_close = daily.get(d, {}).get("c")
            for kind, c1, c2 in detect(cands, prev["h"], prev["l"]):
                st["setups"] += 1
                direction = "long" if kind == "Bullish" else "short"
                ls = "Long" if kind == "Bullish" else "Short"
                b = str(bias).strip().lower()
                if b == "bullish":
                    trend = "with trend" if direction == "long" else "counter trend"
                elif b == "bearish":
                    trend = "with trend" if direction == "short" else "counter trend"
                else:
                    trend = "neutral"
                entry = c2["o"]
                pdh, pdl = prev["h"], prev["l"]

                # DATE-RANGE BIAS gate: only trade dates whose configured bias
                # matches the setup direction (long<-bull, short<-bear).
                if ENABLE_DATE_BIAS:
                    want = "bull" if direction == "long" else "bear"
                    db = date_bias(date_str)
                    if db != want:
                        setup_at = (f"C1 {ist(c1['t']):%Y-%m-%d %H:%M} / "
                                    f"C2 {ist(c2['t']):%H:%M} IST")
                        rows.append({"date": date_str, "bias": bias,
                                     "long/short": ls,
                                     "Setup Found at (C1,C2)": setup_at,
                                     "trade taken": "No",
                                     "Entry Price": round(entry, 2),
                                     "Stop loss": "", "Take Profit": "",
                                     "Entry Trigger Time": "", "Net R:R": "",
                                     "Final Value": "", "Profit/Loss": "",
                                     "Trade Close Time": "", "trend": trend,
                                     "NOTE": (f"Not taken (date bias): {date_str} "
                                              f"bias '{db or 'none'}' != required "
                                              f"'{want}' for {direction}")})
                        st["blk_date"] += 1
                        continue

                    # SERVER DAILY BIAS gate: the configured (code) bias matched
                    # the direction above; now the server's daily bias must agree
                    # or be neutral before we take the trade.
                    #   long  (code bull) -> server bias BULLISH or NEUTRAL
                    #   short (code bear) -> server bias BEARISH or NEUTRAL
                    sb = str(bias).strip().lower()
                    server_ok = {"long": ("bullish", "neutral"),
                                 "short": ("bearish", "neutral")}[direction]
                    if sb not in server_ok:
                        setup_at = (f"C1 {ist(c1['t']):%Y-%m-%d %H:%M} / "
                                    f"C2 {ist(c2['t']):%H:%M} IST")
                        rows.append({"date": date_str, "bias": bias,
                                     "long/short": ls,
                                     "Setup Found at (C1,C2)": setup_at,
                                     "trade taken": "No",
                                     "Entry Price": round(entry, 2),
                                     "Stop loss": "", "Take Profit": "",
                                     "Entry Trigger Time": "", "Net R:R": "",
                                     "Final Value": "", "Profit/Loss": "",
                                     "Trade Close Time": "", "trend": trend,
                                     "NOTE": (f"Not taken (server bias): {date_str} "
                                              f"server daily bias '{bias}' does not "
                                              f"agree (needs {server_ok[0]} or "
                                              f"neutral) for {direction}")})
                        st["blk_server_bias"] += 1
                        continue

                ok, sl, tp, zone_label, zone_note = structure_trade(
                    direction, entry, pdh, pdl)

                if not ok:
                    # structure/zone rejection: emit a non-taken audit row
                    setup_at = (f"C1 {ist(c1['t']):%Y-%m-%d %H:%M} / "
                                f"C2 {ist(c2['t']):%H:%M} IST")
                    row = {"date": date_str, "bias": bias, "long/short": ls,
                           "Setup Found at (C1,C2)": setup_at, "trade taken": "No",
                           "Entry Price": round(entry, 2), "Stop loss": "",
                           "Take Profit": "", "Entry Trigger Time": "",
                           "Net R:R": "", "Final Value": "", "Profit/Loss": "",
                           "Trade Close Time": "", "trend": trend,
                           "NOTE": f"Not taken: {zone_note}"}
                    rows.append(row)
                    continue

                jp = jp_risk(direction, entry, sl, tp)
                net_rr = round(jp["rrAfterFees"], 3)
                setup_found = c2["t"] + ONE_HOUR_MS
                fill = find_fill(m15, direction, entry, setup_found, day_end)

                setup_at = (f"C1 {ist(c1['t']):%Y-%m-%d %H:%M} / "
                            f"C2 {ist(c2['t']):%H:%M} IST")
                row = {"date": date_str, "bias": bias, "long/short": ls,
                       "Setup Found at (C1,C2)": setup_at, "trade taken": "No",
                       "Entry Price": round(entry, 2), "Stop loss": round(sl, 2),
                       "Take Profit": round(tp, 2),
                       "Entry Trigger Time": (f"{ist(fill):%Y-%m-%d %H:%M} IST"
                                              if fill is not None else ""),
                       "Net R:R": net_rr, "Final Value": "", "Profit/Loss": "",
                       "Trade Close Time": "", "trend": trend,
                       "NOTE": zone_note}

                blockers = []
                if fill is None:
                    st["no_trigger"] += 1
                    row["NOTE"] = "Entry trigger not reached before day end (5:30 IST)"
                    rows.append(row)
                    continue
                sh = ist(setup_found).hour
                if not (TRADE_START_H <= sh < TRADE_END_H):
                    blockers.append(f"setup time {ist(setup_found):%H:%M} IST outside 07:00-23:00")
                    st["blk_time"] += 1
                ih = ist(fill).hour
                if not (TRADE_START_H <= ih < TRADE_END_H):
                    blockers.append(f"entry time {ist(fill):%H:%M} IST outside 07:00-23:00")
                    st["blk_time"] += 1
                if (fill - setup_found) > MAX_SETUP_TO_ENTRY_H * ONE_HOUR_MS:
                    hrs = (fill - setup_found) / ONE_HOUR_MS
                    blockers.append(f"setup->entry {hrs:.1f}h > {MAX_SETUP_TO_ENTRY_H}h")
                    st["blk_stale"] += 1
                if jp["maxPositionAfterFees"] > MAX_POS_AFTER_FEES:
                    blockers.append(f"maxPos ${jp['maxPositionAfterFees']:,.0f} > $250k")
                    st["blk_pos"] += 1
                if net_rr <= ENGINE_MIN_RR:
                    blockers.append(f"net R:R {net_rr} <= {ENGINE_MIN_RR}")
                    st["blk_rr"] += 1

                if blockers:
                    row["NOTE"] = "Not taken: " + "; ".join(blockers)
                    rows.append(row)
                    continue

                row["trade taken"] = "Yes"
                st["taken"] += 1
                outcome, close_ms = resolve(m15, direction, sl, tp, fill, day_end)
                if outcome == "TP":
                    pnl, cat = jp["profitAfterFees"], "Profit"
                    st["tp"] += 1
                elif outcome == "SL":
                    pnl, cat = -jp["lossAfterFees"], "Loss"
                    st["sl"] += 1
                    row["Net R:R"] = -1
                else:
                    pnl = manual_pnl(direction, entry, day_close)
                    if pnl > 0:
                        cat = "Partial Profit"; st["pp"] += 1
                    else:
                        cat = "Partial Loss"; st["pl"] += 1
                    jp_eod = jp_risk(direction, entry, sl, day_close)
                    row["Net R:R"] = round(jp_eod["rrAfterFees"], 3)
                row["Final Value"] = round(POSITION + pnl, 2)
                row["Profit/Loss"] = cat
                row["Trade Close Time"] = f"{ist(close_ms):%Y-%m-%d %H:%M} IST"
                tag = {"TP": "TP hit", "SL": "SL hit",
                       "EOD": f"Closed at day end {day_close:.2f}"}[outcome]
                row["NOTE"] = (f"{zone_label}; {tag}; "
                               f"entry {ist(fill):%Y-%m-%d %H:%M} IST; "
                               f"P/L ${pnl:,.2f}")
                rows.append(row)
        d += ONE_DAY_MS

    # write the engine CSV
    out_path = OUT_CSV
    try:
        f = open(out_path, "w", newline="")
    except PermissionError:
        out_path = OUT_CSV.replace(".csv", "_new.csv")
        print(f"!! {OUT_CSV} is locked (open in Excel?) -> writing {out_path}")
        f = open(out_path, "w", newline="")
    with f:
        w = csv.DictWriter(f, fieldnames=ENGINE_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    taken = [r for r in rows if r["trade taken"] == "Yes"]
    net = sum(r["Final Value"] - POSITION for r in taken)
    resolved = st["tp"] + st["sl"] + st["pp"] + st["pl"]
    wins = st["tp"] + st["pp"]
    print(f"Wrote {len(rows)} engine rows -> {out_path}\n")
    print(f" setups detected     {st['setups']}")
    print(f" trades TAKEN        {st['taken']}")
    print(f"   TP / SL           {st['tp']} / {st['sl']}")
    print(f"   Partial +/-       {st['pp']} / {st['pl']}")
    print(f" not taken:")
    print(f"   trigger not hit   {st['no_trigger']}")
    print(f"   blocked time      {st['blk_time']}")
    print(f"   blocked stale>{MAX_SETUP_TO_ENTRY_H}h   {st['blk_stale']}")
    print(f"   blocked maxPos    {st['blk_pos']}")
    print(f"   blocked R:R<{ENGINE_MIN_RR}  {st['blk_rr']}")
    print(f"   blocked date bias {st['blk_date']}")
    print(f"   blocked server bias {st['blk_server_bias']}")
    if resolved:
        print(f" win rate (taken)    {100*wins/resolved:.1f}%  ({wins}/{resolved})")
    print(f" net P/L (taken)     ${net:,.2f}\n")

    candles_by_ms = {c["t"]: c for c in h1}
    daily_by_date = {ist(b).strftime("%Y-%m-%d"): {"h": v["h"], "l": v["l"], "c": v["c"]}
                     for b, v in daily.items()}
    return rows, daily_by_date, m15, candles_by_ms


# ============================================================================
# SUMMARY
# ============================================================================
def summarize_year(year, year_rows):
    """Build one summary row from the engine rows of a single year.

    Counts are over TAKEN trades. 'with bias' = the subset whose trend ==
    'with trend'."""
    taken = [r for r in year_rows
             if str(r.get(COL_TAKEN)).strip().lower() == TAKEN_YES.lower()]

    days = len({str(r.get(COL_DATE))[:10] for r in year_rows if r.get(COL_DATE)})

    def pl_is(r, label):
        return str(r.get(COL_PL)).strip().lower() == label

    profit  = sum(1 for r in taken if pl_is(r, "profit"))
    loss    = sum(1 for r in taken if pl_is(r, "loss"))
    p_prof  = sum(1 for r in taken if pl_is(r, "partial profit"))
    p_loss  = sum(1 for r in taken if pl_is(r, "partial loss"))

    def rr_val(r):
        v = fnum(r.get(COL_RR))
        return v if v is not None else 0.0

    net_rr = round(sum(rr_val(r) for r in taken), 3)

    with_bias = [r for r in taken
                 if str(r.get("trend")).strip().lower() == "with trend"]
    net_rr_bias = round(sum(rr_val(r) for r in with_bias), 3)

    def fv(r):
        v = fnum(r.get(COL_FINAL))
        return (v - POSITION) if v is not None else 0.0

    net_pl = round(sum(fv(r) for r in taken), 2)
    wins = profit + p_prof
    resolved = profit + loss + p_prof + p_loss
    win_rate = f"{100*wins/resolved:.1f}%" if resolved else "0.0%"

    return {
        "year": year,
        "total_days": days,
        "total_setups": len(taken),
        "total_no_of_profit_setups": profit,
        "total_no_of_loss_setups": loss,
        "total_no_of_partialprofit_setups": p_prof,
        "total_no_of_partialloss_setups": p_loss,
        "net RR": net_rr,
        "total_setup(with bias)": len(with_bias),
        "net RR(with bias)": net_rr_bias,
        "total_taken": len(taken),
        "win_rate": win_rate,
        "net_PL_$": net_pl,
    }


def _summary_total(summary_rows):
    """Build the TOTAL row (sum numerics; win_rate recomputed from totals)."""
    total = {h: 0 for h in SUMMARY_HEADERS}
    total["year"] = "TOTAL"
    int_cols = ["total_days", "total_setups", "total_no_of_profit_setups",
                "total_no_of_loss_setups", "total_no_of_partialprofit_setups",
                "total_no_of_partialloss_setups", "total_setup(with bias)",
                "total_taken"]
    float_cols = ["net RR", "net RR(with bias)", "net_PL_$"]
    for h in int_cols:
        total[h] = sum(r[h] for r in summary_rows)
    for h in float_cols:
        total[h] = round(sum(r[h] for r in summary_rows), 3)
    wins = total["total_no_of_profit_setups"] + total["total_no_of_partialprofit_setups"]
    resolved = (total["total_no_of_profit_setups"] + total["total_no_of_loss_setups"]
                + total["total_no_of_partialprofit_setups"] + total["total_no_of_partialloss_setups"])
    total["win_rate"] = f"{100*wins/resolved:.1f}%" if resolved else "0.0%"
    return total


def write_summary(summary_rows):
    out_path = SUMMARY_CSV
    try:
        f = open(out_path, "w", newline="")
    except PermissionError:
        out_path = SUMMARY_CSV.replace(".csv", "_new.csv")
        print(f"!! {SUMMARY_CSV} locked -> writing {out_path}")
        f = open(out_path, "w", newline="")
    with f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_HEADERS)
        w.writeheader()
        w.writerows(summary_rows)
        w.writerow(_summary_total(summary_rows))
    return out_path


# ============================================================================
# MAIN
# ============================================================================
def main():
    print("=== STAGE 1: ENGINE (no v2/v3 filters) ===")
    engine_rows, daily, m15, candles = run_engine()

    print("=== SUMMARY (per year, engine rows only) ===")
    summary_rows = []
    for year in SUMMARY_YEARS:
        year_rows = [r for r in engine_rows
                     if str(r.get("date", ""))[:4] == str(year)]
        summary_rows.append(summarize_year(year, year_rows))
        taken = sum(1 for r in year_rows
                    if str(r.get(COL_TAKEN)).strip().lower() == TAKEN_YES.lower())
        print(f"  {year}: {len(year_rows):4d} rows, {taken:3d} taken")

    path = write_summary(summary_rows)
    print(f"\nWrote {path}\n")

    # pretty-print the summary table to console
    hdr = SUMMARY_HEADERS
    widths = [max(len(str(h)), 6) for h in hdr]
    print("  ".join(str(h).ljust(w) for h, w in zip(hdr, widths)))
    for r in summary_rows:
        print("  ".join(str(r[h]).ljust(w) for h, w in zip(hdr, widths)))
    total = _summary_total(summary_rows)
    print("  ".join(str(total[h]).ljust(w) for h, w in zip(hdr, widths)))


if __name__ == "__main__":
    main()
