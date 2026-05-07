"""
News collector.

Free sources:
  - CryptoPanic API (free tier, ~200 reqs/day with key)
  - Coindesk RSS (no key needed)

We use simple keyword-based sentiment for transparency. For ML sentiment,
swap in a HuggingFace pipeline later.
"""
import re
import logging
from datetime import datetime
from typing import List

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


# Bullish/bearish keyword dictionaries (deliberately simple, easy to audit)
BULLISH_KEYWORDS = {
    'rally', 'surge', 'soars', 'breakout', 'all-time high', 'ath',
    'institutional', 'adopt', 'approval', 'etf approval', 'upgrade',
    'partnership', 'integration', 'launches', 'bullish', 'accumulate',
    'inflow', 'buy', 'long', 'rebound', 'recovery', 'support holds',
    'green', 'pump', 'spike higher', 'breakout above',
    'cuts rate', 'rate cut', 'dovish', 'easing', 'stimulus',
}

BEARISH_KEYWORDS = {
    'crash', 'plunge', 'plummet', 'breakdown', 'sell-off', 'sell off',
    'liquidation', 'hack', 'exploit', 'rug', 'fraud', 'investigation',
    'lawsuit', 'sec charges', 'ban', 'crackdown', 'regulatory',
    'bearish', 'dump', 'outflow', 'short', 'capitulation',
    'recession', 'inflation surges', 'hawkish', 'rate hike',
    'rejected', 'denied', 'collapse', 'bankrupt', 'insolvent',
}


def _score_headline(text: str) -> float:
    """Simple keyword-based sentiment. Returns -1 to +1."""
    if not text:
        return 0.0
    lower = text.lower()
    bull = sum(1 for kw in BULLISH_KEYWORDS if kw in lower)
    bear = sum(1 for kw in BEARISH_KEYWORDS if kw in lower)
    if bull == 0 and bear == 0:
        return 0.0
    return (bull - bear) / max(bull + bear, 1)


def _aggregate_news_score(articles: List[dict],
                          max_articles: int = 30) -> tuple:
    """
    Score a list of articles. Returns (avg_score, bull_count, bear_count, sample).
    """
    scored = []
    sample = []
    for art in articles[:max_articles]:
        title = art.get('title') or art.get('description') or ''
        s = _score_headline(title)
        if s != 0:
            scored.append(s)
            sample.append({'title': title[:120], 'score': round(s, 2)})
    if not scored:
        return 0.0, 0, 0, []
    avg = sum(scored) / len(scored)
    bull = sum(1 for s in scored if s > 0)
    bear = sum(1 for s in scored if s < 0)
    # Top 3 most directional
    sample = sorted(sample, key=lambda x: abs(x['score']), reverse=True)[:3]
    return avg, bull, bear, sample


def collect() -> List[Signal]:
    if not CONFIG.ENABLE_NEWS:
        return []
    signals = []
    now = datetime.utcnow()

    # ---- CRYPTOPANIC ----
    cp_articles = []
    if CONFIG.CRYPTOPANIC_API_KEY:
        cp = http_get(
            f"{CONFIG.CRYPTOPANIC}/posts/",
            params={
                'auth_token': CONFIG.CRYPTOPANIC_API_KEY,
                'currencies': 'BTC',
                'kind': 'news',
                'public': 'true',
                'filter': 'hot',
            },
        )
        if cp and 'results' in cp:
            cp_articles = cp['results']

    if cp_articles:
        avg, bull, bear, sample = _aggregate_news_score(cp_articles)
        signals.append(Signal(
            source='cryptopanic', category='news',
            name='crypto_news_sentiment',
            raw_value=round(avg, 3),
            score=avg,  # Already in -1 to +1 range
            confidence=Confidence.MEDIUM, timestamp=now,
            meta={
                'article_count': len(cp_articles),
                'bullish': bull, 'bearish': bear,
                'top_articles': sample,
            },
        ))

    # ---- NEWSAPI MACRO HEADLINES (optional) ----
    if CONFIG.NEWSAPI_API_KEY:
        # Macro/economic news that affects crypto via DXY/yields
        macro = http_get(
            "https://newsapi.org/v2/everything",
            params={
                'q': '(Federal Reserve OR Fed OR inflation OR CPI OR rate cut OR recession)',
                'language': 'en',
                'sortBy': 'publishedAt',
                'pageSize': 30,
                'apiKey': CONFIG.NEWSAPI_API_KEY,
            },
        )
        if macro and 'articles' in macro:
            avg, bull, bear, sample = _aggregate_news_score(macro['articles'])
            signals.append(Signal(
                source='newsapi', category='news',
                name='macro_news_sentiment',
                raw_value=round(avg, 3),
                score=avg,
                confidence=Confidence.LOW,  # macro→BTC mapping is loose
                timestamp=now,
                meta={
                    'article_count': len(macro['articles']),
                    'bullish': bull, 'bearish': bear,
                    'top_articles': sample,
                },
            ))

    # ---- COINDESK RSS (always available, no auth) ----
    # We skip RSS parsing here for now (would require feedparser dep).
    # Easy to add later: feedparser.parse('https://www.coindesk.com/arc/outboundfeeds/rss/')

    return signals
