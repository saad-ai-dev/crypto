# Developer Handbook

Complete guide for developing, debugging, and operating the Crypto TP/SL Trading System.

## Table of Contents

- [Development Setup](#development-setup)
- [Running the System](#running-the-system)
- [Strategy Deep Dive](#strategy-deep-dive)
- [Trade Lifecycle](#trade-lifecycle)
- [Risk Management](#risk-management)
- [Dashboard](#dashboard)
- [Debugging](#debugging)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)

---

## Development Setup

### Prerequisites

- Python 3.11 or higher
- pip
- Git
- MongoDB (optional, for persistence)

### Installation

```bash
git clone https://github.com/rishat5081/crypto.git
cd crypto
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest flake8  # dev dependencies
```

### Verify Installation

```bash
pytest tests/ -v                    # 33 tests should pass
python -c "import json; json.load(open('config.json'))"
python -c "from src.strategy import StrategyEngine; print('OK')"
```

---

## Running the System

### Full Automated Launch

```bash
./run_all.sh
```

This handles everything: deps, optimization, dashboard, live trading.

### Manual Component Launch

```bash
# Terminal 1: Dashboard server
cd frontend && python server.py
# Dashboard at http://127.0.0.1:8787

# Terminal 2: Live trading
python run_live_adaptive.py --config config.json

# Output goes to stdout as JSON Lines
# Pipe to file: python run_live_adaptive.py --config config.json | tee data/live_events.jsonl
```

### Other Runners

```bash
python run_ml_walkforward.py      # ML walk-forward optimization
python run_retune_thresholds.py   # Retune from trade history
python run_validate_10.py         # Quick 10-trade validation
python run_bulk_optimize.py       # Grid-search parameters
python discover_symbols.py        # Find available symbols
```

---

## Strategy Deep Dive

### Signal Generation Pipeline

```
Candles (260 bars) → Indicators → Signal Detection → Confidence Score → Filter → Signal
```

### Indicators

| Indicator | Period | Purpose |
|-----------|--------|---------|
| EMA Fast | 21 | Short-term trend |
| EMA Slow | 55 | Long-term trend |
| RSI | 14 | Overbought/oversold |
| ATR | 14 | Volatility for TP/SL sizing |

### Three Signal Types

#### 1. Crossover Entry
- **Trigger**: EMA(21) crosses EMA(55) within last 12 bars
- **Filter**: RSI in range, price above/below fast EMA, funding rate OK
- **Confidence**: Full (no discount)
- **Stale filter**: Rejects crossovers 3+ bars old where price moved >0.5 ATR

#### 2. Pullback Entry
- **Trigger**: Price within 1.2 ATR of fast EMA in established trend (EMA gap > 0.3%)
- **Filter**: Same RSI/funding filters
- **Confidence**: 0.92x multiplier
- **Use case**: Catches trend continuations when crossovers are rare

#### 3. Momentum Entry
- **Trigger**: Price moving in trend direction, EMA gap > 0.4%, price beyond fast EMA
- **Filter**: RSI alignment
- **Confidence**: 0.88x multiplier
- **Use case**: Strong trending moves where pullback hasn't occurred

### Confidence Calculation

```
confidence = 0.10 (base)
           + 0.40 * trend_score      # EMA separation normalized
           + 0.20 * rsi_score        # Proximity to RSI sweet spot
           + 0.18 * vol_score        # ATR position in allowed range
           + 0.12 * funding_score    # Low funding = good
```

### TP/SL Placement

```
SL distance = ATR(14) * atr_multiplier (1.5)
TP distance = SL distance * risk_reward (1.2)

LONG:  SL = entry - SL_distance,  TP = entry + TP_distance
SHORT: SL = entry + SL_distance,  TP = entry - TP_distance
```

---

## Trade Lifecycle

### Opening

1. `_signal_candidates()` generates all viable signals
2. Filtered by `min_candidate_confidence` and `min_candidate_expectancy_r`
3. Win probability estimated (60% setup quality + 40% symbol history)
4. Further filtered by `execute_min_*` thresholds
5. Best candidate by score is selected
6. Trade opened via `TradeEngine.maybe_open_trade()`

### Monitoring (`_wait_for_close`)

Each candle close triggers:
1. **TP/SL check**: `OpenTrade.update_with_candle()` checks if levels are hit
2. **Trailing stop**: If `best_r >= 0.5`, trail SL keeping 85% of peak
3. **Break-even**: If `best_r >= 0.8`, move SL to entry + offset
4. **Adverse cut**: If worst intra-bar R < -1.1R, force close
5. **Momentum reversal**: 3+ consecutive adverse bars AND current R < -0.4R
6. **Stagnation**: 6+ bars with best_r < 0.1R
7. **Candle timeout**: After 12 candles
8. **Network protection**: 5 consecutive API failures → force close

### Closing

1. `ClosedTrade` created with `pnl_r` and `pnl_usd`
2. **Critical**: If `pnl_r > 0`, result is always "WIN" (even if SL triggered)
3. Trade recorded in `recent_trades` and `symbol_recent_trades`
4. Feedback applied: tighten on loss, relax on win
5. Loss guard checked: pause after consecutive losses
6. Performance guard: cool down weak symbols

---

## Risk Management

### Adaptive Feedback

After each trade, execution thresholds adjust:

| Parameter | On LOSS | On WIN |
|-----------|---------|--------|
| Symbol confidence | +0.015 (max 0.93) | -0.015 (min 0.50) |
| execute_min_confidence | +0.0015 (max 0.92) | -0.004 (min 0.58) |
| execute_min_expectancy_r | +0.003 (max 0.50) | -0.01 (min 0.03) |
| execute_min_score | +0.0015 (max 0.85) | -0.004 (min 0.50) |

**Design principle**: Steps are intentionally tiny to prevent filter lockout.

### Loss Guard

- **Symbol level**: 3 consecutive losses → pause symbol for 4 cycles
- **Global level**: 3 consecutive losses → pause all trading for 3 cycles + tighten thresholds

### Performance Guard

- Rolling window of 12 trades per symbol
- Symbols with <40% win rate or negative expectancy get cooled down for 6 cycles
- Minimum 3 active symbols maintained (prevents total lockout)

### Filter Relaxation

If execution filters block all candidates for 6 consecutive cycles, they gradually relax back toward floor values.

---

## Dashboard

### Architecture

```
Browser ←→ server.py (port 8787) ←→ data/live_events.jsonl
                                  ←→ MongoDB (optional)
                                  ←→ Binance API (news, symbols)
```

### Sections

| Tab | Polls | Interval | Content |
|-----|-------|----------|---------|
| Overview | `/api/state` | 2s | Active trade, latest result, performance |
| Analytics | `/api/analytics` | 10s | Charts: equity, win rate, PnL, drawdown |
| Opportunities | `/api/state` | 2s | Candidate pool with probability buckets |
| Market | `/api/state` | 2s | Live prices for all symbols |
| Activity | `/api/state` | 2s | Per-coin status + system logs |
| History | `/api/history` | 10s | Complete trade history table |

### Charts (Chart.js)

- **Equity Curve**: Cumulative PnL (R) over time
- **Rolling Win Rate**: Win rate over last 10 trades
- **PnL Distribution**: Histogram of trade outcomes
- **Drawdown**: Maximum equity drawdown percentage

---

## Debugging

### Reading Bot Output

All output is JSON Lines. Parse with:
```bash
cat data/live_events.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line.strip())
    if d['type'] == 'TRADE_RESULT':
        t = d['trade']
        print(f'{t[\"symbol\"]} {t[\"result\"]} pnl_r={t[\"pnl_r\"]:.4f}')
"
```

### Common Debug Patterns

**No candidates generated:**
```python
# Check each symbol's indicators manually
python3 -c "
from src.binance_futures_rest import BinanceFuturesRestClient
from src.indicators import ema, rsi, atr
client = BinanceFuturesRestClient()
candles = client.fetch_klines('BTCUSDT', '15m', 260)
closes = [c.close for c in candles]
print(f'EMA(21)={ema(closes,21):.2f} EMA(55)={ema(closes,55):.2f}')
print(f'RSI={rsi(closes,14):.1f} ATR%={atr(candles,14)/closes[-1]:.4f}')
"
```

**Execution filter blocking:**
Look for `NO_SIGNAL` events with `reason: EXECUTION_FILTER_BLOCK` in the output.

**Trade never closes:**
Check `TRADE_MONITOR_FETCH_ERROR` events. Network issues cause the monitor to retry.

---

## Deployment

### EC2 Production

```bash
chmod +x deploy_ec2.sh
./deploy_ec2.sh
```

Creates systemd service `crypto-trader` that:
- Auto-starts on boot
- Restarts on failure (5s delay)
- Logs to journalctl

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTEND_HOST` | `127.0.0.1` | Dashboard bind address |
| `FRONTEND_PORT` | `8787` | Dashboard port |
| `MONGO_URI` | `mongodb://127.0.0.1:27017` | MongoDB connection |
| `MONGO_DB` | `crypto_trading_live` | Database name |
| `START_FRONTEND` | `1` | Set `0` to disable UI |

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Zero candidates | Market not trending or ATR out of range | Check indicator values, widen RSI range |
| All trades timeout | TP too far from entry | Lower `atr_multiplier` or increase `max_wait_candles` |
| Bot hangs | Network timeout in trade monitor | Network error protection auto-closes after 5 failures |
| Filters too tight | Feedback loop over-tightened | Relaxation kicks in after 6 blocked cycles, or manually edit config |
| WIN classified as LOSS | Missing `pnl_r > 0` check | Fixed in `models.py:OpenTrade.update_with_candle()` |
| Dashboard not updating | JSONL file not being written | Check bot is running and `tee` is piping to the file |

---

## Known API Behaviors

These are confirmed from live testing against Binance Futures:

### `BinanceFuturesRestClient`

| Method | Return type | Notes |
|--------|-------------|-------|
| `fetch_all_ticker_prices()` | `dict[str, float]` | Keyed by symbol, e.g. `{"BTCUSDT": 70655.5}` — not a list |
| `fetch_market_context(symbol)` | `MarketContext` | Fields: `mark_price`, `funding_rate`, `open_interest` only |
| `fetch_klines(symbol, interval, limit)` | `list[Candle]` | Returns up to `limit` candles; check `len >= 60` before use |
| `fetch_all_premium_index()` | `dict` | Bulk funding rate fetch — used by `_refresh_batch_market_data()` |

### `MarketContext` fields

```python
ctx.mark_price    # float — futures mark price
ctx.funding_rate  # float — current funding rate (e.g. -0.0000154)
ctx.open_interest # float — total open contracts
# ctx.index_price does NOT exist
```

### Output buffering

When redirecting stdout to a file, Python may buffer output. Always use:
```bash
python3 -u run_live_adaptive.py 2>&1 | tee data/session.log
```

See [`docs/LIVE_OPERATIONS.md`](LIVE_OPERATIONS.md) for the complete operations guide.
