import unittest

from src.ml_pipeline import LogisticBinaryClassifier, MLWalkForwardOptimizer, StandardScaler


class MLPipelineTests(unittest.TestCase):
    def test_logistic_classifier_learns_simple_boundary(self) -> None:
        x = [
            [0.0, 0.0],
            [0.1, -0.1],
            [1.0, 1.2],
            [1.1, 0.9],
        ]
        y = [0, 0, 1, 1]

        scaler = StandardScaler()
        scaler.fit(x)
        xs = scaler.transform(x)

        model = LogisticBinaryClassifier(learning_rate=0.1, epochs=300, l2=0.0)
        model.fit(xs, y)
        probs = model.predict_proba(xs)

        self.assertLess(probs[0], 0.5)
        self.assertLess(probs[1], 0.5)
        self.assertGreater(probs[2], 0.5)
        self.assertGreater(probs[3], 0.5)

    def test_trade_cost_r_positive_when_fees_present(self) -> None:
        optimizer = MLWalkForwardOptimizer(risk_usd=1.0, fee_bps_per_side=2.0, slippage_bps_per_side=1.0)
        cost_r = optimizer.trade_cost_r(entry=100.0, stop_loss=99.0)
        self.assertGreater(cost_r, 0.0)


if __name__ == "__main__":
    unittest.main()
