from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .indicators import atr, ema, rsi
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

        side: Optional[str] = None
        if (
            ema_fast_v > ema_slow_v
            and self.params.long_rsi_min <= rsi_v <= self.params.long_rsi_max
            and entry >= ema_fast_v
            and market.funding_rate <= self.params.funding_abs_limit
        ):
            side = "LONG"
        elif (
            ema_fast_v < ema_slow_v
            and self.params.short_rsi_min <= rsi_v <= self.params.short_rsi_max
            and entry <= ema_fast_v
            and market.funding_rate >= -self.params.funding_abs_limit
        ):
            side = "SHORT"

        if not side:
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
        rsi_score = 1 - abs(rsi_v - 50) / 50
        vol_score = 1 - abs(atr_pct - ((self.params.min_atr_pct + self.params.max_atr_pct) / 2)) / (
            self.params.max_atr_pct
        )
        funding_score = 1 - min(abs(market.funding_rate) / self.params.funding_abs_limit, 1.0)

        confidence = 0.25 + (0.35 * trend_score) + (0.15 * rsi_score) + (0.15 * vol_score) + (0.10 * funding_score)
        confidence = max(0.0, min(confidence, 0.99))

        if confidence < self.params.min_confidence:
            return None

        reason = (
            f"{side} trend setup | EMA({self.params.ema_fast}/{self.params.ema_slow})={ema_fast_v:.2f}/{ema_slow_v:.2f}, "
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
        # Losses tighten filters and slightly reduce RR to improve next hit probability.
        if trade_result == "LOSS":
            self.params.min_confidence = min(0.8, self.params.min_confidence + 0.02)
            self.params.risk_reward = max(1.2, self.params.risk_reward - 0.1)
            self.params.atr_multiplier = min(2.6, self.params.atr_multiplier + 0.05)
            return

        # Wins slowly restore baseline aggressiveness.
        self.params.min_confidence = max(0.55, self.params.min_confidence - 0.01)
        self.params.risk_reward = min(2.2, self.params.risk_reward + 0.05)
        self.params.atr_multiplier = max(1.4, self.params.atr_multiplier - 0.03)
