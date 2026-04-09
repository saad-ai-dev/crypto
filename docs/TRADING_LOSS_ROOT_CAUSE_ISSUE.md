# GitHub Issue Draft: Trading Engine Produces Too Many Bad Losses Due to Weak Entry Selection, Aggressive Adverse Cuts, and Exit Logic Defects

## Title

Trading engine loses too many trades because of weak crossover entries, aggressive `ADVERSE_CUT` exits, a short-side break-even bug, and a noisy symbol universe

## Summary

The current trading engine is not failing for one reason.

Recent trade analysis shows the loss problem is a combination of:

1. weak trade selection on some setups, especially crossover entries,
2. loss concentration in `ADVERSE_CUT` exits,
3. a real short-side break-even stop bug,
4. an over-broad and noisy symbol universe,
5. and secondary exit-tightness that can cut trades before the intended move fully develops.

This means the fix is not "just loosen exits" or "just hold longer".
The system needs a coordinated plan that addresses:

- entry quality,
- symbol quality,
- risk-manager behavior,
- and one concrete execution bug.

## Evidence

### Data quality note

Trade history contains duplicate `TRADE_RESULT` rows in `data/live_events_history.jsonl`.

Observed in analysis:

- `157` trade result rows
- only `97` unique trades
- `60` duplicate extra rows

All conclusions below are based on the deduplicated unique trade set.

### Deduplicated trade results

From the unique sample of `97` trades:

- `74` direct TP/SL-style exits
- `17` `ADVERSE_CUT` exits
- `5` `TIMEOUT_EXIT` exits
- `1` `NETWORK_ERROR_EXIT`

By exit family:

- `DIRECT_EXIT`
  - count: `74`
  - win rate: `86.5%`
  - avg result: `+0.4177R`
- `ADVERSE_CUT`
  - count: `17`
  - win rate: `11.8%`
  - avg result: `-0.4012R`
- `TIMEOUT_EXIT`
  - count: `5`
  - win rate: `80%`
  - avg result: `+0.4334R`

Implication:

- timeout is not the primary loss source,
- `ADVERSE_CUT` is a major negative expectancy cluster,
- and the system is also taking a non-trivial number of outright bad direct losses.

### Signal-type performance

By signal type on unique trades:

- `crossover`
  - count: `57`
  - win rate: `71.9%`
  - avg result: `+0.2115R`
- `pullback`
  - count: `38`
  - win rate: `76.3%`
  - avg result: `+0.3904R`
- `momentum`
  - count: `1`
  - avg result: negative

Implication:

- pullback setups are materially stronger than crossover setups,
- crossover entries are lower quality and likely overrepresented in the loss set.

### Loss concentration by cluster

Largest negative clusters in the deduplicated trade set:

- `crossover + DIRECT_EXIT`
  - `6` losses
  - net `-1.2 USD`
- `crossover + ADVERSE_CUT`
  - `9` losses
  - net `-1.0144 USD`
- `pullback + DIRECT_EXIT`
  - `4` losses
  - net `-0.8 USD`
- `pullback + ADVERSE_CUT`
  - `5` losses
  - net `-0.4232 USD`

Implication:

- the largest loss source is weak crossover entries,
- the second largest source is adverse-cut handling on crossover trades,
- pullbacks are also affected, but less severely.

### Bad-loss timing

Fast loss sample:

- `37` losses closed in `15` minutes or less
- average result: `-0.6761R`

Examples:

- `CRVUSDT` `ADVERSE_CUT` `-0.4827R`
- `GMTUSDT` `ADVERSE_CUT` `-0.5759R`
- `FILUSDT` `ADVERSE_CUT` `-0.8037R`
- `SUSHIUSDT` `ADVERSE_CUT` `-0.9735R`
- `KITEUSDT` direct loss `-1.0R`
- `DEXEUSDT` direct loss `-1.0R`

Implication:

- many losses are happening very quickly after entry,
- which points to entry-quality problems and over-aggressive adverse-cut behavior,
- not just late-exit or long-hold issues.

### Current exit config is still very tight

