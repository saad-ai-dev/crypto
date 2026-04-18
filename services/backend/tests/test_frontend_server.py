import json
import tempfile
import unittest
from pathlib import Path

from services.frontend.server import AnalyticsEngine, EventStateCache, NewsFetcher, TradeHistoryCache


class TradeHistoryCacheTests(unittest.TestCase):
    def test_event_state_marks_binance_orphan_close_as_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            events_file = data_dir / "live_events.jsonl"

            events = [
                {
                    "type": "OPEN_TRADE",
                    "time": "2026-04-10T00:24:12.735134+00:00",
                    "cycle": 10,
                    "symbol": "AAVEUSDT",
                    "timeframe": "15m",
                    "side": "SHORT",
                    "entry": 89.96,
                    "take_profit": 89.20645,
                    "stop_loss": 90.71355,
                    "confidence": 0.9793,
                    "score": 0.745667,
                    "reason": "SHORT crossover | test",
                },
                {
                    "type": "BINANCE_ORDER",
                    "time": "2026-04-10T00:24:16.274395+00:00",
                    "action": "OPEN",
                    "symbol": "AAVEUSDT",
                    "side": "SHORT",
                    "result": {
                        "status": "filled",
                        "executed": True,
                        "order_id": 146448068,
                        "entry_price": 89.89777777778,
                        "quantity": 2.7,
                        "notional": 242.72,
                    },
                },
                {
                    "type": "BINANCE_ORDER",
                    "time": "2026-04-10T00:39:13.225121+00:00",
                    "action": "ORPHAN_CLOSE",
                    "symbol": "AAVEUSDT",
                    "side": "SHORT",
                    "pnl": -0.54600002,
                },
            ]
            events_file.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            cache = EventStateCache(events_file=events_file)
            state = cache.refresh()
            open_trade = state["open_trade"]

            self.assertEqual(open_trade["symbol"], "AAVEUSDT")
            self.assertEqual(open_trade["signal_state"], "CLOSED")
            self.assertEqual(open_trade["binance_close_action"], "ORPHAN_CLOSE")
            self.assertEqual(open_trade["closed_result"], "LOSS")
            self.assertAlmostEqual(open_trade["closed_pnl_usd"], -0.54600002, places=6)
            self.assertFalse(open_trade["binance_executed"])


