"""
Forecast accuracy backtester.

For each historical forecast in the DB, fetch the BTC price `horizon_hours`
later and check if the direction was right.

Usage:
    python forecast_accuracy.py
    python forecast_accuracy.py --threshold 0.5   # require 0.5%+ move to count
"""
import argparse
import logging
import sqlite3
from pathlib import Path
import pandas as pd

from collectors.technicals import get_klines
from config import CONFIG

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def evaluate(threshold_pct: float = 0.3):
    """
    Walk through all forecasts, fetch actual price at horizon, score each.

    threshold_pct: minimum % move required to consider a "real" outcome.
                   Below this, we treat the actual outcome as 'neutral'.
    """
    db_path = Path(CONFIG.LOG_DIR) / CONFIG.DB_PATH
    if not db_path.exists():
        print("No forecast database found yet. Run main.py first.")
        return

    conn = sqlite3.connect(str(db_path))
    forecasts = pd.read_sql_query(
        "SELECT * FROM forecasts ORDER BY timestamp ASC",
        conn,
        parse_dates=['timestamp'],
    )
    if forecasts.empty:
        print("No forecasts logged yet.")
        return

    print(f"Evaluating {len(forecasts)} forecasts...\n")

    # Pull recent BTC kline history once (covers the evaluation window)
    klines = get_klines('1h', 1000)  # ~42 days of hourly data
    if klines.empty:
        print("Could not fetch BTC history.")
        return

    klines_indexed = klines.set_index('time').sort_index()

    results = []
    for _, row in forecasts.iterrows():
        ts = row['timestamp']
        horizon_h = int(row['horizon_hours'])
        target_time = ts + pd.Timedelta(hours=horizon_h)

        # Find the kline at or just after target_time
        future = klines_indexed[klines_indexed.index >= target_time]
        if future.empty:
            continue  # forecast hasn't matured yet
        actual_price = float(future.iloc[0]['close'])

        forecast_price = float(row['btc_price'])
        if forecast_price == 0:
            continue

        actual_change = (actual_price - forecast_price) / forecast_price * 100

        # Determine actual outcome direction
        if actual_change > threshold_pct:
            actual_direction = 'bullish'
        elif actual_change < -threshold_pct:
            actual_direction = 'bearish'
        else:
            actual_direction = 'neutral'

        forecast_direction = row['direction']
        correct = forecast_direction == actual_direction

        results.append({
            'timestamp': ts,
            'horizon_h': horizon_h,
            'forecast_direction': forecast_direction,
            'actual_direction': actual_direction,
            'forecast_price': forecast_price,
            'actual_price': actual_price,
            'actual_change_pct': round(actual_change, 2),
            'composite': float(row['composite']),
            'confidence': row['confidence'],
            'correct': correct,
        })

        # Update the row in DB with the realized data
        conn.execute("""
            UPDATE forecasts
            SET actual_btc_price_at_horizon = ?,
                actual_change_pct = ?,
                forecast_correct = ?
            WHERE id = ?
        """, (actual_price, round(actual_change, 4), int(correct), row['id']))

    conn.commit()
    conn.close()

    if not results:
        print("No matured forecasts to evaluate yet "
              "(need to wait `horizon_hours` after each forecast).")
        return

    rdf = pd.DataFrame(results)

    print("=" * 80)
    print(f"FORECAST ACCURACY ({len(rdf)} matured forecasts)")
    print("=" * 80)
    print(f"Outcome threshold: ±{threshold_pct}% change")
    print()

    # Overall hit rate
    overall = rdf['correct'].mean() * 100
    print(f"Overall accuracy:        {overall:.1f}%")

    # Hit rate excluding neutral forecasts (where the bar is low)
    non_neutral = rdf[rdf['forecast_direction'] != 'neutral']
    if len(non_neutral) > 0:
        nn_acc = non_neutral['correct'].mean() * 100
        print(f"Directional accuracy:    {nn_acc:.1f}% "
              f"(excluding neutral forecasts)")

    # Hit rate by confidence
    print("\n=== By forecast confidence ===")
    by_conf = rdf.groupby('confidence').agg(
        count=('correct', 'count'),
        accuracy=('correct', lambda x: x.mean() * 100),
    ).round(1)
    print(by_conf)

    # Hit rate by forecast direction
    print("\n=== By forecast direction ===")
    by_dir = rdf.groupby('forecast_direction').agg(
        count=('correct', 'count'),
        accuracy=('correct', lambda x: x.mean() * 100),
        mean_actual_change=('actual_change_pct', 'mean'),
    ).round(2)
    print(by_dir)

    # Confusion matrix
    print("\n=== Confusion matrix (forecast → actual) ===")
    cm = pd.crosstab(rdf['forecast_direction'], rdf['actual_direction'],
                     margins=True, margins_name='Total')
    print(cm)

    # Save full results
    out = Path(CONFIG.LOG_DIR) / 'forecast_accuracy.csv'
    rdf.to_csv(out, index=False)
    print(f"\nFull results: {out}")

    # Honest interpretation
    print("\n" + "=" * 80)
    print("INTERPRETATION")
    print("=" * 80)
    print("If overall accuracy > 50% on directional forecasts, you have edge.")
    print("If > 60% on high-confidence calls, the confidence filter is working.")
    print("If accuracy is similar across confidence levels, the engine is")
    print("over-confident and weights need tuning in config.py.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--threshold', type=float, default=0.3,
                        help='Minimum % move to count as a directional outcome')
    args = parser.parse_args()
    evaluate(args.threshold)
