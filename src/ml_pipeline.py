from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

from .bulk_backtester import MarketDataset
from .indicators import atr, ema, rsi
from .strategy import StrategyEngine


@dataclass
class SignalSample:
    symbol: str
    timeframe: str
    side: str
    open_time_ms: int
    close_time_ms: int
    features: List[float]
    label: int
    pnl_r: float
    confidence: float


@dataclass
class FoldResult:
    fold_index: int
    threshold: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    expectancy_r: float


@dataclass
class WalkForwardResult:
    strategy: Dict
    tested_signals: int
    total_selected_trades: int
    wins: int
    losses: int
    win_rate: float
    expectancy_r: float
    folds: List[FoldResult]
    per_market: List[Dict]
    tested_thresholds: List[float]


class StandardScaler:
    def __init__(self) -> None:
        self.mean: List[float] = []
        self.std: List[float] = []

    def fit(self, vectors: Sequence[Sequence[float]]) -> None:
        if not vectors:
            raise ValueError("Cannot fit scaler on empty vectors")

        dim = len(vectors[0])
        n = len(vectors)
        self.mean = [0.0] * dim
        self.std = [0.0] * dim

        for row in vectors:
            for i, value in enumerate(row):
                self.mean[i] += value

        self.mean = [m / n for m in self.mean]

        for row in vectors:
            for i, value in enumerate(row):
                diff = value - self.mean[i]
                self.std[i] += diff * diff

        self.std = [math.sqrt(v / max(1, n - 1)) if v > 0 else 1.0 for v in self.std]

    def transform(self, vectors: Sequence[Sequence[float]]) -> List[List[float]]:
        out: List[List[float]] = []
        for row in vectors:
            scaled = []
            for i, value in enumerate(row):
                denom = self.std[i] if self.std[i] > 1e-12 else 1.0
                scaled.append((value - self.mean[i]) / denom)
            out.append(scaled)
        return out


class LogisticBinaryClassifier:
    def __init__(self, learning_rate: float = 0.05, epochs: int = 250, l2: float = 0.0005):
        self.learning_rate = learning_rate
        self.epochs = epochs
        self.l2 = l2
        self.weights: List[float] = []
        self.bias = 0.0

    @staticmethod
    def _sigmoid(z: float) -> float:
        if z >= 0:
            ez = math.exp(-z)
            return 1 / (1 + ez)
        ez = math.exp(z)
        return ez / (1 + ez)

    def fit(self, x: Sequence[Sequence[float]], y: Sequence[int]) -> None:
        if not x:
            raise ValueError("Cannot fit model on empty dataset")

        n = len(x)
        d = len(x[0])
        self.weights = [0.0] * d
        self.bias = 0.0

        for _ in range(self.epochs):
            grad_w = [0.0] * d
            grad_b = 0.0

            for row, label in zip(x, y):
                z = self.bias
                for j in range(d):
                    z += self.weights[j] * row[j]
                p = self._sigmoid(z)
                error = p - label

                grad_b += error
                for j in range(d):
                    grad_w[j] += error * row[j]

            grad_b /= n
            for j in range(d):
                grad_w[j] = (grad_w[j] / n) + (self.l2 * self.weights[j])
                self.weights[j] -= self.learning_rate * grad_w[j]

            self.bias -= self.learning_rate * grad_b

    def predict_proba(self, x: Sequence[Sequence[float]]) -> List[float]:
        probs: List[float] = []
        for row in x:
            z = self.bias
            for j, value in enumerate(row):
                z += self.weights[j] * value
            probs.append(self._sigmoid(z))
        return probs


