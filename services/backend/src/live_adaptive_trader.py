from __future__ import annotations

import copy
import json
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .alerts import play_trade_alert
from .binance_executor import BinanceExecutor
from .binance_futures_rest import BinanceFuturesRestClient
from .indicators import ema
from .ml_pipeline import MLWalkForwardOptimizer
from .models import ClosedTrade, MarketContext, Signal
from .policy_engine import SmartPolicyEngine
from .strategy import StrategyEngine
from .trade_engine import TradeEngine


@dataclass
class CandidateSignal:
    signal: Signal
    trend_strength: float
    cost_r: float
    rr: float
    expectancy_r: float
    symbol_quality: float
    score: float


@dataclass
class ManagedTrade:
    signal: Signal
    engine: TradeEngine
    start_time: float
    timeframe_minutes: int
    max_wait_seconds: int
    last_seen_close_ms: int
    best_r: float
    original_risk: float
    bars_seen: int
    consecutive_adverse_bars: int
    moved_to_break_even: bool
    trailing_stop_active: bool
    consecutive_fetch_errors: int
    last_known_candles: Optional[List]
    binance_opened: bool


class LiveAdaptivePaperTrader:
    def __init__(self, config: Dict):
        self.config = config
        ds = config.get("data_source", {})
        self.client = BinanceFuturesRestClient(
            allow_mock_fallback=bool(ds.get("allow_mock_fallback", False)),
            force_mock=bool(ds.get("force_mock", False)),
            mock_seed=int(ds.get("mock_seed", 42)),
        )

        self.strategy_payload = copy.deepcopy(config["strategy"])
        self.base_strategy = StrategyEngine.from_dict(copy.deepcopy(config["strategy"]))

        acct = config["account"]
        self.starting_balance_usd = float(acct["starting_balance_usd"])
        self.risk_per_trade_pct = float(acct["risk_per_trade_pct"])
        self.paper_risk_usd = acct.get("paper_risk_usd")
        self.risk_usd = (
            float(self.paper_risk_usd)
            if self.paper_risk_usd is not None
            else self.starting_balance_usd * self.risk_per_trade_pct
        )
        self.risk_sizing_mode = "paper_risk_usd" if self.paper_risk_usd is not None else "balance_pct"

        execution_cfg = config.get("execution", {})
        self.cost_model = MLWalkForwardOptimizer(
            risk_usd=self.risk_usd,
            fee_bps_per_side=float(execution_cfg.get("fee_bps_per_side", 0.0)),
            slippage_bps_per_side=float(execution_cfg.get("slippage_bps_per_side", 0.0)),
        )

        live_cfg = config.get("live_loop", {})
        self.symbols = self._normalize_symbols(live_cfg.get("symbols", []))
        self.timeframes = live_cfg.get("timeframes", ["1m", "5m", "15m"])
        self.execute_timeframes = {
            str(v).strip()
            for v in live_cfg.get("execute_timeframes", self.timeframes)
            if str(v).strip()
        }
        if not self.execute_timeframes:
            self.execute_timeframes = {str(v).strip() for v in self.timeframes if str(v).strip()}
        self.lookback = int(live_cfg.get("lookback_candles", 260))
        self.poll_seconds = int(live_cfg.get("poll_seconds", 12))
        self.max_wait_minutes_per_trade = int(live_cfg.get("max_wait_minutes_per_trade", 120))
        self.min_rr_floor = float(live_cfg.get("min_rr_floor", 0.4))
        self.min_trend_strength = float(live_cfg.get("min_trend_strength", 0.0007))
        self.top_n = int(live_cfg.get("top_n", 3))
        self.max_parallel_candidates = int(live_cfg.get("max_parallel_candidates", 10))
        self.possible_trades_limit = int(live_cfg.get("possible_trades_limit", max(100, self.max_parallel_candidates)))
        self.possible_trades_limit = max(10, min(self.possible_trades_limit, 5000))
        self.min_candidate_confidence = float(live_cfg.get("min_candidate_confidence", 0.7))
        self.min_candidate_expectancy_r = float(live_cfg.get("min_candidate_expectancy_r", 0.0))
        self.execute_min_confidence = float(
            live_cfg.get(
                "execute_min_confidence",
                max(self.min_candidate_confidence, float(self.strategy_payload.get("min_confidence", 0.6))),
            )
        )
        self.execute_min_expectancy_r = float(live_cfg.get("execute_min_expectancy_r", max(0.08, self.min_candidate_expectancy_r)))
        self.execute_min_score = float(live_cfg.get("execute_min_score", 0.72))
        self.execute_min_win_probability = float(live_cfg.get("execute_min_win_probability", 0.72))
        self.require_dual_timeframe_confirm = bool(live_cfg.get("require_dual_timeframe_confirm", True))
        self.min_score_gap = float(live_cfg.get("min_score_gap", 0.02))
        self.relax_after_filter_blocks = int(live_cfg.get("relax_after_filter_blocks", 8))
        self.relax_conf_step = float(live_cfg.get("relax_conf_step", 0.005))
        self.relax_expectancy_step = float(live_cfg.get("relax_expectancy_step", 0.01))
        self.relax_score_step = float(live_cfg.get("relax_score_step", 0.005))
        self.relax_min_execute_confidence = float(live_cfg.get("relax_min_execute_confidence", 0.82))
        self.relax_min_execute_expectancy_r = float(live_cfg.get("relax_min_execute_expectancy_r", 0.1))
        self.relax_min_execute_score = float(live_cfg.get("relax_min_execute_score", 0.65))
        self.target_trades = int(live_cfg.get("target_trades", 30))
        self.target_win_rate = float(live_cfg.get("target_win_rate", 0.75))
        self.min_trades_for_success = int(live_cfg.get("min_trades_for_success", 20))
        self.max_cycles = int(live_cfg.get("max_cycles", 1200))
        self.max_open_trades = int(live_cfg.get("max_open_trades", 1))
        self.enable_sound = bool(config.get("scanner", {}).get("enable_sound", True))
        self.enable_break_even = bool(live_cfg.get("enable_break_even", True))
        self.break_even_trigger_r = float(live_cfg.get("break_even_trigger_r", 0.5))
        self.break_even_offset_r = float(live_cfg.get("break_even_offset_r", 0.02))
        self.crossover_score_multiplier = float(live_cfg.get("crossover_score_multiplier", 0.88))
        self.pullback_score_multiplier = float(live_cfg.get("pullback_score_multiplier", 1.03))
        self.bb_reversion_score_multiplier = float(live_cfg.get("bb_reversion_score_multiplier", 1.0))
        self.supertrend_score_multiplier = float(live_cfg.get("supertrend_score_multiplier", 1.08))
        self.max_same_direction_trades = int(live_cfg.get("max_same_direction_trades", 3))
        self.disabled_signal_types = {str(v).strip().upper() for v in live_cfg.get("disabled_signal_types", []) if str(v).strip()}
        self.allowed_execution_regimes = {
            str(v).strip().upper()
            for v in live_cfg.get("allowed_execution_regimes", [])
            if str(v).strip()
        }
        self.crossover_min_trend_strength = float(live_cfg.get("crossover_min_trend_strength", self.min_trend_strength))
        self.crossover_min_confidence = float(live_cfg.get("crossover_min_confidence", self.min_candidate_confidence))
        self.crossover_execute_min_confidence = float(
            live_cfg.get("crossover_execute_min_confidence", max(self.execute_min_confidence, self.crossover_min_confidence))
        )
        self.crossover_execute_min_expectancy_r = float(
            live_cfg.get("crossover_execute_min_expectancy_r", max(self.execute_min_expectancy_r, self.min_candidate_expectancy_r))
        )
        self.crossover_execute_min_score = float(
            live_cfg.get("crossover_execute_min_score", max(self.execute_min_score, self.crossover_score_multiplier))
        )
        self.crossover_execute_min_win_probability = float(
            live_cfg.get("crossover_execute_min_win_probability", self.execute_min_win_probability)
        )
        self.min_symbol_quality_for_entry = float(live_cfg.get("min_symbol_quality_for_entry", 0.55))
        self.min_symbol_history_for_entry = int(live_cfg.get("min_symbol_history_for_entry", 3))
        self.min_symbol_win_rate_for_entry = float(live_cfg.get("min_symbol_win_rate_for_entry", 0.40))
        self.min_symbol_expectancy_r_for_entry = float(live_cfg.get("min_symbol_expectancy_r_for_entry", -0.02))
        self.min_open_interest_notional_usd = float(live_cfg.get("min_open_interest_notional_usd", 0.0))
        self.enable_trailing_stop = bool(live_cfg.get("enable_trailing_stop", True))
        self.trail_trigger_r = float(live_cfg.get("trail_trigger_r", 0.2))
        self.trail_keep_pct = float(live_cfg.get("trail_keep_pct", 0.7))
        self.max_adverse_r_cut = float(live_cfg.get("max_adverse_r_cut", 0.9))
        self.max_wait_candles = int(live_cfg.get("max_wait_candles", 12))
        self.max_stagnation_bars = int(live_cfg.get("max_stagnation_bars", 6))
        self.min_progress_r_for_stagnation = float(live_cfg.get("min_progress_r_for_stagnation", 0.10))
        self.momentum_reversal_bars = int(live_cfg.get("momentum_reversal_bars", 3))
        self.momentum_reversal_r = float(live_cfg.get("momentum_reversal_r", -0.4))
        self.close_orphaned_positions_on_startup = bool(live_cfg.get("close_orphaned_positions_on_startup", False))
        self.reentry_cooldown_cycles = int(live_cfg.get("reentry_cooldown_cycles", 4))
        self.fast_exit_reentry_cooldown_cycles = int(
            live_cfg.get("fast_exit_reentry_cooldown_cycles", max(self.reentry_cooldown_cycles + 2, 6))
        )
        self.fast_exit_minutes_threshold = float(live_cfg.get("fast_exit_minutes_threshold", 8.0))
        guard_cfg = live_cfg.get("performance_guard", {})
        self.guard_enabled = bool(guard_cfg.get("enabled", True))
        self.guard_symbol_window = int(guard_cfg.get("rolling_window_trades", 12))
        self.guard_min_symbol_trades = int(guard_cfg.get("min_symbol_trades", 4))
        self.guard_min_symbol_win_rate = float(guard_cfg.get("min_symbol_win_rate", 0.45))
        self.guard_min_symbol_expectancy_r = float(guard_cfg.get("min_symbol_expectancy_r", -0.05))
        self.guard_cooldown_cycles = int(guard_cfg.get("cooldown_cycles", 6))
        self.guard_min_active_symbols = int(guard_cfg.get("min_active_symbols", 3))
        self.guard_global_window = int(guard_cfg.get("global_window_trades", 10))
        self.guard_global_min_win_rate = float(guard_cfg.get("global_min_win_rate", 0.5))
        self.guard_global_min_expectancy_r = float(guard_cfg.get("global_min_expectancy_r", 0.0))
        loss_guard_cfg = live_cfg.get("loss_guard", {})
        self.loss_guard_enabled = bool(loss_guard_cfg.get("enabled", True))
        self.max_global_consecutive_losses = int(loss_guard_cfg.get("max_global_consecutive_losses", 2))
        self.global_pause_cycles = int(loss_guard_cfg.get("global_pause_cycles", 4))
        self.max_symbol_consecutive_losses = int(loss_guard_cfg.get("max_symbol_consecutive_losses", 2))
        self.symbol_pause_cycles = int(loss_guard_cfg.get("symbol_pause_cycles", max(4, self.guard_cooldown_cycles)))
        self.daily_loss_limit_r = float(live_cfg.get("daily_loss_limit_r", 0.0))

        # Rotating klines window: scan a subset of symbols each cycle
        self.klines_window_size = int(live_cfg.get("klines_window_size", 20))
        self._klines_window_offset = 0

        # Batch market data caches (refreshed each cycle via batch endpoints)
        self._premium_cache: Dict[str, "MarketContext"] = {}
        self._ticker_cache: Dict[str, float] = {}
        self.invalid_symbol_failures: Dict[str, int] = defaultdict(int)
        self.invalid_symbol_failure_threshold = int(live_cfg.get("invalid_symbol_failure_threshold", 2))

        cfg_path = config.get("_config_path")
        root_dir = Path(cfg_path).resolve().parent if cfg_path else Path.cwd()
        runtime_control_path = live_cfg.get("runtime_control_file", "/tmp/crypto-runtime/runtime_control.json")
        runtime_path = Path(runtime_control_path)
        if not runtime_path.is_absolute():
            runtime_path = (root_dir / runtime_path).resolve()
        self.runtime_control_file = runtime_path
        events_path = live_cfg.get("events_file", "")
        if events_path:
            candidate_events_path = Path(events_path)
            if not candidate_events_path.is_absolute():
                candidate_events_path = (root_dir / candidate_events_path).resolve()
        else:
            candidate_events_path = self.runtime_control_file.parent / "live_events.jsonl"
        self.events_file = candidate_events_path
        self._runtime_control_mtime_ns: Optional[int] = None

        base_conf = float(self.strategy_payload.get("min_confidence", 0.6))
        self.symbol_confidence: Dict[str, float] = {s: base_conf for s in self.symbols}
        self.recent_trades: List[ClosedTrade] = []
        self.symbol_recent_trades: Dict[str, List[ClosedTrade]] = defaultdict(list)
        policy_cfg = config.get("policy", {})
        self.policy_engine = SmartPolicyEngine(
            enabled=bool(policy_cfg.get("enable_policy_engine", True)),
            min_trades_for_setup_eval=int(policy_cfg.get("min_trades_for_setup_eval", 3)),
            setup_pause_cycles=int(policy_cfg.get("setup_pause_cycles", 20)),
            negative_expectancy_pause=bool(policy_cfg.get("negative_expectancy_pause", True)),
            min_setup_win_rate=float(policy_cfg.get("min_setup_win_rate", 0.0)),
        )
        self.symbol_cooldowns: Dict[str, int] = {}
        self.symbol_consecutive_losses: Dict[str, int] = defaultdict(int)
        self.global_consecutive_losses = 0
        self.global_pause_cycles_left = 0
        self.no_trade_filter_block_streak = 0
        self.filter_rejections: Dict[str, int] = defaultdict(int)
        self.open_trades: Dict[str, ManagedTrade] = {}
        self._emitted_trade_result_keys: set[str] = set()
        self._daily_loss_pause_day: Optional[str] = None

        # Binance order executor (demo or live)
        self.executor = BinanceExecutor.from_env(config)
        self._close_orphaned_positions()

    @staticmethod
    def _normalize_symbols(symbols: List[str]) -> List[str]:
        out: List[str] = []
        for symbol in symbols or []:
            clean = str(symbol).strip().upper()
            if not clean or clean in out:
                continue
            out.append(clean)
        return out

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _stdout_targets_events_file(self) -> bool:
        try:
            stdout_target = Path(os.readlink("/proc/self/fd/1")).resolve()
            return stdout_target == self.events_file.resolve()
        except Exception:
            return False

    def _emit_event(self, payload: Dict, persist: bool = False) -> None:
        line = json.dumps(payload)
        print(line)
        if not persist or self._stdout_targets_events_file():
            return
        try:
            self.events_file.parent.mkdir(parents=True, exist_ok=True)
            with self.events_file.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except Exception:
            pass

    def _apply_runtime_control(self) -> None:
        try:
            if not self.runtime_control_file.exists():
                return

            stats = self.runtime_control_file.stat()
            mtime_ns = int(stats.st_mtime_ns)
            if self._runtime_control_mtime_ns is not None and mtime_ns <= self._runtime_control_mtime_ns:
                return

            payload = json.loads(self.runtime_control_file.read_text(encoding="utf-8"))
            symbols = payload.get("symbols")
            if not isinstance(symbols, list):
                self._runtime_control_mtime_ns = mtime_ns
                return

            normalized = self._normalize_symbols(symbols)
            if not normalized:
                self._runtime_control_mtime_ns = mtime_ns
                return

            if normalized != self.symbols:
                old_symbols = self.symbols[:]
                self.symbols = normalized

                base_conf = float(self.strategy_payload.get("min_confidence", 0.6))
                new_conf: Dict[str, float] = {}
                for symbol in self.symbols:
                    new_conf[symbol] = self.symbol_confidence.get(symbol, base_conf)
                self.symbol_confidence = new_conf
                for symbol in self.symbols:
                    self.symbol_recent_trades.setdefault(symbol, [])
                    self.symbol_cooldowns.setdefault(symbol, 0)

                print(
                    json.dumps(
                        {
                            "type": "RUNTIME_SYMBOLS_UPDATED",
                            "time": self._now_iso(),
                            "old_symbols": old_symbols,
                            "new_symbols": self.symbols,
                        }
                    )
                )

            self._runtime_control_mtime_ns = mtime_ns
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "type": "RUNTIME_UPDATE_ERROR",
                        "time": self._now_iso(),
                        "error": str(exc),
                    }
                )
            )

    def _active_symbols(self) -> List[str]:
        return [s for s in self.symbols if int(self.symbol_cooldowns.get(s, 0)) <= 0]

    def _open_trade_symbols(self) -> set[str]:
        return {managed.signal.symbol for managed in self.open_trades.values()}

    def _decrement_cooldowns(self) -> None:
        changed: List[Dict[str, int]] = []
        for symbol in list(self.symbols):
            remaining = int(self.symbol_cooldowns.get(symbol, 0))
            if remaining <= 0:
                self.symbol_cooldowns[symbol] = 0
                continue
            updated = max(0, remaining - 1)
            self.symbol_cooldowns[symbol] = updated
            if updated == 0:
                changed.append({"symbol": symbol, "cooldown_cycles_left": 0})

        if changed:
            print(
                json.dumps(
                    {
                        "type": "SYMBOL_COOLDOWN_CLEARED",
                        "time": self._now_iso(),
                        "symbols": changed,
                    }
                )
            )
        for item in self.policy_engine.tick():
            print(
                json.dumps(
                    {
                        "type": "SETUP_SIDE_COOLDOWN_CLEARED",
                        "time": self._now_iso(),
                        "slice_key": item["slice_key"],
                    }
                )
            )

    @staticmethod
    def _stats(trades: List[ClosedTrade]) -> Dict:
        count = len(trades)
        wins = sum(1 for t in trades if t.result == "WIN")
        losses = count - wins
        win_rate = (wins / count) if count else 0.0
        expectancy_r = (sum(t.pnl_r for t in trades) / count) if count else 0.0
        return {
            "trades": count,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "expectancy_r": expectancy_r,
        }

    def _record_trade(self, trade: ClosedTrade) -> None:
        self.recent_trades.append(trade)
        max_global = max(self.target_trades * 2, self.guard_global_window * 4, 40)
        if len(self.recent_trades) > max_global:
            del self.recent_trades[:-max_global]

        bucket = self.symbol_recent_trades[trade.symbol]
        bucket.append(trade)
        max_symbol = max(self.guard_symbol_window * 3, 20)
        if len(bucket) > max_symbol:
            del bucket[:-max_symbol]

    @staticmethod
    def _utc_day_from_ms(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000.0, tz=timezone.utc).date().isoformat()

    def _current_utc_day(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _daily_realized_pnl(self, utc_day: Optional[str] = None) -> Dict[str, float | int | str]:
        day = utc_day or self._current_utc_day()
        realized = [trade for trade in self.recent_trades if self._utc_day_from_ms(trade.closed_at_ms) == day]
        pnl_r = sum(float(trade.pnl_r) for trade in realized)
        pnl_usd = sum(float(trade.pnl_usd) for trade in realized)
        return {
            "utc_day": day,
            "trades": len(realized),
            "pnl_r": round(pnl_r, 6),
            "pnl_usd": round(pnl_usd, 6),
        }

    def _apply_performance_guard(self, cycle: int) -> None:
        if not self.guard_enabled:
            return

        active_symbols = self._active_symbols()
        for symbol in list(self.symbols):
            if int(self.symbol_cooldowns.get(symbol, 0)) > 0:
                continue
            if len(active_symbols) <= self.guard_min_active_symbols:
                break

            bucket = self.symbol_recent_trades.get(symbol, [])
            stats = self._stats(bucket[-self.guard_symbol_window :])
            if stats["trades"] < self.guard_min_symbol_trades:
                continue

            bad_symbol = (
                stats["win_rate"] < self.guard_min_symbol_win_rate
                or stats["expectancy_r"] < self.guard_min_symbol_expectancy_r
            )
            if not bad_symbol:
                continue

            self.symbol_cooldowns[symbol] = max(self.guard_cooldown_cycles, int(self.symbol_cooldowns.get(symbol, 0)))
            active_symbols = self._active_symbols()
            print(
                json.dumps(
                    {
                        "type": "SYMBOL_COOLDOWN_APPLIED",
                        "time": self._now_iso(),
                        "cycle": cycle,
                        "symbol": symbol,
                        "cooldown_cycles": self.symbol_cooldowns[symbol],
                        "stats": {
                            "trades": stats["trades"],
                            "wins": stats["wins"],
                            "losses": stats["losses"],
                            "win_rate": round(stats["win_rate"], 4),
                            "expectancy_r": round(stats["expectancy_r"], 6),
                        },
                        "thresholds": {
                            "min_symbol_win_rate": self.guard_min_symbol_win_rate,
                            "min_symbol_expectancy_r": self.guard_min_symbol_expectancy_r,
                        },
                    }
                )
            )

        recent = self.recent_trades[-self.guard_global_window :]
        global_stats = self._stats(recent)
        if global_stats["trades"] < max(4, self.guard_global_window // 2):
            return

        changed = False
        if (
            global_stats["win_rate"] < self.guard_global_min_win_rate
            or global_stats["expectancy_r"] < self.guard_global_min_expectancy_r
        ):
            prev = {
                "min_candidate_confidence": self.min_candidate_confidence,
                "min_rr_floor": self.min_rr_floor,
                "min_trend_strength": self.min_trend_strength,
            }
            self.min_candidate_confidence = min(0.95, self.min_candidate_confidence + 0.01)
            self.min_rr_floor = min(0.85, self.min_rr_floor + 0.01)
            self.min_trend_strength = min(0.0045, self.min_trend_strength + 0.00003)
            changed = True
            direction = "TIGHTEN"
        elif (
            global_stats["win_rate"] >= (self.guard_global_min_win_rate + 0.12)
            and global_stats["expectancy_r"] >= (self.guard_global_min_expectancy_r + 0.05)
        ):
            prev = {
                "min_candidate_confidence": self.min_candidate_confidence,
                "min_rr_floor": self.min_rr_floor,
                "min_trend_strength": self.min_trend_strength,
            }
            self.min_candidate_confidence = max(0.65, self.min_candidate_confidence - 0.005)
            self.min_rr_floor = max(0.25, self.min_rr_floor - 0.005)
            self.min_trend_strength = max(0.0007, self.min_trend_strength - 0.00001)
            changed = True
            direction = "RELAX"

        if changed:
            print(
                json.dumps(
                    {
                        "type": "GUARD_RETUNE",
                        "time": self._now_iso(),
                        "cycle": cycle,
                        "direction": direction,
                        "recent_global_stats": {
                            "trades": global_stats["trades"],
                            "wins": global_stats["wins"],
                            "losses": global_stats["losses"],
                            "win_rate": round(global_stats["win_rate"], 4),
                            "expectancy_r": round(global_stats["expectancy_r"], 6),
                        },
                        "previous": prev,
                        "updated": {
                            "min_candidate_confidence": round(self.min_candidate_confidence, 6),
                            "min_rr_floor": round(self.min_rr_floor, 6),
                            "min_trend_strength": round(self.min_trend_strength, 6),
                        },
                    }
                )
            )

    def _refresh_batch_market_data(self) -> None:
        """Fetch premium index + ticker prices for ALL symbols in 2 batch calls."""
        try:
            self._premium_cache = self.client.fetch_all_premium_index()
        except Exception as exc:
            print(json.dumps({"type": "BATCH_PREMIUM_ERROR", "time": self._now_iso(), "error": str(exc)}))

        try:
            self._ticker_cache = self.client.fetch_all_ticker_prices()
        except Exception as exc:
            print(json.dumps({"type": "BATCH_TICKER_ERROR", "time": self._now_iso(), "error": str(exc)}))

        self._reconcile_symbol_universe()

    def _reconcile_symbol_universe(self) -> None:
        known_symbols = set(self._premium_cache) | set(self._ticker_cache)
        if not known_symbols:
            return

        removed: List[Dict[str, int]] = []
        open_trade_symbols = self._open_trade_symbols()
        for symbol in list(self.symbols):
            if symbol in known_symbols:
                self.invalid_symbol_failures[symbol] = 0
                continue

            self.invalid_symbol_failures[symbol] = int(self.invalid_symbol_failures.get(symbol, 0)) + 1
            if self.invalid_symbol_failures[symbol] < self.invalid_symbol_failure_threshold:
                continue
            if symbol in open_trade_symbols:
                continue

            self.symbols.remove(symbol)
            self.symbol_confidence.pop(symbol, None)
            self.symbol_cooldowns.pop(symbol, None)
            self.symbol_consecutive_losses.pop(symbol, None)
            removed.append({"symbol": symbol, "failures": self.invalid_symbol_failures[symbol]})

        if removed:
            print(
                json.dumps(
                    {
                        "type": "SYMBOLS_FILTERED",
                        "time": self._now_iso(),
                        "removed": removed,
                        "remaining_symbols": len(self.symbols),
                    }
                )
            )

    def _get_klines_window(self) -> List[str]:
        """Return the next window of symbols to fetch klines for (rotating)."""
        active = self._active_symbols()
        if not active:
            return []
        window_size = min(self.klines_window_size, len(active))
        start = self._klines_window_offset % len(active)
        window = active[start:start + window_size]
        if len(window) < window_size:
            window += active[:window_size - len(window)]
        self._klines_window_offset = (start + window_size) % max(len(active), 1)
        return window

    def _market_snapshot(self, symbol: str) -> Dict:
        price = self._ticker_cache.get(symbol)
        if price is not None:
            return {"symbol": symbol, "price": price, "time": 0}
        tick = self.client._get_json("/fapi/v1/ticker/price", {"symbol": symbol})
        return {
            "symbol": symbol,
            "price": float(tick["price"]),
            "time": int(tick.get("time", 0)),
        }

    def _close_orphaned_positions(self) -> None:
        """Inspect Binance positions left open from a previous crash/restart."""
        if not self.executor.enabled:
            return
        try:
            account = self.executor.get_account()
            positions = account.get("positions", [])
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                if amt == 0:
                    continue
                symbol = p["symbol"]
                side = "LONG" if amt > 0 else "SHORT"
                pnl = float(p.get("unrealizedProfit", 0))
                self._emit_event(
                    {
                        "type": "BINANCE_ORDER",
                        "time": self._now_iso(),
                        "action": "ORPHAN_DETECTED",
                        "symbol": symbol,
                        "side": side,
                        "pnl": pnl,
                        "auto_close_enabled": self.close_orphaned_positions_on_startup,
                    },
                    persist=True,
                )
                if self.close_orphaned_positions_on_startup:
                    close_result = self.executor.close_trade(symbol, side, "ORPHAN_CLEANUP")
                    close_payload = {
                        "type": "BINANCE_ORDER",
                        "time": self._now_iso(),
                        "action": "ORPHAN_CLOSE",
                        "symbol": symbol,
                        "side": side,
                        "result": close_result,
                    }
                    if close_result.get("unrealized_pnl") is not None:
                        close_payload["pnl"] = close_result.get("unrealized_pnl")
                    self._emit_event(
                        close_payload,
                        persist=True,
                    )
        except Exception as exc:
            self._emit_event(
                {
                    "type": "BINANCE_ORDER",
                    "time": self._now_iso(),
                    "action": "ORPHAN_CHECK_FAILED",
                    "error": str(exc),
                },
                persist=True,
            )

    def _signal_candidates(self) -> List[CandidateSignal]:
        candidates: List[CandidateSignal] = []
        klines_window = self._get_klines_window()
        rejection_summary: Dict[str, object] = {
            "strategy_returned_none": 0,
            "strategy_rejections": defaultdict(int),
            "rr_below_floor": 0,
            "trend_strength_below_min": 0,
            "quality_blocked": defaultdict(int),
        }

        for symbol in klines_window:
            if int(self.symbol_cooldowns.get(symbol, 0)) > 0:
                continue
            try:
                market = self._premium_cache.get(symbol)
                if market is None:
                    market = self.client.fetch_market_context(symbol)

                for timeframe in self.timeframes:
                    candles = self._closed_candles(
                        self.client.fetch_klines(symbol=symbol, interval=timeframe, limit=self.lookback)
                    )
                    if len(candles) < max(60, int(self.strategy_payload["ema_slow"])):
                        continue

                    strategy_data = copy.deepcopy(self.strategy_payload)
                    strategy_data["min_confidence"] = self.symbol_confidence.get(symbol, strategy_data["min_confidence"])
                    strategy = StrategyEngine.from_dict(strategy_data)
                    strategy_rejections = rejection_summary["strategy_rejections"]

                    signal = strategy.evaluate(symbol, timeframe, candles, market, diagnostics=strategy_rejections)
                    if signal is None:
                        rejection_summary["strategy_returned_none"] = int(rejection_summary["strategy_returned_none"]) + 1
                        continue

                    rr = abs(signal.take_profit - signal.entry) / max(abs(signal.entry - signal.stop_loss), 1e-9)
                    if rr < self.min_rr_floor:
                        rejection_summary["rr_below_floor"] = int(rejection_summary["rr_below_floor"]) + 1
                        continue

                    closes = [c.close for c in candles]
                    ema_fast_v = ema(closes, int(strategy_data["ema_fast"]))
                    ema_slow_v = ema(closes, int(strategy_data["ema_slow"]))
                    trend_strength = abs(ema_fast_v - ema_slow_v) / max(signal.entry, 1e-9)
                    if trend_strength < self.min_trend_strength:
                        rejection_summary["trend_strength_below_min"] = int(
                            rejection_summary["trend_strength_below_min"]
                        ) + 1
                        continue

                    cost_r = self.cost_model.trade_cost_r(signal.entry, signal.stop_loss)
                    expectancy_r = (signal.confidence * rr) - ((1.0 - signal.confidence) * 1.0) - cost_r
                    symbol_quality = self._symbol_quality_factor(symbol)
                    signal_type = self._signal_type_from_reason(signal.reason)
                    quality_block = self._candidate_quality_block_reason(
                        symbol=symbol,
                        market=market,
                        signal_type=signal_type,
                        trend_strength=trend_strength,
                        confidence=signal.confidence,
                        symbol_quality=symbol_quality,
                    )
                    if quality_block is not None:
                        quality_counts = rejection_summary["quality_blocked"]
                        if isinstance(quality_counts, defaultdict):
                            quality_counts[quality_block] += 1
                        continue
                    base_score = (signal.confidence * 0.65) + (trend_strength * 100.0 * 0.25) + ((rr - cost_r) * 0.10)
                    score = base_score * symbol_quality * self._signal_score_multiplier(signal_type)

                    candidates.append(
                        CandidateSignal(
                            signal=signal,
                            trend_strength=trend_strength,
                            cost_r=cost_r,
                            rr=rr,
                            expectancy_r=expectancy_r,
                            symbol_quality=symbol_quality,
                            score=score,
                        )
                    )

                    time.sleep(0.05)  # 50ms delay between klines calls to smooth burst rate
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "type": "MARKET_FETCH_ERROR",
                            "time": self._now_iso(),
                            "symbol": symbol,
                            "error": str(exc),
                        }
                    )
                )

        quality_blocked = rejection_summary["quality_blocked"]
        strategy_rejections = rejection_summary["strategy_rejections"]
        print(
            json.dumps(
                {
                    "type": "CANDIDATE_REJECTION_SUMMARY",
                    "time": self._now_iso(),
                    "window_symbols": klines_window,
                    "counts": {
                        "strategy_returned_none": int(rejection_summary["strategy_returned_none"]),
                        "strategy_rejections": (
                            dict(strategy_rejections) if isinstance(strategy_rejections, defaultdict) else {}
                        ),
                        "rr_below_floor": int(rejection_summary["rr_below_floor"]),
                        "trend_strength_below_min": int(rejection_summary["trend_strength_below_min"]),
                        "quality_blocked": dict(quality_blocked) if isinstance(quality_blocked, defaultdict) else {},
                        "candidates_emitted": len(candidates),
                    },
                }
            )
        )

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    @staticmethod
    def _closed_candles(candles: List, now_ms: Optional[int] = None) -> List:
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        return [c for c in candles if int(getattr(c, "close_time_ms", 0) or 0) < now_ms]

    def _symbol_quality_factor(self, symbol: str) -> float:
        recent = self.symbol_recent_trades.get(symbol, [])
        stats = self._stats(recent[-self.guard_symbol_window :])
        if stats["trades"] < 3:
            return 1.0

        win_rate = stats["win_rate"]
        expectancy = max(-0.2, min(0.2, stats["expectancy_r"]))
        expectancy_component = (expectancy + 0.2) / 0.4
        quality = 0.55 + (win_rate * 0.35) + (expectancy_component * 0.10)
        return max(0.45, min(1.05, quality))

    @staticmethod
    def _signal_type_from_reason(reason: str) -> str:
        upper = str(reason or "").upper()
        if "BB_REVERSION" in upper:
            return "BB_REVERSION"
        if "SUPERTREND" in upper:
            return "SUPERTREND"
        if "PULLBACK" in upper:
            return "PULLBACK"
        if "MOMENTUM" in upper:
            return "MOMENTUM"
        if "CROSSOVER" in upper:
            return "CROSSOVER"
        return "UNKNOWN"

    @staticmethod
    def _signal_regime_from_reason(reason: str) -> str:
        upper = str(reason or "").upper()
        marker = "REGIME="
        if marker not in upper:
            return "UNKNOWN"
        tail = upper.split(marker, 1)[1]
        token = tail.split("|", 1)[0].split(",", 1)[0].strip()
        return token or "UNKNOWN"

    def _signal_score_multiplier(self, signal_type: str) -> float:
        normalized = str(signal_type or "").upper()
        if normalized == "PULLBACK":
            return self.pullback_score_multiplier
        if normalized == "CROSSOVER":
            return self.crossover_score_multiplier
        if normalized == "BB_REVERSION":
            return self.bb_reversion_score_multiplier
        if normalized == "SUPERTREND":
            return self.supertrend_score_multiplier
        return 1.0

    def _candidate_quality_block_reason(
        self,
        symbol: str,
        market: MarketContext,
        signal_type: str,
        trend_strength: float,
        confidence: float,
        symbol_quality: float,
    ) -> Optional[str]:
        normalized_signal = str(signal_type or "").upper()
        if normalized_signal in self.disabled_signal_types:
            return f"signal_type_disabled:{normalized_signal.lower()}"

        if normalized_signal == "CROSSOVER":
            if trend_strength < self.crossover_min_trend_strength:
                return "weak_crossover_trend"
            if confidence < self.crossover_min_confidence:
                return "weak_crossover_confidence"

        if symbol_quality < self.min_symbol_quality_for_entry:
            return "low_symbol_quality"

        symbol_trades = self.symbol_recent_trades.get(symbol, [])
        if len(symbol_trades) >= self.min_symbol_history_for_entry:
            stats = self._stats(symbol_trades[-self.guard_symbol_window :])
            if stats["win_rate"] < self.min_symbol_win_rate_for_entry:
                return "low_symbol_win_rate"
            if stats["expectancy_r"] < self.min_symbol_expectancy_r_for_entry:
                return "low_symbol_expectancy"

        oi_notional = float(market.open_interest or 0.0) * float(market.mark_price or 0.0)
        if oi_notional < self.min_open_interest_notional_usd:
            return "low_open_interest"

        return None

    @staticmethod
    def _break_even_stop_price(side: str, entry: float, risk: float, offset_r: float) -> float:
        if str(side or "").upper() == "SHORT":
            return entry - (offset_r * risk)
        return entry + (offset_r * risk)

    @staticmethod
    def _stop_state(managed: ManagedTrade) -> str:
        if managed.trailing_stop_active:
            return "TRAILING"
        if managed.moved_to_break_even:
            return "BREAKEVEN"
        return "ORIGINAL"

    @staticmethod
    def _exit_type_from_reason(reason: str, result: str) -> str:
        upper = str(reason or "").upper()
        if "ADVERSE_CUT" in upper:
            return "ADVERSE_CUT"
        if "MOMENTUM_REVERSAL" in upper:
            return "MOMENTUM_REVERSAL"
        if "STAGNATION" in upper:
            return "STAGNATION_EXIT"
        if "TIMEOUT" in upper:
            return "TIMEOUT_EXIT"
        if "NETWORK_ERROR" in upper:
            return "NETWORK_ERROR_EXIT"
        result_upper = str(result or "").upper()
        if result_upper == "WIN":
            return "DIRECT_TP"
        if result_upper == "LOSS":
            return "DIRECT_SL"
        return "DIRECT_EXIT"

    @staticmethod
    def _hold_minutes(closed: ClosedTrade) -> Optional[float]:
        try:
            opened = int(closed.opened_at_ms)
            closed_at = int(closed.closed_at_ms)
        except (TypeError, ValueError):
            return None
        if closed_at <= opened:
            return None
        return round((closed_at - opened) / 60000.0, 4)

    def _build_trade_meta(self, managed: ManagedTrade, closed: ClosedTrade) -> Dict[str, object]:
        return {
            "signal_type": self._signal_type_from_reason(closed.reason),
            "regime": self._signal_regime_from_reason(closed.reason),
            "exit_type": self._exit_type_from_reason(closed.reason, closed.result),
            "stop_state": self._stop_state(managed),
            "hold_minutes": self._hold_minutes(closed),
        }

    @staticmethod
    def _trade_result_key(closed: ClosedTrade) -> str:
        return (
            f"{closed.symbol}|{closed.timeframe}|{closed.side}|"
            f"{closed.opened_at_ms}|{closed.closed_at_ms}|{round(float(closed.exit_price), 8)}"
        )

    def _post_close_cooldown_cycles(self, closed: ClosedTrade, trade_meta: Dict[str, object]) -> int:
        cooldown = max(0, self.reentry_cooldown_cycles)
        hold_minutes = trade_meta.get("hold_minutes")
        exit_type = str(trade_meta.get("exit_type") or "").upper()
        if isinstance(hold_minutes, (int, float)) and float(hold_minutes) <= self.fast_exit_minutes_threshold:
            cooldown = max(cooldown, self.fast_exit_reentry_cooldown_cycles)
        if exit_type in {"ADVERSE_CUT", "STAGNATION_EXIT", "MOMENTUM_REVERSAL", "TIMEOUT_EXIT"}:
            cooldown = max(cooldown, self.fast_exit_reentry_cooldown_cycles)
        return cooldown

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    def _estimate_win_probability(self, candidate: CandidateSignal) -> float:
        # Historical-calibrated estimator: blend 60% setup quality + 40% actual symbol win rate.
        conf_component = self._clamp(candidate.signal.confidence - 0.03, 0.0, 1.0)
        rr_component = self._clamp(candidate.rr / 1.8, 0.0, 1.0)
        exp_component = self._clamp((candidate.expectancy_r + 0.25) / 0.85, 0.0, 1.0)
        trend_component = self._clamp(candidate.trend_strength / 0.01, 0.0, 1.0)
        quality_component = self._clamp(candidate.symbol_quality, 0.0, 1.0)
        setup_quality = (
            (conf_component * 0.40)
            + (exp_component * 0.25)
            + (trend_component * 0.15)
            + (quality_component * 0.12)
            + (rr_component * 0.08)
        )
        # Actual symbol win rate from recent trades (default 0.5 if no history)
        symbol = candidate.signal.symbol
        symbol_trades = self.symbol_recent_trades.get(symbol, [])
        if len(symbol_trades) >= 3:
            symbol_wins = sum(1 for t in symbol_trades if t.result == "WIN")
            actual_win_rate = symbol_wins / len(symbol_trades)
        else:
            actual_win_rate = 0.5
        blended = (setup_quality * 0.60) + (actual_win_rate * 0.40)
        calibrated = (blended * 0.95) + 0.01
        return self._clamp(calibrated, 0.01, 0.99)

    @staticmethod
    def _probability_bucket(win_probability: float) -> Dict[str, str]:
        if win_probability >= 0.7:
            return {"id": "ge_70", "label": "70%+ Win-Likely"}
        if win_probability >= 0.5:
            return {"id": "between_50_69", "label": "50-69% Mixed"}
        if win_probability >= 0.3:
            return {"id": "between_30_49", "label": "30-49% Risky"}
        if win_probability >= 0.2:
            return {"id": "between_20_29", "label": "20-29% Weak"}
        return {"id": "below_20", "label": "<20% Loss-Likely"}

    def _build_probability_categories(self, trades: List[Dict]) -> Dict[str, Dict]:
        categories: Dict[str, Dict] = {
            "ge_70": {"label": "70%+ Win-Likely", "count": 0},
            "between_50_69": {"label": "50-69% Mixed", "count": 0},
            "between_30_49": {"label": "30-49% Risky", "count": 0},
            "between_20_29": {"label": "20-29% Weak", "count": 0},
            "below_20": {"label": "<20% Loss-Likely", "count": 0},
        }
        for trade in trades:
            bucket_id = str(trade.get("probability_bucket") or "")
            if bucket_id in categories:
                categories[bucket_id]["count"] += 1
        return categories

    @staticmethod
    def _timeframe_minutes(timeframe: str) -> int:
        raw = str(timeframe or "").strip().lower()
        if raw.endswith("m"):
            return max(1, int(raw[:-1] or "1"))
        if raw.endswith("h"):
            return max(1, int(raw[:-1] or "1")) * 60
        if raw.endswith("d"):
            return max(1, int(raw[:-1] or "1")) * 1440
        return 1

    def _effective_wait_minutes(self, timeframe_minutes: int) -> int:
        candle_based_wait = self.max_wait_candles * timeframe_minutes
        return max(self.max_wait_minutes_per_trade, candle_based_wait, timeframe_minutes * 2)

    def _wait_for_close(self, signal: Signal) -> ClosedTrade:
        engine = TradeEngine(risk_usd=self.risk_usd)
        opened = engine.maybe_open_trade(signal)
        if not opened:
            raise RuntimeError("Failed to open paper trade")

        start = time.time()
        timeframe_minutes = self._timeframe_minutes(signal.timeframe)
        # Never let the hard safety timeout undercut the candle budget for the trade.
        effective_wait_minutes = self._effective_wait_minutes(timeframe_minutes)
        max_wait_seconds = effective_wait_minutes * 60
        last_seen_close_ms = signal.signal_time_ms
        best_r = 0.0
        original_risk = max(abs(signal.entry - signal.stop_loss), 1e-9)
        bars_seen = 0
        consecutive_adverse_bars = 0
        moved_to_break_even = False
        trailing_stop_active = False
        consecutive_fetch_errors = 0
        last_known_candles = None

        def current_r_multiple(side: str, entry: float, stop_loss: float, price: float) -> float:
            risk = max(abs(entry - stop_loss), 1e-9)
            pnl_per_unit = price - entry if side == "LONG" else entry - price
            return pnl_per_unit / risk

        def _make_exit(active, latest, reason_prefix):
            pnl_per_unit = latest.close - active.entry if active.side == "LONG" else active.entry - latest.close
            gross_r = pnl_per_unit / original_risk
            cost_r = self.cost_model.trade_cost_r(active.entry, signal.stop_loss)
            net_r = gross_r - cost_r
            return ClosedTrade(
                symbol=active.symbol,
                timeframe=active.timeframe,
                side=active.side,
                entry=active.entry,
                take_profit=active.take_profit,
                stop_loss=active.stop_loss,
                exit_price=latest.close,
                result="WIN" if net_r > 0 else "LOSS",
                opened_at_ms=active.opened_at_ms,
                closed_at_ms=latest.close_time_ms,
                pnl_r=net_r,
                pnl_usd=net_r * self.risk_usd,
                reason=f"{reason_prefix} | {active.reason}",
            )

        while True:
            # Hard time-based safety timeout FIRST (before any API calls)
            if time.time() - start >= max_wait_seconds:
                active = engine.active_trade
                if active:
                    latest = last_known_candles[-1] if last_known_candles else None
                    if latest:
                        return _make_exit(active, latest, "TIMEOUT_EXIT")

            # Network error protection: wrap klines fetch in try/except
            try:
                candles = self.client.fetch_klines(symbol=signal.symbol, interval=signal.timeframe, limit=10)
                last_known_candles = candles
                consecutive_fetch_errors = 0
            except Exception as exc:
                consecutive_fetch_errors += 1
                print(json.dumps({
                    "type": "TRADE_MONITOR_FETCH_ERROR",
                    "time": self._now_iso(),
                    "symbol": signal.symbol,
                    "error": str(exc),
                    "consecutive_errors": consecutive_fetch_errors,
                }))
                # After 5 consecutive failures, force-close at last known price
                if consecutive_fetch_errors >= 5 and last_known_candles:
                    active = engine.active_trade
                    if active:
                        latest = last_known_candles[-1]
                        return _make_exit(active, latest, "NETWORK_ERROR_EXIT")
                time.sleep(self.poll_seconds)
                continue

            closed_candles = [c for c in candles if c.close_time_ms < int(time.time() * 1000)]
            if closed_candles:
                latest = closed_candles[-1]
                if latest.close_time_ms > last_seen_close_ms:
                    last_seen_close_ms = latest.close_time_ms

                    active = engine.active_trade
                    if active is None:
                        raise RuntimeError("Active trade missing while waiting for close")

                    bars_seen += 1
                    now_r = current_r_multiple(active.side, active.entry, signal.stop_loss, latest.close)
                    favorable_price = latest.high if active.side == "LONG" else latest.low
                    peak_r = current_r_multiple(active.side, active.entry, signal.stop_loss, favorable_price)
                    best_r = max(best_r, peak_r)

                    # Track consecutive adverse bars for momentum reversal exit
                    if now_r < 0:
                        consecutive_adverse_bars += 1
                    else:
                        consecutive_adverse_bars = 0

                    # Check TP/SL FIRST with original stop-loss, before any mutation
                    closed = engine.on_candle(latest)
                    if closed:
                        return closed

                    active = engine.active_trade
                    if active is None:
                        raise RuntimeError("Active trade missing after candle update")

                    # THEN apply trailing stop or break-even (for next candle, not this one)
                    if self.enable_trailing_stop and best_r >= self.trail_trigger_r:
                        trail_sl_r = best_r * self.trail_keep_pct
                        if active.side == "LONG":
                            new_sl = active.entry + (trail_sl_r * original_risk)
                            if new_sl > active.stop_loss:
                                active.stop_loss = new_sl
                        else:
                            new_sl = active.entry - (trail_sl_r * original_risk)
                            if new_sl < active.stop_loss:
                                active.stop_loss = new_sl

                        if not trailing_stop_active:
                            trailing_stop_active = True
                            print(
                                json.dumps(
                                    {
                                        "type": "RISK_MANAGER_UPDATE",
                                        "time": self._now_iso(),
                                        "symbol": active.symbol,
                                        "timeframe": active.timeframe,
                                        "action": "TRAILING_STOP_ACTIVATED",
                                        "updated_stop_loss": round(active.stop_loss, 6),
                                        "best_r": round(best_r, 4),
                                        "trail_keep_pct": self.trail_keep_pct,
                                    }
                                )
                            )
                        else:
                            print(
                                json.dumps(
                                    {
                                        "type": "RISK_MANAGER_UPDATE",
                                        "time": self._now_iso(),
                                        "symbol": active.symbol,
                                        "timeframe": active.timeframe,
                                        "action": "TRAILING_STOP_UPDATED",
                                        "updated_stop_loss": round(active.stop_loss, 6),
                                        "best_r": round(best_r, 4),
                                    }
                                )
                            )

                    elif self.enable_break_even and (not moved_to_break_even) and best_r >= self.break_even_trigger_r:
                        risk = max(abs(active.entry - active.stop_loss), 1e-9)
                        be_stop = self._break_even_stop_price(active.side, active.entry, risk, self.break_even_offset_r)
                        if active.side == "LONG":
                            if be_stop > active.stop_loss:
                                active.stop_loss = be_stop
                                moved_to_break_even = True
                        else:
                            if be_stop < active.stop_loss:
                                active.stop_loss = be_stop
                                moved_to_break_even = True

                        if moved_to_break_even:
                            print(
                                json.dumps(
                                    {
                                        "type": "RISK_MANAGER_UPDATE",
                                        "time": self._now_iso(),
                                        "symbol": active.symbol,
                                        "timeframe": active.timeframe,
                                        "action": "STOP_TO_BREAKEVEN",
                                        "updated_stop_loss": round(active.stop_loss, 6),
                                        "best_r": round(best_r, 6),
                                    }
                                )
                            )

                    # Use worst intra-candle price for adverse cut check (always use original risk)
                    worst_price = latest.low if active.side == "LONG" else latest.high
                    adverse_r = current_r_multiple(active.side, active.entry, signal.stop_loss, worst_price)
                    if adverse_r <= (-1.0 * self.max_adverse_r_cut):
                        return _make_exit(active, latest, "ADVERSE_CUT")

                    # Momentum reversal exit: if price goes against for N consecutive bars past threshold
                    if (consecutive_adverse_bars >= self.momentum_reversal_bars
                            and now_r <= self.momentum_reversal_r):
                        print(json.dumps({
                            "type": "RISK_MANAGER_UPDATE",
                            "time": self._now_iso(),
                            "symbol": active.symbol,
                            "timeframe": active.timeframe,
                            "action": "MOMENTUM_REVERSAL_EXIT",
                            "now_r": round(now_r, 4),
                            "consecutive_adverse_bars": consecutive_adverse_bars,
                        }))
                        return _make_exit(active, latest, "MOMENTUM_REVERSAL")

                    # Stagnation exit: no progress after N bars
                    if bars_seen >= self.max_stagnation_bars and best_r < self.min_progress_r_for_stagnation:
                        return _make_exit(active, latest, "STAGNATION_EXIT")

                    # Candle-count based timeout
                    if bars_seen >= self.max_wait_candles:
                        return _make_exit(active, latest, "CANDLE_TIMEOUT")

            time.sleep(self.poll_seconds)

    def _apply_feedback(self, trade: ClosedTrade) -> None:
        symbol = trade.symbol
        current = self.symbol_confidence.get(symbol, float(self.strategy_payload["min_confidence"]))

        if trade.result == "LOSS":
            self.symbol_confidence[symbol] = min(0.93, current + 0.015)
            self.min_trend_strength = min(0.003, self.min_trend_strength + 0.000015)
            self.min_rr_floor = min(0.75, self.min_rr_floor + 0.0025)
            self.execute_min_confidence = min(0.92, self.execute_min_confidence + 0.0015)
            self.execute_min_expectancy_r = min(0.5, self.execute_min_expectancy_r + 0.003)
            self.execute_min_score = min(0.85, self.execute_min_score + 0.0015)
        else:
            self.symbol_confidence[symbol] = max(0.50, current - 0.015)
            self.min_trend_strength = max(0.0004, self.min_trend_strength - 0.00003)
            self.execute_min_confidence = max(0.58, self.execute_min_confidence - 0.004)
            self.execute_min_expectancy_r = max(0.03, self.execute_min_expectancy_r - 0.01)
            self.execute_min_score = max(0.50, self.execute_min_score - 0.004)

    def _apply_loss_guard(self, trade: ClosedTrade, cycle: int) -> None:
        if not self.loss_guard_enabled:
            return

        symbol = trade.symbol
        if trade.result == "LOSS":
            self.global_consecutive_losses += 1
            self.symbol_consecutive_losses[symbol] = int(self.symbol_consecutive_losses.get(symbol, 0)) + 1
        else:
            self.global_consecutive_losses = 0
            self.symbol_consecutive_losses[symbol] = 0

        if trade.result != "LOSS":
            return

        symbol_streak = int(self.symbol_consecutive_losses.get(symbol, 0))
        if symbol_streak >= self.max_symbol_consecutive_losses:
            self.symbol_cooldowns[symbol] = max(self.symbol_pause_cycles, int(self.symbol_cooldowns.get(symbol, 0)))
            print(
                json.dumps(
                    {
                        "type": "LOSS_GUARD_SYMBOL_PAUSE",
                        "time": self._now_iso(),
                        "cycle": cycle,
                        "symbol": symbol,
                        "symbol_consecutive_losses": symbol_streak,
                        "cooldown_cycles": self.symbol_cooldowns[symbol],
                        "max_symbol_consecutive_losses": self.max_symbol_consecutive_losses,
                    }
                )
            )
            self.symbol_consecutive_losses[symbol] = 0

        if self.global_consecutive_losses >= self.max_global_consecutive_losses:
            before = {
                "min_candidate_confidence": self.min_candidate_confidence,
                "min_rr_floor": self.min_rr_floor,
                "min_trend_strength": self.min_trend_strength,
                "execute_min_confidence": self.execute_min_confidence,
                "execute_min_expectancy_r": self.execute_min_expectancy_r,
                "execute_min_score": self.execute_min_score,
            }
            self.global_pause_cycles_left = max(self.global_pause_cycles_left, self.global_pause_cycles)
            self.min_candidate_confidence = min(0.90, self.min_candidate_confidence + 0.005)
            self.min_rr_floor = min(0.8, self.min_rr_floor + 0.01)
            self.min_trend_strength = min(0.004, self.min_trend_strength + 0.000025)
            self.execute_min_confidence = min(0.92, self.execute_min_confidence + 0.005)
            self.execute_min_expectancy_r = min(0.5, self.execute_min_expectancy_r + 0.025)
            self.execute_min_score = min(0.88, self.execute_min_score + 0.01)
            print(
                json.dumps(
                    {
                        "type": "LOSS_GUARD_GLOBAL_PAUSE",
                        "time": self._now_iso(),
                        "cycle": cycle,
                        "global_consecutive_losses": self.global_consecutive_losses,
                        "global_pause_cycles_left": self.global_pause_cycles_left,
                        "max_global_consecutive_losses": self.max_global_consecutive_losses,
                        "before": before,
                        "after": {
                            "min_candidate_confidence": round(self.min_candidate_confidence, 6),
                            "min_rr_floor": round(self.min_rr_floor, 6),
                            "min_trend_strength": round(self.min_trend_strength, 6),
                            "execute_min_confidence": round(self.execute_min_confidence, 6),
                            "execute_min_expectancy_r": round(self.execute_min_expectancy_r, 6),
                            "execute_min_score": round(self.execute_min_score, 6),
                            "execute_min_win_probability": round(self.execute_min_win_probability, 6),
                        },
                    }
                )
            )
            self.global_consecutive_losses = 0

    def _maybe_relax_execution_filters(self, cycle: int, candidate_count: int) -> None:
        if self.relax_after_filter_blocks <= 0:
            return
        if self.no_trade_filter_block_streak < self.relax_after_filter_blocks:
            return

        before = {
            "execute_min_confidence": self.execute_min_confidence,
            "execute_min_expectancy_r": self.execute_min_expectancy_r,
            "execute_min_score": self.execute_min_score,
            "execute_min_win_probability": self.execute_min_win_probability,
        }
        self.execute_min_confidence = max(
            self.relax_min_execute_confidence, self.execute_min_confidence - self.relax_conf_step
        )
        self.execute_min_expectancy_r = max(
            self.relax_min_execute_expectancy_r, self.execute_min_expectancy_r - self.relax_expectancy_step
        )
        self.execute_min_score = max(
            self.relax_min_execute_score, self.execute_min_score - self.relax_score_step
        )
        self.execute_min_win_probability = max(0.48, self.execute_min_win_probability - 0.01)
        self.no_trade_filter_block_streak = 0
        print(
            json.dumps(
                {
                    "type": "EXECUTION_FILTER_RELAX",
                    "time": self._now_iso(),
                    "cycle": cycle,
                    "candidate_count": candidate_count,
                    "relax_after_filter_blocks": self.relax_after_filter_blocks,
                    "before": before,
                    "after": {
                        "execute_min_confidence": round(self.execute_min_confidence, 6),
                        "execute_min_expectancy_r": round(self.execute_min_expectancy_r, 6),
                        "execute_min_score": round(self.execute_min_score, 6),
                        "execute_min_win_probability": round(self.execute_min_win_probability, 6),
                    },
                }
            )
        )

    def _summary(self) -> Dict:
        trades = len(self.recent_trades)
        wins = sum(1 for t in self.recent_trades if t.result == "WIN")
        losses = trades - wins
        win_rate = (wins / trades) if trades else 0.0
        expectancy_r = (sum(t.pnl_r for t in self.recent_trades) / trades) if trades else 0.0
        symbol_health: Dict[str, Dict] = {}
        for symbol in self.symbols:
            stats = self._stats(self.symbol_recent_trades.get(symbol, [])[-self.guard_symbol_window :])
            symbol_health[symbol] = {
                "trades": stats["trades"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round(stats["win_rate"], 4),
                "expectancy_r": round(stats["expectancy_r"], 6),
                "cooldown_cycles_left": int(self.symbol_cooldowns.get(symbol, 0)),
            }

        blocked_symbols = [
            {"symbol": s, "cooldown_cycles_left": int(self.symbol_cooldowns.get(s, 0))}
            for s in self.symbols
            if int(self.symbol_cooldowns.get(s, 0)) > 0
        ]
        daily_realized = self._daily_realized_pnl()
        return {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "expectancy_r": round(expectancy_r, 6),
            "expectancy_usd_per_trade": round(expectancy_r * self.risk_usd, 6),
            "risk_usd": round(self.risk_usd, 6),
            "risk_sizing_mode": self.risk_sizing_mode,
            "starting_balance_usd": round(self.starting_balance_usd, 6),
            "risk_per_trade_pct": round(self.risk_per_trade_pct, 6),
            "paper_risk_usd": None if self.paper_risk_usd is None else round(float(self.paper_risk_usd), 6),
            "symbol_confidence": self.symbol_confidence,
            "min_rr_floor": round(self.min_rr_floor, 4),
            "min_trend_strength": round(self.min_trend_strength, 6),
            "min_candidate_confidence": round(self.min_candidate_confidence, 6),
            "execute_min_confidence": round(self.execute_min_confidence, 6),
            "execute_min_expectancy_r": round(self.execute_min_expectancy_r, 6),
            "execute_min_score": round(self.execute_min_score, 6),
            "execute_min_win_probability": round(self.execute_min_win_probability, 6),
            "filter_rejections": dict(self.filter_rejections),
            "max_open_trades": int(self.max_open_trades),
            "open_trades_count": len(self.open_trades),
            "open_trades": [
                {
                    "symbol": managed.signal.symbol,
                    "timeframe": managed.signal.timeframe,
                    "side": managed.signal.side,
                    "entry": managed.signal.entry,
                    "bars_seen": managed.bars_seen,
                    "best_r": round(managed.best_r, 6),
                }
                for managed in self.open_trades.values()
            ],
            "possible_trades_limit": int(self.possible_trades_limit),
            "max_parallel_candidates": int(self.max_parallel_candidates),
            "global_pause_cycles_left": int(self.global_pause_cycles_left),
            "global_consecutive_losses": int(self.global_consecutive_losses),
            "daily_loss_limit_r": round(self.daily_loss_limit_r, 6),
            "daily_loss_pause_day": self._daily_loss_pause_day,
            "daily_realized_pnl": daily_realized,
            "no_trade_filter_block_streak": int(self.no_trade_filter_block_streak),
            "active_symbols": self._active_symbols(),
            "blocked_symbols": blocked_symbols,
            "symbol_health": symbol_health,
            "setup_side_health": self.policy_engine.health(),
        }

    @staticmethod
    def _current_r_multiple(side: str, entry: float, stop_loss: float, price: float) -> float:
        risk = max(abs(entry - stop_loss), 1e-9)
        pnl_per_unit = price - entry if side == "LONG" else entry - price
        return pnl_per_unit / risk

    def _make_managed_trade(self, signal: Signal, binance_opened: bool) -> ManagedTrade:
        engine = TradeEngine(risk_usd=self.risk_usd)
        opened = engine.maybe_open_trade(signal)
        if not opened:
            raise RuntimeError("Failed to open paper trade")

        timeframe_minutes = self._timeframe_minutes(signal.timeframe)
        effective_wait_minutes = self._effective_wait_minutes(timeframe_minutes)
        return ManagedTrade(
            signal=signal,
            engine=engine,
            start_time=time.time(),
            timeframe_minutes=timeframe_minutes,
            max_wait_seconds=effective_wait_minutes * 60,
            last_seen_close_ms=signal.signal_time_ms,
            best_r=0.0,
            original_risk=max(abs(signal.entry - signal.stop_loss), 1e-9),
            bars_seen=0,
            consecutive_adverse_bars=0,
            moved_to_break_even=False,
            trailing_stop_active=False,
            consecutive_fetch_errors=0,
            last_known_candles=None,
            binance_opened=binance_opened,
        )

    def _make_exit(self, managed: ManagedTrade, latest, reason_prefix: str) -> ClosedTrade:
        active = managed.engine.active_trade
        if active is None:
            raise RuntimeError("Active trade missing while building exit")

        pnl_per_unit = latest.close - active.entry if active.side == "LONG" else active.entry - latest.close
        gross_r = pnl_per_unit / managed.original_risk
        cost_r = self.cost_model.trade_cost_r(active.entry, managed.signal.stop_loss)
        net_r = gross_r - cost_r
        return ClosedTrade(
            symbol=active.symbol,
            timeframe=active.timeframe,
            side=active.side,
            entry=active.entry,
            take_profit=active.take_profit,
            stop_loss=active.stop_loss,
            exit_price=latest.close,
            result="WIN" if net_r > 0 else "LOSS",
            opened_at_ms=active.opened_at_ms,
            closed_at_ms=latest.close_time_ms,
            pnl_r=net_r,
            pnl_usd=net_r * self.risk_usd,
            reason=f"{reason_prefix} | {active.reason}",
        )

    def _close_binance_trade(self, closed: ClosedTrade) -> bool:
        if not self.executor.enabled:
            return False

        binance_closed = False
        try:
            has_position = self.executor.has_open_position(closed.symbol)
            if has_position:
                close_result = self.executor.close_trade(
                    symbol=closed.symbol,
                    side=closed.side,
                    reason=closed.reason,
                )
                binance_closed = close_result.get("executed", False)
                print(
                    json.dumps({
                        "type": "BINANCE_ORDER",
                        "time": self._now_iso(),
                        "action": "CLOSE",
                        "symbol": closed.symbol,
                        "side": closed.side,
                        "result": close_result,
                    })
                )
                if not binance_closed or self.executor.has_open_position(closed.symbol):
                    time.sleep(1)
                    retry = self.executor.close_trade(closed.symbol, closed.side, "RETRY_CLOSE")
                    binance_closed = retry.get("executed", False)
                    print(
                        json.dumps({
                            "type": "BINANCE_ORDER",
                            "time": self._now_iso(),
                            "action": "RETRY_CLOSE",
                            "symbol": closed.symbol,
                            "result": retry,
                        })
                    )
        except Exception as exc:
            print(
                json.dumps({
                    "type": "BINANCE_ORDER",
                    "time": self._now_iso(),
                    "action": "CLOSE_FAILED",
                    "symbol": closed.symbol,
                    "error": str(exc),
                })
            )
        return binance_closed

    def _finalize_closed_trade(
        self,
        managed: ManagedTrade,
        closed: ClosedTrade,
        cycle: int,
        binance_opened: bool,
        binance_closed: bool,
    ) -> None:
        trade_key = self._trade_result_key(closed)
        if trade_key in self._emitted_trade_result_keys:
            print(
                json.dumps(
                    {
                        "type": "TRADE_RESULT_DUPLICATE_SKIPPED",
                        "time": self._now_iso(),
                        "cycle": cycle,
                        "symbol": closed.symbol,
                        "trade_key": trade_key,
                    }
                )
            )
            return
        self._emitted_trade_result_keys.add(trade_key)
        trade_meta = self._build_trade_meta(managed, closed)
        self._record_trade(closed)
        policy_result = self.policy_engine.record_trade(
            signal_type=str(trade_meta.get("signal_type") or ""),
            side=closed.side,
            trade=closed,
        )
        self._apply_feedback(closed)
        self._apply_loss_guard(closed, cycle)
        self._apply_performance_guard(cycle)
        if policy_result.get("paused"):
            stats = policy_result["stats"]
            print(
                json.dumps(
                    {
                        "type": "SETUP_SIDE_COOLDOWN_APPLIED",
                        "time": self._now_iso(),
                        "cycle": cycle,
                        "slice_key": policy_result["slice_key"],
                        "cooldown_cycles": self.policy_engine.slice_cooldowns.get(policy_result["slice_key"], 0),
                        "stats": {
                            "trades": stats.trades,
                            "wins": stats.wins,
                            "losses": stats.losses,
                            "win_rate": round(stats.win_rate, 4),
                            "expectancy_r": round(stats.expectancy_r, 6),
                        },
                    }
                )
            )
        cooldown_cycles = self._post_close_cooldown_cycles(closed, trade_meta)
        if cooldown_cycles > 0:
            self.symbol_cooldowns[closed.symbol] = max(int(self.symbol_cooldowns.get(closed.symbol, 0)), cooldown_cycles)
            print(
                json.dumps(
                    {
                        "type": "SYMBOL_REENTRY_COOLDOWN_APPLIED",
                        "time": self._now_iso(),
                        "cycle": cycle,
                        "symbol": closed.symbol,
                        "cooldown_cycles": self.symbol_cooldowns[closed.symbol],
                        "trade_key": trade_key,
                        "exit_type": trade_meta.get("exit_type"),
                        "hold_minutes": trade_meta.get("hold_minutes"),
                    }
                )
            )
        print(
            json.dumps(
                {
                    "type": "TRADE_RESULT",
                    "time": self._now_iso(),
                    "cycle": cycle,
                    "trade": asdict(closed),
                    "trade_key": trade_key,
                    "trade_meta": trade_meta,
                    "summary": self._summary(),
                    "binance_executed": binance_opened,
                    "binance_closed": binance_closed,
                }
            )
        )

    def _update_managed_trade(self, managed: ManagedTrade) -> Optional[ClosedTrade]:
        if time.time() - managed.start_time >= managed.max_wait_seconds and managed.last_known_candles:
            return self._make_exit(managed, managed.last_known_candles[-1], "TIMEOUT_EXIT")

        try:
            candles = self.client.fetch_klines(symbol=managed.signal.symbol, interval=managed.signal.timeframe, limit=10)
            managed.last_known_candles = candles
            managed.consecutive_fetch_errors = 0
        except Exception as exc:
            managed.consecutive_fetch_errors += 1
            print(json.dumps({
                "type": "TRADE_MONITOR_FETCH_ERROR",
                "time": self._now_iso(),
                "symbol": managed.signal.symbol,
                "error": str(exc),
                "consecutive_errors": managed.consecutive_fetch_errors,
            }))
            if managed.consecutive_fetch_errors >= 5 and managed.last_known_candles:
                return self._make_exit(managed, managed.last_known_candles[-1], "NETWORK_ERROR_EXIT")
            return None

        closed_candles = [c for c in candles if c.close_time_ms < int(time.time() * 1000)]
        if not closed_candles:
            return None

        latest = closed_candles[-1]
        if latest.close_time_ms <= managed.last_seen_close_ms:
            return None

        managed.last_seen_close_ms = latest.close_time_ms
        active = managed.engine.active_trade
        if active is None:
            raise RuntimeError("Active trade missing while updating managed trade")

        managed.bars_seen += 1
        now_r = self._current_r_multiple(active.side, active.entry, managed.signal.stop_loss, latest.close)
        favorable_price = latest.high if active.side == "LONG" else latest.low
        peak_r = self._current_r_multiple(active.side, active.entry, managed.signal.stop_loss, favorable_price)
        managed.best_r = max(managed.best_r, peak_r)

        if now_r < 0:
            managed.consecutive_adverse_bars += 1
        else:
            managed.consecutive_adverse_bars = 0

        closed = managed.engine.on_candle(latest)
        if closed:
            return closed

        active = managed.engine.active_trade
        if active is None:
            raise RuntimeError("Active trade missing after candle update")

        if self.enable_trailing_stop and managed.best_r >= self.trail_trigger_r:
            trail_sl_r = managed.best_r * self.trail_keep_pct
            if active.side == "LONG":
                new_sl = active.entry + (trail_sl_r * managed.original_risk)
                if new_sl > active.stop_loss:
                    active.stop_loss = new_sl
            else:
                new_sl = active.entry - (trail_sl_r * managed.original_risk)
                if new_sl < active.stop_loss:
                    active.stop_loss = new_sl

            action = "TRAILING_STOP_UPDATED" if managed.trailing_stop_active else "TRAILING_STOP_ACTIVATED"
            managed.trailing_stop_active = True
            payload = {
                "type": "RISK_MANAGER_UPDATE",
                "time": self._now_iso(),
                "symbol": active.symbol,
                "timeframe": active.timeframe,
                "action": action,
                "updated_stop_loss": round(active.stop_loss, 6),
                "best_r": round(managed.best_r, 4),
            }
            if action == "TRAILING_STOP_ACTIVATED":
                payload["trail_keep_pct"] = self.trail_keep_pct
            print(json.dumps(payload))

        elif self.enable_break_even and (not managed.moved_to_break_even) and managed.best_r >= self.break_even_trigger_r:
            risk = max(abs(active.entry - active.stop_loss), 1e-9)
            be_stop = self._break_even_stop_price(active.side, active.entry, risk, self.break_even_offset_r)
            if active.side == "LONG":
                if be_stop > active.stop_loss:
                    active.stop_loss = be_stop
                    managed.moved_to_break_even = True
            else:
                if be_stop < active.stop_loss:
                    active.stop_loss = be_stop
                    managed.moved_to_break_even = True

            if managed.moved_to_break_even:
                print(
                    json.dumps(
                        {
                            "type": "RISK_MANAGER_UPDATE",
                            "time": self._now_iso(),
                            "symbol": active.symbol,
                            "timeframe": active.timeframe,
                            "action": "STOP_TO_BREAKEVEN",
                            "updated_stop_loss": round(active.stop_loss, 6),
                            "best_r": round(managed.best_r, 6),
                        }
                    )
                )

        worst_price = latest.low if active.side == "LONG" else latest.high
        adverse_r = self._current_r_multiple(active.side, active.entry, managed.signal.stop_loss, worst_price)
        if adverse_r <= (-1.0 * self.max_adverse_r_cut):
            return self._make_exit(managed, latest, "ADVERSE_CUT")

        if (
            managed.consecutive_adverse_bars >= self.momentum_reversal_bars
            and now_r <= self.momentum_reversal_r
        ):
            print(json.dumps({
                "type": "RISK_MANAGER_UPDATE",
                "time": self._now_iso(),
                "symbol": active.symbol,
                "timeframe": active.timeframe,
                "action": "MOMENTUM_REVERSAL_EXIT",
                "now_r": round(now_r, 4),
                "consecutive_adverse_bars": managed.consecutive_adverse_bars,
            }))
            return self._make_exit(managed, latest, "MOMENTUM_REVERSAL")

        if managed.bars_seen >= self.max_stagnation_bars and managed.best_r < self.min_progress_r_for_stagnation:
            return self._make_exit(managed, latest, "STAGNATION_EXIT")

        if managed.bars_seen >= self.max_wait_candles:
            return self._make_exit(managed, latest, "CANDLE_TIMEOUT")

        return None

    def _update_open_trades(self, cycle: int) -> None:
        for key, managed in list(self.open_trades.items()):
            closed = self._update_managed_trade(managed)
            if closed is None:
                continue
            binance_closed = self._close_binance_trade(closed)
            del self.open_trades[key]
            self._finalize_closed_trade(managed, closed, cycle, managed.binance_opened, binance_closed)

    def _close_all_open_trades_on_exit(self, cycle: int) -> None:
        """Gracefully close all open Binance positions before the bot exits."""
        if not self.open_trades:
            return
        print(
            json.dumps(
                {
                    "type": "GRACEFUL_SHUTDOWN",
                    "time": self._now_iso(),
                    "cycle": cycle,
                    "open_trades_count": len(self.open_trades),
                    "symbols": [m.signal.symbol for m in self.open_trades.values()],
                }
            )
        )
        for key, managed in list(self.open_trades.items()):
            if not managed.binance_opened:
                del self.open_trades[key]
                continue
            try:
                close_result = self.executor.close_trade(
                    symbol=managed.signal.symbol,
                    side=managed.signal.side,
                    reason="GRACEFUL_SHUTDOWN",
                )
                self._emit_event(
                    {
                        "type": "BINANCE_ORDER",
                        "time": self._now_iso(),
                        "action": "SHUTDOWN_CLOSE",
                        "symbol": managed.signal.symbol,
                        "side": managed.signal.side,
                        "result": close_result,
                    },
                    persist=True,
                )
            except Exception as exc:
                self._emit_event(
                    {
                        "type": "BINANCE_ORDER",
                        "time": self._now_iso(),
                        "action": "SHUTDOWN_CLOSE_FAILED",
                        "symbol": managed.signal.symbol,
                        "error": str(exc),
                    },
                    persist=True,
                )
            del self.open_trades[key]

    def run(self) -> Dict:
        cycles = 0

        while cycles < self.max_cycles:
            self._apply_runtime_control()
            self._decrement_cooldowns()
            cycles += 1
            current_day = self._current_utc_day()

            if self._daily_loss_pause_day is not None and self._daily_loss_pause_day != current_day:
                print(
                    json.dumps(
                        {
                            "type": "DAILY_LOSS_LIMIT_CLEARED",
                            "time": self._now_iso(),
                            "cycle": cycles,
                            "previous_day": self._daily_loss_pause_day,
                            "current_day": current_day,
                        }
                    )
                )
                self._daily_loss_pause_day = None

            self._refresh_batch_market_data()
            snapshots = []
            for symbol in self.symbols:
                price = self._ticker_cache.get(symbol)
                if price is not None:
                    snapshots.append({"symbol": symbol, "price": price, "time": 0})
            print(json.dumps({"type": "LIVE_MARKET", "time": self._now_iso(), "snapshots": snapshots}))

            self._update_open_trades(cycles)

            daily_realized = self._daily_realized_pnl(current_day)
            if (
                self.daily_loss_limit_r > 0
                and self._daily_loss_pause_day is None
                and float(daily_realized["pnl_r"]) <= (-1.0 * self.daily_loss_limit_r)
            ):
                self._daily_loss_pause_day = current_day
                print(
                    json.dumps(
                        {
                            "type": "DAILY_LOSS_LIMIT_PAUSE",
                            "time": self._now_iso(),
                            "cycle": cycles,
                            "utc_day": current_day,
                            "daily_loss_limit_r": round(self.daily_loss_limit_r, 6),
                            "daily_realized_pnl_r": daily_realized["pnl_r"],
                            "daily_realized_pnl_usd": daily_realized["pnl_usd"],
                        }
                    )
                )

            if self._daily_loss_pause_day == current_day:
                self.no_trade_filter_block_streak = 0
                print(
                    json.dumps(
                        {
                            "type": "NO_SIGNAL",
                            "time": self._now_iso(),
                            "cycle": cycles,
                            "reason": "DAILY_LOSS_LIMIT_PAUSED",
                            "utc_day": current_day,
                            "daily_loss_limit_r": round(self.daily_loss_limit_r, 6),
                            "daily_realized_pnl_r": daily_realized["pnl_r"],
                            "daily_realized_pnl_usd": daily_realized["pnl_usd"],
                        }
                    )
                )
                time.sleep(self.poll_seconds)
                continue

            candidates = self._signal_candidates()
            candidate_win_prob = {id(c): self._estimate_win_probability(c) for c in candidates}
            possible_trades = []
            execution_candidates: List[CandidateSignal] = []
            candidate_rejections: Dict[str, int] = defaultdict(int)
            for candidate in candidates:
                if candidate.signal.confidence < self.min_candidate_confidence:
                    candidate_rejections["candidate_confidence"] += 1
                    continue
                if candidate.expectancy_r < self.min_candidate_expectancy_r:
                    candidate_rejections["candidate_expectancy"] += 1
                    continue
                execution_candidates.append(candidate)
                win_probability = candidate_win_prob.get(id(candidate), self._estimate_win_probability(candidate))
                bucket = self._probability_bucket(win_probability)
                possible_trades.append(
                    {
                        "symbol": candidate.signal.symbol,
                        "timeframe": candidate.signal.timeframe,
                        "side": candidate.signal.side,
                        "entry": candidate.signal.entry,
                        "take_profit": candidate.signal.take_profit,
                        "stop_loss": candidate.signal.stop_loss,
                        "confidence": round(candidate.signal.confidence, 6),
                        "trend_strength": round(candidate.trend_strength, 6),
                        "rr": round(candidate.rr, 6),
                        "expectancy_r": round(candidate.expectancy_r, 6),
                        "score": round(candidate.score, 6),
                        "symbol_quality": round(candidate.symbol_quality, 6),
                        "win_probability": round(win_probability, 6),
                        "probability_bucket": bucket["id"],
                        "probability_bucket_label": bucket["label"],
                        "loss_likely": bool(win_probability < 0.5),
                        "reason": candidate.signal.reason,
                    }
                )
                if len(possible_trades) >= self.possible_trades_limit:
                    break

            probability_categories = self._build_probability_categories(possible_trades)
            print(
                json.dumps(
                    {
                        "type": "POSSIBLE_TRADES",
                        "time": self._now_iso(),
                        "cycle": cycles,
                        "min_candidate_confidence": self.min_candidate_confidence,
                        "min_candidate_expectancy_r": self.min_candidate_expectancy_r,
                        "max_parallel_candidates": self.max_parallel_candidates,
                        "possible_trades_limit": self.possible_trades_limit,
                        "total_candidates_seen": len(candidates),
                        "total_possible_trades": len(possible_trades),
                        "candidate_rejections": dict(candidate_rejections),
                        "probability_categories": probability_categories,
                        "blocked_symbols": [
                            {"symbol": s, "cooldown_cycles_left": int(self.symbol_cooldowns.get(s, 0))}
                            for s in self.symbols
                            if int(self.symbol_cooldowns.get(s, 0)) > 0
                        ],
                        "trades": possible_trades,
                    }
                )
            )

            if self.global_pause_cycles_left > 0:
                print(
                    json.dumps(
                        {
                            "type": "NO_SIGNAL",
                            "time": self._now_iso(),
                            "cycle": cycles,
                            "reason": "GLOBAL_RISK_OFF",
                            "global_pause_cycles_left": self.global_pause_cycles_left,
                        }
                    )
                )
                self.global_pause_cycles_left = max(0, self.global_pause_cycles_left - 1)
                if self.global_pause_cycles_left == 0:
                    print(
                        json.dumps(
                            {
                                "type": "GLOBAL_RISK_OFF_CLEARED",
                                "time": self._now_iso(),
                                "cycle": cycles,
                            }
                        )
                    )
                time.sleep(self.poll_seconds)
                continue

            if not candidates:
                self.no_trade_filter_block_streak = 0
                print(json.dumps({"type": "NO_SIGNAL", "time": self._now_iso(), "cycle": cycles, "reason": "NO_CANDIDATES"}))
                time.sleep(self.poll_seconds)
                continue

            if not execution_candidates:
                self.no_trade_filter_block_streak = 0
                print(json.dumps({"type": "NO_SIGNAL", "time": self._now_iso(), "cycle": cycles, "reason": "NO_CANDIDATES"}))
                time.sleep(self.poll_seconds)
                continue

            confirmations: Dict[tuple[str, str], set[str]] = {}
            for candidate in execution_candidates:
                key = (candidate.signal.symbol, candidate.signal.side)
                confirmations.setdefault(key, set()).add(candidate.signal.timeframe)

            qualified: List[CandidateSignal] = []
            execution_rejections: Dict[str, int] = defaultdict(int)
            open_trade_symbols = self._open_trade_symbols()
            for candidate in execution_candidates:
                signal_type = self._signal_type_from_reason(candidate.signal.reason)
                is_crossover = signal_type == "CROSSOVER"
                min_confidence = self.crossover_execute_min_confidence if is_crossover else self.execute_min_confidence
                min_expectancy_r = (
                    self.crossover_execute_min_expectancy_r if is_crossover else self.execute_min_expectancy_r
                )
                min_score = self.crossover_execute_min_score if is_crossover else self.execute_min_score

                if candidate.signal.confidence < min_confidence:
                    execution_rejections["execute_confidence"] += 1
                    if is_crossover:
                        execution_rejections["execute_crossover_confidence"] += 1
                    continue
                if candidate.expectancy_r < min_expectancy_r:
                    execution_rejections["execute_expectancy"] += 1
                    if is_crossover:
                        execution_rejections["execute_crossover_expectancy"] += 1
                    continue
                if candidate.score < min_score:
                    execution_rejections["execute_score"] += 1
                    if is_crossover:
                        execution_rejections["execute_crossover_score"] += 1
                    continue
                win_probability = candidate_win_prob.get(id(candidate), self._estimate_win_probability(candidate))
                min_win_probability = (
                    self.crossover_execute_min_win_probability if is_crossover else self.execute_min_win_probability
                )
                if win_probability < min_win_probability:
                    execution_rejections["execute_win_probability"] += 1
                    if is_crossover:
                        execution_rejections["execute_crossover_win_probability"] += 1
                    continue
                if self.require_dual_timeframe_confirm:
                    if len(confirmations.get((candidate.signal.symbol, candidate.signal.side), set())) < 2:
                        execution_rejections["execute_dual_timeframe_confirm"] += 1
                        continue
                if candidate.signal.timeframe not in self.execute_timeframes:
                    execution_rejections["execute_timeframe_not_allowed"] += 1
                    continue
                candidate_regime = self._signal_regime_from_reason(candidate.signal.reason)
                if self.allowed_execution_regimes and candidate_regime not in self.allowed_execution_regimes:
                    execution_rejections["execute_regime_not_allowed"] += 1
                    continue
                if candidate.signal.symbol in open_trade_symbols:
                    execution_rejections["execute_symbol_already_open"] += 1
                    continue
                policy_decision = self.policy_engine.evaluate_candidate(signal_type, candidate.signal.side)
                if not policy_decision.allowed:
                    execution_rejections["policy_setup_side_paused"] += 1
                    continue
                qualified.append(candidate)

            for key, count in candidate_rejections.items():
                self.filter_rejections[key] += count
            for key, count in execution_rejections.items():
                self.filter_rejections[key] += count

            if not qualified:
                self.no_trade_filter_block_streak += 1
                print(
                    json.dumps(
                        {
                            "type": "NO_SIGNAL",
                            "time": self._now_iso(),
                            "cycle": cycles,
                            "reason": "EXECUTION_FILTER_BLOCK",
                            "candidate_count": len(execution_candidates),
                            "execute_min_confidence": self.execute_min_confidence,
                            "execute_min_expectancy_r": self.execute_min_expectancy_r,
                            "execute_min_score": self.execute_min_score,
                            "execute_min_win_probability": self.execute_min_win_probability,
                            "execution_rejections": dict(execution_rejections),
                            "no_trade_filter_block_streak": self.no_trade_filter_block_streak,
                        }
                    )
                )
                self._maybe_relax_execution_filters(cycles, len(execution_candidates))
                time.sleep(self.poll_seconds)
                continue

            available_slots = max(0, self.max_open_trades - len(self.open_trades))
            if available_slots <= 0:
                self.no_trade_filter_block_streak = 0
                print(
                    json.dumps(
                        {
                            "type": "NO_SIGNAL",
                            "time": self._now_iso(),
                            "cycle": cycles,
                            "reason": "MAX_OPEN_TRADES_REACHED",
                            "max_open_trades": self.max_open_trades,
                            "open_trades_count": len(self.open_trades),
                        }
                    )
                )
                time.sleep(self.poll_seconds)
                continue

            if len(qualified) > 1 and (qualified[0].score - qualified[1].score) < self.min_score_gap:
                self.no_trade_filter_block_streak = 0
                print(
                    json.dumps(
                        {
                            "type": "NO_SIGNAL",
                            "time": self._now_iso(),
                            "cycle": cycles,
                            "reason": "LOW_SCORE_SEPARATION",
                            "top_score": round(qualified[0].score, 6),
                            "second_score": round(qualified[1].score, 6),
                            "min_score_gap": round(self.min_score_gap, 6),
                        }
                    )
                )
                time.sleep(self.poll_seconds)
                continue

            self.no_trade_filter_block_streak = 0
            # Count open trade directions for diversity limit
            open_long_count = sum(1 for m in self.open_trades.values() if m.signal.side == "LONG")
            open_short_count = sum(1 for m in self.open_trades.values() if m.signal.side == "SHORT")

            selected_candidates: List[CandidateSignal] = []
            seen_symbols = set(self._open_trade_symbols())
            directional_limit_hits = {"LONG": 0, "SHORT": 0}
            for candidate in qualified:
                if candidate.signal.symbol in seen_symbols:
                    continue
                # Directional diversity check
                if candidate.signal.side == "LONG" and open_long_count >= self.max_same_direction_trades:
                    directional_limit_hits["LONG"] += 1
                    continue
                if candidate.signal.side == "SHORT" and open_short_count >= self.max_same_direction_trades:
                    directional_limit_hits["SHORT"] += 1
                    continue
                selected_candidates.append(candidate)
                seen_symbols.add(candidate.signal.symbol)
                if candidate.signal.side == "LONG":
                    open_long_count += 1
                else:
                    open_short_count += 1
                if len(selected_candidates) >= min(self.top_n, available_slots):
                    break

            if not selected_candidates:
                blocked_by_direction = directional_limit_hits["LONG"] > 0 or directional_limit_hits["SHORT"] > 0
                print(
                    json.dumps(
                        {
                            "type": "NO_SIGNAL",
                            "time": self._now_iso(),
                            "cycle": cycles,
                            "reason": (
                                "DIRECTIONAL_EXPOSURE_LIMIT"
                                if blocked_by_direction
                                else "ALL_QUALIFIED_SYMBOLS_ALREADY_OPEN"
                            ),
                            "open_trade_symbols": sorted(self._open_trade_symbols()),
                            "max_same_direction_trades": self.max_same_direction_trades,
                            "directional_limit_hits": directional_limit_hits,
                            "open_long_count": open_long_count,
                            "open_short_count": open_short_count,
                        }
                    )
                )
                time.sleep(self.poll_seconds)
                continue

            for selected in selected_candidates:
                selected_win_probability = candidate_win_prob.get(id(selected), self._estimate_win_probability(selected))
                selected_bucket = self._probability_bucket(selected_win_probability)
                play_trade_alert(self.enable_sound)
                print(
                    json.dumps(
                        {
                            "type": "OPEN_TRADE",
                            "time": self._now_iso(),
                            "cycle": cycles,
                            "symbol": selected.signal.symbol,
                            "timeframe": selected.signal.timeframe,
                            "side": selected.signal.side,
                            "entry": selected.signal.entry,
                            "take_profit": selected.signal.take_profit,
                            "stop_loss": selected.signal.stop_loss,
                            "confidence": selected.signal.confidence,
                            "trend_strength": round(selected.trend_strength, 6),
                            "cost_r": round(selected.cost_r, 6),
                            "score": round(selected.score, 6),
                            "symbol_quality": round(selected.symbol_quality, 6),
                            "win_probability": round(selected_win_probability, 6),
                            "probability_bucket": selected_bucket["id"],
                            "probability_bucket_label": selected_bucket["label"],
                            "reason": selected.signal.reason,
                        }
                    )
                )

                binance_opened = False
                if self.executor.enabled:
                    try:
                        exec_result = self.executor.open_trade(
                            symbol=selected.signal.symbol,
                            side=selected.signal.side,
                            entry_price=selected.signal.entry,
                            stop_loss=selected.signal.stop_loss,
                            take_profit=selected.signal.take_profit,
                        )
                        binance_opened = exec_result.get("executed", False)
                        print(
                            json.dumps({
                                "type": "BINANCE_ORDER",
                                "time": self._now_iso(),
                                "action": "OPEN",
                                "symbol": selected.signal.symbol,
                                "side": selected.signal.side,
                                "result": exec_result,
                            })
                        )
                        if not binance_opened:
                            continue
                    except Exception as exc:
                        print(
                            json.dumps({
                                "type": "BINANCE_ORDER",
                                "time": self._now_iso(),
                                "action": "OPEN_FAILED",
                                "symbol": selected.signal.symbol,
                                "error": str(exc),
                            })
                        )

                key = f"{selected.signal.symbol}:{selected.signal.timeframe}:{selected.signal.side}"
                self.open_trades[key] = self._make_managed_trade(selected.signal, binance_opened)

            summary = self._summary()
            if (
                summary["trades"] >= self.min_trades_for_success
                and summary["trades"] >= self.target_trades
                and summary["win_rate"] >= self.target_win_rate
            ):
                self._close_all_open_trades_on_exit(cycles)
                return {
                    "status": "TARGET_REACHED",
                    "cycles": cycles,
                    "summary": summary,
                }

            if summary["trades"] >= self.target_trades:
                self._close_all_open_trades_on_exit(cycles)
                return {
                    "status": "TARGET_NOT_REACHED",
                    "cycles": cycles,
                    "summary": summary,
                }

        self._close_all_open_trades_on_exit(cycles)
        return {
            "status": "MAX_CYCLES_REACHED",
            "cycles": cycles,
            "summary": self._summary(),
        }