From [config.json](/home/saad/Desktop/WORK/crypto/config.json#L167):

- `max_wait_candles = 3`
- `break_even_trigger_r = 0.3`
- `trail_trigger_r = 0.2`
- `max_adverse_r_cut = 0.7`
- `max_stagnation_bars = 3`
- `risk_reward = 1.0`

Implication:

- trades are managed aggressively very early,
- but the historical data shows this is not the only problem,
- because timeout exits are not the main negative cluster.

## Confirmed Defect

### Short-side break-even stop is wrong

In:

- [src/live_adaptive_trader.py](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L832)
- [src/live_adaptive_trader.py](/home/saad/Desktop/WORK/crypto/src/live_adaptive_trader.py#L1295)

The short-side break-even branch sets:

```python
be_stop = active.entry + (self.break_even_offset_r * risk)
```

For a short trade, this is above entry, which is not true break-even protection.
This can convert a favorable short into a small locked loss instead of a protected trade.

This defect must be fixed before further strategy evaluation.

## Root Causes

### 1. Weak crossover entries are contributing too many losses

Crossover entries have:

- lower win rate than pullbacks,
- lower average expectancy than pullbacks,
- the largest direct-loss cluster,
- and the largest adverse-cut loss cluster.

Conclusion:

- crossover logic is too permissive for the current symbol set and market regime.

### 2. `ADVERSE_CUT` is a major negative-expectancy mechanism

`ADVERSE_CUT` trades are:

- frequent enough to matter,
- mostly losing,
- and often closed within one to three 5m candles.

This suggests one or both of:

- the threshold is too aggressive,
- the underlying entries are too fragile and should never have been opened.

This means `ADVERSE_CUT` should be treated as a diagnostic signal of bad entries, not just a safety feature.

### 3. The engine is taking trades on too many noisy symbols

The configured live watchlist includes many lower-quality, highly unstable, or marginal symbols.

Loss examples come from names such as:

- `WIFUSDT`
- `NEARUSDT`
- `ZROUSDT`
- `SUSHIUSDT`
- `FILUSDT`
- `GMTUSDT`
- `TAOUSDT`
- `ENAUSDT`
- `MSTRUSDT`

Conclusion:

- symbol quality is not being enforced strongly enough,
- and the watchlist is too broad for a strategy with small RR and tight management.

### 4. Exit management is too tight, but that is a secondary amplifier

The current risk manager:

- trails very early,
- moves toward break-even early,
- times out quickly,
- and uses a relatively tight adverse-cut threshold.

This likely amplifies the problem by clipping trades too early and compressing winner size.

However, the deduplicated history shows:

- `TIMEOUT_EXIT` is mostly profitable,
- so the bigger issue is still bad entries and adverse-cut concentration.

### 5. Duplicate trade-history rows are degrading analytics quality

The runtime history contains duplicated `TRADE_RESULT` entries.

That makes performance analysis less trustworthy and can distort:

- symbol ranking,
- exit-type ranking,
- and dashboard summaries.

This needs to be cleaned up so further tuning is based on correct data.

## Plan

### Phase 1: Fix correctness defects first

1. Fix the short-side break-even calculation.
2. Add regression tests for:
   - long break-even,
   - short break-even,
   - trailing-stop interactions,
   - adverse-cut interactions.
3. Fix duplicate trade-history ingestion or emission so one trade produces one final result.

Acceptance criteria:

- short break-even uses the correct direction,
- no duplicate trade records appear in deduplicated test fixtures,
- full test suite passes.

### Phase 2: Add hard loss diagnostics

1. Add analytics grouped by:
   - symbol,
   - signal type,
   - exit type,
   - hold duration,
   - net PnL after cost.
2. Add dashboard tables for:
   - worst symbols,
   - worst exit types,
   - worst signal-type and exit-type combinations.
3. Record whether the stop was:
   - original,
   - break-even adjusted,
   - trailing adjusted,
   at the time of exit.

Acceptance criteria:

- a losing day can be decomposed into exact loss buckets without manual log parsing.

### Phase 3: Reduce bad entries before loosening exits

1. Penalize or disable weak crossover setups.
2. Prefer pullback setups over crossover setups in ranking.
3. Add symbol-quality filtering using:
   - recent win rate,
   - recent expectancy,
   - liquidity / valid-market checks.
4. Shrink the live watchlist to liquid, clean symbols first.

Suggested first reduced universe:

- `BTCUSDT`
- `ETHUSDT`
- `SOLUSDT`
- `BNBUSDT`
- `XRPUSDT`
- `ADAUSDT`
- `DOGEUSDT`
- `LINKUSDT`
- `AVAXUSDT`
- `DOTUSDT`
- `AAVEUSDT`
- `LTCUSDT`

Acceptance criteria:

- lower frequency of fast `ADVERSE_CUT` losses,
- better average expectancy per trade,
- fewer full `-1R` direct losses on noisy symbols.

### Phase 4: Retune risk management after entry quality improves

Only after Phases 1 to 3:

1. Relax early management slightly:
   - raise `trail_trigger_r`
   - raise `break_even_trigger_r`
   - review `max_wait_candles`
2. Re-test `max_adverse_r_cut` using real history.
3. Re-evaluate `risk_reward = 1.0`; test larger targets for stronger symbols.

Acceptance criteria:

- winner size improves without increasing bad-loss frequency materially,
- `ADVERSE_CUT` count and cost decline,
- net USDT PnL improves, not just gross win rate.

### Phase 5: Validate on recent live-style data

1. Run on a narrowed liquid watchlist.
2. Compare before vs after:
   - total trades,
   - win rate,
   - expectancy R,
   - net USDT PnL,
   - adverse-cut frequency,
   - average hold duration,
   - per-signal-type performance.

Acceptance criteria:

- crossover losses are reduced or crossover share is reduced,
- `ADVERSE_CUT` is no longer a dominant loss bucket,
- the strategy grows the quote asset the user actually cares about.

## Definition of Done

This issue is done when:

1. short break-even logic is corrected and tested,
2. duplicate trade-history rows are eliminated,
3. worst-symbol and worst-exit diagnostics are visible in the dashboard,
4. the live watchlist is reduced to a quality-controlled universe,
5. crossover entries are either improved, deprioritized, or disabled where weak,
6. `ADVERSE_CUT` no longer represents a dominant negative-expectancy cluster,
7. and the system improves **net USDT balance**, not just headline trade count or gross wallet value.

## Proposed Labels

- `bug`
- `trading-logic`
- `risk-management`
- `analytics`
- `priority:high`

