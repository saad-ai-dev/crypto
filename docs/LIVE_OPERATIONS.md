# Live Operations Guide

Operational reference for running, monitoring, and diagnosing the live trading system.

---

## Table of Contents

- [Starting the System](#starting-the-system)
- [Live Market Scan](#live-market-scan)
- [Reading the Output](#reading-the-output)
- [Signal Diagnosis](#signal-diagnosis)
- [No Signals — What It Means](#no-signals--what-it-means)
- [Indicator Reference](#indicator-reference)
- [Runtime Control](#runtime-control)
- [Monitoring Checklist](#monitoring-checklist)
- [Common Issues](#common-issues)

---

## Starting the System

### Full stack (recommended)

```bash
./run_all.sh
```

Starts dashboard on `http://127.0.0.1:8787` and the live trading loop.

### Components individually

```bash
# Terminal 1 — Dashboard
cd frontend && python3 server.py

# Terminal 2 — Live trader
python3 run_live_adaptive.py --config config.json
```

### Quick market scan (no trader loop)

```python
from src.binance_futures_rest import BinanceFuturesRestClient
from src.strategy import StrategyEngine
from src.config import load_config

client = BinanceFuturesRestClient()
config = load_config("config.json")
strategy = StrategyEngine.from_dict(config["strategy"])

# Fetch all live prices (returns dict: {symbol: price})
prices = client.fetch_all_ticker_prices()
print(prices["BTCUSDT"])   # e.g. 70655.5

# Fetch market context (mark price, funding rate, open interest)
ctx = client.fetch_market_context("BTCUSDT")
print(ctx.mark_price, ctx.funding_rate, ctx.open_interest)

# Evaluate signal on a symbol/timeframe
klines = client.fetch_klines("BTCUSDT", "15m", limit=260)
signal = strategy.evaluate("BTCUSDT", "15m", klines, ctx)
print(signal)  # None if no setup, Signal object if triggered
```

---

## Live Market Scan

The system scans 10 symbols × 2 timeframes (5m + 15m) = **20 combinations per cycle**.

### Symbols monitored

```
XRPUSDT, SOLUSDT, ADAUSDT, BNBUSDT, BTCUSDT,
ETHUSDT, DOGEUSDT, AVAXUSDT, DOTUSDT, LINKUSDT
```

### Cycle output (JSON Lines)

Each cycle emits two JSON events to stdout (piped to `data/live_events.jsonl`):

**1. `LIVE_MARKET`** — price snapshot

```json
{
  "type": "LIVE_MARKET",
  "time": "2026-03-20T09:40:17Z",
  "snapshots": [
    {"symbol": "BTCUSDT", "price": 70655.5, "time": 0},
    {"symbol": "ETHUSDT", "price": 2144.6, "time": 0}
  ]
}
```

**2. `POSSIBLE_TRADES`** — filtered candidates

```json
{
  "type": "POSSIBLE_TRADES",
  "time": "2026-03-20T09:40:34Z",
  "cycle": 1,
  "min_candidate_confidence": 0.65,
  "min_candidate_expectancy_r": 0.05,
  "total_candidates_seen": 3,
  "total_possible_trades": 1,
  "trades": [
    {
      "symbol": "ETHUSDT",
      "timeframe": "15m",
      "side": "LONG",
      "entry": 2144.60,
      "take_profit": 2165.23,
      "stop_loss": 2127.41,
      "confidence": 0.712,
      "rr": 1.21,
      "expectancy_r": 0.27,
      "score": 0.6834,
      "win_probability": 0.68,
      "probability_bucket": "likely_win",
      "reason": "CROSSOVER-LONG ema_gap=0.42%"
    }
  ]
}
```

---

## Reading the Output

### Signal fields

| Field | Description |
|-------|-------------|
| `side` | `"LONG"` or `"SHORT"` |
| `entry` | Candle close price at signal time |
| `take_profit` | Entry ± (ATR × atr_multiplier × risk_reward) |
| `stop_loss` | Entry ∓ (ATR × atr_multiplier) |
| `confidence` | Strategy confidence score [0.0–1.0] |
| `rr` | Risk/reward ratio = (TP−entry) / (entry−SL) |
| `expectancy_r` | Expected R per trade = (conf × rr) − ((1−conf) × 1) − cost_r |
| `score` | Composite: 65% confidence + 25% trend strength + 10% adjusted RR |
| `win_probability` | Bayesian estimate using historical symbol performance |
| `probability_bucket` | `likely_win`, `likely_loss`, `uncertain`, `strong_win` |
| `reason` | Human-readable trigger: e.g., `CROSSOVER-LONG ema_gap=0.35%` |

### Exit reason prefixes

| Prefix | Meaning |
|--------|---------|
| `TP:` | Take profit hit |
| `SL:` | Stop loss hit |
| `BE-SL:` | Break-even stop hit |
| `TRAIL-SL:` | Trailing stop hit |
| `REVERSAL:` | Momentum reversal cut |
| `STAGNATION:` | No progress after N bars |
| `TIMEOUT:` | Max candle wait exceeded |
| `ADVERSE-CUT:` | Forced close at 1.1× adverse R |

---

## Signal Diagnosis

Use this script to understand **why** no signal fired on a given symbol:

```python
from src.binance_futures_rest import BinanceFuturesRestClient
from src.indicators import ema, ema_series, rsi, atr
from src.config import load_config

client = BinanceFuturesRestClient()
config = load_config("config.json")
p = config["strategy"]

sym, tf = "BTCUSDT", "15m"
klines = client.fetch_klines(sym, tf, limit=260)
ctx    = client.fetch_market_context(sym)
closes = [c.close for c in klines]
entry  = closes[-1]

ema_f   = ema(closes, p["ema_fast"])
ema_s   = ema(closes, p["ema_slow"])
rsi_v   = rsi(closes, p["rsi_period"])
atr_v   = atr(klines, p["atr_period"])
atr_pct = atr_v / entry

# Crossover check
fast_s = ema_series(closes, p["ema_fast"])
slow_s = ema_series(closes, p["ema_slow"])
look   = p["crossover_lookback"] + 1
diffs  = [fast_s[-look+k] - slow_s[-look+k] for k in range(look)]
bull_x = any(diffs[j] <= 0 and diffs[j+1] > 0 for j in range(len(diffs)-1))
bear_x = any(diffs[j] >= 0 and diffs[j+1] < 0 for j in range(len(diffs)-1))

print(f"EMA fast vs slow : {'fast > slow (bullish)' if ema_f > ema_s else 'fast < slow (bearish)'}")
print(f"RSI              : {rsi_v:.1f}  (long={p['long_rsi_min']}–{p['long_rsi_max']}, short={p['short_rsi_min']}–{p['short_rsi_max']})")
print(f"ATR%             : {atr_pct*100:.3f}%  (range {p['min_atr_pct']*100:.2f}%–{p['max_atr_pct']*100:.1f}%)")
print(f"Funding rate     : {ctx.funding_rate*100:+.4f}%  (limit ±{p['funding_abs_limit']*100:.3f}%)")
print(f"Bullish crossover: {bull_x}")
print(f"Bearish crossover: {bear_x}")
```

---

## No Signals — What It Means

The strategy requires **all conditions simultaneously**:

### For a LONG signal
1. EMA(21) > EMA(55) — bullish trend
2. EMA crossover within last 12 bars
3. RSI between 45 and 72
4. Price ≥ fast EMA
5. |funding_rate| ≤ 0.001
6. ATR% between 0.15% and 3.0%

### For a SHORT signal
1. EMA(21) < EMA(55) — bearish trend
2. EMA crossover within last 12 bars
3. RSI between 18 and 50
4. Price ≤ fast EMA
5. |funding_rate| ≤ 0.001
6. ATR% between 0.15% and 3.0%

### Typical blockers

| Blocker | What it means | Action |
|---------|---------------|--------|
| No EMA crossover | Market is mid-trend or consolidating — the crossover already happened | Wait for next momentum shift |
| RSI out of range | RSI too neutral (40–45) — no directional conviction | Normal in sideways markets |
| Funding rate too high | Futures market over-leveraged; risk of squeeze | System correctly avoids trade |
| ATR% too low | Market is too quiet / choppy | May occur in late-session hours |
| Crossover drift | Crossover happened but price already moved >0.5 ATR from it | Fresh signal will come |

### Live observation (2026-03-20)

All 20 symbol/timeframe combos blocked by:
- **Primary**: no EMA crossover in last 12 bars
- Market context: RSI 40–50 (neutral), funding rates ±0.001–0.010% (healthy)
- All ATR values within valid range
- This is **normal** — not a bug. The system waits for quality setups.

---

## Indicator Reference

### EMA (Exponential Moving Average)

```python
from src.indicators import ema, ema_series
val = ema(close_prices, period=21)        # Single latest value
series = ema_series(close_prices, period=21)  # All values
```

### RSI (Relative Strength Index)

```python
from src.indicators import rsi
val = rsi(close_prices, period=14)  # Returns latest RSI value
```

### ATR (Average True Range)

```python
from src.indicators import atr
val = atr(candles, period=14)  # candles = list of Candle objects
```

---

## Runtime Control

Edit `data/runtime_control.json` while the system is running to change behavior:

```json
{
  "pause": false,
  "symbols": ["BTCUSDT", "ETHUSDT"],
  "max_cycles": 200
}
```

Changes are applied within 1 poll cycle (~12 seconds). The file is watched by `_apply_runtime_control()`.

---

## Monitoring Checklist

Use these checks during a live session:

```bash
# 1. Are prices being fetched?
tail -f data/live_events.jsonl | grep LIVE_MARKET

# 2. Any signals firing?
tail -f data/live_events.jsonl | grep -E '"side":|POSSIBLE_TRADES'

# 3. Any trades opened?
tail -f data/live_events.jsonl | grep TRADE_OPEN

# 4. Any guard events?
tail -f data/live_events.jsonl | grep -E 'LOSS_GUARD|PERFORMANCE_GUARD'

# 5. System health via dashboard
curl http://127.0.0.1:8787/api/health

# 6. Current trade state
curl http://127.0.0.1:8787/api/state | python3 -m json.tool
```

---

## Common Issues

### `'BinanceFuturesRestClient' object has no attribute 'get_ticker_24h'`

The client does not expose 24h ticker. Use `fetch_all_ticker_prices()` instead:
```python
prices = client.fetch_all_ticker_prices()  # returns dict {symbol: price}
price = prices.get("BTCUSDT", 0)
```

### `'MarketContext' object has no attribute 'index_price'`

`MarketContext` only has three fields:
```python
ctx.mark_price    # float
ctx.funding_rate  # float (e.g. -0.00001546)
ctx.open_interest # float (e.g. 87383.7)
```

### `fetch_all_ticker_prices()` returns zeros

The return type is `dict`, not a list of dicts:
```python
# Wrong
for p in prices:
    val = float(p["price"])  # KeyError

# Correct
val = prices.get("BTCUSDT", 0)
```

### Trader produces no output / empty log file

Python buffering can suppress output when piped. Use `-u` flag:
```bash
python3 -u run_live_adaptive.py > live.log 2>&1
```

Or redirect directly:
```bash
python3 run_live_adaptive.py 2>&1 | tee data/session.log
```

### No signals for extended period

This is expected in range-bound markets. The strategy requires a **fresh EMA crossover** — not just aligned EMAs. Use the signal diagnosis script above to confirm what's blocking each symbol. Consider widening `crossover_lookback` from 12 to 18–24 in `config.json` if the market is trending but slow to cross.
