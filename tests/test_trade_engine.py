import unittest

from src.models import Candle, Signal
from src.trade_engine import TradeEngine


class TradeEngineTests(unittest.TestCase):
    def test_long_trade_hits_tp(self) -> None:
        engine = TradeEngine(risk_usd=1.0)
        signal = Signal(
            symbol="BTCUSDT",
            timeframe="5m",
            side="LONG",
            entry=100.0,
            take_profit=102.0,
            stop_loss=99.0,
            confidence=0.7,
            reason="test",
            signal_time_ms=1,
        )
        self.assertTrue(engine.maybe_open_trade(signal))

        candle = Candle(
            open_time_ms=2,
            open=100.0,
            high=102.5,
            low=99.8,
            close=101.8,
            volume=1.0,
            close_time_ms=3,
        )
        closed = engine.on_candle(candle)
        self.assertIsNotNone(closed)
        self.assertEqual(closed.result, "WIN")

    def test_short_trade_hits_sl(self) -> None:
        engine = TradeEngine(risk_usd=1.0)
        signal = Signal(
            symbol="BTCUSDT",
            timeframe="5m",
            side="SHORT",
            entry=100.0,
            take_profit=98.0,
            stop_loss=101.0,
            confidence=0.7,
            reason="test",
            signal_time_ms=1,
        )
        self.assertTrue(engine.maybe_open_trade(signal))

        candle = Candle(
            open_time_ms=2,
            open=100.0,
            high=101.2,
            low=99.5,
            close=100.8,
            volume=1.0,
            close_time_ms=3,
        )
        closed = engine.on_candle(candle)
        self.assertIsNotNone(closed)
        self.assertEqual(closed.result, "LOSS")


if __name__ == "__main__":
    unittest.main()
