<p align="center">
  <h1 align="center">Crypto TP/SL Trading System</h1>
  <p align="center">
    Real-time cryptocurrency signal engine with adaptive TP/SL management, live dashboard, and analytics
  </p>
</p>

<p align="center">
  <a href="https://github.com/rishat5081/crypto/actions/workflows/ci.yml"><img src="https://github.com/rishat5081/crypto/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/rishat5081/crypto/actions/workflows/code-quality.yml"><img src="https://github.com/rishat5081/crypto/actions/workflows/code-quality.yml/badge.svg" alt="Code Quality"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/data-Binance_Futures-F0B90B?logo=binance&logoColor=white" alt="Binance">
  <img src="https://img.shields.io/badge/trading-paper_only-orange" alt="Paper Trading">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
</p>

---

## Overview

A data-only crypto futures signal system that monitors Binance Futures markets in real-time, generates LONG/SHORT signals using technical analysis, and tracks paper trade performance with adaptive strategy tuning. No real orders are placed.

### Key Features

- **Multi-Strategy Signal Engine** - EMA crossover, pullback entry, and trend momentum signals
- **Adaptive Feedback Loop** - Strategy parameters auto-adjust after each trade result
- **Live Dashboard** - Real-time monitoring with Chart.js analytics, trade history, and market news
- **Risk Management** - Trailing stops, break-even stops, momentum reversal exits, and stagnation detection
- **Multi-Coin Scanning** - Simultaneous monitoring of 10+ symbols across multiple timeframes
- **Loss Guard System** - Automatic cooldowns after consecutive losses with threshold tightening
- **Performance Analytics** - Equity curve, drawdown, win rate, PnL distribution, and per-symbol breakdown

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Live Dashboard                     │
│         (HTML/CSS/JS + Chart.js)                     │
│  Overview | Analytics | Opportunities | Market       │
│  Activity | History | News | Guard Monitor           │
└─────────────┬───────────────────────────┬───────────┘
              │ HTTP API                  │ WebSocket
┌─────────────▼───────────────────────────▼───────────┐
│              Dashboard Server (server.py)             │
│  /api/state | /api/analytics | /api/history          │
│  /api/news  | /api/symbols   | /api/config           │
└─────────────┬───────────────────────────────────────┘
              │ JSON Lines
┌─────────────▼───────────────────────────────────────┐
│          Live Adaptive Trader                        │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ Strategy │  │  Trade   │  │  Risk Manager    │   │
│  │  Engine  │→ │  Engine  │→ │  (Trail/BE/Cut)  │   │
│  └──────────┘  └──────────┘  └──────────────────┘   │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ Feedback │  │  Loss    │  │  Performance     │   │
│  │  System  │  │  Guard   │  │  Guard           │   │
│  └──────────┘  └──────────┘  └──────────────────┘   │
└─────────────┬───────────────────────────────────────┘
              │ REST API
┌─────────────▼───────────────────────────────────────┐
│         Binance Futures (Public Data Only)            │
│  /fapi/v1/klines | /fapi/v1/premiumIndex             │
│  /fapi/v1/ticker/price                               │
└─────────────────────────────────────────────────────┘
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| **Backend** | Python 3.11+ |
| **Signal Engine** | Custom EMA/RSI/ATR strategy with adaptive tuning |
| **Data Source** | Binance Futures REST API (public endpoints) |
| **Dashboard** | Vanilla HTML/CSS/JS with Chart.js |
| **API Server** | Python http.server |
| **Storage** | JSON Lines (file-based) + MongoDB (optional) |
| **ML Pipeline** | Walk-forward optimizer with logistic classifier |
| **Deployment** | systemd service with Docker MongoDB |

## Quick Start

### One Command Setup

```bash
git clone https://github.com/rishat5081/crypto.git
cd crypto
./run_all.sh
```

This will:
1. Detect OS and install Python if needed
2. Create virtualenv and install dependencies
3. Start the dashboard at `http://127.0.0.1:8787`
4. Run ML optimization on recent market data
5. Start the live adaptive trading loop

### Manual Setup

```bash
# Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run tests
pytest tests/ -v

# Start dashboard
cd frontend && python server.py &

# Start live trading
python run_live_adaptive.py --config config.json
```

## Configuration

All configuration is in `config.json`:

| Section | Key Parameters |
|---------|---------------|
| **Strategy** | `ema_fast/slow`, `rsi_period`, `atr_multiplier`, `risk_reward`, `min_confidence` |
| **Live Loop** | `symbols`, `timeframes`, `max_wait_candles`, `execute_min_*` thresholds |
| **Risk** | `break_even_trigger_r`, `trail_trigger_r`, `max_adverse_r_cut`, `momentum_reversal_*` |
| **Loss Guard** | `max_global_consecutive_losses`, `max_symbol_consecutive_losses`, pause cycles |
| **Performance Guard** | `min_symbol_win_rate`, `rolling_window_trades`, cooldown settings |

## Signal Types

| Type | Trigger | Confidence |
|------|---------|------------|
| **Crossover** | EMA fast crosses slow within lookback window | Full |
| **Pullback** | Price touches fast EMA in established trend | 0.92x |
| **Momentum** | Price moving in trend direction with strong EMA separation | 0.88x |

