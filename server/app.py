"""Quorum bridge: Yahoo Finance bars and TradingAgents runs for the terminal.

Run from the project root:

    pip install -r server/requirements.txt
    pip install -e TradingAgents          # optional: enables live agent runs
    uvicorn server.app:app --port 8000 --reload

The Vite dev server proxies /api to this process. Every endpoint fails soft on the client side:
without this server the terminal keeps its simulated feed and sample runs.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import logging
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional
    def load_dotenv(*_a, **_k):
        return False
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qstore  # noqa: E402
import quniverse  # noqa: E402
import qedgar  # noqa: E402
import qnse  # noqa: E402
import qkronos  # noqa: E402
import qdocuments  # noqa: E402
import qalerts  # noqa: E402
import qratios  # noqa: E402
import qreports  # noqa: E402

load_dotenv(Path(__file__).with_name(".env"))
load_dotenv(Path(__file__).resolve().parent.parent / "TradingAgents" / ".env")

DATA_DIR = Path(os.environ.get('QUORUM_DATA_DIR') or Path(__file__).with_name('data'))
qstore.init(DATA_DIR / 'quorum.db')
ALLOWED_ORIGINS = [origin.strip().rstrip('/') for origin in os.environ.get(
    'QUORUM_ALLOWED_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173'
).split(',') if origin.strip()]

log = logging.getLogger("quorum")
app = FastAPI(title="Quorum bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOCUMENTS = qdocuments.install(app, DATA_DIR / 'documents')
qreports.install(app)

try:
    import yfinance as yf
    HAS_FEED = True
except Exception:  # pragma: no cover - optional dependency
    yf = None
    HAS_FEED = False

try:
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.agents.rating import run_rating
    HAS_TA = True
except Exception:  # pragma: no cover - optional dependency
    HAS_TA = False

# Any one of these keys lets TradingAgents call a model. The CLI reads the same variables.
LLM_KEYS = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY", "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY", "ZHIPU_API_KEY", "MINIMAX_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY",
    "MOONSHOT_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY", "OPENAI_COMPATIBLE_API_KEY",
]

# Each market's clock and hours, matching terminal/src/data/universe.js.
MARKETS = {
    "US": {"tz": ZoneInfo("America/New_York"), "open": (9, 30), "close": (16, 0)},
    "IN": {"tz": ZoneInfo("Asia/Kolkata"), "open": (9, 15), "close": (15, 30)},
}


def market_of(symbol: str) -> str:
    return "IN" if symbol.upper().endswith((".NS", ".BO")) else "US"


# Yahoo's exchange codes to the names the terminal prints.
EXCHANGES = {"NSI": "NSE", "BSE": "BSE", "NMS": "Nasdaq", "NGM": "Nasdaq", "NCM": "Nasdaq", "NYQ": "NYSE", "PCX": "NYSE Arca", "ASE": "NYSE American", "BTS": "Cboe BZX"}
ALLOWED_EXCHANGES = set(EXCHANGES)


def exchange_name(code: str) -> str:
    return EXCHANGES.get(code, code or "")


def code_of(symbol: str) -> str:
    """The ticker without Yahoo's suffix: RELIANCE.NS -> RELIANCE."""
    return symbol.split(".")[0] if market_of(symbol) == "IN" else symbol


def has_llm() -> bool:
    return any(os.environ.get(k) for k in LLM_KEYS) or os.environ.get("TRADINGAGENTS_LLM_PROVIDER") in {"ollama", "chatgpt"}


@app.get("/api/health")
def health():
    wait = BREAKER["until"] - time.time()
    return {
        "tradingagents": HAS_TA,
        "llm": HAS_TA and has_llm(),
        "feed": HAS_FEED,
        "kronos": qkronos.available(),
        "provider": os.environ.get("TRADINGAGENTS_LLM_PROVIDER") or ("configured" if has_llm() else None),
        "yahooBlockedFor": int(wait) if wait > 0 else 0,
    }


# ---------------------------------------------------------------- market data

# Yahoo calls are cached per symbol and part, so a terminal refreshing 46 symbols every minute
# re-reads only the five-minute bars; the year of daily history and the week of half-hours are kept
# for a while. Each entry is (fetched_at, frame).
CACHE: dict[tuple[str, str], tuple[float, object]] = {}
CACHE_LOCK = threading.Lock()
TTL = {"daily": 1800, "intra": 115, "week": 900, "fx": 600, "search": 3600, "profile": 21600, "fundamentals": 21600, "edgar": 86400, "quote": 60,
       "bars-1m": 60, "bars-5m": 90, "bars-15m": 120, "bars-30m": 180, "bars-1h": 300, "bars-1d": 1800, "bars-1wk": 3600, "bars-1mo": 3600}


BREAKER = {"until": 0.0, "last": None}
BREAKER_PAUSE = 300


def _is_rate_limit(exc) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "429" in text or "too many" in text


def yahoo_guard():
    """Raise 503 at once while Yahoo's block is thought to be in force."""
    wait = BREAKER["until"] - time.time()
    if wait > 0:
        raise HTTPException(503, f"Yahoo Finance rate-limited this machine; retrying in {int(wait)}s")


def _call_yahoo(fetch):
    """Run one Yahoo call behind the breaker."""
    yahoo_guard()
    try:
        return fetch()
    except Exception as exc:
        if _is_rate_limit(exc):
            BREAKER["until"] = time.time() + BREAKER_PAUSE
            BREAKER["last"] = datetime.utcnow().isoformat()
            raise HTTPException(503, f"Yahoo Finance rate-limited this machine; retrying in {BREAKER_PAUSE}s") from exc
        raise


def _cached(symbol: str, part: str, fetch):
    key = (symbol, part)
    now = time.time()
    with CACHE_LOCK:
        hit = CACHE.get(key)
    if hit and now - hit[0] < (TTL.get(part) or TTL[part.split(":")[0]]):
        return hit[1]
    frame = fetch() if part == "edgar" else _call_yahoo(fetch)
    with CACHE_LOCK:
        CACHE[key] = (now, frame)
    return frame


