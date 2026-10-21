"""Company PDF ingestion and citations: deterministic fixtures, no network calls.

Run: python -m unittest server.test_qdocuments -v
"""
import io
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from server import qdocuments as documents


def pdf_bytes(texts=("Revenue grew 15 percent. Cash increased.", "Debt risk and auditor guidance for capital."), password=None):
    """Small text PDFs authored using PDF text streams and a standard font."""
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    font_ref = writer._add_object(font)
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref})})
        if text:
            safe = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            stream = DecodedStreamObject()
            stream.set_data(f'BT /F1 12 Tf 72 720 Td ({safe}) Tj ET'.encode('ascii'))
            page[NameObject('/Contents')] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def resolved(address):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, '', (address, 443))]


class DocumentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = documents.DocumentStore(self.root)
        self.pdf = pdf_bytes()

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, symbol='NVDA', data=None, **metadata):
        return self.store.add(symbol, self.pdf if data is None else data, metadata.pop('filename', 'annual.pdf'),
                              published=metadata.pop('published', '2026-07-01'), **metadata)

    def test_page_numbered_text_and_search_are_bound_to_original_pdf(self):
        doc = self.add(title='Annual report')
        self.assertEqual(doc['page_count'], 2)
        self.assertEqual(doc['text_pages'], 2)
        self.assertEqual(doc['status'], 'ready')
        self.assertEqual((self.root / f'{doc["id"]}.pdf').read_bytes(), self.pdf)
        matches = self.store.search('NVDA', 'auditor debt')
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match['page'], 2)
        self.assertEqual(match['citation'], f'[DOC:{doc["id"]}:p2]')
        self.assertEqual(match['url'], f'/api/documents/{doc["id"]}/file#page=2')
        self.assertEqual(match['sha256'], doc['sha256'])
        self.assertIn('Debt risk', match['text'])

    def test_symbol_isolation_and_duplicate_reuse(self):
        first = self.add()
        duplicate = self.add(title='Different display title')
        other = self.add(symbol='AAPL')
        self.assertEqual(first['id'], duplicate['id'])
        self.assertNotEqual(first['id'], other['id'])
        self.assertEqual(len(self.store.list('nvda')), 1)
        self.assertEqual(self.store.search('MSFT', 'revenue'), [])
        with self.assertRaisesRegex(ValueError, 'another company'):
            self.store.archive('AAPL', first['id'])
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.store.context('NVDA', [other['id']], '2026-10-01')

    def test_archive_hides_report_but_keeps_original_and_citation(self):
        doc = self.add()
        self.store.archive('NVDA', doc['id'])
        self.assertEqual(self.store.list('NVDA'), [])
        self.assertEqual(self.store.search('NVDA', 'debt'), [])
        self.assertEqual(self.store.get(doc['id'])['archived'], 1)
        self.assertEqual((self.root / f'{doc["id"]}.pdf').read_bytes(), self.pdf)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            self.store.context('NVDA', [doc['id']], '2026-10-01')

    def test_byte_type_password_page_and_text_limits(self):
        with self.assertRaisesRegex(ValueError, 'Only PDF'):
            self.add(data=b'<html>login required</html>')
        with patch.object(documents, 'MAX_BYTES', 32), self.assertRaisesRegex(ValueError, '20 MB'):
            self.add()
        with self.assertRaisesRegex(ValueError, 'Password'):
            self.add(data=pdf_bytes(password='secret'))
        with patch.object(documents, 'MAX_PAGES', 1), self.assertRaisesRegex(ValueError, 'between 1 and 1'):
            self.add()
        with patch.object(documents, 'MAX_TEXT', 10), self.assertRaisesRegex(ValueError, '2 million'):
            self.add()
        self.assertEqual(self.store.list('NVDA'), [])
        self.assertEqual(list(self.root.glob('*.pdf')), [])

    def test_scanned_pdf_is_preserved_but_cannot_supply_agent_context(self):
        doc = self.add(data=pdf_bytes(texts=('', '')))
        self.assertEqual(doc['status'], 'needs_ocr')
        self.assertEqual(doc['text_pages'], 0)
        self.assertEqual(self.store.search('NVDA', ''), [])
        with self.assertRaisesRegex(ValueError, 'no extractable text'):
            self.store.context('NVDA', [doc['id']], '2026-10-01')

    def test_publication_dates_prevent_future_report_context(self):
        early = self.add(published='2026-01-01')
        future = self.add(data=pdf_bytes(('Future revenue and risk.',)), published='2026-12-01')
        undated = self.add(data=pdf_bytes(('Undated debt.',)), published='')
        matches = self.store.search('NVDA', '', as_of='2026-06-01')
        self.assertTrue(matches)
        self.assertEqual({m['documentId'] for m in matches}, {early['id']})
        with self.assertRaisesRegex(ValueError, 'after the analysis date'):
            self.store.context('NVDA', [future['id']], '2026-06-01')
        with self.assertRaisesRegex(ValueError, 'publication date'):
            self.store.context('NVDA', [undated['id']], '2026-06-01')
        with self.assertRaises(ValueError):
            self.add(published='2026-02-30')

    def test_context_uses_stable_citations_and_represents_each_selected_report(self):
        one = self.add(title='Annual report')
        two = self.add(data=pdf_bytes(('Segment outlook and liquidity.',)), kind='presentation', title='Investor presentation')
        text, sources = self.store.context('NVDA', [one['id'], two['id'], one['id']], '2026-10-01')
        self.assertIn('untrusted reference data', text)
        self.assertIn('not full reports', text)
        self.assertEqual({s['documentId'] for s in sources}, {one['id'], two['id']})
        self.assertEqual(len({s['citation'] for s in sources}), len(sources))
        for source in sources:
            self.assertIn(source['citation'], text)
            self.assertTrue(source['url'].endswith(f'#page={source["page"]}'))
        self.assertEqual(self.store.context('NVDA', [], '2026-10-01'), ('', []))
        with self.assertRaisesRegex(ValueError, 'at most 12'):
            self.store.context('NVDA', [f'{i:032x}' for i in range(13)], '2026-10-01')

    def test_filenames_and_document_ids_cannot_escape_library(self):
        doc = self.add(filename='..\\..\\secret.pdf')
        self.assertEqual(doc['filename'], 'secret.pdf')
        for value in ('../secret', 'a' * 31, 'G' * 32):
            with self.assertRaises(ValueError):
                self.store.get(value)
        with self.assertRaises(ValueError):
            self.add(symbol='../NVDA')


