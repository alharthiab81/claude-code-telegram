"""Wires the pre-market scanner and watchlist alerts into APScheduler.

Deliberately separate from src/scheduler/scheduler.py (the Claude-agent
job scheduler): these jobs are numeric/data checks, not agent prompts, so
running them without an LLM call in the loop keeps them fast, cheap, and
resilient to Claude API hiccups.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]
from telegram import Bot
from telegram.constants import ParseMode

from .finnhub_client import FinnhubClient
from .premarket_scan import format_scan_message, run_scan
from .watchlist_alerts import AlertState, check_watchlist

logger = structlog.get_logger()

_ET = ZoneInfo("America/New_York")


class MarketMonitor:
    """Owns the Finnhub client + two scheduled jobs, and exposes on-demand
    versions of both for the /premarket and /watchlist chat commands."""

    def __init__(
        self,
        bot: Bot,
        finnhub_api_key: str,
        watchlist: List[str],
        alert_threshold_pct: float,
        notify_chat_ids: List[int],
        base_dir: Path,
        premarket_scan_hour_et: int = 8,
        premarket_scan_minute_et: int = 30,
        watchlist_poll_minutes: int = 15,
    ) -> None:
        self._bot = bot
        self._client = FinnhubClient(finnhub_api_key)
        self._watchlist = watchlist
        self._threshold = alert_threshold_pct
        self._notify_chat_ids = notify_chat_ids
        self._base_dir = base_dir
        self._alert_state = AlertState()
        self._scheduler = AsyncIOScheduler()

        self._scheduler.add_job(
            self._run_premarket_scan,
            trigger=CronTrigger(
                hour=premarket_scan_hour_et,
                minute=premarket_scan_minute_et,
                day_of_week="mon-fri",
                timezone=_ET,
            ),
            id="premarket_scan",
            name="Pre-open movers scan",
        )
        self._scheduler.add_job(
            self._run_watchlist_check,
            trigger=IntervalTrigger(minutes=watchlist_poll_minutes),
            id="watchlist_alerts",
            name="Watchlist price-move alerts",
        )

    async def start(self) -> None:
        self._scheduler.start()
        logger.info(
            "Market monitor started",
            watchlist=self._watchlist,
            threshold=self._threshold,
        )

    async def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        await self._client.close()
        logger.info("Market monitor stopped")

    async def _send(self, text: str) -> None:
        for chat_id in self._notify_chat_ids:
            try:
                await self._bot.send_message(
                    chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                logger.exception("Failed to deliver market monitor message", chat_id=chat_id)

    async def _run_premarket_scan(self) -> None:
        logger.info("Running scheduled pre-open movers scan")
        try:
            candidates = await run_scan(self._client, self._base_dir)
            await self._send(format_scan_message(candidates))
        except Exception:
            logger.exception("Pre-open movers scan failed")

    async def _run_watchlist_check(self) -> None:
        if not self._watchlist:
            return
        try:
            messages = await check_watchlist(
                self._client, self._watchlist, self._threshold, self._alert_state
            )
            for msg in messages:
                await self._send(msg)
        except Exception:
            logger.exception("Watchlist alert check failed")

    # --- On-demand entry points for /premarket and /watchlist commands ---

    async def run_premarket_scan_now(self) -> str:
        candidates = await run_scan(self._client, self._base_dir)
        return format_scan_message(candidates)

    async def run_watchlist_check_now(self) -> str:
        if not self._watchlist:
            return "No watchlist tickers configured (set MARKET_WATCHLIST)."

        lines = []
        for symbol in self._watchlist:
            quote = await self._client.quote(symbol)
            if not quote or quote.get("c") is None:
                lines.append(f"*{symbol}*: no data")
                continue
            pct = quote.get("dp")
            if pct is None:
                c, pc = quote.get("c"), quote.get("pc")
                pct = (c - pc) / pc * 100 if c and pc else 0.0
            arrow = "🟢" if pct >= 0 else "🔴"
            lines.append(f"{arrow} *{symbol}*: ${quote.get('c'):,.2f} ({pct:+.1f}%)")
        return "📈 *Watchlist snapshot*\n\n" + "\n".join(lines)