def _bars(symbol: str):
    """Daily history for a year, today's five-minute bars and the last week's half-hour closes."""
    t = yf.Ticker(symbol)
    daily = _cached(symbol, "daily", lambda: t.history(period="1y", interval="1d", auto_adjust=False))
    if daily.empty:
        raise HTTPException(404, f"Yahoo Finance has no history for {symbol}")
    mk = market_of(symbol)
    tz = MARKETS[mk]["tz"]
    now = datetime.now(tz)
    # Today's bars, or the last session's when the market is closed.
    intra = _cached(symbol, "intra", lambda: t.history(period="5d", interval="5m", auto_adjust=False, prepost=False))
    week = _cached(symbol, "week", lambda: t.history(period="7d", interval="30m", auto_adjust=False, prepost=False))
    daily_rows = [
        {"date": idx.strftime("%Y-%m-%d"), "o": round(float(r.Open), 2), "h": round(float(r.High), 2),
         "l": round(float(r.Low), 2), "c": round(float(r.Close), 2), "v": int(r.Volume)}
        for idx, r in daily.iterrows()
    ]
    session_date = None
    bars = []
    if not intra.empty:
        intra = intra.tz_convert(tz)
        session_date = intra.index[-1].strftime("%Y-%m-%d")
        for idx, r in intra[intra.index.strftime("%Y-%m-%d") == session_date].iterrows():
            bars.append({"t": idx.hour * 60 + idx.minute, "o": round(float(r.Open), 2), "h": round(float(r.High), 2),
                         "l": round(float(r.Low), 2), "c": round(float(r.Close), 2), "v": int(r.Volume)})
    # The prior close is the last daily close before the session shown.
    prev_rows = [d for d in daily_rows if session_date is None or d["date"] < session_date]
    prev_close = prev_rows[-1]["c"] if prev_rows else daily_rows[-1]["c"]
    history = prev_rows if session_date else daily_rows[:-1]
    week30 = {}
    if not week.empty:
        week = week.tz_convert(tz)
        for day, grp in week.groupby(week.index.strftime("%Y-%m-%d")):
            if session_date and day >= session_date:
                continue
            week30[day] = {"closes": [round(float(c), 2) for c in grp.Close], "vols": [int(v) for v in grp.Volume]}
    open_h, open_m = MARKETS[mk]["open"]
    close_h, close_m = MARKETS[mk]["close"]
    live_sec = now.hour * 3600 + now.minute * 60 + now.second
    if session_date and session_date != now.strftime("%Y-%m-%d"):
        live_sec = close_h * 3600 + close_m * 60  # a past session is shown complete
    return {
        "symbol": symbol, "market": mk, "sessionDate": session_date or now.strftime("%Y-%m-%d"),
        "liveSec": live_sec, "prevClose": prev_close, "daily": history, "bars": bars, "week30": week30,
        "hours": {"open": open_h * 60 + open_m, "close": close_h * 60 + close_m},
    }


@app.get("/api/market/{symbol}")
def market(symbol: str):
    if not HAS_FEED:
        raise HTTPException(503, "yfinance is not installed")
    try:
        return _bars(symbol)
    except HTTPException:
        raise
    except Exception as exc:  # network, parsing
        raise HTTPException(502, f"Yahoo Finance failed for {symbol}: {exc}") from exc


# The chart's intervals: Yahoo's name and the period that fits it (Yahoo caps 1m at 7 days, other intraday at 60 days).
INTERVALS = {"1m": ("1m", "5d"), "5m": ("5m", "5d"), "15m": ("15m", "1mo"), "30m": ("30m", "1mo"), "1h": ("60m", "3mo"),
             "1d": ("1d", "1y"), "1wk": ("1wk", "5y"), "1mo": ("1mo", "max")}
PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max"}


@app.get("/api/bars/{symbol}")
def bars_at(symbol: str, interval: str = "1d", period: str | None = None):
    """Bars at one interval, times as the exchange's own clock written as UTC seconds, so the chart labels
    them in exchange time. Daily and longer bars sit at midnight of their date."""
    if not HAS_FEED:
        raise HTTPException(503, "No price feed: install yfinance")
    if interval not in INTERVALS:
        raise HTTPException(400, f"interval must be one of {', '.join(INTERVALS)}")
    yf_interval, default_period = INTERVALS[interval]
    period = period if period in PERIODS else default_period
    tz = MARKETS[market_of(symbol)]["tz"]
    intraday = interval not in ("1d", "1wk", "1mo")
    t = yf.Ticker(symbol)
    frame = _cached(symbol, f"bars-{interval}:{period}", lambda: t.history(period=period, interval=yf_interval, auto_adjust=False, prepost=False))
    if frame.empty:
        raise HTTPException(404, f"Yahoo Finance has no {interval} bars for {symbol}")
    idx = frame.index.tz_convert(tz) if frame.index.tz is not None else frame.index.tz_localize("UTC").tz_convert(tz)
    out = []
    for ts, r in zip(idx, frame.itertuples()):
        if any(x != x for x in (r.Open, r.High, r.Low, r.Close)):  # NaN rows
            continue
        when = (ts.year, ts.month, ts.day, ts.hour, ts.minute, 0) if intraday else (ts.year, ts.month, ts.day, 0, 0, 0)
        out.append({"t": calendar.timegm(when), "o": round(float(r.Open), 2), "h": round(float(r.High), 2), "l": round(float(r.Low), 2),
                    "c": round(float(r.Close), 2), "v": int(r.Volume) if r.Volume == r.Volume else 0})
    return {"symbol": symbol, "interval": interval, "period": period, "tz": str(tz), "bars": out}


@app.get("/api/fx")
def fx():
    if not HAS_FEED:
        raise HTTPException(503, "yfinance is not installed")
    try:
        h = _cached("USDINR=X", "fx", lambda: yf.Ticker("USDINR=X").history(period="5d", interval="1d"))
        return {"USDINR": round(float(h.Close.iloc[-1]), 2), "asOf": h.index[-1].strftime("%Y-%m-%d")}
    except Exception as exc:
        raise HTTPException(502, f"Yahoo Finance failed for USDINR=X: {exc}") from exc


# ---------------------------------------------------------------- quote (top of book)

@app.get("/api/quote/{symbol}")
def quote(symbol: str):
    """Yahoo's delayed best bid and ask with sizes, for the selected symbol only."""
    if not HAS_FEED:
        raise HTTPException(503, "yfinance is not installed")
    try:
        info = _cached(symbol, "quote", lambda: yf.Ticker(symbol).info or {})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Yahoo Finance failed for {symbol}: {exc}") from exc
    bid, ask = info.get("bid"), info.get("ask")
    return {"symbol": symbol, "bid": bid or None, "ask": ask or None, "bidSize": info.get("bidSize"), "askSize": info.get("askSize"),
            "last": info.get("currentPrice") or info.get("regularMarketPrice"), "asOf": datetime.utcnow().isoformat()}


# ---------------------------------------------------------------- search and profile

@app.get("/api/search")
def search(q: str):
    """Any listed company on NSE, BSE, NYSE or Nasdaq, by name or ticker, through Yahoo's search."""
    if not HAS_FEED:
        raise HTTPException(503, "yfinance is not installed")
    q = q.strip()
    if len(q) < 2:
        return {"results": []}
    try:
        quotes = _cached("search:" + q.lower(), "search", lambda: yf.Search(q, max_results=12).quotes)
    except Exception as exc:
        raise HTTPException(502, f"Yahoo search failed: {exc}") from exc
    out = []
    for r in quotes:
        sym = r.get("symbol") or ""
        if r.get("quoteType") not in ("EQUITY", "ETF") or r.get("exchange") not in ALLOWED_EXCHANGES:
            continue
        if "-" in sym and market_of(sym) == "IN":
            continue  # NSE series suffixes such as -BL, -BE
        out.append({"symbol": sym, "code": code_of(sym), "name": r.get("longname") or r.get("shortname") or sym,
                    "exchange": exchange_name(r.get("exchange")), "market": market_of(sym), "type": r.get("quoteType")})
    return {"results": out}


