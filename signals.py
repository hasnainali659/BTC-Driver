"""
Base data structures.

All collectors return a list of Signal objects with a uniform contract.
The forecast engine reduces these into a Forecast.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Any
import json


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Signal:
    """
    One atomic data point with directional interpretation.

    Fields:
      source        — e.g. 'binance', 'fred', 'cryptopanic'
      category      — e.g. 'technical', 'derivatives', 'sentiment'
      name          — human-readable name e.g. 'funding_rate', 'rsi_4h'
      raw_value     — the actual measurement (any type)
      score         — directional vote: -1.0 (max bearish) to +1.0 (max bullish)
      confidence    — high / medium / low
      timestamp     — UTC time of the signal
      meta          — anything else useful for debug/logs
    """
    source: str
    category: str
    name: str
    raw_value: Any
    score: float
    confidence: Confidence = Confidence.MEDIUM
    timestamp: datetime = field(default_factory=datetime.utcnow)
    meta: Dict = field(default_factory=dict)

    def __post_init__(self):
        # Clamp score to [-1, +1]
        self.score = max(-1.0, min(1.0, float(self.score)))

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        d['confidence'] = self.confidence.value
        return d


@dataclass
class Forecast:
    """
    Final synthesized forecast for the next forecast horizon.

    Fields:
      timestamp     — when the forecast was made
      horizon_hours — how far ahead this applies
      direction     — bullish / bearish / neutral
      composite     — final weighted score, range [-1, +1]
      confidence    — overall confidence level
      agreement_pct — what % of signals agreed with the final direction
      signal_count  — how many signals fed in
      key_drivers   — top 3-5 signals pushing toward the call
      contradictions — top 1-3 signals against the call
      btc_price     — current BTC price for reference
      summary       — human-readable explanation
    """
    timestamp: datetime
    horizon_hours: int
    direction: Direction
    composite: float
    confidence: Confidence
    agreement_pct: float
    signal_count: int
    key_drivers: List[Dict]
    contradictions: List[Dict]
    btc_price: float
    summary: str = ""

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        d['direction'] = self.direction.value
        d['confidence'] = self.confidence.value
        return d
