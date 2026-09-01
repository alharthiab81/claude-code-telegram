"""Minimal async client for the Finnhub REST API (free tier).

Docs: https://finnhub.io/docs/api

Only the endpoints this bot needs are wrapped: real-time-ish quotes,
per-symbol company news, general market news, analyst recommendation
trends, and analyst price targets. All of these are available on
Finnhub's free tier (no credit card required).

Free-tier caveat: Finnhub's free plan does not include true real-time
pre-market/after-hours tick data for US equities — `quote()` reflects
the latest regular-session trade until the next session opens. The
pre-market scanner in `premarket_scan.py` works around this by ranking
candidates on overnight news + prior-session momentum + analyst
sentiment rather than live pre-market tape volume. If you later move to
a paid feed (Polygon.io, IEX Cloud, etc.) with real pre-market data,
only this client needs a new implementation — callers are unaffected.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx
import structlog

logger = structlog.get_logger()

BASE_URL = "https://finnhub.io/api/v1"

# Free tier allows 60 calls/minute. Stay comfortably under that.
_MAX_CALLS_PER_MINUTE = 50


class FinnhubError(Exception):
    """Raised when a Finnhub request fails after retries."""


class _RateLimiter:
    """Simple sliding-window limiter so we never trip Finnhub's free-tier cap."""

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._calls: List[float] = []
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < 60]
            if len(self._calls) >= self._max:
                sleep_for = 60 - (now - self._calls[0]) + 0.05
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < 60]
            self._calls.append(time.monotonic())


class FinnhubClient:
    """Thin, defensive wrapper around the Finnhub REST API."""

    def __init__(self, api_key: str, timeout: float = 10.0) -> None:
        if not api_key:
            raise ValueError("Finnhub API key is required")
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout)
        self._limiter = _RateLimiter(_MAX_CALLS_PER_MINUTE)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FinnhubClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None, retries: int = 2
    ) -> Any:
        params = dict(params or {})
        params["token"] = self._api_key

        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            await self._limiter.wait()
            try:
                resp = await self._client.get(path, params=params)
                if resp.status_code == 429:
                    # Rate-limited despite our own throttling — back off and retry.
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                if 400 <= resp.status_code < 500:
                    # Any other client error (bad symbol, endpoint not included
                    # on this API plan, etc.) won't be fixed by retrying — fail
                    # fast instead of burning the rate-limit budget and the
                    # caller's time on retries that can only ever repeat the
                    # same error.
                    logger.warning(
                        "Finnhub request rejected, not retrying",
                        path=path,
                        status=resp.status_code,
                    )
                    return None
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning(
                    "Finnhub request failed",
                    path=path,
                    attempt=attempt,
                    error=str(exc),
                )
                await asyncio.sleep(0.5 * (attempt + 1))

        logger.error("Finnhub request exhausted retries", path=path, error=str(last_error))
        return None

    async def quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Latest quote snapshot.

        Returns a dict with keys: c (current), d (change), dp (% change),
        h/l/o (day high/low/open), pc (previous close), t (timestamp).
        """
        return await self._get("/quote", {"symbol": symbol})

    async def company_news(
        self, symbol: str, from_date: str, to_date: str
    ) -> List[Dict[str, Any]]:
        """News for one symbol between from_date and to_date (YYYY-MM-DD)."""
        data = await self._get(
            "/company-news",
            {"symbol": symbol, "from": from_date, "to": to_date},
        )
        return data or []

    async def market_news(self, category: str = "general") -> List[Dict[str, Any]]:
        """General market news feed (not symbol-specific)."""
        data = await self._get("/news", {"category": category})
        return data or []

    async def recommendation_trends(self, symbol: str) -> List[Dict[str, Any]]:
        """Analyst buy/hold/sell counts, most recent period first."""
        data = await self._get("/stock/recommendation", {"symbol": symbol})
        return data or []

    async def price_target(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Analyst price target consensus: targetHigh/Low/Mean/Median.

        NOTE: unlike the other endpoints on this client, /stock/price-target
        is gated behind a paid Finnhub plan — free-tier keys get a 403 for
        every call. Kept here for accounts that do have a paid plan, but
        premarket_scan.py deliberately does not call this on the free tier
        (see its _enrich_with_analyst_data).
        """
        return await self._get("/stock/price-target", {"symbol": symbol})
