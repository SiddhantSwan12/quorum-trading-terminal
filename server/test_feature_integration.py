"""Actual bridge endpoint integration with no lifespan, network or model runs.

Run: python -m unittest server.test_feature_integration -v
"""
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from server.test_qdocuments import pdf_bytes

# The bridge initialises its on-disk stores on import. Replace those initialisers
# before importing it, so these tests never touch the trader's real databases.
sys.path.insert(0, str(Path(__file__).resolve().parent))
store_module = importlib.import_module('qstore')
documents_module = importlib.import_module('qdocuments')
alerts_module = importlib.import_module('qalerts')
with patch.object(store_module, 'init'), patch.object(documents_module, 'install', return_value=None), patch.object(alerts_module, 'install', return_value=None):
    bridge = importlib.import_module('server.app')


class RunDocumentIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.docs = documents_module.DocumentStore(self.tmp.name)
        self.original_runs = set(bridge.RUNS)
        self.workers = MagicMock()
        # Replace this module's threading reference, not threading.Thread globally:
        # TestClient/AnyIO still needs real threads for its HTTP test portal.
        self.patches = [patch.object(bridge, 'DOCUMENTS', self.docs),
                        patch.object(bridge, 'HAS_TA', True),
                        patch.object(bridge, 'has_llm', return_value=True),
                        patch.object(bridge, 'threading', SimpleNamespace(Thread=self.workers))]
        for item in self.patches:
            item.start()
        self.client = TestClient(bridge.app)  # no context manager: no startup handlers

    def tearDown(self):
        self.client.close()
        with bridge.LOCK:
            for run_id in set(bridge.RUNS) - self.original_runs:
                bridge.RUNS.pop(run_id, None)
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def doc(self, symbol='NVDA', published='2026-07-01', text='Revenue and cash guidance.'):
        return self.docs.add(symbol, pdf_bytes((text, 'Debt risks and auditor notes.')), 'report.pdf', title='Company report', published=published)

    def test_selected_document_context_and_sources_are_captured_before_launch(self):
        doc = self.doc()
        observed = []
        created_workers = []
        def make_worker(**kwargs):
            run_id, symbol, date, context = kwargs['args']
            stored = bridge.RUNS[run_id]
            observed.append(stored.copy())
            self.assertEqual(symbol, 'NVDA')
            self.assertEqual(date, '2026-10-21')
            self.assertEqual(stored['documentIds'], [doc['id']])
            self.assertTrue(stored['sources'])
            self.assertIn(stored['sources'][0]['citation'], context)
            self.assertIn('Revenue and cash guidance.', context)
            self.assertEqual(stored['sources'][0]['documentId'], doc['id'])
            self.assertTrue(kwargs['daemon'])
            worker = MagicMock()
            created_workers.append(worker)
            return worker
        self.workers.side_effect = make_worker
        response = self.client.post('/api/runs', json={'symbol': 'nvda', 'date': '2026-10-21', 'documentIds': [doc['id']]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['status'], 'running')
        self.assertEqual(body['sources'], observed[0]['sources'])
        self.assertEqual(body['sources'][0]['sha256'], doc['sha256'])
        created_workers[0].start.assert_called_once()
        self.assertEqual(self.workers.call_count, 1)
        self.assertEqual(len(bridge.RUNS) - len(self.original_runs), 1)

    def test_rejects_future_undated_other_company_and_missing_documents(self):
        examples = [self.doc(published='2026-12-01'), self.doc(published='', text='Undated revenue.'),
                    self.doc(symbol='AAPL'), {'id': '0' * 32}]
        for doc in examples:
            with self.subTest(doc=doc['id']):
                response = self.client.post('/api/runs', json={'symbol': 'NVDA', 'date': '2026-10-21', 'documentIds': [doc['id']]})
                self.assertEqual(response.status_code, 400, response.text)
        self.workers.assert_not_called()
        self.assertEqual(set(bridge.RUNS), self.original_runs)

    def test_run_without_documents_remains_supported(self):
        response = self.client.post('/api/runs', json={'symbol': 'NVDA', 'date': '2026-10-21'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['sources'], [])
        self.assertEqual(self.workers.call_args.kwargs['args'][-1], '')
        self.workers.return_value.start.assert_called_once()

    def test_invalid_date_and_excess_document_selection_do_not_launch(self):
        self.assertEqual(self.client.post('/api/runs', json={'symbol': 'NVDA', 'date': '2026-02-30'}).status_code, 400)
        self.assertEqual(self.client.post('/api/runs', json={'symbol': 'NVDA', 'date': '2026-10-21', 'documentIds': [f'{i:032x}' for i in range(13)]}).status_code, 422)
        self.workers.assert_not_called()


class AlertProviderIntegrationTest(unittest.TestCase):
    def get_json(self, table, key, *_args):
        if table == 'latest_run':
            return 'finished-run'
        if table == 'runs':
            return {'status': 'done', 'rating': 'Buy'}
        return None

    def test_uses_yahoo_trade_time_and_regular_price_not_fetch_time(self):
        trade_timestamp = 1792527300
        info = {'currentPrice': 999, 'regularMarketPrice': 215.75, 'regularMarketTime': trade_timestamp}
        with patch.object(bridge, 'HAS_FEED', True), patch.object(bridge.qstore, 'get_json', side_effect=self.get_json), patch.object(bridge, '_cached', return_value=info) as cache:
            result = bridge._alert_snapshot('NVDA')
        self.assertEqual(result['price'], 215.75)
        self.assertEqual(result['priceAt'], trade_timestamp)
        self.assertEqual(result['rating'], 'Buy')
        self.assertEqual(cache.call_args.args[:2], ('NVDA', 'quote'))

    def test_quote_failure_does_not_discard_completed_agent_rating(self):
        with patch.object(bridge, 'HAS_FEED', True), patch.object(bridge.qstore, 'get_json', side_effect=self.get_json), patch.object(bridge, '_cached', side_effect=RuntimeError('Yahoo unavailable')):
            result = bridge._alert_snapshot('NVDA')
        self.assertEqual(result['rating'], 'Buy')
        self.assertNotIn('price', result)
        self.assertIn('Yahoo unavailable', result['sourceError'])

    def test_incomplete_rating_and_unavailable_feed_are_not_synthetic_observations(self):
        def stored(table, *_args):
            return 'unfinished' if table == 'latest_run' else {'status': 'running', 'rating': 'Sell'}
        with patch.object(bridge, 'HAS_FEED', False), patch.object(bridge.qstore, 'get_json', side_effect=stored), patch.object(bridge, '_cached') as cache:
            result = bridge._alert_snapshot('NVDA')
        self.assertEqual(result, {'rating': None})
        cache.assert_not_called()


class IncomeBasisIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(bridge.app)
        self.cache = {}
        self.fetch = MagicMock(side_effect=lambda symbol, basis: {'basis': basis, 'source': 'NSE', 'quarters': [{'period': '2026-06', 'sales': 100 if basis == 'standalone' else 200}], 'annual': []})
        self.patches = [patch.object(bridge.qstore, 'get_json', side_effect=lambda table, symbol, *_args: self.cache.get((table, symbol))),
                        patch.object(bridge.qstore, 'put_json', side_effect=lambda table, symbol, value: self.cache.__setitem__((table, symbol), value)),
                        patch.object(bridge.qnse, 'available', return_value=True),
                        patch.object(bridge.qnse, 'income_statements', self.fetch)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()

    def test_standalone_and_consolidated_use_isolated_caches(self):
        standalone = self.client.get('/api/fundamentals/TCS.NS/income?basis=standalone')
        consolidated = self.client.get('/api/fundamentals/TCS.NS/income?basis=consolidated')
        self.assertEqual(standalone.status_code, 200, standalone.text)
        self.assertEqual(consolidated.status_code, 200, consolidated.text)
        self.assertEqual(standalone.json()['quarters'][0]['sales'], 100)
        self.assertEqual(consolidated.json()['quarters'][0]['sales'], 200)
        self.assertIn(('nse_income_standalone', 'TCS.NS'), self.cache)
        self.assertIn(('nse_income_consolidated', 'TCS.NS'), self.cache)
        self.client.get('/api/fundamentals/TCS.NS/income?basis=standalone')
        self.assertEqual(self.fetch.call_count, 2)
        self.fetch.assert_any_call('TCS', 'standalone')
        self.fetch.assert_any_call('TCS', 'consolidated')

    def test_unavailable_source_returns_503_without_substituting_other_basis(self):
        self.cache[('nse_income_consolidated', 'TCS.NS')] = {'basis': 'consolidated', 'quarters': [{'period': '2026-06', 'sales': 200}], 'annual': []}
        with patch.object(bridge.qnse, 'available', return_value=False):
            response = self.client.get('/api/fundamentals/TCS.NS/income?basis=standalone')
        self.assertEqual(response.status_code, 503, response.text)
        self.fetch.assert_not_called()
        self.assertNotIn(('nse_income_standalone', 'TCS.NS'), self.cache)

    def test_empty_selected_basis_and_invalid_requests_are_explicit(self):
        self.fetch.return_value = {'basis': 'standalone', 'quarters': [], 'annual': []}
        self.fetch.side_effect = None
        response = self.client.get('/api/fundamentals/TCS.NS/income?basis=standalone')
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn('no other basis', response.json()['detail'])
        self.assertEqual(self.client.get('/api/fundamentals/NVDA/income?basis=standalone').status_code, 400)
        self.assertEqual(self.client.get('/api/fundamentals/TCS.NS/income?basis=other').status_code, 400)


if __name__ == '__main__':
    unittest.main()
