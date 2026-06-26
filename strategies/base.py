"""
strategies/base.py — the fixed interface every drop-in strategy implements.

Defines the two shared shapes referenced throughout webinfo.txt section 3.1:
  * Signal   — what a strategy returns when a setup fires.
  * Strategy — the class interface the Runner discovers and drives.

Logic is intentionally stubbed; only the contract is defined here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Signal:
    """A fired trade setup. Returned by Strategy.on_candle(), or None for no setup."""
    strategy: str
    symbol: str
    side: str          # e.g. 'long' / 'short'
    entry: float
    stop_loss: float
    take_profit: float
    reason: str        # human-readable explanation


class Strategy:
    """
    Base interface for a drop-in strategy.

    A concrete strategy sets these class attributes and implements on_candle:
        name:     unique strategy name
        symbols:  list of symbols it subscribes to, e.g. ['BTCUSDT']
        interval: candle interval it runs on, e.g. '1h'
        lookback: how many recent candles it needs (N)
    """

    name: str = ""
    symbols: list[str] = []
    interval: str = ""
    lookback: int = 0

    def on_candle(self, symbol, candles, context) -> Signal | None:
        """
        Called by the Runner on each CLOSED candle for a subscribed symbol.

          symbol   — the symbol that just closed a candle
          candles  — the latest `lookback` candles (oldest..newest)
          context  — bundle of { trend } read from Neon (the manual trend)

        Returns a Signal if a setup fired, else None. May be either a regular
        method or an `async def` — the Runner awaits the result if it's a
        coroutine (so a strategy can await, e.g., an HTTP bias check).
        """
        raise NotImplementedError
