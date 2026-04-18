# 15m Trade Review Task

## Objective

Audit every real `15m` trade from MongoDB against Binance historical candles and the current strategy rules, then separate:

- trades that were valid under the rules at entry time
- trades that should not have been taken
- trades that were valid but still show a weak edge

This worksheet is the chart-review pack for the current round.

## Evidence Files

- Full audit JSON: `/tmp/crypto-runtime/15m_trade_audit.json`
- Full audit Markdown: `/tmp/crypto-runtime/15m_trade_audit.md`
- Recent combined audit: `/tmp/crypto-runtime/15m_trade_audit_recent.md`
- Recent long-only audit: `/tmp/crypto-runtime/15m_trade_audit_recent_long.md`
- Recent short-only audit: `/tmp/crypto-runtime/15m_trade_audit_recent_short.md`

## Current Status

- Full real `15m` trades audited: `56`
- Recent current-relevant `15m` trades from `2026-04-17` onward: `10`
- Recent verdict split:
  - `9` matched the current rules
  - `1` did not match the current rules
- Post-fix `15m` trades since `2026-04-17 15:02:36 UTC`: `9`
- Post-fix verdict split:
  - `9` matched the current rules
  - `0` mismatched

Important: the audit fetch was corrected on `2026-04-18` to use the exact Binance candle ending at the trade open time. Older conclusions from the earlier broken audit should be ignored.

## Main Findings So Far

1. The only clearly incorrect recent long under the current rules is:
   - `AAVEUSDT LONG 15m` at `2026-04-17 13:29:59.999 UTC`
   - Current replay rejects it.
   - Concrete issue: `pullback_confirmation_failed` and `no_recent_crossover`
   - Candle shape at entry was weak: the entry candle closed near the low after a large up candle.
   - Chart review confirmation: this was a resistance rejection and should not have been a long; it was a short candidate instead.

2. The recent short side is the cleanest current loophole.
   - Post-fix `15m SHORT` trades are all `trend_bias=NEUTRAL`
   - Post-fix result for that bucket: `6` trades, `2` wins, `4` losses, `-0.6863R` total, `-0.1144R` average
   - They are not being blocked by rules; they are being explicitly allowed and often rated too highly
   - Example live candidate ratings observed in the event stream:
     - `AVAXUSDT SHORT`: confidence about `0.8901`, score about `0.7986`, win_probability about `0.6697`
     - `ETHUSDT SHORT`: confidence about `0.9303`, score about `0.7959`, win_probability about `0.6692`

3. The recent long side is weaker than it should be, but it is not primarily a rules-mismatch problem.
   - Recent `15m LONG` trades: `6`
   - `5` matched the current rules, `1` did not
   - The matched long bucket is only slightly positive overall, which points more to weak edge / threshold quality than an obvious implementation bug

4. The current live problem after the fix is not “the bot is breaking rules.”
   - Post-fix `15m` trades are all `RULES_MATCHED`
   - The current issue is: some allowed setups have weak edge
   - The clearest example is `SHORT pullback` with `trend_bias=NEUTRAL`

## Current Live Focus

Separate pre-fix and post-fix when reviewing charts.

- Pre-fix review target:
  - `AAVEUSDT LONG` is still the clearest likely bad entry
- Post-fix review target:
  - `15m SHORT + trend_bias=NEUTRAL` is the clearest weak branch

Post-fix bucket performance:

- `LONG + BULL`: `3` trades, `1` win, `2` losses, `-0.3035R` total, `-0.1012R` average
- `SHORT + NEUTRAL`: `6` trades, `2` wins, `4` losses, `-0.6863R` total, `-0.1144R` average

Interpretation:

- the long side still needs review, but it is mixed quality
- the short side has a more coherent repeatable problem
- the ranking layer appears overconfident on those neutral-trend shorts

## Chart Review Queue

Use Binance charts on `15m` and inspect these entries in order.

### Longs

1. `ADAUSDT LONG`
   - Opened: `2026-04-17 12:14:59.999 UTC`
   - Result: `LOSS`
   - Verdict: `RULES_MATCHED`
   - Basis: `trend_bias=BULL`, `regime=TRENDING`, `RSI=60.25`, `ADX=26.91`, near structure, reward room true, pullback confirmed
   - Chart validation: long idea was good, but stop loss was too tight and got swept
   - User-confirmed better structural stop area: around `0.2555`
   - Confirmed issue: stop placement / execution quality, not direction

2. `LTCUSDT LONG`
   - Opened: `2026-04-17 12:59:59.999 UTC`
   - Result: `WIN`
   - Verdict: `RULES_MATCHED`
   - Basis: strong pullback confirmation, support nearby, clean bullish close
   - Chart validation: clean valid long
   - Stop placement acceptable
   - Take profit appears conservative / too close relative to the continuation strength
   - Use as the control example for a clean long with possibly under-extended TP

3. `AAVEUSDT LONG`
   - Opened: `2026-04-17 13:29:59.999 UTC`
   - Result: `LOSS`
   - Verdict: `RULES_MISMATCH`
   - Basis problem: current rules say no confirmed pullback and no recent crossover
   - Entry candle: closed near the low after a strong prior push
   - Chart validation: confirmed bad long; this candle rejected resistance and was a short candidate, not a long

