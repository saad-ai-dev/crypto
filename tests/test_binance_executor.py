import unittest
from unittest.mock import patch

from src.binance_executor import BinanceExecutor


class BinanceExecutorTests(unittest.TestCase):
    def test_from_env_uses_configured_wallet_risk_pct(self) -> None:
        config = {
            "account": {
                "risk_per_trade_pct": 0.02,
            }
        }

        with patch("src.binance_executor.os.getenv") as getenv, patch.object(
            BinanceExecutor, "_load_exchange_info", lambda self: None
        ), patch.object(BinanceExecutor, "get_balance", lambda self: 250.0):
            getenv.side_effect = lambda key, default="": {
                "BINANCE_API_KEY": "key",
                "BINANCE_SECRET_KEY": "secret",
                "BINANCE_DEMO": "1",
            }.get(key, default)
            executor = BinanceExecutor.from_env(config)

        self.assertTrue(executor.enabled)
        self.assertAlmostEqual(executor.risk_per_trade_usd, 5.0)
        self.assertAlmostEqual(executor.max_position_usd, 12.5)


if __name__ == "__main__":
    unittest.main()