## Dashboard Sections

| Section | Description |
|---------|-------------|
| **Overview** | Active trade, latest result, performance summary |
| **Analytics** | Equity curve, rolling win rate, PnL distribution, drawdown charts |
| **Opportunities** | Multi-coin candidate pool with probability buckets |
| **Market** | Live price snapshot across all monitored symbols |
| **Activity** | Per-coin status and system event logs |
| **History** | Complete closed trade history |
| **News** | Aggregated crypto market headlines |
| **Guard** | Symbol health and adaptive retuning events |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/state` | Current bot state (active trade, performance, signals) |
| GET | `/api/analytics` | Full analytics (equity curve, drawdown, streaks, PnL) |
| GET | `/api/history` | Closed trade history |
| GET | `/api/news` | Market news headlines |
| GET | `/api/symbols` | Searchable Binance symbol catalog |
| GET | `/api/storage` | MongoDB connection status |
| POST | `/api/config/symbols` | Update watchlist at runtime |

## Project Structure

```
crypto/
├── .github/                 # CI/CD workflows, templates, config
├── src/
│   ├── strategy.py          # Signal generation engine
│   ├── trade_engine.py      # Paper trade lifecycle
│   ├── models.py            # Data models
│   ├── live_adaptive_trader.py  # Main trading loop
│   ├── binance_futures_rest.py  # Binance API client
│   ├── indicators.py        # Technical indicators
│   ├── ml_pipeline.py       # ML walk-forward optimizer
│   └── alerts.py            # Sound/terminal alerts
├── frontend/
│   ├── server.py            # Dashboard API server
│   ├── index.html           # Dashboard UI
│   ├── app.js               # Frontend logic + charts
│   └── styles.css           # Dashboard styling
├── tests/                   # Unit tests
├── config.json              # Configuration
├── run_all.sh               # One-command launcher
├── run_live_adaptive.py     # Live trading entry point
├── deploy_ec2.sh            # EC2 deployment script
└── requirements.txt         # Python dependencies
```

## Production Deployment

### EC2 (One Script)

```bash
chmod +x deploy_ec2.sh
./deploy_ec2.sh
```

Creates a systemd service with auto-restart:

```bash
sudo systemctl status crypto-trader
sudo journalctl -u crypto-trader -f
sudo systemctl restart crypto-trader
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTEND_HOST` | `127.0.0.1` | Dashboard bind address |
| `FRONTEND_PORT` | `8787` | Dashboard port |
| `START_FRONTEND` | `1` | Enable/disable dashboard |
| `MONGO_URI` | `mongodb://127.0.0.1:27017` | MongoDB connection |
| `MONGO_DB` | `crypto_trading_live` | Database name |
| `OPTIMIZE_TIMEOUT_SEC` | `45` | ML optimizer timeout |

## Testing

```bash
# Run all tests
pytest tests/ -v

# With coverage report
pytest tests/ -v --cov=src --cov-report=html

# Validate config
python -c "import json; json.load(open('config.json')); print('OK')"
```

## Documentation

| Document | Description |
|----------|-------------|
| [`docs/HANDBOOK.md`](docs/HANDBOOK.md) | Developer guide: setup, strategy deep-dive, trade lifecycle, deployment |
| [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) | All REST endpoints with request/response schemas |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System design, data flow, module dependencies, data models |
| [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) | Every `config.json` parameter with types and defaults |
| [`docs/LIVE_OPERATIONS.md`](docs/LIVE_OPERATIONS.md) | Live market monitoring, signal diagnosis, runtime control |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history and release notes |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Contribution guidelines and PR process |

---

## Live System Results

Last live scan: **2026-03-20 09:40 UTC** — Binance Futures, 10 symbols × 2 timeframes

| Symbol | Price | RSI (15m) | EMA Bias | Status |
|--------|-------|-----------|----------|--------|
| BTC | $70,655 | 48.8 | Bullish | No crossover |
| ETH | $2,144 | 47.6 | Bullish | No crossover |
| SOL | $88.97 | 46.1 | Bullish | No crossover |
| XRP | $1.449 | 46.5 | Bullish | No crossover |
| BNB | $642.8 | 49.2 | Bullish | No crossover |
| ADA | $0.269 | 45.9 | Bullish | No crossover |
| DOGE | $0.094 | 47.7 | Bullish | No crossover |
| AVAX | $9.531 | 50.3 | Bullish | No crossover |

> Market in **sideways consolidation** — RSI neutral (40–50), funding rates ±0.001–0.010% (healthy).
> System correctly awaiting next EMA(21)/EMA(55) crossover before entering.

---

## ML Performance (Walk-Forward)

| Metric | Value |
|--------|-------|
| Selected trades | 66 |
| Win rate | 60.6% |
| Expectancy-R | 0.38 |
| High-hit backtest | 90% win rate (9/10 trades, 3 symbols, 15m) |

---

## Disclaimer

This is a **decision-support tool** for educational and research purposes. It uses paper trading with live market data. No real orders are placed. No strategy guarantees profits. Use at your own risk. Keep API usage within exchange rate limits.

## License

[MIT](LICENSE)
