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
from datetime import datetime, timedelta
from typing import List, Optional

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


def _parse_instrument(name: str) -> Optional[dict]:
    """Parse 'BTC-27JUN26-60000-P' → {expiry, strike, type}."""
    try:
        parts = name.split('-')
        if len(parts) != 4:
            return None
        expiry = datetime.strptime(parts[1], '%d%b%y')
        return {'expiry': expiry, 'strike': float(parts[2]),
                'type': parts[3].upper()}
    except (ValueError, IndexError):
        return None


def _iv_skew_proxy(book: list, now: datetime) -> Optional[dict]:
    """
    Put-call IV skew from the option book summary (mark IVs).

    Picks the expiry nearest 7 days out (2-21d window), then compares
    average mark IV of OTM puts (strikes 85-97% of spot) vs OTM calls
    (strikes 103-115% of spot). Positive skew = puts richer = downside
    hedging demand.
    """
    parsed = []
    for entry in book:
        if not isinstance(entry, dict):
            continue
        info = _parse_instrument(entry.get('instrument_name', ''))
        iv = entry.get('mark_iv')
        spot = entry.get('underlying_price')
        if not info or iv is None or not spot:
            continue
        info.update({'iv': float(iv), 'spot': float(spot)})
        parsed.append(info)
    if not parsed:
        return None

    # Choose expiry closest to 7 days ahead, within 2-21 days
    target = now + timedelta(days=7)
    expiries = {}
    for p in parsed:
        days_out = (p['expiry'] - now).total_seconds() / 86400
        if 2 <= days_out <= 21:
            expiries.setdefault(p['expiry'], []).append(p)
    if not expiries:
        return None
    best_expiry = min(expiries.keys(), key=lambda e: abs(e - target))
    chain = expiries[best_expiry]

    spot = chain[0]['spot']
    put_ivs = [p['iv'] for p in chain if p['type'] == 'P'
               and 0.85 * spot <= p['strike'] <= 0.97 * spot]
    call_ivs = [p['iv'] for p in chain if p['type'] == 'C'
                and 1.03 * spot <= p['strike'] <= 1.15 * spot]
    if not put_ivs or not call_ivs:
        return None

    skew = sum(put_ivs) / len(put_ivs) - sum(call_ivs) / len(call_ivs)
    return {'skew_pts': round(skew, 2),
            'expiry': best_expiry.strftime('%Y-%m-%d'),
            'n_puts': len(put_ivs), 'n_calls': len(call_ivs)}


def _skew_score(skew_pts: float) -> float:
    """
    skew = avg OTM put IV - avg OTM call IV (vol points).
      > +15: panic-level hedging — contrarian bullish
      +5..+15: downside fear — bearish lean
      -5..+5: balanced — no signal
      -15..-5: call demand — bullish positioning
      < -15: FOMO extreme — contrarian bearish
    """
    if skew_pts > 15:
        return 0.3
    if skew_pts > 5:
        return -0.2
    if skew_pts < -15:
        return -0.3
    if skew_pts < -5:
        return 0.2
    return 0.0


def collect() -> List[Signal]:
    if not CONFIG.ENABLE_OPTIONS:
        return []
    signals = []
    now = datetime.utcnow()

    # ---- 25-DELTA SKEW PROXY (from option book mark IVs) ----
    book = http_get(
        f"{CONFIG.DERIBIT}/get_book_summary_by_currency",
        params={'currency': 'BTC', 'kind': 'option'},
    )
    if book and isinstance(book, dict) and isinstance(book.get('result'), list):
        skew_data = _iv_skew_proxy(book['result'], now)
        if skew_data:
            signals.append(Signal(
                source='deribit', category='options', name='iv_skew_proxy',
                raw_value=skew_data['skew_pts'],
                score=_skew_score(skew_data['skew_pts']),
                confidence=Confidence.MEDIUM, timestamp=now,
                meta=skew_data,
            ))

    # ---- DVOL ----
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
