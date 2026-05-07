"""
BTC Forecaster — main entry point.

Runs all enabled collectors, feeds signals into the engine, persists the
forecast, and alerts.

Usage:
    python main.py                       # one-shot forecast
    python main.py --loop                # continuous (every hour)
    python main.py --loop --interval 1800  # every 30 minutes
    python main.py --quiet               # no Telegram alert this run
"""
# Optional: load .env file if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env loading is optional

import argparse
import logging
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

from signals import Signal
from config import CONFIG
from collectors import (
    technicals, derivatives, onchain, sentiment, news, macro, whales,
    dominance, options,
)
from engine import forecast as engine
import storage.db as db
import alerter
import interpreter


logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


# ============================================================
# COLLECTOR REGISTRY
# ============================================================
COLLECTORS = {
    'technicals': technicals.collect,
    'derivatives': derivatives.collect,
    'dominance': dominance.collect,
    'options': options.collect,
    'onchain': onchain.collect,
    'sentiment': sentiment.collect,
    'news': news.collect,
    'macro': macro.collect,
    'whales': whales.collect,
}


def run_collectors_parallel(max_workers: int = 6) -> List[Signal]:
    """Fire all collectors concurrently."""
    all_signals = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fn): name for name, fn in COLLECTORS.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                sigs = fut.result()
                all_signals.extend(sigs)
                logger.info(f"  [{name}] {len(sigs)} signals")
            except Exception as e:
                logger.error(f"  [{name}] failed: {e}", exc_info=True)
    return all_signals


def run_forecast_cycle(quiet: bool = False, dry_run: bool = False) -> dict:
    """One full forecast cycle: collect → synthesize → persist → alert."""
    cycle_start = datetime.utcnow()
    logger.info("=" * 70)
    logger.info(f"Forecast cycle start @ {cycle_start.isoformat()}")

    # 1) Run all collectors
    logger.info("Running collectors...")
    signals = run_collectors_parallel()
    logger.info(f"Collected {len(signals)} signals")

    # 2) Get current BTC price for the forecast object
    btc_price = technicals.get_current_price() or 0.0

    # 3) Synthesize forecast
    forecast = engine.synthesize(signals, btc_price)

    # 4) Print structured forecast
    print_forecast(forecast, signals)

    # 5) LLM interpretation (best-effort; never blocks on failure)
    interpretation = interpreter.interpret(forecast, signals)
    if interpretation:
        interpreter.print_interpretation(interpretation)

    # 6) Persist
    if not dry_run:
        forecast_id = db.save_forecast(forecast, signals,
                                       interpretation=interpretation)
        logger.info(f"Saved forecast id={forecast_id}")
        db.export_latest_forecast_to_json("latest_forecast.json")

    # 7) Alert
    if not quiet and not dry_run and CONFIG.TELEGRAM_BOT_TOKEN:
        msg = alerter.format_forecast_message(forecast,
                                              interpretation=interpretation)
        alerter.send(msg)

    duration = (datetime.utcnow() - cycle_start).total_seconds()
    logger.info(f"Cycle complete in {duration:.1f}s")
    return {
        'forecast': forecast,
        'signals': signals,
        'interpretation': interpretation,
        'duration_s': duration,
    }


def print_forecast(forecast, signals):
    """Pretty-print the forecast and contributing signals."""
    print()
    print("=" * 90)
    print(f"BTC FORECAST — {forecast.timestamp.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 90)
    print(f"BTC Price:      ${forecast.btc_price:,.2f}")
    print(f"Direction:      {forecast.direction.value.upper()} "
          f"({forecast.horizon_hours}h horizon)")
    print(f"Composite:      {forecast.composite:+.3f}  (range -1 to +1)")
    print(f"Confidence:     {forecast.confidence.value}")
    print(f"Agreement:      {forecast.agreement_pct:.0f}% of signals aligned")
    print(f"Signal count:   {forecast.signal_count}")
    print("-" * 90)

    if forecast.key_drivers:
        print("KEY DRIVERS:")
        for d in forecast.key_drivers[:6]:
            print(f"  {d['contribution']:+.3f}  "
                  f"[{d['category']:<11}] {d['name']:<28}  "
                  f"value={d['value'][:40]}")

    if forecast.contradictions:
        print("\nCONTRADICTIONS:")
        for c in forecast.contradictions[:3]:
            print(f"  {c['contribution']:+.3f}  "
                  f"[{c['category']:<11}] {c['name']:<28}  "
                  f"value={c['value'][:40]}")

    print("-" * 90)

    # Group signals by category for visibility
    by_cat = {}
    for s in signals:
        by_cat.setdefault(s.category, []).append(s)
    print("ALL SIGNALS BY CATEGORY:")
    for cat in sorted(by_cat.keys()):
        sigs = by_cat[cat]
        print(f"  [{cat}] ({len(sigs)} signals)")
        for s in sigs:
            arrow = ('↑' if s.score > 0.05
                     else '↓' if s.score < -0.05 else '·')
            value_str = str(s.raw_value)[:35]
            print(f"    {arrow} {s.name:<30} score={s.score:+.2f}  "
                  f"conf={s.confidence.value:<6}  val={value_str}")

    print("=" * 90)
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--loop', action='store_true',
                        help='Run continuously (default every hour)')
    parser.add_argument('--interval', type=int,
                        default=CONFIG.SCAN_INTERVAL_SECONDS,
                        help='Loop interval in seconds')
    parser.add_argument('--quiet', action='store_true',
                        help='Suppress Telegram alerts this run')
    parser.add_argument('--dry-run', action='store_true',
                        help="Don't persist or alert")
    args = parser.parse_args()

    if args.loop:
        logger.info(f"Loop mode ON, interval {args.interval}s")
        while True:
            try:
                run_forecast_cycle(quiet=args.quiet, dry_run=args.dry_run)
                logger.info(f"Sleeping {args.interval}s...")
                time.sleep(args.interval)
            except KeyboardInterrupt:
                logger.info("Stopped by user.")
                break
            except Exception as e:
                logger.error(f"Cycle crashed: {e}", exc_info=True)
                time.sleep(60)
    else:
        run_forecast_cycle(quiet=args.quiet, dry_run=args.dry_run)


if __name__ == "__main__":
    main()