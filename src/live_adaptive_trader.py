from __future__ import annotations

import copy
import json
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


class LiveAdaptivePaperTrader:
    def __init__(self, config: Dict):
        self.config = config
        ds = config.get("data_source", {})
        self.client = BinanceFuturesRestClient(
            allow_mock_fallback=bool(ds.get("allow_mock_fallback", False)),
            force_mock=bool(ds.get("force_mock", False)),
            mock_seed=int(ds.get("mock_seed", 42)),
        )

        # Binance order executor (demo or live)
        self.executor = BinanceExecutor.from_env(config)
        self._close_orphaned_positions()

        self.strategy_payload = copy.deepcopy(config["strategy"])
        self.base_strategy = StrategyEngine.from_dict(copy.deepcopy(config["strategy"]))

        acct = config["account"]
        self.risk_usd = float(acct["starting_balance_usd"]) * float(acct["risk_per_trade_pct"])

        execution_cfg = config.get("execution", {})
        self.cost_model = MLWalkForwardOptimizer(
            risk_usd=self.risk_usd,
            fee_bps_per_side=float(execution_cfg.get("fee_bps_per_side", 0.0)),
            slippage_bps_per_side=float(execution_cfg.get("slippage_bps_per_side", 0.0)),
        )

        live_cfg = config.get("live_loop", {})
        self.symbols = self._normalize_symbols(live_cfg.get("symbols", []))
        self.timeframes = live_cfg.get("timeframes", ["1m", "5m", "15m"])
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
        self.enable_sound = bool(config.get("scanner", {}).get("enable_sound", True))
        self.enable_break_even = bool(live_cfg.get("enable_break_even", True))
        self.break_even_trigger_r = float(live_cfg.get("break_even_trigger_r", 0.5))
        self.break_even_offset_r = float(live_cfg.get("break_even_offset_r", 0.02))
        self.enable_trailing_stop = bool(live_cfg.get("enable_trailing_stop", True))
        self.trail_trigger_r = float(live_cfg.get("trail_trigger_r", 0.2))
        self.trail_keep_pct = float(live_cfg.get("trail_keep_pct", 0.7))
        self.max_adverse_r_cut = float(live_cfg.get("max_adverse_r_cut", 0.9))
        self.max_wait_candles = int(live_cfg.get("max_wait_candles", 12))
        self.max_stagnation_bars = int(live_cfg.get("max_stagnation_bars", 6))
        self.min_progress_r_for_stagnation = float(live_cfg.get("min_progress_r_for_stagnation", 0.10))
        self.momentum_reversal_bars = int(live_cfg.get("momentum_reversal_bars", 3))
        self.momentum_reversal_r = float(live_cfg.get("momentum_reversal_r", -0.4))
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

        # Rotating klines window: scan a subset of symbols each cycle
        self.klines_window_size = int(live_cfg.get("klines_window_size", 20))
        self._klines_window_offset = 0

        # Batch market data caches (refreshed each cycle via batch endpoints)
        self._premium_cache: Dict[str, "MarketContext"] = {}
        self._ticker_cache: Dict[str, float] = {}

        cfg_path = config.get("_config_path")
        root_dir = Path(cfg_path).resolve().parent if cfg_path else Path.cwd()
        runtime_control_path = live_cfg.get("runtime_control_file", "data/runtime_control.json")
        runtime_path = Path(runtime_control_path)
        if not runtime_path.is_absolute():
            runtime_path = (root_dir / runtime_path).resolve()
        self.runtime_control_file = runtime_path
        self._runtime_control_mtime_ns: Optional[int] = None

        base_conf = float(self.strategy_payload.get("min_confidence", 0.6))
        self.symbol_confidence: Dict[str, float] = {s: base_conf for s in self.symbols}
        self.recent_trades: List[ClosedTrade] = []
        self.symbol_recent_trades: Dict[str, List[ClosedTrade]] = defaultdict(list)
        self.symbol_cooldowns: Dict[str, int] = {}
        self.symbol_consecutive_losses: Dict[str, int] = defaultdict(int)
        self.global_consecutive_losses = 0
        self.global_pause_cycles_left = 0
        self.no_trade_filter_block_streak = 0

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
        """Close any Binance positions left open from a previous crash/restart."""
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
                print(
                    json.dumps({
                        "type": "BINANCE_ORDER",
                        "time": self._now_iso(),
                        "action": "ORPHAN_CLOSE",
                        "symbol": symbol,
                        "side": side,
                        "pnl": pnl,
                    })
                )
                self.executor.close_trade(symbol, side, "ORPHAN_CLEANUP")
        except Exception as exc:
            print(
                json.dumps({
                    "type": "BINANCE_ORDER",
                    "time": self._now_iso(),
                    "action": "ORPHAN_CHECK_FAILED",
                    "error": str(exc),
                })
            )

    def _signal_candidates(self) -> List[CandidateSignal]:
        candidates: List[CandidateSignal] = []
        klines_window = self._get_klines_window()

        for symbol in klines_window:
            if int(self.symbol_cooldowns.get(symbol, 0)) > 0:
                continue
            try:
                market = self._premium_cache.get(symbol)
                if market is None:
                    market = self.client.fetch_market_context(symbol)

                for timeframe in self.timeframes:
                    candles = self.client.fetch_klines(symbol=symbol, interval=timeframe, limit=self.lookback)
                    if len(candles) < max(60, int(self.strategy_payload["ema_slow"])):
                        continue

                    strategy_data = copy.deepcopy(self.strategy_payload)
                    strategy_data["min_confidence"] = self.symbol_confidence.get(symbol, strategy_data["min_confidence"])
                    strategy = StrategyEngine.from_dict(strategy_data)

                    signal = strategy.evaluate(symbol, timeframe, candles, market)
                    if signal is None:
                        continue

                    rr = abs(signal.take_profit - signal.entry) / max(abs(signal.entry - signal.stop_loss), 1e-9)
                    if rr < self.min_rr_floor:
                        continue

                    closes = [c.close for c in candles]
                    ema_fast_v = ema(closes, int(strategy_data["ema_fast"]))
                    ema_slow_v = ema(closes, int(strategy_data["ema_slow"]))
                    trend_strength = abs(ema_fast_v - ema_slow_v) / max(signal.entry, 1e-9)
                    if trend_strength < self.min_trend_strength:
                        continue

                    cost_r = self.cost_model.trade_cost_r(signal.entry, signal.stop_loss)
                    expectancy_r = (signal.confidence * rr) - ((1.0 - signal.confidence) * 1.0) - cost_r
                    symbol_quality = self._symbol_quality_factor(symbol)
                    base_score = (signal.confidence * 0.65) + (trend_strength * 100.0 * 0.25) + ((rr - cost_r) * 0.10)
                    score = base_score * symbol_quality

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

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

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
        calibrated = (blended * 0.92) + 0.02
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

    def _wait_for_close(self, signal: Signal) -> ClosedTrade:
        engine = TradeEngine(risk_usd=self.risk_usd)
        opened = engine.maybe_open_trade(signal)
        if not opened:
            raise RuntimeError("Failed to open paper trade")

        start = time.time()
        timeframe_minutes = self._timeframe_minutes(signal.timeframe)
        # Use candle-based timeout: max_wait_candles * timeframe, capped by max_wait_minutes
        candle_based_wait = self.max_wait_candles * timeframe_minutes
        effective_wait_minutes = min(self.max_wait_minutes_per_trade, max(candle_based_wait, timeframe_minutes * 2))
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
                        if active.side == "LONG":
                            be_stop = active.entry + (self.break_even_offset_r * risk)
                            if be_stop > active.stop_loss:
                                active.stop_loss = be_stop
                                moved_to_break_even = True
                        else:
                            be_stop = active.entry + (self.break_even_offset_r * risk)
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

            # Hard time-based safety timeout (fallback)
            if time.time() - start >= max_wait_seconds:
                now_ms = int(time.time() * 1000)
                completed = [c for c in candles if c.close_time_ms < now_ms]
                latest = completed[-1] if completed else candles[-1]
                active = engine.active_trade
                if active is None:
                    raise RuntimeError("Active trade missing during timeout close")
                return _make_exit(active, latest, "TIMEOUT_EXIT")

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
        self.execute_min_win_probability = max(0.62, self.execute_min_win_probability - 0.01)
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
        return {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 4),
            "expectancy_r": round(expectancy_r, 6),
            "expectancy_usd_per_trade": round(expectancy_r * self.risk_usd, 6),
            "symbol_confidence": self.symbol_confidence,
            "min_rr_floor": round(self.min_rr_floor, 4),
            "min_trend_strength": round(self.min_trend_strength, 6),
            "min_candidate_confidence": round(self.min_candidate_confidence, 6),
            "execute_min_confidence": round(self.execute_min_confidence, 6),
            "execute_min_expectancy_r": round(self.execute_min_expectancy_r, 6),
            "execute_min_score": round(self.execute_min_score, 6),
            "execute_min_win_probability": round(self.execute_min_win_probability, 6),
            "possible_trades_limit": int(self.possible_trades_limit),
            "max_parallel_candidates": int(self.max_parallel_candidates),
            "global_pause_cycles_left": int(self.global_pause_cycles_left),
            "global_consecutive_losses": int(self.global_consecutive_losses),
            "no_trade_filter_block_streak": int(self.no_trade_filter_block_streak),
            "active_symbols": self._active_symbols(),
            "blocked_symbols": blocked_symbols,
            "symbol_health": symbol_health,
        }

    def run(self) -> Dict:
        cycles = 0

        while cycles < self.max_cycles:
            self._apply_runtime_control()
            self._decrement_cooldowns()
            cycles += 1

            self._refresh_batch_market_data()
            snapshots = []
            for symbol in self.symbols:
                price = self._ticker_cache.get(symbol)
                if price is not None:
                    snapshots.append({"symbol": symbol, "price": price, "time": 0})
            print(json.dumps({"type": "LIVE_MARKET", "time": self._now_iso(), "snapshots": snapshots}))

            candidates = self._signal_candidates()
            candidate_win_prob = {id(c): self._estimate_win_probability(c) for c in candidates}
            possible_trades = []
            for candidate in candidates:
                if candidate.signal.confidence < self.min_candidate_confidence:
                    continue
                if candidate.expectancy_r < self.min_candidate_expectancy_r:
                    continue
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

            confirmations: Dict[tuple[str, str], set[str]] = {}
            for candidate in candidates:
                key = (candidate.signal.symbol, candidate.signal.side)
                confirmations.setdefault(key, set()).add(candidate.signal.timeframe)

            qualified: List[CandidateSignal] = []
            for candidate in candidates:
                if candidate.signal.confidence < self.execute_min_confidence:
                    continue
                if candidate.expectancy_r < self.execute_min_expectancy_r:
                    continue
                if candidate.score < self.execute_min_score:
                    continue
                win_probability = candidate_win_prob.get(id(candidate), self._estimate_win_probability(candidate))
                if win_probability < self.execute_min_win_probability:
                    continue
                if self.require_dual_timeframe_confirm:
                    if len(confirmations.get((candidate.signal.symbol, candidate.signal.side), set())) < 2:
                        continue
                qualified.append(candidate)

            if not qualified:
                self.no_trade_filter_block_streak += 1
                print(
                    json.dumps(
                        {
                            "type": "NO_SIGNAL",
                            "time": self._now_iso(),
                            "cycle": cycles,
                            "reason": "EXECUTION_FILTER_BLOCK",
                            "candidate_count": len(candidates),
                            "execute_min_confidence": self.execute_min_confidence,
                            "execute_min_expectancy_r": self.execute_min_expectancy_r,
                            "execute_min_score": self.execute_min_score,
                            "execute_min_win_probability": self.execute_min_win_probability,
                            "no_trade_filter_block_streak": self.no_trade_filter_block_streak,
                        }
                    )
                )
                self._maybe_relax_execution_filters(cycles, len(candidates))
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

            selected = qualified[: self.top_n][0]
            selected_win_probability = candidate_win_prob.get(id(selected), self._estimate_win_probability(selected))
            selected_bucket = self._probability_bucket(selected_win_probability)
            self.no_trade_filter_block_streak = 0
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

            # Execute on Binance (demo or live)
            binance_opened = False
            binance_closed = False
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

            closed = self._wait_for_close(selected.signal)

            # Always try to close on Binance — check for position regardless of open result
            if self.executor.enabled:
                try:
                    # Check if there's actually a position to close
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
                        # Verify position is actually closed
                        if not binance_closed or self.executor.has_open_position(closed.symbol):
                            # Force close with retry
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

            self._record_trade(closed)
            self._apply_feedback(closed)
            self._apply_loss_guard(closed, cycles)
            self._apply_performance_guard(cycles)

            print(
                json.dumps(
                    {
                        "type": "TRADE_RESULT",
                        "time": self._now_iso(),
                        "cycle": cycles,
                        "trade": asdict(closed),
                        "summary": self._summary(),
                        "binance_executed": binance_opened,
                        "binance_closed": binance_closed,
                    }
                )
            )

            summary = self._summary()
            if (
                summary["trades"] >= self.min_trades_for_success
                and summary["trades"] >= self.target_trades
                and summary["win_rate"] >= self.target_win_rate
            ):
                return {
                    "status": "TARGET_REACHED",
                    "cycles": cycles,
                    "summary": summary,
                }

            if summary["trades"] >= self.target_trades:
                return {
                    "status": "TARGET_NOT_REACHED",
                    "cycles": cycles,
                    "summary": summary,
                }

        return {
            "status": "MAX_CYCLES_REACHED",
            "cycles": cycles,
            "summary": self._summary(),
        }
