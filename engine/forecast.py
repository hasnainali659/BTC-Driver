"""
Forecast Engine.

Takes all collected signals, applies category-level weights, and produces
a single directional Forecast object with confidence and key drivers.

Architecture is rule-based for now (transparent, debuggable). Once you
have 1-3 months of forecast/outcome history in the DB, swap in an ML
model trained on that history — same input contract.
"""
import logging
from datetime import datetime
from typing import List, Dict, Optional

from signals import Signal, Forecast, Direction, Confidence
from config import CONFIG

logger = logging.getLogger(__name__)


# Map signal name → weight (lookup; falls back to category default)
SIGNAL_WEIGHTS = {
    # Technicals
    'trend_4h': CONFIG.W_TECHNICAL_TREND,
    'trend_1d': CONFIG.W_TECHNICAL_TREND,
    'rsi_4h': CONFIG.W_TECHNICAL_MOMENTUM,
    'rsi_1d': CONFIG.W_TECHNICAL_MOMENTUM,
    'macd_4h': CONFIG.W_TECHNICAL_MOMENTUM,
    'above_50d_ma': CONFIG.W_TECHNICAL_LEVELS,
    'above_200d_ma': CONFIG.W_TECHNICAL_LEVELS,
    'dist_30d_high_pct': CONFIG.W_TECHNICAL_LEVELS,
    'bb_squeeze_4h': 0.0,        # info-only
    'atr_1h_pct': 0.0,            # info-only
    'taker_delta_4h': CONFIG.W_TECHNICAL_FLOW * 0.7,
    'taker_delta_24h': CONFIG.W_TECHNICAL_FLOW,

    # Derivatives
    'funding_rate': CONFIG.W_DERIVATIVES_FUNDING,
    'oi_change_24h_pct': CONFIG.W_DERIVATIVES_OI,
    'top_trader_lsr': CONFIG.W_DERIVATIVES_LSR,
    'liquidations_24h_usd': CONFIG.W_DERIVATIVES_LIQUIDATIONS,
    'basis_pct': CONFIG.W_DERIVATIVES_BASIS,

    # Options
    'dvol': CONFIG.W_OPTIONS_SKEW,
    'iv_skew_proxy': CONFIG.W_OPTIONS_SKEW,

    # Dominance
    'btc_dominance_pct': CONFIG.W_DOMINANCE,
    'total_mcap_24h_change_pct': CONFIG.W_DOMINANCE,

    # Onchain
    'mempool_count': 0.0,
    'fee_fastest_satvb': 0.0,
    'hashrate_30d_change_pct': CONFIG.W_ONCHAIN_NETWORK,
    'difficulty': 0.0,
    'exchange_flow_data': CONFIG.W_ONCHAIN_FLOWS,

    # Whales
    'recent_mempool_large_tx_count': CONFIG.W_ONCHAIN_WHALES * 0.3,

    # Sentiment
    'fear_greed_index': CONFIG.W_SENTIMENT_FNG,
    'galaxy_score': CONFIG.W_SENTIMENT_SOCIAL,

    # News
    'crypto_news_sentiment': CONFIG.W_NEWS_CRYPTO,
    'macro_news_sentiment': CONFIG.W_NEWS_MACRO,

    # Macro
    'dxy_30d_direction': CONFIG.W_MACRO_DXY,
    'dxy_5d_change_pct': CONFIG.W_MACRO_DXY,
    'us_10y_yield_direction': CONFIG.W_MACRO_YIELDS,
    'real_yield_10y_direction': CONFIG.W_MACRO_YIELDS,
    'spx_5d_change_pct': CONFIG.W_MACRO_EQUITIES,
    'gold_5d_change_pct': CONFIG.W_MACRO_EQUITIES * 0.5,
}

CATEGORY_DEFAULT_WEIGHTS = {
    'technical': 1.0,
    'derivatives': 1.0,
    'sentiment': 0.5,
    'news': 0.7,
    'macro': 0.7,
    'onchain': 0.5,
    'whales': 0.5,
    'dominance': 0.5,
    'options': 0.7,
}


def _confidence_multiplier(c: Confidence) -> float:
    return {
        Confidence.HIGH: 1.0,
        Confidence.MEDIUM: 0.7,
        Confidence.LOW: 0.4,
    }.get(c, 0.5)


def _get_weight(signal: Signal) -> float:
    """Look up weight for a signal."""
    if signal.name in SIGNAL_WEIGHTS:
        return SIGNAL_WEIGHTS[signal.name]
    return CATEGORY_DEFAULT_WEIGHTS.get(signal.category, 0.5)


