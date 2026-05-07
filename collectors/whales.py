"""
Whale activity collector.

Free sources:
  - Mempool.space (recent large mempool TXs — limited but free)
  - Blockchain.info recent large transactions

For real whale tracking (Arkham, Nansen, Whale Alert) you need paid APIs.
We focus on blockchain.info free queries for large recent transfers as a
basic proxy.

Note: This is the WEAKEST collector right now without paid feeds.
For meaningful whale data, sign up for:
  - Whale Alert (whale-alert.io) — free tier 7 alerts/day
  - Arkham Intelligence — free with usage limits
  - CryptoQuant — netflows have a free tier
"""
import logging
from datetime import datetime
from typing import List

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


def collect() -> List[Signal]:
    if not CONFIG.ENABLE_WHALES:
        return []
    signals = []
    now = datetime.utcnow()

    # ---- Free proxy: large transactions in mempool ----
    # The unconfirmed-transactions endpoint returns recent activity.
    # Response shape can vary — sometimes returns a dict with 'txs',
    # sometimes a list directly, sometimes an error string.
    txs_data = http_get(f"{CONFIG.BLOCKCHAIN_INFO}/unconfirmed-transactions",
                        params={'format': 'json'})

    # Defensive parsing — handle multiple response shapes gracefully
    tx_list = None
    if isinstance(txs_data, dict) and 'txs' in txs_data:
        tx_list = txs_data['txs']
    elif isinstance(txs_data, list):
        tx_list = txs_data

    if tx_list and isinstance(tx_list, list):
        large_count = 0
        total_value_btc = 0.0
        for tx in tx_list[:100]:
            if not isinstance(tx, dict):
                continue
            outputs = tx.get('out', [])
            if not isinstance(outputs, list):
                continue
            value_satoshis = 0
            for out in outputs:
                if not isinstance(out, dict):
                    continue
                v = out.get('value', 0)
                try:
                    value_satoshis += int(v)
                except (ValueError, TypeError):
                    continue
            value_btc = value_satoshis / 100_000_000
            total_value_btc += value_btc
            if value_btc > 100:
                large_count += 1

        signals.append(Signal(
            source='blockchain.info', category='whales',
            name='recent_mempool_large_tx_count',
            raw_value=large_count,
            score=0.0,  # direction unknown without context
            confidence=Confidence.LOW, timestamp=now,
            meta={'total_value_btc_recent_100': round(total_value_btc, 2)},
        ))
    else:
        logger.debug("Whales: blockchain.info returned unexpected shape, skipping")

    return signals