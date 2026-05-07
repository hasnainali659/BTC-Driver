"""
Telegram alerter for forecast updates.
"""
import logging
import requests
from typing import List, Optional

from signals import Forecast, Direction
from config import CONFIG

logger = logging.getLogger(__name__)


def format_forecast_message(forecast: Forecast,
                            interpretation: Optional[str] = None) -> str:
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

    # Append LLM interpretation if available
    if interpretation:
        lines.append("")
        lines.append("---")
        lines.append("*Analyst view:*")
        # Telegram has a 4096 char limit; trim if combined message gets too big
        current_len = sum(len(l) + 1 for l in lines)
        budget = max(500, 3800 - current_len)
        snippet = interpretation
        if len(snippet) > budget:
            snippet = snippet[:budget].rsplit(' ', 1)[0] + '... [truncated]'
        lines.append(snippet)

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