"""Durable, one-shot alerts. Monitoring requires the bridge, not an open browser.

Only observations supplied by the bridge's real-data provider are evaluated. A
failed quote is recorded and retried; it is never replaced with simulated data.
The inbox is local: no email, push service or webhook is contacted.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class AlertService:
    def __init__(self, path, provider, interval=60, pace=1):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.interval = max(15, interval)
        self.pace = pace
        self.stop_event = threading.Event()
        self.thread = None
        self.last_cycle = None
        self.last_error = None
        self.last_error_at = None
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL,
                    value TEXT, note TEXT, created TEXT NOT NULL, fired_at TEXT,
                    checked_at TEXT, error TEXT, observed TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT UNIQUE,
                    at TEXT NOT NULL, symbol TEXT NOT NULL, text TEXT NOT NULL
                );
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def public(row):
        return {"id": row["id"], "symbol": row["symbol"], "kind": row["kind"],
                "value": json.loads(row["value"]), "note": row["note"],
                "created": row["created"], "firedAt": row["fired_at"],
                "checkedAt": row["checked_at"], "error": row["error"],
                "observed": json.loads(row["observed"]) if row["observed"] else None,
                "persistent": True}

    def create(self, data):
        symbol = str(data.get("symbol", "")).strip().upper()
        kind = data.get("kind")
        value = data.get("value")
        if not re.fullmatch(r"[A-Z0-9.^=_-]{1,40}", symbol):
            raise ValueError("Choose a valid company symbol.")
        if kind not in {"above", "below", "results", "rating"}:
            raise ValueError("Unknown alert condition.")
        if kind in {"above", "below"}:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError("Enter a positive price.") from None
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Enter a positive price.")
        elif kind == "results":
            try:
                value = datetime.strptime(str(value), "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                raise ValueError("Enter a results date in YYYY-MM-DD format.") from None
        elif value is not None:
            value = str(value)[:80]
        alert_id = str(data.get("id") or uuid.uuid4().hex)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", alert_id):
            raise ValueError("Invalid alert identifier.")
        with self.db() as db:
            if db.execute("SELECT COUNT(*) FROM alerts WHERE fired_at IS NULL").fetchone()[0] >= 500:
                if not db.execute("SELECT 1 FROM alerts WHERE id=?", (alert_id,)).fetchone():
                    raise ValueError("The local monitor supports up to 500 waiting alerts.")
            db.execute("INSERT OR IGNORE INTO alerts (id,symbol,kind,value,note,created) VALUES (?,?,?,?,?,?)",
                       (alert_id, symbol, kind, json.dumps(value), str(data.get("note") or "")[:1000], stamp()))
            row = db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        return self.public(row)

    def remove(self, alert_id):
        with self.db() as db:
            db.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
        # Fired events intentionally survive removal of the original alert.

    def snapshot(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM alerts ORDER BY created DESC LIMIT 1000").fetchall()
            events = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
        return {"alerts": [self.public(r) for r in rows],
                "events": [{"id": r["alert_id"], "at": r["at"], "symbol": r["symbol"], "text": r["text"], "persistent": True} for r in events],
                "monitor": {"running": bool(self.thread and self.thread.is_alive()), "intervalSeconds": self.interval,
                            "lastCycle": self.last_cycle, "lastError": self.last_error, "lastErrorAt": self.last_error_at,
                            "source": "Yahoo Finance delayed quotes and completed agent runs"}}

    def evaluate(self, alert, observation, now=None):
        now = now or datetime.now(timezone.utc)
        symbol, kind, target = alert["symbol"], alert["kind"], alert["value"]
        if kind in {"above", "below"}:
            price = observation.get("price")
            if price is None or not math.isfinite(float(price)) or float(price) <= 0:
                raise ValueError("No valid Yahoo quote; waiting for the data source.")
            price = float(price)
            price_at = observation.get("priceAt")
            try:
                observed_at = float(price_at) if isinstance(price_at, (int, float)) else datetime.fromisoformat(str(price_at).replace("Z", "+00:00")).timestamp()
                age = now.timestamp() - observed_at
            except (TypeError, ValueError, OverflowError):
                raise ValueError("Quote has no trade timestamp; waiting for a dated Yahoo quote.") from None
            if not math.isfinite(age) or age > 1800 or age < -60:
                raise ValueError("Latest trade is over 30 minutes old or has an invalid timestamp; waiting for a fresh quote.")
            if (kind == "above" and price >= target) or (kind == "below" and price <= target):
                return f"{symbol}: delayed quote {price:,.2f}, {kind} your {target:,.2f}."
        elif kind == "rating":
            rating = observation.get("rating")
            if rating is not None and rating != target:
                return f"{symbol}: agents now say {rating}; previous rating {target or 'none'}."
        else:
            tz = ZoneInfo("Asia/Kolkata" if symbol.endswith((".NS", ".BO")) else "America/New_York")
            days = (datetime.strptime(target, "%Y-%m-%d").date() - now.astimezone(tz).date()).days
            if days <= 3:
                return f"{symbol}: results {'were due' if days < 0 else 'are due'} on {target}."
        return None

    def scan(self, now=None):
        with self.db() as db:
            active = [self.public(r) for r in db.execute("SELECT * FROM alerts WHERE fired_at IS NULL").fetchall()]
        groups = {}
        for alert in active:
            groups.setdefault(alert["symbol"], []).append(alert)
        for index, (symbol, alerts) in enumerate(groups.items()):
            observation, provider_error = {}, None
            if any(a["kind"] != "results" for a in alerts):
                try:
                    observation = self.provider(symbol) or {}
                except Exception as exc:
                    provider_error = str(exc)[:300] or "Source unavailable."
            for alert in alerts:
                try:
                    if provider_error and alert["kind"] != "results":
                        raise ValueError(provider_error)
                    text = self.evaluate(alert, observation, now)
                    at = stamp()
                    with self.db() as db:
                        if text:
                            # The conditional update and unique event make repeat scans/restarts safe.
                            changed = db.execute("UPDATE alerts SET fired_at=?,checked_at=?,error=NULL,observed=? WHERE id=? AND fired_at IS NULL",
                                                 (at, at, json.dumps(observation), alert["id"])).rowcount
                            if changed:
                                db.execute("INSERT OR IGNORE INTO events (alert_id,at,symbol,text) VALUES (?,?,?,?)", (alert["id"], at, symbol, text))
                        else:
                            db.execute("UPDATE alerts SET checked_at=?,error=NULL,observed=? WHERE id=?", (at, json.dumps(observation), alert["id"]))
                except Exception as exc:
                    with self.db() as db:
                        db.execute("UPDATE alerts SET checked_at=?,error=? WHERE id=?", (stamp(), str(exc)[:300], alert["id"]))
            if index < len(groups) - 1 and self.pace and self.stop_event.wait(self.pace):
                break
        self.last_cycle = stamp()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def work():
            while not self.stop_event.is_set():
                try:
                    self.scan()
                    self.last_error = None
                except Exception as exc:
                    # A temporary DB/source error must not kill monitoring permanently.
                    self.last_error = str(exc)[:300] or "Alert monitoring failed."
                    self.last_error_at = stamp()
                self.stop_event.wait(self.interval)
        self.thread = threading.Thread(target=work, name="quorum-alerts", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)


def install(app, path, provider, interval=60):
    """Register routes and worker lifecycle without importing the bridge."""
    from fastapi import HTTPException
    from pydantic import BaseModel
    service = AlertService(path, provider, interval)

    class AlertRequest(BaseModel):
        id: str | None = None
        symbol: str
        kind: str
        value: str | float | None = None
        note: str = ""

    @app.get("/api/alerts")
    def list_alerts():
        return service.snapshot()

    # Explicit annotation assignment avoids postponed local-class resolution in FastAPI.
    def add_alert(body):
        try:
            return service.create(body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    add_alert.__annotations__["body"] = AlertRequest
    app.post("/api/alerts")(add_alert)

    @app.delete("/api/alerts/{alert_id}")
    def delete_alert(alert_id: str):
        service.remove(alert_id)
        return {"ok": True}

    app.router.add_event_handler("startup", service.start)
    app.router.add_event_handler("shutdown", service.stop)
    return service
