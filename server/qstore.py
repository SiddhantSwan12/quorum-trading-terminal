"""A small SQLite store for the bridge: company profiles, fundamentals, statement history and the
screener rows. Statement periods are kept forever, so the history on the page grows by a quarter
every quarter even though Yahoo only ever serves the latest few.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_DB: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (tbl TEXT, key TEXT, json TEXT, updated REAL, PRIMARY KEY (tbl, key));
CREATE TABLE IF NOT EXISTS periods (tbl TEXT, symbol TEXT, period TEXT, json TEXT, PRIMARY KEY (tbl, symbol, period));
CREATE TABLE IF NOT EXISTS universe (symbol TEXT PRIMARY KEY, market TEXT, code TEXT, name TEXT, exchange TEXT,
    sector TEXT, industry TEXT, idx TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS rows (symbol TEXT PRIMARY KEY, market TEXT, json TEXT, updated REAL);
"""


def init(path: str | Path) -> None:
    global _DB
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _DB = sqlite3.connect(str(path), check_same_thread=False)
    _DB.executescript(SCHEMA)
    _DB.commit()


def _db() -> sqlite3.Connection:
    if _DB is None:
        init(Path(__file__).with_name("data") / "quorum.db")
    return _DB


# ---------------------------------------------------------------- key/value documents

def get_json(tbl: str, key: str, max_age: float | None = None):
    with _LOCK:
        row = _db().execute("SELECT json, updated FROM kv WHERE tbl=? AND key=?", (tbl, key)).fetchone()
    if not row:
        return None
    if max_age is not None and time.time() - row[1] > max_age:
        return None
    return json.loads(row[0])


def put_json(tbl: str, key: str, obj) -> None:
    with _LOCK:
        _db().execute("INSERT OR REPLACE INTO kv (tbl, key, json, updated) VALUES (?, ?, ?, ?)",
                      (tbl, key, json.dumps(obj), time.time()))
        _db().commit()


def updated_at(tbl: str, key: str) -> float | None:
    with _LOCK:
        row = _db().execute("SELECT updated FROM kv WHERE tbl=? AND key=?", (tbl, key)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- statement periods

def upsert_periods(tbl: str, symbol: str, rows: list) -> None:
    with _LOCK:
        _db().executemany(
            "INSERT OR REPLACE INTO periods (tbl, symbol, period, json) VALUES (?, ?, ?, ?)",
            [(tbl, symbol, r["period"], json.dumps(r)) for r in rows if r.get("period")],
        )
        _db().commit()


def get_periods(tbl: str, symbol: str) -> list:
    with _LOCK:
        rows = _db().execute("SELECT json FROM periods WHERE tbl=? AND symbol=? ORDER BY period", (tbl, symbol)).fetchall()
    return [json.loads(r[0]) for r in rows]


def merge_periods(tbl: str, symbol: str, fresh: list) -> list:
    """Stored history with the fresh rows laid over it (fresh wins), oldest first."""
    upsert_periods(tbl, symbol, fresh)
    return get_periods(tbl, symbol)


# ---------------------------------------------------------------- universe and screener rows

def upsert_universe(rows: list) -> None:
    now = time.time()
    with _LOCK:
        _db().executemany(
            "INSERT OR REPLACE INTO universe (symbol, market, code, name, exchange, sector, industry, idx, updated) VALUES (?,?,?,?,?,?,?,?,?)",
            [(r["symbol"], r["market"], r["code"], r["name"], r.get("exchange", ""), r.get("sector", ""), r.get("industry", ""), r.get("idx", ""), now) for r in rows],
        )
        _db().commit()


def universe_rows(market: str | None = None) -> list:
    q = "SELECT symbol, market, code, name, exchange, sector, industry, idx, updated FROM universe"
    args = ()
    if market:
        q += " WHERE market=?"
        args = (market,)
    with _LOCK:
        rows = _db().execute(q + " ORDER BY symbol", args).fetchall()
    keys = ["symbol", "market", "code", "name", "exchange", "sector", "industry", "idx", "updated"]
    return [dict(zip(keys, r)) for r in rows]


def universe_entry(symbol: str) -> dict | None:
    with _LOCK:
        r = _db().execute("SELECT symbol, market, code, name, exchange, sector, industry, idx, updated FROM universe WHERE symbol=?", (symbol,)).fetchone()
    if not r:
        return None
    return dict(zip(["symbol", "market", "code", "name", "exchange", "sector", "industry", "idx", "updated"], r))


def universe_age() -> float | None:
    with _LOCK:
        r = _db().execute("SELECT MIN(updated) FROM universe").fetchone()
    return (time.time() - r[0]) if r and r[0] else None


def put_row(symbol: str, market: str, row: dict) -> None:
    with _LOCK:
        _db().execute("INSERT OR REPLACE INTO rows (symbol, market, json, updated) VALUES (?, ?, ?, ?)",
                      (symbol, market, json.dumps(row), time.time()))
        _db().commit()


def get_row(symbol: str) -> dict | None:
    with _LOCK:
        r = _db().execute("SELECT json, updated FROM rows WHERE symbol=?", (symbol,)).fetchone()
    if not r:
        return None
    row = json.loads(r[0])
    row["updated"] = r[1]
    return row


def rows(market: str | None = None) -> list:
    q = "SELECT json, updated FROM rows"
    args = ()
    if market:
        q += " WHERE market=?"
        args = (market,)
    with _LOCK:
        out = _db().execute(q, args).fetchall()
    result = []
    for j, u in out:
        row = json.loads(j)
        row["updated"] = u
        result.append(row)
    return result


def row_ages() -> dict:
    with _LOCK:
        out = _db().execute("SELECT symbol, updated FROM rows").fetchall()
    return dict(out)
