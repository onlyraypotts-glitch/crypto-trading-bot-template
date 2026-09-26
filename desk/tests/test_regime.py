import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
from regime_snapshot import classify  # noqa: E402


class RegimeTest(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(classify(None, None, None, None), "UNKNOWN")
        self.assertEqual(classify(-9, 50, 0, 0), "CRASH")
        self.assertEqual(classify(0, 8, 0, 0), "CRASH")
        self.assertEqual(classify(-4, 50, 0, 0), "RISK_OFF")
        self.assertEqual(classify(1, 50, -1, 0), "RISK_OFF")
        self.assertEqual(classify(1, 90, 0.5, 0.0006), "EUPHORIA")
        self.assertEqual(classify(3, 60, 0.2, 0.0001), "RISK_ON")
        self.assertEqual(classify(0.5, 74, 0.6, 0), "NEUTRAL")


if __name__ == "__main__":
    unittest.main()
