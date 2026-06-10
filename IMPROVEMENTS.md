# Critical Improvements — BTC Forecaster

Audit of the full repo (engine, all 9 collectors, backtester, storage, interpreter), prioritized by expected impact on directional accuracy.

**Honest framing first:** no system predicts BTC's next move with "very very high" accuracy — 4h direction is close to a coin flip for everyone. A realistic, valuable target is **55–58% directional accuracy with well-calibrated confidence**, so high-confidence calls are right far more often than low-confidence ones and you can size/skip accordingly. Everything below is aimed at that goal. The single biggest theme: **the system currently has no learning loop — every score and weight is a hand-written guess that has never been validated against outcomes.**

---

## P0 — Critical (these dominate accuracy)

### 1. Close the learning loop: backfill history and fit weights from data
**Problem:** All scores and weights in `config.py` / `engine/forecast.py` are hand-tuned guesses. `forecast_accuracy.py` measures accuracy but nothing feeds back. With 1 forecast/hour you'd wait months for enough live data.

**Fix (highest single-impact change in the repo):**
- Build a **historical feature backfill**: Binance gives full kline history, funding history (`/fapi/v1/fundingRate`), OI history, and long/short ratio history for free. Alternative.me F&G has full daily history. Replay your collectors over the past 2–3 years to generate ~6,500+ (signal-vector → realized 4h return) samples *today* instead of waiting.
- Train a simple model (logistic regression first, then gradient boosting / XGBoost) on that matrix to predict P(up) at the horizon. Use **walk-forward validation** (train on past, test on next month, roll) — never random splits, they leak.
- Keep the rule engine as a fallback/sanity check; the engine docstring already promises "swap in an ML model" — this is how.
- Output a **probability**, not a score, and calibrate it (Platt scaling / isotonic). Evaluate with Brier score and calibration curves, not just hit rate.

### 2. Fix the horizon mismatch — slow signals are forecasting a fast target
**Problem:** The forecast horizon is **4h**, but the highest-weighted inputs are daily/multi-week features: `trend_1d` (weight 2.0), `above_200d_ma`, `rsi_1d`, 5-day macro changes, daily F&G. A 200d MA says almost nothing about the next 4 hours. Today's run shows it: top 5 drivers are all daily-timeframe.

