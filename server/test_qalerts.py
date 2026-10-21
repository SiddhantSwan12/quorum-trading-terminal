"""Run with python -m unittest server.test_qalerts. No live market calls."""
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from server.qalerts import AlertService, install


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "alerts.db"
        self.observation = {"price": 105, "priceAt": time.time(), "rating": "Buy"}
        self.service = AlertService(self.path, lambda _: self.observation, pace=0)

    def tearDown(self):
        self.service.stop()
        self.tmp.cleanup()

    def add(self, **patch):
        return self.service.create({"id": "test-alert", "symbol": "NVDA", "kind": "above", "value": 100, **patch})

    def test_durable_condition_and_exactly_one_event_after_restart(self):
        self.add()
        self.service.scan()
        second = AlertService(self.path, lambda _: self.observation, pace=0)
        second.scan()
        result = second.snapshot()
        self.assertEqual(len(result["alerts"]), 1)
        self.assertEqual(len(result["events"]), 1)
        self.assertIsNotNone(result["alerts"][0]["firedAt"])

    def test_stale_missing_and_future_timestamps_do_not_fire(self):
        self.add()
        for price_at in (None, time.time() - 3600, time.time() + 3600):
            self.observation["priceAt"] = price_at
            self.service.scan()
            result = self.service.snapshot()
            self.assertIsNone(result["alerts"][0]["firedAt"])
            self.assertTrue(result["alerts"][0]["error"])
            self.assertEqual(result["events"], [])

    def test_source_failure_recovers_without_synthetic_price(self):
        self.add()
        def unavailable(_):
            raise RuntimeError("Yahoo is unavailable")
        self.service.provider = unavailable
        self.service.scan()
        self.assertEqual(self.service.snapshot()["alerts"][0]["error"], "Yahoo is unavailable")
        self.service.provider = lambda _: self.observation
        self.service.scan()
        result = self.service.snapshot()
        self.assertEqual(len(result["events"]), 1)
        self.assertIsNone(result["alerts"][0]["error"])

    def test_idempotent_import_cannot_reset_fired_alert(self):
        self.add()
        self.service.scan()
        self.add(value=900)
        result = self.service.snapshot()
        self.assertEqual(result["alerts"][0]["value"], 100)
        self.assertIsNotNone(result["alerts"][0]["firedAt"])

    def test_invalid_values_are_rejected(self):
        for value in (float("nan"), float("inf"), -1, 0, "bad"):
            with self.assertRaises(ValueError):
                self.add(value=value)
        with self.assertRaises(ValueError):
            self.add(symbol="../secret")
        with self.assertRaises(ValueError):
            self.add(kind="results", value="2026-99-01")

    def test_rating_does_not_require_a_price(self):
        self.add(kind="rating", value="Hold")
        self.observation = {"rating": "Buy"}
        self.service.scan()
        self.assertIn("agents now say Buy", self.service.snapshot()["events"][0]["text"])

    def test_result_reminder_uses_exchange_day_without_a_quote(self):
        self.add(symbol="TCS.NS", kind="results", value="2026-10-24")
        now = datetime(2026, 10, 20, 22, tzinfo=timezone.utc)  # Oct 21 in India
        self.service.scan(now)
        self.assertEqual(len(self.service.snapshot()["events"]), 1)

    def test_remove_keeps_fired_inbox(self):
        alert = self.add()
        self.service.scan()
        self.service.remove(alert["id"])
        result = self.service.snapshot()
        self.assertEqual(result["alerts"], [])
        self.assertEqual(len(result["events"]), 1)

    def test_worker_evaluates_without_a_browser(self):
        self.add()
        self.service.start()
        deadline = time.monotonic() + 2
        while not self.service.snapshot()["events"] and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.service.snapshot()["events"]), 1)
        self.service.stop()
        self.assertFalse(self.service.thread.is_alive())

    def test_http_routes_and_backend_lifecycle(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        service = install(app, Path(self.tmp.name) / "api.db", lambda _: self.observation, interval=15)
        with TestClient(app) as client:
            self.assertTrue(client.get("/api/alerts").json()["monitor"]["running"])
            response = client.post("/api/alerts", json={"id": "api-test", "symbol": "AAPL", "kind": "below", "value": 50})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["persistent"])
            self.assertEqual(client.post("/api/alerts", json={"symbol": "AAPL", "kind": "above", "value": -5}).status_code, 400)
            self.assertEqual(client.delete("/api/alerts/api-test").status_code, 200)
            self.assertEqual(client.get("/api/alerts").json()["alerts"], [])
        self.assertFalse(service.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
