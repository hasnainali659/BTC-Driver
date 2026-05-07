"""
BTC Forecaster — central configuration.

Every tunable lives here. API keys come from env vars.
"""
import os
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Config:
    # ============ API KEYS (all optional — collectors gracefully no-op without them) ============
    COINGLASS_API_KEY: Optional[str] = field(
        default_factory=lambda: os.getenv("COINGLASS_API_KEY")
    )
    CRYPTOPANIC_API_KEY: Optional[str] = field(
        default_factory=lambda: os.getenv("CRYPTOPANIC_API_KEY")
    )
    NEWSAPI_API_KEY: Optional[str] = field(
        default_factory=lambda: os.getenv("NEWSAPI_API_KEY")
    )
    FRED_API_KEY: Optional[str] = field(
        default_factory=lambda: os.getenv("FRED_API_KEY")
    )
    LUNARCRUSH_API_KEY: Optional[str] = field(
        default_factory=lambda: os.getenv("LUNARCRUSH_API_KEY")
    )
    TELEGRAM_BOT_TOKEN: Optional[str] = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN")
    )
    TELEGRAM_CHAT_ID: Optional[str] = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID")
    )

    # ============ COLLECTOR TOGGLES ============
    # Which collectors to run. False = skip.
    ENABLE_PRICE: bool = True
    ENABLE_DERIVATIVES: bool = True
    ENABLE_ONCHAIN: bool = False
    ENABLE_SENTIMENT: bool = True
    ENABLE_NEWS: bool = True
    ENABLE_MACRO: bool = True
    ENABLE_WHALES: bool = True
    ENABLE_TECHNICALS: bool = True
    ENABLE_DOMINANCE: bool = True
    ENABLE_OPTIONS: bool = True

    # ============ TIMEFRAMES ============
    FORECAST_HORIZON_HOURS: int = 4
    SCAN_INTERVAL_SECONDS: int = 3600  # hourly
    REQUEST_TIMEOUT: int = 15
    REQUEST_RETRIES: int = 2

    # ============ FORECAST ENGINE WEIGHTS ============
    # Each signal contributes a -1 to +1 vote; weights determine influence.
    # Tune these from forecast_accuracy.py once you have data.
    W_TECHNICAL_TREND: float = 2.0
    W_TECHNICAL_MOMENTUM: float = 1.5
    W_TECHNICAL_LEVELS: float = 1.0
    W_DERIVATIVES_FUNDING: float = 1.5
    W_DERIVATIVES_OI: float = 1.0
    W_DERIVATIVES_LIQUIDATIONS: float = 1.0
    W_DERIVATIVES_LSR: float = 0.8
    W_DERIVATIVES_BASIS: float = 0.8
    W_OPTIONS_SKEW: float = 0.7
    W_DOMINANCE: float = 0.5
    W_ONCHAIN_FLOWS: float = 1.5
    W_ONCHAIN_WHALES: float = 1.5
    W_ONCHAIN_NETWORK: float = 0.5
    W_SENTIMENT_FNG: float = 0.6
    W_SENTIMENT_SOCIAL: float = 0.5
    W_NEWS_CRYPTO: float = 1.0
    W_NEWS_MACRO: float = 0.8
    W_MACRO_DXY: float = 0.7
    W_MACRO_YIELDS: float = 0.7
    W_MACRO_EQUITIES: float = 0.5

    # ============ FORECAST INTERPRETATION ============
    # Composite signal mapping (after weighted sum normalization)
    BULLISH_THRESHOLD: float = 0.30       # >+0.30 = bullish
    STRONG_BULLISH_THRESHOLD: float = 0.55
    BEARISH_THRESHOLD: float = -0.30      # <-0.30 = bearish
    STRONG_BEARISH_THRESHOLD: float = -0.55
    HIGH_CONFIDENCE_AGREEMENT: float = 0.7  # % of signals agreeing

    # ============ STORAGE ============
    DB_PATH: str = "btc_forecaster.db"
    LOG_DIR: str = "logs"

    # ============ SOURCES (URLs as constants for clarity) ============
    BINANCE_SPOT: str = "https://api.binance.com"
    BINANCE_FUTURES: str = "https://fapi.binance.com"
    COINGECKO: str = "https://api.coingecko.com/api/v3"
    BLOCKCHAIN_INFO: str = "https://api.blockchain.info"
    MEMPOOL_SPACE: str = "https://mempool.space/api"
    ALTERNATIVE_FNG: str = "https://api.alternative.me/fng"
    CRYPTOPANIC: str = "https://cryptopanic.com/api/v1"
    DERIBIT: str = "https://www.deribit.com/api/v2/public"
    COINGLASS_V3: str = "https://open-api-v3.coinglass.com/api"
    FRED: str = "https://api.stlouisfed.org/fred"


CONFIG = Config()
