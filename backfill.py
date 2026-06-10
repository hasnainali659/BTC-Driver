"""
Historical backfill — builds a leak-free training dataset from free history.

Instead of waiting months at 1 live forecast/hour, this replays the
collectors' logic over years of free historical data:

  - Binance spot 1h klines (full history)          -> all technical features
  - Binance futures 1h klines (full history)       -> basis
  - Binance funding rate history (full history)    -> funding + 30d z-score
  - alternative.me Fear & Greed (full history)     -> sentiment
  - Deribit DVOL index (history from ~2021)        -> options vol
  - Yahoo daily closes (SPX, gold, DXY)            -> macro

Output (per 4h-aligned timestamp):
  1. Feature matrix row (raw features, model inputs)
  2. Forward returns at 1h/4h/24h + direction label   (targets)
  3. Rule-engine replay (composite + direction the CURRENT engine would
     have produced) — so the existing rules can be benchmarked instantly.

Saved to logs/feature_matrix.csv and the `feature_matrix` table in the
SQLite DB.

Usage:
    python backfill.py                  # 730 days
    python backfill.py --days 1095     # 3 years
    python backfill.py --threshold 0.5 # different neutral band for labels
"""
import argparse
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from config import CONFIG
from http_util import http_get
from signals import Signal, Confidence, Direction
from engine import forecast as engine
import features as F

from collectors.technicals import (
    rsi_score, macd_score, dist_high_score, taker_delta_score,
    TREND_SCORES, MA50_SCORES, MA200_SCORES,
)
from collectors.derivatives import _funding_score, basis_score
from collectors.sentiment import _fng_score
from collectors.options import dvol_score
from collectors.macro import dxy_5d_score, spx_5d_score, gold_5d_score

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

KLINE_COLS = ['time', 'open', 'high', 'low', 'close', 'volume', 'close_time',
              'qvol', 'trades', 'tbbav', 'tbqav', 'ignore']
NUMERIC_COLS = ['open', 'high', 'low', 'close', 'volume', 'qvol',
                'tbbav', 'tbqav']
PAUSE_S = 0.35   # be polite between paginated calls


# ============================================================
# PAGINATED FETCHERS
# ============================================================
def fetch_klines_history(interval: str, start_ms: int, end_ms: int,
                         futures: bool = False) -> pd.DataFrame:
    """Paginated kline fetch (spot or USDT-M futures)."""
    base = (f"{CONFIG.BINANCE_FUTURES}/fapi/v1/klines" if futures
            else f"{CONFIG.BINANCE_SPOT}/api/v3/klines")
    limit = 1500 if futures else 1000
    frames = []
    cursor = start_ms
    while cursor < end_ms:
        data = http_get(base, params={
            'symbol': 'BTCUSDT', 'interval': interval,
            'startTime': cursor, 'endTime': end_ms, 'limit': limit,
        })
        if not data:
            break
        df = pd.DataFrame(data, columns=KLINE_COLS)
        frames.append(df)
        last_open = int(df['time'].iloc[-1])
        if len(data) < limit:
            break
        cursor = last_open + 1
        time.sleep(PAUSE_S)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).drop_duplicates(subset='time')
    for c in NUMERIC_COLS:
        out[c] = pd.to_numeric(out[c], errors='coerce')
    out['time'] = pd.to_datetime(out['time'].astype(np.int64), unit='ms')
    return out.sort_values('time').reset_index(drop=True)


def fetch_funding_history(start_ms: int, end_ms: int) -> Optional[pd.Series]:
    """Binance funding rate history (one value per 8h). Full history is free."""
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        data = http_get(f"{CONFIG.BINANCE_FUTURES}/fapi/v1/fundingRate",
                        params={'symbol': 'BTCUSDT', 'startTime': cursor,
                                'limit': 1000})
        if not data:
            break
        rows.extend(data)
        last = int(data[-1]['fundingTime'])
        if len(data) < 1000 or last >= end_ms:
            break
        cursor = last + 1
        time.sleep(PAUSE_S)
    if not rows:
        return None
    df = pd.DataFrame(rows).drop_duplicates(subset='fundingTime')
    s = pd.Series(
        pd.to_numeric(df['fundingRate'], errors='coerce').values,
        index=pd.to_datetime(df['fundingTime'].astype(np.int64), unit='ms'),
    ).sort_index()
    return s