def _profile(symbol: str) -> dict:
    info = yf.Ticker(symbol).info or {}
    if not info.get("symbol") and not info.get("shortName"):
        raise HTTPException(404, f"Yahoo Finance has no listing for {symbol}")
    mk = market_of(symbol)
    exch = exchange_name(info.get("exchange"))
    # Indian large caps trade on both exchanges; say so, with the quoted venue first.
    listed = exch
    if mk == "IN" and exch in ("NSE", "BSE"):
        listed = "NSE, BSE" if exch == "NSE" else "BSE, NSE"
    return {
        "symbol": symbol, "code": code_of(symbol), "name": info.get("longName") or info.get("shortName") or symbol,
        "exchange": exch, "listed": listed, "market": mk, "ccy": info.get("currency") or ("INR" if mk == "IN" else "USD"),
        "sector": info.get("sector") or "", "industry": info.get("industry") or "", "website": info.get("website") or "",
        "summary": (info.get("longBusinessSummary") or "")[:600],
        "sigma": 0.02, "adv": info.get("averageVolume") or 1e6,
    }


@app.get("/api/profile/{symbol}")
def profile(symbol: str):
    if not HAS_FEED:
        raise HTTPException(503, "yfinance is not installed")
    try:
        return _cached(symbol, "profile", lambda: _profile(symbol))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Yahoo Finance failed for {symbol}: {exc}") from exc


# ---------------------------------------------------------------- fundamentals

def _sum(rows, key):
    """The total of a statement line over the rows, or None when any row lacks it."""
    vals = [r.get(key) for r in rows]
    return sum(vals) if rows and all(v is not None for v in vals) else None


def _row(frame, names, col):
    """The first row of `names` the frame has, at column `col`; None when absent or NaN."""
    for n in names:
        if n in frame.index:
            v = frame.at[n, col]
            if v == v and v is not None:
                return float(v)
    return None


def _period(ts, quarterly: bool) -> str:
    return ts.strftime("%Y-%m") if quarterly else ts.strftime("%Y")


def _income_rows(frame, quarterly: bool) -> list:
    out = []
    for col in sorted(frame.columns):
        sales = _row(frame, ["Total Revenue", "Operating Revenue"], col)
        op = _row(frame, ["Operating Income"], col)
        ebit = _row(frame, ["EBIT"], col)
        dep = _row(frame, ["Reconciled Depreciation", "Depreciation And Amortization"], col)
        if op is None and ebit is not None:
            op = ebit
        interest = _row(frame, ["Interest Expense", "Interest Expense Non Operating"], col)
        pbt = _row(frame, ["Pretax Income"], col)
        tax = _row(frame, ["Tax Provision"], col)
        net = _row(frame, ["Net Income", "Net Income Common Stockholders"], col)
        eps = _row(frame, ["Diluted EPS", "Basic EPS"], col)
        other = _row(frame, ["Other Income Expense", "Other Non Operating Income Expenses"], col)
        # Screener's "operating profit" is EBITDA; Yahoo gives EBITDA directly on most names.
        ebitda = _row(frame, ["EBITDA", "Normalized EBITDA"], col)
        if ebitda is None and op is not None and dep is not None:
            ebitda = op + dep
        out.append({
            "period": _period(col, quarterly), "sales": sales,
            "expenses": (sales - ebitda) if sales is not None and ebitda is not None else None,
            "opProfit": ebitda, "opm": (ebitda / sales) if sales and ebitda is not None else None,
            "ebit": ebit if ebit is not None else op,
            "costOfRevenue": _row(frame, ["Cost Of Revenue", "Cost Of Goods And Services Sold"], col),
            "otherIncome": other, "interest": interest, "depreciation": dep, "pbt": pbt, "tax": tax,
            "netProfit": net, "npm": (net / sales) if sales and net is not None else None, "eps": eps,
            "dividendsPaid": None,
        })
    return out


def _balance_rows(frame) -> list:
    out = []
    for col in sorted(frame.columns):
        out.append({
            "period": _period(col, False),
            "equity": _row(frame, ["Stockholders Equity", "Common Stock Equity"], col),
            "shareCapital": _row(frame, ["Common Stock", "Capital Stock"], col),
            "reserves": _row(frame, ["Retained Earnings"], col),
            "debt": _row(frame, ["Total Debt"], col),
            "currentLiab": _row(frame, ["Current Liabilities"], col),
            "totalLiab": _row(frame, ["Total Liabilities Net Minority Interest"], col),
            "fixedAssets": _row(frame, ["Net PPE"], col),
            "investments": _row(frame, ["Investmentin Financial Assets", "Investments And Advances", "Long Term Equity Investment"], col),
            "currentAssets": _row(frame, ["Current Assets"], col),
            "cash": _row(frame, ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"], col),
            "receivables": _row(frame, ["Accounts Receivable", "Receivables"], col),
            "inventory": _row(frame, ["Inventory"], col),
            "payables": _row(frame, ["Accounts Payable"], col),
            "totalAssets": _row(frame, ["Total Assets"], col),
            "shares": _row(frame, ["Ordinary Shares Number", "Share Issued"], col),
        })
    return out


def _cash_rows(frame) -> list:
    out = []
    for col in sorted(frame.columns):
        cfo = _row(frame, ["Operating Cash Flow"], col)
        cfi = _row(frame, ["Investing Cash Flow"], col)
        cff = _row(frame, ["Financing Cash Flow"], col)
        capex = _row(frame, ["Capital Expenditure"], col)
        fcf = _row(frame, ["Free Cash Flow"], col)
        if fcf is None and cfo is not None and capex is not None:
            fcf = cfo + capex
        out.append({
            "period": _period(col, False), "cfo": cfo, "cfi": cfi, "cff": cff, "capex": capex, "fcf": fcf,
            "dividendsPaid": _row(frame, ["Cash Dividends Paid", "Common Stock Dividend Paid"], col),
            "netChange": _row(frame, ["Changes In Cash"], col),
        })
    return out


def _cagr(first, last, years):
    if not first or not last or first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1 / years) - 1


NSE_FILLING: set = set()


def _nse_fill(symbol: str, code: str):
    """Fetch NSE's filings once, keep every period the store lacks, then recompute the fundamentals."""
    try:
        got = qnse.statements(code)
        for tbl in ("quarters", "annual"):
            have = {r["period"] for r in qstore.get_periods(tbl, symbol) if r.get("source") != "NSE"}
            fresh = [r for r in got.get(tbl, []) if r["period"] not in have]
            if fresh:
                qstore.upsert_periods(tbl, symbol, fresh)
        if got.get("holding"):
            qstore.put_json("nse_holding", symbol, got["holding"])
        qstore.put_json("nse", symbol, {"quarters": len(got.get("quarters", [])), "annual": len(got.get("annual", [])), "holding": len(got.get("holding", []))})
        _fundamentals(symbol)  # the stored page now carries the merged history
    except Exception as exc:
        log.info("NSE fill %s failed: %s", symbol, exc)
        qstore.put_json("nse", symbol, {"error": str(exc)[:200]})
    finally:
        NSE_FILLING.discard(symbol)


