import unittest
from unittest.mock import patch

from server import qnse


class NseBasisTests(unittest.TestCase):
    def listing(self):
        return [
            {"period": "Quarterly", "toDate": "31-Mar-2025", "consolidated": "Consolidated", "params": "cons", "filingDate": "2025-05-01"},
            {"period": "Quarterly", "toDate": "31-Mar-2025", "consolidated": "Non-Consolidated", "params": "stand", "filingDate": "2025-05-02"},
            {"period": "Quarterly", "toDate": "31-Mar-2025", "consolidated": "", "params": "unknown", "filingDate": "2025-05-03"},
        ]

    def read(self, path):
        if "financial-results?" in path:
            return self.listing()
        value = 10 if "params=cons" in path else 5
        return {"resultsData2": {"re_net_sale": value, "re_pl_own_par": 0, "re_basic_eps": 0}}

    def test_explicit_basis_keeps_series_separate(self):
        with patch.object(qnse, "_get", side_effect=self.read):
            consolidated = qnse._results("EXAMPLE", "Quarterly", "consolidated")
            standalone = qnse._results("EXAMPLE", "Quarterly", "standalone")
        self.assertEqual(consolidated[0]["sales"], 10 * qnse.LAKH)
        self.assertEqual(standalone[0]["sales"], 5 * qnse.LAKH)
        self.assertEqual(consolidated[0]["basis"], "consolidated")
        self.assertEqual(standalone[0]["basis"], "standalone")
        self.assertEqual(standalone[0]["filedAt"], "2025-05-02")
        self.assertTrue(standalone[0]["sourceUrl"].startswith(qnse.BASE))

    def test_missing_requested_basis_is_not_replaced(self):
        with patch.object(qnse, "_get", return_value=[self.listing()[0]]):
            self.assertEqual(qnse._results("EXAMPLE", "Quarterly", "standalone"), [])

    def test_unknown_labels_are_not_assumed_standalone(self):
        with patch.object(qnse, "_get", return_value=[self.listing()[2]]):
            self.assertEqual(qnse._results("EXAMPLE", "Quarterly"), [])

    def test_zero_profit_and_eps_survive(self):
        row = qnse._income_row({"resultsData2": {"re_net_sale": 10, "re_pl_own_par": 0, "re_proloss_ord_act": 6, "re_basic_eps": 0}}, "2025")
        self.assertEqual(row["netProfit"], 0)
        self.assertEqual(row["eps"], 0)

    def test_bad_basis_rejected_before_network(self):
        with self.assertRaises(ValueError):
            qnse.income_statements("EXAMPLE", "unknown")


if __name__ == "__main__":
    unittest.main()
