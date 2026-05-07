"""
Technical analysis collector.

Pulls BTC price data from Binance and produces signals for:
  - Multi-timeframe trend (1H, 4H, Daily, Weekly)
  - Momentum (RSI, MACD)
  - Volatility regime (ATR, BB squeeze)
  - Position vs key MAs (50, 200)
  - Distance from recent highs/lows
"""
import logging
import numpy as np
import pandas as pd
from datetime import datetime
from typing import List, Optional

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


# ============================================================
# DATA FETCHING
# ============================================================
def get_klines(interval: str = "1h", limit: int = 500) -> pd.DataFrame:
    """Fetch BTC/USDT klines from Binance."""
    data = http_get(
        f"{CONFIG.BINANCE_SPOT}/api/v3/klines",
        params={'symbol': 'BTCUSDT', 'interval': interval, 'limit': limit},
    )
    if not data:
        return pd.DataFrame()
    cols = ['time', 'open', 'high', 'low', 'close', 'volume', 'close_time',
            'qvol', 'trades', 'tbbav', 'tbqav', 'ignore']
    df = pd.DataFrame(data, columns=cols)
    for c in ['open', 'high', 'low', 'close', 'volume', 'qvol']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    return df


def get_current_price() -> Optional[float]:
    data = http_get(f"{CONFIG.BINANCE_SPOT}/api/v3/ticker/price",
                    params={'symbol': 'BTCUSDT'})
    if data and 'price' in data:
        return float(data['price'])
    return None


# ============================================================
# INDICATORS
# ============================================================
def rsi(series: pd.Series, period: int = 14) -> float:
    """Last RSI value."""
    delta = series.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    down = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / down.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    return float(rsi_val.iloc[-1]) if not rsi_val.empty else 50.0


def macd_state(series: pd.Series) -> dict:
    """Returns last MACD line, signal line, and histogram."""
    ema_fast = series.ewm(span=12).mean()
    ema_slow = series.ewm(span=26).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=9).mean()
    hist = macd - signal
    return {
        'macd': float(macd.iloc[-1]),
        'signal': float(signal.iloc[-1]),
        'hist': float(hist.iloc[-1]),
        'hist_prev': float(hist.iloc[-2]) if len(hist) > 1 else 0.0,
    }


