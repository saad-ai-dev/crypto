# 🤝 Contributing to Crypto TP/SL Trading System

Thank you for your interest in contributing! This guide will help you get started.

## 🛠️ Development Setup

```bash
# Clone the repository
git clone https://github.com/rishat5081/crypto.git
cd crypto

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install pytest flake8 mypy
```

## 🧪 Running Tests

```bash
# All tests
pytest tests/ -v

# With coverage
pytest tests/ -v --cov=src --cov-report=term-missing

# Single test file
pytest tests/test_strategy.py -v
```

## 🌿 Branch Naming Convention

```
feature/add-rsi-divergence
fix/trailing-stop-calculation
docs/update-api-reference
refactor/strategy-engine
test/add-pullback-tests
chore/update-dependencies
```

## 💬 Commit Convention

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add momentum entry signal type
fix: correct WIN/LOSS classification for trailing stop exits
docs: update API endpoint documentation
test: add pullback entry integration tests
refactor: extract trade exit logic into helper
perf: reduce API calls with batch endpoint
```

## 🚀 Pull Request Process

1. 🌿 Create a feature branch from `main`
2. ✏️ Make your changes with clear, focused commits
3. 🧪 Ensure all tests pass: `pytest tests/ -v`
4. ⚙️ Verify config: `python -c "import json; json.load(open('config.json'))"`
5. 📤 Open a PR with the provided template
6. ✅ Wait for CI checks to pass
7. 👀 Request review from @rishat5081

## 🎨 Code Style

- Max line length: 120 characters
- Use type hints for function signatures
- Follow existing patterns in `src/`
- No hardcoded credentials or API keys
- JSON-line logging format for bot output

## 🏗️ Architecture Overview

```
src/
  strategy.py        # Signal generation (EMA crossover, pullback, momentum)
  trade_engine.py    # Paper trade lifecycle and PnL calculation
  models.py          # Data models (Candle, Signal, OpenTrade, ClosedTrade)
  live_adaptive_trader.py  # Main trading loop with adaptive feedback
  binance_futures_rest.py  # Binance Futures REST API client
  indicators.py      # Technical indicators (EMA, RSI, ATR)
  ml_pipeline.py     # ML walk-forward optimizer

frontend/
  server.py          # Dashboard API server
  index.html         # Dashboard UI
  app.js             # Frontend logic and chart rendering
  styles.css         # Dashboard styling
```

## 🧭 Key Principles

- 📊 **Data-only**: No real orders are placed. This is paper trading with live market data.
- 🛡️ **Conservative fills**: When both TP and SL are hit in the same candle, assume SL hit first.
- 🔄 **Adaptive feedback**: Strategy parameters adjust after each trade result.
- 🌐 **Network resilience**: API clients retry across multiple Binance endpoints with curl fallback.

## ❓ Questions?

Open an [Issue](https://github.com/rishat5081/crypto/issues) or check existing discussions.
