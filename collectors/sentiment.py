"""
Sentiment collector.

Free sources:
  - Alternative.me Fear & Greed Index

Optional paid:
  - LunarCrush (social momentum, Galaxy Score, AltRank)
"""
import logging
from datetime import datetime
from typing import List

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


def _fng_score(fng: int) -> float:
    """
    Fear & Greed index is a CONTRARIAN indicator at extremes.
    0-25 (extreme fear) = bullish (buy fear)
    25-45 (fear) = mild bullish
    45-55 (neutral) = no signal
    55-75 (greed) = mild bearish (caution)
    75-100 (extreme greed) = bearish (sell greed)
    """
    if fng < 20:
        return 0.7
    if fng < 35:
        return 0.4
    if fng < 50:
        return 0.1
    if fng < 60:
        return -0.1
    if fng < 75:
        return -0.4
    return -0.7


def collect() -> List[Signal]:
    if not CONFIG.ENABLE_SENTIMENT:
        return []
    signals = []
    now = datetime.utcnow()

    # ---- FEAR & GREED INDEX ----
    fng_data = http_get(CONFIG.ALTERNATIVE_FNG, params={'limit': 7})
    if fng_data and 'data' in fng_data and fng_data['data']:
        latest = fng_data['data'][0]
        fng_value = int(latest['value'])
        fng_class = latest['value_classification']

        # Compute 7d delta if we have history
        delta = None
        if len(fng_data['data']) >= 7:
            week_ago = int(fng_data['data'][6]['value'])
            delta = fng_value - week_ago

        signals.append(Signal(
            source='alternative.me', category='sentiment',
            name='fear_greed_index',
            raw_value=fng_value,
            score=_fng_score(fng_value),
            confidence=Confidence.MEDIUM, timestamp=now,
            meta={'classification': fng_class, '7d_delta': delta},
        ))

    # ---- LUNARCRUSH (optional, paid) ----
    if CONFIG.LUNARCRUSH_API_KEY:
        lc = http_get(
            "https://lunarcrush.com/api4/public/coins/btc/v1",
            headers={'Authorization': f'Bearer {CONFIG.LUNARCRUSH_API_KEY}'},
        )
        if lc and isinstance(lc, dict) and 'data' in lc:
            try:
                d = lc['data']
                galaxy = float(d.get('galaxy_score', 50))
                # Galaxy Score: 0-100, higher = healthier sentiment
                score = (galaxy - 50) / 50  # -1 to +1 mapped
                signals.append(Signal(
                    source='lunarcrush', category='sentiment',
                    name='galaxy_score', raw_value=galaxy,
                    score=score, confidence=Confidence.MEDIUM,
                    timestamp=now,
                    meta={'altrank': d.get('alt_rank')},
                ))
            except (KeyError, ValueError, TypeError):
                pass

    return signals
