"""
On-chain collector — what's happening on the BTC blockchain itself.

Free sources:
  - Mempool.space (mempool size, fees, hashrate)
  - Blockchain.info (network stats)

Paid sources (skipped without key):
  - CryptoQuant (best for exchange flows)
  - Glassnode (deepest analytics)

For exchange flow signal we use a free proxy: tracking known exchange
hot wallets via Blockchain.info would require their pro API. We use
mempool dynamics + miner-to-exchange proxy data where available.
"""
import logging
from datetime import datetime
from typing import List

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


def _mempool_score(mempool_count: int) -> float:
    """
    Mempool congestion is a weak signal — usually means activity, often
    coincides with volatility but not directional by itself. Use as
    confidence modifier only.
    """
    return 0.0


def _hashrate_change_score(change_pct: float) -> float:
    """
    Hashrate up = miners optimistic about future revenue (mildly bullish)
    Hashrate down = miners capitulating (mildly bearish)
    """
    if change_pct > 5:
        return 0.2
    if change_pct < -5:
        return -0.2
    return 0.0


def collect() -> List[Signal]:
    if not CONFIG.ENABLE_ONCHAIN:
        return []
    signals = []
    now = datetime.utcnow()

    # ---- MEMPOOL: pending TX count ----
    # mempool.space can be slow/blocked from some regions (notably Pakistan).
    # Use a short timeout; fall back to blockchain.info if it fails.
    mempool = http_get(f"{CONFIG.MEMPOOL_SPACE}/mempool", timeout=5)
    if mempool and isinstance(mempool, dict) and 'count' in mempool:
        count = int(mempool['count'])
        signals.append(Signal(
            source='mempool.space', category='onchain',
            name='mempool_count', raw_value=count,
            score=_mempool_score(count),
            confidence=Confidence.LOW, timestamp=now,
            meta={'vsize': mempool.get('vsize')},
        ))
    else:
        # Fallback: blockchain.info gives unconfirmed count via a different path
        unconfirmed = http_get(
            f"{CONFIG.BLOCKCHAIN_INFO}/q/unconfirmedcount", timeout=5
        )
        if unconfirmed is not None:
            try:
                count = int(unconfirmed)
                signals.append(Signal(
                    source='blockchain.info', category='onchain',
                    name='mempool_count', raw_value=count,
                    score=_mempool_score(count),
                    confidence=Confidence.LOW, timestamp=now,
                ))
            except (ValueError, TypeError):
                pass

    # ---- FEES (mempool.space — short timeout) ----
    fees = http_get(f"{CONFIG.MEMPOOL_SPACE}/v1/fees/recommended", timeout=5)
    if fees and isinstance(fees, dict) and 'fastestFee' in fees:
        signals.append(Signal(
            source='mempool.space', category='onchain',
            name='fee_fastest_satvb', raw_value=int(fees['fastestFee']),
            score=0.0, confidence=Confidence.LOW, timestamp=now,
            meta=fees,
        ))

    # ---- HASHRATE TREND (mempool.space) ----
    # blockchain.info /q/hashrate is dead (returns 404). mempool.space
    # gives a 1-month history, so we can score the *trend* (miner
    # capitulation vs expansion) instead of logging a meaningless level.
    hr = http_get(f"{CONFIG.MEMPOOL_SPACE}/v1/mining/hashrate/1m", timeout=5)
    if hr and isinstance(hr, dict) and hr.get('hashrates'):
        try:
            series = [float(p['avgHashrate']) for p in hr['hashrates']
                      if p.get('avgHashrate')]
            if len(series) >= 2 and series[0] > 0:
                change_pct = (series[-1] - series[0]) / series[0] * 100
                signals.append(Signal(
                    source='mempool.space', category='onchain',
                    name='hashrate_30d_change_pct',
                    raw_value=round(change_pct, 2),
                    score=_hashrate_change_score(change_pct),
                    confidence=Confidence.LOW, timestamp=now,
                    meta={'current_hashrate': hr.get('currentHashrate')},
                ))
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Hashrate parse error: {e}")

    # ---- DIFFICULTY ----
    diff = http_get(f"{CONFIG.BLOCKCHAIN_INFO}/q/getdifficulty")
    if diff is not None:
        try:
            difficulty = float(diff)
            signals.append(Signal(
                source='blockchain.info', category='onchain',
                name='difficulty', raw_value=difficulty,
                score=0.0, confidence=Confidence.LOW, timestamp=now,
            ))
        except (ValueError, TypeError):
            pass

    # ---- EXCHANGE NETFLOW (Coinglass — paid but free tier exists) ----
    if CONFIG.COINGLASS_API_KEY:
        flow = http_get(
            f"{CONFIG.COINGLASS_V3}/spot/orderbook/ask-bids-history",
            params={'symbol': 'BTC', 'exchange': 'Binance', 'interval': '1h'},
            headers={'CG-API-KEY': CONFIG.COINGLASS_API_KEY},
        )
        if flow and isinstance(flow, dict) and flow.get('code') == '0':
            signals.append(Signal(
                source='coinglass', category='onchain',
                name='exchange_flow_data',
                raw_value='available',
                score=0.0,
                confidence=Confidence.LOW, timestamp=now,
                meta={'note': 'extend interpretation in collector'},
            ))

    return signals