# BTC Forecaster — Usage Guide

A complete walkthrough of what this project does, how it works, and how to use every part of it. If you've cloned the repo and want to actually use it, start here.

---

## Table of Contents

1. [What this project does](#what-this-project-does)
2. [Mental model](#mental-model)
3. [Quick start](#quick-start)
4. [Setup in detail](#setup-in-detail)
5. [Commands reference](#commands-reference)
6. [Output explained](#output-explained)
7. [The signals — what each one tells you](#the-signals)
8. [Configuration reference](#configuration-reference)
9. [File structure & module reference](#file-structure)
10. [Public API for downstream systems](#public-api)
11. [Troubleshooting](#troubleshooting)
12. [Extending the system](#extending-the-system)
13. [FAQ](#faq)

---

## What this project does

Every hour, this system:

1. Pulls live data from 9 different domains (technicals, derivatives, on-chain, sentiment, news, macro, options, dominance, whales)
2. Converts each data point into a normalized "signal" with a directional score from -1 (bearish) to +1 (bullish)
3. Combines all signals using weighted scoring into a single composite forecast
4. Classifies the forecast as `bullish` / `bearish` / `neutral` with a confidence level
5. Optionally asks an LLM (GPT-4o-mini, etc) to write a human-readable trade briefing
6. Saves everything to SQLite for later accuracy backtesting
7. Sends a Telegram alert (if configured)
8. Writes the forecast to `logs/latest_forecast.json` for downstream systems to consume

**It does not place trades. It produces a structured opinion.** What you do with that opinion is your business.

---

## Mental model

Three concepts and you understand the whole system:

### 1. Signal

A single piece of evidence with a directional vote.

```python
Signal(
    source='binance',           # where the data came from
    category='technical',       # what kind of signal
    name='trend_4h',            # specific name
    raw_value='up',             # the actual measurement
    score=0.6,                  # -1 to +1; +0.6 = moderately bullish
    confidence=Confidence.HIGH, # how reliable this measurement is
)
```

There are roughly 20-25 signals per forecast cycle.

### 2. Forecast

The final synthesized opinion. One per hourly run.

```python
Forecast(
    direction='bullish',       # the call
    composite=+0.42,           # weighted score, -1 to +1
    confidence='medium',       # high/medium/low
    agreement_pct=68.0,        # % of signals aligned with direction
    key_drivers=[...],         # top 5 signals supporting the call
    contradictions=[...],      # top 3 signals against the call
    btc_price=81976.59,        # for reference
    summary='BTC $81,976 | ↑ BULLISH (4h horizon) | Composite: +0.420...',
)
```

### 3. Interpretation (optional)

LLM-generated narrative explaining the forecast in plain English. Sections:
- **The Call** — direction + conviction
- **The Story** — what regime BTC is in, why signals align/disagree
- **Key Tensions** — contradictions in the case
- **What to Watch Next** — specific signals that would flip the call
- **Bottom Line** — trade implications

The interpretation is an *enrichment*. If the LLM is unavailable, you still get the structured forecast.

---

## Quick start

Three commands and you're running:

```bash
cd btc_forecaster
pip install -r requirements.txt
python main.py
```

That works without any API keys. You'll get a forecast based on free data sources only (Binance, Deribit, CoinGecko, alternative.me Fear & Greed). To upgrade quality, see the next section.

---

## Setup in detail

### Step 1 — Install Python dependencies

```bash
pip install -r requirements.txt
```

Installs:
- `requests` — HTTP client
- `pandas` / `numpy` — data manipulation
- `openai` — for LLM interpretation (optional)
- `python-dotenv` — auto-load `.env` files (optional)

### Step 2 — Configure API keys (recommended)

Copy the template:

```bash
cp .env.example .env       # mac/linux
copy .env.example .env     # windows
```

Edit `.env` and fill in keys you have. **All keys are optional** — the system works without any of them, just with reduced signal quality.

Tier-1 keys (huge quality boost, both free):

| Key | What it adds | Get it from |
|-----|-------------|-------------|
| `FRED_API_KEY` | DXY, US 10Y yield, real yields. Highest-impact macro data. | https://fred.stlouisfed.org/docs/api/api_key.html |
| `OPENAI_API_KEY` | LLM interpretation that turns numbers into a trade briefing. | https://platform.openai.com/api-keys |

Tier-2 keys (nice to have):

| Key | What it adds | Get it from |
|-----|-------------|-------------|
| `CRYPTOPANIC_API_KEY` | Aggregated crypto news sentiment | https://cryptopanic.com/developers/api/ |
| `COINGLASS_API_KEY` | Liquidation data, exchange flows | https://www.coinglass.com/api |
| `NEWSAPI_API_KEY` | Macro news headlines | https://newsapi.org/register |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Push alerts to your phone | @BotFather on Telegram |

### Step 3 — Test it

```bash
python main.py
```

You should see:
- Each collector reporting how many signals it produced
- A structured forecast table
- An LLM interpretation (if `OPENAI_API_KEY` is set)
- A "Saved forecast id=N" log line

If something breaks, see [Troubleshooting](#troubleshooting).

### Step 4 — Run it on a schedule

Pick one option:

**Option A — Loop in foreground (easy):**

```bash
python main.py --loop --interval 3600
```

Runs forever, every hour. Press Ctrl+C to stop. Best for testing.

**Option B — Windows Task Scheduler:**

1. Open Task Scheduler
2. Create Basic Task → "BTC Forecaster"
3. Trigger: Daily, repeat every 1 hour
4. Action: Start a program
   - Program: `python.exe` (full path)
   - Arguments: `main.py`
   - Start in: `C:\Repos\Projects\btc_files`

**Option C — Cron (Linux/Mac):**

```bash
crontab -e
# Add this line for hourly runs:
0 * * * * cd /path/to/btc_forecaster && /usr/bin/python3 main.py >> logs/cron.log 2>&1
```

**Option D — systemd service (Linux production):** see Extending below.

---

## Commands reference

### `python main.py`

Run one forecast cycle and exit.

| Flag | Effect |
|------|--------|
| `--loop` | Keep running, sleep between cycles |
| `--interval 1800` | Loop interval in seconds (default 3600 = hourly) |
| `--quiet` | Don't send Telegram alert this run |
| `--dry-run` | Don't save to DB or alert (testing only) |

Examples:
```bash
python main.py                            # one cycle
python main.py --loop                     # hourly forever
python main.py --loop --interval 1800     # every 30 min
python main.py --quiet                    # no alert
python main.py --dry-run                  # don't persist
```

### `python forecast_accuracy.py`

Evaluate how well the forecaster has been predicting. Run after you have at least 24-48 hours of forecasts logged.

| Flag | Effect |
|------|--------|
| `--threshold 0.3` | Min % move to count as a directional outcome (default 0.3%) |

Examples:
```bash
python forecast_accuracy.py
python forecast_accuracy.py --threshold 0.5    # require 0.5% move
python forecast_accuracy.py --threshold 1.0    # only big moves count
```

Output includes:
- Overall accuracy
- Accuracy by confidence level (high/medium/low)
- Accuracy by direction
- Confusion matrix
- CSV with per-forecast breakdown saved to `logs/forecast_accuracy.csv`

---

## Output explained

When you run `python main.py`, you'll see something like this. Let me walk through every section.

### Section 1 — Collector log

```
2026-05-06 18:26:08 [INFO]   [dominance] 2 signals
2026-05-06 18:26:08 [INFO]   [news] 0 signals
2026-05-06 18:26:09 [INFO]   [sentiment] 1 signals
2026-05-06 18:26:09 [INFO]   [technicals] 9 signals
...
```

Each line = one collector finished. If a collector returns 0 signals, that data domain didn't contribute (could be missing API key, network issue, or genuinely no relevant data right now).

### Section 2 — Forecast summary

```
==========================================================================================
BTC FORECAST — 2026-05-06 13:29 UTC
==========================================================================================
BTC Price:     $81,976.59
Direction:     NEUTRAL (4h horizon)
Composite:     +0.244  (range -1 to +1)
Confidence:    LOW
Agreement:     44% of signals aligned
Signal count:  18
```

**How to read this:**

- **Direction**: the call — `BULLISH`, `BEARISH`, or `NEUTRAL`
- **Composite**: weighted score from all signals
  - `-1.0 to -0.55` = strong bearish
  - `-0.55 to -0.30` = bearish
  - `-0.30 to +0.30` = neutral (no clear edge)
  - `+0.30 to +0.55` = bullish
  - `+0.55 to +1.0` = strong bullish
- **Confidence**: `low` / `medium` / `high`. Low = the system is telling you it doesn't know. Don't trade off low-confidence calls.
- **Agreement**: what % of voting signals point the same way as the composite. Low agreement = signals are split. High agreement = signals are aligned.

### Section 3 — Key drivers and contradictions

```
KEY DRIVERS:
  +1.200  [technical  ] trend_4h               value=up
  +1.200  [technical  ] trend_1d               value=up
  +0.600  [technical  ] rsi_4h                 value=64.9
  -0.600  [technical  ] above_200d_ma          value=False
```

The **top contributors** to the final score, sorted by absolute contribution. Negative entries here are notable — they're significant signals pulling against the direction.

### Section 4 — All signals by category

```
ALL SIGNALS BY CATEGORY:
[derivatives] (4 signals)
   · funding_rate          score=+0.00  conf=high    val=-3.36e-06
   · oi_change_24h_pct     score=+0.00  conf=high    val=-3.01
   ↑ top_trader_lsr        score=+0.10  conf=medium  val=0.52
   ↓ basis_pct             score=-0.10  conf=medium  val=-0.0708
```

Every collected signal grouped by category.

- `↑` = bullish vote
- `↓` = bearish vote
- `·` = neutral / informational

### Section 5 — Analyst interpretation (if LLM enabled)

```
==========================================================================================
ANALYST INTERPRETATION  (LLM-generated)
==========================================================================================
## The Call
BTC leans mildly bullish on momentum but lacks conviction.

## The Story
Short-term technicals are aligned (uptrend, healthy RSI) but price is below
the 200-day MA — structurally still in correction...

## Key Tensions
- Derivatives positioning is flat while technicals are bullish
- Below 200D MA limits upside

## What to Watch Next
- A break above 200D MA confirms regime change
- Funding turning negative would add squeeze fuel

## Bottom Line
Low confidence — no-trade setup. Wait for stronger alignment.
==========================================================================================
```

LLM-generated. The prompt instructs the model to never invent price targets or certainty claims — only restructure what the data says.

---

## The signals

Reference for every signal the system produces, what it measures, and how it scores.

### Technical signals (Binance — free)

| Signal | What it measures | Bullish when | Bearish when |
|--------|-----------------|--------------|---------------|
| `trend_4h` | 4-hour price trend (MA slope) | up | down |
| `trend_1d` | Daily price trend | up | down |
| `rsi_4h` | 4-hour Relative Strength Index | 30-70 healthy | >80 overbought, <20 oversold |
| `rsi_1d` | Daily RSI | (same scale) | (same scale) |
| `macd_4h` | 4-hour MACD histogram | positive & rising | negative & falling |
| `bb_squeeze_4h` | Bollinger Band width vs 100-period avg | (info only) | (info only) — squeeze precedes breakout |
| `above_50d_ma` | Price vs 50-day MA | above | below |
| `above_200d_ma` | Price vs 200-day MA | above | below |
| `dist_30d_high_pct` | Distance from 30-day high | <2% from high | >10% below |
| `atr_1h_pct` | 1-hour Average True Range as % | (info only — vol regime) | |

### Derivatives signals (Binance Futures — free)

| Signal | What it measures | Bullish when | Bearish when |
|--------|-----------------|--------------|---------------|
| `funding_rate` | Perpetual swap funding rate | Negative (shorts paying = squeeze fuel) | Very positive (crowded longs) |
| `oi_change_24h_pct` | 24h Open Interest change | Rising with price | Rising with falling price |
| `top_trader_lsr` | Top trader long/short ratio | <0.7 (heavy short = squeeze potential) | >2.5 (heavy long = top heavy) |
| `basis_pct` | Futures premium over spot | Premium >0.1% | Discount |
| `liquidations_24h_usd` | 24h liquidations breakdown (Coinglass) | Heavy long liqs (capitulation) | Heavy short liqs (squeeze exhaustion) |

### Sentiment signals (free)

| Signal | What it measures | Bullish when | Bearish when |
|--------|-----------------|--------------|---------------|
| `fear_greed_index` | Fear & Greed Index (alternative.me) | <25 (extreme fear, contrarian) | >75 (extreme greed, contrarian) |
| `galaxy_score` | LunarCrush social momentum (paid) | High (>60) | Low (<40) |

### News signals (CryptoPanic + NewsAPI)

| Signal | What it measures |
|--------|-----------------|
| `crypto_news_sentiment` | Keyword-based sentiment of recent BTC news |
| `macro_news_sentiment` | Keyword-based sentiment of recent Fed/inflation/recession news |

Sentiment scoring: bullish keywords (rally, surge, ETF approval, rate cut, etc.) vs bearish keywords (crash, hack, lawsuit, hawkish, etc.). Score = (bull - bear) / (bull + bear).

### Macro signals (FRED + Yahoo Finance)

| Signal | What it measures | Bullish when | Bearish when |
|--------|-----------------|--------------|---------------|
| `dxy_30d_direction` (or `dxy_5d_change_pct`) | US Dollar Index | Falling | Rising |
| `us_10y_yield_direction` | 10Y Treasury yield | Falling | Rising |
| `real_yield_10y_direction` | TIPS 10Y real yield | Falling | Rising |
| `spx_5d_change_pct` | S&P 500 5-day change | Rising (risk-on) | Falling (risk-off) |
| `gold_5d_change_pct` | Gold 5-day change | Mildly bullish | (regime dependent) |

### Dominance signals (CoinGecko — free)

| Signal | What it measures | Bullish for BTC when | Bearish for BTC when |
|--------|-----------------|----------------------|----------------------|
| `btc_dominance_pct` | BTC market cap as % of total crypto | High >55% | Low <45% |
| `total_mcap_24h_change_pct` | Total crypto market cap 24h change | Rising | Falling |

### Options signals (Deribit — free)

| Signal | What it measures | Bullish when | Bearish when |
|--------|-----------------|--------------|---------------|
| `dvol` | Deribit BTC volatility index | >80 (panic = often near low) | <35 (complacency) |

### On-chain signals (mempool.space, blockchain.info — free)

Mostly informational; direction unknown without expensive paid feeds.

| Signal | What it measures |
|--------|-----------------|
| `mempool_count` | Pending transaction count |
| `fee_fastest_satvb` | Recommended fastest fee |
| `hashrate_ths` | Network hashrate |
| `difficulty` | Mining difficulty |

### Whales signal

`recent_mempool_large_tx_count` — number of >100 BTC transactions in recent mempool. Weak proxy without paid whale tracking (Whale Alert / Arkham).

---

## Configuration reference

All settings live in `config.py`. The most important ones:

### Forecast horizon and intervals

```python
FORECAST_HORIZON_HOURS: int = 4         # how far ahead the forecast applies
SCAN_INTERVAL_SECONDS: int = 3600       # default loop interval
REQUEST_TIMEOUT: int = 15               # HTTP timeout
REQUEST_RETRIES: int = 2                # how many retries on failure
```

### Collector toggles

```python
ENABLE_PRICE: bool = True
ENABLE_DERIVATIVES: bool = True
ENABLE_ONCHAIN: bool = True             # set False if mempool.space hangs
ENABLE_SENTIMENT: bool = True
ENABLE_NEWS: bool = True
ENABLE_MACRO: bool = True
ENABLE_WHALES: bool = True
ENABLE_TECHNICALS: bool = True
ENABLE_DOMINANCE: bool = True
ENABLE_OPTIONS: bool = True
```

Set any to `False` to skip that collector entirely.

### Scoring weights

These determine how much each signal category influences the final composite. Defaults are reasonable starting points; tune based on `forecast_accuracy.py` results.

```python
W_TECHNICAL_TREND: float = 2.0          # trend signals are heaviest
W_TECHNICAL_MOMENTUM: float = 1.5
W_TECHNICAL_LEVELS: float = 1.0
W_DERIVATIVES_FUNDING: float = 1.5
W_DERIVATIVES_OI: float = 1.0
W_ONCHAIN_FLOWS: float = 1.5
W_NEWS_CRYPTO: float = 1.0
W_NEWS_MACRO: float = 0.8
W_MACRO_DXY: float = 0.7
W_MACRO_YIELDS: float = 0.7
# ... and so on
```

### Direction thresholds

```python
BULLISH_THRESHOLD: float = 0.30         # composite >+0.30 = bullish
STRONG_BULLISH_THRESHOLD: float = 0.55  # composite >+0.55 = strong bullish
BEARISH_THRESHOLD: float = -0.30
STRONG_BEARISH_THRESHOLD: float = -0.55
HIGH_CONFIDENCE_AGREEMENT: float = 0.7  # 70%+ agreement = high confidence
```

Lower thresholds = more sensitive to small moves. Higher = more conservative.

### LLM interpretation

```python
OPENAI_API_KEY: ...                     # from .env
OPENAI_MODEL: str = "gpt-4o-mini"       # cheap and good
OPENAI_BASE_URL: ...                    # for OpenRouter / Ollama / Azure
LLM_INTERPRETATION_ENABLED: bool = True
LLM_TIMEOUT_SECONDS: int = 30
LLM_TEMPERATURE: float = 0.3            # low for analytical writing
LLM_MAX_TOKENS: int = 800
```

---

## File structure

```
btc_forecaster/
├── config.py                    # all tunables + env-var loading
├── signals.py                   # Signal + Forecast + Direction + Confidence types
├── http_util.py                 # shared retry/timeout HTTP wrapper
├── interpreter.py               # LLM interpretation
├── alerter.py                   # Telegram alert formatting & sending
├── btc_forecaster_client.py     # helper for downstream systems to consume
├── main.py                      # entry point
├── forecast_accuracy.py         # accuracy backtester
├── requirements.txt
├── .env.example                 # template for API keys
├── README.md
├── usage.md                     # this file
│
├── collectors/                  # one module per data domain
│   ├── __init__.py
│   ├── technicals.py            # price, RSI, MACD, ATR, MAs
│   ├── derivatives.py           # funding, OI, LSR, basis, liquidations
│   ├── dominance.py             # BTC.D, total mcap
│   ├── options.py               # Deribit DVOL
│   ├── onchain.py               # mempool, fees, hashrate
│   ├── sentiment.py             # Fear & Greed, LunarCrush
│   ├── news.py                  # CryptoPanic + NewsAPI sentiment
│   ├── macro.py                 # DXY, yields, SPX, gold
│   └── whales.py                # large mempool TXs
│
├── engine/
│   ├── __init__.py
│   └── forecast.py              # signal synthesis logic
│
├── storage/
│   ├── __init__.py
│   └── db.py                    # SQLite persistence + JSON export
│
└── logs/                        # auto-created
    ├── btc_forecaster.db        # SQLite history
    ├── latest_forecast.json     # latest forecast for downstream consumers
    └── forecast_accuracy.csv    # backtester output (after running it)
```

### Module responsibilities

| Module | What it does |
|--------|-------------|
| `config.py` | Single source of truth for all tunables. Loads API keys from env. |
| `signals.py` | Defines `Signal`, `Forecast`, `Direction`, `Confidence` dataclasses. |
| `http_util.py` | `http_get()` — wraps `requests.get` with retry, timeout, silent error suppression. Used by all collectors. |
| `collectors/*.py` | Each module exposes one function: `collect() -> List[Signal]`. Independent, swappable. |
| `engine/forecast.py` | `synthesize(signals, btc_price) -> Forecast`. Weighted scoring, direction classification, confidence calculation. |
| `interpreter.py` | `interpret(forecast, signals) -> str`. LLM-based narrative. |
| `storage/db.py` | `save_forecast()`, `latest_forecast()`, `export_latest_forecast_to_json()`. |
| `alerter.py` | `format_forecast_message()`, `send()`. Telegram only for now. |
| `main.py` | Orchestrates everything: parallel collectors → engine → storage → alerter → interpreter. |
| `forecast_accuracy.py` | Stand-alone script. Reads logged forecasts, fetches actual prices at horizon, computes hit rate. |
| `btc_forecaster_client.py` | Helper for consumers (e.g. your scanner repo). |

---

## Public API

### For downstream systems (e.g. your alt scanner)

The forecaster writes the latest forecast to `logs/latest_forecast.json` after each cycle. Any other system can read that file.

```python
# In your scanner's main.py:
from btc_forecaster_client import (
    load_latest_forecast,
    forecast_to_score_multiplier,
    should_trade_alts,
)

forecast = load_latest_forecast(
    "/path/to/btc_forecaster/logs/latest_forecast.json",
    max_age_minutes=90  # don't trust forecasts older than 90 minutes
)

if not should_trade_alts(forecast):
    print("BTC forecaster says: stand down")
    return

multiplier = forecast_to_score_multiplier(forecast)  # e.g. 1.2 in favorable, 0.0 in dangerous
# Apply to your scanner's scores
```

#### `load_latest_forecast(filepath, max_age_minutes=90) -> Optional[Dict]`

Returns the latest forecast as a dict, or `None` if:
- File doesn't exist
- File is malformed
- Forecast is older than `max_age_minutes` (forecaster probably stopped)

#### `forecast_to_score_multiplier(forecast) -> float`

Maps forecast direction × confidence to a multiplier:

| Direction | Confidence | Multiplier |
|-----------|-----------|------------|
| bearish | high | 0.0 (stand down) |
| bearish | medium | 0.4 |
| bearish | low | 0.7 |
| neutral | * | 1.0 |
| bullish | low | 1.05 |
| bullish | medium | 1.1 |
| bullish | high | 1.3 |

#### `should_trade_alts(forecast) -> bool`

Hard gate. Returns `False` only if BTC forecast is `bearish` + `high confidence`. Otherwise `True`.

### Programmatic use within Python

If you want to call the forecaster from another Python program:

```python
from main import run_forecast_cycle

result = run_forecast_cycle(quiet=True, dry_run=False)

forecast = result['forecast']           # Forecast object
signals = result['signals']             # List[Signal]
interpretation = result['interpretation']  # str or None
duration_s = result['duration_s']
```

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'collectors'`

Missing `__init__.py` files in package folders. Create empty files:

```bash
touch collectors/__init__.py engine/__init__.py storage/__init__.py
```

(Or paste a comment line into each — empty files sometimes don't transfer through file pickers.)

### Run takes 3+ minutes (mempool.space hangs)

Some networks (notably some ISPs in Pakistan) can't reach `mempool.space`. The system uses a 5s timeout but with retries that can still add up.

**Quick fix** — disable on-chain collector in `config.py`:

```python
ENABLE_ONCHAIN: bool = False
```

You lose 3-4 informational signals but cut 30+ seconds off cycle time. The on-chain signals are the lowest-value ones in this system anyway.

### `[news] 0 signals`

You don't have a `CRYPTOPANIC_API_KEY` set. Either:
1. Get a free key from cryptopanic.com/developers/api/
2. Or accept reduced quality — news is one of many inputs

### `[whales] 0 signals` or whales crashes

The blockchain.info `unconfirmed-transactions` endpoint sometimes returns malformed data or rate-limits. The collector now defensively handles this. If you still see crashes, paste the traceback.

### LLM interpretation not appearing

Check in order:
1. Is `OPENAI_API_KEY` set in `.env`? Verify with `echo $env:OPENAI_API_KEY` (PowerShell) or `echo $OPENAI_API_KEY` (bash)
2. Is the `openai` package installed? `pip show openai`
3. Look for a log line: `LLM interpretation: ok (N in, M out)` = success, `LLM interpretation: ... skipping` = problem
4. Check the model name. `OPENAI_MODEL=gpt-4o-mini` is cheap and reliable. If using OpenRouter, model names are different (e.g. `anthropic/claude-3.5-sonnet`).
5. If using a non-OpenAI endpoint, ensure `OPENAI_BASE_URL` is set correctly.

### `HTTP 429` rate-limited warnings

Some free tiers have low limits. The system has automatic exponential backoff but if you hit it consistently, lower your scan frequency:

```bash
python main.py --loop --interval 7200    # every 2 hours
```

### Telegram alerts not sending

1. Verify both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set
2. Send a test message to your bot first via `t.me/your_bot_username`
3. Get your chat_id from @userinfobot on Telegram
4. The bot must be added to your chat (or you DM the bot)

### `forecast_accuracy.py` says "No matured forecasts to evaluate yet"

Forecasts need to age `FORECAST_HORIZON_HOURS` (default 4) before they can be evaluated. Wait at least 4-5 hours after your first run.

### `DeprecationWarning: datetime.datetime.utcnow()`

Cosmetic warning, not breaking. Python is signaling that `datetime.utcnow()` will be removed in some future version. Safe to ignore for years. (If it bothers you, replace with `datetime.now(timezone.utc).replace(tzinfo=None)`.)

---

## Extending the system

### Adding a new collector

Say you want to add an exchange flow collector via CryptoQuant.

**1. Create the file** `collectors/exchange_flows.py`:

```python
"""Exchange flow collector — CryptoQuant API."""
import logging
from datetime import datetime
from typing import List

from signals import Signal, Confidence
from http_util import http_get
from config import CONFIG

logger = logging.getLogger(__name__)


def collect() -> List[Signal]:
    if not getattr(CONFIG, 'ENABLE_EXCHANGE_FLOWS', True):
        return []
    if not getattr(CONFIG, 'CRYPTOQUANT_API_KEY', None):
        return []

    signals = []
    now = datetime.utcnow()

    data = http_get(
        "https://api.cryptoquant.com/v1/btc/exchange-flows/inflow",
        headers={'Authorization': f'Bearer {CONFIG.CRYPTOQUANT_API_KEY}'},
    )
    if not data:
        return signals

    inflow_btc = data.get('value', 0)
    # Big inflows = sell pressure proxy = bearish
    if inflow_btc > 5000:
        score = -0.5
    elif inflow_btc > 2000:
        score = -0.2
    elif inflow_btc < 500:
        score = 0.2
    else:
        score = 0.0

    signals.append(Signal(
        source='cryptoquant', category='onchain',
        name='exchange_inflow_btc',
        raw_value=inflow_btc, score=score,
        confidence=Confidence.MEDIUM, timestamp=now,
    ))
    return signals
```

**2. Register it** in `main.py`:

```python
from collectors import (
    technicals, derivatives, ..., exchange_flows  # add this
)

COLLECTORS = {
    ...
    'exchange_flows': exchange_flows.collect,  # add this
}
```

**3. Add weight** in `engine/forecast.py`:

```python
SIGNAL_WEIGHTS = {
    ...
    'exchange_inflow_btc': CONFIG.W_ONCHAIN_FLOWS,
}
```

That's it. The collector runs on the next cycle.

### Tuning weights based on real performance

After 30+ days of forecasts:

```bash
python forecast_accuracy.py
```

Look at the **per-direction** and **per-confidence** breakdowns. If `bearish high-confidence` calls are 70% accurate but `bullish high-confidence` are only 50%, your weights are biased toward bullish — adjust.

You can also query the SQLite directly:

```sql
-- Which signals contribute the most to correct forecasts?
SELECT s.name,
       COUNT(*) as n,
       AVG(CASE WHEN f.forecast_correct = 1 THEN s.score ELSE 0 END) as avg_correct_score
FROM signals s
JOIN forecasts f ON s.forecast_id = f.id
WHERE f.forecast_correct IS NOT NULL
GROUP BY s.name
ORDER BY n DESC;
```

### Swapping the LLM provider

Edit `.env`:

**OpenAI (default):**
```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

**OpenRouter (use any model — Claude, Gemini, Llama, etc.):**
```
OPENAI_API_KEY=sk-or-v1-...
OPENAI_MODEL=anthropic/claude-3.5-sonnet
OPENAI_BASE_URL=https://openrouter.ai/api/v1
```

**Local Ollama (free, runs on your machine):**
```
OPENAI_API_KEY=ollama
OPENAI_MODEL=llama3.1
OPENAI_BASE_URL=http://localhost:11434/v1
```

**Azure OpenAI:**
```
OPENAI_API_KEY=<azure-key>
OPENAI_MODEL=<your-deployment-name>
OPENAI_BASE_URL=https://<resource>.openai.azure.com/openai/deployments/<deployment>
```

### Running as a systemd service (Linux)

Create `/etc/systemd/system/btc-forecaster.service`:

```ini
[Unit]
Description=BTC Forecaster
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/btc_forecaster
EnvironmentFile=/path/to/btc_forecaster/.env
ExecStart=/usr/bin/python3 /path/to/btc_forecaster/main.py --loop --interval 3600
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable btc-forecaster
sudo systemctl start btc-forecaster
sudo systemctl status btc-forecaster
journalctl -u btc-forecaster -f   # follow logs
```

### Upgrading from rule-based to ML

Once you have 1-3 months of forecasts in SQLite, you have a labeled dataset. Each row has:
- Per-signal scores (features)
- The actual outcome (label, in `forecast_correct`)

Train a gradient-boosted classifier (`xgboost` / `lightgbm`) on that data and replace the synthesis logic in `engine/forecast.py`. The collector and storage interfaces don't change.

---

## FAQ

### Is this profitable?

**Unknown until you measure it.** That's the whole point of `forecast_accuracy.py`. Run for 30+ days, check accuracy by confidence level, compare to a coin-flip baseline. If your high-confidence calls hit 60%+ on directional outcomes, you have edge. If they're at 50%, the system is no better than guessing.

### Why are some collectors free and others paid?

Crypto data has a tier structure:
- **Truly free, high-quality:** Binance public API, CoinGecko, alternative.me, Deribit
- **Free with API key:** CryptoPanic, NewsAPI, FRED, Coinglass free tier
- **Paid:** Glassnode, CryptoQuant Pro, Whale Alert, LunarCrush, Nansen, Arkham

The architecture lets you start free and upgrade selectively. The biggest single quality boost from a paid feed is **CryptoQuant exchange flows** — that's what would replace the weak whales/onchain collectors.

### How much does the LLM interpretation cost?

With `gpt-4o-mini` and ~1500-token prompts, hourly runs cost about **$0.0003 each**, or roughly **24 cents per month**. Switch to `gpt-4o` for 10x sharper analysis at ~$2.50/month.

### Can I run multiple instances (e.g. for different coins)?

Yes, but you'll need to:
1. Copy the repo
2. Change `DB_PATH` in `config.py` to a different filename
3. Modify the symbol references throughout collectors (currently hardcoded to `BTCUSDT`)

A future refactor could parameterize the symbol. Open to PRs.

### What's the minimum I need for this to be useful?

**Zero API keys + 5 minutes of setup** gets you a functional forecaster using only free public data. Forecast quality at this tier is roughly: passable technicals, decent derivatives, weak news/macro.

**`FRED_API_KEY` + `OPENAI_API_KEY` (both free)** is the sweet spot. ~30 minutes total. Forecast quality jumps significantly because real yields and DXY are two of the highest-impact macro inputs.

### What's the right way to think about the forecast?

It's a **probability tool, not a prediction tool**. The composite score and confidence don't tell you what BTC will do — they tell you what the *current evidence implies*. Evidence shifts. The system updates hourly to track that shift.

Use it like this:
- `composite > +0.5, high confidence`: evidence strongly favors up. Bias longs, avoid shorts.
- `composite < -0.5, high confidence`: evidence strongly favors down. Bias shorts, avoid longs.
- `composite near 0, low confidence`: evidence is mixed. **Don't trade off this.** Wait.
- `composite high but confidence low`: signals point one way but disagree among themselves. Caution.

### Should I trade based on individual signals?

**No.** The whole point of having 20+ signals is that no single one drives the call. If you find yourself reading one signal (e.g. "funding turned negative!") and acting on it, you've defeated the architecture. Use the synthesized output.

### How often should I retrain weights?

Quarterly. Crypto regimes shift — what worked in a bull phase fails in a chop phase. Re-run `forecast_accuracy.py` monthly to spot drift; rebalance weights in `config.py` when one category systematically underperforms its weight.

---

## Disclaimer

Research code, not financial advice. Crypto trading carries substantial risk of loss. This system is a probability tool, not a prediction tool — it organizes evidence into structured opinion, but cannot guarantee outcomes. Test thoroughly. Backtest accuracy before acting on any forecast. Risk only what you can afford to lose.