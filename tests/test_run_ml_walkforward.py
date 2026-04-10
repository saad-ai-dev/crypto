import tempfile
import unittest
from pathlib import Path

from run_ml_walkforward import list_missing_cache_files, should_apply_candidate_over_baseline
from src.ml_pipeline import WalkForwardResult


class RunMLWalkforwardTests(unittest.TestCase):
    def test_list_missing_cache_files_reports_required_market_cache_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            missing = list_missing_cache_files(str(base), ["BTCUSDT"], ["5m", "15m"])

        self.assertEqual(
            missing,
            [
                str(base / "BTCUSDT_premium.json"),
                str(base / "BTCUSDT_open_interest.json"),
                str(base / "BTCUSDT_5m_klines.json"),
                str(base / "BTCUSDT_15m_klines.json"),
            ],
        )

    def test_candidate_must_clear_baseline_to_be_applied(self) -> None:
        baseline = WalkForwardResult(
            strategy={"risk_reward": 1.0},
            tested_signals=100,
            total_selected_trades=20,
            wins=12,
            losses=8,
            win_rate=0.60,
            expectancy_r=0.08,
            folds=[],
            per_market=[],
            tested_thresholds=[],
        )
        candidate = WalkForwardResult(
            strategy={"risk_reward": 1.5},
            tested_signals=100,
            total_selected_trades=20,
            wins=12,
            losses=8,
            win_rate=0.60,
            expectancy_r=0.085,
            folds=[],
            per_market=[],
            tested_thresholds=[],
        )

        self.assertFalse(should_apply_candidate_over_baseline(candidate, baseline))

    def test_candidate_is_applied_when_expectancy_improves_clearly(self) -> None:
        baseline = WalkForwardResult(
            strategy={"risk_reward": 1.0},
            tested_signals=100,
            total_selected_trades=20,
            wins=12,
            losses=8,
            win_rate=0.60,
            expectancy_r=0.08,
            folds=[],
            per_market=[],
            tested_thresholds=[],
        )
        candidate = WalkForwardResult(
            strategy={"risk_reward": 1.5},
            tested_signals=100,
            total_selected_trades=24,
            wins=14,
            losses=10,
            win_rate=0.59,
            expectancy_r=0.11,
            folds=[],
            per_market=[],
            tested_thresholds=[],
        )

        self.assertTrue(should_apply_candidate_over_baseline(candidate, baseline))


if __name__ == "__main__":
    unittest.main()