def synthesize(signals: List[Signal], btc_price: float) -> Forecast:
    """
    Reduce a list of signals into a single Forecast.
    """
    now = datetime.utcnow()

    if not signals:
        return Forecast(
            timestamp=now, horizon_hours=CONFIG.FORECAST_HORIZON_HOURS,
            direction=Direction.UNKNOWN, composite=0.0,
            confidence=Confidence.LOW, agreement_pct=0.0,
            signal_count=0, key_drivers=[], contradictions=[],
            btc_price=btc_price,
            summary="No signals available — cannot forecast.",
        )

    # Compute weighted scores
    weighted = []
    for s in signals:
        w = _get_weight(s)
        if w == 0:
            continue
        conf_mult = _confidence_multiplier(s.confidence)
        contribution = s.score * w * conf_mult
        weighted.append({
            'signal': s,
            'weight': w,
            'confidence_mult': conf_mult,
            'contribution': contribution,
        })

    if not weighted:
        return Forecast(
            timestamp=now, horizon_hours=CONFIG.FORECAST_HORIZON_HOURS,
            direction=Direction.UNKNOWN, composite=0.0,
            confidence=Confidence.LOW, agreement_pct=0.0,
            signal_count=0, key_drivers=[], contradictions=[],
            btc_price=btc_price,
            summary="No weighted signals — all weights zero.",
        )

    # Only signals that actually vote (|score| > 0.05) enter the denominator.
    # A zero-score signal means "no information right now", not "actively
    # neutral" — including its weight in the denominator diluted the
    # composite toward 0 and forced NEUTRAL calls regardless of the
    # voting signals' conviction.
    voting_weighted = [w for w in weighted if abs(w['signal'].score) > 0.05]
    total_contribution = sum(w['contribution'] for w in weighted)
    total_weight = sum(abs(w['weight'] * w['confidence_mult'])
                       for w in voting_weighted)
    composite = total_contribution / total_weight if total_weight > 0 else 0

    # Direction
    if composite >= CONFIG.STRONG_BULLISH_THRESHOLD:
        direction = Direction.BULLISH
    elif composite >= CONFIG.BULLISH_THRESHOLD:
        direction = Direction.BULLISH
    elif composite <= CONFIG.STRONG_BEARISH_THRESHOLD:
        direction = Direction.BEARISH
    elif composite <= CONFIG.BEARISH_THRESHOLD:
        direction = Direction.BEARISH
    else:
        direction = Direction.NEUTRAL

    # Agreement: % of voting signals that pointed in the same direction as composite
    voting = voting_weighted
    if voting:
        if direction == Direction.BULLISH:
            agreeing = sum(1 for w in voting if w['signal'].score > 0)
        elif direction == Direction.BEARISH:
            agreeing = sum(1 for w in voting if w['signal'].score < 0)
        else:
            agreeing = sum(1 for w in voting if abs(w['signal'].score) < 0.2)
        agreement_pct = agreeing / len(voting) * 100
    else:
        agreement_pct = 0.0

    # Confidence: combination of |composite| and agreement
    if abs(composite) > CONFIG.STRONG_BULLISH_THRESHOLD and agreement_pct > 70:
        confidence = Confidence.HIGH
    elif abs(composite) > CONFIG.BULLISH_THRESHOLD and agreement_pct > 55:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW

    # Key drivers — top contributions in the direction of composite
    sorted_by_abs = sorted(weighted, key=lambda x: abs(x['contribution']),
                            reverse=True)
    key_drivers = []
    contradictions = []
    for w in sorted_by_abs:
        s = w['signal']
        if direction == Direction.BULLISH and w['contribution'] > 0:
            key_drivers.append(_signal_brief(w))
        elif direction == Direction.BEARISH and w['contribution'] < 0:
            key_drivers.append(_signal_brief(w))
        elif direction == Direction.NEUTRAL:
            key_drivers.append(_signal_brief(w))
        else:
            # Goes against direction
            if abs(w['contribution']) > 0.05:
                contradictions.append(_signal_brief(w))

    # Build summary
    summary = _build_summary(direction, composite, confidence,
                             agreement_pct, key_drivers[:3], contradictions[:2],
                             btc_price)

    return Forecast(
        timestamp=now,
        horizon_hours=CONFIG.FORECAST_HORIZON_HOURS,
        direction=direction,
        composite=round(composite, 3),
        confidence=confidence,
        agreement_pct=round(agreement_pct, 1),
        signal_count=len(weighted),
        key_drivers=key_drivers[:5],
        contradictions=contradictions[:3],
        btc_price=btc_price,
        summary=summary,
    )


def _signal_brief(w: Dict) -> Dict:
    """Compact representation of a contributing signal."""
    s = w['signal']
    return {
        'name': s.name,
        'category': s.category,
        'value': str(s.raw_value)[:80],
        'score': round(s.score, 2),
        'contribution': round(w['contribution'], 3),
    }


def _build_summary(direction: Direction, composite: float,
                   confidence: Confidence, agreement: float,
                   drivers: List[Dict], contradictions: List[Dict],
                   btc_price: float) -> str:
    """One-paragraph human-readable summary."""
    arrow = {
        Direction.BULLISH: '↑',
        Direction.BEARISH: '↓',
        Direction.NEUTRAL: '→',
        Direction.UNKNOWN: '?',
    }[direction]

    horizon = CONFIG.FORECAST_HORIZON_HOURS
    lines = [
        f"BTC ${btc_price:,.0f} | {arrow} {direction.value.upper()} "
        f"({horizon}h horizon)",
        f"Composite: {composite:+.3f} | Confidence: {confidence.value} "
        f"| Agreement: {agreement:.0f}%",
    ]
    if drivers:
        d_str = ", ".join(f"{d['name']}({d['contribution']:+.2f})"
                          for d in drivers[:3])
        lines.append(f"Drivers: {d_str}")
    if contradictions:
        c_str = ", ".join(f"{c['name']}({c['contribution']:+.2f})"
                          for c in contradictions[:2])
        lines.append(f"Contradictions: {c_str}")
    return " | ".join(lines)
