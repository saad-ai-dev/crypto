from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int


@dataclass(frozen=True)
class MarketContext:
    mark_price: float
    funding_rate: float
    open_interest: float


@dataclass(frozen=True)
class Signal:
    symbol: str
    timeframe: str
    side: str
    entry: float
    take_profit: float
    stop_loss: float
    confidence: float
    reason: str
    signal_time_ms: int


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    timeframe: str
    side: str
    entry: float
    take_profit: float
    stop_loss: float
    exit_price: float
    result: str
    opened_at_ms: int
    closed_at_ms: int
    pnl_r: float
    pnl_usd: float
    reason: str


@dataclass
class OpenTrade:
    symbol: str
    timeframe: str
    side: str
    entry: float
    take_profit: float
    stop_loss: float
    opened_at_ms: int
    reason: str
    signal_confidence: float

    def update_with_candle(
        self,
        candle: Candle,
        risk_usd: float,
    ) -> Optional[ClosedTrade]:
        if self.side == "LONG":
            hit_sl = candle.low <= self.stop_loss
            hit_tp = candle.high >= self.take_profit
            if not hit_sl and not hit_tp:
                return None

            # Conservative fill order when both levels are touched in one candle.
            if hit_sl:
                exit_price = self.stop_loss
                result = "LOSS"
            else:
                exit_price = self.take_profit
                result = "WIN"
        else:
            hit_sl = candle.high >= self.stop_loss
            hit_tp = candle.low <= self.take_profit
            if not hit_sl and not hit_tp:
                return None

            if hit_sl:
                exit_price = self.stop_loss
                result = "LOSS"
            else:
                exit_price = self.take_profit
                result = "WIN"

        risk_per_unit = abs(self.entry - self.stop_loss)
        pnl_per_unit = (
            exit_price - self.entry if self.side == "LONG" else self.entry - exit_price
        )
        pnl_r = pnl_per_unit / risk_per_unit if risk_per_unit > 0 else 0.0
        pnl_usd = pnl_r * risk_usd

        return ClosedTrade(
            symbol=self.symbol,
            timeframe=self.timeframe,
            side=self.side,
            entry=self.entry,
            take_profit=self.take_profit,
            stop_loss=self.stop_loss,
            exit_price=exit_price,
            result=result,
            opened_at_ms=self.opened_at_ms,
            closed_at_ms=candle.close_time_ms,
            pnl_r=pnl_r,
            pnl_usd=pnl_usd,
            reason=self.reason,
        )