def _fundamentals(symbol: str) -> dict:
    yahoo_guard()
    t = yf.Ticker(symbol)
    try:
        info = t.info or {}
        q = t.quarterly_income_stmt
        a = t.income_stmt
        b = t.balance_sheet
        c = t.cashflow
    except Exception as exc:
        if _is_rate_limit(exc):
            BREAKER["until"] = time.time() + BREAKER_PAUSE
            BREAKER["last"] = datetime.utcnow().isoformat()
        raise
    if a.empty and q.empty:
        raise HTTPException(404, f"Yahoo Finance has no financial statements for {symbol}")
    # For US companies, SEC EDGAR carries every quarter and year ever filed; it goes into the store
    # first so Yahoo's figures win where both have the same period.
    if market_of(symbol) == "US":
        try:
            edgar = _cached(symbol, "edgar", lambda: qedgar.statements(symbol))
            for tbl in ("quarters", "annual", "balance", "cashflow"):
                if edgar.get(tbl):
                    qstore.upsert_periods(tbl, symbol, edgar[tbl])
        except Exception as exc:
            log.info("EDGAR %s skipped: %s", symbol, exc)
    nse_state = None
    if market_of(symbol) == "IN":
        nse_state = qstore.get_json("nse", symbol)
        if nse_state is None and qnse.available() and symbol not in NSE_FILLING:
            NSE_FILLING.add(symbol)
            threading.Thread(target=_nse_fill, args=(symbol, code_of(symbol)), daemon=True).start()
    # Fresh rows laid over everything the store has seen before: the history only ever grows.
    quarters = qstore.merge_periods("quarters", symbol, _income_rows(q, True) if not q.empty else [])
    annual = qstore.merge_periods("annual", symbol, _income_rows(a, False) if not a.empty else [])
    balance = qstore.merge_periods("balance", symbol, _balance_rows(b) if not b.empty else [])
    cash = qstore.merge_periods("cashflow", symbol, _cash_rows(c) if not c.empty else [])
    for row in annual:  # dividends sit on the cash-flow statement
        m = next((x for x in cash if x["period"] == row["period"]), None)
        if m:
            row["dividendsPaid"] = m["dividendsPaid"]

    latest_a = annual[-1] if annual else {}
    latest_b = balance[-1] if balance else {}
    latest_c = cash[-1] if cash else {}
    price = info.get("currentPrice") or info.get("regularMarketPrice")
    equity = latest_b.get("equity")
    net = latest_a.get("netProfit")
    # ROCE = EBIT / (total assets - current liabilities); ROE = net profit / equity. Both on the latest year.
    ebit_a = None
    if not a.empty:
        last_col = sorted(a.columns)[-1]
        ebit_a = _row(a, ["EBIT"], last_col)
        if ebit_a is None:
            ebit_a = _row(a, ["Operating Income"], last_col)
    # Returns on the average of the opening and closing balance sheets, the way Screener does it.
    prev_b = balance[-2] if len(balance) >= 2 else latest_b
    def avg(k):
        x, y = latest_b.get(k), prev_b.get(k)
        return (x + y) / 2 if x is not None and y is not None else x
    cap_employed = (avg("totalAssets") or 0) - (avg("currentLiab") or 0)
    roce = (ebit_a / cap_employed) if ebit_a is not None and cap_employed > 0 else None
    avg_equity = avg("equity")
    roe = (net / avg_equity) if net is not None and avg_equity else None
    payout = None
    if net and latest_a.get("dividendsPaid") is not None:
        payout = abs(latest_a["dividendsPaid"]) / net if net > 0 else None
    shares = info.get("sharesOutstanding") or latest_b.get("shares")
    book_value = (equity / shares) if equity and shares else info.get("bookValue")

    ratios = {
        "marketCap": info.get("marketCap"), "price": price,
        "high52": info.get("fiftyTwoWeekHigh"), "low52": info.get("fiftyTwoWeekLow"),
        "pe": info.get("trailingPE"), "eps": info.get("trailingEps"), "bookValue": book_value,
        "pb": (price / book_value) if price and book_value else info.get("priceToBook"),
        "divYield": (info.get("dividendYield") / 100) if info.get("dividendYield") is not None else None,
        "roe": roe, "roce": roce, "payout": payout,
        "debtToEquity": ((latest_b.get("debt") or 0) / equity) if equity else None,
        "evEbitda": info.get("enterpriseToEbitda"), "shares": shares,
        "opm": latest_a.get("opm"), "npm": latest_a.get("npm"),
    }

    def growth(rows, key, years):
        if len(rows) <= years:
            return None
        return _cagr(rows[-1 - years].get(key), rows[-1].get(key), years)

    # The latest quarter against the same quarter a year earlier, matched by period: Yahoo skips
    # quarters now and then, so counting rows back would compare the wrong periods.
    yoy_sales = yoy_profit = None
    if quarters:
        now = quarters[-1]
        want = f"{int(now['period'][:4]) - 1}{now['period'][4:]}"
        prev = next((x for x in quarters if x["period"] == want), None)
        if prev:
            if prev.get("sales") and now.get("sales"):
                yoy_sales = now["sales"] / prev["sales"] - 1
            if prev.get("netProfit") and now.get("netProfit") and prev["netProfit"] > 0:
                yoy_profit = now["netProfit"] / prev["netProfit"] - 1
    growth_out = {
        "sales1y": growth(annual, "sales", 1), "profit1y": growth(annual, "netProfit", 1),
        "sales3y": growth(annual, "sales", 3), "profit3y": growth(annual, "netProfit", 3),
        "salesQtrYoY": yoy_sales, "profitQtrYoY": yoy_profit,
    }

    ins = info.get("heldPercentInsiders")
    inst = info.get("heldPercentInstitutions")
    holding = {
        "promoters": ins, "institutions": inst,
        "public": (1 - ins - inst) if ins is not None and inst is not None else None,
        "institutionsCount": None,
    }
    try:
        mh = t.major_holders
        if mh is not None and "Value" in mh.columns and "institutionsCount" in mh.index:
            holding["institutionsCount"] = int(mh.at["institutionsCount", "Value"])
    except Exception:
        pass
    nse_holding = qstore.get_json("nse_holding", symbol) if market_of(symbol) == "IN" else None
    if nse_holding:
        latest = nse_holding[-1]
        holding.update(promoters=latest["promoters"], public=latest["public"], history=nse_holding[-16:], holdingSource="NSE filings", asOf=latest["period"])

    try:
        raw = t.calendar or {}
        ed = raw.get("Earnings Date") or []
        cal = {"earningsDate": str(ed[0]) if ed else None,
               "exDividendDate": str(raw.get("Ex-Dividend Date")) if raw.get("Ex-Dividend Date") else None}
    except Exception:
        cal = {"earningsDate": None, "exDividendDate": None}

    # Rule-based checks in the spirit of Screener's pros and cons. The page labels them as checks.
    checks = []

    def check(ok, text):
        checks.append({"ok": ok, "text": text})

    if payout is not None:
        if payout >= 0.15:
            check(True, f"Pays out {payout * 100:.0f}% of profit as dividends")
        elif payout == 0:
            check(None, "Pays no dividend")
    de = ratios["debtToEquity"]
    if de is not None:
        if de < 0.1:
            check(True, "Almost debt-free")
        elif de > 1:
            check(False, f"Debt is {de:.1f} times equity")
    if ratios["pb"] is not None and ratios["pb"] > 5:
        check(False, f"Trades at {ratios['pb']:.1f} times its book value")
    if growth_out["profit3y"] is not None:
        if growth_out["profit3y"] >= 0.15:
            check(True, f"Profit grew {growth_out['profit3y'] * 100:.0f}% a year over 3 years")
        elif growth_out["profit3y"] < 0:
            check(False, f"Profit shrank {abs(growth_out['profit3y']) * 100:.0f}% a year over 3 years")
    if growth_out["sales3y"] is not None and growth_out["sales3y"] < 0.05:
        check(False, f"Sales grew only {growth_out['sales3y'] * 100:.1f}% a year over 3 years")
    if roce is not None:
        if roce >= 0.15:
            check(True, f"ROCE of {roce * 100:.1f}%")
        elif roce < 0.08:
            check(False, f"Low ROCE of {roce * 100:.1f}%")
    if latest_c.get("fcf") is not None and latest_c["fcf"] < 0:
        check(False, "Negative free cash flow last year")
    if ratios["pe"] is not None and ratios["pe"] > 60:
        check(False, f"P/E of {ratios['pe']:.0f} prices in a lot of growth")
    if nse_holding and len(nse_holding) >= 13:
        delta = nse_holding[-1]["promoters"] - nse_holding[-13]["promoters"]
        if delta <= -0.03:
            check(False, f"Promoter holding fell {abs(delta) * 100:.1f} points over 3 years")
        elif delta >= 0.03:
            check(True, f"Promoter holding rose {delta * 100:.1f} points over 3 years")

    mk = market_of(symbol)
    result = {
        "symbol": symbol, "ccy": info.get("financialCurrency") or info.get("currency") or ("INR" if mk == "IN" else "USD"),
        "market": mk, "asOf": datetime.utcnow().strftime("%Y-%m-%d"),
        "fiscalYearEnd": sorted(a.columns)[-1].strftime("%b") if not a.empty else None,
        "version": FUND_VERSION,
        "ratios": ratios, "quarters": quarters[-60:], "annual": annual[-15:], "balance": balance[-10:], "cashflow": cash[-10:],
        "growth": growth_out, "holding": holding, "calendar": cal, "checks": checks,
        "ratioHistory": qratios.history(annual, balance, cash),
        "nse": ({"status": "done", **nse_state} if nse_state and "error" not in nse_state else {"status": "unavailable", **nse_state} if nse_state else {"status": "loading" if qnse.available() else "unavailable"}) if mk == "IN" else None,
        "name": info.get("longName") or info.get("shortName") or symbol,
        "sector": info.get("sector") or "", "industry": info.get("industry") or "",
    }
    qstore.put_json("fund", symbol, result)
    # The row the screener and the peer tables read: one line per company.
    u = qstore.universe_entry(symbol) or {}
    qstore.put_row(symbol, mk, {
        "symbol": symbol, "code": code_of(symbol), "name": u.get("name") or result["name"], "exchange": u.get("exchange") or exchange_name(info.get("exchange")),
        "market": mk, "ccy": result["ccy"], "sector": u.get("sector") or result["sector"], "industry": u.get("industry") or result["industry"],
        "price": price, "mcap": ratios["marketCap"], "pe": ratios["pe"], "pb": ratios["pb"], "divYield": ratios["divYield"],
        "roce": roce, "roe": roe, "de": ratios["debtToEquity"], "opm": ratios["opm"], "npm": ratios["npm"],
        "sales3y": growth_out["sales3y"], "profit3y": growth_out["profit3y"], "sales1y": growth_out["sales1y"], "profit1y": growth_out["profit1y"],
        "salesQtrYoY": yoy_sales, "profitQtrYoY": yoy_profit, "promoters": ins, "institutions": inst,
        "high52": ratios["high52"], "low52": ratios["low52"], "evEbitda": ratios["evEbitda"], "payout": payout,
        "checksBad": sum(1 for ch in checks if ch["ok"] is False), "checksGood": sum(1 for ch in checks if ch["ok"] is True),
        # Yahoo's quote fields the screener bar filters on; absent for many Indian names.
        "chgPct": (info["regularMarketChangePercent"] / 100) if info.get("regularMarketChangePercent") is not None else None,
        "beta": info.get("beta"), "peg": info.get("trailingPegRatio"), "chg52w": info.get("52WeekChange"),
        "analyst": info.get("recommendationKey"), "analystMean": info.get("recommendationMean"), "analysts": info.get("numberOfAnalystOpinions"),
        "target": info.get("targetMeanPrice"), "epsQtrYoY": info.get("earningsQuarterlyGrowth"), "revYoY": info.get("revenueGrowth"),
        "earningsDate": cal.get("earningsDate"), "rowVersion": ROW_VERSION,
        # Technicals, extended hours, dividends, forecasts and statement totals for the screener's column sets.
        "sma50": info.get("fiftyDayAverage"), "sma200": info.get("twoHundredDayAverage"),
        "vol": info.get("volume") or info.get("regularMarketVolume"), "avgVol": info.get("averageVolume"),
        "pre": info.get("preMarketPrice"), "preChg": (info["preMarketChangePercent"] / 100) if info.get("preMarketChangePercent") is not None else None,
        "post": info.get("postMarketPrice"), "postChg": (info["postMarketChangePercent"] / 100) if info.get("postMarketChangePercent") is not None else None,
        "targetLow": info.get("targetLowPrice"), "targetHigh": info.get("targetHighPrice"),
        "dps": info.get("dividendRate"), "divYield5y": (info["fiveYearAvgDividendYield"] / 100) if info.get("fiveYearAvgDividendYield") is not None else None,
        "exDividendDate": cal.get("exDividendDate"),
        "salesTTM": _sum(quarters[-4:], "sales") if len(quarters) >= 4 else None, "profitTTM": _sum(quarters[-4:], "netProfit") if len(quarters) >= 4 else None,
        "ebitda": info.get("ebitda"), "equity": latest_b.get("equity"), "debt": latest_b.get("debt"), "cash": latest_b.get("cash"), "totalAssets": latest_b.get("totalAssets"),
        "cfo": latest_c.get("cfo"), "capex": latest_c.get("capex"), "fcf": latest_c.get("fcf"),
        "eps": ratios["eps"], "bookValue": ratios["bookValue"], "fcfps": (latest_c["fcf"] / shares) if latest_c.get("fcf") is not None and shares else None,
    })
    return result


