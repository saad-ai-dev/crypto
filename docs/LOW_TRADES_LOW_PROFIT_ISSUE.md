# GitHub Issue Draft: Scalping Engine Takes Too Few Trades and Produces Too Little Daily Profit

## Title

Scalping engine is under-trading and under-monetized due to strict execution gates, blocking trade lifecycle, and tiny paper risk sizing

## Summary

The current live trading engine is profitable in win-rate terms, but it is not operating like a real scalping system.

Main symptoms:

1. Too few trades are executed.
2. Daily dollar profit stays very low, roughly a few dollars per day.

This is not caused by one bad parameter. It is the combined effect of:

- paper risk sizing that caps dollar PnL,
- a blocking one-trade-at-a-time architecture,
- multiple stacked execution filters that reject many valid candidates,
- exits that protect too early and limit profit expansion,
- and a signal engine that is still closer to filtered trend-following than true scalping.

## Evidence

### Current config is structurally limiting profit

From [config.json](/home/saad/Desktop/WORK/crypto/config.json#L2):

- `starting_balance_usd = 10.0`
- `risk_per_trade_pct = 0.02`

From [src/live_adaptive_trader.py](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L50):

- `risk_usd = starting_balance_usd * risk_per_trade_pct`

That means current paper risk is:

- `risk_usd = 10 * 0.02 = $0.20` per trade

So even if the system averages `0.25R` to `0.30R`, each trade only makes about:

- `0.25R * $0.20 = $0.05`
- `0.30R * $0.20 = $0.06`

Low daily dollar profit is mathematically expected under this setup.

### Historical runtime evidence shows the engine sees setups but often does not execute them

Parsed from `data/live_events_history.jsonl` while skipping malformed lines:

- `79` closed trades
- `59` wins
- `20` losses
- win rate: `74.68%`
- total pnl: `$4.365938`
- average pnl per trade: `$0.055265`
- average expectancy: `0.276325R`
- `376` `EXECUTION_FILTER_BLOCK` cycles
- `72` `NO_CANDIDATES` cycles
- average candidates seen per `POSSIBLE_TRADES` event: `9.2555`
- average possible trades per cycle: `3.3376`
- `322` `MARKET_FETCH_ERROR` events

Implication:

- the engine is seeing candidates,
- the filters are rejecting many of them,
- and the winners that do get filled are still too small in dollar terms because `risk_usd` is tiny.

### The runtime is not clean JSONL today

The event history file contains at least two non-JSON problems:

1. bell/control characters before some JSON lines,
2. pretty-printed multi-line JSON fragments mixed into the stream.

This violates the project invariant that every output line must be valid JSON and makes analytics less trustworthy.

## Root Causes

### 1. Paper sizing is capping PnL before strategy quality even matters

References:

- [config.json](/home/saad/Desktop/WORK/crypto/config.json#L2)
- [src/live_adaptive_trader.py](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L50)

The current profit ceiling is mostly a sizing problem:

- `risk_usd` is fixed at `$0.20`
- average trade expectancy from history is about `0.276R`
- expected profit per trade is therefore about `$0.055`

This is why the engine can look "good" in win rate and still look "bad" in daily profit.

### 2. The engine blocks scanning while a trade is open

References:

- [_wait_for_close in live_adaptive_trader.py](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L605)
- [trade open/monitor flow in live_adaptive_trader.py](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L1165)

Current behavior:

1. one candidate is selected,
2. `_wait_for_close()` is called,
3. the loop stays inside trade monitoring until the trade exits,
4. no new market scan happens during that period.

That is a major mismatch for scalping across many symbols and multiple timeframes. The bot is effectively serializing all opportunity capture.

### 3. Execution filters are stricter than the strategy itself

References:

- [config.json](/home/saad/Desktop/WORK/crypto/config.json#L169)
- [live thresholds in live_adaptive_trader.py](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L66)
- [qualification gate in live_adaptive_trader.py](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L1109)

Base strategy threshold:

- `strategy.min_confidence = 0.65`

Live execution thresholds:

- `min_candidate_confidence = 0.73`
- `min_candidate_expectancy_r = 0.18`
- `execute_min_confidence = 0.75`
- `execute_min_expectancy_r = 0.20`
- `execute_min_score = 0.75`
- `execute_min_win_probability = 0.50`
- plus optional dual-timeframe confirmation support

This creates two layers of selection:

1. strategy says "signal exists",
2. execution layer often says "still not good enough".

The `376` filter-block cycles confirm this is a real bottleneck.

### 4. Exits are tuned to protect quickly, not to let scalps expand

References:

- [config.json](/home/saad/Desktop/WORK/crypto/config.json#L167)
- [exit logic in live_adaptive_trader.py](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L717)
- [TP/SL construction in strategy.py](/home/saad/Desktop/WORK/crypto/src/strategy.py#L219)

Current live settings:

- `risk_reward = 1.0`
- `max_wait_candles = 3`
- `break_even_trigger_r = 0.3`
- `trail_trigger_r = 0.2`
- `trail_keep_pct = 0.85`
- `max_adverse_r_cut = 0.7`
- `max_stagnation_bars = 3`

This combination causes several problems:

- trades are managed aggressively almost immediately,
- winners are often clipped before they can reach meaningful size,
- timeout and stagnation exits can remove trades before normal 5m/15m movement plays out.

For a scalping system, exits should be fast, but not so fast that every trade is forced into a tiny R distribution.

### 5. Strategy generation is still narrow for a scalping use case

References:

- [signal generation in strategy.py](/home/saad/Desktop/WORK/crypto/src/strategy.py#L80)
- [pullback logic in strategy.py](/home/saad/Desktop/WORK/crypto/src/strategy.py#L153)
- [disabled momentum block in strategy.py](/home/saad/Desktop/WORK/crypto/src/strategy.py#L184)

The strategy currently depends on:

- fresh EMA crossover logic,
- EMA(200) macro direction filter,
- pullback confirmation,
- RSI windows,
- ATR window,
- funding filter,
- low-volume rejection,
- reversal-candle rejection.

Momentum entries are fully disabled.

This produces cleaner entries, but it reduces frequency substantially. It is better described as selective trend continuation than broad scalping.

### 6. The rotating klines window can miss timely entries

References:

- [config.json](/home/saad/Desktop/WORK/crypto/config.json#L58)
- [_get_klines_window in live_adaptive_trader.py](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L397)

Current setup:

- about `100` symbols in the watchlist,
- `klines_window_size = 60`

Only a subset is scanned each cycle. For slower swing logic this is acceptable, but for a scalping engine it means some symbols are not evaluated when they are actually moving.

### 7. Watchlist quality and fetch errors are reducing effective opportunity

References:

- [config.json watchlist](/home/saad/Desktop/WORK/crypto/config.json#L58)
- [market fetch error path](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L511)

The current watchlist includes several questionable or likely invalid symbols. Runtime history also shows:

- `322` `MARKET_FETCH_ERROR` events

This means part of the scanning budget is being wasted on symbols that do not produce usable data reliably.

## Proposed Fix Plan

### Phase 1: Separate strategy analysis from tiny paper-account sizing

1. Add `account.paper_risk_usd` as an optional override.
2. Keep `risk_per_trade_pct` as the fallback if override is not set.
3. Report both:
   - `pnl_usd_actual`
   - `pnl_r`
4. Treat paper performance evaluation primarily in `R` and secondarily in dollars.

Expected result:

- daily profit reporting becomes meaningful,
- the strategy is no longer judged through a `$0.20` risk cap.

### Phase 2: Refactor the live loop to support concurrent paper trades

1. Replace blocking `_wait_for_close()` with a non-blocking active trade registry.
2. Add `max_open_trades` config, start with `2` or `3`.
3. Scan every cycle even while trades are open.
4. Move exit monitoring into per-cycle updates.
5. Add portfolio-level caps:
   - max total risk,
   - max symbols per side,
   - max trades per symbol.

Expected result:

- more trades,
- better cross-symbol capture,
- architecture that actually fits scalping.

### Phase 3: Relax execution filters in a measured, testable way

Start with config-only tuning before changing strategy logic:

- `min_candidate_confidence: 0.73 -> 0.68`
- `min_candidate_expectancy_r: 0.18 -> 0.08`
- `execute_min_confidence: 0.75 -> 0.70`
- `execute_min_expectancy_r: 0.20 -> 0.10`
- `execute_min_score: 0.75 -> 0.68`

Also add rejection telemetry:

- rejected by confidence,
- rejected by expectancy,
- rejected by score,
- rejected by win probability,
- rejected by dual-timeframe confirmation.

Expected result:

- fewer `EXECUTION_FILTER_BLOCK` cycles,
- clearer understanding of which gate is actually suppressing trades.

### Phase 4: Re-tune exits for better scalping economics

Run experiments on:

- `max_wait_candles: 3 -> 5 or 6`
- `trail_trigger_r: 0.2 -> 0.35`
- `break_even_trigger_r: 0.3 -> 0.45`
- `max_stagnation_bars: 3 -> 4 or 5`
- `risk_reward: 1.0 -> 1.2 or dynamic by setup strength`

Also log exit distribution by reason and average `best_r` before exit.

Expected result:

- fewer tiny wins,
- fewer premature exits,
- more useful profit expansion on stronger trades.

### Phase 5: Add an actual scalping profile instead of forcing the trend profile to do everything

Create a dedicated scalping mode for `1m/3m/5m` with its own rules:

- breakout continuation,
- fast pullback continuation,
- range compression to expansion,
- failed-break retest,
- high-relative-volume impulse entries.

Keep the current strategy as the "selective trend" profile and do not overload it.

Expected result:

- more frequent entries,
- cleaner separation between swing-style filters and scalping filters.

### Phase 6: Clean the symbol universe

1. Validate the configured watchlist against Binance Futures exchange info.
2. Drop invalid, illiquid, or repeatedly failing symbols automatically.
3. Track per-symbol fetch failure rates.
4. Prefer a smaller high-liquidity list for scalping.

Expected result:

- fewer fetch errors,
- more useful scan budget,
- better fill quality and better signal reliability.

### Phase 7: Fix JSONL output integrity

1. Remove control characters from stdout output.
2. Ensure all auxiliary output is also single-line JSON.
3. Add a runtime validator or test for JSONL integrity.

Expected result:

- dashboard analytics become reliable,
- historical analysis becomes easier to automate.

## Recommended Implementation Order

1. Add `paper_risk_usd` override and keep reporting in `R`.
2. Add per-filter rejection telemetry.
3. Clean the watchlist automatically.
4. Relax config thresholds and re-run live validation.
5. Re-tune exit timings and targets.
6. Refactor to concurrent trade monitoring.
7. Build a dedicated low-timeframe scalping profile.

## Acceptance Criteria

1. Trade count increases materially without collapsing expectancy.
2. `EXECUTION_FILTER_BLOCK` share drops meaningfully.
3. `MARKET_FETCH_ERROR` count drops after symbol cleanup.
4. JSONL logs are fully parseable line by line.
5. Runtime can scan while existing trades remain open.
6. Daily PnL in dollars reflects chosen paper risk sizing, not a hardcoded `$0.20` risk cap.

## Notes

- This issue is primarily about architecture and configuration, not a single bug.
- The current engine is not obviously failing at signal quality. It is mostly too conservative, too serialized, and too small in position sizing to behave like a productive scalping bot.
