import json
import unittest
from unittest.mock import patch

from src.live_adaptive_trader import CandidateSignal, LiveAdaptivePaperTrader
from src.models import Signal


def _config() -> dict:
    return {
        "account": {
            "starting_balance_usd": 10.0,
            "risk_per_trade_pct": 0.02,
        },
        "execution": {"fee_bps_per_side": 2, "slippage_bps_per_side": 1},
        "strategy": {
            "ema_fast": 8,
            "ema_slow": 34,
            "rsi_period": 14,
            "atr_period": 14,
            "atr_multiplier": 1.2,
            "risk_reward": 1.0,
            "min_atr_pct": 0.0015,
            "max_atr_pct": 0.01,
            "funding_abs_limit": 0.001,
            "min_confidence": 0.65,
            "long_rsi_min": 45,
            "long_rsi_max": 70,
            "short_rsi_min": 20,
            "short_rsi_max": 50,
        },
        "live_loop": {
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "timeframes": ["5m"],
            "lookback_candles": 260,
            "poll_seconds": 0,
            "execute_min_confidence": 0.75,
            "execute_min_expectancy_r": 0.2,
            "execute_min_score": 0.75,
            "execute_min_win_probability": 0.5,
            "min_candidate_confidence": 0.73,
            "min_candidate_expectancy_r": 0.18,
            "require_dual_timeframe_confirm": False,
            "max_cycles": 1,
            "target_trades": 999,
            "min_trades_for_success": 999,
        },
        "scanner": {"enable_sound": False},
    }


def _candidate(symbol: str, confidence: float, expectancy_r: float, score: float) -> CandidateSignal:
    signal = Signal(
        symbol=symbol,
        timeframe="5m",
        side="LONG",
        entry=100.0,
        take_profit=101.0,
        stop_loss=99.0,
        confidence=confidence,
        reason="test",
        signal_time_ms=1,
    )
    return CandidateSignal(
        signal=signal,
        trend_strength=0.01,
        cost_r=0.01,
        rr=1.0,
        expectancy_r=expectancy_r,
        symbol_quality=1.0,
        score=score,
    )


