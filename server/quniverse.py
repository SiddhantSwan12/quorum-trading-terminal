"""The companies the screener and the peer tables know: the Nifty 500 from NSE's own constituent
file and the S&P 500 from Wikipedia's constituents table. Both are public; both are refreshed
weekly and kept in the store so a restart needs no network.
"""
from __future__ import annotations

import io
import logging
import urllib.request

import pandas as pd

import qstore

log = logging.getLogger("quorum.universe")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
    "Accept": "text/csv,text/html,*/*",
}
NIFTY500_URLS = [
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
]
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WEEK = 7 * 24 * 3600


def _fetch(url: str) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=40).read()


def fetch_nifty500() -> list:
    last = None
    for url in NIFTY500_URLS:
        try:
            df = pd.read_csv(io.BytesIO(_fetch(url)))
            out = []
            for r in df.to_dict("records"):
                code = str(r.get("Symbol", "")).strip()
                if not code or str(r.get("Series", "EQ")).strip() not in ("EQ", "BE"):
                    continue
                industry = str(r.get("Industry", "")).strip()
                out.append({"symbol": f"{code}.NS", "code": code, "name": str(r.get("Company Name", code)).strip().removesuffix(" Ltd.").removesuffix(" Ltd").removesuffix(" Limited"),
                            "exchange": "NSE", "market": "IN", "sector": industry, "industry": industry, "idx": "Nifty 500"})
            if out:
                return out
        except Exception as exc:  # try the next mirror
            last = exc
    raise RuntimeError(f"Nifty 500 list unavailable: {last}")


def fetch_sp500() -> list:
    tables = pd.read_html(io.BytesIO(_fetch(SP500_URL)))
    df = tables[0]
    out = []
    for r in df.to_dict("records"):
        code = str(r.get("Symbol", "")).strip()
        if not code:
            continue
        symbol = code.replace(".", "-")  # Wikipedia writes BRK.B, Yahoo wants BRK-B
        out.append({"symbol": symbol, "code": symbol, "name": str(r.get("Security", code)).strip(),
                    "exchange": "", "market": "US", "sector": str(r.get("GICS Sector", "")).strip(),
                    "industry": str(r.get("GICS Sub-Industry", "")).strip(), "idx": "S&P 500"})
    if not out:
        raise RuntimeError("S&P 500 table was empty")
    return out


def load(force: bool = False) -> list:
    """The universe from the store, refreshed from the sources when a week old or missing."""
    age = qstore.universe_age()
    if not force and age is not None and age < WEEK:
        return qstore.universe_rows()
    rows = []
    for name, fn in (("Nifty 500", fetch_nifty500), ("S&P 500", fetch_sp500)):
        try:
            got = fn()
            log.info("%s: %d companies", name, len(got))
            rows.extend(got)
        except Exception as exc:
            log.warning("%s list not refreshed: %s", name, exc)
    if rows:
        qstore.upsert_universe(rows)
    return qstore.universe_rows()
