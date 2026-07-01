"""
main.py — the Engine (Process A). Single long-lived asyncio process under systemd.

Three logical pieces (webinfo.txt section 3.1):

  DATA MANAGER — REST-backfills the last N candles per symbol+interval on
    startup, opens ONE multiplexed Binance WebSocket for every symbol+interval,
    reacts only to CLOSED candles (k.x == true), and self-reconnects + gap-
    backfills if the socket drops.

  RUNNER — auto-discovers strategy files in strategies/, and on each closed
    candle runs every subscribed strategy concurrently via asyncio. Reads latest
    candles + bias + trend from Neon and bundles { bias, trend } as context.
    When a strategy returns a Signal, runs the signal pipeline (section 3.2 / 4):
      1. call FastAPI POST /internal/signal (localhost, strict 1-2s timeout)
      2. UNCONDITIONALLY write the signal to Neon (deduped via last_alert),
         regardless of whether step 1 succeeded.
    Also runs the telegram_pending retry sweep (periodic + on startup).

  STRATEGIES — drop-in files; see strategies/base.py.

All logic is intentionally stubbed — only the structure is here.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import pkgutil
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone

import httpx
import websockets

import db
import strategies as strategies_pkg
from strategies.base import Strategy


# ===========================================================================
# STRATEGY DISCOVERY
# ===========================================================================
def discover_strategies():
    """Auto-import every module in the strategies/ package and instantiate every
    concrete Strategy subclass found. Adding a strategy = dropping one file."""
    found = []
    seen = set()
    for mod_info in pkgutil.iter_modules(strategies_pkg.__path__):
        name = mod_info.name
        if name.startswith("_") or name == "base":
            continue
        try:
            module = importlib.import_module(f"{strategies_pkg.__name__}.{name}")
        except Exception as exc:               # noqa: BLE001 — bad file: skip, don't crash
            print(f"[discover] SKIPPED strategy module {name!r}: {exc!r} — "
                  f"other strategies still load")
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (issubclass(obj, Strategy) and obj is not Strategy
                    and getattr(obj, "name", "") and obj not in seen):
                seen.add(obj)
                found.append(obj())
    return found


def subscriptions_from_strategies(strategies):
    """Collapse the discovered strategies into the set of (symbol, interval) feeds
    the Data Manager must subscribe to, plus how many candles to keep per feed
    (the largest lookback any strategy needs on that feed, with a small margin)."""
    keep_n = {}
    for strat in strategies:
        interval = strat.interval
        want = max(int(getattr(strat, "lookback", 0)), 1) + BACKFILL_MARGIN
        for symbol in strat.symbols:
            key = (symbol, interval)
            keep_n[key] = max(keep_n.get(key, 0), want)
    return keep_n


# ===========================================================================
# DATA MANAGER
# ===========================================================================
# Binance public market-data endpoints (no API key). Defaults use the
# data.binance.vision mirror the existing app uses (not geo-blocked); override
# via env if needed.
BINANCE_REST_BASE = os.environ.get("BINANCE_REST_BASE", "https://data-api.binance.vision")
BINANCE_WS_BASE = os.environ.get("BINANCE_WS_BASE", "wss://data-stream.binance.vision")

BACKFILL_MARGIN = 5          # keep a few extra candles beyond the largest lookback
WS_MAX_BACKOFF = 60          # seconds; reconnect backoff ceiling


class DataManager:
    """Owns Binance market data: REST backfill + multiplexed WebSocket feed.

    On startup (and on every WS reconnect) it REST-backfills the newest N CLOSED
    candles per (symbol, interval) into Neon, then consumes one multiplexed
    WebSocket. It acts ONLY on closed candles (k.x == true); in-progress ticks are
    ignored. Each closed candle is upserted (idempotent) and the buffer trimmed to
    the newest N. `on_closed` (optional) is awaited after the DB write so the
    Runner can hook strategy evaluation in later."""

    def __init__(self, keep_n, on_closed=None):
        # keep_n: { (symbol, interval): N }
        self.keep_n = dict(keep_n)
        self.subscriptions = list(keep_n.keys())
        self.on_closed = on_closed

    @classmethod
    def from_strategies(cls, strategies, on_closed=None):
        return cls(subscriptions_from_strategies(strategies), on_closed=on_closed)

    # --- REST backfill ------------------------------------------------------
    async def backfill(self, symbol, interval, n):
        """REST-fetch the last N CLOSED candles for one symbol+interval into Neon.

        Binance returns the in-progress candle as the final element, so we request
        one extra and drop it — only closed candles are written. Idempotent, so this
        doubles as the gap-fill on reconnect."""
        url = f"{BINANCE_REST_BASE}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": n + 1}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            klines = resp.json()
        # Drop the last (currently-forming) candle — we only persist closed ones.
        closed = klines[:-1] if klines else []
        for k in closed:
            candle = {
                "open_time": int(k[0]), "open": k[1], "high": k[2], "low": k[3],
                "close": k[4], "volume": k[5], "close_time": int(k[6]),
            }
            await db.upsert_candle(symbol, interval, candle)
        await db.trim_candles(symbol, interval, n)
        return len(closed)

    async def backfill_all(self):
        """Backfill every subscription (startup + every reconnect gap-fill)."""
        for (symbol, interval) in self.subscriptions:
            n = self.keep_n[(symbol, interval)]
            count = await self.backfill(symbol, interval, n)
            print(f"[backfill] {symbol} {interval}: {count} closed candles -> Neon")

    # --- live WebSocket -----------------------------------------------------
    def _stream_url(self):
        streams = "/".join(f"{s.lower()}@kline_{i}" for (s, i) in self.subscriptions)
        return f"{BINANCE_WS_BASE}/stream?streams={streams}"

    async def _handle_message(self, raw):
        """Parse one combined-stream message; act ONLY on a closed candle."""
        msg = json.loads(raw)
        k = msg.get("data", {}).get("k")
        if not k or not k.get("x"):       # k.x == true means the candle has CLOSED
            return
        symbol, interval = k["s"], k["i"]
        candle = {
            "open_time": int(k["t"]), "open": k["o"], "high": k["h"], "low": k["l"],
            "close": k["c"], "volume": k["v"], "close_time": int(k["T"]),
        }
        await db.upsert_candle(symbol, interval, candle)
        keep = self.keep_n.get((symbol, interval))
        if keep:
            await db.trim_candles(symbol, interval, keep)
        print(f"[closed] {symbol} {interval} @ {candle['open_time']} "
              f"close={candle['close']} -> Neon")
        if self.on_closed is not None:
            await self.on_closed(symbol, interval, candle)

    async def run(self):
        """Reconnect loop: (re)backfill any gap, open the multiplexed WS, consume
        closed candles forever. Any drop reopens the socket after a backoff and
        gap-fills before resuming."""
        backoff = 1
        while True:
            try:
                # Backfill the gap BEFORE trusting the live feed (startup + reconnect).
                await self.backfill_all()
                url = self._stream_url()
                print(f"[ws] connecting: {url}")
                async with websockets.connect(url, ping_interval=180,
                                               ping_timeout=600,
                                               max_queue=None) as ws:
                    print("[ws] connected — waiting for closed candles")
                    backoff = 1                # reset after a successful connect
                    async for raw in ws:
                        await self._handle_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:           # noqa: BLE001 — any drop -> reconnect
                print(f"[ws] dropped ({exc!r}); reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, WS_MAX_BACKOFF)


# ===========================================================================
# RUNNER
# ===========================================================================
# Engine -> FastAPI internal hop (localhost; bypasses Nginx — webinfo.txt 3.3).
FASTAPI_INTERNAL_URL = os.environ.get("FASTAPI_INTERNAL_URL", "http://127.0.0.1:8000")
# STRICT timeout on the FastAPI call so a slow/hung FastAPI never stalls candle
# processing (webinfo.txt 4.2). Covers connect + read.
FASTAPI_TIMEOUT = 1.5         # seconds (the recommended 1–2s band)
SWEEP_INTERVAL = 180         # seconds between telegram_pending retry sweeps (a few min)

# ---------------------------------------------------------------------------
# Node backend keep-alive (Render free tier sleeps after 15 min of inactivity)
# ---------------------------------------------------------------------------
NODE_BACKEND_URL    = os.environ.get("NODE_BACKEND_URL", "http://localhost:3001").rstrip("/")
KEEPALIVE_INTERVAL  = 600     # 10 minutes — well under Render's 15-min sleep threshold
KEEPALIVE_TIMEOUT   = 30.0    # generous; server may be mid-wake on first ping after idle


class Runner:
    """Drives strategies on each closed candle and runs the signal pipeline
    (webinfo.txt 3.1 / 3.2 / 4)."""

    def __init__(self, strategies):
        self.strategies = list(strategies)
        # index: (symbol, interval) -> [strategies subscribed to that feed]
        self.by_feed = defaultdict(list)
        for strat in self.strategies:
            for symbol in strat.symbols:
                self.by_feed[(symbol, strat.interval)].append(strat)
        # observability: count FastAPI failures so a pattern is visible in logs.
        self.fastapi_failures = 0

    # --- per closed candle --------------------------------------------------
    async def on_closed_candle(self, symbol, interval, candle):
        """Run EVERY strategy subscribed to this (symbol, interval) concurrently."""
        strats = self.by_feed.get((symbol, interval))
        if not strats:
            return
        await asyncio.gather(
            *(self._run_one(strat, symbol, candle) for strat in strats)
        )

    async def _run_one(self, strat, symbol, candle):
        """Read context from Neon, evaluate one strategy, dispatch any Signal.

        Isolated in try/except so one strategy's error can't sink the others or
        stall candle processing."""
        try:
            if not await db.get_strategy_enabled(strat.name):
                return
            candles = await db.get_recent_candles(symbol, strat.interval, strat.lookback)
            trend_row = await db.get_trend(symbol, strat.name)
            # Context carries the manual trend + user-set R:R from the Neon `trend`
            # table. The computed bias is not here — a strategy fetches it live from
            # the Node backend when it finds a setup (see strategies/crt.py).
            context = {
                "trend": trend_row["trend"] if trend_row else None,
                "rr": float(trend_row["rr"]) if trend_row and trend_row["rr"] is not None else None,
            }
            # on_candle may be sync OR async (async lets a strategy await an HTTP
            # bias check); await it if it returned a coroutine.
            signal = strat.on_candle(symbol, candles, context)
            if inspect.isawaitable(signal):
                signal = await signal
            if signal is not None:
                await self.handle_signal(signal, candle)
        except Exception as exc:                       # noqa: BLE001
            print(f"[runner] strategy {getattr(strat, 'name', '?')} on {symbol} "
                  f"raised {exc!r}")

    # --- the signal pipeline (webinfo.txt 4) --------------------------------
    async def handle_signal(self, signal, candle):
        """Section-4 pipeline. ORDER MATTERS:

          0. Dedupe against last_alert FIRST — a redelivered/gap-backfilled candle
             must not re-alert or re-write the same setup. (This is the ONLY reason
             the write is skipped; it is unrelated to the FastAPI call.)
          1. Call FastAPI POST /internal/signal FIRST, with a STRICT 1.5s timeout.
             Best-effort: success/timeout/error are all captured, never raised.
          2. Write the signal to Neon UNCONDITIONALLY (telegram_pending = true),
             regardless of the FastAPI outcome.
          3. Update the last_alert dedupe fingerprint to point at this signal.
          4. Log + count any failed/timed-out FastAPI call (strategy, symbol, ts).
             On confirmed success, clear telegram_pending so the retry sweep won't
             re-send; on failure, leave it true for the sweep to retry."""
        alert_key = str(candle["open_time"])           # fingerprint = closed-candle open time

        # 0. DEDUPE -----------------------------------------------------------
        last = await db.get_last_alert(signal.strategy, signal.symbol)
        if last and last["alert_key"] == alert_key and last["side"] == signal.side:
            print(f"[dedupe] {signal.strategy} {signal.symbol} {signal.side} "
                  f"key={alert_key} already alerted — skipping")
            return

        # 1. FASTAPI FIRST (strict timeout, best-effort) ----------------------
        #    `delivered` is True only when Telegram actually sent (not merely 2xx).
        payload = {**asdict(signal), "alert_key": alert_key}
        delivered = await self._post_to_fastapi(payload)

        # 2. UNCONDITIONAL DB WRITE (telegram_pending defaults true) -----------
        #    Reached no matter what step 1 returned — success, timeout, or error.
        signal_id = await db.insert_signal(signal)

        # 3. DEDUPE RECORD updated to this signal's fingerprint ----------------
        await db.upsert_last_alert(signal.strategy, signal.symbol, signal.side,
                                   signal_id, alert_key)

        # 4. Clear the pending flag ONLY on confirmed Telegram delivery; otherwise
        #    leave it true and log+count so the sweep retries it. (On the live call
        #    the row didn't exist yet, so FastAPI couldn't clear it — the Engine
        #    does it here now that it has the id.)
        if delivered:
            await db.clear_telegram_pending(signal_id)
        else:
            self.fastapi_failures += 1
            ts = datetime.now(timezone.utc).isoformat()
            print(f"[delivery-fail #{self.fastapi_failures}] strategy={signal.strategy} "
                  f"symbol={signal.symbol} side={signal.side} at={ts} — FastAPI "
                  f"unreachable or Telegram failed; signal STILL written "
                  f"(id={signal_id}, telegram_pending=true; sweep will retry)")
        print(f"[signal] id={signal_id} {signal.strategy} {signal.symbol} "
              f"{signal.side} entry={signal.entry} sl={signal.stop_loss} "
              f"tp={signal.take_profit} delivered={delivered}")

    async def _post_to_fastapi(self, payload) -> bool:
        """POST one signal payload to FastAPI with the strict timeout and report
        whether Telegram DELIVERY was confirmed.

        Returns True only when the call returns 2xx AND the response body's
        `telegram` flag is true — i.e. Telegram actually sent. A 2xx with
        telegram=false (FastAPI up but the Telegram API failed) returns False, so
        the signal stays telegram_pending and the sweep retries it. Any
        timeout/connection/HTTP error also returns False and is NEVER propagated
        (the DB write must not be blocked by it)."""
        try:
            async with httpx.AsyncClient(timeout=FASTAPI_TIMEOUT) as client:
                resp = await client.post(
                    f"{FASTAPI_INTERNAL_URL}/internal/signal", json=payload)
                resp.raise_for_status()
                body = resp.json()
            return bool(body.get("telegram"))
        except Exception as exc:                       # noqa: BLE001
            print(f"[fastapi-call] {payload.get('strategy')} {payload.get('symbol')}: "
                  f"{exc!r}")
            return False

    # --- telegram retry sweep (webinfo.txt 4.2) -----------------------------
    async def telegram_retry_sweep(self):
        """BACKGROUND TASK. On Engine startup and then every SWEEP_INTERVAL seconds,
        query Neon for signals still flagged telegram_pending = true and re-attempt
        delivery by POSTing each to FastAPI's /internal/signal again. On a confirmed
        Telegram send FastAPI clears telegram_pending by signal_id itself (this
        function does NOT clear it) — so a row only leaves the pending set once
        Telegram has actually sent. A Telegram alert missed because FastAPI was down
        when the signal first fired is therefore RETRIED later, not silently lost."""
        while True:
            try:
                pending = await db.get_pending_telegram_signals()
                if pending:
                    print(f"[sweep] {len(pending)} pending signal(s); re-attempting")
                for s in pending:
                    # signal_id lets FastAPI clear telegram_pending by id on a
                    # confirmed re-send (the row already exists on this path).
                    payload = {
                        "strategy": s["strategy"], "symbol": s["symbol"],
                        "side": s["side"], "entry": float(s["entry"]),
                        "stop_loss": float(s["stop_loss"]),
                        "take_profit": float(s["take_profit"]), "reason": s["reason"],
                        "signal_id": s["id"], "alert_key": str(s["id"]),
                    }
                    if await self._post_to_fastapi(payload):
                        # FastAPI cleared telegram_pending; just log the delivery.
                        print(f"[sweep] re-delivered signal id={s['id']}")
                    else:
                        print(f"[sweep] signal id={s['id']} still undelivered; "
                              f"will retry next sweep")
            except Exception as exc:                   # noqa: BLE001
                print(f"[sweep] error: {exc!r}")
            await asyncio.sleep(SWEEP_INTERVAL)


# ===========================================================================
# NODE BACKEND KEEP-ALIVE
# ===========================================================================
async def node_keepalive():
    """Ping the Render-hosted Node backend every KEEPALIVE_INTERVAL seconds so
    the free-tier service never reaches the 15-minute inactivity threshold and
    sleeps. Uses the cheapest available endpoint (root path). On failure it
    just logs — a cold start is handled gracefully by the bias-fetch fallback
    in each strategy."""
    # Wait one full interval before the first ping so startup logs aren't noisy.
    await asyncio.sleep(KEEPALIVE_INTERVAL)
    ping_url = f"{NODE_BACKEND_URL}/"
    while True:
        try:
            async with httpx.AsyncClient(timeout=KEEPALIVE_TIMEOUT) as client:
                resp = await client.get(ping_url)
            print(f"[keepalive] Node backend OK ({resp.status_code})")
        except Exception as exc:           # noqa: BLE001
            print(f"[keepalive] ping failed — server may be cold-starting: {exc!r}")
        await asyncio.sleep(KEEPALIVE_INTERVAL)


# ===========================================================================
# ENTRYPOINT
# ===========================================================================
async def main():
    """Init DB pool, discover strategies, then run the Data Manager and the
    Runner's telegram-retry sweep concurrently. The Data Manager calls the
    Runner on every closed candle (DataManager.on_closed -> Runner.on_closed_candle)."""
    await db.init_pool()
    strategies = discover_strategies()
    if not strategies:
        print("No strategies discovered in strategies/ — nothing to subscribe to.")
        await db.close_pool()
        return
    keep_n = subscriptions_from_strategies(strategies)
    print(f"Discovered {len(strategies)} strateg(y/ies); "
          f"{len(keep_n)} (symbol, interval) feed(s):")
    for (symbol, interval), n in keep_n.items():
        print(f"  {symbol} {interval}  keep {n}")
    runner = Runner(strategies)
    dm = DataManager(keep_n, on_closed=runner.on_closed_candle)
    try:
        await asyncio.gather(dm.run(), runner.telegram_retry_sweep(), node_keepalive())
    finally:
        await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
