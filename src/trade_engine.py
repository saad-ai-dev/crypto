from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .models import Candle, ClosedTrade, OpenTrade, Signal


@dataclass
class TradeEngine:
    risk_usd: float
    active_trade: Optional[OpenTrade] = None
    closed_trades: List[ClosedTrade] = field(default_factory=list)

    def maybe_open_trade(self, signal: Signal) -> bool:
        if self.active_trade is not None:
            return False

        self.active_trade = OpenTrade(
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            side=signal.side,
            entry=signal.entry,
            take_profit=signal.take_profit,
            stop_loss=signal.stop_loss,
            opened_at_ms=signal.signal_time_ms,
            reason=signal.reason,
            signal_confidence=signal.confidence,
        )
        return True

    def on_candle(self, candle: Candle) -> Optional[ClosedTrade]:
        if self.active_trade is None:
            return None

        closed = self.active_trade.update_with_candle(candle, self.risk_usd)
        if closed:
            self.closed_trades.append(closed)
            self.active_trade = None
            return closed

        return None
