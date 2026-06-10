"""
Leak-free historical feature construction.

Mirrors the live collectors' indicator formulas, but computes each
quantity as a full time series — one value per bar, using ONLY data up
to and including that bar (no lookahead). This lets backfill.py rebuild
years of training rows that match what the live pipeline computes each
cycle.

Conventions:
  - All input DataFrames are indexed by bar CLOSE time (open time +
    interval). The value at index t is fully known at time t.
  - Daily features use only COMPLETED daily candles (no partial-candle
    noise — intentional improvement over the live collector, which
    includes the in-progress candle).

Note on EWM-based indicators (MACD): the live collector computes EWMs
over a 200-bar fetch window; here they run over the full history. Values
converge after ~3x the span, so rows differ only in the first ~100 bars,
which backfill drops as warm-up.
"""
import numpy as np
import pandas as pd


WARMUP_DAYS = 220   # need 200 completed daily candles for the 200d MA


# ============================================================
# RESAMPLING
# ============================================================
def klines_to_close_indexed(df: pd.DataFrame, interval_hours: float) -> pd.DataFrame:
    """Re-index a Binance kline frame (indexed/columned by OPEN time)
    to bar close time."""
    out = df.copy()
    out['close_time_idx'] = out['time'] + pd.Timedelta(hours=interval_hours)
    return out.set_index('close_time_idx').sort_index()