def fetch_fng_history() -> Optional[pd.Series]:
    """Full daily Fear & Greed history from alternative.me (limit=0)."""
    data = http_get(CONFIG.ALTERNATIVE_FNG, params={'limit': 0})
    if not data or 'data' not in data:
        return None
    rows = [(pd.to_datetime(int(d['timestamp']), unit='s'), int(d['value']))
            for d in data['data']]
    s = pd.Series({ts: v for ts, v in rows}).sort_index()
    return s


def fetch_dvol_history(start_ms: int, end_ms: int) -> Optional[pd.Series]:
    """Deribit DVOL hourly history, chunked ~28 days per call."""
    chunk_ms = 28 * 24 * 3600 * 1000
    vals = {}
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(cursor + chunk_ms, end_ms)
        data = http_get(f"{CONFIG.DERIBIT}/get_volatility_index_data",
                        params={'currency': 'BTC',
                                'start_timestamp': cursor,
                                'end_timestamp': chunk_end,
                                'resolution': '3600'})
        if data and 'result' in data and data['result'].get('data'):
            for ts, _o, _h, _l, close in data['result']['data']:
                vals[pd.to_datetime(int(ts), unit='ms')] = float(close)
        cursor = chunk_end
        time.sleep(PAUSE_S)
    if not vals:
        return None
    return pd.Series(vals).sort_index()


def fetch_yahoo_daily(symbol: str, days: int) -> Optional[pd.Series]:
    """Daily closes from Yahoo's free chart endpoint."""
    rng = '5y' if days > 660 else '2y'
    data = http_get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={'range': rng, 'interval': '1d'},
        headers={'User-Agent': 'Mozilla/5.0'},
    )
    try:
        result = data['chart']['result'][0]
        ts = result['timestamp']
        closes = result['indicators']['quote'][0]['close']
        s = pd.Series(closes, index=pd.to_datetime(ts, unit='s')).dropna()
        # Normalize to date boundaries (timestamps are exchange-local opens)
        s.index = s.index.normalize()
        return s[~s.index.duplicated(keep='last')].sort_index()
    except (TypeError, KeyError, IndexError):
        return None


# ============================================================
# ALIGNMENT HELPERS
# ============================================================
def asof_align(series: Optional[pd.Series], index: pd.DatetimeIndex,
               max_staleness: Optional[pd.Timedelta] = None) -> pd.Series:
    """Last-known-value alignment of a series onto a target index
    (leak-free: only values with timestamp <= t are used at t)."""
    if series is None or series.empty:
        return pd.Series(np.nan, index=index)
    s = series.reindex(series.index.union(index)).ffill().reindex(index)
    if max_staleness is not None:
        # Blank out values whose source point is too old
        src_times = pd.Series(series.index, index=series.index)
        src_aligned = src_times.reindex(
            series.index.union(index)).ffill().reindex(index)
        stale = (index - pd.DatetimeIndex(src_aligned)) > max_staleness
        s[np.asarray(stale)] = np.nan
    return s


# ============================================================
# RULE-ENGINE REPLAY
# ============================================================
TREND_NAME = {1: 'up', -1: 'down', 0: 'sideways'}


