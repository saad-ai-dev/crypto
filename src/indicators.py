from __future__ import annotations

from typing import Iterable, List

from .models import Candle


def _as_list(values: Iterable[float]) -> List[float]:
    return list(values)


def ema(values: Iterable[float], period: int) -> float:
    series = _as_list(values)
    if len(series) < period:
        raise ValueError("Not enough values for EMA")

    k = 2 / (period + 1)
    current = sum(series[:period]) / period
    for v in series[period:]:
        current = (v * k) + (current * (1 - k))
    return current


def rsi(values: Iterable[float], period: int) -> float:
    series = _as_list(values)
    if len(series) <= period:
        raise ValueError("Not enough values for RSI")

    gains = []
    losses = []
    for i in range(1, len(series)):
        diff = series[i] - series[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles: List[Candle], period: int) -> float:
    if len(candles) <= period:
        raise ValueError("Not enough candles for ATR")

    true_ranges: List[float] = []
    for i in range(1, len(candles)):
        curr = candles[i]
        prev = candles[i - 1]
        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low - prev.close),
        )
        true_ranges.append(tr)

    atr_value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr_value = ((atr_value * (period - 1)) + tr) / period

    return atr_value
