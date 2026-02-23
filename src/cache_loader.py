from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, List

from .bulk_backtester import MarketDataset
from .models import Candle, MarketContext


def load_market_datasets_from_cache(
    cache_dir: str,
    symbols: Iterable[str],
    timeframes: Iterable[str],
) -> List[MarketDataset]:
    base = Path(cache_dir)
    datasets: List[MarketDataset] = []

    for symbol in symbols:
        premium_path = base / f"{symbol}_premium.json"
        oi_path = base / f"{symbol}_open_interest.json"

        premium = json.loads(premium_path.read_text(encoding="utf-8"))
        open_interest = json.loads(oi_path.read_text(encoding="utf-8"))
        market = MarketContext(
            mark_price=float(premium["markPrice"]),
            funding_rate=float(premium["lastFundingRate"]),
            open_interest=float(open_interest["openInterest"]),
        )

        for timeframe in timeframes:
            kline_path = base / f"{symbol}_{timeframe}_klines.json"
            payload = json.loads(kline_path.read_text(encoding="utf-8"))
            candles = [
                Candle(
                    open_time_ms=int(item[0]),
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                    close_time_ms=int(item[6]),
                )
                for item in payload
            ]
            datasets.append(MarketDataset(symbol=symbol, timeframe=timeframe, candles=candles, market=market))

    return datasets
