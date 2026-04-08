from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .indicators import atr, ema, ema_series, rsi
from .models import Candle, MarketContext, Signal


@dataclass
class StrategyParameters:
    ema_fast: int
    ema_slow: int
    rsi_period: int
    atr_period: int
    atr_multiplier: float
    risk_reward: float
    min_atr_pct: float
    max_atr_pct: float
    funding_abs_limit: float
    min_confidence: float
    long_rsi_min: float
    long_rsi_max: float
    short_rsi_min: float
    short_rsi_max: float
    crossover_lookback: int = 5
    ema_trend: int = 0  # 0 = disabled; set to e.g. 200 to only trade with macro trend


class StrategyEngine:
    def __init__(self, params: StrategyParameters):
        self.params = params

    @classmethod
    def from_dict(cls, payload: Dict) -> "StrategyEngine":
        params = StrategyParameters(
            ema_fast=int(payload["ema_fast"]),
            ema_slow=int(payload["ema_slow"]),
            rsi_period=int(payload["rsi_period"]),
            atr_period=int(payload["atr_period"]),
            atr_multiplier=float(payload["atr_multiplier"]),
            risk_reward=float(payload["risk_reward"]),
            min_atr_pct=float(payload["min_atr_pct"]),
            max_atr_pct=float(payload["max_atr_pct"]),
            funding_abs_limit=float(payload["funding_abs_limit"]),
            min_confidence=float(payload["min_confidence"]),
            long_rsi_min=float(payload["long_rsi_min"]),
            long_rsi_max=float(payload["long_rsi_max"]),
            short_rsi_min=float(payload["short_rsi_min"]),
            short_rsi_max=float(payload["short_rsi_max"]),
            crossover_lookback=int(payload.get("crossover_lookback", 5)),
            ema_trend=int(payload.get("ema_trend", 0)),
        )
        return cls(params)

    def evaluate(
        self,
        symbol: str,
        timeframe: str,
        candles: List[Candle],
        market: MarketContext,
    ) -> Optional[Signal]:
        needed = max(self.params.ema_slow, self.params.rsi_period + 1, self.params.atr_period + 1)
        if len(candles) < needed:
            return None

        close_prices = [c.close for c in candles]
        last = candles[-1]
        entry = last.close

        ema_fast_v = ema(close_prices, self.params.ema_fast)
        ema_slow_v = ema(close_prices, self.params.ema_slow)
        rsi_v = rsi(close_prices, self.params.rsi_period)
        atr_v = atr(candles, self.params.atr_period)

        atr_pct = atr_v / entry if entry else 0.0
        if atr_pct < self.params.min_atr_pct or atr_pct > self.params.max_atr_pct:
            return None

        # Crossover recency: require the EMA crossover happened within the
        # last ``crossover_lookback`` bars to avoid mid-trend stale entries.
        crossover_lookback = self.params.crossover_lookback
        fast_series = ema_series(close_prices, self.params.ema_fast)
        slow_series = ema_series(close_prices, self.params.ema_slow)

        # Align: ema_series for period P returns len(close)-P+1 values.
        # We need the last ``crossover_lookback + 1`` aligned pairs.
        look = min(crossover_lookback + 1, len(fast_series), len(slow_series))
        recent_diffs = []
        for k in range(look):
            fi = len(fast_series) - look + k
            si = len(slow_series) - look + k
            recent_diffs.append(fast_series[fi] - slow_series[si])

        # Detect if a crossover happened recently
        bullish_cross = any(recent_diffs[j] <= 0 and recent_diffs[j + 1] > 0
                           for j in range(len(recent_diffs) - 1))
        bearish_cross = any(recent_diffs[j] >= 0 and recent_diffs[j + 1] < 0
                           for j in range(len(recent_diffs) - 1))

        # Momentum confirmation: find how many bars ago the crossover happened
        # and reject if price already moved >0.5 ATR from the crossover bar.
        def _crossover_age_and_drift(diffs, bullish: bool) -> tuple:
            """Return (bars_ago, price_drift_atr) for the most recent crossover."""
            for j in range(len(diffs) - 1, 0, -1):
                if bullish and diffs[j - 1] <= 0 and diffs[j] > 0:
                    return (len(diffs) - 1 - j, abs(entry - close_prices[-(len(diffs) - j)]) / atr_v if atr_v else 0.0)
                if not bullish and diffs[j - 1] >= 0 and diffs[j] < 0:
                    return (len(diffs) - 1 - j, abs(entry - close_prices[-(len(diffs) - j)]) / atr_v if atr_v else 0.0)
            return (len(diffs), 0.0)

        if bullish_cross:
            bull_age, bull_drift = _crossover_age_and_drift(recent_diffs, True)
            if bull_age >= 3 and bull_drift > 0.5:
                bullish_cross = False

        if bearish_cross:
            bear_age, bear_drift = _crossover_age_and_drift(recent_diffs, False)
            if bear_age >= 3 and bear_drift > 0.5:
                bearish_cross = False

        # Macro trend filter: restrict direction to align with EMA(ema_trend).
        # Prevents taking SHORT trades in a long-term bull market and vice-versa.
        allow_long = True
        allow_short = True
        if self.params.ema_trend > 0 and len(close_prices) >= self.params.ema_trend:
            ema_trend_v = ema(close_prices, self.params.ema_trend)
            allow_long = entry >= ema_trend_v
            allow_short = entry <= ema_trend_v

        side: Optional[str] = None
        signal_type = "CROSSOVER"

        if (
            allow_long
            and ema_fast_v > ema_slow_v
            and bullish_cross
            and self.params.long_rsi_min <= rsi_v <= self.params.long_rsi_max
            and entry >= ema_fast_v
            and abs(market.funding_rate) <= self.params.funding_abs_limit
        ):
            side = "LONG"
        elif (
            allow_short
            and ema_fast_v < ema_slow_v
            and bearish_cross
            and self.params.short_rsi_min <= rsi_v <= self.params.short_rsi_max
            and entry <= ema_fast_v
            and abs(market.funding_rate) <= self.params.funding_abs_limit
        ):
            side = "SHORT"

        # Trend continuation / pullback entry: when EMAs are clearly separated
        # (strong trend) and price pulls back to touch the fast EMA, enter in
        # the trend direction. This catches moves that crossover mode misses in
        # sustained trends.
        # Requires 2-candle confirmation: current candle must confirm the bounce/rejection
        # (not just touch the EMA). This prevents whipsaw entries.
        if not side and abs(market.funding_rate) <= self.params.funding_abs_limit:
            trend_strength = abs(ema_fast_v - ema_slow_v) / entry if entry else 0.0
            # Require established trend: EMA gap > 0.3%
            if trend_strength > 0.003 and len(candles) >= 3:
                prev_close = candles[-2].close if len(candles) >= 2 else entry
                prev2_close = candles[-3].close if len(candles) >= 3 else prev_close
                if allow_long and ema_fast_v > ema_slow_v:
                    # Bullish trend: price pulled back near fast EMA and bounced
                    touch_dist = abs(candles[-1].low - ema_fast_v) / atr_v if atr_v else 999
                    bounced = entry > ema_fast_v and prev_close <= ema_fast_v * 1.002
                    # 2-candle confirmation: previous candle must have come from below/near EMA
                    confirmed = prev_close > prev2_close and entry > prev_close
                    if (touch_dist < 1.0 or bounced) and confirmed and self.params.long_rsi_min <= rsi_v <= self.params.long_rsi_max:
                        side = "LONG"
                        signal_type = "PULLBACK"
                elif allow_short and ema_fast_v < ema_slow_v:
                    # Bearish trend: price pulled back near fast EMA and rejected
                    touch_dist = abs(candles[-1].high - ema_fast_v) / atr_v if atr_v else 999
                    rejected = entry < ema_fast_v and prev_close >= ema_fast_v * 0.998
                    # 2-candle confirmation: previous candle must have come from above/near EMA
                    confirmed = prev_close < prev2_close and entry < prev_close
                    if (touch_dist < 1.0 or rejected) and confirmed and self.params.short_rsi_min <= rsi_v <= self.params.short_rsi_max:
                        side = "SHORT"
                        signal_type = "PULLBACK"

        # Trend momentum entry: DISABLED — historical data shows 0% win rate.
        # Momentum entries chase price too late and get whipsawed.
        # Keeping code commented for future re-evaluation with better filters.
        # if not side and abs(market.funding_rate) <= self.params.funding_abs_limit:
        #     trend_strength = abs(ema_fast_v - ema_slow_v) / entry if entry else 0.0
        #     if trend_strength > 0.004:
        #         if allow_short and ema_fast_v < ema_slow_v and entry < ema_fast_v:
        #             if self.params.short_rsi_min <= rsi_v <= self.params.short_rsi_max:
        #                 side = "SHORT"
        #                 signal_type = "MOMENTUM"
        #         elif allow_long and ema_fast_v > ema_slow_v and entry > ema_fast_v:
        #             if self.params.long_rsi_min <= rsi_v <= self.params.long_rsi_max:
        #                 side = "LONG"
        #                 signal_type = "MOMENTUM"

        if not side:
            return None

        # Volume confirmation: skip only very low volume entries.
        if len(candles) >= 20:
            recent_vols = [c.volume for c in candles[-20:]]
            avg_vol = sum(recent_vols) / len(recent_vols)
            if avg_vol > 0 and last.volume < avg_vol * 0.5:
                return None

        # Candle body confirmation: skip only strong reversal candles
        # (body > 50% of range in wrong direction)
        candle_range = last.high - last.low
        if candle_range > 0:
            body_ratio = abs(last.close - last.open) / candle_range
            if side == "LONG" and last.close < last.open and body_ratio > 0.6:
                return None
            if side == "SHORT" and last.close > last.open and body_ratio > 0.6:
                return None

        sl_distance = atr_v * self.params.atr_multiplier
        if side == "LONG":
            stop_loss = entry - sl_distance
            take_profit = entry + (sl_distance * self.params.risk_reward)
        else:
            stop_loss = entry + sl_distance
            take_profit = entry - (sl_distance * self.params.risk_reward)

        trend_strength = abs(ema_fast_v - ema_slow_v) / entry if entry else 0.0
        trend_score = min(trend_strength / 0.002, 1.0)
        if side == "LONG":
            rsi_center = (self.params.long_rsi_min + self.params.long_rsi_max) / 2
            rsi_half = max((self.params.long_rsi_max - self.params.long_rsi_min) / 2, 1.0)
        else:
            rsi_center = (self.params.short_rsi_min + self.params.short_rsi_max) / 2
            rsi_half = max((self.params.short_rsi_max - self.params.short_rsi_min) / 2, 1.0)
        rsi_score = max(0.0, 1 - abs(rsi_v - rsi_center) / rsi_half)
        atr_mid = (self.params.min_atr_pct + self.params.max_atr_pct) / 2
        atr_half_range = max((self.params.max_atr_pct - self.params.min_atr_pct) / 2, 1e-9)
        vol_score = max(0.0, 1 - abs(atr_pct - atr_mid) / atr_half_range)
        funding_score = 1 - min(abs(market.funding_rate) / self.params.funding_abs_limit, 1.0)

        # Crossover freshness: how recently did the EMA cross happen (fewer bars ago = better)?
        if signal_type == "CROSSOVER":
            is_bull = side == "LONG"
            cross_age, _ = _crossover_age_and_drift(recent_diffs, is_bull)
            freshness_score = max(0.0, 1.0 - cross_age / crossover_lookback)
        else:
            freshness_score = 0.0  # pullback/momentum don't rely on crossover age

        # Macro trend alignment bonus: signal passed the EMA(trend) filter → aligned with macro trend
        macro_bonus = 0.08 if (self.params.ema_trend > 0 and len(close_prices) >= self.params.ema_trend) else 0.0

        confidence = (
            0.08
            + (0.35 * trend_score)
            + (0.18 * rsi_score)
            + (0.15 * vol_score)
            + (0.10 * funding_score)
            + (0.12 * freshness_score)
            + macro_bonus
        )
        # Non-crossover entries get confidence discounts
        if signal_type == "PULLBACK":
            confidence *= 0.85  # tightened — pullbacks need stronger base conviction
        confidence = max(0.0, min(confidence, 0.99))

        if confidence < self.params.min_confidence:
            return None

        reason = (
            f"{side} {signal_type.lower()} | EMA({self.params.ema_fast}/{self.params.ema_slow})={ema_fast_v:.2f}/{ema_slow_v:.2f}, "
            f"RSI={rsi_v:.1f}, ATR%={atr_pct:.4f}, funding={market.funding_rate:.5f}"
        )
        return Signal(
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            entry=round(entry, 6),
            take_profit=round(take_profit, 6),
            stop_loss=round(stop_loss, 6),
            confidence=round(confidence, 4),
            reason=reason,
            signal_time_ms=last.close_time_ms,
        )

    def adaptive_tune_after_trade(self, trade_result: str) -> None:
        if trade_result == "LOSS":
            self.params.min_confidence = min(0.90, self.params.min_confidence + 0.01)
            return

        self.params.min_confidence = max(0.75, self.params.min_confidence - 0.005)
