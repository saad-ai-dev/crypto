import unittest

from src.config import validate_config


def _valid_config():
    return {
        "account": {"starting_balance_usd": 10.0, "risk_per_trade_pct": 0.02},
        "execution": {"fee_bps_per_side": 2, "slippage_bps_per_side": 1},
        "strategy": {
            "ema_fast": 21, "ema_slow": 55, "rsi_period": 14, "atr_period": 14,
            "atr_multiplier": 1.5, "risk_reward": 2.0,
            "min_atr_pct": 0.001, "max_atr_pct": 0.03,
            "funding_abs_limit": 0.001, "min_confidence": 0.6,
            "long_rsi_min": 55, "long_rsi_max": 72,
            "short_rsi_min": 28, "short_rsi_max": 48,
        },
        "live_loop": {
            "symbols": ["BTCUSDT"], "timeframes": ["5m"],
            "lookback_candles": 220, "poll_seconds": 12,
            "execute_min_confidence": 0.86,
            "execute_min_expectancy_r": 0.12,
            "execute_min_score": 0.66,
            "relax_min_execute_expectancy_r": 0.08,
        },
    }


class ConfigValidationTests(unittest.TestCase):

    def test_valid_config_passes(self) -> None:
        errors = validate_config(_valid_config())
        self.assertEqual(errors, [])

    def test_missing_section_detected(self) -> None:
        cfg = _valid_config()
        del cfg["strategy"]
        errors = validate_config(cfg)
        self.assertTrue(any("strategy" in e for e in errors))

    def test_missing_strategy_key_detected(self) -> None:
        cfg = _valid_config()
        del cfg["strategy"]["ema_fast"]
        errors = validate_config(cfg)
        self.assertTrue(any("ema_fast" in e for e in errors))

    def test_rsi_min_gte_max_detected(self) -> None:
        cfg = _valid_config()
        cfg["strategy"]["long_rsi_min"] = 80
        cfg["strategy"]["long_rsi_max"] = 70
        errors = validate_config(cfg)
        self.assertTrue(any("long_rsi_min" in e for e in errors))

    def test_atr_min_gte_max_detected(self) -> None:
        cfg = _valid_config()
        cfg["strategy"]["min_atr_pct"] = 0.05
        cfg["strategy"]["max_atr_pct"] = 0.01
        errors = validate_config(cfg)
        self.assertTrue(any("min_atr_pct" in e for e in errors))

    def test_relax_floor_above_initial_detected(self) -> None:
        cfg = _valid_config()
        cfg["live_loop"]["relax_min_execute_expectancy_r"] = 0.20
        cfg["live_loop"]["execute_min_expectancy_r"] = 0.12
        errors = validate_config(cfg)
        self.assertTrue(any("relax_min_execute_expectancy_r" in e for e in errors))

    def test_non_positive_paper_risk_usd_detected(self) -> None:
        cfg = _valid_config()
        cfg["account"]["paper_risk_usd"] = 0.0
        errors = validate_config(cfg)
        self.assertTrue(any("paper_risk_usd" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
