"""
Derivatives collector — leveraged positioning across the BTC futures complex.

Pulls from Binance Futures (free) and Coinglass (free tier API key) where useful.

Signals:
  - Funding rate (current and 24h average)
  - Open Interest 24h change
  - Long/Short Ratio (top traders)
  - 24h liquidations (longs vs shorts)
  - Basis (futures vs spot)
"""
import logging
import pandas as pd
from datetime import datetime
from typing import List, Optional

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


def _funding_score(funding_rate: float) -> float:
    """
    Funding rate scoring. NOTE: Binance funding is per 8 HOURS (not hourly);
    the baseline/neutral rate is +0.0001 (+0.01% per 8h). Thresholds are
    centered on that baseline.

    Negative funding = shorts paying longs = squeeze fuel = bullish
    Very positive funding = crowded longs = mean-reversion bearish
    """
    if funding_rate < -0.0003:    # heavy negative (3x baseline inverted)
        return 0.7
    if funding_rate < -0.00005:   # below zero = shorts paying
        return 0.4
    if funding_rate > 0.0010:     # extreme positive (10x baseline)
        return -0.7
    if funding_rate > 0.0005:     # very positive (5x baseline)
        return -0.4
    if funding_rate > 0.0003:     # elevated above baseline
        return -0.1
    return 0.0                    # around baseline 0.0001 = neutral


def _oi_change_score(oi_change_pct: float, price_change_pct: float) -> float:
    """
    OI changes interpreted with price direction:
      OI up + price up = real long buildup (bullish)
      OI up + price down = short buildup (bearish)
      OI down + price up = short covering (bullish but exhausting)
      OI down + price down = long unwind (bearish but exhausting)
    """
    if abs(oi_change_pct) < 2:
        return 0.0
    if oi_change_pct > 5 and price_change_pct > 0.5:
        return 0.5  # strong long buildup
    if oi_change_pct > 5 and price_change_pct < -0.5:
        return -0.5  # short buildup
    if oi_change_pct < -5 and price_change_pct > 0.5:
        return 0.2  # short covering
    if oi_change_pct < -5 and price_change_pct < -0.5:
        return -0.2  # long unwind
    return 0.0


def basis_score(basis_pct: float) -> float:
    """Futures premium >0.1% = bullish positioning; discount = bearish."""
    if basis_pct > 0.15:
        return 0.3
    if basis_pct > 0.05:
        return 0.1
    if basis_pct < -0.15:
        return -0.3
    if basis_pct < -0.05:
        return -0.1
    return 0.0


def _liq_score(long_liq: float, short_liq: float) -> float:
    """
    Heavy long liquidations = bearish flush, often capitulation bottom signal
    Heavy short liquidations = squeeze in progress, often near-term top
    Symmetric = no signal
    """
    if long_liq + short_liq < 1_000_000:
        return 0.0  # no meaningful action
    ratio = long_liq / (long_liq + short_liq)
    if ratio > 0.85:
        return 0.4   # heavy long flush, oversold bounce likely
    if ratio > 0.70:
        return 0.2
    if ratio < 0.15:
        return -0.4  # heavy short squeeze, exhaustion likely
    if ratio < 0.30:
        return -0.2
    return 0.0


