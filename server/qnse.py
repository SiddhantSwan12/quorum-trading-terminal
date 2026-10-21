"""NSE's own filings for Indian companies: quarterly and annual results back to about 2010, and the
promoter / public shareholding by quarter. Unofficial: the site serves these to a browser, so the
client impersonates one, paces itself, and backs off for ten minutes when refused. Figures arrive
in rupee lakh and are stored in rupees, in the same row shape as the Yahoo statements.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from urllib.parse import quote

log = logging.getLogger("quorum.nse")

try:
    from curl_cffi import requests as cr
    HAS_CLIENT = True
except Exception:  # pragma: no cover
    cr = None
    HAS_CLIENT = False

BASE = "https://www.nseindia.com"
HEADERS = {"Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/"}
LAKH = 1e5
PAUSE = 1.0          # between calls
BLOCK_PAUSE = 600    # after a refusal
MAX_DETAILS = 60     # quarters fetched per company on first load

_lock = threading.Lock()
_session = None
_primed_at = 0.0
_blocked_until = 0.0


def available() -> bool:
    return HAS_CLIENT and time.time() >= _blocked_until


def _get(path: str):
    """One call through a primed, Chrome-impersonating session; None when refused."""
    global _session, _primed_at, _blocked_until
    if not available():
        return None
    with _lock:
        if _session is None or time.time() - _primed_at > 600:
            _session = cr.Session(impersonate="chrome")
            _session.get(BASE + "/", headers=HEADERS, timeout=30)
            _primed_at = time.time()
            time.sleep(PAUSE)
        r = _session.get(BASE + path, headers=HEADERS, timeout=30)
        time.sleep(PAUSE)
    if r.status_code in (401, 403, 429):
        _blocked_until = time.time() + BLOCK_PAUSE
        log.info("NSE refused %s (%s); pausing %ss", path, r.status_code, BLOCK_PAUSE)
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def _num(v):
    try:
        return float(v) * LAKH if v not in (None, "", "-") else None
    except (TypeError, ValueError):
        return None


def _period(end: str, quarterly: bool) -> str:
    d = datetime.strptime(end, "%d-%b-%Y")
    return d.strftime("%Y-%m") if quarterly else d.strftime("%Y")


def _income_row(d: dict, period: str) -> dict | None:
    r = d.get("resultsData2") or {}
    sales = _num(r.get("re_net_sale"))
    if sales is None:
        return None
    other = _num(r.get("re_oth_inc_new"))
    total_exp = _num(r.get("re_oth_tot_exp"))
    interest = _num(r.get("re_int_new"))
    dep = _num(r.get("re_depr_und_exp"))
    # Operating profit as Screener shows it: sales less operating expenses, before interest,
    # depreciation and other income.
    op = None
    if total_exp is not None:
        op = sales - (total_exp - (interest or 0) - (dep or 0))
    net = next((v for v in (_num(r.get("re_pl_own_par")), _num(r.get("re_proloss_ord_act")), _num(r.get("re_con_pro_loss"))) if v is not None), None)
    eps = None
    for k in ("re_basic_eps_for_cont_dic_opr", "re_dil_eps_bfr_exi", "re_basic_eps"):
        try:
            raw = r.get(k)
            if raw in (None, "", "-"):
                continue
            v = float(raw)
        except (TypeError, ValueError):
            v = 0
        eps = v
        break
    return {
        "period": period, "sales": sales, "expenses": (sales - op) if op is not None else None, "opProfit": op,
        "opm": (op / sales) if op is not None and sales else None, "otherIncome": other, "interest": interest, "depreciation": dep,
        "pbt": _num(r.get("re_pro_loss_bef_tax")), "tax": _num(r.get("re_tax")), "netProfit": net,
        "npm": (net / sales) if net is not None and sales else None, "eps": eps, "dividendsPaid": None, "source": "NSE",
    }


def _basis(value) -> str | None:
    label = str(value or "").strip().casefold().replace("_", "-")
    if label == "consolidated":
        return "consolidated"
    if label in {"non-consolidated", "non consolidated", "standalone"}:
        return "standalone"
    return None


def _results(code: str, period: str, basis: str | None = None) -> list:
    """Read one reported basis; an explicit basis never falls back to the other."""
    if basis not in {None, "consolidated", "standalone"}:
        raise ValueError("basis must be consolidated or standalone")
    code = quote(code, safe="")
    listing = _get(f"/api/corporates-financial-results?index=equities&symbol={code}&period={period}")
    if not isinstance(listing, list):
        return []
    quarterly = period == "Quarterly"
    rows = [f for f in listing if f.get("period") == period and f.get("cumulative", "Non-cumulative") != "Cumulative" and f.get("params")]
    cons = [f for f in rows if _basis(f.get("consolidated")) == "consolidated"]
    if basis is not None:
        chosen = [f for f in rows if _basis(f.get("consolidated")) == basis]
    else:
        chosen = cons or [f for f in rows if _basis(f.get("consolidated")) == "standalone"]
    # One filing per period end, the latest filed; newest periods first so a partial fetch still helps.
    by_end: dict = {}
    for f in chosen:
        k = f["toDate"]
        if k not in by_end or f.get("filingDate", "") > by_end[k].get("filingDate", ""):
            by_end[k] = f
    ordered = sorted(by_end.values(), key=lambda f: datetime.strptime(f["toDate"], "%d-%b-%Y"), reverse=True)[:MAX_DETAILS]
    out = []
    for f in ordered:
        d = _get(f"/api/corporates-financial-results-data?index=equities&params={f['params']}&seq_id={f.get('seqNumber', '')}&industry=-&frOldNewFlag={f.get('oldNewFlag', 'N')}&ind=N&format={f.get('format', 'New')}")
        if not isinstance(d, dict):
            break  # refused or blocked: keep what we have
        row = _income_row(d, _period(f["toDate"], quarterly))
        if row:
            row.update(basis=_basis(f.get("consolidated")), periodEnd=f["toDate"], filedAt=f.get("filingDate"),
                       sourceUrl=f"{BASE}/companies-listing/corporate-filings-financial-results?symbol={code}")
            out.append(row)
    return sorted(out, key=lambda r: r["period"])


def holding(code: str) -> list:
    """[{period: 'YYYY-MM', promoters: 0.5048, public: 0.4952}] by quarter, oldest first."""
    rows = _get(f"/api/corporate-share-holdings-master?index=equities&symbol={code}")
    if not isinstance(rows, list):
        return []
    out = {}
    for r in rows:
        try:
            d = datetime.strptime(r["date"], "%d-%b-%Y")
            p = float(r["pr_and_prgrp"]) / 100
            pub = float(r["public_val"]) / 100
        except (KeyError, TypeError, ValueError):
            continue
        k = d.strftime("%Y-%m")
        if k not in out or (r.get("submissionDate") or "") > out[k][2]:
            out[k] = (p, pub, r.get("submissionDate") or "")
    return [{"period": k, "promoters": v[0], "public": v[1]} for k, v in sorted(out.items())]


def statements(code: str) -> dict:
    return {"quarters": _results(code, "Quarterly"), "annual": _results(code, "Annual"), "holding": holding(code)}


def income_statements(code: str, basis: str) -> dict:
    """Separate P&L series for explicit-basis requests; never writes the blended store."""
    if basis not in {"consolidated", "standalone"}:
        raise ValueError("basis must be consolidated or standalone")
    quarters = _results(code, "Quarterly", basis)
    annual = _results(code, "Annual", basis)
    return {"basis": basis, "source": "NSE", "quarters": quarters, "annual": annual}