FUND_VERSION = 4  # ratio history includes EBIT, COGS and payables when reported
ROW_VERSION = 3  # bump when the screener row gains fields, so the background fill rebuilds every row


def fundamentals_for(symbol: str, max_age: float = 24 * 3600) -> dict:
    """Stored fundamentals when fresh enough and of the current shape, else a fetch (which stores them)."""
    stored = qstore.get_json("fund", symbol, max_age)
    if stored and stored.get("version") == FUND_VERSION:
        return stored
    return _fundamentals(symbol)


@app.get("/api/fundamentals/{symbol}")
def fundamentals(symbol: str):
    if not HAS_FEED:
        raise HTTPException(503, "yfinance is not installed")
    try:
        return fundamentals_for(symbol)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Yahoo Finance failed for {symbol}: {exc}") from exc


def fundamentals_brief(symbol: str) -> str:
    """A compact text snapshot of the same figures the page shows, for the agents' context."""
    try:
        f = fundamentals_for(symbol)
    except Exception:
        return ""
    ccy = f["ccy"]
    unit, div = ("crore", 1e7) if ccy == "INR" else ("million", 1e6)

    def m(v):
        return "n/a" if v is None else f"{v / div:,.1f}"

    def p(v):
        return "n/a" if v is None else f"{v * 100:.1f}%"

    r = f["ratios"]
    lines = [
        f"Fundamentals snapshot for {symbol} (Yahoo Finance, as of {f['asOf']}; figures in {ccy} {unit}, consolidated where reported):",
        f"- Market cap {m(r['marketCap'])}, price {r['price']}, 52-week {r['low52']} to {r['high52']}, P/E {r['pe']}, EPS {r['eps']}, P/B {None if r['pb'] is None else round(r['pb'], 2)}, dividend yield {p(r['divYield'])}",
        f"- ROE {p(r['roe'])}, ROCE {p(r['roce'])}, debt/equity {None if r['debtToEquity'] is None else round(r['debtToEquity'], 2)}, EV/EBITDA {r['evEbitda']}, operating margin {p(r['opm'])}, net margin {p(r['npm'])}",
        "- Quarterly sales / net profit / EPS: " + "; ".join(f"{x['period']}: {m(x['sales'])} / {m(x['netProfit'])} / {x['eps']}" for x in f["quarters"][-5:]),
        "- Annual sales / net profit: " + "; ".join(f"{x['period']}: {m(x['sales'])} / {m(x['netProfit'])}" for x in f["annual"][-4:]),
        f"- Growth: sales 3y CAGR {p(f['growth']['sales3y'])}, profit 3y CAGR {p(f['growth']['profit3y'])}, latest quarter YoY sales {p(f['growth']['salesQtrYoY'])}, profit {p(f['growth']['profitQtrYoY'])}",
    ]
    if f["balance"]:
        bb = f["balance"][-1]
        lines.append(f"- Balance sheet {bb['period']}: equity {m(bb['equity'])}, debt {m(bb['debt'])}, cash {m(bb['cash'])}, total assets {m(bb['totalAssets'])}")
    if f["cashflow"]:
        cc = f["cashflow"][-1]
        lines.append(f"- Cash flow {cc['period']}: operating {m(cc['cfo'])}, capex {m(cc['capex'])}, free cash flow {m(cc['fcf'])}, dividends paid {m(cc['dividendsPaid'])}")
    h = f["holding"]
    lines.append(f"- Holding: promoters/insiders {p(h['promoters'])}, institutions {p(h['institutions'])}, public {p(h['public'])}")
    if f["checks"]:
        lines.append("- Checks: " + "; ".join(("+ " if ch["ok"] else "- " if ch["ok"] is False else "") + ch["text"] for ch in f["checks"]))
    return "\n".join(lines)


