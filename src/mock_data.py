from __future__ import annotations

import random
import time
from typing import Dict, List, Tuple

from .models import Candle, MarketContext

_INTERVAL_TO_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

_BASE_PRICE = {
    "BTCUSDT": 96000.0,
    "ETHUSDT": 2900.0,
    "SOLUSDT": 185.0,
    "BNBUSDT": 620.0,
    "XRPUSDT": 2.4,
}


class MockFuturesData:
    def __init__(self, seed: int = 42):
        self.seed = seed
        self._cache: Dict[Tuple[str, str, int], List[Candle]] = {}

    def _interval_seconds(self, interval: str) -> int:
        if interval not in _INTERVAL_TO_SECONDS:
            raise ValueError(f"Unsupported interval for mock mode: {interval}")
        return _INTERVAL_TO_SECONDS[interval]

    def klines(self, symbol: str, interval: str, limit: int) -> List[Candle]:
        key = (symbol, interval, limit)
        if key in self._cache:
            return self._cache[key]

        interval_seconds = self._interval_seconds(interval)
        end_time_s = int(time.time())
        start_time_s = end_time_s - (limit * interval_seconds)

        rng = random.Random(f"{self.seed}:{symbol}:{interval}:{limit}")
        base = _BASE_PRICE.get(symbol, 100.0)

        candles: List[Candle] = []
        prev_close = base

        for i in range(limit):
            t_s = start_time_s + (i * interval_seconds)
            regime = (i // 120) % 3
            if regime == 0:
                drift = 0.0012
            elif regime == 1:
                drift = -0.001
            else:
                drift = 0.0002

            noise = rng.uniform(-0.0035, 0.0035)
            pct_move = drift + noise

            open_price = prev_close
            close_price = max(0.0001, open_price * (1 + pct_move))

            # Add wicks to create realistic TP/SL touches.
            wick_up = max(open_price, close_price) * rng.uniform(0.0006, 0.0024)
            wick_dn = min(open_price, close_price) * rng.uniform(0.0006, 0.0024)
            high = max(open_price, close_price) + wick_up
            low = min(open_price, close_price) - wick_dn
            volume = abs(close_price - open_price) * rng.uniform(200, 1400)

            candles.append(
                Candle(
                    open_time_ms=t_s * 1000,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close_price,
                    volume=volume,
                    close_time_ms=(t_s + interval_seconds - 1) * 1000,
                )
            )
            prev_close = close_price

        self._cache[key] = candles
        return candles

    def market_context(self, symbol: str, mark_price: float) -> MarketContext:
        rng = random.Random(f"{self.seed}:{symbol}:market")
        funding = rng.uniform(-0.0004, 0.0004)
        oi_base = _BASE_PRICE.get(symbol, 100.0) * 2500
        open_interest = oi_base * (1 + rng.uniform(-0.08, 0.08))
        return MarketContext(
            mark_price=mark_price,
            funding_rate=funding,
            open_interest=open_interest,
        )
