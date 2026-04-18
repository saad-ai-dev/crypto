from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


_REQUIRED_SECTIONS = ["account", "execution", "strategy", "live_loop"]

_STRATEGY_KEYS = [
    "ema_fast", "ema_slow", "rsi_period", "atr_period",
    "atr_multiplier", "risk_reward", "min_atr_pct", "max_atr_pct",
    "funding_abs_limit", "min_confidence",
    "long_rsi_min", "long_rsi_max", "short_rsi_min", "short_rsi_max",
]

_LIVE_LOOP_KEYS = [
    "symbols", "timeframes", "lookback_candles", "poll_seconds",
    "execute_min_confidence", "execute_min_expectancy_r", "execute_min_score",
]

_VALID_REGIMES = {"TRENDING", "RANGING", "VOLATILE"}


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    for section in _REQUIRED_SECTIONS:
        if section not in cfg:
            errors.append(f"Missing required section: '{section}'")

    strat = cfg.get("strategy", {})
    for key in _STRATEGY_KEYS:
        if key not in strat:
            errors.append(f"Missing strategy key: '{key}'")

    ll = cfg.get("live_loop", {})
    for key in _LIVE_LOOP_KEYS:
        if key not in ll:
            errors.append(f"Missing live_loop key: '{key}'")

    account = cfg.get("account", {})
    starting_balance = account.get("starting_balance_usd")
    risk_per_trade_pct = account.get("risk_per_trade_pct")
    paper_risk_usd = account.get("paper_risk_usd")
    max_position_pct = account.get("max_position_pct")

    if starting_balance is not None and float(starting_balance) <= 0:
        errors.append("account.starting_balance_usd must be > 0")
    if risk_per_trade_pct is not None and float(risk_per_trade_pct) <= 0:
        errors.append("account.risk_per_trade_pct must be > 0")
    if paper_risk_usd is not None and float(paper_risk_usd) <= 0:
        errors.append("account.paper_risk_usd must be > 0")
    if max_position_pct is not None and not (0 < float(max_position_pct) <= 1):
        errors.append("account.max_position_pct must be > 0 and <= 1")
    daily_loss_limit_r = ll.get("daily_loss_limit_r")
    if daily_loss_limit_r is not None and float(daily_loss_limit_r) <= 0:
        errors.append("live_loop.daily_loss_limit_r must be > 0 when provided")

    if strat.get("long_rsi_min", 0) >= strat.get("long_rsi_max", 100):
        errors.append("strategy.long_rsi_min must be < long_rsi_max")
    if strat.get("short_rsi_min", 0) >= strat.get("short_rsi_max", 100):
        errors.append("strategy.short_rsi_min must be < short_rsi_max")
    if strat.get("min_atr_pct", 0) >= strat.get("max_atr_pct", 1):
        errors.append("strategy.min_atr_pct must be < max_atr_pct")

    relax_exp = ll.get("relax_min_execute_expectancy_r", 0)
    exec_exp = ll.get("execute_min_expectancy_r", 0)
    if relax_exp > exec_exp:
        errors.append(
            f"live_loop.relax_min_execute_expectancy_r ({relax_exp}) "
            f"must be <= execute_min_expectancy_r ({exec_exp})"
        )

    timeframes = [str(v).strip() for v in ll.get("timeframes", []) if str(v).strip()]
    execute_timeframes = [str(v).strip() for v in ll.get("execute_timeframes", timeframes) if str(v).strip()]
    if execute_timeframes:
        invalid_execute_timeframes = [tf for tf in execute_timeframes if tf not in timeframes]
        if invalid_execute_timeframes:
            errors.append(
                "live_loop.execute_timeframes must be a subset of live_loop.timeframes "
                f"(invalid: {invalid_execute_timeframes})"
            )

    allowed_execution_regimes = [
        str(v).strip().upper() for v in ll.get("allowed_execution_regimes", []) if str(v).strip()
    ]
    invalid_regimes = [regime for regime in allowed_execution_regimes if regime not in _VALID_REGIMES]
    if invalid_regimes:
        errors.append(
            "live_loop.allowed_execution_regimes contains invalid values "
            f"(valid: {sorted(_VALID_REGIMES)}, invalid: {invalid_regimes})"
        )

    return errors


def load_config(config_path: str) -> Dict[str, Any]:
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    errors = validate_config(cfg)
    if errors:
        raise ValueError(
            f"Config validation failed ({len(errors)} errors):\n"
            + "\n".join(f"  - {e}" for e in errors)
        )
    return cfg
