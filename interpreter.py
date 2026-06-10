"""
LLM Interpreter — turns the structured Forecast + raw signals into a
human-readable trade briefing.

Uses an OpenAI-compatible API. Compatible with OpenAI proper, Azure OpenAI,
OpenRouter, Anthropic via proxies, local Ollama, etc — controlled via
OPENAI_BASE_URL env var.

Prompt design intentionally:
  - Gives structured data, not narrative ('show, don't tell')
  - Asks for specific deliverables (story, what-to-watch, confidence caveats)
  - Forbids speculative price targets to avoid hallucination
  - Keeps temperature low for analytical writing
  - Falls back gracefully if API unavailable

Output is enrichment, never a dependency. If LLM call fails, the forecast
still completes; we just don't print an interpretation.
"""
import json
import logging
import textwrap
from typing import List, Optional, Dict
from datetime import datetime

from signals import Signal, Forecast
from config import CONFIG

logger = logging.getLogger(__name__)


# ============================================================
# PROMPT
# ============================================================
SYSTEM_PROMPT = """You are a senior quantitative crypto analyst with 15 \
years of experience trading BTC. You read structured signal data and \
explain what it means in plain, actionable English to a professional \
trader.

Your job: read the BTC forecast data below and write a concise, trade-ready \
briefing.

Rules:
1. Be direct. No hedging-speak like "it could potentially be argued that".
2. Tell the trader the STORY behind the signals, not just restate them.
3. Acknowledge contradictions honestly. If signals disagree, say so.
4. NEVER invent specific price targets. The model has no level data, only direction.
5. NEVER predict outcomes with certainty. Use language like "leans bullish", \
"momentum favors", "structure suggests".
6. If confidence is LOW, say plainly that this is not a high-conviction setup.
7. End with concrete WHAT-TO-WATCH items — specific signals that would \
flip the call.
8. Keep total length ~250-350 words. No filler.

Format your response in these sections, using these EXACT headers:

## The Call
(2-3 sentences: direction + conviction + most important takeaway)

## The Story
(3-5 sentences: why the signals are aligned/contradicting; what regime BTC \
is in; what the market is "saying")

## Key Tensions
(1-3 bullet points: contradictions or weaknesses in the bull/bear case)

## What to Watch Next
(2-4 bullet points: specific signals or thresholds that would change the picture)

## Bottom Line
(1-2 sentences: trade implications. If LOW confidence → say "no-trade" \
explicitly. If HIGH → say what direction has edge.)
"""


def _build_user_prompt(forecast: Forecast, signals: List[Signal]) -> str:
    """Pack forecast + signals into a structured prompt."""
    # Group signals by category
    by_cat: Dict[str, List[Signal]] = {}
    for s in signals:
        by_cat.setdefault(s.category, []).append(s)

    lines = [
        f"# BTC FORECAST DATA — {forecast.timestamp.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "## Forecast Summary",
        f"- BTC Price: ${forecast.btc_price:,.2f}",
        f"- Direction: {forecast.direction.value.upper()}",
        f"- Composite score: {forecast.composite:+.3f} (range -1 to +1)",
        f"- Confidence: {forecast.confidence.value}",
        f"- Signal agreement: {forecast.agreement_pct:.0f}% of signals aligned with direction",
        f"- Total signals: {forecast.signal_count}",
        f"- Forecast horizon: {forecast.horizon_hours} hours",
        "",
        "## Composite Score Interpretation Scale",
        "  -1.00 to -0.55: STRONG BEARISH",
        "  -0.55 to -0.30: BEARISH",
        "  -0.30 to +0.30: NEUTRAL (no clear edge)",
        "  +0.30 to +0.55: BULLISH",
        "  +0.55 to +1.00: STRONG BULLISH",
        "",
        "## Top Bullish Drivers",
    ]
    for d in forecast.key_drivers[:5]:
        if d['contribution'] > 0:
            lines.append(
                f"- {d['name']} ({d['category']}): "
                f"value={d['value']}, contribution={d['contribution']:+.2f}"
            )

    lines.append("")
    lines.append("## Top Bearish Drivers / Contradictions")
    for c in forecast.contradictions[:5]:
        lines.append(
            f"- {c['name']} ({c['category']}): "
            f"value={c['value']}, contribution={c['contribution']:+.2f}"
        )
    # Negative entries from key_drivers also count as bearish
    for d in forecast.key_drivers[:10]:
        if d['contribution'] < 0:
            lines.append(
                f"- {d['name']} ({d['category']}): "
                f"value={d['value']}, contribution={d['contribution']:+.2f}"
            )

    lines.append("")
    lines.append("## All Signals by Category")
    for cat in sorted(by_cat.keys()):
        sigs = by_cat[cat]
        lines.append(f"\n### {cat.upper()} ({len(sigs)} signals)")
        for s in sigs:
            value_str = str(s.raw_value)[:60]
            arrow = ('↑bullish' if s.score > 0.05
                     else '↓bearish' if s.score < -0.05 else '·neutral')
            lines.append(
                f"  - {s.name}: value={value_str} | "
                f"score={s.score:+.2f} ({arrow}) | confidence={s.confidence.value}"
            )

    lines.append("")
    lines.append("---")
    lines.append("Now write the trade briefing using the exact section "
                 "headers specified.")
    return "\n".join(lines)


# ============================================================
# LLM CALL
# ============================================================
def interpret(forecast: Forecast, signals: List[Signal]) -> Optional[str]:
    """
    Call OpenAI-compatible API to interpret the forecast.

    Returns the interpretation text, or None if disabled / unavailable / failed.
    """
    if not CONFIG.LLM_INTERPRETATION_ENABLED:
        return None
    if not CONFIG.OPENAI_API_KEY:
        logger.info("LLM interpretation: OPENAI_API_KEY not set, skipping")
        return None

    try:
        # Lazy import — only required if user wants this feature
        from openai import OpenAI
    except ImportError:
        logger.warning(
            "LLM interpretation: 'openai' package not installed. "
            "Install with: pip install openai"
        )
        return None

    try:
        client_kwargs = {
            'api_key': CONFIG.OPENAI_API_KEY,
            'timeout': CONFIG.LLM_TIMEOUT_SECONDS,
        }
        if CONFIG.OPENAI_BASE_URL:
            client_kwargs['base_url'] = CONFIG.OPENAI_BASE_URL

        client = OpenAI(**client_kwargs)

        user_prompt = _build_user_prompt(forecast, signals)

        logger.info(f"LLM interpretation: calling {CONFIG.OPENAI_MODEL}...")
        response = client.chat.completions.create(
            model=CONFIG.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )

        text = response.choices[0].message.content
        if text:
            text = text.strip()
            usage = response.usage
            if usage:
                logger.info(
                    f"LLM interpretation: ok "
                    f"({usage.prompt_tokens} in, {usage.completion_tokens} out)"
                )
            return text
        logger.warning("LLM interpretation: empty response")
        return None

    except Exception as e:
        logger.warning(f"LLM interpretation failed: {e}")
        return None


# ============================================================
# PRETTY PRINT
# ============================================================
def print_interpretation(text: str):
    """Render the LLM interpretation with a divider."""
    if not text:
        return
    from term import paint, CYAN
    print()
    print(paint("=" * 90, CYAN))
    print(paint("ANALYST INTERPRETATION  (LLM-generated)", CYAN, bold=True))
    print(paint("=" * 90, CYAN))
    # Render verbatim — the LLM already formatted it with markdown-style headers
    print(text)
    print("=" * 90)
    print()