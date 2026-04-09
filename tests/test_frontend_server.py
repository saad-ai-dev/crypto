import json
import tempfile
import unittest
from pathlib import Path

from frontend.server import TradeHistoryCache


class TradeHistoryCacheTests(unittest.TestCase):
    def test_binance_orphan_close_is_added_to_history_and_replaced_by_trade_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            history_file = data_dir / "live_events_history.jsonl"
            config_file = root / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "execution": {
                            "fee_bps_per_side": 2,
                            "slippage_bps_per_side": 1,
                        }
                    }
                ),
                encoding="utf-8",
            )

            events = [
                {
                    "type": "OPEN_TRADE",
                    "time": "2026-04-09T16:26:01.405427+00:00",
                    "cycle": 46,
                    "symbol": "ETHUSDT",
                    "timeframe": "15m",
                    "side": "LONG",
                    "entry": 2211.31,
                    "take_profit": 2225.996958,
                    "stop_loss": 2196.623042,
                },
                {
                    "type": "BINANCE_ORDER",
                    "time": "2026-04-09T16:26:04.616823+00:00",
                    "action": "OPEN",
                    "symbol": "ETHUSDT",
                    "side": "LONG",
                    "result": {
                        "status": "filled",
                        "executed": True,
                        "order_id": 8634595243,
                        "entry_price": 2211.31,
                        "quantity": 0.112,
                        "notional": 247.67,
                    },
                },
                {
                    "type": "BINANCE_ORDER",
                    "time": "2026-04-09T16:30:27.002570+00:00",
                    "action": "ORPHAN_CLOSE",
                    "symbol": "ETHUSDT",
                    "side": "LONG",
                    "pnl": -0.30912,
                },
            ]
            history_file.write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )

            cache = TradeHistoryCache(history_file=history_file, max_items=100)
            first = cache.refresh(limit=50)

            self.assertEqual(first["count"], 1)
            row = first["items"][0]
            self.assertEqual(row["symbol"], "ETHUSDT")
            self.assertEqual(row["result"], "LOSS")
            self.assertEqual(row["reason"], "BINANCE_ORPHAN_CLOSE")
            self.assertTrue(row["binance_executed"])
            self.assertTrue(row["synthetic"])
            self.assertAlmostEqual(row["pnl_usd"], -0.30912, places=6)
            self.assertAlmostEqual(row["pnl_r"], -0.1879218283, places=6)

            actual_result = {
                "type": "TRADE_RESULT",
                "time": "2026-04-09T16:30:28.002570+00:00",
                "cycle": 46,
                "trade": {
                    "symbol": "ETHUSDT",
                    "timeframe": "15m",
                    "side": "LONG",
                    "entry": 2211.31,
                    "take_profit": 2225.996958,
                    "stop_loss": 2196.623042,
                    "exit_price": 2208.55,
                    "result": "LOSS",
                    "opened_at_ms": 1775751961405,
                    "closed_at_ms": 1775752228002,
                    "pnl_r": -0.21,
                    "pnl_usd": -0.345,
                    "reason": "MANAGED_EXIT",
                },
                "summary": {"wins": 0, "losses": 1},
                "binance_executed": True,
                "binance_closed": True,
            }
            with history_file.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(actual_result) + "\n")

            second = cache.refresh(limit=50)
            self.assertEqual(second["count"], 1)
            updated = second["items"][0]
            self.assertEqual(updated["reason"], "MANAGED_EXIT")
            self.assertFalse(updated.get("synthetic", False))
            self.assertAlmostEqual(updated["pnl_usd"], -0.345, places=6)


if __name__ == "__main__":
    unittest.main()
