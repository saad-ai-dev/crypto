from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Dict, Tuple

from .alerts import play_trade_alert
from .binance_futures_rest import BinanceFuturesRestClient
from .strategy import StrategyEngine
from .trade_engine import TradeEngine


class MarketScanner:
    def __init__(self, config: Dict):
        self.config = config
        ds = config.get("data_source", {})
        self.client = BinanceFuturesRestClient(
            allow_mock_fallback=bool(ds.get("allow_mock_fallback", False)),
            force_mock=bool(ds.get("force_mock", False)),
            mock_seed=int(ds.get("mock_seed", 42)),
        )
        self.strategy = StrategyEngine.from_dict(config["strategy"])
        account_cfg = config["account"]
        starting_balance = float(account_cfg["starting_balance_usd"])
        risk_per_trade_pct = float(account_cfg["risk_per_trade_pct"])
        self.trade_engines: Dict[Tuple[str, str], TradeEngine] = {
            (symbol, timeframe): TradeEngine(risk_usd=starting_balance * risk_per_trade_pct)
            for symbol in config["pairs"]
            for timeframe in config["timeframes"]
        }
        self.last_signal_key: Dict[Tuple[str, str], int] = {}

    def run_once(self) -> None:
        signals = []
        closures = []

        for symbol in self.config["pairs"]:
            market = self.client.fetch_market_context(symbol)
            for timeframe in self.config["timeframes"]:
                candles = self.client.fetch_klines(
                    symbol=symbol,
                    interval=timeframe,
                    limit=int(self.config["strategy"]["lookback_candles"]),
                )
                if not candles:
                    continue

                key = (symbol, timeframe)
                engine = self.trade_engines[key]

                closed = engine.on_candle(candles[-1])
                if closed:
                    closures.append(closed)

                signal = self.strategy.evaluate(symbol, timeframe, candles, market)
                if signal is None:
                    continue

                if self.last_signal_key.get(key) == signal.signal_time_ms:
                    continue

                opened = engine.maybe_open_trade(signal)
                self.last_signal_key[key] = signal.signal_time_ms
                if opened:
                    signals.append(signal)

        now = datetime.now(timezone.utc).isoformat()
        if self.client.used_mock:
            print(json.dumps({"type": "INFO", "time": now, "message": "Mock fallback data source is active"}))

        for signal in signals:
            play_trade_alert(self.config["scanner"]["enable_sound"])
            print(
                json.dumps(
                    {
                        "type": "NEW_SIGNAL",
                        "time": now,
                        "symbol": signal.symbol,
                        "timeframe": signal.timeframe,
                        "side": signal.side,
                        "entry": signal.entry,
                        "take_profit": signal.take_profit,
                        "stop_loss": signal.stop_loss,
                        "confidence": signal.confidence,
                        "reason": signal.reason,
                    }
                )
            )

        for closed in closures:
            print(
                json.dumps(
                    {
                        "type": "TRADE_CLOSED",
                        "time": now,
                        "symbol": closed.symbol,
                        "timeframe": closed.timeframe,
                        "side": closed.side,
                        "entry": closed.entry,
                        "exit_price": closed.exit_price,
                        "take_profit": closed.take_profit,
                        "stop_loss": closed.stop_loss,
                        "result": closed.result,
                        "pnl_r": round(closed.pnl_r, 4),
                        "pnl_usd": round(closed.pnl_usd, 4),
                    }
                )
            )

    def run_forever(self) -> None:
        poll_seconds = int(self.config["scanner"]["poll_seconds"])
        while True:
            try:
                self.run_once()
            except Exception as exc:
                print(json.dumps({"type": "ERROR", "message": str(exc)}))
            time.sleep(poll_seconds)
