"""
Telegram alerter for forecast updates.
"""
import logging
import requests
from typing import List

from signals import Forecast, Direction
from config import CONFIG

logger = logging.getLogger(__name__)


def format_forecast_message(forecast: Forecast) -> str:
    arrow = {
        Direction.BULLISH: '🟢↑',
        Direction.BEARISH: '🔴↓',
        Direction.NEUTRAL: '⚪→',
        Direction.UNKNOWN: '⚫?',
    }.get(forecast.direction, '⚫')

    lines = [
        f"*BTC Forecast — {forecast.timestamp.strftime('%Y-%m-%d %H:%M UTC')}*",
        "",
        f"{arrow} *{forecast.direction.value.upper()}* "
        f"({forecast.horizon_hours}h horizon)",
        f"Price: *${forecast.btc_price:,.0f}*",
        f"Composite: `{forecast.composite:+.3f}` | "
        f"Confidence: `{forecast.confidence.value}` | "
        f"Agreement: `{forecast.agreement_pct:.0f}%`",
        f"Signals: {forecast.signal_count}",
    ]
    if forecast.key_drivers:
        lines.append("")
        lines.append("*Key drivers:*")
        for d in forecast.key_drivers[:4]:
            lines.append(
                f"  • `{d['name']}` ({d['category']}): "
                f"{d['contribution']:+.2f}"
            )
    if forecast.contradictions:
        lines.append("")
        lines.append("*Contradictions:*")
        for c in forecast.contradictions[:2]:
            lines.append(
                f"  • `{c['name']}`: {c['contribution']:+.2f}"
            )
    return "\n".join(lines)


def send(message: str) -> bool:
    if not CONFIG.TELEGRAM_BOT_TOKEN or not CONFIG.TELEGRAM_CHAT_ID:
        return False
    try:
        url = (f"https://api.telegram.org/bot{CONFIG.TELEGRAM_BOT_TOKEN}"
               f"/sendMessage")
        r = requests.post(url, json={
            'chat_id': CONFIG.TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'Markdown',
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram failed: {e}")
        return False