**Fix:**
- For a 4h horizon, weight features measured on 5m–4h scales: 1h/4h momentum and returns, taker buy/sell delta (CVD), funding, basis, OI changes, order-book imbalance.
- Either (a) re-weight so daily/macro features act as a *regime filter* (they gate or scale fast signals, they don't vote on direction), or (b) predict **multiple horizons** (4h, 24h, 7d) with horizon-appropriate feature sets. Option (b) is more useful downstream.

### 3. Condition mean-reversion signals on regime — they fight your trend signals by design
**Problem:** `rsi_score()` in `technicals.py` returns +0.7 for RSI<20 (bet on bounce) while `trend_1d` simultaneously votes −0.6×2.0. F&G is scored contrarian-bullish at extreme fear. In a genuine downtrend, oversold stays oversold and extreme fear gets more extreme — these contrarian bets are exactly wrong in the regime where they fire hardest. Today's output is the proof: composite −0.118, agreement 27%, conclusion "no idea."

**Fix:** Add an explicit regime classifier (e.g., ADX or price vs 50d/200d slope → trending-up / trending-down / ranging) and condition scores on it:
- Ranging regime → mean-reversion rules active (oversold RSI bullish, extreme fear bullish).
- Trending regime → momentum rules active; oversold in a downtrend scores ~0 or mildly bearish (continuation), not +0.7.
This one change resolves the recurring "deeply conflicted, low conviction" output pattern.

### 4. Stop double-counting correlated signals
**Problem:** `trend_1d`, `above_50d_ma`, `above_200d_ma`, `dist_30d_high_pct`, `macd_4h`, `rsi_4h` are all functions of the same price series and are heavily correlated. One bearish daily trend gets counted ~5 times, drowning out independent information (funding, basis, flows). `agreement_pct` is inflated the same way.

**Fix:** Group signals into independent factor buckets — *trend, short-term momentum, positioning (funding/OI/LSR/basis), sentiment, macro regime, flows* — average within a bucket, then weight across buckets. Six quasi-independent votes beat 19 correlated ones. (The ML model in #1 handles this automatically; this is the fix for the rule engine.)

### 5. Replace hardcoded score breakpoints with rolling-distribution normalization
**Problem:** Every collector maps raw values through arbitrary step functions (`if funding > 0.0010: -0.7 ...`). These cliffs are guesses, regime-dependent, and go stale. The funding comment even says "hourly funding" — Binance funding is **8h**, so the thresholds were likely calibrated against the wrong unit.

**Fix:** Score each signal as a **z-score or percentile vs its own rolling history** (e.g., funding vs trailing 30d distribution → smooth score in [−1, 1] via tanh). Store raw series in SQLite to support this. Applies to funding, basis, OI change, DVOL, dominance, mempool, F&G delta.

---

## P1 — High impact

### 6. Fix the composite dilution that forces NEUTRAL
**Problem:** In `engine.synthesize()`, the denominator `total_weight` includes every signal with non-zero weight even when its score is 0 (today: funding 0.0, OI 0.0, dvol 0.0 still add ~3.2 of weight to the denominator). Neutral signals mechanically drag the composite toward 0, so |composite| rarely clears the ±0.30 threshold → system says NEUTRAL almost always. "Always neutral" also games the accuracy metric (see #8).

**Fix:** Decide semantics: a 0-score signal should either mean "no information" (exclude from denominator) or "actively neutral" (keep). For most of these (funding in normal band = genuinely no edge) **exclude them**. Recompute thresholds after the change.

### 7. Use the data you already download but throw away
- Binance klines already include **taker buy volume (`tbbav`)** — `technicals.py` parses it and never uses it. Taker buy/sell delta (CVD) is one of the strongest free short-horizon signals. Zero new API calls.
- `onchain.py`: every signal is hardcoded `score=0.0` — the whole collector contributes nothing. Either derive real scores (fee/mempool *spikes* vs baseline, hashrate trend) or drop the API calls.
- `whales.py` always emits score 0. As written it's pure cost.
- `options.py` makes a `get_book_summary_by_currency` call whose result is never used (dead request), and the documented 25-delta skew signal was never implemented — skew is genuinely predictive; implement it from the option book you're already fetching.
- `blockchain.info/q/hashrate` returns 404 every run (see logs) — endpoint is dead; remove or replace with mempool.space mining API.

### 8. Make the evaluator honest
**Problems in `forecast_accuracy.py`:**
- A NEUTRAL forecast counts as "correct" whenever the move is < threshold — predicting neutral forever scores well. No baseline comparison.
- Evaluation klines limited to `get_klines('1h', 1000)` ≈ 42 days — older forecasts silently never get scored.
- Only direction hit-rate; no measure of whether *composite magnitude* means anything.

**Fix:**
- Report against naive baselines (always-up, persistence/last-4h-sign). Edge = your accuracy minus the best baseline.
- Report per-class precision/recall and the correlation (IC) between composite and realized forward return, plus AUC of P(up). Track Brier score once #1 lands.
- Score outcomes incrementally (a small job that resolves matured forecasts each cycle) instead of refetching a limited window.
- Run accuracy by confidence tier every week; if high-conf ≈ low-conf accuracy, the confidence model is broken — fix before trusting it.

### 9. Add the missing high-value 4h signals (all free)
In rough order of expected value for a 4h horizon:
1. **CVD / taker delta** (already have the data — see #7).
2. **Coinbase–Binance spot premium** (US institutional bid; free from both public APIs).
3. **Funding z-score + OI-weighted funding history** (free from Binance `/fapi/v1/fundingRate`).
4. **ETF net flows** (daily, e.g. Farside; the dominant structural flow post-2024 and entirely absent here).
5. **Event calendar gating**: CPI, FOMC, NFP timestamps are known in advance. Pre-event, force confidence to LOW regardless of composite — predicting through an FOMC release destroys average accuracy.
6. **Time-of-day/weekend effects** (US open vol, weekend illiquidity) as features.
7. **Liquidation clusters / nearby liquidity** (Coinglass free tier; you already have the key wiring).
8. **Stablecoin mcap 7d change** (CoinGecko, free) — dry-powder proxy.

### 10. Fix news sentiment substring matching (real bugs)
`_score_headline()` uses `kw in lower` — raw substring matching: `'ath'` matches "death"/"marathon", `'ban'` matches "bank", `'rug'` matches "struggle", `'short'` matches "shortly", `'green'` matches "evergreen". Headlines get scored on accidents.

**Fix:** Tokenize with word boundaries (`\b` regex) at minimum. Better: you already pay for an LLM call per cycle — have it score the top 20 headlines (−1..+1 each) in the same call or a cheap separate one. Also add negation handling ("fails to rally" currently scores bullish).

---

## P2 — Meaningful refinements

### 11. Labels: use triple-barrier instead of fixed-horizon close
Close-to-close at exactly +4h is noisy and ignores path. Triple-barrier labeling (first touch of +X%, −X%, or time expiry) produces cleaner labels and maps directly to how a trade would actually resolve. (López de Prado, *Advances in Financial ML*.)

### 12. Calibrate confidence from history, not thresholds
`Confidence.HIGH/MEDIUM/LOW` comes from arbitrary composite/agreement cutoffs, and per-signal confidence is also hand-assigned (RSI is always HIGH conf regardless of regime). Once you have outcome history, define confidence tiers by *realized* hit rate buckets.

### 13. Technical indicator correctness
- `rsi()` uses simple rolling means — standard RSI uses Wilder's smoothing (`ewm(alpha=1/period)`). Your values diverge from what every other trader sees, which matters for an indicator whose edge is behavioral.
- `ma_50`/`ma_200` via `tail(n).mean()` include the current *unfinished* daily candle — intraday noise flips the above/below-MA signals; use completed candles.
- BTC price for the forecast comes from the last 1h kline close (up to 1h stale) in one place and a live ticker in another — standardize on the live ticker.

### 14. Data hygiene
- Add a staleness check per signal (e.g., FRED series can be days old); decay weight by age.
- Cache & dedupe HTTP calls within a cycle (spot ticker is fetched 3 times per run).
- Persist raw collector payloads (compressed) so you can re-derive new features over past data without re-collecting.

### 15. Keep the LLM out of the prediction path (it's correctly placed now)
The interpreter is presentation-only — good, keep it that way. An LLM adds zero predictive accuracy over the structured signals it's handed. Its highest-value roles are: headline sentiment scoring (#10) and event extraction (#9.5). Don't let prose feed back into the composite.

---

## Suggested order of execution

| Step | Item | Why first |
|------|------|-----------|
| 1 | ✅ **DONE** — #7 quick wins + #10 bug fixes + #6 dilution fix (+ funding 8h-unit fix, IV skew signal) | Days of work, immediate signal-quality gain |
| 2 | #1 historical backfill + feature matrix | Unblocks everything data-driven |
| 3 | #8 honest evaluator + baselines | You can't improve what you can't measure |
| 4 | #3 regime conditioning + #4 factor buckets | Biggest rule-engine accuracy lift |
| 5 | #2 horizon-matched features + #9 new signals | Feeds the model real short-horizon edge |
| 6 | #1 model training + calibrated probabilities | Replace hand weights; ship P(up) |
| 7 | #11–#14 refinements | Compounding quality |

**Measure of success:** not raw hit rate, but (a) accuracy minus naive-baseline accuracy, (b) monotonically increasing accuracy across confidence tiers, (c) Brier score trending down. If those three hold on walk-forward data, the system has real, usable edge.