def replay_rule_engine(feat: pd.DataFrame) -> pd.DataFrame:
    """
    For each historical row, build the Signal list the live collectors
    would have produced (using the SAME score functions) and run the
    current engine. Returns composite + direction per row.
    """
    composites, directions = [], []

    def sig(name, cat, score, conf):
        return Signal(source='backfill', category=cat, name=name,
                      raw_value=None, score=score, confidence=conf)

    H, M, L = Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW

    for row in feat.itertuples():
        signals = []

        def have(v):
            return v is not None and not (isinstance(v, float) and np.isnan(v))

        if have(row.trend_4h):
            signals.append(sig('trend_4h', 'technical',
                               TREND_SCORES[TREND_NAME[int(row.trend_4h)]], H))
        if have(row.trend_1d):
            signals.append(sig('trend_1d', 'technical',
                               TREND_SCORES[TREND_NAME[int(row.trend_1d)]], H))
        if have(row.rsi_4h):
            signals.append(sig('rsi_4h', 'technical', rsi_score(row.rsi_4h), H))
        if have(row.rsi_1d):
            signals.append(sig('rsi_1d', 'technical', rsi_score(row.rsi_1d), H))
        if have(row.macd_hist_4h) and have(row.macd_hist_4h_prev):
            signals.append(sig('macd_4h', 'technical',
                               macd_score(row.macd_hist_4h,
                                          row.macd_hist_4h_prev), M))
        if have(row.ma_50d):
            s = MA50_SCORES[0] if row.close > row.ma_50d else MA50_SCORES[1]
            signals.append(sig('above_50d_ma', 'technical', s, H))
        if have(row.ma_200d):
            s = MA200_SCORES[0] if row.close > row.ma_200d else MA200_SCORES[1]
            signals.append(sig('above_200d_ma', 'technical', s, H))
        if have(row.dist_30d_high_pct):
            signals.append(sig('dist_30d_high_pct', 'technical',
                               dist_high_score(row.dist_30d_high_pct), M))
        if have(row.taker_ratio_4h):
            signals.append(sig('taker_delta_4h', 'technical',
                               taker_delta_score(row.taker_ratio_4h), M))
        if have(row.taker_ratio_24h):
            signals.append(sig('taker_delta_24h', 'technical',
                               taker_delta_score(row.taker_ratio_24h), H))
        if have(row.funding_rate):
            signals.append(sig('funding_rate', 'derivatives',
                               _funding_score(row.funding_rate), H))
        if have(row.basis_pct):
            signals.append(sig('basis_pct', 'derivatives',
                               basis_score(row.basis_pct), M))
        if have(row.fng):
            signals.append(sig('fear_greed_index', 'sentiment',
                               _fng_score(int(row.fng)), M))
        if have(row.dvol):
            signals.append(sig('dvol', 'options', dvol_score(row.dvol), M))
        if have(row.spx_5d_chg_pct):
            signals.append(sig('spx_5d_change_pct', 'macro',
                               spx_5d_score(row.spx_5d_chg_pct), L))
        if have(row.gold_5d_chg_pct):
            signals.append(sig('gold_5d_change_pct', 'macro',
                               gold_5d_score(row.gold_5d_chg_pct), L))
        if have(row.dxy_5d_chg_pct):
            signals.append(sig('dxy_5d_change_pct', 'macro',
                               dxy_5d_score(row.dxy_5d_chg_pct), M))

        fc = engine.synthesize(signals, row.close)
        composites.append(fc.composite)
        directions.append(fc.direction.value)

    out = pd.DataFrame(index=feat.index)
    out['rule_composite'] = composites
    out['rule_direction'] = directions
    return out