4. `LTCUSDT LONG`
   - Opened: `2026-04-17 16:14:59.999 UTC`
   - Result: `WIN`
   - Verdict: `RULES_MATCHED`
   - Basis: confirmed pullback, trend aligned, support nearby
   - Chart validation: valid long
   - Same issue pattern as Trade 3: take profit too close / conservative
   - Use as the second control example for a valid long with under-extended TP

5. `XRPUSDT LONG`
   - Opened: `2026-04-17 17:59:59.999 UTC`
   - Result: `LOSS`
   - Verdict: `RULES_MATCHED`
   - Basis: trend aligned and structure-supported, but the move failed quickly
   - Chart validation: long idea was good, but stop loss was too close to entry and got taken out
   - Same issue pattern as Trade 2: stop placement / execution quality, not direction

6. `BNBUSDT LONG`
   - Opened: `2026-04-17 19:44:59.999 UTC`
   - Result: `LOSS`
   - Verdict: `RULES_MATCHED`
   - Basis: valid by rules, support nearby, pullback confirmed
   - Chart validation: long was the wrong side; price rejected from resistance in a bearish context
   - Stop loss was also too close
   - Confirmed issue: wrong-side long plus tight stop placement

### Shorts

1. `XRPUSDT SHORT`
   - Opened: `2026-04-18 04:14:59.999 UTC`
   - Result: `WIN`
   - Verdict: `RULES_MATCHED`
   - Basis: current rules allowed it, but `trend_bias=NEUTRAL`
   - Use as the control example for a neutral-trend short that still worked

2. `AVAXUSDT SHORT`
   - Opened: `2026-04-18 04:44:59.999 UTC`
   - Result: `LOSS`
   - Verdict: `RULES_MATCHED`
   - Basis: `trend_bias=NEUTRAL`, pullback confirmed, structure nearby
   - Chart validation: unsupported short
   - Market was ranging and there was no proper bearish signal or clean rejection for a short entry
   - If treated as a very short-term trade, the candle context leaned more toward a possible long than a short
   - Confirmed issue: short was allowed without a clearly bearish macro trend or decisive breakdown

3. `LTCUSDT SHORT`
   - Opened: `2026-04-18 04:59:59.999 UTC`
   - Result: `LOSS`
   - Verdict: `RULES_MATCHED`
   - Basis: `trend_bias=NEUTRAL`, pullback confirmed, structure nearby
   - Chart validation: this was a proper long trade, but the system took a short trade
   - Confirmed issue: wrong-side trade in neutral market context
   - Same loophole pattern as Trade 8: short was allowed without a real bearish trigger

4. `ETHUSDT SHORT`
   - Opened: `2026-04-18 05:29:59.999 UTC`
   - Result: `LOSS`
   - Verdict: `RULES_MATCHED`
   - Basis: `trend_bias=NEUTRAL`, pullback confirmed, structure nearby
   - Problem candidate: same loophole again; this strengthens the case that neutral-trend shorts should be blocked or downgraded

5. `ETHUSDT SHORT`
   - Opened: `2026-04-18 06:14:59.999 UTC`
   - Result: `WIN`
   - Verdict: `RULES_MATCHED`
   - Basis: `trend_bias=NEUTRAL`, pullback confirmed, structure nearby
   - Use as the second control example showing that neutral-trend shorts are not always wrong, but the bucket is still net negative

6. `SOLUSDT SHORT`
   - Opened: `2026-04-18 06:44:59.999 UTC`
   - Result: `LOSS`
   - Verdict: `RULES_MATCHED`
   - Basis: `trend_bias=NEUTRAL`, pullback confirmed, structure nearby
   - Live candidate rating was still high: confidence about `0.8584`, score about `0.7627`, win_probability about `0.6608`
   - Chart validation: stop loss was too close, same pattern as earlier stop-sweep trades
   - Problem candidate: the bot is still ranking this weak branch as good enough to trade while using tight stop placement

## Working Hypotheses To Validate On Chart

1. `15m SHORT` trades in `trend_bias=NEUTRAL` are too permissive.
2. The live ranking layer is overstating the quality of neutral-trend shorts.
3. Some `15m LONG` losses are valid by rule but still not high enough quality; the likely issue is entry quality, not trade management.
4. `AAVEUSDT LONG` is the clearest recent example of an actually incorrect long entry.

## Next Actions After Chart Review

1. Confirm or reject the `AAVEUSDT LONG` mismatch as a true bad long.
2. Confirm whether the `AVAXUSDT`, `LTCUSDT`, and `ETHUSDT` shorts should have been blocked because the market was only neutral, not bearish.
3. Add `SOLUSDT SHORT` and the later `ETHUSDT SHORT` win into the review so the neutral-short bucket is judged as a group, not from one loss only.
4. If chart review agrees, tighten `15m SHORT` logic first by requiring bearish macro trend for pullback shorts.
5. Revisit `15m LONG` thresholds only after the clearly incorrect entries are removed.
