import unittest

from src.indicators import ema, rsi


class IndicatorTests(unittest.TestCase):
    def test_ema_output_is_float(self) -> None:
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        out = ema(values, 5)
        self.assertIsInstance(out, float)

    def test_rsi_bounds(self) -> None:
        values = [100, 101, 99, 102, 100, 104, 102, 105, 104, 107, 105, 108, 109, 110, 108]
        out = rsi(values, 14)
        self.assertGreaterEqual(out, 0)
        self.assertLessEqual(out, 100)


if __name__ == "__main__":
    unittest.main()
