"""Report discovery tests use isolated in-memory cache and public-page fixtures."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qreports as reports


class ReportTests(unittest.TestCase):
    def test_indian_links_are_yearly_originals_and_prefer_pdf(self):
        html = '''<a href="https://archives.nseindia.com/annual_reports/old.zip">Annual Report 2024</a>
        <a href="https://www.bseindia.com/annual/2024.pdf">Annual Report 2024</a>
        <a href="https://www.bseindia.com/annual/2025.pdf">Annual Report 2025</a>
        <a href="/summary/2025">AI Summary</a>
        <a href="https://evil.example/report.pdf">Annual Report 2023</a>
        <a href="javascript:alert(1)">Annual Report 2022</a>
        <a href="https://www.bseindia.com/q.pdf">Quarterly results</a>'''
        with patch.object(reports, 'read_page', return_value=html):
            rows, source = reports.indian_reports('TCS.NS')
        self.assertEqual({r['year'] for r in rows}, {2024, 2025})
        self.assertTrue(all(r['format'] == 'PDF' for r in rows))
        self.assertEqual(source, 'https://www.screener.in/company/TCS/')

    def test_empty_source_is_failure_not_successful_empty_index(self):
        with patch.object(reports, 'read_page', return_value='<h1>Access denied</h1>'):
            with self.assertRaises(ValueError):
                reports.indian_reports('TCS.NS')

    def test_sec_uses_reporting_year_and_real_accession(self):
        data = dict(form=['10-K', '8-K', '10-K'], reportDate=['2024-12-31'] * 3,
                    filingDate=['2025-02-01'] * 3, accessionNumber=['0000000001-25-000001'] * 3,
                    primaryDocument=['annual.htm', 'event.htm', '../bad.htm'])
        rows = reports.sec_rows(data, 1, 'https://www.sec.gov/')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['year'], 2024)
        self.assertEqual(rows[0]['published'], '2025-02-01')
        self.assertEqual(rows[0]['url'], 'https://www.sec.gov/Archives/edgar/data/1/000000000125000001/annual.htm')

    def test_cache_keeps_last_success_and_cools_down_failed_provider(self):
        saved = dict(symbol='TEST.NS', reports=[dict(year=2024)], fetchedAt='2025-01-01')
        reports._retry.pop('TEST.NS', None)
        def cached(table, symbol, max_age=None):
            return saved if max_age is None else None
        with patch.object(reports.qstore, 'get_json', side_effect=cached), \
             patch.object(reports.qstore, 'put_json') as write, \
             patch.object(reports, 'indian_reports', side_effect=TimeoutError) as fetch:
            result = reports.discover('TEST.NS')
            self.assertTrue(result['stale'])
            self.assertEqual(result['reports'], saved['reports'])
            self.assertTrue(result['warning'])
            reports.discover('TEST.NS')
            self.assertEqual(fetch.call_count, 1)
            write.assert_not_called()
        reports._retry.pop('TEST.NS', None)

    def test_success_is_persisted_and_fresh_cache_skips_fetch(self):
        saved = dict(symbol='TCS.NS', reports=[dict(year=2025)], fetchedAt='2025-01-01')
        with patch.object(reports.qstore, 'get_json', return_value=saved), \
             patch.object(reports, 'indian_reports') as fetch:
            self.assertFalse(reports.discover('TCS.NS')['stale'])
            fetch.assert_not_called()
        with patch.object(reports.qstore, 'get_json', return_value=None), \
             patch.object(reports.qstore, 'put_json') as write, \
             patch.object(reports, 'indian_reports', return_value=([dict(year=2024), dict(year=2025)], 'https://example.com')):
            result = reports.discover('TCS.NS')
            self.assertEqual(result['reports'][0]['year'], 2025)
            write.assert_called_once()

    def test_invalid_symbol_rejected_before_network(self):
        with self.assertRaises(ValueError):
            reports.discover('../../etc')


if __name__ == '__main__':
    unittest.main()
