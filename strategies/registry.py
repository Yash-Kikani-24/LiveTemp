"""
strategies/registry.py — the single source of truth for "what strategies exist".

Both processes read the SAME strategy files off disk through here:
  * the Engine (main.py) calls discover() to get live Strategy instances to run;
  * the API (api.py) calls strategy_meta() to expose the list to the frontend
    (GET /strategies) and to label Telegram alerts.

Because both import from this one module, dropping a new file in strategies/ makes
the strategy appear everywhere — engine, API, Telegram and every frontend page —
with no other edits. Set `name` (+ optional `label`) on the class; that's it.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import strategies as strategies_pkg
from strategies.base import Strategy


def _label_for(strat: Strategy) -> str:
    """The display name for a strategy: its explicit `label`, else a title-cased
    fallback derived from `name` (e.g. 'rsi_4h' -> 'Rsi 4H')."""
    label = (getattr(strat, "label", "") or "").strip()
    if label:
        return label
    # Fallback: turn 'rsi_4h' into a readable form, upper-casing timeframe tokens.
    parts = []
    for tok in str(strat.name).split("_"):
        if tok and tok[0].isdigit():           # '4h' / '1h' -> '4H' / '1H'
            parts.append(tok.upper())
        else:
            parts.append(tok.capitalize())
    return " ".join(parts) or str(strat.name)


def discover() -> list[Strategy]:
    """Auto-import every module in strategies/ and instantiate every concrete
    Strategy subclass found. A bad file is skipped (logged), never fatal."""
    found: list[Strategy] = []
    seen = set()
    for mod_info in pkgutil.iter_modules(strategies_pkg.__path__):
        name = mod_info.name
        if name.startswith("_") or name in ("base", "registry"):
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


def strategy_meta() -> list[dict]:
    """Discover strategies and return JSON-safe metadata for the frontend/API,
    sorted by label for a stable menu order.

    Each entry: { name, label, interval, symbols }.
    """
    meta = [
        {
            "name": s.name,
            "label": _label_for(s),
            "interval": s.interval,
            "symbols": list(getattr(s, "symbols", []) or []),
        }
        for s in discover()
    ]
    meta.sort(key=lambda m: m["label"].lower())
    return meta
