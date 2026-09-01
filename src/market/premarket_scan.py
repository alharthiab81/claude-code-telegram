"""Daily pre-open "optimistic movers" scan.

Ranks a ticker universe by prior-session momentum, freshness of company
news, a lightweight keyword sentiment nudge, and (for the strongest
candidates) analyst recommendation/price-target data — then returns the
top N as a formatted Telegram message.

IMPORTANT DATA CAVEAT: Finnhub's free tier does not provide true live
pre-market tick/volume data for US equities — see finnhub_client.py for
detail. This scan is therefore a best-effort "what's already moving on
real news, and what do analysts think" screen you'd read before the
open, not a live pre-market tape. If a paid real-time feed is added
later, only the quote-fetch step here needs to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import structlog

from .finnhub_client import FinnhubClient

logger = structlog.get_logger()

_POSITIVE_WORDS = {
    "beat",
    "beats",
    "raises",
    "raised",
    "upgrade",
    "upgraded",
    "surge",
    "surges",
    "record",
    "approval",
    "approved",
    "wins",
    "won",
    "soar",
    "soars",
    "rally",
    "strong",
    "outperform",
    "buyback",
    "partnership",
    "expands",
    "breakthrough",
}
_NEGATIVE_WORDS = {
    "misses",
    "miss",
    "downgrade",
    "downgraded",
    "lawsuit",
    "recall",
    "cuts",
    "cut",
    "investigation",
    "plunge",
    "plunges",
    "probe",
    "fraud",
    "bankruptcy",
    "delist",
    "delisted",
    "warning",
    "layoffs",
    "recession",
}

_DEFAULT_UNIVERSE_FILE = Path("config/scan_universe.txt")
_EXAMPLE_UNIVERSE_FILE = Path("config/scan_universe.example.txt")


@dataclass
class NewsHit:
    headline: str
    source: str
    url: str
    published: datetime


@dataclass
class Candidate:
    symbol: str
    pct_change: float
    news: List[NewsHit] = field(default_factory=list)
    sentiment_score: int = 0
    analyst_summary: Optional[str] = None
    analyst_bonus: float = 0.0

    @property
    def score(self) -> float:
        news_bonus = min(len(self.news), 3) * 1.5
        return self.pct_change + news_bonus + self.sentiment_score + self.analyst_bonus


def load_universe(base_dir: Path) -> List[str]:
    """Load the ticker universe, falling back to the bundled example list."""
    path = base_dir / _DEFAULT_UNIVERSE_FILE
    if not path.exists():
        path = base_dir / _EXAMPLE_UNIVERSE_FILE
    if not path.exists():
        logger.warning("No scan universe file found; scan will cover 0 tickers")
        return []

    tickers: List[str] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tickers.extend(line.split())
    # De-dupe, preserve order
    seen = set()
    unique = []
    for t in tickers:
        t = t.upper()
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def _keyword_sentiment(headline: str) -> int:
    words = set(re.findall(r"[a-zA-Z']+", headline.lower()))
    score = len(words & _POSITIVE_WORDS) - len(words & _NEGATIVE_WORDS)
    return max(-2, min(2, score))


async def _scan_one(
    client: FinnhubClient, symbol: str, since: datetime
) -> Optional[Candidate]:
    quote = await client.quote(symbol)
    if not quote or not quote.get("pc"):
        return None

    pct_change = quote.get("dp")
    if pct_change is None:
        c, pc = quote.get("c"), quote.get("pc")
        if not c or not pc:
            return None
        pct_change = (c - pc) / pc * 100

    from_date = since.strftime("%Y-%m-%d")
    to_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_news = await client.company_news(symbol, from_date, to_date)

    hits: List[NewsHit] = []
    for item in raw_news:
        ts = item.get("datetime")
        if not ts:
            continue
        published = datetime.fromtimestamp(ts, tz=timezone.utc)
        if published < since:
            continue
        hits.append(
            NewsHit(
                headline=item.get("headline", ""),
                source=item.get("source", ""),
                url=item.get("url", ""),
                published=published,
            )
        )

    if not hits:
        return None

    hits.sort(key=lambda h: h.published, reverse=True)
    sentiment = sum(_keyword_sentiment(h.headline) for h in hits[:5])

    return Candidate(symbol=symbol, pct_change=pct_change, news=hits, sentiment_score=sentiment)


async def _enrich_with_analyst_data(client: FinnhubClient, candidate: Candidate) -> None:
    trends = await client.recommendation_trends(candidate.symbol)
    target = await client.price_target(candidate.symbol)

    if trends:
        latest = trends[0]
        buy = latest.get("strongBuy", 0) + latest.get("buy", 0)
        hold = latest.get("hold", 0)
        sell = latest.get("sell", 0) + latest.get("strongSell", 0)
        total = buy + hold + sell
        parts = [f"{buy} Buy / {hold} Hold / {sell} Sell"]
        if total:
            bullish_ratio = buy / total
            candidate.analyst_bonus += (bullish_ratio - 0.5) * 4  # -2..+2
    else:
        parts = []

    if target and target.get("targetMean"):
        quote_price = None
        # We don't refetch quote here to save a call; use pct_change-implied
        # price only if needed later. Just show the raw consensus target.
        mean = target["targetMean"]
        parts.append(f"target ${mean:,.2f}")

    if parts:
        candidate.analyst_summary = ", ".join(parts)


async def run_scan(
    client: FinnhubClient,
    base_dir: Path,
    lookback_hours: int = 18,
    top_n: int = 20,
    enrich_top: int = 30,
) -> List[Candidate]:
    """Run the full scan and return the top_n candidates, best first."""
    universe = load_universe(base_dir)
    if not universe:
        return []

    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    candidates: List[Candidate] = []
    for symbol in universe:
        try:
            candidate = await _scan_one(client, symbol, since)
        except Exception:
            logger.exception("Scan failed for symbol", symbol=symbol)
            continue
        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(key=lambda c: c.score, reverse=True)
    top_candidates = candidates[:enrich_top]

    for candidate in top_candidates:
        try:
            await _enrich_with_analyst_data(client, candidate)
        except Exception:
            logger.exception("Analyst enrichment failed", symbol=candidate.symbol)

    top_candidates.sort(key=lambda c: c.score, reverse=True)
    return top_candidates[:top_n]


def format_scan_message(candidates: List[Candidate]) -> str:
    if not candidates:
        return (
            "📊 *Pre-open movers scan*\n\n"
            "No tickers in the scan universe showed fresh news in the lookback "
            "window. Nothing to report this run."
        )

    lines = [
        "📊 *Pre-open movers scan — top optimistic names*",
        "_Ranked by last session's move + fresh news + analyst sentiment. "
        "Not live pre-market tape (see caveat in finnhub_client.py). "
        "Not financial advice._",
        "",
    ]
    for i, c in enumerate(candidates, start=1):
        arrow = "🟢" if c.pct_change >= 0 else "🔴"
        line = f"{i}. *{c.symbol}* {arrow} {c.pct_change:+.1f}%"
        if c.news:
            top_news = c.news[0]
            line += f"\n   📰 {top_news.headline} ({top_news.source})"
        if c.analyst_summary:
            line += f"\n   🧑‍💼 {c.analyst_summary}"
        lines.append(line)

    return "\n".join(lines)
