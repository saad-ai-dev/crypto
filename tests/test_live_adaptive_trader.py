import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.live_adaptive_trader import CandidateSignal, LiveAdaptivePaperTrader
from src.models import Candle, ClosedTrade, MarketContext, Signal


class _DisabledExecutor:
    enabled = False


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


def _trader(cfg: dict | None = None) -> LiveAdaptivePaperTrader:
    with patch("src.live_adaptive_trader.BinanceExecutor.from_env", return_value=_DisabledExecutor()):
        return LiveAdaptivePaperTrader(cfg or _config())


def _candidate(
    symbol: str,
    confidence: float,
    expectancy_r: float,
    score: float,
    reason: str = "LONG pullback | test",
) -> CandidateSignal:
    signal = Signal(
        symbol=symbol,
        timeframe="5m",
        side="LONG",
        entry=100.0,
        take_profit=101.0,
        stop_loss=99.0,
        confidence=confidence,
        reason=reason,
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
        trader = _trader(cfg)

        self.assertEqual(trader.risk_usd, 5.0)
        self.assertEqual(trader.risk_sizing_mode, "paper_risk_usd")
        self.assertEqual(trader._summary()["paper_risk_usd"], 5.0)

    def test_filter_rejection_telemetry_is_reported(self) -> None:
        cfg = _config()
        cfg["live_loop"]["require_dual_timeframe_confirm"] = True
        trader = _trader(cfg)
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
        trader = _trader(cfg)

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
        trader = _trader(cfg)

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

    def test_short_break_even_price_moves_below_entry(self) -> None:
        trader = _trader(_config())
        self.assertEqual(trader._break_even_stop_price("SHORT", 100.0, 2.0, 0.05), 99.9)

    def test_signal_score_multiplier_penalizes_crossover_and_boosts_pullback(self) -> None:
        trader = _trader(_config())
        self.assertLess(trader._signal_score_multiplier("CROSSOVER"), 1.0)
        self.assertGreater(trader._signal_score_multiplier("PULLBACK"), 1.0)

    def test_finalize_closed_trade_emits_trade_meta(self) -> None:
        trader = _trader(_config())
        signal = Signal(
            symbol="BTCUSDT",
            timeframe="5m",
            side="LONG",
            entry=100.0,
            take_profit=101.0,
            stop_loss=99.0,
            confidence=0.8,
            reason="LONG pullback | test",
            signal_time_ms=1,
        )
        managed = trader._make_managed_trade(signal, binance_opened=False)
        managed.moved_to_break_even = True
        managed.last_known_candles = [
            Candle(
                open_time_ms=1,
                open=100.0,
                high=100.8,
                low=99.9,
                close=100.6,
                volume=10.0,
                close_time_ms=300001,
            )
        ]
        managed.engine.active_trade.stop_loss = 100.05
        closed = trader._make_exit(managed, managed.last_known_candles[-1], "ADVERSE_CUT")

        printed = []

        def fake_print(line: str) -> None:
            printed.append(json.loads(line))

        with patch("src.live_adaptive_trader.print", fake_print):
            trader._finalize_closed_trade(managed, closed, 1, False, False)

        event = next(item for item in printed if item["type"] == "TRADE_RESULT")
        self.assertEqual(event["type"], "TRADE_RESULT")
        self.assertEqual(event["trade_meta"]["signal_type"], "PULLBACK")
        self.assertEqual(event["trade_meta"]["exit_type"], "ADVERSE_CUT")
        self.assertEqual(event["trade_meta"]["stop_state"], "BREAKEVEN")

    def test_candidate_quality_block_reason_rejects_weak_crossover(self) -> None:
        trader = _trader(_config())
        reason = trader._candidate_quality_block_reason(
            symbol="BTCUSDT",
            market=MarketContext(mark_price=100.0, funding_rate=0.0, open_interest=100000.0),
            signal_type="CROSSOVER",
            trend_strength=trader.crossover_min_trend_strength / 2.0,
            confidence=0.9,
            symbol_quality=1.0,
        )
        self.assertEqual(reason, "weak_crossover_trend")

    def test_finalize_closed_trade_skips_duplicate_result_emission(self) -> None:
        trader = _trader(_config())
        signal = Signal(
            symbol="BTCUSDT",
            timeframe="5m",
            side="LONG",
            entry=100.0,
            take_profit=101.0,
            stop_loss=99.0,
            confidence=0.8,
            reason="LONG pullback | test",
            signal_time_ms=1,
        )
        managed = trader._make_managed_trade(signal, binance_opened=False)
        candle = Candle(
            open_time_ms=1,
            open=100.0,
            high=100.8,
            low=99.9,
            close=100.6,
            volume=10.0,
            close_time_ms=300001,
        )
        closed = trader._make_exit(managed, candle, "ADVERSE_CUT")

        printed = []

        def fake_print(line: str) -> None:
            printed.append(json.loads(line))

        with patch("src.live_adaptive_trader.print", fake_print):
            trader._finalize_closed_trade(managed, closed, 1, False, False)
            trader._finalize_closed_trade(managed, closed, 1, False, False)

        self.assertEqual(sum(1 for event in printed if event["type"] == "TRADE_RESULT"), 1)
        self.assertEqual(sum(1 for event in printed if event["type"] == "TRADE_RESULT_DUPLICATE_SKIPPED"), 1)

    def test_finalize_closed_trade_applies_reentry_cooldown(self) -> None:
        trader = _trader(_config())
        signal = Signal(
            symbol="BTCUSDT",
            timeframe="5m",
            side="LONG",
            entry=100.0,
            take_profit=101.0,
            stop_loss=99.0,
            confidence=0.8,
            reason="LONG pullback | test",
            signal_time_ms=1,
        )
        managed = trader._make_managed_trade(signal, binance_opened=False)
        candle = Candle(
            open_time_ms=1,
            open=100.0,
            high=100.8,
            low=99.9,
            close=100.6,
            volume=10.0,
            close_time_ms=300001,
        )
        closed = trader._make_exit(managed, candle, "STAGNATION_EXIT")

        with patch("src.live_adaptive_trader.print", lambda *_: None):
            trader._finalize_closed_trade(managed, closed, 1, False, False)

        self.assertEqual(trader.symbol_cooldowns["BTCUSDT"], trader.fast_exit_reentry_cooldown_cycles)

    def test_crossover_execution_gate_blocks_marginal_setup(self) -> None:
        cfg = _config()
        cfg["live_loop"]["crossover_min_confidence"] = 0.84
        cfg["live_loop"]["crossover_execute_min_confidence"] = 0.84
        cfg["live_loop"]["crossover_execute_min_expectancy_r"] = 0.32
        cfg["live_loop"]["crossover_execute_min_score"] = 0.86
        cfg["live_loop"]["crossover_execute_min_win_probability"] = 0.72
        trader = _trader(cfg)
        candidates = [
            _candidate(
                "BTCUSDT",
                confidence=0.84,
                expectancy_r=0.24,
                score=0.84,
                reason="LONG crossover | test",
            )
        ]

        printed = []

        def fake_print(line: str) -> None:
            printed.append(json.loads(line))

        with patch("src.live_adaptive_trader.time.sleep", lambda *_: None), patch(
            "src.live_adaptive_trader.print", fake_print
        ):
            trader._refresh_batch_market_data = lambda: None
            trader._signal_candidates = lambda: candidates
            trader._estimate_win_probability = lambda candidate: 0.70
            result = trader.run()

        self.assertEqual(result["status"], "MAX_CYCLES_REACHED")
        no_signal = next(event for event in printed if event["type"] == "NO_SIGNAL")
        self.assertEqual(no_signal["reason"], "EXECUTION_FILTER_BLOCK")
        self.assertEqual(no_signal["execution_rejections"]["execute_expectancy"], 1)
        self.assertEqual(no_signal["execution_rejections"]["execute_crossover_expectancy"], 1)

    def test_daily_loss_limit_pauses_new_entries_for_the_day(self) -> None:
        cfg = _config()
        cfg["live_loop"]["daily_loss_limit_r"] = 1.5
        trader = _trader(cfg)
        loss_day = "2026-04-10"
        closed_at_ms = int(datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        trader._record_trade(
            ClosedTrade(
                symbol="BTCUSDT",
                timeframe="5m",
                side="LONG",
                entry=100.0,
                take_profit=101.0,
                stop_loss=99.0,
                exit_price=98.4,
                result="LOSS",
                opened_at_ms=closed_at_ms - 300000,
                closed_at_ms=closed_at_ms,
                pnl_r=-1.6,
                pnl_usd=-0.32,
                reason="DIRECT_SL",
            )
        )

        printed = []

        def fake_print(line: str) -> None:
            printed.append(json.loads(line))

        with patch("src.live_adaptive_trader.time.sleep", lambda *_: None), patch(
            "src.live_adaptive_trader.print", fake_print
        ):
            trader._current_utc_day = lambda: loss_day
            trader._refresh_batch_market_data = lambda: None
            trader._signal_candidates = lambda: [_candidate("BTCUSDT", confidence=0.95, expectancy_r=0.5, score=0.95)]
            result = trader.run()

        self.assertEqual(result["status"], "MAX_CYCLES_REACHED")
        self.assertTrue(any(event["type"] == "DAILY_LOSS_LIMIT_PAUSE" for event in printed))
        no_signal = next(event for event in printed if event["type"] == "NO_SIGNAL")
        self.assertEqual(no_signal["reason"], "DAILY_LOSS_LIMIT_PAUSED")

    def test_daily_loss_limit_clears_on_new_utc_day(self) -> None:
        cfg = _config()
        cfg["live_loop"]["daily_loss_limit_r"] = 1.5
        trader = _trader(cfg)
        trader._daily_loss_pause_day = "2026-04-10"

        printed = []

        def fake_print(line: str) -> None:
            printed.append(json.loads(line))

        with patch("src.live_adaptive_trader.time.sleep", lambda *_: None), patch(
            "src.live_adaptive_trader.print", fake_print
        ):
            trader._current_utc_day = lambda: "2026-04-11"
            trader._refresh_batch_market_data = lambda: None
            trader._signal_candidates = lambda: []
            result = trader.run()

        self.assertEqual(result["status"], "MAX_CYCLES_REACHED")
        self.assertTrue(any(event["type"] == "DAILY_LOSS_LIMIT_CLEARED" for event in printed))
        no_signal = next(event for event in printed if event["type"] == "NO_SIGNAL")
        self.assertEqual(no_signal["reason"], "NO_CANDIDATES")


if __name__ == "__main__":
    unittest.main()
