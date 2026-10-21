"""Annual ratios from stored statements, with explicit missing-data handling.

Average-balance ratios require the preceding fiscal year's balance sheet; a gap
is never silently treated as a one-year period. Sources follow the input rows.
Values are fractions for percentages, multiples for ratios, and days for cycles.
"""
from __future__ import annotations

import math


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _ratio(numerator, denominator):
    a, b = _number(numerator), _number(denominator)
    return a / b if a is not None and b is not None and b > 0 else None


def _difference(a, b):
    a, b = _number(a), _number(b)
    return a - b if a is not None and b is not None else None


def history(annual: list, balance: list, cashflow: list) -> list:
    incomes = {r["period"]: r for r in annual if str(r.get("period", "")).isdigit() and len(str(r["period"])) == 4}
    balances = {r["period"]: r for r in balance if r.get("period")}
    cash = {r["period"]: r for r in cashflow if r.get("period")}
    result = []
    for period, income in sorted(incomes.items()):
        close = balances.get(period, {})
        opening = balances.get(str(int(period) - 1), {})
        flow = cash.get(period, {})

        def average(key):
            a, b = _number(close.get(key)), _number(opening.get(key))
            return (a + b) / 2 if a is not None and b is not None else None

        sales = income.get("sales")
        cogs = income.get("costOfRevenue")
        ebit = income.get("ebit")
        # Do not call EBITDA EBIT. SEC rows expose operating income explicitly.
        if ebit is None:
            ebit = income.get("opIncome")
        debtors = _ratio(average("receivables"), sales)
        inventory = _ratio(average("inventory"), cogs)
        payables = _ratio(average("payables"), cogs)
        debtor_days = debtors * 365 if debtors is not None else None
        inventory_days = inventory * 365 if inventory is not None else None
        payable_days = payables * 365 if payables is not None else None
        working = _difference(close.get("currentAssets"), close.get("currentLiab"))
        working_days = _ratio(working, sales)
        avg_capital = _difference(average("totalAssets"), average("currentLiab"))
        current_assets_ex_inventory = _difference(close.get("currentAssets"), close.get("inventory"))
        sources = sorted({r.get("source") or "Yahoo Finance" for r in (income, close, opening, flow) if r})
        result.append({
            "period": period,
            "roe": _ratio(income.get("netProfit"), average("equity")),
            "roce": _ratio(ebit, avg_capital),
            "debtToEquity": _ratio(close.get("debt"), close.get("equity")),
            "currentRatio": _ratio(close.get("currentAssets"), close.get("currentLiab")),
            "quickRatio": _ratio(current_assets_ex_inventory, close.get("currentLiab")),
            "debtorDays": debtor_days,
            "inventoryDays": inventory_days,
            "payableDays": payable_days,
            "cashConversionDays": debtor_days + inventory_days - payable_days if all(v is not None for v in (debtor_days, inventory_days, payable_days)) else None,
            "workingCapitalDays": working_days * 365 if working_days is not None else None,
            "cashToProfit": _ratio(flow.get("cfo"), income.get("netProfit")),
            "fcfMargin": _ratio(flow.get("fcf"), sales),
            "interestCoverage": _ratio(ebit, income.get("interest")),
            "sources": sources,
            "hasOpeningBalance": bool(opening),
        })
    return result
