# Task: Fix 15m Strategy Issues Found In Last-24h Chart Review

## Scope

This task covers only the real `15m` trades reviewed from the last 24-hour window ending at the latest reviewed trade on `2026-04-18 06:44:59.999 UTC`.

- Window start: `2026-04-17 06:44:59.999 UTC`
- Window end: `2026-04-18 06:44:59.999 UTC`
- Real `15m` trades reviewed: `12 / 12`

Source review notes:

- `services/backend/analysis/15m_trade_review_task.md`
- `services/backend/analysis/15m_postfix_trade_notes.md`

## Chart-Confirmed Findings

### 1. Wrong-side trades were taken

Confirmed wrong-side or unsupported entries:

- `AAVEUSDT LONG`
  - resistance rejection
  - should not have been a long
  - chart bias favored short
- `BNBUSDT LONG`
  - rejection from resistance in bearish context
  - long was the wrong side
- `AVAXUSDT SHORT`
  - ranging / neutral context
  - no proper bearish signal for short
- `LTCUSDT SHORT`
  - chart looked like a proper long trade
  - system took a short instead

### 2. Stop loss was too close on valid trade ideas

Confirmed good-direction trades with bad stop placement:

- `ADAUSDT LONG`
- `XRPUSDT LONG`
- `SOLUSDT SHORT`
- `BNBUSDT LONG` also had tight stop in addition to wrong-side bias

Observed pattern:

- stop is often too close to the entry candle / local noise
- market sweeps the stop and then continues in the expected direction

### 3. Take profit was too conservative

Confirmed valid-direction trades with TP too close:

- `LTCUSDT LONG` at `2026-04-17 12:59:59.999 UTC`
- `LTCUSDT LONG` at `2026-04-17 16:14:59.999 UTC`
- `XRPUSDT SHORT` at `2026-04-18 04:14:59.999 UTC`
- `ETHUSDT SHORT` at `2026-04-18 06:14:59.999 UTC`

Observed pattern:

- the system exits profitable trades too early
- continuation strength is not being captured

### 4. Neutral-trend shorts are the clearest live loophole

Post-fix `15m SHORT` trades were repeatedly allowed in `trend_bias=NEUTRAL`.

Chart review and replay both support this as the clearest repeatable problem branch.

### 5. Timeout / cost handling needs review

Example:

- `ETHUSDT SHORT` at `2026-04-18 05:29:59.999 UTC`

Raw price moved slightly in favor of the short, but not enough to beat configured fees/slippage, so timeout closed it as a stored `LOSS`.

This may be technically correct in net terms, but it is hard to interpret operationally and should be reviewed.

## Required Fixes

- Tighten `15m SHORT` pullback logic.
  - block or heavily penalize pullback shorts when `trend_bias=NEUTRAL`
  - require explicit bearish rejection or breakdown confirmation before allowing short entries in neutral context

- Tighten wrong-side long logic near resistance.
  - reject longs when the setup candle shows clear resistance rejection / weak close
  - add bearish rejection-candle filtering around resistance

- Rework stop-loss placement.
  - anchor SL to stronger structural levels / liquidity sweep zones
  - avoid placing SL too close to the entry candle body or local noise

- Rework take-profit logic.
  - allow more room on strong continuation trades
  - consider structure-based TP extension or stronger trailing logic

- Review timeout result handling.
  - keep net-PnL accounting correct
  - but make timeout exits easier to interpret when raw price direction was favorable but net result is negative after costs

## Acceptance Criteria

- No `15m SHORT` should be opened in neutral market context unless a stronger bearish trigger is present.
- Resistance rejection candles should not produce bullish continuation longs.
- SL placement should survive common one-candle stop sweeps on otherwise valid setups.
- TP placement should not repeatedly cap strong continuation trades too early.
- Re-run the same last-24h replay / review set and confirm the previously flagged trades are either:
  - blocked correctly, or
  - managed with better SL/TP placement.
