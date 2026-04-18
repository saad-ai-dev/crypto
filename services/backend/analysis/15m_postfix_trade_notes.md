# 15m Post-Fix Trade Notes

These notes cover only trades opened after the live closed-candle fix on `2026-04-17 15:02:36 UTC`.

## Bucket Summary

- `LONG + BULL`: `3` trades, `1` win, `2` losses, `-0.3035R`
- `SHORT + NEUTRAL`: `6` trades, `2` wins, `4` losses, `-0.6863R`

## Trade Notes

### LTCUSDT LONG

- Opened: `2026-04-17 16:14:59.999 UTC`
- Result: `WIN` `+1.5000R`
- Basis: `trend_bias=BULL`, `RSI=51.91`, `ADX=35.59`, pullback confirmed
- Entry candle body: `+0.18%`
- First next-bar return: `+0.3532%`
- Read: clean continuation immediately after entry

### XRPUSDT LONG

- Opened: `2026-04-17 17:59:59.999 UTC`
- Result: `LOSS` `-0.8629R`
- Basis: `trend_bias=BULL`, `RSI=54.25`, `ADX=35.96`, pullback confirmed
- Entry candle body: `-0.01%`
- Entry close position in candle range: `0.2698`
- First next-bar return: `-0.5933%`
- Read: long was valid by rules, but the entry candle was weak and the move failed immediately

### BNBUSDT LONG

- Opened: `2026-04-17 19:44:59.999 UTC`
- Result: `LOSS` `-0.9406R`
- Basis: `trend_bias=BULL`, `RSI=54.67`, `ADX=27.54`, pullback confirmed, bounce flag true
- Entry candle body: `+0.12%`
- First next-bar return: `-0.0312%`
- Read: valid by rules, but no real continuation strength after entry

### XRPUSDT SHORT

- Opened: `2026-04-18 04:14:59.999 UTC`
- Result: `WIN` `+0.4836R`
- Basis: `trend_bias=NEUTRAL`, `RSI=39.02`, `ADX=25.69`, pullback confirmed
- Entry candle body: `-0.05%`
- First next-bar return: `+0.1772%`
- Read: neutral-trend short that worked, use as a control example

### AVAXUSDT SHORT

- Opened: `2026-04-18 04:44:59.999 UTC`
- Result: `LOSS` `-0.3065R`
- Basis: `trend_bias=NEUTRAL`, `RSI=38.09`, `ADX=29.05`, pullback confirmed
- Entry candle body: `-0.05%`
- First next-bar return: `-0.1037%`
- Read: short was allowed without bearish macro trend and moved wrong almost immediately

### LTCUSDT SHORT

- Opened: `2026-04-18 04:59:59.999 UTC`
- Result: `LOSS` `-1.0000R`
- Basis: `trend_bias=NEUTRAL`, `RSI=40.04`, `ADX=25.67`, pullback confirmed
- Entry candle body: `+0.11%`
- First next-bar return: `-0.2493%`
- Read: short entry was especially weak because the entry candle itself closed green

### ETHUSDT SHORT

- Opened: `2026-04-18 05:29:59.999 UTC`
- Result: `LOSS` `-0.1937R`
- Basis: `trend_bias=NEUTRAL`, `RSI=42.60`, `ADX=33.06`, pullback confirmed
- Entry candle body: `-0.12%`
- First next-bar return: `+0.0602%`
- Read: slight favorable move first, but not enough follow-through; still a weak neutral-trend short

### ETHUSDT SHORT

- Opened: `2026-04-18 06:14:59.999 UTC`
- Result: `WIN` `+0.4810R`
- Basis: `trend_bias=NEUTRAL`, `RSI=43.61`, `ADX=34.10`, pullback confirmed
- Entry candle body: `+0.01%`
- First next-bar return: `+0.0245%`
- Read: another neutral-trend short, but only mild follow-through; use as the second control example

### SOLUSDT SHORT

- Opened: `2026-04-18 06:44:59.999 UTC`
- Result: `LOSS` `-0.1508R`
- Basis: `trend_bias=NEUTRAL`, `RSI=45.80`, `ADX=33.29`, pullback confirmed
- Live candidate rating at entry:
  - confidence about `0.8584`
  - score about `0.7627`
  - win_probability about `0.6608`
- Entry candle body: `+0.06%`
- Entry close position in candle range: `0.7500`
- First next-bar return: `+0.0339%`
- Read: the live ranker still treated this as a decent short even though the candle itself did not support a strong bearish rejection

## Working Read

1. Post-fix long losses are mostly “valid but weak continuation” trades.
2. Post-fix short losses are a more coherent bucket: they are repeatedly allowed in `trend_bias=NEUTRAL`.
3. The bot is not only allowing those neutral-trend shorts; it is often rating them as mid-to-high quality.