def resample_ohlcv(df_1h: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Resample a close-time-indexed 1h frame to a larger timeframe,
    aligned to UTC boundaries (matches Binance 4h/1d candle alignment).
    Result is indexed by bar close time. The last (incomplete) bar is
    dropped.
    """
    open_indexed = df_1h.copy()
    open_indexed.index = open_indexed.index - pd.Timedelta(hours=1)
    agg = {
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last',
        'volume': 'sum',
    }
    if 'tbbav' in open_indexed.columns:
        agg['tbbav'] = 'sum'
    res = open_indexed.resample(rule, label='left', closed='left').agg(agg)
    res = res.dropna(subset=['close'])
    period = pd.tseries.frequencies.to_offset(rule)
    res.index = res.index + period  # back to close time
    # Drop the final bar if it hasn't fully closed within the data range:
    # a bar closing at T needs 1h data through close time T.
    res = res[res.index <= df_1h.index.max()]
    return res


# ============================================================
# INDICATOR SERIES (formula-identical to collectors/technicals.py)
# ============================================================
def rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).rolling(period).mean()
    down = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd_hist_series(close: pd.Series) -> pd.Series:
    ema_fast = close.ewm(span=12).mean()
    ema_slow = close.ewm(span=26).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=9).mean()
    return macd - signal


def atr_pct_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df['high'] - df['low']
    hc = (df['high'] - df['close'].shift()).abs()
    lc = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr / df['close'] * 100


def bb_squeeze_series(close: pd.Series, period: int = 20) -> pd.Series:
    ma = close.rolling(period).mean()
    std = close.rolling(period).std()
    width = (4 * std) / ma
    return width / width.rolling(100).mean()


def trend_series(close: pd.Series) -> pd.Series:
    """
    Vectorized trend_classification: +1 up / -1 down / 0 sideways.
    Same rule as the collector: price vs MA20 vs MA50 + MA20 slope over
    the last 9 bars (the collector's iloc[-10] reference point).
    """
    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    slope = (ma20 - ma20.shift(9)) / ma20.shift(9) * 100
    up = (close > ma20) & (ma20 > ma50) & (slope > 0.2)
    down = (close < ma20) & (ma20 < ma50) & (slope < -0.2)
    return pd.Series(np.where(up, 1, np.where(down, -1, 0)),
                     index=close.index)


def taker_ratio_series(df: pd.DataFrame, bars: int) -> pd.Series:
    vol = df['volume'].rolling(bars).sum()
    buy = df['tbbav'].rolling(bars).sum()
    return buy / vol.replace(0, np.nan)


def dist_30d_high_series(df_1h: pd.DataFrame) -> pd.Series:
    high_30d = df_1h['high'].rolling(720).max()
    return (df_1h['close'] - high_30d) / high_30d * 100


def rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    """Z-score of each value vs its own trailing window."""
    mean = series.rolling(window).mean()
    std = series.rolling(window).std()
    return (series - mean) / std.replace(0, np.nan)


def pct_change_n_days(daily_close: pd.Series, days: int) -> pd.Series:
    return daily_close.pct_change(days) * 100


# ============================================================
# FEATURE MATRIX
# ============================================================
def build_price_features(df_1h: pd.DataFrame) -> pd.DataFrame:
    """
    Build all price-derived features on a 4h-aligned grid.

    df_1h: 1h klines indexed by close time, with columns
           open/high/low/close/volume/tbbav (numeric).

    Returns a DataFrame indexed by 4h bar close time. Every value in row
    t is computable at time t.
    """
    df_4h = resample_ohlcv(df_1h, '4h')
    df_1d = resample_ohlcv(df_1h, '1D')

    c4, c1d = df_4h['close'], df_1d['close']

    feat = pd.DataFrame(index=df_4h.index)
    feat['close'] = c4

    # 4h-timeframe features
    feat['rsi_4h'] = rsi_series(c4)
    hist4 = macd_hist_series(c4)
    feat['macd_hist_4h'] = hist4
    feat['macd_hist_4h_prev'] = hist4.shift(1)
    # Normalize MACD hist by price so it's comparable across years
    feat['macd_hist_4h_pct'] = hist4 / c4 * 100
    feat['trend_4h'] = trend_series(c4)
    feat['bb_squeeze_4h'] = bb_squeeze_series(c4)

    # Daily-timeframe features (completed candles only).
    # A daily bar closing at t is usable at any 4h close >= t, so an
    # asof-merge with the daily close-time index is leak-free.
    daily = pd.DataFrame(index=df_1d.index)
    daily['rsi_1d'] = rsi_series(c1d)
    daily['trend_1d'] = trend_series(c1d)
    daily['ma_50d'] = c1d.rolling(50).mean()
    daily['ma_200d'] = c1d.rolling(200).mean()
    daily_asof = daily.reindex(
        daily.index.union(feat.index)).ffill().reindex(feat.index)
    feat = feat.join(daily_asof)
    feat['above_50d_ma'] = (feat['close'] > feat['ma_50d']).astype(float)
    feat['above_200d_ma'] = (feat['close'] > feat['ma_200d']).astype(float)
    feat['dist_50d_ma_pct'] = (feat['close'] - feat['ma_50d']) / feat['ma_50d'] * 100
    feat['dist_200d_ma_pct'] = (feat['close'] - feat['ma_200d']) / feat['ma_200d'] * 100

    # 1h-grid features sampled at the 4h closes
    h1 = pd.DataFrame(index=df_1h.index)
    h1['dist_30d_high_pct'] = dist_30d_high_series(df_1h)
    h1['atr_1h_pct'] = atr_pct_series(df_1h, 14)
    h1['taker_ratio_4h'] = taker_ratio_series(df_1h, 4)
    h1['taker_ratio_24h'] = taker_ratio_series(df_1h, 24)
    h1['ret_1h_pct'] = df_1h['close'].pct_change() * 100
    h1['ret_4h_pct_back'] = df_1h['close'].pct_change(4) * 100
    h1['ret_24h_pct_back'] = df_1h['close'].pct_change(24) * 100
    feat = feat.join(h1.reindex(feat.index))

    return feat


def add_forward_returns(feat: pd.DataFrame, df_1h: pd.DataFrame,
                        neutral_threshold_pct: float = 0.3) -> pd.DataFrame:
    """
    Append forward returns + labels. These columns are TARGETS — they use
    future data by definition and must never be used as model inputs.
    """
    close_1h = df_1h['close']
    aligned = close_1h.reindex(feat.index)

    for h, col in [(1, 'fwd_ret_1h_pct'), (4, 'fwd_ret_4h_pct'),
                   (24, 'fwd_ret_24h_pct')]:
        future = close_1h.reindex(feat.index + pd.Timedelta(hours=h))
        future.index = feat.index
        feat[col] = (future / aligned - 1) * 100

    thr = neutral_threshold_pct
    feat['label_4h'] = np.select(
        [feat['fwd_ret_4h_pct'] > thr, feat['fwd_ret_4h_pct'] < -thr],
        [1, -1], default=0)
    feat.loc[feat['fwd_ret_4h_pct'].isna(), 'label_4h'] = np.nan
    return feat
