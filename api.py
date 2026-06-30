"""
api.py — the FastAPI service (Process B). Single Uvicorn worker, no Gunicorn
(webinfo.txt 2.1). Run: uvicorn api:app --host 127.0.0.1 --port 8000

Responsibilities (webinfo.txt 3.3):
  * Receive fired signals from the Engine on an internal-only endpoint, then
    send the Telegram alert and push over WebSocket to every dashboard.
  * On a successful Telegram send, clear the signal's telegram_pending flag.
  * Serve the user's manual trend (read/write).
  * Serve signal history / fallback queries (read-only).

FastAPI NEVER creates signal rows — the Engine owns that write. Its only
signal-table write is clearing telegram_pending.

telegram_pending clearing detail: the Engine calls /internal/signal FIRST and
writes the row AFTER (FastAPI-first ordering, webinfo.txt 2.3/4), so on the live
call the row does not exist yet and carries no id — there is nothing for FastAPI
to clear, and the Engine clears it itself post-write. The retry sweep, however,
re-sends EXISTING pending rows and passes their `signal_id`, so on those calls
FastAPI clears telegram_pending by id on a confirmed Telegram send. Either way,
clearing the flag is the ONLY write FastAPI makes to the signals table.

Reached from the internet only via Nginx (TLS) on /; the Engine calls it
directly on localhost:8000. CORS is locked to the Vercel origin (section 5.5).

All secrets (Telegram token + chat id, database url, CORS origin) come from the
environment — nothing is hardcoded.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from decimal import Decimal
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import db

load_dotenv()

# --- secrets / config (env only) --------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# CORS_ALLOWED_ORIGIN may be a single origin or a comma-separated list (e.g. the
# Vercel origin in prod, plus localhost dev ports). No trailing slashes.
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGIN", "").split(",") if o.strip()
]
TELEGRAM_TIMEOUT = 10.0     # seconds; FastAPI's own call to the Telegram API


def _build_telegram_routes() -> dict[str, dict]:
    """Scan env for TELEGRAM_ROUTE_<STRATEGY>_CHAT_ID at startup.

    e.g. TELEGRAM_ROUTE_CRT_1H_CHAT_ID=-100... maps "crt_1h" -> that chat_id.
    Strategies with no entry fall back to the global TELEGRAM_CHAT_ID.
    """
    routes: dict[str, dict] = {}
    prefix = "TELEGRAM_ROUTE_"
    for key, val in os.environ.items():
        if key.startswith(prefix) and key.endswith("_CHAT_ID") and val.strip():
            seg = key[len(prefix):-len("_CHAT_ID")].lower()
            routes[seg] = {"chat_id": val.strip()}
    return routes


TELEGRAM_ROUTES: dict[str, dict] = _build_telegram_routes()


# --- lifecycle (shared Neon pool) -------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_pool()
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(title="Crypto Strategy Alert API", lifespan=lifespan)

# CORS locked to the single Vercel origin from the env var (section 5.5).
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- JSON serialisation helper ----------------------------------------------
def _ser(row) -> dict:
    """Turn an asyncpg Record into a JSON-safe dict (Decimal -> float, datetime
    -> ISO string)."""
    out = dict(row)
    for k, v in out.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


# ===========================================================================
# WebSocket connection hub
# ===========================================================================
class ConnectionManager:
    """Tracks connected dashboards and broadcasts each fired signal to them."""

    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)

    async def broadcast(self, message: dict) -> int:
        """Send `message` to every live client; drop any that error. Returns the
        number of clients the push reached."""
        sent = 0
        for ws in list(self.active):           # iterate a copy — set may mutate
            try:
                await ws.send_json(message)
                sent += 1
            except Exception:                  # noqa: BLE001 — dead socket, prune it
                self.disconnect(ws)
        return sent


manager = ConnectionManager()


# ===========================================================================
# Telegram
# ===========================================================================

_STRATEGY_LABELS: dict[str, str] = {
    "crt_1h":   "CRT 1H",
    "crt_4h":   "CRT 4H",
    "sweep_4h": "4H Sweep",
}


def _fmt_price(v) -> str:
    try:
        return f"{float(v):,.4f}"
    except (TypeError, ValueError):
        return str(v)


async def send_telegram(payload: dict) -> bool:
    """Send one alert to the per-strategy Telegram chat. Returns True on a confirmed
    send. Never raises — a Telegram failure must not break the request."""
    strategy = str(payload.get("strategy", "")).lower()

    # Resolve destination: per-strategy route first, then global fallback.
    chat_id = (TELEGRAM_ROUTES.get(strategy) or {}).get("chat_id") or TELEGRAM_CHAT_ID

    if not TELEGRAM_BOT_TOKEN or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN or chat_id not set — skipping")
        return False

    symbol   = payload.get("symbol", "")
    side     = str(payload.get("side", "")).lower()
    reason   = payload.get("reason", "")

    strat_label = _STRATEGY_LABELS.get(strategy, strategy.upper())
    dir_label   = "🟢  LONG" if side == "long" else "🔴  SHORT"

    text = (
        f"🔔  {symbol}\n"
        f"\n"
        f"Strategy  :  {strat_label}\n"
        f"Direction :  {dir_label}\n"
        f"\n"
        f"Entry       :  {_fmt_price(payload.get('entry'))}\n"
        f"Stop Loss   :  {_fmt_price(payload.get('stop_loss'))}\n"
        f"Take Profit :  {_fmt_price(payload.get('take_profit'))}\n"
        f"\n"
        f"{'─' * 22}\n"
        f"{reason}"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT) as client:
            resp = await client.post(
                url, json={"chat_id": chat_id, "text": text})
            resp.raise_for_status()
        return True
    except Exception as exc:                   # noqa: BLE001
        print(f"[telegram] send failed: {exc!r}")
        return False


# ===========================================================================
# Routes
# ===========================================================================
@app.post("/internal/signal")
async def internal_signal(payload: dict):
    """Internal-only (Engine -> localhost, bypassing Nginx). Pushes the signal to
    every connected dashboard over WebSocket and sends the Telegram alert. On a
    confirmed Telegram send, clears telegram_pending for the signal IF the payload
    carries its `signal_id` (the retry-sweep path — see module docstring)."""
    # WS push first: it's instant and local, so dashboards get the signal even if
    # the Telegram API is slow.
    ws_clients = await manager.broadcast(payload)
    telegram_ok = await send_telegram(payload)

    cleared = False
    signal_id = payload.get("signal_id") or payload.get("id")
    if telegram_ok and signal_id is not None:
        await db.clear_telegram_pending(signal_id)
        cleared = True

    return {"telegram": telegram_ok, "ws_clients": ws_clients, "cleared": cleared}


@app.websocket("/ws/signals")
async def ws_signals(ws: WebSocket):
    """Real-time signal push channel to connected dashboards. We only push; inbound
    frames are ignored (the receive loop just detects disconnects)."""
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:                          # noqa: BLE001
        manager.disconnect(ws)


@app.post("/trend")
async def set_trend(payload: dict):
    """Upsert the user's manual per-symbol/per-strategy trend and optional R:R."""
    symbol = payload.get("symbol")
    strategy = payload.get("strategy")
    trend = payload.get("trend")
    rr = payload.get("rr")          # optional; stored alongside trend for the strategy to use
    if not symbol or not strategy or trend is None:
        return {"ok": False, "error": "symbol, strategy and trend are required"}
    await db.upsert_trend(symbol, strategy, trend, rr)
    return {"ok": True}