class DownloadTest(unittest.TestCase):
    def test_private_reserved_and_mixed_dns_answers_are_rejected(self):
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1', '0.0.0.0'):
            with self.subTest(ip=ip), patch.object(documents.socket, 'getaddrinfo', return_value=resolved(ip)):
                with self.assertRaisesRegex(ValueError, 'public internet'):
                    documents.public_target('https://reports.example/annual.pdf')
        with patch.object(documents.socket, 'getaddrinfo', return_value=resolved('93.184.216.34') + resolved('127.0.0.1')):
            with self.assertRaises(ValueError):
                documents.public_target('https://reports.example/annual.pdf')

    def test_only_uncredentialed_public_https_port_443_is_allowed(self):
        for url in ('http://reports.example/a.pdf', 'file:///secret.pdf', 'https://user:pw@reports.example/a.pdf', 'https://reports.example:8000/a.pdf'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                documents.public_target(url)
        with patch.object(documents.socket, 'getaddrinfo', return_value=resolved('93.184.216.34')):
            parsed, address = documents.public_target('https://reports.example/annual.pdf')
            self.assertEqual(parsed.hostname, 'reports.example')
            self.assertEqual(address, '93.184.216.34')

    def test_redirect_destination_is_revalidated_before_connection(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.getheader.side_effect = lambda name: 'https://127.0.0.1/private.pdf' if name == 'location' else None
        def dns(host, *_args, **_kwargs):
            return resolved('127.0.0.1' if host == '127.0.0.1' else '93.184.216.34')
        with patch.object(documents.socket, 'getaddrinfo', side_effect=dns), patch.object(documents, 'PinnedHTTPS', return_value=connection) as factory:
            with self.assertRaisesRegex(ValueError, 'public internet'):
                documents.fetch_pdf('https://reports.example/report.pdf')
            self.assertEqual(factory.call_count, 1)
            connection.close.assert_called_once()

    def test_downloader_pins_public_address_and_limits_response_bytes(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.return_value = '100'
        response.read.side_effect = [b'%PDF-fixture', b'']
        with patch.object(documents.socket, 'getaddrinfo', return_value=resolved('93.184.216.34')), patch.object(documents, 'PinnedHTTPS', return_value=connection) as factory:
            body, url, name = documents.fetch_pdf('https://reports.example/My%20Report.pdf')
            self.assertEqual(body, b'%PDF-fixture')
            self.assertEqual(name, 'My Report.pdf')
            factory.assert_called_once_with('reports.example', '93.184.216.34')
        response.getheader.return_value = str(documents.MAX_BYTES + 1)
        with patch.object(documents.socket, 'getaddrinfo', return_value=resolved('93.184.216.34')), patch.object(documents, 'PinnedHTTPS', return_value=connection):
            with self.assertRaisesRegex(ValueError, '20 MB'):
                documents.fetch_pdf('https://reports.example/large.pdf')


class DocumentsRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = FastAPI()
        self.store = documents.install(self.app, self.tmp.name)
        self.client = TestClient(self.app)
        self.pdf = pdf_bytes()

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_upload_list_search_original_and_archive(self):
        response = self.client.post('/api/companies/NVDA/documents', params={'filename': 'Annual.pdf', 'published': '2026-07-01'}, content=self.pdf, headers={'content-type': 'application/pdf'})
        self.assertEqual(response.status_code, 200, response.text)
        doc = response.json()
        self.assertEqual(len(self.client.get('/api/companies/NVDA/documents').json()['documents']), 1)
        self.assertEqual(self.client.get('/api/companies/AAPL/documents').json()['documents'], [])
        matches = self.client.get('/api/companies/NVDA/documents/search', params={'q': 'auditor'}).json()['matches']
        self.assertEqual(matches[0]['page'], 2)
        original = self.client.get(f'/api/documents/{doc["id"]}/file')
        self.assertEqual(original.content, self.pdf)
        self.assertEqual(original.headers['x-content-type-options'], 'nosniff')
        self.assertIn('inline', original.headers['content-disposition'])
        self.assertEqual(self.client.delete(f'/api/companies/AAPL/documents/{doc["id"]}').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/companies/NVDA/documents/{doc["id"]}').status_code, 200)
        self.assertEqual(self.client.get('/api/companies/NVDA/documents').json()['documents'], [])
        self.assertEqual(self.client.get(f'/api/documents/{doc["id"]}/file').content, self.pdf)

    def test_import_uses_downloaded_pdf_and_source_identity(self):
        with patch.object(documents, 'fetch_pdf', return_value=(self.pdf, 'https://issuer.example/final.pdf', 'final.pdf')) as download:
            response = self.client.post('/api/companies/NVDA/documents/import', json={'url': 'https://issuer.example/report.pdf', 'published': '2026-07-01', 'kind': 'annual_report'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['source_url'], 'https://issuer.example/final.pdf')
            download.assert_called_once_with('https://issuer.example/report.pdf')
        with patch.object(documents, 'fetch_pdf', side_effect=ValueError('Report URLs must resolve to public internet addresses')):
            self.assertEqual(self.client.post('/api/companies/NVDA/documents/import', json={'url': 'https://localhost/secret'}).status_code, 400)

    def test_rejects_non_pdf_and_oversized_upload_without_persistence(self):
        self.assertEqual(self.client.post('/api/companies/NVDA/documents', content=b'not a PDF').status_code, 400)
        with patch.object(documents, 'MAX_BYTES', 32):
            self.assertEqual(self.client.post('/api/companies/NVDA/documents', content=self.pdf).status_code, 413)
        self.assertEqual(self.store.list('NVDA'), [])
        self.assertEqual(self.client.get('/api/documents/' + '0' * 32 + '/file').status_code, 404)


if __name__ == '__main__':
    unittest.main()