# ============================================================
# SUMMARY / BENCHMARK
# ============================================================
def print_summary(feat: pd.DataFrame, threshold: float):
    from term import paint, pct_color, CYAN, GREEN, YELLOW, RED, DIM, BOLD
    n = len(feat)
    matured = feat.dropna(subset=['fwd_ret_4h_pct'])
    print()
    print(paint("=" * 78, CYAN))
    print(paint(f"FEATURE MATRIX: {n} rows "
                f"({feat.index.min()} -> {feat.index.max()}, 4h grid)",
                CYAN, bold=True))
    print(paint("=" * 78, CYAN))

    lab = matured['label_4h']
    print(f"Label distribution (threshold +/-{threshold}%):  "
          f"up={int((lab == 1).sum())}  "
          f"down={int((lab == -1).sum())}  "
          f"neutral={int((lab == 0).sum())}")

    # Rule-engine benchmark on directional calls only
    d = matured[matured['rule_direction'].isin(['bullish', 'bearish'])]
    print(f"\nRule engine made a directional call on "
          f"{len(d)}/{len(matured)} rows "
          f"({len(d) / max(len(matured), 1) * 100:.0f}%)")
    if len(d) > 0:
        pred_up = d['rule_direction'] == 'bullish'
        hit = np.where(pred_up, d['fwd_ret_4h_pct'] > 0,
                       d['fwd_ret_4h_pct'] < 0)
        base_up = (d['fwd_ret_4h_pct'] > 0).mean()
        always_up_acc = max(base_up, 1 - base_up)
        persist = np.sign(d['ret_4h_pct_back'])
        persist_hit = np.where(persist > 0, d['fwd_ret_4h_pct'] > 0,
                               np.where(persist < 0,
                                        d['fwd_ret_4h_pct'] < 0, False))
        acc = hit.mean() * 100
        edge = (hit.mean() - max(always_up_acc, persist_hit.mean())) * 100
        edge_col = GREEN if edge > 1 else (YELLOW if edge > -1 else RED)
        print(f"  Rule directional accuracy:   "
              f"{paint(f'{acc:.1f}%', pct_color(acc, 56, 50), bold=True)}")
        print(f"  Baseline (best constant):    {always_up_acc * 100:.1f}%")
        print(f"  Baseline (persistence):      {persist_hit.mean() * 100:.1f}%")
        print(f"  {paint(f'EDGE vs best baseline:       {edge:+.1f} pts', edge_col, bold=True)}")

    # Spearman via ranks (avoids the scipy dependency)
    ic = matured['rule_composite'].rank().corr(
        matured['fwd_ret_4h_pct'].rank())
    ic_col = GREEN if ic > 0.03 else (YELLOW if ic > 0 else RED)
    print(f"\nInformation coefficient (spearman composite vs fwd 4h ret): "
          f"{paint(f'{ic:+.4f}', ic_col, bold=True)}")
    print(paint("  (>+0.03 is meaningful for a 4h horizon; ~0 means the "
                "composite", DIM))
    print(paint("   has no predictive ordering and weights need retraining)",
                DIM))
    print(paint("=" * 78, CYAN))


