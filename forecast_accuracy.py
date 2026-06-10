"""
Forecast accuracy evaluator — honest edition.

What changed vs the original:
  - Outcomes are resolved INCREMENTALLY and persisted (no more 42-day
    fetch window silently dropping old forecasts). main.py also calls
    resolve_outcomes() each cycle.
  - Accuracy is reported AGAINST BASELINES (always-up / persistence).
    Raw hit rate is meaningless; edge = accuracy minus best baseline.
  - Per-class precision/recall, accuracy by confidence tier (with a
    monotonicity check), information coefficient, and a pseudo-Brier
    score for calibration.
  - "Neutral" forecasts are reported separately and can't game the
    headline number.

Usage:
    python forecast_accuracy.py
    python forecast_accuracy.py --threshold 0.5
"""
import argparse
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config import CONFIG
from term import paint, pct_color, direction_color, confidence_color, \
    GREEN, RED, YELLOW, CYAN, DIM, BOLD

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def _db_path() -> Path:
    return Path(CONFIG.LOG_DIR) / CONFIG.DB_PATH


def _fetch_history(start_ms: int, end_ms: int) -> pd.DataFrame:
    """Paginated 1h kline history (reuses backfill's fetcher)."""
    from backfill import fetch_klines_history
    return fetch_klines_history('1h', start_ms, end_ms)


# ============================================================
# INCREMENTAL OUTCOME RESOLUTION
# ============================================================
def resolve_outcomes(threshold_pct: float = 0.3, quiet: bool = False) -> int:
    """
    Fill in actual_btc_price_at_horizon / actual_change_pct /
    forecast_correct for every matured, unresolved forecast.
    Returns the number of rows resolved. Safe to call every cycle.
    """
    db = _db_path()
    if not db.exists():
        return 0
    conn = sqlite3.connect(str(db))
    try:
        rows = pd.read_sql_query("""
            SELECT id, timestamp, horizon_hours, direction, btc_price
            FROM forecasts
            WHERE actual_btc_price_at_horizon IS NULL
        """, conn, parse_dates=['timestamp'])
        if rows.empty:
            return 0

        now = datetime.utcnow()
        rows['target'] = rows['timestamp'] + pd.to_timedelta(
            rows['horizon_hours'], unit='h')
        matured = rows[rows['target'] <= now]
        if matured.empty:
            return 0

        start = matured['target'].min() - timedelta(hours=2)
        klines = _fetch_history(int(start.timestamp() * 1000),
                                int(now.timestamp() * 1000))
        if klines.empty:
            if not quiet:
                logger.warning("resolve_outcomes: no kline data available")
            return 0
        # Index by bar close time; close of bar ending at/after target
        k = klines.set_index(
            klines['time'] + pd.Timedelta(hours=1)).sort_index()

        resolved = 0
        for r in matured.itertuples():
            future = k[k.index >= r.target]
            if future.empty or not r.btc_price:
                continue
            actual = float(future.iloc[0]['close'])
            chg = (actual - float(r.btc_price)) / float(r.btc_price) * 100
            if chg > threshold_pct:
                actual_dir = 'bullish'
            elif chg < -threshold_pct:
                actual_dir = 'bearish'
            else:
                actual_dir = 'neutral'
            correct = int(r.direction == actual_dir)
            conn.execute("""
                UPDATE forecasts
                SET actual_btc_price_at_horizon = ?,
                    actual_change_pct = ?,
                    forecast_correct = ?
                WHERE id = ?
            """, (actual, round(chg, 4), correct, int(r.id)))
            resolved += 1
        conn.commit()
        if resolved and not quiet:
            logger.info(f"Resolved outcomes for {resolved} forecast(s)")
        return resolved
    finally:
        conn.close()


