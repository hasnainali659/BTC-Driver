# BTC Forecaster

A standalone hourly forecasting service for Bitcoin. Aggregates technicals,
derivatives positioning, on-chain activity, sentiment, news, macro, and
options data into a single directional forecast (bullish/bearish/neutral)
with confidence and key drivers.

Designed to be consumed by other systems (e.g. the alt scanner) via a
JSON file or direct DB query — runs as its own process, has its own
data lifecycle, has its own accuracy backtester.

---

## Why a separate repo?

BTC drives everything else in crypto. Trying to do BTC analysis as one
feature inside a multi-coin scanner is the wrong architecture:

- **BTC analysis needs its own data pipelines** (FRED, Coinglass, news APIs)
  that have nothing to do with the scanner's per-coin work
- **It needs its own cadence** — hourly is right for BTC, 15min is right
  for the scanner
- **It needs its own backtester** — forecast accuracy is a different
  evaluation than scanner hit rate
- **It produces a clean output contract** — direction + confidence + reasons —
  that any downstream system can consume

---

## Architecture

```
            ┌─────────────┐
            │  Collectors │  (one module per data domain)
            └──────┬──────┘
                   │ Signal[]   <- list of normalized signals
                   ▼
            ┌─────────────┐
            │   Engine    │  (rule-based scoring; ML-ready)
            └──────┬──────┘
                   │ Forecast
                   ▼
            ┌─────────────┐
            │   Storage   │  (SQLite history + JSON export)
            └──────┬──────┘
                   │
                   ▼
            ┌─────────────┐    ┌─────────────────┐
            │  Alerter    │    │ Downstream      │
            │ (Telegram)  │    │ Scanner         │
            └─────────────┘    └─────────────────┘
```

Each collector is independent. Add or remove without touching the engine.

---

## Collectors

| Collector | Data | Free? |
|-----------|------|-------|
| `technicals` | Multi-TF trend, RSI, MACD, ATR, BB, key levels (Binance) | ✓ |
| `derivatives` | Funding rate, OI, LSR, basis (Binance Futures) | ✓ |
| `dominance` | BTC.D, total mcap (CoinGecko) | ✓ |
| `options` | DVOL volatility index (Deribit) | ✓ |
| `onchain` | Mempool, fees, hashrate (mempool.space + blockchain.info) | ✓ |
| `sentiment` | Fear & Greed Index (alternative.me) | ✓ |
| `news` | Crypto + macro headlines (CryptoPanic/NewsAPI) | partial — keys boost |
| `macro` | DXY, real yields, SPX, gold (FRED + Yahoo) | partial — FRED key boost |
| `whales` | Recent large mempool TXs | weak without paid feeds |

**Honest note:** the whales and exchange-flow collectors are deliberately
basic. For real edge here you need paid feeds (Whale Alert, CryptoQuant,
Arkham). The architecture supports them — wire them in when the budget
makes sense.

---

## Setup

```bash
cd btc_forecaster
pip install -r requirements.txt

# Optional API keys — collectors gracefully no-op without them
export COINGLASS_API_KEY="..."     # liquidations, exchange flows
export CRYPTOPANIC_API_KEY="..."   # crypto news (free tier ~200/day)
export FRED_API_KEY="..."          # US Treasury yields, DXY (free)
export NEWSAPI_API_KEY="..."       # macro news (free tier 100/day)
export LUNARCRUSH_API_KEY="..."    # social momentum (paid)
export TELEGRAM_BOT_TOKEN="..."    # alerts
export TELEGRAM_CHAT_ID="..."
```

---

## Usage

```bash
# One-off forecast
python main.py

# Continuous loop (default hourly)
python main.py --loop

# Faster loop
python main.py --loop --interval 1800   # every 30 min

# Don't alert this run
python main.py --quiet

# Don't persist (testing)
python main.py --dry-run
```

After every cycle, the latest forecast is written to:
```
logs/latest_forecast.json
```

That file is the contract for downstream consumers.

---

## Forecast output schema