@app.get("/api/forecast/{symbol}")
def forecast(symbol: str):
    """Kronos's projection for the next sessions from the symbol's completed daily bars."""
    if not HAS_FEED:
        raise HTTPException(503, "No price feed: install yfinance")
    if not qkronos.available():
        raise HTTPException(503, "Kronos is not installed: pip install torch einops safetensors")
    try:
        return qkronos.forecast(symbol, _bars(symbol)["daily"])
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


# ---------------------------------------------------------------- universe, screener, peers

REFRESH = {"running": False, "done": 0, "total": 0, "errors": 0, "startedAt": None, "finishedAt": None}
ROW_MAX_AGE = 24 * 3600


def _refresh_rows():
    """Walk the universe and fetch fundamentals for every company whose row is older than a day."""
    if REFRESH["running"]:
        return
    symbols = [u["symbol"] for u in qstore.universe_rows()]
    ages = qstore.row_ages()
    now = time.time()
    stale = {r["symbol"] for r in qstore.rows() if r.get("rowVersion") != ROW_VERSION}
    todo = [s for s in symbols if now - ages.get(s, 0) > ROW_MAX_AGE or s in stale]
    REFRESH.update(running=True, done=0, total=len(todo), errors=0, startedAt=datetime.utcnow().isoformat(), finishedAt=None, lastError=None)
    if not todo:
        REFRESH.update(running=False, finishedAt=datetime.utcnow().isoformat())
        return

    def rate_limited(exc) -> bool:
        text = str(exc).lower()
        return "429" in text or "too many" in text or "rate" in text

    try:
        for sym in todo:
            wait = BREAKER["until"] - time.time()
            if wait > 0:
                time.sleep(wait + 5)  # the block is in force; do not add to it
            try:
                _fundamentals(sym)
            except Exception as exc:
                REFRESH["errors"] += 1
                REFRESH["lastError"] = f"{sym}: {str(exc)[:160]}"
                log.info("refresh %s failed: %s", sym, exc)
            finally:
                REFRESH["done"] += 1
                time.sleep(2.5)  # about 1,400 Yahoo calls an hour for the fill, leaving room for the live feed
    finally:
        REFRESH.update(running=False, finishedAt=datetime.utcnow().isoformat())
        # Companies that failed get another pass in a quarter of an hour, up to a few times.
        REFRESH["passes"] = REFRESH.get("passes", 0) + 1
        if REFRESH["errors"] and REFRESH["passes"] < 6:
            threading.Timer(900, _refresh_rows).start()


def _bootstrap():
    try:
        rows = quniverse.load()
        log.info("universe: %d companies", len(rows))
    except Exception as exc:
        log.warning("universe not loaded: %s", exc)
    if HAS_FEED:
        time.sleep(45)  # let a terminal that opens with the server load its own symbols first
        _refresh_rows()


@app.on_event("startup")
def _start_background():
    threading.Thread(target=_bootstrap, daemon=True).start()


@app.get("/api/universe/status")
def universe_status():
    uni = qstore.universe_rows()
    loaded = qstore.row_ages()
    return {
        "companies": len(uni), "byMarket": {m: sum(1 for u in uni if u["market"] == m) for m in ("US", "IN")},
        "loaded": len(loaded), "refresh": REFRESH,
        "sectors": {m: sorted({u["sector"] for u in uni if u["market"] == m and u["sector"]}) for m in ("US", "IN")},
    }


@app.post("/api/universe/refresh")
def universe_refresh():
    threading.Thread(target=_refresh_rows, daemon=True).start()
    return {"started": not REFRESH["running"]}


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