# ============================================================
# EVALUATION
# ============================================================
def evaluate(threshold_pct: float = 0.3):
    db = _db_path()
    if not db.exists():
        print("No forecast database found yet. Run main.py first.")
        return

    resolve_outcomes(threshold_pct)

    conn = sqlite3.connect(str(db))
    rdf = pd.read_sql_query("""
        SELECT timestamp, horizon_hours, direction, composite, confidence,
               btc_price, actual_btc_price_at_horizon AS actual_price,
               actual_change_pct
        FROM forecasts
        WHERE actual_btc_price_at_horizon IS NOT NULL
        ORDER BY timestamp ASC
    """, conn, parse_dates=['timestamp'])
    conn.close()

    if rdf.empty:
        print("No matured forecasts to evaluate yet "
              "(wait `horizon_hours` after each forecast).")
        return

    # Prior 4h return at forecast time (for the persistence baseline)
    start = rdf['timestamp'].min() - timedelta(hours=8)
    end = rdf['timestamp'].max() + timedelta(hours=1)
    klines = _fetch_history(int(start.timestamp() * 1000),
                            int(end.timestamp() * 1000))
    rdf['prior_4h_chg'] = np.nan
    if not klines.empty:
        k = klines.set_index(
            klines['time'] + pd.Timedelta(hours=1))['close'].sort_index()

        def prior_ret(ts):
            past = k[k.index <= ts]
            if len(past) < 5:
                return np.nan
            return (past.iloc[-1] / past.iloc[-5] - 1) * 100
        rdf['prior_4h_chg'] = rdf['timestamp'].map(prior_ret)

    chg = rdf['actual_change_pct'].astype(float)
    thr = threshold_pct
    rdf['actual_dir'] = np.select([chg > thr, chg < -thr],
                                  ['bullish', 'bearish'], default='neutral')

    H = "=" * 78
    print()
    print(paint(H, CYAN))
    print(paint(f"FORECAST ACCURACY — {len(rdf)} matured forecasts "
                f"(threshold ±{thr}%)", CYAN, bold=True))
    print(paint(H, CYAN))

    # ---- Directional forecasts vs baselines ----
    d = rdf[rdf['direction'].isin(['bullish', 'bearish'])].copy()
    print(paint("\n## Directional calls (the only ones that matter)",
                BOLD))
    print(f"Made on {len(d)}/{len(rdf)} forecasts "
          f"({len(d) / len(rdf) * 100:.0f}%)")
    if len(d) >= 5:
        d_chg = d['actual_change_pct'].astype(float)
        hit = np.where(d['direction'] == 'bullish', d_chg > 0, d_chg < 0)
        acc = float(hit.mean()) * 100

        p_up = float((d_chg > 0).mean())
        const_acc = max(p_up, 1 - p_up) * 100

        prior = d['prior_4h_chg'].astype(float)
        persist_hit = np.where(prior > 0, d_chg > 0,
                               np.where(prior < 0, d_chg < 0, False))
        persist_acc = float(np.nanmean(
            persist_hit[~prior.isna()])) * 100 if prior.notna().any() else 50.0

        best_base = max(const_acc, persist_acc)
        edge = acc - best_base
        print(f"  Directional accuracy:     "
              f"{paint(f'{acc:.1f}%', pct_color(acc, 56, 50), bold=True)}")
        print(f"  Baseline (best constant): {const_acc:.1f}%")
        print(f"  Baseline (persistence):   {persist_acc:.1f}%")
        edge_col = GREEN if edge > 1 else (YELLOW if edge > -1 else RED)
        print(f"  {paint(f'EDGE vs best baseline:    {edge:+.1f} pts', edge_col, bold=True)}")
    else:
        print(paint("  Too few directional calls to score yet (need >=5).",
                    DIM))

    # ---- Per-class precision / recall ----
    print(paint("\n## Per-class precision / recall "
                f"(labels at ±{thr}%)", BOLD))
    for cls in ['bullish', 'bearish', 'neutral']:
        pred = rdf['direction'] == cls
        actual = rdf['actual_dir'] == cls
        prec = (pred & actual).sum() / pred.sum() * 100 if pred.sum() else np.nan
        rec = (pred & actual).sum() / actual.sum() * 100 if actual.sum() else np.nan
        cls_txt = paint(f"{cls:<8}", direction_color(cls))
        prec_txt = f"{prec:5.1f}%" if not np.isnan(prec) else "  n/a "
        rec_txt = f"{rec:5.1f}%" if not np.isnan(rec) else "  n/a "
        print(f"  {cls_txt} precision={prec_txt}  recall={rec_txt}  "
              f"(predicted {pred.sum()}, actual {actual.sum()})")

    # ---- Confusion matrix ----
    print(paint("\n## Confusion matrix (forecast -> actual)", BOLD))
    cm = pd.crosstab(rdf['direction'], rdf['actual_dir'],
                     margins=True, margins_name='Total')
    print(cm.to_string())

    # ---- By confidence tier (monotonicity check) ----
    print(paint("\n## Directional accuracy by confidence tier", BOLD))
    tier_accs = {}
    for conf in ['high', 'medium', 'low']:
        sub = d[d['confidence'] == conf] if len(d) else pd.DataFrame()
        if len(sub) >= 3:
            s_chg = sub['actual_change_pct'].astype(float)
            s_hit = np.where(sub['direction'] == 'bullish',
                             s_chg > 0, s_chg < 0)
            tier_accs[conf] = float(s_hit.mean()) * 100
            print(f"  {paint(f'{conf:<7}', confidence_color(conf))} "
                  f"{tier_accs[conf]:5.1f}%  (n={len(sub)})")
        else:
            print(f"  {paint(f'{conf:<7}', confidence_color(conf))} "
                  f"{paint(f'n={len(sub)} — too few', DIM)}")
    if len(tier_accs) >= 2:
        ordered = [tier_accs.get(c) for c in ['high', 'medium', 'low']
                   if c in tier_accs]
        if all(ordered[i] >= ordered[i + 1] for i in range(len(ordered) - 1)):
            print(paint("  OK: accuracy rises with confidence — "
                        "the tiers mean something.", GREEN))
        else:
            print(paint("  WARNING: confidence tiers are NOT monotonic — "
                        "the confidence model is broken; don't size by it.",
                        RED, bold=True))

    # ---- Composite quality: IC + pseudo-Brier ----
    print(paint("\n## Composite score quality", BOLD))
    # Spearman via ranks (avoids the scipy dependency)
    ic = rdf['composite'].astype(float).rank().corr(chg.rank())
    ic_col = GREEN if ic > 0.03 else (YELLOW if ic > 0 else RED)
    print(f"  Information coefficient (spearman): "
          f"{paint(f'{ic:+.4f}', ic_col, bold=True)}"
          f"{paint('  (>+0.03 meaningful at 4h)', DIM)}")
    p = np.clip(0.5 + rdf['composite'].astype(float) / 2, 0.0, 1.0)
    y = (chg > 0).astype(float)
    brier = float(((p - y) ** 2).mean())
    b_col = GREEN if brier < 0.245 else (YELLOW if brier <= 0.255 else RED)
    print(f"  Pseudo-Brier (composite as prob):   "
          f"{paint(f'{brier:.4f}', b_col, bold=True)}"
          f"{paint('  (0.25 = no skill; lower = better)', DIM)}")

    # ---- Neutral honesty ----
    neu = rdf[rdf['direction'] == 'neutral']
    if len(neu) >= 3:
        print(paint("\n## Neutral forecasts", BOLD))
        print(f"  {len(neu)} neutral calls; avg |move| when neutral: "
              f"{neu['actual_change_pct'].abs().mean():.2f}% "
              f"vs overall {chg.abs().mean():.2f}%")
        print(paint("  (a useful 'neutral' should coincide with smaller "
                    "moves)", DIM))

    # ---- Save ----
    out = Path(CONFIG.LOG_DIR) / 'forecast_accuracy.csv'
    rdf.to_csv(out, index=False)
    print(f"\nFull results: {out}")
    print(paint(H, CYAN))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--threshold', type=float, default=0.3,
                        help='Minimum %% move to count as directional')
    args = parser.parse_args()
    evaluate(args.threshold)
