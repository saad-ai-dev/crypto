from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .indicators import (
    adx,
    atr,
    bb_width,
    bollinger_bands,
    ema,
    ema_series,
    macd_histogram_series,
    rsi,
    support_resistance_zones,
    supertrend,
    supertrend_series,
    volume_profile,
)
from .models import Candle, MarketContext, Signal


@dataclass
class StrategyParameters:
    ema_fast: int
    ema_slow: int
    rsi_period: int
    atr_period: int
    atr_multiplier: float
    risk_reward: float
    min_atr_pct: float
    max_atr_pct: float
    funding_abs_limit: float
    min_confidence: float
    long_rsi_min: float
    long_rsi_max: float
    short_rsi_min: float
    short_rsi_max: float
    crossover_lookback: int = 5
    crossover_min_trend_strength: float = 0.0
    crossover_long_rsi_min: float = 0.0
    crossover_short_rsi_max: float = 100.0
    crossover_max_drift_atr: float = 0.5
    pullback_min_trend_strength: float = 0.003
    pullback_confirmation_slack_pct: float = 0.0
    pullback_risk_reward: float = 0.0
    pullback_stop_lookback: int = 6
    pullback_stop_buffer_atr: float = 0.8
    structure_stop_max_atr: float = 4.0
    rejection_wick_to_body_ratio: float = 1.2
    rejection_close_position_threshold: float = 0.45
    rejection_extreme_tolerance_atr: float = 0.3
    volume_ratio_min: float = 0.5
    ema_trend: int = 0  # 0 = disabled; set to e.g. 200 to only trade with macro trend
    # Regime detection parameters
    adx_period: int = 14
    adx_trending_threshold: float = 25.0
    adx_ranging_threshold: float = 20.0
    bb_period: int = 20
    bb_std: float = 2.0
    bb_width_volatile_threshold: float = 0.06
    vol_ratio_volatile_threshold: float = 1.5
    supertrend_period: int = 10
    supertrend_multiplier: float = 3.0
    # Bollinger Band mean reversion parameters
    bb_reversion_rsi_oversold: float = 30.0
    bb_reversion_rsi_overbought: float = 70.0
    bb_reversion_volume_spike: float = 1.5
    bb_reversion_stop_atr_mult: float = 0.5
    # Structure / market trend filters
    sr_zone_lookback: int = 120
    sr_swing_lookback: int = 4
    sr_merge_pct: float = 0.003
    sr_min_touches: int = 3
    sr_entry_tolerance_atr: float = 0.9
    sr_stop_buffer_atr: float = 0.35
    sr_target_buffer_atr: float = 0.2
    sr_min_room_atr: float = 1.2
    ma_break_lookback: int = 4
    ema_trend_slope_bars: int = 5
    ema_trend_slope_min: float = 0.0005


@dataclass(frozen=True)
class MarketRegime:
    """Classification of current market state."""
    regime: str          # "TRENDING", "RANGING", "VOLATILE"
    adx: float
    bb_width_val: float
    trend_direction: str  # "BULL", "BEAR", "NEUTRAL"
    confidence: float     # 0-1


@dataclass(frozen=True)
class MarketStructure:
    support: Optional[float]
    resistance: Optional[float]
    support_touches: int
    resistance_touches: int
    hvn_support: Optional[float]
    hvn_resistance: Optional[float]
    recent_swing_low: Optional[float] = None
    recent_swing_high: Optional[float] = None


class RegimeDetector:
    """Classifies market regime using ADX + BB width + volatility ratio."""

    def __init__(
        self,
        adx_period: int = 14,
        adx_trending: float = 25.0,
        adx_ranging: float = 20.0,
        bb_period: int = 20,
        bb_std: float = 2.0,
        bb_width_volatile: float = 0.06,
        vol_ratio_volatile: float = 1.5,
    ):
        self.adx_period = adx_period
        self.adx_trending = adx_trending
        self.adx_ranging = adx_ranging
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.bb_width_volatile = bb_width_volatile
        self.vol_ratio_volatile = vol_ratio_volatile

    def detect(
        self,
        candles: List[Candle],
        close_prices: List[float],
        ema_fast_v: float = 0.0,
        ema_slow_v: float = 0.0,
    ) -> MarketRegime:
        """Classify market regime from candle data."""
        # ADX
        min_adx_candles = 2 * self.adx_period + 1
        adx_val = 0.0
        if len(candles) >= min_adx_candles:
            adx_val = adx(candles, self.adx_period)

        # BB width
        bbw = 0.0
        if len(close_prices) >= self.bb_period:
            bbw = bb_width(close_prices, self.bb_period, self.bb_std)

        # Volatility ratio: current ATR / rolling average ATR
        vol_ratio = 1.0
        if len(candles) > 20:
            recent_ranges = [(c.high - c.low) for c in candles[-20:]]
            avg_range = sum(recent_ranges) / len(recent_ranges)
            current_range = candles[-1].high - candles[-1].low
            if avg_range > 0:
                vol_ratio = current_range / avg_range

        # Trend direction
        if ema_fast_v > ema_slow_v * 1.001:
            trend_dir = "BULL"
        elif ema_fast_v < ema_slow_v * 0.999:
            trend_dir = "BEAR"
        else:
            trend_dir = "NEUTRAL"

        # Classification with confidence
        if vol_ratio > self.vol_ratio_volatile and bbw > self.bb_width_volatile:
            regime = "VOLATILE"
            confidence = min(1.0, (vol_ratio - self.vol_ratio_volatile) / self.vol_ratio_volatile + 0.5)
        elif adx_val >= self.adx_trending:
            regime = "TRENDING"
            confidence = min(1.0, (adx_val - self.adx_trending) / 25.0 + 0.5)
        elif adx_val <= self.adx_ranging:
            regime = "RANGING"
            confidence = min(1.0, (self.adx_ranging - adx_val) / self.adx_ranging + 0.5)
        else:
            # Transition zone — default to ranging (conservative)
            regime = "RANGING"
            confidence = 0.4

        return MarketRegime(
            regime=regime,
            adx=adx_val,
            bb_width_val=bbw,
            trend_direction=trend_dir,
            confidence=confidence,
        )


