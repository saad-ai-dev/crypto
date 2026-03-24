# Changelog

All notable changes to the Crypto TP/SL Trading System are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### In Progress
- Dashboard UI improvements (branch: `creating-UI`)
- Multi-platform job scraper integration

---

## [1.3.0] — 2026-03-20

### Added
- Live market scan diagnostic tool (signal blocker analysis per symbol/timeframe)
- Real-time indicator snapshot: EMA bias, RSI, ATR%, funding rate per symbol
- Live run confirmed on Binance Futures: 10 symbols × 2 timeframes = 20 combinations scanned
- `docs/LIVE_OPERATIONS.md` — operational runbook for live market monitoring
- `CHANGELOG.md` — this file

### Fixed
- `fetch_all_ticker_prices()` correctly handled as `dict` (not list) keyed by symbol
- `MarketContext` fields confirmed: `mark_price`, `funding_rate`, `open_interest`
- Signal candidate scan now uses `strategy.evaluate()` output with correct field access

### Notes (Live Market Observation — 2026-03-20 09:40 UTC)
- BTC: $70,655 | ETH: $2,144 | SOL: $88.97 | BNB: $642 | XRP: $1.449
- No signals generated — market in consolidation (RSI 40–50 across all pairs)
- All funding rates below ±0.01% — healthy, non-over-leveraged market
- Primary blocker: no EMA(21)/EMA(55) crossover in last 12 bars on any symbol

---

## [1.2.0] — 2026-03-19

### Added
- Comprehensive documentation suite:
  - `docs/API_REFERENCE.md` — all REST endpoints with request/response schemas
  - `docs/ARCHITECTURE.md` — system design, data flow, module dependencies, data models
  - `docs/CONFIGURATION.md` — every config.json parameter documented with types and defaults
  - `docs/HANDBOOK.md` — developer handbook (setup, strategy deep-dive, trade lifecycle, deployment)
  - `CONTRIBUTING.md` — contributor guidelines and PR process
  - `AGENTS.md` — AI agent instructions and conventions
- `.gitignore` — Python artifacts, data files, secrets excluded

### Changed
- README.md restructured with architecture diagram, tech stack table, dashboard section guide
- `config.json` live_loop defaults tuned: `max_cycles=50`, `poll_seconds=12`, `target_trades=3`

---

## [1.1.0] — 2026-03-15

### Added
- **Analytics Engine** — equity curve, drawdown, rolling win rate, PnL distribution
- **Guard Monitor** dashboard section — per-symbol health and adaptive retuning events
- **News Feed** — RSS-aggregated crypto market headlines cached for 5 minutes
- **Symbol Catalog** — searchable Binance USDT perpetual symbol list via CoinGecko
- **MongoDB persistence** (optional) — closed trade storage and retrieval
- `POST /api/config/symbols` — runtime watchlist updates without restart
- `POST /api/config/symbol` — single symbol override
- CI/CD GitHub Actions:
  - `ci.yml` — pytest, flake8, config validation
  - `code-quality.yml` — static analysis
  - `e2e.yml` — end-to-end smoke test
  - `pr-checks.yml` — PR validation with semantic PR title enforcement

### Changed
- Dashboard restyled with dark theme, glassmorphism cards
- Section tabs: Overview, Analytics, Opportunities, Market, Activity, History, News, Guard
- `app.js` polling now adaptive: 2s active trade, 10s idle
- Performance Guard now emits `GUARD_EVENT` JSON to event stream

### Fixed
- Trade engine: `maybe_open_trade()` correctly handles duplicate open prevention
- Binance REST client: retry logic with 3 attempts and exponential backoff
- Funding rate check now uses `abs(funding_rate) <= funding_abs_limit`

---

## [1.0.0] — 2026-03-10

### Added
- **Core signal engine** — EMA crossover, pullback, and momentum entry modes
- **Adaptive paper trader** — live Binance Futures data, no real orders
- **Risk management** — trailing stop, break-even stop, momentum reversal exit, stagnation exit
- **Loss Guard** — global and per-symbol consecutive loss streak detection with pause cycles
- **Performance Guard** — per-symbol win rate and expectancy monitoring with cooldown
- **Filter relaxation** — prevents system lockout in low-volatility markets
- **ML walk-forward optimizer** — logistic classifier with cost model integration
- **Dashboard server** (`frontend/server.py`) — HTTP API on port 8787
- **Live dashboard** (`frontend/index.html`, `app.js`, `styles.css`)
- **EC2 deployment script** (`deploy_ec2.sh`) with systemd service
- **Test suite** — 33 unit tests covering strategy, indicators, trade engine, config, ML

### Strategy Parameters (v1.0 defaults)
- EMA: fast=21, slow=55 | RSI period: 14 | ATR multiplier: 1.5
- Crossover lookback: 12 bars | Min confidence: 0.60
- Long RSI: 45–72 | Short RSI: 18–50

### ML Results (initial walk-forward)
- Selected trades: 66 | Wins: 40 | Losses: 26
- Win rate: 60.6% | Expectancy-R: 0.38

---

[Unreleased]: https://github.com/rishat5081/crypto/compare/main...HEAD
[1.3.0]: https://github.com/rishat5081/crypto/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/rishat5081/crypto/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/rishat5081/crypto/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/rishat5081/crypto/releases/tag/v1.0.0