class MLWalkForwardOptimizer:
    def __init__(
        self,
        risk_usd: float,
        fee_bps_per_side: float = 0.0,
        slippage_bps_per_side: float = 0.0,
    ):
        self.risk_usd = risk_usd
        self.fee_bps_per_side = fee_bps_per_side
        self.slippage_bps_per_side = slippage_bps_per_side

    @staticmethod
    def _safe_div(a: float, b: float) -> float:
        if abs(b) < 1e-12:
            return 0.0
        return a / b

    def trade_cost_r(self, entry: float, stop_loss: float) -> float:
        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit < 1e-12:
            return 0.0

        roundtrip_bps = 2.0 * (self.fee_bps_per_side + self.slippage_bps_per_side)
        roundtrip_cost_per_unit = entry * (roundtrip_bps / 10_000.0)
        return roundtrip_cost_per_unit / risk_per_unit

    def _feature_vector(
        self,
        dataset: MarketDataset,
        candles,
        idx: int,
        entry: float,
        stop_loss: float,
        take_profit: float,
        side: str,
        confidence: float,
        ema_fast_period: int,
        ema_slow_period: int,
        rsi_period: int,
        atr_period: int,
    ) -> List[float]:
        window = candles[: idx + 1]
        closes = [c.close for c in window]
        ema_fast_v = ema(closes, ema_fast_period)
        ema_slow_v = ema(closes, ema_slow_period)
        rsi_v = rsi(closes, rsi_period)
        atr_v = atr(window, atr_period)

        candle = candles[idx]
        body = candle.close - candle.open
        total_range = max(candle.high - candle.low, 1e-9)
        upper_wick = candle.high - max(candle.open, candle.close)
        lower_wick = min(candle.open, candle.close) - candle.low

        prev3 = candles[idx - 3].close if idx >= 3 else candles[0].close
        prev6 = candles[idx - 6].close if idx >= 6 else candles[0].close
        prev12 = candles[idx - 12].close if idx >= 12 else candles[0].close

        vol_slice = [c.volume for c in candles[max(0, idx - 20) : idx + 1]]
        vol_mean = sum(vol_slice) / max(1, len(vol_slice))

        rr = self._safe_div(abs(take_profit - entry), abs(entry - stop_loss))
        side_num = 1.0 if side == "LONG" else -1.0

        return [
            side_num,
            self._safe_div(entry - ema_fast_v, entry),
            self._safe_div(ema_fast_v - ema_slow_v, entry),
            rsi_v / 100.0,
            self._safe_div(atr_v, entry),
            self._safe_div(body, total_range),
            self._safe_div(upper_wick, total_range),
            self._safe_div(lower_wick, total_range),
            self._safe_div(candle.close - prev3, prev3),
            self._safe_div(candle.close - prev6, prev6),
            self._safe_div(candle.close - prev12, prev12),
            self._safe_div(candle.volume - vol_mean, vol_mean),
            dataset.market.funding_rate,
            confidence,
            rr,
        ]

    def _simulate_outcome(
        self,
        side: str,
        entry: float,
        take_profit: float,
        stop_loss: float,
        candles,
        start_idx: int,
        max_horizon_bars: int,
    ) -> Tuple[bool, int] | None:
        end = min(len(candles), start_idx + max_horizon_bars)

        for i in range(start_idx, end):
            c = candles[i]
            if side == "LONG":
                hit_sl = c.low <= stop_loss
                hit_tp = c.high >= take_profit
                if not hit_sl and not hit_tp:
                    continue
                # Conservative order when both are touched in same candle.
                if hit_sl:
                    return False, c.close_time_ms
                return True, c.close_time_ms

            hit_sl = c.high >= stop_loss
            hit_tp = c.low <= take_profit
            if not hit_sl and not hit_tp:
                continue
            if hit_sl:
                return False, c.close_time_ms
            return True, c.close_time_ms

        return None

    def generate_samples(
        self,
        datasets: List[MarketDataset],
        strategy_payload: Dict,
        max_horizon_bars: int = 120,
    ) -> List[SignalSample]:
        strategy = StrategyEngine.from_dict(copy.deepcopy(strategy_payload))

        samples: List[SignalSample] = []
        for dataset in datasets:
            candles = dataset.candles
            warmup = max(
                strategy.params.ema_slow,
                strategy.params.rsi_period + 2,
                strategy.params.atr_period + 2,
            )

            for idx in range(warmup, len(candles) - 1):
                signal = strategy.evaluate(
                    dataset.symbol,
                    dataset.timeframe,
                    candles[: idx + 1],
                    dataset.market,
                )
                if signal is None:
                    continue

                outcome = self._simulate_outcome(
                    side=signal.side,
                    entry=signal.entry,
                    take_profit=signal.take_profit,
                    stop_loss=signal.stop_loss,
                    candles=candles,
                    start_idx=idx + 1,
                    max_horizon_bars=max_horizon_bars,
                )
                if outcome is None:
                    continue

                is_win, close_time_ms = outcome
                rr = self._safe_div(
                    abs(signal.take_profit - signal.entry),
                    abs(signal.entry - signal.stop_loss),
                )
                gross_pnl_r = rr if is_win else -1.0
                cost_r = self.trade_cost_r(signal.entry, signal.stop_loss)
                pnl_r = gross_pnl_r - cost_r

                features = self._feature_vector(
                    dataset=dataset,
                    candles=candles,
                    idx=idx,
                    entry=signal.entry,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                    side=signal.side,
                    confidence=signal.confidence,
                    ema_fast_period=strategy.params.ema_fast,
                    ema_slow_period=strategy.params.ema_slow,
                    rsi_period=strategy.params.rsi_period,
                    atr_period=strategy.params.atr_period,
                )

                samples.append(
                    SignalSample(
                        symbol=dataset.symbol,
                        timeframe=dataset.timeframe,
                        side=signal.side,
                        open_time_ms=signal.signal_time_ms,
                        close_time_ms=close_time_ms,
                        features=features,
                        label=1 if pnl_r > 0 else 0,
                        pnl_r=pnl_r,
                        confidence=signal.confidence,
                    )
                )

        samples.sort(key=lambda s: s.open_time_ms)
        return samples

    def _select_sequential_trades(
        self,
        samples: Sequence[SignalSample],
        probs: Sequence[float],
        threshold: float,
        start_available_ms: int,
    ) -> Tuple[List[SignalSample], int]:
        selected: List[SignalSample] = []
        available_ms = start_available_ms

        for sample, p in sorted(zip(samples, probs), key=lambda t: t[0].open_time_ms):
            if p < threshold:
                continue
            if sample.open_time_ms < available_ms:
                continue
            selected.append(sample)
            available_ms = sample.close_time_ms

        return selected, available_ms

    @staticmethod
    def _score_samples(samples: Sequence[SignalSample]) -> Tuple[int, int, float, float]:
        trades = len(samples)
        if trades == 0:
            return 0, 0, 0.0, 0.0
        wins = sum(1 for s in samples if s.label == 1)
        losses = trades - wins
        win_rate = wins / trades
        expectancy_r = sum(s.pnl_r for s in samples) / trades
        return wins, losses, win_rate, expectancy_r

    def walk_forward(
        self,
        samples: List[SignalSample],
        strategy_payload: Dict,
        target_trades: int,
        folds: int = 6,
        initial_train_frac: float = 0.55,
        threshold_grid: Iterable[float] | None = None,
    ) -> WalkForwardResult:
        if threshold_grid is None:
            threshold_grid = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9]

        if len(samples) < 250:
            raise RuntimeError("Not enough labeled signals for walk-forward")

        threshold_grid = list(threshold_grid)
        n = len(samples)
        train_start = int(n * initial_train_frac)
        step = max(1, (n - train_start) // folds)

        selected_all: List[SignalSample] = []
        fold_results: List[FoldResult] = []
        available_ms = -1

        for fold_index in range(folds):
            train_end = train_start + (fold_index * step)
            test_end = n if fold_index == folds - 1 else min(n, train_end + step)
            if train_end < 120 or test_end - train_end < 20:
                continue

            train_pool = samples[:train_end]
            test_samples = samples[train_end:test_end]

            calib_size = max(30, int(len(train_pool) * 0.2))
            fit_samples = train_pool[:-calib_size]
            calib_samples = train_pool[-calib_size:]
            if len(fit_samples) < 80:
                continue

            scaler = StandardScaler()
            scaler.fit([s.features for s in fit_samples])
            x_fit = scaler.transform([s.features for s in fit_samples])
            y_fit = [s.label for s in fit_samples]

            model = LogisticBinaryClassifier(learning_rate=0.05, epochs=260, l2=0.0008)
            model.fit(x_fit, y_fit)

            x_calib = scaler.transform([s.features for s in calib_samples])
            p_calib = model.predict_proba(x_calib)

            best_threshold = 0.6
            best_score = None
            for threshold in threshold_grid:
                picked, _ = self._select_sequential_trades(
                    calib_samples,
                    p_calib,
                    threshold,
                    start_available_ms=-1,
                )
                wins, losses, win_rate, expectancy_r = self._score_samples(picked)
                trades = len(picked)
                if trades < 5:
                    continue
                score = (win_rate, wins, expectancy_r)
                if best_score is None or score > best_score:
                    best_score = score
                    best_threshold = threshold

            x_test = scaler.transform([s.features for s in test_samples])
            p_test = model.predict_proba(x_test)
            chosen, available_ms = self._select_sequential_trades(
                test_samples,
                p_test,
                threshold=best_threshold,
                start_available_ms=available_ms,
            )

            wins, losses, win_rate, expectancy_r = self._score_samples(chosen)
            fold_results.append(
                FoldResult(
                    fold_index=fold_index + 1,
                    threshold=best_threshold,
                    trades=len(chosen),
                    wins=wins,
                    losses=losses,
                    win_rate=win_rate,
                    expectancy_r=expectancy_r,
                )
            )

            selected_all.extend(chosen)
            if len(selected_all) >= target_trades:
                break

        selected_all.sort(key=lambda s: s.open_time_ms)
        selected_all = selected_all[:target_trades]

        wins, losses, win_rate, expectancy_r = self._score_samples(selected_all)
        per_market: Dict[Tuple[str, str], Dict] = {}
        for sample in selected_all:
            key = (sample.symbol, sample.timeframe)
            if key not in per_market:
                per_market[key] = {
                    "symbol": sample.symbol,
                    "timeframe": sample.timeframe,
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                }
            rec = per_market[key]
            rec["trades"] += 1
            if sample.label == 1:
                rec["wins"] += 1
            else:
                rec["losses"] += 1

        per_market_list = []
        for key in sorted(per_market.keys()):
            rec = per_market[key]
            rec["win_rate"] = rec["wins"] / rec["trades"] if rec["trades"] else 0.0
            per_market_list.append(rec)

        return WalkForwardResult(
            strategy=copy.deepcopy(strategy_payload),
            tested_signals=len(samples),
            total_selected_trades=len(selected_all),
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            expectancy_r=expectancy_r,
            folds=fold_results,
            per_market=per_market_list,
            tested_thresholds=threshold_grid,
        )

    def optimize(
        self,
        datasets: List[MarketDataset],
        base_strategy: Dict,
        target_trades: int,
        target_wins: int,
        max_candidates: int = 48,
    ) -> Tuple[WalkForwardResult, int]:
        candidates: List[Dict] = []

        for ema_fast in [8, 13, 21]:
            for ema_slow in [34, 55]:
                if ema_fast >= ema_slow:
                    continue
                for atr_mult in [0.8, 1.0, 1.2, 1.6]:
                    for rr in [0.5, 0.8, 1.0]:
                        for min_conf in [0.6, 0.7, 0.8]:
                            candidate = copy.deepcopy(base_strategy)
                            candidate["ema_fast"] = ema_fast
                            candidate["ema_slow"] = ema_slow
                            candidate["atr_multiplier"] = atr_mult
                            candidate["risk_reward"] = rr
                            candidate["min_confidence"] = min_conf
                            candidate["long_rsi_min"] = 55
                            candidate["long_rsi_max"] = 72
                            candidate["short_rsi_min"] = 28
                            candidate["short_rsi_max"] = 48
                            candidates.append(candidate)

        tested = 0
        best: WalkForwardResult | None = None

        for strategy_payload in candidates[:max_candidates]:
            samples = self.generate_samples(datasets, strategy_payload)
            if len(samples) < 250:
                tested += 1
                continue

            result = self.walk_forward(
                samples=samples,
                strategy_payload=strategy_payload,
                target_trades=target_trades,
            )
            tested += 1

            score = (result.wins, result.win_rate, result.expectancy_r)
            if best is None or score > (best.wins, best.win_rate, best.expectancy_r):
                best = result

            if result.total_selected_trades >= target_trades and result.wins >= target_wins:
                return result, tested

        if best is None:
            raise RuntimeError("ML optimizer did not produce valid results")

        return best, tested