class StrategyEngine:
    def __init__(self, params: StrategyParameters):
        self.params = params
        self.regime_detector = RegimeDetector(
            adx_period=params.adx_period,
            adx_trending=params.adx_trending_threshold,
            adx_ranging=params.adx_ranging_threshold,
            bb_period=params.bb_period,
            bb_std=params.bb_std,
            bb_width_volatile=params.bb_width_volatile_threshold,
            vol_ratio_volatile=params.vol_ratio_volatile_threshold,
        )

    @classmethod
    def from_dict(cls, payload: Dict) -> "StrategyEngine":
        params = StrategyParameters(
            ema_fast=int(payload["ema_fast"]),
            ema_slow=int(payload["ema_slow"]),
            rsi_period=int(payload["rsi_period"]),
            atr_period=int(payload["atr_period"]),
            atr_multiplier=float(payload["atr_multiplier"]),
            risk_reward=float(payload["risk_reward"]),
            min_atr_pct=float(payload["min_atr_pct"]),
            max_atr_pct=float(payload["max_atr_pct"]),
            funding_abs_limit=float(payload["funding_abs_limit"]),
            min_confidence=float(payload["min_confidence"]),
            long_rsi_min=float(payload["long_rsi_min"]),
            long_rsi_max=float(payload["long_rsi_max"]),
            short_rsi_min=float(payload["short_rsi_min"]),
            short_rsi_max=float(payload["short_rsi_max"]),
            crossover_lookback=int(payload.get("crossover_lookback", 5)),
            crossover_min_trend_strength=float(payload.get("crossover_min_trend_strength", 0.0)),
            crossover_long_rsi_min=float(payload.get("crossover_long_rsi_min", payload["long_rsi_min"])),
            crossover_short_rsi_max=float(payload.get("crossover_short_rsi_max", payload["short_rsi_max"])),
            crossover_max_drift_atr=float(payload.get("crossover_max_drift_atr", 0.5)),
            pullback_min_trend_strength=float(payload.get("pullback_min_trend_strength", 0.003)),
            pullback_confirmation_slack_pct=float(payload.get("pullback_confirmation_slack_pct", 0.0)),
            pullback_risk_reward=float(payload.get("pullback_risk_reward", payload.get("risk_reward", 1.5))),
            pullback_stop_lookback=int(payload.get("pullback_stop_lookback", 6)),
            pullback_stop_buffer_atr=float(payload.get("pullback_stop_buffer_atr", 0.8)),
            structure_stop_max_atr=float(payload.get("structure_stop_max_atr", 4.0)),
            rejection_wick_to_body_ratio=float(payload.get("rejection_wick_to_body_ratio", 1.2)),
            rejection_close_position_threshold=float(payload.get("rejection_close_position_threshold", 0.45)),
            rejection_extreme_tolerance_atr=float(payload.get("rejection_extreme_tolerance_atr", 0.3)),
            volume_ratio_min=float(payload.get("volume_ratio_min", 0.5)),
            ema_trend=int(payload.get("ema_trend", 0)),
            adx_period=int(payload.get("adx_period", 14)),
            adx_trending_threshold=float(payload.get("adx_trending_threshold", 25.0)),
            adx_ranging_threshold=float(payload.get("adx_ranging_threshold", 20.0)),
            bb_period=int(payload.get("bb_period", 20)),
            bb_std=float(payload.get("bb_std", 2.0)),
            bb_width_volatile_threshold=float(payload.get("bb_width_volatile_threshold", 0.06)),
            vol_ratio_volatile_threshold=float(payload.get("vol_ratio_volatile_threshold", 1.5)),
            supertrend_period=int(payload.get("supertrend_period", 10)),
            supertrend_multiplier=float(payload.get("supertrend_multiplier", 3.0)),
            bb_reversion_rsi_oversold=float(payload.get("bb_reversion_rsi_oversold", 30.0)),
            bb_reversion_rsi_overbought=float(payload.get("bb_reversion_rsi_overbought", 70.0)),
            bb_reversion_volume_spike=float(payload.get("bb_reversion_volume_spike", 1.5)),
            bb_reversion_stop_atr_mult=float(payload.get("bb_reversion_stop_atr_mult", 0.5)),
            sr_zone_lookback=int(payload.get("sr_zone_lookback", 120)),
            sr_swing_lookback=int(payload.get("sr_swing_lookback", 4)),
            sr_merge_pct=float(payload.get("sr_merge_pct", 0.003)),
            sr_min_touches=int(payload.get("sr_min_touches", 3)),
            sr_entry_tolerance_atr=float(payload.get("sr_entry_tolerance_atr", 0.9)),
            sr_stop_buffer_atr=float(payload.get("sr_stop_buffer_atr", 0.35)),
            sr_target_buffer_atr=float(payload.get("sr_target_buffer_atr", 0.2)),
            sr_min_room_atr=float(payload.get("sr_min_room_atr", 1.2)),
            ma_break_lookback=int(payload.get("ma_break_lookback", 4)),
            ema_trend_slope_bars=int(payload.get("ema_trend_slope_bars", 5)),
            ema_trend_slope_min=float(payload.get("ema_trend_slope_min", 0.0005)),
        )
        return cls(params)

    # ------------------------------------------------------------------ #
    #  Helper: swing point detection for Fibonacci scoring                #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _find_swing_points(candles: List[Candle], lookback: int = 20) -> tuple:
        """Return (swing_high, swing_low) from the last *lookback* bars."""
        window = candles[-lookback:] if len(candles) >= lookback else candles
        swing_high = max(c.high for c in window)
        swing_low = min(c.low for c in window)
        return (swing_high, swing_low)

    @staticmethod
    def _fibonacci_retracement_score(price: float, swing_high: float, swing_low: float, side: str) -> float:
        """Score based on Fibonacci retracement depth. Optimal zone: 38.2%-61.8%."""
        rng = swing_high - swing_low
        if rng <= 0:
            return 0.5
        if side == "LONG":
            depth = (swing_high - price) / rng  # how far price pulled back from high
        else:
            depth = (price - swing_low) / rng  # how far price pulled back from low
        # Optimal zone scoring
        if 0.382 <= depth <= 0.618:
            return 1.0  # golden zone
        elif 0.236 <= depth < 0.382:
            return 0.8
        elif 0.618 < depth <= 0.786:
            return 0.7
        else:
            return 0.4

    @staticmethod
    def _candle_quality_score(candles: List[Candle], side: str) -> float:
        """Score candle patterns: engulfing, pin bar, or plain confirmation."""
        if len(candles) < 2:
            return 0.3
        last = candles[-1]
        prev = candles[-2]
        last_body = abs(last.close - last.open)
        prev_body = abs(prev.close - prev.open)
        last_range = last.high - last.low
        if last_range <= 0:
            return 0.3

        # Engulfing pattern
        if side == "LONG":
            is_engulfing = (last.close > last.open and prev.close < prev.open
                            and last_body > prev_body * 1.1)
        else:
            is_engulfing = (last.close < last.open and prev.close > prev.open
                            and last_body > prev_body * 1.1)
        if is_engulfing:
            return 1.0

        # Pin bar (wick > 2x body on the rejection side)
        if side == "LONG":
            lower_wick = min(last.open, last.close) - last.low
            if lower_wick > 2 * last_body and last_body > 0:
                return 0.9
        else:
            upper_wick = last.high - max(last.open, last.close)
            if upper_wick > 2 * last_body and last_body > 0:
                return 0.9

        # Basic directional confirmation
        if (side == "LONG" and last.close > last.open) or (side == "SHORT" and last.close < last.open):
            return 0.6
        return 0.3

    def _build_market_structure(self, candles: List[Candle], entry: float) -> MarketStructure:
        window = candles[-self.params.sr_zone_lookback :] if len(candles) > self.params.sr_zone_lookback else candles

        zones = [
            (level, touches)
            for level, touches in support_resistance_zones(
                window,
                lookback=self.params.sr_swing_lookback,
                merge_pct=self.params.sr_merge_pct,
            )
            if touches >= self.params.sr_min_touches
        ]
        zones_by_price = sorted(zones, key=lambda item: item[0])

        support = None
        resistance = None
        support_touches = 0
        resistance_touches = 0
        for level, touches in zones_by_price:
            if level <= entry:
                support = level
                support_touches = touches
                continue
            resistance = level
            resistance_touches = touches
            break

        hvn_support = None
        hvn_resistance = None
        profile = volume_profile(window, num_bins=20)
        for level, _volume in profile:
            if hvn_support is None and level <= entry:
                hvn_support = level
            if hvn_resistance is None and level >= entry:
                hvn_resistance = level
            if hvn_support is not None and hvn_resistance is not None:
                break

        recent_window_size = max(3, self.params.pullback_stop_lookback)
        recent_window = candles[-recent_window_size:] if len(candles) > recent_window_size else candles
        recent_swing_low = min((c.low for c in recent_window), default=None)
        recent_swing_high = max((c.high for c in recent_window), default=None)

        return MarketStructure(
            support=support,
            resistance=resistance,
            support_touches=support_touches,
            resistance_touches=resistance_touches,
            hvn_support=hvn_support,
            hvn_resistance=hvn_resistance,
            recent_swing_low=recent_swing_low,
            recent_swing_high=recent_swing_high,
        )

    @staticmethod
    def _aligned_with_trend(side: str, trend_bias: str) -> bool:
        return trend_bias == "NEUTRAL" or (
            (side == "LONG" and trend_bias == "BULL")
            or (side == "SHORT" and trend_bias == "BEAR")
        )

    @staticmethod
    def _strictly_aligned_with_trend(side: str, trend_bias: str) -> bool:
        return (
            (side == "LONG" and trend_bias == "BULL")
            or (side == "SHORT" and trend_bias == "BEAR")
        )

    def _macro_trend_bias(
        self,
        close_prices: List[float],
        entry: float,
        ema_fast_v: float,
        ema_slow_v: float,
    ) -> str:
        if not close_prices:
            return "NEUTRAL"

        trend_period = self.params.ema_trend if (
            self.params.ema_trend > 0 and len(close_prices) >= self.params.ema_trend
        ) else self.params.ema_slow
        trend_ema = ema(close_prices, trend_period)

        slope_bars = max(1, self.params.ema_trend_slope_bars)
        slope = 0.0
        try:
            trend_series = ema_series(close_prices, trend_period)
            if len(trend_series) > slope_bars:
                slope = (trend_series[-1] - trend_series[-1 - slope_bars]) / max(entry, 1e-9)
        except ValueError:
            slope = (ema_fast_v - ema_slow_v) / max(entry, 1e-9)

        bullish = (
            entry >= trend_ema
            and ema_fast_v >= ema_slow_v
            and slope >= self.params.ema_trend_slope_min
        )
        bearish = (
            entry <= trend_ema
            and ema_fast_v <= ema_slow_v
            and slope <= -self.params.ema_trend_slope_min
        )
        if bullish:
            return "BULL"
        if bearish:
            return "BEAR"
        return "NEUTRAL"

    def _has_recent_ma_break(
        self,
        close_prices: List[float],
        side: str,
        period: int,
    ) -> bool:
        if period <= 1 or len(close_prices) < period + 1:
            return False
        try:
            ma_series = ema_series(close_prices, period)
        except ValueError:
            return False
        if len(ma_series) < 2:
            return False

        offset = len(close_prices) - len(ma_series)
        start = max(1, len(ma_series) - self.params.ma_break_lookback)
        for idx in range(start, len(ma_series)):
            prev_price = close_prices[offset + idx - 1]
            curr_price = close_prices[offset + idx]
            prev_ma = ma_series[idx - 1]
            curr_ma = ma_series[idx]
            if side == "LONG" and prev_price <= prev_ma and curr_price > curr_ma:
                return True
            if side == "SHORT" and prev_price >= prev_ma and curr_price < curr_ma:
                return True
        return False

    @staticmethod
    def _support_reference(structure: MarketStructure) -> Optional[float]:
        return structure.support if structure.support is not None else structure.hvn_support

    @staticmethod
    def _resistance_reference(structure: MarketStructure) -> Optional[float]:
        return structure.resistance if structure.resistance is not None else structure.hvn_resistance

    @staticmethod
    def _format_structure_level(level: Optional[float]) -> str:
        if level is None:
            return "na"
        return f"{level:.2f}"

    def _is_near_structure(
        self,
        side: str,
        entry: float,
        atr_v: float,
        structure: MarketStructure,
    ) -> bool:
        if atr_v <= 0:
            return False
        reference = (
            self._support_reference(structure)
            if side == "LONG"
            else self._resistance_reference(structure)
        )
        if reference is None:
            return False
        return abs(entry - reference) / atr_v <= self.params.sr_entry_tolerance_atr

    def _has_reward_room(
        self,
        side: str,
        entry: float,
        atr_v: float,
        structure: MarketStructure,
    ) -> bool:
        if atr_v <= 0:
            return False
        target_reference = (
            self._resistance_reference(structure)
            if side == "LONG"
            else self._support_reference(structure)
        )
        if target_reference is None:
            return True
        room = abs(target_reference - entry) / atr_v
        return room >= self.params.sr_min_room_atr

    def _is_rejection_against_entry(
        self,
        side: str,
        candles: List[Candle],
        atr_v: float,
        structure: MarketStructure,
    ) -> bool:
        if not candles or atr_v <= 0:
            return False

        last = candles[-1]
        candle_range = last.high - last.low
        if candle_range <= 0:
            return False

        body = abs(last.close - last.open)
        upper_wick = last.high - max(last.open, last.close)
        lower_wick = min(last.open, last.close) - last.low
        close_position = (last.close - last.low) / candle_range
        wick_threshold = max(body * self.params.rejection_wick_to_body_ratio, candle_range * 0.35)
        extreme_tolerance = atr_v * self.params.rejection_extreme_tolerance_atr

        resistance_ref = self._resistance_reference(structure)
        support_ref = self._support_reference(structure)
        near_recent_high = (
            structure.recent_swing_high is not None
            and last.high >= (structure.recent_swing_high - extreme_tolerance)
        )
        near_recent_low = (
            structure.recent_swing_low is not None
            and last.low <= (structure.recent_swing_low + extreme_tolerance)
        )
        near_resistance = (
            (resistance_ref is not None and last.high >= (resistance_ref - extreme_tolerance))
            or near_recent_high
        )
        near_support = (
            (support_ref is not None and last.low <= (support_ref + extreme_tolerance))
            or near_recent_low
        )

        if side == "LONG":
            return (
                near_resistance
                and upper_wick >= wick_threshold
                and close_position <= self.params.rejection_close_position_threshold
            )
        return (
            near_support
            and lower_wick >= wick_threshold
            and close_position >= (1.0 - self.params.rejection_close_position_threshold)
        )

    def _build_trade_levels(
        self,
        side: str,
        signal_type: str,
        entry: float,
        atr_v: float,
        structure: MarketStructure,
        extra: Optional[Dict],
    ) -> tuple[float, float]:
        support_ref = self._support_reference(structure)
        resistance_ref = self._resistance_reference(structure)
        stop_buffer = atr_v * self.params.sr_stop_buffer_atr
        target_buffer = atr_v * self.params.sr_target_buffer_atr
        rr_multiplier = self.params.risk_reward
        if signal_type == "PULLBACK":
            rr_multiplier = max(self.params.risk_reward, self.params.pullback_risk_reward)

        if signal_type == "BB_REVERSION" and extra and "bb" in extra:
            _bb_upper, bb_mid, _bb_lower = extra["bb"][:3]
            sl_distance = atr_v * self.params.bb_reversion_stop_atr_mult
            if side == "LONG":
                stop_loss = entry - sl_distance
                if support_ref is not None and support_ref < entry:
                    candidate = support_ref - stop_buffer
                    if 0 < entry - candidate <= sl_distance * 2.0:
                        stop_loss = candidate
                take_profit = bb_mid if bb_mid > entry else entry + sl_distance
            else:
                stop_loss = entry + sl_distance
                if resistance_ref is not None and resistance_ref > entry:
                    candidate = resistance_ref + stop_buffer
                    if 0 < candidate - entry <= sl_distance * 2.0:
                        stop_loss = candidate
                take_profit = bb_mid if bb_mid < entry else entry - sl_distance
            return (stop_loss, take_profit)

        if signal_type == "SUPERTREND" and extra and "st_value" in extra:
            st_val = extra["st_value"]
            if side == "LONG":
                stop_loss = st_val - atr_v * 0.2
                if support_ref is not None and support_ref < entry:
                    stop_loss = min(stop_loss, support_ref - stop_buffer)
                sl_distance = max(entry - stop_loss, atr_v * 0.5)
                take_profit = entry + sl_distance * rr_multiplier
            else:
                stop_loss = st_val + atr_v * 0.2
                if resistance_ref is not None and resistance_ref > entry:
                    stop_loss = max(stop_loss, resistance_ref + stop_buffer)
                sl_distance = max(stop_loss - entry, atr_v * 0.5)
                take_profit = entry - sl_distance * rr_multiplier
        else:
            sl_distance = atr_v * self.params.atr_multiplier
            if side == "LONG":
                stop_loss = entry - sl_distance
                if signal_type == "PULLBACK":
                    structure_stop_limit = atr_v * self.params.structure_stop_max_atr
                    swing_buffer = max(stop_buffer, atr_v * self.params.pullback_stop_buffer_atr)
                    candidates = [stop_loss]
                    if support_ref is not None and support_ref < entry:
                        candidate = support_ref - stop_buffer
                        if 0 < entry - candidate <= structure_stop_limit:
                            candidates.append(candidate)
                    if structure.recent_swing_low is not None and structure.recent_swing_low < entry:
                        candidate = structure.recent_swing_low - swing_buffer
                        if 0 < entry - candidate <= structure_stop_limit:
                            candidates.append(candidate)
                    stop_loss = min(candidates)
                    sl_distance = max(entry - stop_loss, atr_v * 0.75)
                else:
                    if support_ref is not None and support_ref < entry:
                        candidate = support_ref - stop_buffer
                        if 0 < entry - candidate <= sl_distance * 2.0:
                            stop_loss = candidate
                    sl_distance = max(entry - stop_loss, atr_v * 0.5)
                take_profit = entry + (sl_distance * rr_multiplier)
            else:
                stop_loss = entry + sl_distance
                if signal_type == "PULLBACK":
                    structure_stop_limit = atr_v * self.params.structure_stop_max_atr
                    swing_buffer = max(stop_buffer, atr_v * self.params.pullback_stop_buffer_atr)
                    candidates = [stop_loss]
                    if resistance_ref is not None and resistance_ref > entry:
                        candidate = resistance_ref + stop_buffer
                        if 0 < candidate - entry <= structure_stop_limit:
                            candidates.append(candidate)
                    if structure.recent_swing_high is not None and structure.recent_swing_high > entry:
                        candidate = structure.recent_swing_high + swing_buffer
                        if 0 < candidate - entry <= structure_stop_limit:
                            candidates.append(candidate)
                    stop_loss = max(candidates)
                    sl_distance = max(stop_loss - entry, atr_v * 0.75)
                else:
                    if resistance_ref is not None and resistance_ref > entry:
                        candidate = resistance_ref + stop_buffer
                        if 0 < candidate - entry <= sl_distance * 2.0:
                            stop_loss = candidate
                    sl_distance = max(stop_loss - entry, atr_v * 0.5)
                take_profit = entry - (sl_distance * rr_multiplier)

        if side == "LONG" and resistance_ref is not None and resistance_ref > entry:
            capped_tp = resistance_ref - target_buffer
            target_distance = take_profit - entry
            should_cap = signal_type != "PULLBACK" or (capped_tp - entry) <= (target_distance * 0.5)
            if capped_tp > entry and should_cap:
                take_profit = min(take_profit, capped_tp)
        if side == "SHORT" and support_ref is not None and support_ref < entry:
            capped_tp = support_ref + target_buffer
            target_distance = entry - take_profit
            should_cap = signal_type != "PULLBACK" or (entry - capped_tp) <= (target_distance * 0.5)
            if capped_tp < entry and should_cap:
                take_profit = max(take_profit, capped_tp)

        return (stop_loss, take_profit)

    # ------------------------------------------------------------------ #
    #  Structure-based confidence scoring                                  #
    # ------------------------------------------------------------------ #
    def _compute_confidence(
        self,
        side: str,
        signal_type: str,
        regime: MarketRegime,
        candles: List[Candle],
        close_prices: List[float],
        market: MarketContext,
        entry: float,
        atr_v: float,
        ema_fast_v: float,
        ema_slow_v: float,
        rsi_v: float,
        bb_vals: Optional[tuple] = None,
    ) -> float:
        """Compute confidence from structure, not indicator centrality."""
        trend_strength = abs(ema_fast_v - ema_slow_v) / entry if entry else 0.0

        # 1. Regime alignment (0.25)
        regime_map = {
            ("TRENDING", "CROSSOVER"): 1.0,
            ("TRENDING", "PULLBACK"): 1.0,
            ("TRENDING", "SUPERTREND"): 1.0,
            ("RANGING", "BB_REVERSION"): 1.0,
            ("RANGING", "CROSSOVER"): 0.5,
            ("RANGING", "PULLBACK"): 0.5,
            ("RANGING", "SUPERTREND"): 0.6,
            ("VOLATILE", "BB_REVERSION"): 0.8,
            ("VOLATILE", "SUPERTREND"): 0.5,
        }
        regime_score = regime_map.get((regime.regime, signal_type), 0.3)
        if regime.regime == "VOLATILE" and signal_type not in ("BB_REVERSION", "SUPERTREND"):
            regime_score = 0.2

        # 2. Structure score (0.30)
        if signal_type == "PULLBACK":
            swing_high, swing_low = self._find_swing_points(candles)
            structure_score = self._fibonacci_retracement_score(entry, swing_high, swing_low, side)
        elif signal_type == "BB_REVERSION" and bb_vals:
            bb_upper, bb_mid, bb_lower = bb_vals[:3]
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                if side == "LONG":
                    structure_score = min(1.0, (bb_lower - entry) / (bb_range * 0.1) + 0.8) if entry <= bb_lower else 0.5
                else:
                    structure_score = min(1.0, (entry - bb_upper) / (bb_range * 0.1) + 0.8) if entry >= bb_upper else 0.5
            else:
                structure_score = 0.5
        elif signal_type == "SUPERTREND":
            # ADX strength + MACD momentum
            adx_score = min(regime.adx / 40.0, 1.0) if regime.adx > 0 else 0.3
            macd_score = 0.5
            try:
                hist = macd_histogram_series(close_prices)
                if len(hist) >= 2:
                    # Increasing histogram = momentum expanding
                    if (side == "LONG" and hist[-1] > hist[-2]) or (side == "SHORT" and hist[-1] < hist[-2]):
                        macd_score = 0.9
                    elif (side == "LONG" and hist[-1] > 0) or (side == "SHORT" and hist[-1] < 0):
                        macd_score = 0.7
            except (ValueError, IndexError):
                pass
            structure_score = 0.5 * adx_score + 0.5 * macd_score
        else:
            # Crossover: use trend strength
            structure_score = min(trend_strength / 0.002, 1.0)

        # 3. Timing score (0.20) — candle quality + stop distance quality
        candle_score = self._candle_quality_score(candles, side)
        sl_distance_atr = (self.params.atr_multiplier if atr_v > 0 else 1.0)
        stop_quality = max(0.0, min(1.0, 1.0 - (sl_distance_atr - 1.0) / 2.0))
        timing_score = 0.6 * candle_score + 0.4 * stop_quality

        # 4. Volume score (0.10) — relative volume as bonus
        volume_score = 0.5
        if len(candles) >= 20:
            recent_vols = [c.volume for c in candles[-20:]]
            avg_vol = sum(recent_vols) / len(recent_vols)
            if avg_vol > 0:
                rel_vol = candles[-1].volume / avg_vol
                volume_score = min(1.0, rel_vol / 1.5)

        # 5. Context score (0.15) — macro trend + funding
        # Soft penalty: counter-trend trades get 0.6 not 0.3 (was too harsh)
        macro_aligned = 1.0
        if self.params.ema_trend > 0 and len(close_prices) >= self.params.ema_trend:
            ema_trend_v = ema(close_prices, self.params.ema_trend)
            if side == "LONG":
                macro_aligned = 1.0 if entry >= ema_trend_v else 0.6
            else:
                macro_aligned = 1.0 if entry <= ema_trend_v else 0.6
        # BB_REVERSION is inherently counter-trend — don't penalize it for that
        if signal_type == "BB_REVERSION":
            macro_aligned = max(macro_aligned, 0.8)
        funding_score = 1.0 - min(abs(market.funding_rate) / max(self.params.funding_abs_limit, 1e-9), 1.0)
        context_score = 0.6 * macro_aligned + 0.4 * funding_score

        confidence = (
            0.05
            + 0.25 * regime_score
            + 0.30 * structure_score
            + 0.20 * timing_score
            + 0.10 * volume_score
            + 0.15 * context_score
        )
        return max(0.0, min(confidence, 0.99))

    # ------------------------------------------------------------------ #
    #  Sub-strategy: Crossover                                             #
    # ------------------------------------------------------------------ #
    def _evaluate_crossover(
        self,
        candles: List[Candle],
        close_prices: List[float],
        market: MarketContext,
        regime: MarketRegime,
        entry: float,
        ema_fast_v: float,
        ema_slow_v: float,
        rsi_v: float,
        atr_v: float,
        allow_long: bool,
        allow_short: bool,
        note,
    ) -> Optional[tuple]:
        """Return (side, signal_type, extra_info) or None."""
        crossover_lookback = self.params.crossover_lookback
        fast_series_vals = ema_series(close_prices, self.params.ema_fast)
        slow_series_vals = ema_series(close_prices, self.params.ema_slow)

        look = min(crossover_lookback + 1, len(fast_series_vals), len(slow_series_vals))
        recent_diffs = []
        for k in range(look):
            fi = len(fast_series_vals) - look + k
            si = len(slow_series_vals) - look + k
            recent_diffs.append(fast_series_vals[fi] - slow_series_vals[si])

        bullish_cross = any(recent_diffs[j] <= 0 and recent_diffs[j + 1] > 0
                           for j in range(len(recent_diffs) - 1))
        bearish_cross = any(recent_diffs[j] >= 0 and recent_diffs[j + 1] < 0
                           for j in range(len(recent_diffs) - 1))
        trend_strength = abs(ema_fast_v - ema_slow_v) / entry if entry else 0.0
        if trend_strength < self.params.crossover_min_trend_strength:
            if bullish_cross or bearish_cross:
                note("crossover_trend_too_weak")
            return None

        if not bullish_cross and not bearish_cross:
            note("no_recent_crossover")
            return None

        def _crossover_age_and_drift(diffs, bullish: bool) -> tuple:
            for j in range(len(diffs) - 1, 0, -1):
                if bullish and diffs[j - 1] <= 0 and diffs[j] > 0:
                    return (len(diffs) - 1 - j, abs(entry - close_prices[-(len(diffs) - j)]) / atr_v if atr_v else 0.0)
                if not bullish and diffs[j - 1] >= 0 and diffs[j] < 0:
                    return (len(diffs) - 1 - j, abs(entry - close_prices[-(len(diffs) - j)]) / atr_v if atr_v else 0.0)
            return (len(diffs), 0.0)

        if bullish_cross:
            age, drift = _crossover_age_and_drift(recent_diffs, True)
            if age >= 3 and drift > self.params.crossover_max_drift_atr:
                note("crossover_drift_too_large")
                bullish_cross = False
        if bearish_cross:
            age, drift = _crossover_age_and_drift(recent_diffs, False)
            if age >= 3 and drift > self.params.crossover_max_drift_atr:
                note("crossover_drift_too_large")
                bearish_cross = False

        if (
            allow_long and ema_fast_v > ema_slow_v and bullish_cross
            and self.params.long_rsi_min <= rsi_v <= self.params.long_rsi_max
            and rsi_v >= self.params.crossover_long_rsi_min
            and entry >= ema_fast_v
            and abs(market.funding_rate) <= self.params.funding_abs_limit
        ):
            return ("LONG", "CROSSOVER", None)
        elif (
            allow_short and ema_fast_v < ema_slow_v and bearish_cross
            and self.params.short_rsi_min <= rsi_v <= self.params.short_rsi_max
            and rsi_v <= self.params.crossover_short_rsi_max
            and entry <= ema_fast_v
            and abs(market.funding_rate) <= self.params.funding_abs_limit
        ):
            return ("SHORT", "CROSSOVER", None)

        # Diagnostic notes for crossover failures
        if bullish_cross or bearish_cross:
            if abs(market.funding_rate) > self.params.funding_abs_limit:
                note("crossover_funding_blocked")
            elif bullish_cross and not allow_long:
                note("crossover_macro_trend_blocked")
            elif bearish_cross and not allow_short:
                note("crossover_macro_trend_blocked")
            else:
                note("crossover_rsi_out_of_range")
        return None

    # ------------------------------------------------------------------ #
    #  Sub-strategy: Trend Pullback (improved with Fib + candle patterns) #
    # ------------------------------------------------------------------ #
    def _evaluate_trend_pullback(
        self,
        candles: List[Candle],
        close_prices: List[float],
        market: MarketContext,
        regime: MarketRegime,
        entry: float,
        ema_fast_v: float,
        ema_slow_v: float,
        rsi_v: float,
        atr_v: float,
        allow_long: bool,
        allow_short: bool,
        st_direction: str,
        note,
    ) -> Optional[tuple]:
        """Improved pullback with ADX gate, SuperTrend alignment, Fib scoring."""
        if abs(market.funding_rate) > self.params.funding_abs_limit:
            note("pullback_funding_blocked")
            return None

        trend_strength = abs(ema_fast_v - ema_slow_v) / entry if entry else 0.0
        if trend_strength <= self.params.pullback_min_trend_strength or len(candles) < 3:
            note("pullback_trend_too_weak")
            return None

        # SuperTrend alignment check (soft — just downgrades confidence if misaligned)
        prev_close = candles[-2].close
        prev2_close = candles[-3].close

        if allow_long and ema_fast_v > ema_slow_v:
            touch_dist = abs(candles[-1].low - ema_fast_v) / atr_v if atr_v else 999
            bounced = entry > ema_fast_v and prev_close <= ema_fast_v * 1.002
            confirmed = (
                prev_close >= prev2_close * (1 - self.params.pullback_confirmation_slack_pct)
                and entry >= prev_close * (1 - self.params.pullback_confirmation_slack_pct)
            )
            if (touch_dist < 1.0 or bounced) and confirmed and self.params.long_rsi_min <= rsi_v <= self.params.long_rsi_max:
                return ("LONG", "PULLBACK", {"st_aligned": st_direction == "UP"})
            elif self.params.long_rsi_min <= rsi_v <= self.params.long_rsi_max:
                note("pullback_confirmation_failed")
            else:
                note("pullback_rsi_out_of_range")

        elif allow_short and ema_fast_v < ema_slow_v:
            touch_dist = abs(candles[-1].high - ema_fast_v) / atr_v if atr_v else 999
            rejected = entry < ema_fast_v and prev_close >= ema_fast_v * 0.998
            prev_was_pullback = prev_close >= prev2_close * (1 - self.params.pullback_confirmation_slack_pct)
            bearish_reclaim = entry < prev_close and entry < candles[-1].open
            confirmed = (
                prev_was_pullback
                and bearish_reclaim
            )
            if (touch_dist < 1.0 or rejected) and confirmed and self.params.short_rsi_min <= rsi_v <= self.params.short_rsi_max:
                return ("SHORT", "PULLBACK", {"st_aligned": st_direction == "DOWN"})
            elif self.params.short_rsi_min <= rsi_v <= self.params.short_rsi_max:
                note("pullback_confirmation_failed")
            else:
                note("pullback_rsi_out_of_range")
        else:
            note("pullback_macro_trend_blocked")

        return None

    # ------------------------------------------------------------------ #
    #  Sub-strategy: SuperTrend Trend Following                            #
    # ------------------------------------------------------------------ #
    def _evaluate_supertrend_trend(
        self,
        candles: List[Candle],
        close_prices: List[float],
        market: MarketContext,
        regime: MarketRegime,
        entry: float,
        rsi_v: float,
        atr_v: float,
        st_direction: str,
        note,
    ) -> Optional[tuple]:
        """SuperTrend flip with ADX + MACD confirmation."""
        if abs(market.funding_rate) > self.params.funding_abs_limit:
            note("st_trend_funding_blocked")
            return None

        if len(candles) <= self.params.supertrend_period + 2:
            return None

        # Need recent SuperTrend flip
        try:
            st_series = supertrend_series(candles, self.params.supertrend_period, self.params.supertrend_multiplier)
        except (ValueError, IndexError):
            return None

        if len(st_series) < 3:
            return None

        curr_dir = st_series[-1][1]
        # Check for recent flip (within last 3 bars)
        flipped = False
        for i in range(max(0, len(st_series) - 4), len(st_series) - 1):
            if st_series[i][1] != st_series[i + 1][1]:
                flipped = True
                break

        if not flipped:
            note("st_trend_no_flip")
            return None

        # MACD confirmation
        macd_ok = True
        try:
            hist = macd_histogram_series(close_prices)
            if len(hist) >= 2:
                if curr_dir == "UP" and hist[-1] < 0:
                    macd_ok = False
                elif curr_dir == "DOWN" and hist[-1] > 0:
                    macd_ok = False
        except (ValueError, IndexError):
            pass  # skip MACD check if not enough data

        if not macd_ok:
            note("st_trend_macd_divergent")
            return None

        # Volume check
        if len(candles) >= 20:
            avg_vol = sum(c.volume for c in candles[-20:]) / 20
            if avg_vol > 0 and candles[-1].volume < avg_vol * 0.8:
                note("st_trend_low_volume")
                return None

        st_val = st_series[-1][0]
        if curr_dir == "UP":
            # Price should be above SuperTrend
            if entry <= st_val:
                note("st_trend_price_below_st")
                return None
            if rsi_v > 75:
                note("st_trend_rsi_exhausted")
                return None
            return ("LONG", "SUPERTREND", {"st_value": st_val})
        else:
            if entry >= st_val:
                note("st_trend_price_above_st")
                return None
            if rsi_v < 25:
                note("st_trend_rsi_exhausted")
                return None
            return ("SHORT", "SUPERTREND", {"st_value": st_val})

    # ------------------------------------------------------------------ #
    #  Sub-strategy: Bollinger Band Mean Reversion                         #
    # ------------------------------------------------------------------ #
    def _evaluate_bb_mean_reversion(
        self,
        candles: List[Candle],
        close_prices: List[float],
        market: MarketContext,
        regime: MarketRegime,
        entry: float,
        rsi_v: float,
        atr_v: float,
        note,
    ) -> Optional[tuple]:
        """Mean reversion from Bollinger Band extremes in ranging markets."""
        if abs(market.funding_rate) > self.params.funding_abs_limit:
            note("bb_reversion_funding_blocked")
            return None

        if len(close_prices) < self.params.bb_period:
            note("bb_reversion_insufficient_data")
            return None

        bb_upper, bb_mid, bb_lower = bollinger_bands(
            close_prices, self.params.bb_period, self.params.bb_std
        )

        # Volume spike check
        has_volume_spike = False
        if len(candles) >= 20:
            avg_vol = sum(c.volume for c in candles[-20:]) / 20
            if avg_vol > 0:
                has_volume_spike = candles[-1].volume >= avg_vol * self.params.bb_reversion_volume_spike

        bb_vals = (bb_upper, bb_mid, bb_lower)

        # LONG: price at or below lower band + RSI oversold
        # Volume spike is a bonus, not a hard requirement
        if entry <= bb_lower and rsi_v <= self.params.bb_reversion_rsi_oversold:
            return ("LONG", "BB_REVERSION", {"bb": bb_vals, "volume_spike": has_volume_spike})

        # SHORT: price at or above upper band + RSI overbought
        if entry >= bb_upper and rsi_v >= self.params.bb_reversion_rsi_overbought:
            return ("SHORT", "BB_REVERSION", {"bb": bb_vals, "volume_spike": has_volume_spike})

        # Diagnostic notes
        if entry <= bb_lower or entry >= bb_upper:
            if not has_volume_spike:
                note("bb_reversion_no_volume_spike")
            else:
                note("bb_reversion_rsi_not_extreme")
        else:
            note("bb_reversion_price_not_at_band")

        return None

    # ------------------------------------------------------------------ #
    #  Main evaluate() — regime-aware router                               #
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        symbol: str,
        timeframe: str,
        candles: List[Candle],
        market: MarketContext,
        diagnostics: Optional[Dict[str, int]] = None,
    ) -> Optional[Signal]:
        def note(reason: str) -> None:
            if diagnostics is not None:
                diagnostics[reason] = int(diagnostics.get(reason, 0)) + 1

        needed = max(self.params.ema_slow, self.params.rsi_period + 1, self.params.atr_period + 1)
        if len(candles) < needed:
            note("not_enough_candles")
            return None

        close_prices = [c.close for c in candles]
        last = candles[-1]
        entry = last.close

        # Core indicators
        ema_fast_v = ema(close_prices, self.params.ema_fast)
        ema_slow_v = ema(close_prices, self.params.ema_slow)
        rsi_v = rsi(close_prices, self.params.rsi_period)
        atr_v = atr(candles, self.params.atr_period)

        atr_pct = atr_v / entry if entry else 0.0
        if atr_pct < self.params.min_atr_pct or atr_pct > self.params.max_atr_pct:
            note("atr_out_of_range")
            return None

        # Regime detection
        regime = self.regime_detector.detect(candles, close_prices, ema_fast_v, ema_slow_v)
        structure = self._build_market_structure(candles, entry)

        # SuperTrend (safe — only if enough candles)
        st_direction = "UP"
        if len(candles) > self.params.supertrend_period:
            try:
                _, st_direction = supertrend(candles, self.params.supertrend_period, self.params.supertrend_multiplier)
            except (ValueError, IndexError):
                pass

        # Market trend is now a first-class filter for trend-following setups.
        trend_bias = self._macro_trend_bias(close_prices, entry, ema_fast_v, ema_slow_v)
        allow_long = trend_bias != "BEAR"
        allow_short = trend_bias != "BULL"

        # Route based on regime
        result = None

        if regime.regime == "VOLATILE":
            note("volatile_regime_standby")
            # In volatile regime, try BB mean reversion (extremes happen here)
            result = self._evaluate_bb_mean_reversion(
                candles, close_prices, market, regime, entry, rsi_v, atr_v, note,
            )
            if not result:
                result = self._evaluate_supertrend_trend(
                    candles, close_prices, market, regime, entry, rsi_v, atr_v, st_direction, note,
                )

        elif regime.regime == "TRENDING":
            # Try SuperTrend first, then pullback, then crossover
            result = self._evaluate_supertrend_trend(
                candles, close_prices, market, regime, entry, rsi_v, atr_v, st_direction, note,
            )
            if not result:
                result = self._evaluate_trend_pullback(
                    candles, close_prices, market, regime, entry,
                    ema_fast_v, ema_slow_v, rsi_v, atr_v, allow_long, allow_short, st_direction, note,
                )
            if not result:
                result = self._evaluate_crossover(
                    candles, close_prices, market, regime, entry,
                    ema_fast_v, ema_slow_v, rsi_v, atr_v, allow_long, allow_short, note,
                )

        elif regime.regime == "RANGING":
            # Try BB mean reversion first, then SuperTrend, then crossover
            result = self._evaluate_bb_mean_reversion(
                candles, close_prices, market, regime, entry, rsi_v, atr_v, note,
            )
            if not result:
                result = self._evaluate_supertrend_trend(
                    candles, close_prices, market, regime, entry, rsi_v, atr_v, st_direction, note,
                )
            if not result:
                result = self._evaluate_crossover(
                    candles, close_prices, market, regime, entry,
                    ema_fast_v, ema_slow_v, rsi_v, atr_v, allow_long, allow_short, note,
                )

        if not result:
            return None

        side, signal_type, extra = result

        if signal_type in ("CROSSOVER", "SUPERTREND"):
            ma_break_confirmed = (
                self._has_recent_ma_break(close_prices, side, self.params.ema_fast)
                or self._has_recent_ma_break(close_prices, side, self.params.ema_slow)
                or (
                    self.params.ema_trend > 0
                    and self._has_recent_ma_break(close_prices, side, self.params.ema_trend)
                )
            )
            if not ma_break_confirmed:
                note("ma_break_not_confirmed")
                return None
            if not self._aligned_with_trend(side, trend_bias):
                note("macro_trend_blocked")
                return None

        if signal_type == "PULLBACK":
            if not self._aligned_with_trend(side, trend_bias):
                note("pullback_macro_trend_blocked")
                return None
            if side == "SHORT" and not self._strictly_aligned_with_trend(side, trend_bias):
                note("pullback_short_requires_bear_trend")
                return None
            if self._is_rejection_against_entry(side, candles, atr_v, structure):
                note("pullback_structure_rejection")
                return None
            if not self._is_near_structure(side, entry, atr_v, structure):
                note("sr_pullback_not_supported")
                return None

        if signal_type == "BB_REVERSION" and not self._is_near_structure(side, entry, atr_v, structure):
            note("sr_reversion_not_supported")
            return None

        if signal_type in ("CROSSOVER", "PULLBACK", "SUPERTREND") and not self._has_reward_room(side, entry, atr_v, structure):
            note("sr_room_too_tight")
            return None

        # Volume confirmation (skip for BB_REVERSION/SUPERTREND which already checked volume)
        if signal_type not in ("BB_REVERSION", "SUPERTREND") and len(candles) >= 20:
            recent_vols = [c.volume for c in candles[-20:]]
            avg_vol = sum(recent_vols) / len(recent_vols)
            if avg_vol > 0 and last.volume < avg_vol * self.params.volume_ratio_min:
                note("volume_too_low")
                return None

        # Reversal candle filter
        candle_range = last.high - last.low
        if candle_range > 0:
            body_ratio = abs(last.close - last.open) / candle_range
            if side == "LONG" and last.close < last.open and body_ratio > 0.6:
                note("reversal_candle_blocked")
                return None
            if side == "SHORT" and last.close > last.open and body_ratio > 0.6:
                note("reversal_candle_blocked")
                return None

        stop_loss, take_profit = self._build_trade_levels(
            side=side,
            signal_type=signal_type,
            entry=entry,
            atr_v=atr_v,
            structure=structure,
            extra=extra,
        )
        if side == "LONG" and not (stop_loss < entry < take_profit):
            note("invalid_trade_levels")
            return None
        if side == "SHORT" and not (take_profit < entry < stop_loss):
            note("invalid_trade_levels")
            return None

        # Structure-based confidence scoring
        bb_vals = extra.get("bb") if extra else None
        confidence = self._compute_confidence(
            side, signal_type, regime, candles, close_prices, market,
            entry, atr_v, ema_fast_v, ema_slow_v, rsi_v, bb_vals,
        )

        if confidence < self.params.min_confidence:
            note("confidence_below_min")
            return None

        note("signal_generated")
        regime_tag = f"regime={regime.regime}"
        support_ref = self._support_reference(structure)
        resistance_ref = self._resistance_reference(structure)
        reason = (
            f"{side} {signal_type.lower()} | {regime_tag} | "
            f"trend={trend_bias} | "
            f"EMA({self.params.ema_fast}/{self.params.ema_slow})={ema_fast_v:.2f}/{ema_slow_v:.2f}, "
            f"RSI={rsi_v:.1f}, ATR%={atr_pct:.4f}, ADX={regime.adx:.1f}, "
            f"SR={self._format_structure_level(support_ref)}/{self._format_structure_level(resistance_ref)}, "
            f"funding={market.funding_rate:.5f}"
        )
        return Signal(
            symbol=symbol,
            timeframe=timeframe,
            side=side,
            entry=round(entry, 6),
            take_profit=round(take_profit, 6),
            stop_loss=round(stop_loss, 6),
            confidence=round(confidence, 4),
            reason=reason,
            signal_time_ms=last.close_time_ms,
        )

    def adaptive_tune_after_trade(self, trade_result: str) -> None:
        if trade_result == "LOSS":
            self.params.min_confidence = min(0.80, self.params.min_confidence + 0.005)
            return

        self.params.min_confidence = max(0.55, self.params.min_confidence - 0.003)
