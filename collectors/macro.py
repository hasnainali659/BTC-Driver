"""
Macro collector — global drivers that affect BTC.

Free sources:
  - FRED API (US Treasury yields, DXY, real yields) — free key
  - Yahoo Finance via yfinance package (DXY, ES futures, GLD, etc)

We hit FRED if key available; fall back to Binance for what we can
(BTC's own price), and a public Yahoo endpoint as last resort.

Signals:
  - DXY direction (5d change)
  - 10Y yield direction
  - 10Y real yield (TIPS) direction
  - SPX / equity futures (risk on/off proxy)
  - Gold (alternative store of value)
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


def _fred_series(series_id: str, days: int = 30) -> Optional[list]:
    """Fetch a FRED series. Returns list of (date, value) or None."""
    if not CONFIG.FRED_API_KEY:
        return None
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)
    data = http_get(
        f"{CONFIG.FRED}/series/observations",
        params={
            'series_id': series_id,
            'api_key': CONFIG.FRED_API_KEY,
            'file_type': 'json',
            'observation_start': start.isoformat(),
            'observation_end': end.isoformat(),
            'sort_order': 'desc',
            'limit': 30,
        },
    )
    if not data or 'observations' not in data:
        return None
    pts = [(o['date'], o['value']) for o in data['observations']
           if o['value'] != '.']
    return pts


def _direction_score(values: list, bull_when_falling: bool = True,
                     min_change_pct: float = 0.5) -> float:
    """
    Score a series direction.
    bull_when_falling: True for things like DXY/yields where falling = bullish for BTC.
    """
    if not values or len(values) < 2:
        return 0.0
    try:
        latest = float(values[0][1])
        old = float(values[-1][1])
        change_pct = (latest - old) / abs(old) * 100 if old != 0 else 0
    except (ValueError, TypeError):
        return 0.0

    if abs(change_pct) < min_change_pct:
        return 0.0

    direction = 1 if change_pct > 0 else -1
    magnitude = min(abs(change_pct) / 3, 1.0)  # cap at 3% for full score
    raw = direction * magnitude
    return -raw if bull_when_falling else raw


def collect() -> List[Signal]:
    if not CONFIG.ENABLE_MACRO:
        return []
    signals = []
    now = datetime.utcnow()

    # ---- FRED-BACKED INDICATORS ----
    # DXY (Trade Weighted Dollar Index — DTWEXBGS, daily)
    if CONFIG.FRED_API_KEY:
        dxy = _fred_series('DTWEXBGS', 30)
        if dxy:
            score = _direction_score(dxy, bull_when_falling=True)
            signals.append(Signal(
                source='fred', category='macro', name='dxy_30d_direction',
                raw_value=float(dxy[0][1]) if dxy else None,
                score=score, confidence=Confidence.MEDIUM, timestamp=now,
                meta={'series': 'DTWEXBGS', 'history_n': len(dxy)},
            ))

        # 10Y nominal yield (DGS10)
        y10 = _fred_series('DGS10', 30)
        if y10:
            score = _direction_score(y10, bull_when_falling=True)
            signals.append(Signal(
                source='fred', category='macro', name='us_10y_yield_direction',
                raw_value=float(y10[0][1]) if y10 else None,
                score=score, confidence=Confidence.MEDIUM, timestamp=now,
            ))

        # 10Y TIPS real yield (DFII10) — most important for risk assets
        real = _fred_series('DFII10', 30)
        if real:
            score = _direction_score(real, bull_when_falling=True)
            signals.append(Signal(
                source='fred', category='macro', name='real_yield_10y_direction',
                raw_value=float(real[0][1]) if real else None,
                score=score, confidence=Confidence.HIGH, timestamp=now,
                meta={'note': 'falling real yields = risk-on, BTC bullish'},
            ))

    # ---- ALTERNATIVE: Yahoo Finance via free chart API ----
    # No API key needed for the v8 chart endpoint
    def yahoo_recent_change(symbol: str, days: int = 5) -> Optional[float]:
        """Returns % change over `days` from Yahoo's free chart endpoint."""
        try:
            data = http_get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={'range': f'{days}d', 'interval': '1d'},
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            if not data or 'chart' not in data:
                return None
            result = data['chart']['result'][0]
            closes = result['indicators']['quote'][0]['close']
            closes = [c for c in closes if c is not None]
            if len(closes) < 2:
                return None
            return (closes[-1] - closes[0]) / closes[0] * 100
        except Exception as e:
            logger.warning(f"Yahoo fetch for {symbol}: {e}")
            return None

    # If FRED unavailable, fall back to Yahoo for DXY (UUP ETF as proxy or DX-Y.NYB)
    if not CONFIG.FRED_API_KEY:
        dxy_change = yahoo_recent_change('DX-Y.NYB', 5)
        if dxy_change is not None:
            # Falling DXY is bullish for BTC
            score = -min(max(dxy_change / 2, -1), 1)  # 2% move = full score
            signals.append(Signal(
                source='yahoo', category='macro', name='dxy_5d_change_pct',
                raw_value=round(dxy_change, 2),
                score=score, confidence=Confidence.MEDIUM, timestamp=now,
            ))

    # SPX (risk-on proxy) — always pull from Yahoo
    spx_change = yahoo_recent_change('^GSPC', 5)
    if spx_change is not None:
        # Rising SPX = risk-on = mildly bullish BTC (correlation regime dependent)
        score = min(max(spx_change / 4, -1), 1)  # 4% move = full score
        signals.append(Signal(
            source='yahoo', category='macro', name='spx_5d_change_pct',
            raw_value=round(spx_change, 2),
            score=score * 0.5,  # weak correlation, halve influence
            confidence=Confidence.LOW, timestamp=now,
        ))

    # Gold — alternative SoV, can be either correlation
    gold_change = yahoo_recent_change('GC=F', 5)
    if gold_change is not None:
        # Currently gold-BTC correlation is regime-dependent.
        # Rising gold often signals dollar weakness which is bullish BTC,
        # but also reflects risk-off which can be bearish BTC. Net: mild positive.
        score = min(max(gold_change / 5, -1), 1) * 0.3
        signals.append(Signal(
            source='yahoo', category='macro', name='gold_5d_change_pct',
            raw_value=round(gold_change, 2),
            score=score, confidence=Confidence.LOW, timestamp=now,
        ))

    return signals