def atr_pct(klines: pd.DataFrame, period: int = 14) -> float:
    """ATR as % of last close."""
    if len(klines) < period + 1:
        return 0.0
    hl = klines['high'] - klines['low']
    hc = (klines['high'] - klines['close'].shift()).abs()
    lc = (klines['low'] - klines['close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr / klines['close'].iloc[-1] * 100)


def bb_squeeze(series: pd.Series, period: int = 20) -> float:
    """
    Bollinger Band width relative to its 100-period average.
    <0.7 = squeeze (low vol, breakout pending). >1.3 = expanded.
    """
    ma = series.rolling(period).mean()
    std = series.rolling(period).std()
    width = (4 * std) / ma  # 2 stds either side
    if len(width.dropna()) < 100:
        return 1.0
    return float(width.iloc[-1] / width.iloc[-100:].mean())


def trend_classification(klines: pd.DataFrame) -> str:
    """Classify trend on a timeframe via short MA slope + position."""
    if len(klines) < 50:
        return 'sideways'
    closes = klines['close']
    ma_20 = closes.rolling(20).mean().iloc[-1]
    ma_50 = closes.rolling(50).mean().iloc[-1]
    last = closes.iloc[-1]
    # Slope of recent 20-bar MA
    ma_20_slope = (ma_20 - closes.rolling(20).mean().iloc[-10]) / closes.rolling(20).mean().iloc[-10] * 100

    if last > ma_20 > ma_50 and ma_20_slope > 0.2:
        return 'up'
    if last < ma_20 < ma_50 and ma_20_slope < -0.2:
        return 'down'
    return 'sideways'


# ============================================================
# COLLECTOR
# ============================================================
def collect() -> List[Signal]:
    """Compute all technical signals from BTC price data."""
    if not CONFIG.ENABLE_TECHNICALS:
        return []
    signals = []
    now = datetime.utcnow()

    # Fetch data on three relevant timeframes
    kl_1h = get_klines('1h', 500)
    kl_4h = get_klines('4h', 200)
    kl_1d = get_klines('1d', 250)

    if kl_1h.empty:
        logger.error("No BTC kline data — skipping technicals")
        return []

    price = float(kl_1h['close'].iloc[-1])

    # ---- TREND ON MULTIPLE TIMEFRAMES ----
    for tf, kl in [('4h', kl_4h), ('1d', kl_1d)]:
        if kl.empty:
            continue
        trend = trend_classification(kl)
        score = {'up': 0.6, 'down': -0.6, 'sideways': 0.0}[trend]
        signals.append(Signal(
            source='binance', category='technical',
            name=f'trend_{tf}',
            raw_value=trend, score=score,
            confidence=Confidence.HIGH,
            timestamp=now, meta={'price': price},
        ))

    # ---- MOMENTUM: RSI ----
    rsi_4h = rsi(kl_4h['close'], 14) if not kl_4h.empty else 50
    rsi_1d = rsi(kl_1d['close'], 14) if not kl_1d.empty else 50

    # RSI scoring: extreme values = mean-reversion bias.
    # Neutral 40-60 = no signal. Trending 30-70 = momentum bias.
    def rsi_score(r: float) -> float:
        if r > 80: return -0.7   # overbought, mean reversion likely
        if r > 70: return -0.3   # extended
        if r > 55: return 0.4    # bullish momentum
        if r > 45: return 0.0    # neutral
        if r > 30: return -0.4   # bearish momentum
        if r > 20: return 0.3    # oversold bounce likely
        return 0.7               # extreme oversold, reversal likely

    signals.append(Signal(
        source='binance', category='technical', name='rsi_4h',
        raw_value=round(rsi_4h, 1), score=rsi_score(rsi_4h),
        confidence=Confidence.HIGH, timestamp=now,
    ))
    signals.append(Signal(
        source='binance', category='technical', name='rsi_1d',
        raw_value=round(rsi_1d, 1), score=rsi_score(rsi_1d),
        confidence=Confidence.HIGH, timestamp=now,
    ))

    # ---- MOMENTUM: MACD ----
    if not kl_4h.empty:
        m = macd_state(kl_4h['close'])
        # Histogram: + and rising = bullish, - and falling = bearish
        if m['hist'] > 0 and m['hist'] > m['hist_prev']:
            macd_score = 0.5
        elif m['hist'] > 0:
            macd_score = 0.2
        elif m['hist'] < 0 and m['hist'] < m['hist_prev']:
            macd_score = -0.5
        else:
            macd_score = -0.2
        signals.append(Signal(
            source='binance', category='technical', name='macd_4h',
            raw_value={'hist': round(m['hist'], 2)},
            score=macd_score, confidence=Confidence.MEDIUM,
            timestamp=now, meta=m,
        ))

    # ---- VOLATILITY: BB Squeeze ----
    if not kl_4h.empty:
        sq = bb_squeeze(kl_4h['close'], 20)
        # Squeeze itself is directionally neutral but predictive of upcoming move.
        # We log it as info; engine can use it for confidence weighting.
        signals.append(Signal(
            source='binance', category='technical', name='bb_squeeze_4h',
            raw_value=round(sq, 2), score=0.0,
            confidence=Confidence.LOW, timestamp=now,
            meta={'interpretation': 'squeeze' if sq < 0.7 else (
                'expanded' if sq > 1.3 else 'normal'
            )},
        ))

    # ---- KEY LEVELS: distance from MAs ----
    if not kl_1d.empty and len(kl_1d) >= 200:
        ma_50 = float(kl_1d['close'].tail(50).mean())
        ma_200 = float(kl_1d['close'].tail(200).mean())
        # Above key MAs = structurally bullish
        ma50_score = 0.4 if price > ma_50 else -0.4
        ma200_score = 0.6 if price > ma_200 else -0.6
        signals.append(Signal(
            source='binance', category='technical', name='above_50d_ma',
            raw_value=price > ma_50, score=ma50_score,
            confidence=Confidence.HIGH, timestamp=now,
            meta={'price': price, 'ma_50': ma_50},
        ))
        signals.append(Signal(
            source='binance', category='technical', name='above_200d_ma',
            raw_value=price > ma_200, score=ma200_score,
            confidence=Confidence.HIGH, timestamp=now,
            meta={'price': price, 'ma_200': ma_200},
        ))

    # ---- KEY LEVELS: distance from 30d high/low ----
    if not kl_1h.empty and len(kl_1h) >= 720:  # 30 days hourly
        recent_high = float(kl_1h['high'].tail(720).max())
        recent_low = float(kl_1h['low'].tail(720).min())
        # Distance from high (negative = below high): bearish if far below
        dist_high_pct = (price - recent_high) / recent_high * 100
        dist_low_pct = (price - recent_low) / recent_low * 100

        # If we're <2% from a 30d high, breakout potential
        # If >5% off, exhaustion signal
        if dist_high_pct > -2:
            high_score = 0.4
        elif dist_high_pct > -5:
            high_score = 0.0
        elif dist_high_pct > -10:
            high_score = -0.3
        else:
            high_score = -0.5

        signals.append(Signal(
            source='binance', category='technical', name='dist_30d_high_pct',
            raw_value=round(dist_high_pct, 2), score=high_score,
            confidence=Confidence.MEDIUM, timestamp=now,
            meta={'30d_high': recent_high, '30d_low': recent_low,
                  'dist_low_pct': round(dist_low_pct, 2)},
        ))

    # ---- VOLATILITY: ATR ----
    atr = atr_pct(kl_1h.tail(48), 14)
    # Vol itself is direction-neutral but feeds confidence.
    # Very high vol = unpredictable; very low vol = squeeze pending.
    signals.append(Signal(
        source='binance', category='technical', name='atr_1h_pct',
        raw_value=round(atr, 3), score=0.0,
        confidence=Confidence.LOW, timestamp=now,
        meta={'regime': 'high' if atr > 1.5 else (
            'low' if atr < 0.3 else 'normal'
        )},
    ))

    return signals