@app.get("/api/screen")
def screen(request: Request, market: str = "IN", sector: str | None = None, q: str | None = None, symbols: str | None = None,
           analyst: str | None = None, earningsWithin: int | None = None, sort: str = "mcap", dir: str = "desc", limit: int = 500):
    """Companies in the market that pass every filter given. Any numeric row field takes a bound as
    min_<field> or max_<field> (ratios are fractions, 0.15 = 15%; market cap in the market's currency);
    sector matches sector or industry; symbols is a comma list; analyst a comma list of Yahoo's
    recommendation keys; earningsWithin a number of days from today."""
    rows = qstore.rows(market)
    uni_total = sum(1 for u in qstore.universe_rows() if u["market"] == market)
    for r in rows:  # derived columns
        r["upside"] = (r["target"] / r["price"] - 1) if r.get("target") and r.get("price") else None
        span = (r.get("high52") or 0) - (r.get("low52") or 0)
        r["pos52"] = ((r["price"] - r["low52"]) / span) if span > 0 and r.get("price") is not None else None
        p = r.get("price")
        r["vs50"] = (p / r["sma50"] - 1) if p and r.get("sma50") else None
        r["vs200"] = (p / r["sma200"] - 1) if p and r.get("sma200") else None
        r["relVol"] = (r["vol"] / r["avgVol"]) if r.get("vol") and r.get("avgVol") else None
        r["from52wHigh"] = (p / r["high52"] - 1) if p and r.get("high52") else None
        r["from52wLow"] = (p / r["low52"] - 1) if p and r.get("low52") else None
    bounds = []
    for key, val in request.query_params.items():
        if key[:4] in ("min_", "max_") and val not in ("", None):
            try:
                bounds.append((key[:3], key[4:], float(val)))
            except ValueError:
                continue
    wanted = set(symbols.split(",")) if symbols else None
    keys = set(analyst.split(",")) if analyst else None
    today = datetime.utcnow().date()
    horizon = (today + timedelta(days=earningsWithin)) if earningsWithin else None

    def ok(r):
        if wanted is not None and r["symbol"] not in wanted:
            return False
        if sector and sector.lower() not in ((r.get("sector") or "").lower(), (r.get("industry") or "").lower()):
            return False
        if q:
            needle = q.lower()
            if needle not in (r.get("code") or "").lower() and needle not in (r.get("name") or "").lower() and needle not in (r.get("industry") or "").lower():
                return False
        if keys is not None and (r.get("analyst") or "") not in keys:
            return False
        if horizon is not None:
            ed = r.get("earningsDate")
            try:
                d = datetime.fromisoformat(str(ed)[:10]).date() if ed else None
            except ValueError:
                d = None
            if d is None or d < today or d > horizon:
                return False
        for side, field, lim in bounds:
            v = r.get(field)
            if v is None or (side == "min" and v < lim) or (side == "max" and v > lim):
                return False
        return True

    out = [r for r in rows if ok(r)]
    key = sort if out and sort in out[0] else "mcap"
    out.sort(key=lambda r: (r.get(key) is None, r.get(key) if r.get(key) is not None else 0), reverse=(dir == "desc"))
    if dir == "desc":  # keep the empty values at the end whichever way we sort
        out = [r for r in out if r.get(key) is not None] + [r for r in out if r.get(key) is None]
    return {"rows": out[:limit], "matched": len(out), "loaded": len(rows), "total": uni_total, "refresh": REFRESH}


class Describe(BaseModel):
    text: str
    market: str = "IN"


SCREEN_FIELDS = ["price", "chgPct", "mcap", "pe", "pb", "peg", "evEbitda", "divYield", "payout", "roce", "roe", "opm", "npm", "de",
                 "sales3y", "profit3y", "sales1y", "profit1y", "salesQtrYoY", "profitQtrYoY", "revYoY", "epsQtrYoY", "chg52w", "pos52",
                 "beta", "analystMean", "upside", "promoters", "institutions", "checksBad", "vs50", "vs200", "relVol", "divYield5y"]


@app.post("/api/screen/describe")
def screen_describe(req: Describe):
    """A screen described in words becomes chip bounds, in the units the chips show. One request of the daily quota."""
    if not has_llm():
        raise HTTPException(503, "No LLM key in server/.env")
    from openai import OpenAI
    model = os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM", "gpt-6-luna")
    unit = "rupees crore (10,000 crore is 10000)" if req.market == "IN" else "US dollars million (10 billion is 10000)"
    system = (
        "You translate a stock screen described in words into JSON bounds for these fields: " + ", ".join(SCREEN_FIELDS) + ". "
        "Units: percentages and growth rates and yields and margins and pos52 as plain percent numbers (15 means 15%); mcap in " + unit + "; "
        "pe, pb, peg, evEbitda, de, beta, relVol as plain numbers; analystMean runs 1 (strong buy) to 5 (sell); checksBad is a count. "
        "Also allowed: sector, a sector name. Reply with one JSON object only, shaped "
        "{\"bounds\": {\"field\": {\"min\": number, \"max\": number}}, \"sector\": string or null, \"note\": \"one short sentence on anything you could not express\"}."
    )
    try:
        # A JSON response format and a roomy budget: a reasoning model spends tokens before it answers.
        r = OpenAI().chat.completions.create(model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": req.text[:600]}],
                                             max_completion_tokens=2000, response_format={"type": "json_object"})
        content = r.choices[0].message.content or ""
        obj = json.loads(content[content.index("{"): content.rindex("}") + 1])
    except Exception as exc:
        raise HTTPException(502, f"The model could not translate that: {str(exc)[:160]}") from exc
    bounds = {k: {s: float(v[s]) for s in ("min", "max") if isinstance(v.get(s), (int, float))}
              for k, v in (obj.get("bounds") or {}).items() if k in SCREEN_FIELDS and isinstance(v, dict)}
    return {"bounds": {k: v for k, v in bounds.items() if v}, "sector": obj.get("sector") or None, "note": obj.get("note"), "model": model}


@app.get("/api/peers/{symbol}")
def peers(symbol: str, limit: int = 10):
    """Companies in the same industry, from the universe, with their screener rows where loaded."""
    me = qstore.universe_entry(symbol) or qstore.get_row(symbol)
    if not me:
        try:
            prof = _cached(symbol, "profile", lambda: _profile(symbol))
            me = {"market": prof["market"], "sector": prof["sector"], "industry": prof["industry"]}
        except Exception:
            return {"industry": None, "rows": []}
    industry = me.get("industry") or me.get("sector")
    if not industry:
        return {"industry": None, "rows": []}
    mk = me.get("market") or market_of(symbol)
    cands = [u for u in qstore.universe_rows(mk) if u["symbol"] != symbol and (u.get("industry") == industry or u.get("sector") == industry)]
    out = []
    for u in cands:
        row = qstore.get_row(u["symbol"]) or {"symbol": u["symbol"], "code": u["code"], "name": u["name"], "exchange": u["exchange"], "market": mk, "loaded": False}
        out.append(row)
    out.sort(key=lambda r: (r.get("mcap") is None, -(r.get("mcap") or 0)))
    return {"industry": industry, "rows": out[:limit]}


# ---------------------------------------------------------------- agent runs

RUNS: dict[str, dict] = {}
LOCK = threading.Lock()


class RunRequest(BaseModel):
    symbol: str
    date: str  # YYYY-MM-DD, the analysis date; data is served as of it
    documentIds: list[str] = Field(default_factory=list, max_length=12)


def _serialisable(state: dict) -> dict:
    """The parts of a TradingAgents state the terminal reads, without the message objects."""
    out = {}
    for k, v in state.items():
        if k == "messages":
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items() if isinstance(vv, (str, int, float, bool)) or vv is None}
    return out