class NewsFetcherTests(unittest.TestCase):
    def test_parse_items_extracts_summary_and_sorts_by_publish_time(self) -> None:
        xml = """
        <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
          <channel>
            <item>
              <title>Older headline</title>
              <link>https://example.com/older</link>
              <pubDate>Fri, 18 Apr 2026 07:00:00 GMT</pubDate>
              <description><![CDATA[<p>Older summary text</p>]]></description>
            </item>
            <item>
              <title>Fresh headline</title>
              <link>https://example.com/fresh</link>
              <pubDate>Fri, 18 Apr 2026 09:30:00 GMT</pubDate>
              <content:encoded><![CDATA[<div>Fresh market summary with <strong>details</strong>.</div>]]></content:encoded>
            </item>
          </channel>
        </rss>
        """
        fetcher = NewsFetcher(refresh_seconds=10, max_items=10)
        items = fetcher._parse_items(xml, "Example")

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Older headline")
        self.assertEqual(items[0]["summary"], "Older summary text")
        self.assertEqual(items[1]["summary"], "Fresh market summary with details.")
        self.assertIsInstance(items[1]["published_ts"], int)

        fetcher._read_url = lambda _url, timeout_sec=6: xml  # type: ignore[assignment]
        payload = fetcher.refresh(force=True)
        self.assertEqual(payload["items"][0]["title"], "Fresh headline")
        self.assertEqual(payload["newest_published_at"], "2026-04-18T09:30:00+00:00")
        self.assertGreater(payload["newest_published_ts"], payload["items"][1]["published_ts"])

    def test_event_state_marks_trade_result_as_not_binance_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            events_file = data_dir / "live_events.jsonl"

            events = [
                {
                    "type": "OPEN_TRADE",
                    "time": "2026-04-10T00:24:12.735134+00:00",
                    "cycle": 10,
                    "symbol": "AAVEUSDT",
                    "timeframe": "15m",
                    "side": "SHORT",
                    "entry": 89.96,
                    "take_profit": 89.20645,
                    "stop_loss": 90.71355,
                    "confidence": 0.9793,
                    "score": 0.745667,
                    "reason": "SHORT crossover | test",
                },
                {
                    "type": "BINANCE_ORDER",
                    "time": "2026-04-10T00:24:16.274395+00:00",
                    "action": "OPEN",
                    "symbol": "AAVEUSDT",
                    "side": "SHORT",
                    "result": {
                        "status": "filled",
                        "executed": True,
                        "order_id": 146448068,
                        "entry_price": 89.89777777778,
                        "quantity": 2.7,
                        "notional": 242.72,
                    },
                },
                {
                    "type": "TRADE_RESULT",
                    "time": "2026-04-10T00:39:13.225121+00:00",
                    "cycle": 10,
                    "trade": {
                        "symbol": "AAVEUSDT",
                        "timeframe": "15m",
                        "side": "SHORT",
                        "entry": 89.96,
                        "take_profit": 89.20645,
                        "stop_loss": 90.71355,
                        "exit_price": 90.10,
                        "result": "LOSS",
                        "opened_at_ms": 1000,
                        "closed_at_ms": 2000,
                        "pnl_r": -0.2,
                        "pnl_usd": -0.04,
                        "reason": "STAGNATION_EXIT",
                    },
                    "summary": {"wins": 0, "losses": 1},
                },
            ]
            events_file.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            cache = EventStateCache(events_file=events_file)
            state = cache.refresh()
            open_trade = state["open_trade"]

            self.assertEqual(open_trade["signal_state"], "CLOSED")
            self.assertFalse(open_trade["binance_executed"])

    def test_duplicate_trade_result_rows_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            history_file = data_dir / "live_events_history.jsonl"
            config_file = root / "config.json"
            config_file.write_text(json.dumps({"execution": {}}), encoding="utf-8")

            trade = {
                "symbol": "BTCUSDT",
                "timeframe": "5m",
                "side": "LONG",
                "entry": 100.0,
                "take_profit": 101.0,
                "stop_loss": 99.0,
                "exit_price": 99.3,
                "result": "LOSS",
                "opened_at_ms": 1000,
                "closed_at_ms": 2000,
                "pnl_r": -0.7,
                "pnl_usd": -0.14,
                "reason": "ADVERSE_CUT | LONG crossover | test",
            }
            events = [
                {
                    "type": "TRADE_RESULT",
                    "time": "2026-04-09T10:00:00+00:00",
                    "cycle": 1,
                    "trade": trade,
                    "trade_meta": {
                        "signal_type": "CROSSOVER",
                        "exit_type": "ADVERSE_CUT",
                        "stop_state": "ORIGINAL",
                        "hold_minutes": 10.0,
                    },
                    "summary": {"wins": 0, "losses": 1},
                },
                {
                    "type": "TRADE_RESULT",
                    "time": "2026-04-09T10:00:05+00:00",
                    "cycle": 1,
                    "trade": dict(trade),
                    "trade_meta": {
                        "signal_type": "CROSSOVER",
                        "exit_type": "ADVERSE_CUT",
                        "stop_state": "ORIGINAL",
                        "hold_minutes": 10.0,
                    },
                    "summary": {"wins": 0, "losses": 1},
                },
            ]
            history_file.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            cache = TradeHistoryCache(history_file=history_file, max_items=100, config_file=config_file)
            payload = cache.refresh(limit=20)

            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["items"][0]["trade_key"], "BTCUSDT|5m|LONG|1000|2000")

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

            cache = TradeHistoryCache(history_file=history_file, max_items=100, config_file=config_file)
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

    def test_analytics_exposes_loss_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            history_file = data_dir / "live_events_history.jsonl"
            config_file = root / "config.json"
            config_file.write_text(json.dumps({"execution": {}}), encoding="utf-8")

            events = [
                {
                    "type": "TRADE_RESULT",
                    "time": "2026-04-09T10:00:00+00:00",
                    "cycle": 1,
                    "trade": {
                        "symbol": "BTCUSDT",
                        "timeframe": "5m",
                        "side": "LONG",
                        "entry": 100.0,
                        "take_profit": 101.0,
                        "stop_loss": 99.0,
                        "exit_price": 99.4,
                        "result": "LOSS",
                        "opened_at_ms": 1000,
                        "closed_at_ms": 601000,
                        "pnl_r": -0.6,
                        "pnl_usd": -0.12,
                        "reason": "ADVERSE_CUT | LONG crossover | test",
                    },
                    "trade_meta": {
                        "signal_type": "CROSSOVER",
                        "exit_type": "ADVERSE_CUT",
                        "stop_state": "ORIGINAL",
                        "hold_minutes": 10.0,
                    },
                },
                {
                    "type": "TRADE_RESULT",
                    "time": "2026-04-09T11:00:00+00:00",
                    "cycle": 2,
                    "trade": {
                        "symbol": "ETHUSDT",
                        "timeframe": "15m",
                        "side": "LONG",
                        "entry": 200.0,
                        "take_profit": 202.0,
                        "stop_loss": 198.0,
                        "exit_price": 202.0,
                        "result": "WIN",
                        "opened_at_ms": 1000,
                        "closed_at_ms": 3601000,
                        "pnl_r": 1.0,
                        "pnl_usd": 0.2,
                        "reason": "TP_HIT | LONG pullback | test",
                    },
                    "trade_meta": {
                        "signal_type": "PULLBACK",
                        "exit_type": "DIRECT_TP",
                        "stop_state": "TRAILING",
                        "hold_minutes": 60.0,
                    },
                },
            ]
            history_file.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            cache = TradeHistoryCache(history_file=history_file, max_items=100, config_file=config_file)
            engine = AnalyticsEngine(history_cache=cache)
            payload = engine.compute()

            self.assertEqual(payload["worst_symbols"][0]["symbol"], "BTCUSDT")
            self.assertEqual(payload["worst_exit_types"][0]["exit_type"], "ADVERSE_CUT")
            self.assertEqual(payload["worst_signal_exit_combos"][0]["combo"], "CROSSOVER x ADVERSE_CUT")
            self.assertEqual(payload["duration_breakdown"][0]["bucket"], "<=15M")
            self.assertEqual(payload["stop_state_breakdown"][0]["stop_state"], "ORIGINAL")


if __name__ == "__main__":
    unittest.main()
