"""
Integration helper — read the latest forecast from the JSON file the
forecaster writes after each cycle.

In the scanner repo, you'd do something like:

    from btc_forecaster_client import load_latest_forecast

    fc = load_latest_forecast("/path/to/btc_forecaster/logs/latest_forecast.json")
    if fc and fc['direction'] == 'bearish' and fc['confidence'] in ('high', 'medium'):
        # stand down
        ...

This decouples the two systems — the scanner doesn't care HOW the forecast
was made, just what the call is.
"""
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict

logger = logging.getLogger(__name__)


def load_latest_forecast(filepath: str = "logs/latest_forecast.json",
                         max_age_minutes: int = 90) -> Optional[Dict]:
    """
    Load the latest forecast JSON.

    Returns None if file missing, malformed, or stale (older than
    max_age_minutes).
    """
    path = Path(filepath)
    if not path.exists():
        logger.warning(f"Forecast file not found: {path}")
        return None

    try:
        with open(path) as f:
            fc = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Could not read forecast file: {e}")
        return None

    # Staleness check
    ts_str = fc.get('timestamp', '')
    try:
        ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        age = datetime.utcnow() - ts.replace(tzinfo=None)
        if age > timedelta(minutes=max_age_minutes):
            logger.warning(
                f"Forecast is stale ({age.total_seconds() / 60:.0f} min old). "
                f"Forecaster may not be running."
            )
            return None
    except (ValueError, TypeError):
        logger.warning("Forecast timestamp unparseable")

    return fc


def forecast_to_score_multiplier(forecast: Optional[Dict]) -> float:
    """
    Map a forecast to a multiplier the scanner can apply.

    Heavily bearish + high confidence → 0.0 (stand down)
    Bearish + medium                  → 0.4
    Neutral                           → 1.0
    Bullish + medium                  → 1.1
    Bullish + high                    → 1.3
    """
    if not forecast:
        return 1.0  # default to neutral if no forecast available

    direction = forecast.get('direction', 'unknown')
    confidence = forecast.get('confidence', 'low')

    table = {
        ('bearish', 'high'):    0.0,
        ('bearish', 'medium'):  0.4,
        ('bearish', 'low'):     0.7,
        ('neutral', 'high'):    1.0,
        ('neutral', 'medium'):  1.0,
        ('neutral', 'low'):     1.0,
        ('bullish', 'high'):    1.3,
        ('bullish', 'medium'):  1.1,
        ('bullish', 'low'):     1.05,
    }
    return table.get((direction, confidence), 1.0)


def should_trade_alts(forecast: Optional[Dict]) -> bool:
    """Hard gate for the scanner: should it run at all?"""
    if not forecast:
        return True  # if forecast unavailable, don't block scanner
    if forecast.get('direction') == 'bearish' and \
       forecast.get('confidence') == 'high':
        return False
    return True
