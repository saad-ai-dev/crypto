from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from .binance_futures_rest import BinanceFuturesRestClient
from .models import Candle, ClosedTrade, MarketContext
from .strategy import StrategyEngine
from .trade_engine import TradeEngine


@dataclass
class MarketDataset:
    symbol: str
    timeframe: str
    candles: List[Candle]
    market: MarketContext


@dataclass
class CandidateResult:
    strategy: Dict
    trades: int
    wins: int
    losses: int
    win_rate: float
    expectancy_r: float
    expectancy_usd: float
    per_market: List[Dict]


class BulkBacktester:
    def __init__(self, config: Dict):
        self.config = config
        ds = config.get("data_source", {})
        self.client = BinanceFuturesRestClient(
            allow_mock_fallback=bool(ds.get("allow_mock_fallback", False)),
            force_mock=bool(ds.get("force_mock", False)),
            mock_seed=int(ds.get("mock_seed", 42)),
        )
        acct = config["account"]
        self.risk_usd = float(acct["starting_balance_usd"]) * float(acct["risk_per_trade_pct"])

    def build_datasets(
        self,
        symbols: Iterable[str],
        timeframes: Iterable[str],
        history_limit: int,
    ) -> List[MarketDataset]:
        datasets: List[MarketDataset] = []
        for symbol in symbols:
            market = self.client.fetch_market_context(symbol)
            for timeframe in timeframes:
                candles = self.client.fetch_klines(symbol=symbol, interval=timeframe, limit=history_limit)
                datasets.append(
                    MarketDataset(symbol=symbol, timeframe=timeframe, candles=candles, market=market)
                )

        if self.client.used_mock:
            raise RuntimeError("Live-only run requested but mock data source was used")

        return datasets

    def simulate_candidate(
        self,
        strategy_payload: Dict,
        datasets: List[MarketDataset],
        target_total_trades: int,
        target_per_market: int,
    ) -> CandidateResult:
        all_closed: List[ClosedTrade] = []
        per_market: List[Dict] = []

        for dataset in datasets:
            strategy = StrategyEngine.from_dict(copy.deepcopy(strategy_payload))
            engine = TradeEngine(risk_usd=self.risk_usd)

            warmup = max(
                strategy.params.ema_slow,
                strategy.params.rsi_period + 2,
                strategy.params.atr_period + 2,
            )

            market_closed: List[ClosedTrade] = []
            for idx in range(warmup, len(dataset.candles)):
                current = dataset.candles[idx]

                closed = engine.on_candle(current)
                if closed:
                    market_closed.append(closed)
                    all_closed.append(closed)
                    strategy.adaptive_tune_after_trade(closed.result)

                    if len(market_closed) >= target_per_market:
                        break
                    if len(all_closed) >= target_total_trades:
                        break

                if engine.active_trade is not None:
                    continue

                signal = strategy.evaluate(
                    dataset.symbol,
                    dataset.timeframe,
                    dataset.candles[: idx + 1],
                    dataset.market,
                )
                if signal:
                    engine.maybe_open_trade(signal)

            wins = sum(1 for t in market_closed if t.result == "WIN")
            losses = sum(1 for t in market_closed if t.result == "LOSS")
            per_market.append(
                {
                    "symbol": dataset.symbol,
                    "timeframe": dataset.timeframe,
                    "trades": len(market_closed),
                    "wins": wins,
                    "losses": losses,
                    "win_rate": round((wins / len(market_closed)), 4) if market_closed else 0.0,
                }
            )

            if len(all_closed) >= target_total_trades:
                break

        trades = len(all_closed)
        wins = sum(1 for t in all_closed if t.result == "WIN")
        losses = sum(1 for t in all_closed if t.result == "LOSS")
        win_rate = (wins / trades) if trades else 0.0
        expectancy_r = (sum(t.pnl_r for t in all_closed) / trades) if trades else 0.0
        expectancy_usd = expectancy_r * self.risk_usd

        return CandidateResult(
            strategy=copy.deepcopy(strategy_payload),
            trades=trades,
            wins=wins,
            losses=losses,
            win_rate=win_rate,
            expectancy_r=expectancy_r,
            expectancy_usd=expectancy_usd,
            per_market=per_market,
        )

    def optimize(
        self,
        datasets: List[MarketDataset],
        target_total_trades: int,
        min_wins_target: int,
        target_per_market: int,
    ) -> Tuple[CandidateResult, int]:
        base = copy.deepcopy(self.config["strategy"])

        ema_fast_opts = [8, 13]
        ema_slow_opts = [34, 55]
        atr_mult_opts = [0.8, 1.0, 1.2]
        rr_opts = [0.9, 1.0, 1.1]
        min_conf_opts = [0.65, 0.7, 0.75, 0.8]
        long_ranges = [(55, 70), (58, 70)]
        short_ranges = [(30, 50), (32, 48)]

        tested = 0
        best: CandidateResult | None = None

        for (
            ema_fast,
            ema_slow,
            atr_mult,
            rr,
            min_conf,
            long_r,
            short_r,
        ) in itertools.product(
            ema_fast_opts,
            ema_slow_opts,
            atr_mult_opts,
            rr_opts,
            min_conf_opts,
            long_ranges,
            short_ranges,
        ):
            if ema_fast >= ema_slow:
                continue

            payload = copy.deepcopy(base)
            payload["ema_fast"] = ema_fast
            payload["ema_slow"] = ema_slow
            payload["atr_multiplier"] = atr_mult
            payload["risk_reward"] = rr
            payload["min_confidence"] = min_conf
            payload["long_rsi_min"], payload["long_rsi_max"] = long_r
            payload["short_rsi_min"], payload["short_rsi_max"] = short_r

            candidate = self.simulate_candidate(
                payload,
                datasets,
                target_total_trades=target_total_trades,
                target_per_market=target_per_market,
            )
            tested += 1

            score = (candidate.wins, candidate.win_rate, candidate.expectancy_r)
            if best is None or score > (best.wins, best.win_rate, best.expectancy_r):
                best = candidate

            if candidate.trades >= target_total_trades and candidate.wins >= min_wins_target:
                return candidate, tested

        if best is None:
            raise RuntimeError("No valid candidate found")

        return best, tested