# ============================================================
# MAIN
# ============================================================
def run(days: int, threshold: float):
    end = datetime.utcnow()
    start = end - timedelta(days=days + F.WARMUP_DAYS)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    logger.info(f"Backfilling {days}d (+{F.WARMUP_DAYS}d warm-up) "
                f"from {start.date()} to {end.date()}")

    # ---- Spot klines (required) ----
    logger.info("Fetching spot 1h klines (paginated)...")
    df_1h = fetch_klines_history('1h', start_ms, end_ms)
    if df_1h.empty:
        logger.error("No spot kline data — cannot backfill.")
        return
    df_1h = F.klines_to_close_indexed(df_1h, 1.0)
    logger.info(f"  {len(df_1h)} hourly bars")

    # ---- Price features + targets ----
    logger.info("Building price features (4h grid)...")
    feat = F.build_price_features(df_1h)
    feat = F.add_forward_returns(feat, df_1h, threshold)

    # ---- Futures klines -> basis ----
    logger.info("Fetching futures 1h klines for basis...")
    df_fut = fetch_klines_history('1h', start_ms, end_ms, futures=True)
    if not df_fut.empty:
        df_fut = F.klines_to_close_indexed(df_fut, 1.0)
        fut_close = df_fut['close'].reindex(feat.index)
        spot_close = df_1h['close'].reindex(feat.index)
        feat['basis_pct'] = (fut_close - spot_close) / spot_close * 100
    else:
        logger.warning("  futures klines unavailable; basis_pct = NaN")
        feat['basis_pct'] = np.nan

    # ---- Funding ----
    logger.info("Fetching funding history...")
    funding = fetch_funding_history(start_ms, end_ms)
    feat['funding_rate'] = asof_align(funding, feat.index,
                                      pd.Timedelta(hours=10))
    if funding is not None:
        fz = F.rolling_zscore(funding, 90)  # 90 events = 30 days of 8h funding
        feat['funding_z30d'] = asof_align(fz, feat.index,
                                          pd.Timedelta(hours=10))
    else:
        feat['funding_z30d'] = np.nan

    # ---- Fear & Greed ----
    logger.info("Fetching Fear & Greed history...")
    fng = fetch_fng_history()
    feat['fng'] = asof_align(fng, feat.index, pd.Timedelta(days=3))
    if fng is not None:
        feat['fng_7d_delta'] = asof_align(fng - fng.shift(7), feat.index,
                                          pd.Timedelta(days=3))
    else:
        feat['fng_7d_delta'] = np.nan

    # ---- DVOL ----
    logger.info("Fetching DVOL history (chunked)...")
    dvol = fetch_dvol_history(start_ms, end_ms)
    feat['dvol'] = asof_align(dvol, feat.index, pd.Timedelta(hours=6))
    if dvol is not None:
        feat['dvol_z30d'] = asof_align(F.rolling_zscore(dvol, 720),
                                       feat.index, pd.Timedelta(hours=6))
    else:
        feat['dvol_z30d'] = np.nan

    # ---- Macro (Yahoo daily; shifted 1 day to avoid same-day leakage) ----
    logger.info("Fetching macro daily history (Yahoo)...")
    for sym, col in [('^GSPC', 'spx_5d_chg_pct'), ('GC=F', 'gold_5d_chg_pct'),
                     ('DX-Y.NYB', 'dxy_5d_chg_pct')]:
        s = fetch_yahoo_daily(sym, days + F.WARMUP_DAYS)
        if s is not None:
            chg = s.pct_change(5) * 100
            chg.index = chg.index + pd.Timedelta(days=1)  # known next day
            feat[col] = asof_align(chg, feat.index, pd.Timedelta(days=5))
        else:
            logger.warning(f"  Yahoo {sym} unavailable; {col} = NaN")
            feat[col] = np.nan
        time.sleep(PAUSE_S)

    # ---- Drop warm-up, replay rules ----
    cutoff = feat.index.min() + pd.Timedelta(days=F.WARMUP_DAYS)
    feat = feat[feat.index >= cutoff]
    logger.info(f"Replaying rule engine over {len(feat)} rows...")
    replay = replay_rule_engine(feat)
    feat = feat.join(replay)

    # ---- Save ----
    Path(CONFIG.LOG_DIR).mkdir(parents=True, exist_ok=True)
    csv_path = Path(CONFIG.LOG_DIR) / 'feature_matrix.csv'
    out = feat.reset_index().rename(columns={'index': 'timestamp',
                                             'close_time_idx': 'timestamp'})
    out.to_csv(csv_path, index=False)
    logger.info(f"Saved {csv_path}")

    db_path = Path(CONFIG.LOG_DIR) / CONFIG.DB_PATH
    conn = sqlite3.connect(str(db_path))
    try:
        out_db = out.copy()
        out_db['timestamp'] = out_db['timestamp'].astype(str)
        out_db.to_sql('feature_matrix', conn, if_exists='replace',
                      index=False)
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Saved feature_matrix table in {db_path}")

    print_summary(feat, threshold)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=730,
                        help='Days of history to backfill (default 730)')
    parser.add_argument('--threshold', type=float, default=0.3,
                        help='Neutral band for 4h labels in %% (default 0.3)')
    args = parser.parse_args()
    run(args.days, args.threshold)