@app.get("/trend")
async def read_trend(symbol: str, strategy: str):
    """Read the user's manual trend for a symbol+strategy (or null)."""
    row = await db.get_trend(symbol, strategy)
    return _ser(row) if row else None


@app.get("/strategy-config")
async def get_strategy_config():
    """Return the enabled state for every strategy.
    Strategies not yet in the table are included with enabled=True (the default)."""
    rows = await db.get_all_strategy_configs()
    stored = {r["strategy"]: {"enabled": bool(r["enabled"]),
                               "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None}
              for r in rows}
    result = []
    for name in _STRATEGY_LABELS:
        if name in stored:
            result.append({"strategy": name, **stored[name]})
        else:
            result.append({"strategy": name, "enabled": True, "updated_at": None})
    return result


@app.post("/strategy-config")
async def set_strategy_config(payload: dict):
    """Toggle a strategy on (enabled=true) or off (enabled=false)."""
    strategy = payload.get("strategy")
    enabled  = payload.get("enabled")
    if not strategy or not isinstance(enabled, bool):
        return {"ok": False, "error": "strategy (str) and enabled (bool) required"}
    await db.set_strategy_enabled(strategy, enabled)
    return {"ok": True, "strategy": strategy, "enabled": enabled}


# (The /bias endpoints were removed: the computed bias is no longer stored in
#  Neon — strategies fetch it live from the Node backend when a setup is found.)


@app.get("/signals")
async def signals_since(since: str | None = None):
    """Recent signals for dashboard initial load / catch-up after a missed push."""
    rows = await db.get_signals_since(since)
    return [_ser(r) for r in rows]


@app.get("/signals/history")
async def signals_history(limit: int = 50, offset: int = 0):
    """Paginated historical setups (indexed on created_at), newest-first."""
    limit = max(1, min(int(limit), 500))      # clamp to a sane page size
    offset = max(0, int(offset))
    rows = await db.get_signals_history(limit, offset)
    return [_ser(r) for r in rows]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
