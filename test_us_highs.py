import unittest

import pandas as pd

from build_us_highs import cap_tier, evaluate, parse_market_cap


class USHighTests(unittest.TestCase):
    def setUp(self):
        self.dates = pd.bdate_range("2025-09-01", "2026-10-22")
        self.history = pd.DataFrame({"HIGH": 100.0, "CLOSE": 99.0, "VOLUME": 100000}, index=self.dates)
        self.sessions = self.dates[-5:]

    def test_three_strict_highs_with_ties_excluded(self):
        self.history.loc[self.sessions, "HIGH"] = [101, 101, 102, 101, 103]
        result = evaluate(self.history, self.sessions)
        self.assertEqual(result["HIGH_HITS_5D"], 3)
        self.assertEqual(result[f"HIT_{self.sessions[1].date()}"], "N")

    def test_no_highs(self):
        self.assertEqual(evaluate(self.history, self.sessions)["HIGH_HITS_5D"], 0)

    def test_missing_recent_volume_rejected(self):
        self.history.loc[self.sessions[0], "VOLUME"] = float("nan")
        with self.assertRaisesRegex(ValueError, "volume"):
            evaluate(self.history, self.sessions)

    def test_missing_old_volume_preserves_prices(self):
        self.history.iloc[0, self.history.columns.get_loc("VOLUME")] = float("nan")
        self.assertEqual(evaluate(self.history, self.sessions)["HIGH_HITS_5D"], 0)

    def test_missing_session_rejected(self):
        with self.assertRaisesRegex(ValueError, "Missing"):
            evaluate(self.history.drop(self.sessions[1]), self.sessions)

    def test_short_history_rejected(self):
        with self.assertRaisesRegex(ValueError, "Insufficient"):
            evaluate(self.history.iloc[-20:], self.sessions)

    def test_old_high_outside_window_does_not_block(self):
        self.history.iloc[0, self.history.columns.get_loc("HIGH")] = 999
        self.history.loc[self.sessions, "HIGH"] = [101, 102, 103, 104, 105]
        self.assertEqual(evaluate(self.history, self.sessions)["HIGH_HITS_5D"], 5)

    def test_illiquid_rejected(self):
        self.history["VOLUME"] = 10
        with self.assertRaisesRegex(ValueError, "liquidity floor"):
            evaluate(self.history, self.sessions)

    def test_liquidity_reported(self):
        result = evaluate(self.history, self.sessions)
        self.assertEqual(result["AVG_DOLLAR_VOL_20D_USD"], round(99.0 * 100000))

    def test_gain_days_counted(self):
        self.history["CLOSE"] = 99.0
        self.history.loc[self.sessions, "CLOSE"] = [100, 99, 101, 102, 101]
        result = evaluate(self.history, self.sessions)
        self.assertEqual(result["GAIN_DAYS_5D"], 3)

    def test_volume_breakout_flag(self):
        result = evaluate(self.history, self.sessions)
        self.assertEqual(result["VOL_BREAKOUT"], "No")
        self.history.loc[self.sessions[-1], "VOLUME"] = 300000
        flagged = evaluate(self.history, self.sessions)
        self.assertEqual(flagged["VOL_BREAKOUT"], "Yes")
        self.assertGreaterEqual(flagged["VOL_RATIO"], 1.5)

    def test_cap_tiers(self):
        self.assertEqual(cap_tier(250e9), "MEGA")
        self.assertEqual(cap_tier(50e9), "LARGE")
        self.assertEqual(cap_tier(5e9), "MID")
        self.assertEqual(cap_tier(1e9), "BELOW_MID")

    def test_parse_market_cap(self):
        self.assertEqual(parse_market_cap("5,114,261,000,000"), 5114261000000.0)
        self.assertEqual(parse_market_cap("N/A"), 0.0)
        self.assertEqual(parse_market_cap(""), 0.0)


if __name__ == "__main__":
    unittest.main()