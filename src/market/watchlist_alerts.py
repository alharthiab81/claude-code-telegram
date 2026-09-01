"""Intraday price-move alerts for a fixed watchlist.

Checks each watchlist ticker's % change from the previous close and
fires a Telegram alert the first time it crosses +/- threshold on a
given trading day. State is kept in memory (per-process) so the same
breach doesn't re-alert every polling cycle; it resets automatically
once the date rolls over.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

import structlog

from .finnhub_client import FinnhubClient

logger = structlog.get_logger()


@dataclass
class AlertState:
    """Tracks which (symbol, direction) breaches have already fired today."""

    _fired_today: Dict[str, str] = None  # symbol -> "up" | "down"
    _day: Optional[date] = None

    def __post_init__(self) -> None:
        if self._fired_today is None:
            self._fired_today = {}

    def _reset_if_new_day(self) -> None:
        today = date.today()
        if self._day != today:
            self._day = today
            self._fired_today = {}

    def should_fire(self, symbol: str, direction: str) -> bool:
        self._reset_if_new_day()
        return self._fired_today.get(symbol) != direction

    def mark_fired(self, symbol: str, direction: str) -> None:
        self._reset_if_new_day()
        self._fired_today[symbol] = direction


async def check_watchlist(
    client: FinnhubClient,
    tickers: List[str],
    threshold_pct: float,
    state: AlertState,
) -> List[str]:
    """Return formatted alert messages for any tickers newly past threshold."""
    messages: List[str] = []

    for symbol in tickers:
        try:
            quote = await client.quote(symbol)
        except Exception:
            logger.exception("Watchlist quote failed", symbol=symbol)
            continue

        if not quote:
            continue

        pct_change = quote.get("dp")
        if pct_change is None:
            c, pc = quote.get("c"), quote.get("pc")
            if not c or not pc:
                continue
            pct_change = (c - pc) / pc * 100

        if abs(pct_change) < threshold_pct:
            continue

        direction = "up" if pct_change > 0 else "down"
        if not state.should_fire(symbol, direction):
            continue

        arrow = "🟢🚀" if direction == "up" else "🔴⚠️"
        price = quote.get("c")
        messages.append(
            f"{arrow} *{symbol}* moved {pct_change:+.1f}% "
            f"(now ${price:,.2f}) — past your {threshold_pct:.0f}% alert threshold."
        )
        state.mark_fired(symbol, direction)

    return messages