class LiveAdaptivePaperTraderTests(unittest.TestCase):
    def test_paper_risk_override_is_used(self) -> None:
        cfg = _config()
        cfg["account"]["paper_risk_usd"] = 5.0
        trader = LiveAdaptivePaperTrader(cfg)

        self.assertEqual(trader.risk_usd, 5.0)
        self.assertEqual(trader.risk_sizing_mode, "paper_risk_usd")
        self.assertEqual(trader._summary()["paper_risk_usd"], 5.0)

    def test_filter_rejection_telemetry_is_reported(self) -> None:
        cfg = _config()
        cfg["live_loop"]["require_dual_timeframe_confirm"] = True
        trader = LiveAdaptivePaperTrader(cfg)
        candidates = [
            _candidate("CONFUSDT", confidence=0.60, expectancy_r=0.30, score=0.90),
            _candidate("EXPUSDT", confidence=0.80, expectancy_r=0.05, score=0.90),
            _candidate("SCOREUSDT", confidence=0.80, expectancy_r=0.30, score=0.60),
            _candidate("WINUSDT", confidence=0.80, expectancy_r=0.30, score=0.90),
            _candidate("DUALUSDT", confidence=0.80, expectancy_r=0.30, score=0.90),
        ]

        printed = []

        def fake_print(line: str) -> None:
            printed.append(json.loads(line))

        def fake_win_probability(candidate: CandidateSignal) -> float:
            if candidate.signal.symbol == "WINUSDT":
                return 0.40
            return 0.80

        with patch("src.live_adaptive_trader.time.sleep", lambda *_: None), patch(
            "src.live_adaptive_trader.print", fake_print
        ):
            trader._refresh_batch_market_data = lambda: None
            trader._signal_candidates = lambda: candidates
            trader._estimate_win_probability = fake_win_probability
            result = trader.run()

        self.assertEqual(result["status"], "MAX_CYCLES_REACHED")
        summary = result["summary"]
        self.assertEqual(summary["filter_rejections"]["candidate_confidence"], 1)
        self.assertEqual(summary["filter_rejections"]["candidate_expectancy"], 1)
        self.assertEqual(summary["filter_rejections"]["execute_confidence"], 1)
        self.assertEqual(summary["filter_rejections"]["execute_expectancy"], 1)
        self.assertEqual(summary["filter_rejections"]["execute_score"], 1)
        self.assertEqual(summary["filter_rejections"]["execute_win_probability"], 1)
        self.assertEqual(summary["filter_rejections"]["execute_dual_timeframe_confirm"], 1)

        possible_trades = next(event for event in printed if event["type"] == "POSSIBLE_TRADES")
        self.assertEqual(possible_trades["candidate_rejections"]["candidate_confidence"], 1)
        self.assertEqual(possible_trades["candidate_rejections"]["candidate_expectancy"], 1)

        no_signal = next(event for event in printed if event["type"] == "NO_SIGNAL")
        self.assertEqual(no_signal["reason"], "EXECUTION_FILTER_BLOCK")
        self.assertEqual(no_signal["execution_rejections"]["execute_confidence"], 1)
        self.assertEqual(no_signal["execution_rejections"]["execute_expectancy"], 1)
        self.assertEqual(no_signal["execution_rejections"]["execute_score"], 1)
        self.assertEqual(no_signal["execution_rejections"]["execute_win_probability"], 1)
        self.assertEqual(no_signal["execution_rejections"]["execute_dual_timeframe_confirm"], 1)

    def test_invalid_symbols_are_filtered_from_watchlist(self) -> None:
        cfg = _config()
        cfg["live_loop"]["invalid_symbol_failure_threshold"] = 1
        cfg["live_loop"]["symbols"] = ["BTCUSDT", "BADUSDT"]
        trader = LiveAdaptivePaperTrader(cfg)

        printed = []

        def fake_print(line: str) -> None:
            printed.append(json.loads(line))

        with patch("src.live_adaptive_trader.print", fake_print):
            trader.client.fetch_all_premium_index = lambda: {"BTCUSDT": object()}
            trader.client.fetch_all_ticker_prices = lambda: {"BTCUSDT": 100.0}
            trader._refresh_batch_market_data()

        self.assertEqual(trader.symbols, ["BTCUSDT"])
        filtered = next(event for event in printed if event["type"] == "SYMBOLS_FILTERED")
        self.assertEqual(filtered["removed"][0]["symbol"], "BADUSDT")

    def test_run_can_open_new_trade_while_previous_trade_remains_open(self) -> None:
        cfg = _config()
        cfg["live_loop"]["max_cycles"] = 2
        cfg["live_loop"]["max_open_trades"] = 2
        trader = LiveAdaptivePaperTrader(cfg)

        cycle_candidates = [
            [_candidate("BTCUSDT", confidence=0.80, expectancy_r=0.30, score=0.90)],
            [_candidate("ETHUSDT", confidence=0.82, expectancy_r=0.32, score=0.91)],
        ]
        printed = []

        def fake_print(line: str) -> None:
            printed.append(json.loads(line))

        with patch("src.live_adaptive_trader.time.sleep", lambda *_: None), patch(
            "src.live_adaptive_trader.print", fake_print
        ):
            trader._refresh_batch_market_data = lambda: None
            trader._signal_candidates = lambda: cycle_candidates.pop(0) if cycle_candidates else []
            trader._update_open_trades = lambda cycle: None
            trader._estimate_win_probability = lambda candidate: 0.80
            result = trader.run()

        self.assertEqual(result["status"], "MAX_CYCLES_REACHED")
        self.assertEqual(result["summary"]["open_trades_count"], 2)
        open_events = [event for event in printed if event["type"] == "OPEN_TRADE"]
        self.assertEqual(len(open_events), 2)
        self.assertEqual([event["symbol"] for event in open_events], ["BTCUSDT", "ETHUSDT"])


if __name__ == "__main__":
    unittest.main()
