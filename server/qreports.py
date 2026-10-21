"""Discover annual-report links; keep the last successful public index on outages."""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import re
import threading
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from fastapi import HTTPException
import qdocuments
import qedgar
import qstore

TABLE = 'published_reports_v1'
_locks = [threading.Lock() for _ in range(64)]
_retry = {}


def read_page(url):
    with urlopen(Request(url, headers={'User-Agent': 'Quorum company research/1.0'}), timeout=20) as response:
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError('Report index is too large')
        return raw.decode('utf-8', errors='replace')


def report_link(label, url, source_url):
    match = re.fullmatch(r'Annual\s+Report\s+(20\d{2}|19\d{2})', ' '.join(label.split()), re.I)
    parsed = urlsplit(url)
    # Only published exchange documents, never Screener's summaries or account links.
    hosts = {'www.bseindia.com', 'bseindia.com', 'archives.nseindia.com', 'nsearchives.nseindia.com'}
    if not match or parsed.scheme != 'https' or parsed.hostname not in hosts or parsed.username or parsed.port not in (None, 443):
        return None
    extension = parsed.path.lower().rsplit('.', 1)[-1]
    if extension not in ('pdf', 'zip'):
        return None
    year = int(match[1])
    if year > datetime.now().year:
        return None
    return dict(year=year, title=f'Annual report {year}', kind='annual_report', url=url,
                format=extension.upper(), source='BSE' if 'bseindia' in parsed.hostname else 'NSE', sourceUrl=source_url)


class AnnualLinks(HTMLParser):
    def __init__(self, source_url):
        super().__init__()
        self.source_url, self.links = source_url, []
        self.href, self.label = None, []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href, self.label = dict(attrs).get('href'), []

    def handle_data(self, data):
        if self.href:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href:
            item = report_link(''.join(self.label), self.href, self.source_url)
            if item:
                self.links.append(item)
            self.href = None


def indian_reports(symbol):
    code = quote(symbol[:-3], safe='')
    source = f'https://www.screener.in/company/{code}/'
    parser = AnnualLinks(source)
    parser.feed(read_page(source))
    # An empty index can mean a changed source page; do not replace good cached data.
    if not parser.links:
        raise ValueError('The public company page did not provide annual-report links')
    years = {}
    for item in parser.links:
        if item['year'] not in years or item['format'] == 'PDF':
            years[item['year']] = item
    return list(years.values()), source


def sec_rows(data, cik, source):
    result = []
    for i, form in enumerate(data.get('form', [])):
        if form not in ('10-K', '20-F', '40-F', '10-K/A', '20-F/A', '40-F/A'):
            continue
        def at(key):
            values = data.get(key, [])
            return values[i] if i < len(values) else ''
        period, filed = at('reportDate'), at('filingDate')
        accession, document = at('accessionNumber'), at('primaryDocument')
        if not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession) or not document or '/' in document or '\\' in document:
            continue
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', period):
            continue
        result.append(dict(year=int(period[:4]), title=f'Annual report · {form}', kind='annual_report',
                           url=f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace("-", "")}/{quote(document, safe="")}',
                           source='SEC EDGAR', sourceUrl=source, format='Filing', reportDate=period, published=filed))
    return result


def us_reports(symbol):
    if not qedgar.enabled():
        raise ValueError('SEC report discovery requires SEC_USER_AGENT with a contact email on the server')
    cik = qedgar.cik_for(symbol)
    if not cik:
        raise ValueError('No SEC company filing record was found for this symbol')
    source = f'https://www.sec.gov/edgar/browse/?CIK={cik}&owner=exclude'
    data = qedgar._get(f'https://data.sec.gov/submissions/CIK{cik:010d}.json')
    rows = sec_rows(data.get('filings', {}).get('recent', {}), cik, source)
    for older in data.get('filings', {}).get('files', [])[:4]:
        if len({r['year'] for r in rows}) >= 10:
            break
        name = older.get('name', '')
        if re.fullmatch(r'CIK\d+-submissions-\d+\.json', name):
            rows += sec_rows(qedgar._get(f'https://data.sec.gov/submissions/{name}'), cik, source)
    if not rows:
        raise ValueError('No annual filings were found for this company')
    return rows, source


def discover(symbol):
    symbol = qdocuments.company(symbol)
    # Serialize cold discoveries, so a busy page cannot repeatedly hit public providers.
    with _locks[hash(symbol) % len(_locks)]:
        fresh = qstore.get_json(TABLE, symbol, max_age=86400)
        if fresh:
            return {**fresh, 'stale': False, 'warning': None}
        previous = qstore.get_json(TABLE, symbol)
        failure = _retry.get(symbol)
        if failure and time.monotonic() - failure[0] < 120:
            return {**(previous or dict(symbol=symbol, reports=[], fetchedAt=None)), 'stale': bool(previous), 'warning': failure[1]}
        try:
            rows, source = indian_reports(symbol) if symbol.endswith(('.NS', '.BO')) else us_reports(symbol)
            rows.sort(key=lambda r: (r['year'], r.get('published', '')), reverse=True)
            result = dict(symbol=symbol, reports=rows[:100], sourceUrl=source,
                          fetchedAt=datetime.now(timezone.utc).isoformat())
            qstore.put_json(TABLE, symbol, result)
            _retry.pop(symbol, None)
            return {**result, 'stale': False, 'warning': None}
        except Exception as exc:
            # Do not expose response bodies, credentials or internal network errors.
            warning = str(exc) if isinstance(exc, ValueError) else 'The report provider is temporarily unavailable. Please try again shortly.'
            _retry[symbol] = (time.monotonic(), warning)
            return {**(previous or dict(symbol=symbol, reports=[], fetchedAt=None)), 'stale': bool(previous), 'warning': warning}


def install(app):
    @app.get('/api/companies/{symbol}/reports')
    def published_reports(symbol: str):
        try:
            return discover(symbol)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
