"""
Options collector — Deribit-based BTC options data.

Deribit is the dominant venue for BTC options (~85% of global BTC options
volume). Their public API requires no auth.

Signals:
  - DVOL (Deribit's volatility index — like VIX for BTC)
  - 25-delta skew (put-call skew at 25 delta)

Skew interpretation:
  Negative skew (puts more expensive than calls) = market hedging downside
    → bearish positioning, often contrarian bullish at extremes
  Positive skew (calls more expensive than puts) = upside speculation
    → bullish positioning, often a sign of FOMO at extremes
"""
import logging
from datetime import datetime
from typing import List

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


def collect() -> List[Signal]:
    if not CONFIG.ENABLE_OPTIONS:
        return []
    signals = []
    now = datetime.utcnow()

    # ---- DVOL ----
    # Deribit historical_volatility endpoint requires currency & start/end.
    # The simpler endpoint for current vol is index_price_volatility:
    dvol = http_get(
        f"{CONFIG.DERIBIT}/get_book_summary_by_currency",
        params={'currency': 'BTC', 'kind': 'option'},
    )
    # That returns the option book; for DVOL we'd want a separate endpoint.
    # Public endpoint that does work for vol index:
    vol_index = http_get(
        f"{CONFIG.DERIBIT}/get_volatility_index_data",
        params={
            'currency': 'BTC',
            'start_timestamp': int((now.timestamp() - 86400) * 1000),
            'end_timestamp': int(now.timestamp() * 1000),
            'resolution': '3600',  # 1h
        },
    )
    if vol_index and 'result' in vol_index and vol_index['result'].get('data'):
        candles = vol_index['result']['data']
        if candles:
            # Each entry: [timestamp, open, high, low, close]
            latest = candles[-1]
            dvol_value = float(latest[4])

            # DVOL > 80 = panic (often near a low)
            # DVOL < 40 = complacency (caution near tops)
            # 50-65 = normal
            if dvol_value > 80:
                score = 0.4   # panic vol = often local bottom
            elif dvol_value > 70:
                score = 0.2
            elif dvol_value < 35:
                score = -0.3  # complacency
            elif dvol_value < 45:
                score = -0.1
            else:
                score = 0.0

            signals.append(Signal(
                source='deribit', category='options', name='dvol',
                raw_value=round(dvol_value, 2),
                score=score, confidence=Confidence.MEDIUM, timestamp=now,
            ))

    return signals
