from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .alerts import play_trade_alert
from .binance_futures_rest import BinanceFuturesRestClient
from .indicators import ema
from .ml_pipeline import MLWalkForwardOptimizer
from .models import Candle, ClosedTrade, Signal
from .strategy import StrategyEngine
from .trade_engine import TradeEngine


@dataclass
class CandidateSignal:
    signal: Signal
    trend_strength: float
    cost_r: float
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
        self.symbols = live_cfg.get(
            "symbols",
            [
                "BTCUSDT",
                "ETHUSDT",
                "SOLUSDT",
                "XRPUSDT",
                "ADAUSDT",
                "BNBUSDT",
                "DOGEUSDT",
                "LINKUSDT",
                "AVAXUSDT",
                "TRXUSDT",
            ],
        )
        self.timeframes = live_cfg.get("timeframes", ["1m", "5m", "15m"])
        self.lookback = int(live_cfg.get("lookback_candles", 260))
        self.poll_seconds = int(live_cfg.get("poll_seconds", 12))
        self.max_wait_minutes_per_trade = int(live_cfg.get("max_wait_minutes_per_trade", 120))
        self.min_rr_floor = float(live_cfg.get("min_rr_floor", 0.4))
        self.min_trend_strength = float(live_cfg.get("min_trend_strength", 0.0007))
        self.top_n = int(live_cfg.get("top_n", 3))
        self.target_trades = int(live_cfg.get("target_trades", 30))
        self.target_win_rate = float(live_cfg.get("target_win_rate", 0.75))
        self.min_trades_for_success = int(live_cfg.get("min_trades_for_success", 20))
        self.max_cycles = int(live_cfg.get("max_cycles", 1200))
        self.enable_sound = bool(config.get("scanner", {}).get("enable_sound", True))

        base_conf = float(self.strategy_payload.get("min_confidence", 0.6))
        self.symbol_confidence: Dict[str, float] = {s: base_conf for s in self.symbols}
        self.recent_trades: List[ClosedTrade] = []

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _market_snapshot(self, symbol: str) -> Dict:
        tick = self.client._get_json("/fapi/v1/ticker/price", {"symbol": symbol})
        return {
            "symbol": symbol,
            "price": float(tick["price"]),
            "time": int(tick.get("time", 0)),
        }

    def _signal_candidates(self) -> List[CandidateSignal]:
        candidates: List[CandidateSignal] = []

        for symbol in self.symbols:
            try:
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
                    score = (signal.confidence * 0.65) + (trend_strength * 100.0 * 0.25) + ((rr - cost_r) * 0.10)

                    candidates.append(
                        CandidateSignal(
                            signal=signal,
                            trend_strength=trend_strength,
                            cost_r=cost_r,
                            score=score,
                        )
                    )
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

    def _wait_for_close(self, signal: Signal) -> ClosedTrade:
        engine = TradeEngine(risk_usd=self.risk_usd)
        opened = engine.maybe_open_trade(signal)
        if not opened:
            raise RuntimeError("Failed to open paper trade")

        start = time.time()
        max_wait_seconds = self.max_wait_minutes_per_trade * 60
        last_seen_close_ms = signal.signal_time_ms

        while True:
            candles = self.client.fetch_klines(symbol=signal.symbol, interval=signal.timeframe, limit=3)
            closed_candles = [c for c in candles if c.close_time_ms < int(time.time() * 1000)]
            if closed_candles:
                latest = closed_candles[-1]
                if latest.close_time_ms > last_seen_close_ms:
                    last_seen_close_ms = latest.close_time_ms
                    closed = engine.on_candle(latest)
                    if closed:
                        return closed

            if time.time() - start >= max_wait_seconds:
                # Force-close at market using latest close when timeout is reached.
                latest = candles[-1]
                active = engine.active_trade
                if active is None:
                    raise RuntimeError("Active trade missing during timeout close")

                exit_price = latest.close
                risk = max(abs(active.entry - active.stop_loss), 1e-9)
                pnl_per_unit = exit_price - active.entry if active.side == "LONG" else active.entry - exit_price
                gross_r = pnl_per_unit / risk
                cost_r = self.cost_model.trade_cost_r(active.entry, active.stop_loss)
                net_r = gross_r - cost_r
                return ClosedTrade(
                    symbol=active.symbol,
                    timeframe=active.timeframe,
                    side=active.side,
                    entry=active.entry,
                    take_profit=active.take_profit,
                    stop_loss=active.stop_loss,
                    exit_price=exit_price,
                    result="WIN" if net_r > 0 else "LOSS",
                    opened_at_ms=active.opened_at_ms,
                    closed_at_ms=latest.close_time_ms,
                    pnl_r=net_r,
                    pnl_usd=net_r * self.risk_usd,
                    reason=f"TIMEOUT_EXIT | {active.reason}",
                )

            time.sleep(self.poll_seconds)

    def _apply_feedback(self, trade: ClosedTrade) -> None:
        symbol = trade.symbol
        current = self.symbol_confidence.get(symbol, float(self.strategy_payload["min_confidence"]))

        if trade.result == "LOSS":
            self.symbol_confidence[symbol] = min(0.97, current + 0.03)
            self.min_trend_strength = min(0.004, self.min_trend_strength + 0.00005)
            self.min_rr_floor = min(0.8, self.min_rr_floor + 0.01)
        else:
            self.symbol_confidence[symbol] = max(0.50, current - 0.005)
            self.min_trend_strength = max(0.0004, self.min_trend_strength - 0.00001)

    def _summary(self) -> Dict:
        trades = len(self.recent_trades)
        wins = sum(1 for t in self.recent_trades if t.result == "WIN")
        losses = trades - wins
        win_rate = (wins / trades) if trades else 0.0
        expectancy_r = (sum(t.pnl_r for t in self.recent_trades) / trades) if trades else 0.0
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
        }

    def run(self) -> Dict:
        cycles = 0

        while cycles < self.max_cycles:
            cycles += 1
            snapshots = []
            for symbol in self.symbols:
                try:
                    snapshots.append(self._market_snapshot(symbol))
                except Exception as exc:
                    print(
                        json.dumps(
                            {
                                "type": "SNAPSHOT_ERROR",
                                "time": self._now_iso(),
                                "symbol": symbol,
                                "error": str(exc),
                            }
                        )
                    )
            print(json.dumps({"type": "LIVE_MARKET", "time": self._now_iso(), "snapshots": snapshots}))

            candidates = self._signal_candidates()
            if not candidates:
                print(json.dumps({"type": "NO_SIGNAL", "time": self._now_iso(), "cycle": cycles}))
                time.sleep(self.poll_seconds)
                continue

            selected = candidates[: self.top_n][0]
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
                        "reason": selected.signal.reason,
                    }
                )
            )

            closed = self._wait_for_close(selected.signal)
            self.recent_trades.append(closed)

            self._apply_feedback(closed)

            print(
                json.dumps(
                    {
                        "type": "TRADE_RESULT",
                        "time": self._now_iso(),
                        "cycle": cycles,
                        "trade": asdict(closed),
                        "summary": self._summary(),
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