# ============================================================
# COLLECTOR
# ============================================================
def collect() -> List[Signal]:
    if not CONFIG.ENABLE_DERIVATIVES:
        return []
    signals = []
    now = datetime.utcnow()

    # ---- FUNDING RATE ----
    fund = http_get(f"{CONFIG.BINANCE_FUTURES}/fapi/v1/premiumIndex",
                    params={'symbol': 'BTCUSDT'},
                    silent_codes=(400, 404))
    if fund and 'lastFundingRate' in fund:
        rate = float(fund['lastFundingRate'])
        signals.append(Signal(
            source='binance_futures', category='derivatives',
            name='funding_rate', raw_value=rate,
            score=_funding_score(rate),
            confidence=Confidence.HIGH, timestamp=now,
            meta={'mark_price': fund.get('markPrice')},
        ))

    # ---- OPEN INTEREST 24h CHANGE ----
    oi_hist = http_get(
        f"{CONFIG.BINANCE_FUTURES}/futures/data/openInterestHist",
        params={'symbol': 'BTCUSDT', 'period': '1h', 'limit': 25},
        silent_codes=(400, 404),
    )
    price_change_24h = 0.0
    if oi_hist and len(oi_hist) >= 2:
        first = float(oi_hist[0]['sumOpenInterestValue'])
        last = float(oi_hist[-1]['sumOpenInterestValue'])
        oi_change_pct = (last - first) / first * 100 if first > 0 else 0
        first_p = float(oi_hist[0]['sumOpenInterest'])
        last_p = float(oi_hist[-1]['sumOpenInterest'])

        # Need price change to interpret OI move
        ticker = http_get(f"{CONFIG.BINANCE_SPOT}/api/v3/ticker/24hr",
                          params={'symbol': 'BTCUSDT'})
        if ticker:
            price_change_24h = float(ticker.get('priceChangePercent', 0))

        signals.append(Signal(
            source='binance_futures', category='derivatives',
            name='oi_change_24h_pct',
            raw_value=round(oi_change_pct, 2),
            score=_oi_change_score(oi_change_pct, price_change_24h),
            confidence=Confidence.HIGH, timestamp=now,
            meta={'oi_value_usd': last, 'price_change_24h': price_change_24h},
        ))

    # ---- TOP TRADER LONG/SHORT RATIO ----
    lsr = http_get(
        f"{CONFIG.BINANCE_FUTURES}/futures/data/topLongShortAccountRatio",
        params={'symbol': 'BTCUSDT', 'period': '1h', 'limit': 1},
        silent_codes=(400, 404),
    )
    if lsr and len(lsr) > 0:
        ratio = float(lsr[0]['longShortRatio'])
        # Top traders heavily long = potentially overcrowded; heavily short = capitulation
        if ratio > 2.5:
            score = -0.3   # too crowded long
        elif ratio > 1.5:
            score = 0.1    # mildly long-biased
        elif ratio < 0.5:
            score = 0.3    # heavily short = squeeze fuel
        elif ratio < 0.7:
            score = 0.1
        else:
            score = 0.0
        signals.append(Signal(
            source='binance_futures', category='derivatives',
            name='top_trader_lsr', raw_value=round(ratio, 2),
            score=score, confidence=Confidence.MEDIUM, timestamp=now,
        ))

    # ---- BASIS (futures vs spot) ----
    spot = http_get(f"{CONFIG.BINANCE_SPOT}/api/v3/ticker/price",
                    params={'symbol': 'BTCUSDT'})
    fut_price = http_get(f"{CONFIG.BINANCE_FUTURES}/fapi/v1/ticker/price",
                         params={'symbol': 'BTCUSDT'})
    if spot and fut_price:
        spot_p = float(spot['price'])
        fut_p = float(fut_price['price'])
        basis_pct = (fut_p - spot_p) / spot_p * 100
        score = basis_score(basis_pct)
        signals.append(Signal(
            source='binance', category='derivatives', name='basis_pct',
            raw_value=round(basis_pct, 4), score=score,
            confidence=Confidence.MEDIUM, timestamp=now,
            meta={'spot': spot_p, 'futures': fut_p},
        ))

    # ---- LIQUIDATIONS (Coinglass — needs free key) ----
    if CONFIG.COINGLASS_API_KEY:
        liq = http_get(
            f"{CONFIG.COINGLASS_V3}/futures/liquidation/v2/history",
            params={'symbol': 'BTC', 'time_type': '24h', 'exchange': 'all'},
            headers={'CG-API-KEY': CONFIG.COINGLASS_API_KEY},
        )
        if liq and isinstance(liq, dict) and 'data' in liq:
            try:
                d = liq['data']
                long_liq = float(d.get('longVolUsd', 0))
                short_liq = float(d.get('shortVolUsd', 0))
                signals.append(Signal(
                    source='coinglass', category='derivatives',
                    name='liquidations_24h_usd',
                    raw_value={'long': long_liq, 'short': short_liq},
                    score=_liq_score(long_liq, short_liq),
                    confidence=Confidence.MEDIUM, timestamp=now,
                ))
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Coinglass liquidations parse error: {e}")

    return signals
