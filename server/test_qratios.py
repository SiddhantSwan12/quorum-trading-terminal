import unittest

from server.qratios import history


class RatioHistoryTests(unittest.TestCase):
    def statements(self):
        annual = [{"period": "2025", "sales": 1000, "costOfRevenue": 600, "ebit": 150, "interest": 30, "netProfit": 100}]
        balance = [
            {"period": "2024", "equity": 400, "totalAssets": 800, "currentAssets": 300, "currentLiab": 100, "inventory": 100, "receivables": 80, "payables": 60},
            {"period": "2025", "equity": 600, "totalAssets": 1200, "currentAssets": 500, "currentLiab": 200, "inventory": 140, "receivables": 120, "payables": 100, "debt": 120},
        ]
        cash = [{"period": "2025", "cfo": 130, "fcf": 70}]
        return annual, balance, cash

    def test_formulas_reconcile(self):
        row = history(*self.statements())[0]
        self.assertAlmostEqual(row["roe"], .2)
        self.assertAlmostEqual(row["roce"], 150 / 850)
        self.assertAlmostEqual(row["currentRatio"], 2.5)
        self.assertAlmostEqual(row["quickRatio"], 1.8)
        self.assertAlmostEqual(row["debtorDays"], 36.5)
        self.assertAlmostEqual(row["inventoryDays"], 73)
        self.assertAlmostEqual(row["payableDays"], 80 / 600 * 365)
        self.assertAlmostEqual(row["cashConversionDays"], 36.5 + 73 - 80 / 600 * 365)
        self.assertAlmostEqual(row["workingCapitalDays"], 109.5)
        self.assertAlmostEqual(row["cashToProfit"], 1.3)
        self.assertAlmostEqual(row["fcfMargin"], .07)
        self.assertAlmostEqual(row["interestCoverage"], 5)

    def test_gap_does_not_use_old_balance_as_opening(self):
        annual, balance, cash = self.statements()
        balance[0]["period"] = "2023"
        row = history(annual, balance, cash)[0]
        self.assertIsNone(row["roe"])
        self.assertIsNone(row["roce"])
        self.assertIsNone(row["debtorDays"])
        self.assertFalse(row["hasOpeningBalance"])
        self.assertEqual(row["currentRatio"], 2.5)

    def test_missing_cogs_does_not_substitute_operating_expenses(self):
        annual, balance, cash = self.statements()
        del annual[0]["costOfRevenue"]
        annual[0]["expenses"] = 850
        row = history(annual, balance, cash)[0]
        self.assertIsNone(row["inventoryDays"])
        self.assertIsNone(row["payableDays"])
        self.assertIsNone(row["cashConversionDays"])
        self.assertEqual(row["debtorDays"], 36.5)

    def test_ebitda_is_not_treated_as_ebit(self):
        annual, balance, cash = self.statements()
        del annual[0]["ebit"]
        annual[0]["opProfit"] = 200
        row = history(annual, balance, cash)[0]
        self.assertIsNone(row["roce"])
        self.assertIsNone(row["interestCoverage"])

    def test_zero_is_not_missing_and_nonpositive_baselines_are_unavailable(self):
        annual, balance, cash = self.statements()
        annual[0].update(netProfit=0, interest=0)
        cash[0].update(fcf=0)
        balance[1]["debt"] = 0
        row = history(annual, balance, cash)[0]
        self.assertEqual(row["roe"], 0)
        self.assertEqual(row["debtToEquity"], 0)
        self.assertEqual(row["fcfMargin"], 0)
        self.assertIsNone(row["cashToProfit"])
        self.assertIsNone(row["interestCoverage"])

    def test_sources_are_kept_and_nonfinite_values_rejected(self):
        annual, balance, cash = self.statements()
        annual[0].update(source="NSE", sales=float("nan"))
        balance[0]["source"] = "SEC EDGAR"
        row = history(annual, balance, cash)[0]
        self.assertEqual(row["sources"], ["NSE", "SEC EDGAR", "Yahoo Finance"])
        self.assertIsNone(row["debtorDays"])
        self.assertIsNone(row["fcfMargin"])


if __name__ == "__main__":
    unittest.main()
