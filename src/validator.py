from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List

from .binance_futures_rest import BinanceFuturesRestClient
from .models import Candle, ClosedTrade
from .strategy import StrategyEngine
from .trade_engine import TradeEngine


class TenTradeValidator:
    def __init__(self, config: Dict):
        self.config = config
        ds = config.get("data_source", {})
        self.client = BinanceFuturesRestClient(
            allow_mock_fallback=bool(ds.get("allow_mock_fallback", False)),
            force_mock=bool(ds.get("force_mock", False)),
            mock_seed=int(ds.get("mock_seed", 42)),
        )
        self.strategy = StrategyEngine.from_dict(config["strategy"])
        acct = config["account"]
        self.risk_usd = float(acct["starting_balance_usd"]) * float(acct["risk_per_trade_pct"])

    def _expectancy(self, trades: List[ClosedTrade]) -> float:
        if not trades:
            return 0.0
        return sum(t.pnl_r for t in trades) / len(trades)

    def run(self, verbose: bool = True) -> Dict:
        vcfg = self.config["validation"]
        symbol = vcfg["symbol"]
        timeframe = vcfg["timeframe"]
        history_limit = int(vcfg["history_limit"])
        target = int(vcfg["target_closed_trades"])

        market = self.client.fetch_market_context(symbol)
        candles = self.client.fetch_klines(symbol=symbol, interval=timeframe, limit=history_limit)

        engine = TradeEngine(risk_usd=self.risk_usd)
        closed_trades: List[ClosedTrade] = []
        signal_logs = []

        warmup = max(
            self.strategy.params.ema_slow,
            self.strategy.params.rsi_period + 2,
            self.strategy.params.atr_period + 2,
        )

        for idx in range(warmup, len(candles)):
            current = candles[idx]

            closed = engine.on_candle(current)
            if closed:
                closed_trades.append(closed)
                self.strategy.adaptive_tune_after_trade(closed.result)
                if len(closed_trades) >= target:
                    break

            if engine.active_trade is not None:
                continue

            window: List[Candle] = candles[: idx + 1]
            signal = self.strategy.evaluate(symbol, timeframe, window, market)
            if signal is None:
                continue

            opened = engine.maybe_open_trade(signal)
            if opened:
                signal_logs.append(
                    {
                        "signal_index": len(signal_logs) + 1,
                        "time": datetime.fromtimestamp(
                            signal.signal_time_ms / 1000, tz=timezone.utc
                        ).isoformat(),
                        "symbol": signal.symbol,
                        "timeframe": signal.timeframe,
                        "side": signal.side,
                        "entry": signal.entry,
                        "take_profit": signal.take_profit,
                        "stop_loss": signal.stop_loss,
                        "confidence": signal.confidence,
                        "params": {
                            "min_confidence": round(self.strategy.params.min_confidence, 4),
                            "risk_reward": round(self.strategy.params.risk_reward, 4),
                            "atr_multiplier": round(self.strategy.params.atr_multiplier, 4),
                        },
                    }
                )

        wins = sum(1 for t in closed_trades if t.result == "WIN")
        losses = sum(1 for t in closed_trades if t.result == "LOSS")
        win_rate = (wins / len(closed_trades)) if closed_trades else 0.0
        expectancy_r = self._expectancy(closed_trades)
        expectancy_usd = expectancy_r * self.risk_usd

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_mode": "mock_fallback" if self.client.used_mock else "live_rest",
            "symbol": symbol,
            "timeframe": timeframe,
            "requested_closed_trades": target,
            "actual_closed_trades": len(closed_trades),
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "expectancy_r": round(expectancy_r, 4),
            "expectancy_usd_per_trade": round(expectancy_usd, 6),
            "risk_usd_per_trade": round(self.risk_usd, 6),
            "signals": signal_logs,
            "closed_trades": [asdict(t) for t in closed_trades],
            "final_strategy_params": {
                "min_confidence": round(self.strategy.params.min_confidence, 4),
                "risk_reward": round(self.strategy.params.risk_reward, 4),
                "atr_multiplier": round(self.strategy.params.atr_multiplier, 4),
            },
        }

        if verbose:
            print(json.dumps(payload, indent=2))
        return payload
