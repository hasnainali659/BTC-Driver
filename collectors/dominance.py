"""
BTC dominance collector.

Pulls real BTC.D from CoinGecko global endpoint (free, no key).

Interpretation:
  - Rising BTC.D = capital flowing INTO BTC (bullish for BTC, bearish for alts)
  - Falling BTC.D = capital flowing OUT to alts (mildly bearish for BTC,
    bullish for alts)

For BTC forecasting specifically, rising dominance during a bull move is
extremely bullish — capital concentrating in BTC. Falling dominance during
a bull move signals weakness — buyers preferring alts.
"""
import logging
from datetime import datetime
from typing import List

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


def collect() -> List[Signal]:
    if not CONFIG.ENABLE_DOMINANCE:
        return []
    signals = []
    now = datetime.utcnow()

    # CoinGecko global market data (free, no auth)
    data = http_get(f"{CONFIG.COINGECKO}/global")
    if data and 'data' in data:
        d = data['data']
        if 'market_cap_percentage' in d:
            btc_d = float(d['market_cap_percentage'].get('btc', 0))
            eth_d = float(d['market_cap_percentage'].get('eth', 0))

            # Score: dominance level itself
            # >55% is high (BTC season territory, bullish for BTC)
            # 50-55% normal range
            # <45% means alts are dominant (BTC may be lagging)
            if btc_d > 58:
                score = 0.4
            elif btc_d > 53:
                score = 0.2
            elif btc_d < 45:
                score = -0.3
            elif btc_d < 48:
                score = -0.1
            else:
                score = 0.0

            signals.append(Signal(
                source='coingecko', category='dominance',
                name='btc_dominance_pct', raw_value=round(btc_d, 2),
                score=score, confidence=Confidence.MEDIUM, timestamp=now,
                meta={'eth_dominance_pct': round(eth_d, 2)},
            ))

            # Total market cap as supplementary (rising = capital flowing in)
            total_cap = float(d.get('total_market_cap', {}).get('usd', 0))
            change_24h = float(d.get('market_cap_change_percentage_24h_usd', 0))
            score_cap = min(max(change_24h / 5, -1), 1) * 0.5
            signals.append(Signal(
                source='coingecko', category='dominance',
                name='total_mcap_24h_change_pct',
                raw_value=round(change_24h, 2),
                score=score_cap, confidence=Confidence.LOW, timestamp=now,
                meta={'total_mcap_usd': total_cap},
            ))

    return signals