class _Usage:
    """Counts the tokens every model call in a run reports, the way the CLI's stats handler does."""

    def __init__(self):
        from langchain_core.callbacks import BaseCallbackHandler

        parent = self

        class Handler(BaseCallbackHandler):
            def on_llm_end(self, response, **kwargs):
                parent.calls += 1
                for gens in getattr(response, "generations", []):
                    for g in gens:
                        um = getattr(getattr(g, "message", None), "usage_metadata", None)
                        if um:
                            parent.tokens_in += um.get("input_tokens", 0) or 0
                            parent.tokens_out += um.get("output_tokens", 0) or 0

        self.handler = Handler()
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def snapshot(self):
        return {"calls": self.calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out}


def _run(run_id: str, symbol: str, date: str, document_context: str = ''):
    config = DEFAULT_CONFIG.copy()
    if document_context:
        # Changed evidence must not resume an older checkpoint's analyst reports.
        config['quorum_document_context_digest'] = hashlib.sha256(document_context.encode()).hexdigest()
    # One debate round each keeps a run to a few minutes and a few dollars; raise them in .env.
    config.setdefault("max_debate_rounds", 1)
    config.setdefault("max_risk_discuss_rounds", 1)
    try:
        usage = _Usage()
        graph = TradingAgentsGraph(debug=False, config=config, callbacks=[usage.handler])
        asset_type = "crypto" if symbol.upper().endswith("-USD") else "stock"
        with LOCK:
            RUNS[run_id]["model"] = f"{config.get('llm_provider')} · {config.get('deep_think_llm')} / {config.get('quick_think_llm')}"
        state = graph.create_run_state(symbol, date, asset_type)
        brief = fundamentals_brief(symbol) if HAS_FEED else ""
        if document_context:
            brief = (brief + '\n\n' if brief else '') + document_context
        if HAS_FEED and qkronos.available():
            try:  # the projection is optional context; a run never waits on a failure here
                brief = (brief + "\n\n" if brief else "") + qkronos.brief(symbol, qkronos.forecast(symbol, _bars(symbol)["daily"]))
            except Exception as exc:
                log.info("no Kronos projection for %s: %s", symbol, exc)
        if brief:
            state["instrument_context"] = (state.get("instrument_context") or "") + "\n\n" + brief
        final = {}
        # The same path propagate() takes: with checkpointing on, a run that stopped part-way for this
        # ticker and date resumes at its last finished step instead of spending the calls again.
        with graph.checkpoint_scope(symbol, date, asset_type) as thread_id:
            args = graph.propagator.get_graph_args()
            if thread_id is not None:
                args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = thread_id
            graph_input = graph.checkpoint_input(state)
            with LOCK:
                RUNS[run_id]["resumed"] = graph_input is None
            for _messages, chunk in graph.stream_run(graph_input, **args):
                if chunk:
                    final.update(chunk)
                    with LOCK:
                        RUNS[run_id]["state"] = _serialisable(final)
                        RUNS[run_id]["usage"] = usage.snapshot()
            graph.record_decision(symbol, date, final)
            graph.clear_checkpoint_on_success(symbol, date, asset_type)
        with LOCK:
            RUNS[run_id].update(status="done", state=_serialisable(final), rating=run_rating(final), usage=usage.snapshot(),
                                finishedAt=datetime.utcnow().isoformat())
            qstore.put_json("runs", run_id, RUNS[run_id])
            qstore.put_json("latest_run", symbol, run_id)
    except Exception as exc:  # the run reports its own failure
        traceback.print_exc()
        with LOCK:
            RUNS[run_id].update(status="error", error=f"{type(exc).__name__}: {exc}"[:400])


@app.post("/api/runs")
def start_run(req: RunRequest):
    if not HAS_TA:
        raise HTTPException(503, "TradingAgents is not installed: pip install -e TradingAgents")
    if not has_llm():
        raise HTTPException(503, "No LLM API key is set. Put one in server/.env (for example OPENAI_API_KEY).")
    try:
        datetime.strptime(req.date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(400, "date must be YYYY-MM-DD") from exc
    try:
        req.symbol = qdocuments.company(req.symbol)
        document_context, sources = DOCUMENTS.context(req.symbol, req.documentIds, req.date)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    run_id = uuid.uuid4().hex[:12]
    with LOCK:
        RUNS[run_id] = {"id": run_id, "symbol": req.symbol, "date": req.date, "status": "running",
                        "state": {}, "rating": None, "error": None, "model": None,
                        "documentIds": req.documentIds, "sources": sources,
                        "startedAt": datetime.utcnow().isoformat()}
    threading.Thread(target=_run, args=(run_id, req.symbol, req.date, document_context), daemon=True).start()
    return RUNS[run_id]


@app.get("/api/runs/latest/{symbol}")
def latest_run(symbol: str):
    """The newest run for a symbol: a running one from memory, else the last finished one from the store."""
    with LOCK:
        live = [r for r in RUNS.values() if r["symbol"] == symbol]
    if live:
        return sorted(live, key=lambda r: r["startedAt"])[-1]
    run_id = qstore.get_json("latest_run", symbol)
    run = qstore.get_json("runs", run_id) if run_id else None
    if not run:
        raise HTTPException(404, "No run for this symbol yet")
    return run


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    with LOCK:
        run = RUNS.get(run_id)
    if not run:
        run = qstore.get_json("runs", run_id)
    if not run:
        raise HTTPException(404, "No such run")
    return run


@app.get('/api/fundamentals/{symbol}/income')
def income_on_basis(symbol: str, basis: str = 'consolidated'):
    try:
        symbol = qdocuments.company(symbol)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not symbol.endswith(('.NS', '.BO')) or basis not in {'consolidated', 'standalone'}:
        raise HTTPException(400, 'Choose an Indian listing and consolidated or standalone basis')
    cached = qstore.get_json('nse_income_' + basis, symbol, 86400)
    if cached:
        return cached
    if not qnse.available():
        raise HTTPException(503, 'NSE filings are unavailable or cooling down. Try again later.')
    try:
        data = qnse.income_statements(code_of(symbol), basis)
    except Exception as exc:
        log.info('NSE %s %s P&L unavailable: %s', symbol, basis, exc)
        raise HTTPException(503, f'NSE {basis} filings could not be retrieved. Try again later; the other basis has not been substituted.') from exc
    if not data['quarters'] and not data['annual']:
        raise HTTPException(404 if qnse.available() else 503,
                            f'No {basis} P&L could be retrieved from NSE; no other basis has been substituted.')
    data.update(symbol=symbol, asOf=datetime.utcnow().strftime('%Y-%m-%d'))
    qstore.put_json('nse_income_' + basis, symbol, data)
    return data


def _alert_snapshot(symbol: str):
    latest_id = qstore.get_json('latest_run', symbol)
    latest = qstore.get_json('runs', latest_id) if latest_id else None
    observation = {'rating': latest.get('rating') if latest and latest.get('status') == 'done' else None}
    if HAS_FEED:
        try:
            info = _cached(symbol, 'quote', lambda: yf.Ticker(symbol).info or {})
            observation.update(price=info.get('regularMarketPrice'), priceAt=info.get('regularMarketTime'))
        except Exception as exc:
            observation['sourceError'] = str(exc)[:300]
    return observation


ALERTS = qalerts.install(app, DATA_DIR / 'alerts.db', _alert_snapshot)
