"""SEC EDGAR company facts for US companies: every quarter and year a company has filed, free and
official. Figures are mapped onto the same row shape the Yahoo statements use, so the store merges
them and the page shows one history.
"""
from __future__ import annotations

import gzip
import json
import os
import time
import urllib.request
from datetime import date

# SEC's fair-access policy requires a User-Agent with a contact email, e.g.
# SEC_USER_AGENT="Quorum terminal you@example.com" in server/.env. Without one EDGAR is skipped.
def user_agent() -> str:
    """Read at call time: the server loads .env after this module is imported."""
    return os.environ.get("SEC_USER_AGENT", "")


def enabled() -> bool:
    return "@" in user_agent()
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# Our row fields and the us-gaap tags that carry them, first match wins. Duration facts (income and
# cash flow) and instant facts (balance sheet) are told apart by whether a fact has a start date.
INCOME = {
    "sales": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "opIncome": ["OperatingIncomeLoss"],
    "costOfRevenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization"],
    "netProfit": ["NetIncomeLoss", "ProfitLoss"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "pbt": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "tax": ["IncomeTaxExpenseBenefit"],
    "interest": ["InterestExpense", "InterestExpenseNonoperating"],
    "depreciation": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "Depreciation"],
}
CASH = {
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "cfi": ["NetCashProvidedByUsedInInvestingActivities"],
    "cff": ["NetCashProvidedByUsedInFinancingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "dividendsPaid": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
}
BALANCE = {
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "debtLT": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "debtST": ["DebtCurrent", "LongTermDebtCurrent", "ShortTermBorrowings"],
    "currentLiab": ["LiabilitiesCurrent"],
    "totalLiab": ["Liabilities"],
    "fixedAssets": ["PropertyPlantAndEquipmentNet"],
    "currentAssets": ["AssetsCurrent"],
    "cash": ["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "CashAndCashEquivalentsAtCarryingValue"],
    "receivables": ["AccountsReceivableNetCurrent"],
    "inventory": ["InventoryNet"],
    "payables": ["AccountsPayableCurrent"],
    "totalAssets": ["Assets"],
    "shares": ["CommonStockSharesOutstanding"],
    "reserves": ["RetainedEarningsAccumulatedDeficit"],
    "shareCapital": ["CommonStocksIncludingAdditionalPaidInCapital", "CommonStockValue"],
}

_tickers = {"at": 0.0, "map": {}}


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=40) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return json.loads(data)


def cik_for(symbol: str):
    if time.time() - _tickers["at"] > 86400:
        m = _get(TICKERS_URL)
        _tickers["map"] = {v["ticker"].upper(): int(v["cik_str"]) for v in m.values()}
        _tickers["at"] = time.time()
    s = symbol.upper()
    return _tickers["map"].get(s) or _tickers["map"].get(s.replace(".", "-"))


def _days(f) -> int:
    return (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days


def _latest(facts: dict, tags: list, unit_pref: tuple) -> list:
    """All facts for the first tag present, in any of the preferred units."""
    for tag in tags:
        entry = facts.get(tag)
        if not entry:
            continue
        for unit in unit_pref:
            if unit in entry.get("units", {}):
                return entry["units"][unit]
    return []


def _pick(rows: list, keyfn) -> dict:
    """The most recently filed fact per key."""
    out = {}
    for f in rows:
        k = keyfn(f)
        if k is None:
            continue
        if k not in out or f.get("filed", "") > out[k].get("filed", ""):
            out[k] = f
    return out


def statements(symbol: str) -> dict:
    """{'quarters': [...], 'annual': [...], 'balance': [...], 'cashflow': [...]} in the store's row shape."""
    if not enabled():
        return {"quarters": [], "annual": [], "balance": [], "cashflow": [], "skipped": "SEC_USER_AGENT with a contact email is not set"}
    cik = cik_for(symbol)
    if not cik:
        return {"quarters": [], "annual": [], "balance": [], "cashflow": []}
    facts = _get(FACTS_URL.format(cik=cik)).get("facts", {}).get("us-gaap", {})

    quarters: dict = {}   # 'YYYY-MM' -> row
    annual: dict = {}     # 'YYYY' -> row
    fy_quarters: dict = {}  # (fy, field) -> {fp: value} for the Q4 = FY - Q1 - Q2 - Q3 derivation
    fy_end: dict = {}     # fy -> end date of the 10-K period

    for field, tags in {**INCOME, **CASH}.items():
        units = ("USD/shares",) if field == "eps" else ("USD",)
        rows = _latest(facts, tags, units)
        q = _pick([f for f in rows if f.get("start") and 80 <= _days(f) <= 100 and f.get("fp") in ("Q1", "Q2", "Q3", "Q4")], lambda f: f["end"])
        a = _pick([f for f in rows if f.get("start") and 350 <= _days(f) <= 380 and f.get("fp") == "FY"], lambda f: f["end"])
        for end, f in q.items():
            quarters.setdefault(end[:7], {"period": end[:7]})[field] = f["val"]
            fy_quarters.setdefault((f.get("fy"), field), {})[f["fp"]] = f["val"]
        for end, f in a.items():
            annual.setdefault(end[:4], {"period": end[:4]})[field] = f["val"]
            fy_end[f.get("fy")] = end
            # Companies file no Q4 statement: it is the year less the three quarters already filed.
            qs = fy_quarters.get((f.get("fy"), field), {})
            if all(k in qs for k in ("Q1", "Q2", "Q3")) and "Q4" not in qs and field != "eps":
                quarters.setdefault(end[:7], {"period": end[:7]})[field] = f["val"] - qs["Q1"] - qs["Q2"] - qs["Q3"]

    balance: dict = {}
    for field, tags in BALANCE.items():
        rows = _latest(facts, tags, ("USD", "shares"))
        inst = _pick([f for f in rows if not f.get("start")], lambda f: f["end"])
        for end, f in inst.items():
            if end in fy_end.values():  # the year-end balance sheet, to match the annual rows
                balance.setdefault(end[:4], {"period": end[:4]})[field] = f["val"]

    def finish_income(r: dict) -> dict:
        op, dep, sales, net = r.get("opIncome"), r.get("depreciation"), r.get("sales"), r.get("netProfit")
        ebitda = (op + dep) if op is not None and dep is not None else None
        return {
            "period": r["period"], "sales": sales, "expenses": (sales - ebitda) if sales is not None and ebitda is not None else None,
            "opProfit": ebitda, "ebit": op, "costOfRevenue": r.get("costOfRevenue"), "opm": (ebitda / sales) if sales and ebitda is not None else None, "otherIncome": None,
            "interest": r.get("interest"), "depreciation": dep, "pbt": r.get("pbt"), "tax": r.get("tax"),
            "netProfit": net, "npm": (net / sales) if sales and net is not None else None, "eps": r.get("eps"),
            "dividendsPaid": r.get("dividendsPaid"), "source": "SEC EDGAR",
        }

    def finish_cash(r: dict) -> dict:
        cfo, capex = r.get("cfo"), r.get("capex")
        capex_neg = -abs(capex) if capex is not None else None
        return {"period": r["period"], "cfo": cfo, "cfi": r.get("cfi"), "cff": r.get("cff"), "capex": capex_neg,
                "fcf": (cfo + capex_neg) if cfo is not None and capex_neg is not None else None,
                "dividendsPaid": -abs(r["dividendsPaid"]) if r.get("dividendsPaid") is not None else None, "netChange": None, "source": "SEC EDGAR"}

    def finish_balance(r: dict) -> dict:
        debt = (r.get("debtLT") or 0) + (r.get("debtST") or 0) if r.get("debtLT") is not None or r.get("debtST") is not None else None
        return {"period": r["period"], "equity": r.get("equity"), "shareCapital": r.get("shareCapital"), "reserves": r.get("reserves"),
                "debt": debt, "currentLiab": r.get("currentLiab"), "totalLiab": r.get("totalLiab"), "fixedAssets": r.get("fixedAssets"),
                "investments": None, "currentAssets": r.get("currentAssets"), "cash": r.get("cash"), "receivables": r.get("receivables"),
                "inventory": r.get("inventory"), "payables": r.get("payables"), "totalAssets": r.get("totalAssets"), "shares": r.get("shares"), "source": "SEC EDGAR"}

    return {
        "quarters": [finish_income(r) for k, r in sorted(quarters.items()) if r.get("sales") is not None or r.get("netProfit") is not None],
        "annual": [finish_income(r) for k, r in sorted(annual.items()) if r.get("sales") is not None or r.get("netProfit") is not None],
        "balance": [finish_balance(r) for k, r in sorted(balance.items())],
        "cashflow": [finish_cash(r) for k, r in sorted(annual.items()) if r.get("cfo") is not None],
    }
