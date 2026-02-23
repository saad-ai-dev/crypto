# Crypto Futures Data-Only Signal System

This folder contains a complete data-only crypto futures scanner with:

- public REST market data pulling (no order placement)
- signal generation (LONG/SHORT)
- entry, take-profit, and stop-loss output
- audible alert when a new trade signal is opened
- one-active-trade-at-a-time lifecycle tracking
- 10 closed-trade validator with adaptive tuning after losses

## Files

- `config.json`: pairs, timeframes, strategy, risk, and validation settings
- `run_scanner.py`: live scanner loop (or one-shot)
- `run_validate_10.py`: sequential 10 closed-trade validation run
- `run_bulk_optimize.py`: multi-coin bulk optimization/backtest on 200 trades
- `fetch_live_cache.sh`: fetch live Binance market snapshots into local JSON cache
- `run_ml_walkforward.py`: ML walk-forward optimizer (feature model + sequential trade validation)
- `run_live_adaptive.py`: continuous live paper-trading loop with auto-feedback tuning
- `src/binance_futures_rest.py`: public Binance futures data client
- `src/strategy.py`: setup logic + adaptive tuning
- `src/trade_engine.py`: paper trade lifecycle and PnL
- `src/alerts.py`: terminal + OS sound alerts

## Run

```bash
cd /Users/user/Desktop/Work/gotoapi/crypto
python3 run_scanner.py --once
python3 run_scanner.py
python3 run_validate_10.py
./fetch_live_cache.sh /Users/user/Desktop/Work/gotoapi/crypto/data/live
python3 run_bulk_optimize.py --cache-dir /Users/user/Desktop/Work/gotoapi/crypto/data/live --target-trades 200 --min-wins 150 --apply-best
python3 run_ml_walkforward.py --cache-dir /Users/user/Desktop/Work/gotoapi/crypto/data/live --target-trades 200 --target-wins 150 --max-candidates 48 --max-candles 500 --apply-best
python3 run_ml_walkforward.py --cache-dir /Users/user/Desktop/Work/gotoapi/crypto/data/live --timeframes 1m,5m,15m --max-candles 700 --single-strategy --initial-train-frac 0.2 --target-trades 200
python3 run_ml_walkforward.py --cache-dir /Users/user/Desktop/Work/gotoapi/crypto/data/live --timeframes 1m,5m,15m --max-candles 700 --single-strategy --initial-train-frac 0.2 --target-trades 200 --fee-bps-per-side 2 --slippage-bps-per-side 1
python3 run_live_adaptive.py
```

## Output

Scanner prints JSON lines, including:
- `NEW_SIGNAL` with pair, timeframe, side, entry, TP, SL, confidence
- `TRADE_CLOSED` with result (`WIN`/`LOSS`) and PnL

Validator prints a JSON report with:
- each signal and trade outcome
- win/loss count and win rate
- expectancy in `R` and USD per trade
- final adapted strategy parameters

## Data source mode

`config.json` -> `data_source`:

- `force_mock: false` keeps live REST as primary
- `allow_mock_fallback: true` falls back to deterministic mock data if API access fails
- `mock_seed` controls repeatable mock runs

For strict real-market runs:

- set `force_mock: false`
- set `allow_mock_fallback: false`

`config.json` -> `execution`:
- `fee_bps_per_side`: exchange fee per side in bps
- `slippage_bps_per_side`: expected slippage per side in bps

`config.json` -> `live_loop`:
- live symbols/timeframes
- scan interval and per-trade max wait
- trade quality filters (`min_rr_floor`, `min_trend_strength`)
- target/stop conditions (`target_trades`, `target_win_rate`, `max_cycles`)

## Important

- This is decision-support tooling, not guaranteed prediction.
- It uses historical candle simulation for validation; real-time outcomes can differ.
- Keep API usage within exchange rate limits.