```json
{
  "id": 42,
  "timestamp": "2026-05-06T10:00:00",
  "horizon_hours": 4,
  "direction": "bullish",
  "composite": 0.412,
  "confidence": "medium",
  "agreement_pct": 68.0,
  "signal_count": 23,
  "btc_price": 81250.0,
  "summary": "BTC $81,250 | ↑ BULLISH ...",
  "key_drivers": [
    {"name": "trend_4h", "category": "technical",
     "value": "up", "score": 0.6, "contribution": 0.84},
    {"name": "funding_rate", "category": "derivatives",
     "value": "-0.00012", "score": 0.7, "contribution": 0.74},
    ...
  ],
  "contradictions": [...],
  "actual_btc_price_at_horizon": null,    // filled by accuracy backtester
  "actual_change_pct": null,
  "forecast_correct": null
}
```

---

## Accuracy backtesting

After 24h+ of running, evaluate how well the forecaster actually
predicts:

```bash
python forecast_accuracy.py
python forecast_accuracy.py --threshold 0.5
```

Outputs:
- Overall accuracy
- Accuracy by confidence level (high/medium/low)
- Accuracy by direction (bullish/bearish/neutral)
- Confusion matrix
- CSV with full per-forecast breakdown

**What to look for:**
- Overall directional accuracy >50% = real edge
- High-confidence accuracy markedly higher than low = confidence filter works
- Confusion matrix shows where systematic errors are (e.g. always too bullish)

Use this output to tune weights in `config.py`.

---

## Integration with the scanner

In your scanner's `main.py`, replace the local BTC analysis with:

```python
from btc_forecaster_client import (
    load_latest_forecast, forecast_to_score_multiplier, should_trade_alts
)

forecast = load_latest_forecast("/path/to/btc_forecaster/logs/latest_forecast.json")

if not should_trade_alts(forecast):
    logger.warning(f"BTC forecaster: {forecast['summary']}")
    return  # stand down

multiplier = forecast_to_score_multiplier(forecast)
# apply multiplier to candidate scores...
```

The scanner becomes simpler — one read, one number — and the BTC analysis
gets to be as deep as you want without ballooning the scanner repo.

---

## File structure

```
btc_forecaster/
├── config.py                    # all tunables + API keys (env)
├── signals.py                   # Signal + Forecast dataclasses
├── http_util.py                 # shared retry/timeout HTTP helper
├── collectors/
│   ├── technicals.py            # price, RSI, MACD, ATR, levels
│   ├── derivatives.py           # funding, OI, LSR, basis, liqs
│   ├── dominance.py             # BTC.D, total mcap
│   ├── options.py               # DVOL
│   ├── onchain.py               # mempool, fees, hashrate
│   ├── sentiment.py             # Fear & Greed
│   ├── news.py                  # CryptoPanic + NewsAPI sentiment
│   ├── macro.py                 # DXY, yields, SPX, gold
│   └── whales.py                # large TX proxy
├── engine/
│   └── forecast.py              # weighted synthesis → Forecast
├── storage/
│   └── db.py                    # SQLite persistence + JSON export
├── alerter.py                   # Telegram formatter
├── btc_forecaster_client.py     # downstream consumer helper
├── main.py                      # orchestrator
├── forecast_accuracy.py         # backtest forecast accuracy
└── requirements.txt
```

---

## How to use this responsibly

1. **Run for 7 days first.** Don't act on it. Just log.
2. **Run `forecast_accuracy.py` daily.** Watch how the system actually
   performs. The composite score is meaningless until validated.
3. **Tune weights in `config.py`** based on accuracy by category. If
   `derivatives` signals consistently disagree with outcomes, drop
   their weight. If `technicals` are reliable, raise theirs.
4. **Add ML when you have data.** Once you have 1-3 months of
   `(signals → outcome)` pairs in the DB, train a gradient-boosted
   classifier on the per-signal scores. The current rule-based engine
   becomes the baseline to beat.
5. **Don't trust any single signal.** The whole point is the synthesis.
   If you find yourself reading individual signals and acting on them,
   you've lost the architecture.

---

## Disclaimer

Research code, not financial advice. Crypto trading carries substantial
risk of loss. This system is a probability tool, not a prediction tool —
its purpose is to organize what we know into a structured opinion,
not to guarantee outcomes.
