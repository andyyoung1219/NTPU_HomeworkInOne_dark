"""Bounded public research adapters: fixed hosts, no redirects or school sessions.

Metadata and OA excerpts remain untrusted source evidence. Each actual request
is reserved by the caller before transmission and carries no account cookies.
"""
import hashlib
import json
import re
from contextlib import nullcontext
from urllib.parse import quote, urlsplit

import httpx
from lxml import etree

from backend.agent.contracts import fail

CROSSREF = 'https://api.crossref.org/works'
EUROPE_PMC = 'https://www.ebi.ac.uk/europepmc/webservices/rest'
UCI_API = 'https://archive.ics.uci.edu/api/dataset'
MAX_RESPONSE = 1_000_000
MAX_FULLTEXT = 3_000_000
MAX_TEXT = 16000


def plain_xml(value, limit=4000):
    if not isinstance(value, str):
        return ''
    # Abstract markup is data; disable DTDs/entities/network at parser level.
    if '<!DOCTYPE' in value.upper() or '<!ENTITY' in value.upper():
        return ''
    try:
        root = etree.fromstring(('<root>' + value + '</root>').encode(),
            parser=etree.XMLParser(resolve_entities=False, no_network=True, recover=False))
        text = ' '.join(root.itertext())
    except etree.XMLSyntaxError:
        text = value
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def normalize_query(query):
    if not isinstance(query, str) or not query.strip() or len(query) > 300 or any(ord(c) < 32 for c in query):
        fail('RESEARCH_QUERY_INVALID')
    return query.strip()


def doi_query(query):
    query = re.sub(r'^https://(?:dx\.)?doi\.org/', '', query, flags=re.I)
    query = re.sub(r'^doi:\s*', '', query, flags=re.I)
    if re.fullmatch(r'10\.\d{4,9}/[^\s?#<>]{1,260}', query) and '..' not in query:
        return query
    return None


class PublicResearch:
    def __init__(self, reserve, check, timeout=25, client=None):
        self.reserve, self.check = reserve, check
        self.timeout = max(0.1, min(timeout, 25))
        self.client = client

    def request(self, url, *, params=None, max_bytes=MAX_RESPONSE, unavailable='RESEARCH_UNAVAILABLE'):
        parts = urlsplit(url)
        allowed = (url == CROSSREF or url.startswith(CROSSREF + '/') or
                   url == EUROPE_PMC + '/search' or
                   re.fullmatch(re.escape(EUROPE_PMC) + r'/PMC\d{1,12}/fullTextXML', url) or
                   url == UCI_API or
                   re.fullmatch(r'https://archive\.ics\.uci\.edu/static/public/\d{1,6}/data\.csv', url))
        if not allowed or parts.username or parts.password or parts.query or parts.fragment or parts.scheme != 'https':
            fail('PUBLIC_SOURCE_NOT_ALLOWED')
        self.check()
        self.reserve()
        try:
            # A fresh client never shares school credentials or a previous site's cookies.
            with (nullcontext(self.client) if self.client else httpx.Client(trust_env=False, follow_redirects=False,
                    timeout=self.timeout, headers={'User-Agent': 'NTPUHomeworkInOne/0.4 public-research'})) as client:
                with client.stream('GET', url, params=params) as response:
                    if response.status_code in {401, 403, 429}:
                        fail('RATE_LIMITED' if response.status_code == 429 else 'PERMISSION_DENIED')
                    if response.status_code != 200:
                        fail(unavailable)
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        self.check()
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            fail('RESEARCH_RESPONSE_TOO_LARGE' if unavailable == 'RESEARCH_UNAVAILABLE' else 'DATASET_TOO_LARGE')
                    return bytes(data)
        except httpx.HTTPError:
            fail('NETWORK_UNAVAILABLE')

    def crossref(self, query):
        doi = doi_query(query)
        raw = self.request(CROSSREF + '/' + quote(doi, safe='') if doi else CROSSREF,
                           params=None if doi else {'query.bibliographic': query, 'rows': 5})
        try:
            message = json.loads(raw)['message']
            items = [message] if doi else message['items'][:5]
            if not isinstance(items, list):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            fail('RESEARCH_UNAVAILABLE')
        sources = []
        for item in items:
            title = (item.get('title') or [''])[0]
            if not isinstance(title, str) or not title.strip():
                continue
            source_doi = str(item.get('DOI', ''))[:300]
            source = {'doi': source_doi, 'title': plain_xml(title, 500),
                'publisher': str(item.get('publisher', ''))[:200],
                'published': item.get('published', {}).get('date-parts', []),
                'provider': 'Crossref', 'url': 'https://doi.org/' + quote(source_doi, safe='/') if doi_query(source_doi) else '',
                'authors': [str(author.get('given', '')) + ' ' + str(author.get('family', ''))
                            for author in item.get('author', [])[:10] if isinstance(author, dict)],
                'abstract': plain_xml(item.get('abstract', '')), 'content_scope': 'metadata_and_abstract',
                'full_text_status': 'not_retrieved', 'retrieval_url': CROSSREF}
            sources.append(source)
        return sources

    def europepmc(self, query, full_text=False):
        doi = doi_query(query)
        raw = self.request(EUROPE_PMC + '/search', params={
            'query': 'DOI:"' + doi + '"' if doi else query, 'format': 'json', 'resultType': 'core', 'pageSize': 5})
        try:
            items = json.loads(raw)['resultList']['result'][:5]
        except (ValueError, KeyError, TypeError):
            fail('RESEARCH_UNAVAILABLE')
        sources, fetched = [], 0
        for item in items:
            pmcid = item.get('pmcid', '')
            if not isinstance(pmcid, str) or not re.fullmatch(r'PMC\d{1,12}', pmcid):
                pmcid = ''
            title = plain_xml(item.get('title', ''), 500)
            if not title:
                continue
            source = {'doi': str(item.get('doi', ''))[:300], 'title': title, 'pmcid': pmcid,
                'publisher': str(item.get('journalInfo', {}).get('journal', {}).get('title', ''))[:200],
                'published': str(item.get('firstPublicationDate', item.get('pubYear', '')))[:30],
                'provider': 'Europe PMC', 'url': 'https://europepmc.org/articles/' + pmcid if pmcid else '',
                'authors': [str(a.get('fullName', ''))[:200] for a in item.get('authorList', {}).get('author', [])[:10]],
                'abstract': plain_xml(item.get('abstractText', '')), 'content_scope': 'metadata_and_abstract',
                'open_access': item.get('isOpenAccess') == 'Y', 'full_text_status': 'not_open_access',
                'retrieval_url': EUROPE_PMC + '/search'}
            if source['open_access']:
                source['full_text_status'] = 'not_requested' if not full_text else 'full_text_limit'
            if full_text and source['open_access'] and pmcid and fetched < 2:
                fetched += 1
                try:
                    full_url = EUROPE_PMC + '/' + pmcid + '/fullTextXML'
                    content = self.request(full_url, max_bytes=MAX_FULLTEXT)
                    source.update(parse_fulltext(content), retrieval_url=full_url)
                except Exception as exc:
                    # Authentication, cancellation and budgets remain fatal.
                    if getattr(exc, 'code', '') not in {'NETWORK_UNAVAILABLE', 'RESEARCH_UNAVAILABLE',
                                                       'RESEARCH_RESPONSE_TOO_LARGE', 'RESEARCH_FULLTEXT_INVALID'}:
                        raise
                    source['full_text_status'] = getattr(exc, 'code')
            sources.append(source)
        return sources

    def uci_metadata(self, identifier):
        if type(identifier) is not int or not 1 <= identifier <= 100000:
            fail('DATASET_NOT_ALLOWED')
        raw = self.request(UCI_API, params={'id': identifier}, unavailable='DATASET_UNAVAILABLE')
        try:
            data = json.loads(raw)['data']
            expected = f'https://archive.ics.uci.edu/static/public/{identifier}/data.csv'
            if data['data_url'] != expected or data['uci_id'] != identifier:
                fail('DATASET_NOT_ALLOWED')
            return {'title': str(data['name'])[:500], 'url': expected,
                'page': f'https://archive.ics.uci.edu/dataset/{identifier}', 'license': 'Check the linked UCI dataset license',
                'delimiter': ',', 'description': plain_xml(data.get('abstract', ''), 1000),
                'uci_id': identifier, 'doi': str(data.get('dataset_doi', ''))[:300]}
        except (ValueError, KeyError, TypeError):
            fail('DATASET_UNAVAILABLE')


def parse_fulltext(content):
    if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
        fail('RESEARCH_FULLTEXT_INVALID')
    try:
        root = etree.fromstring(content, parser=etree.XMLParser(resolve_entities=False, no_network=True))
        if root.tag != 'article':
            raise ValueError()
        body = root.find('body')
        if body is None:
            raise ValueError()
        parts = []
        for node in body.iter():
            if node.tag in {'title', 'p'}:
                text = re.sub(r'\s+', ' ', ' '.join(node.itertext())).strip()
                if text:
                    parts.append(text)
        text = '\n\n'.join(parts)
        if not text:
            raise ValueError()
        license_node = root.find('.//permissions/license')
        license_text = ' '.join(license_node.itertext()).strip()[:1000] if license_node is not None else 'Not specified in API document'
        license_url = license_node.get('{http://www.w3.org/1999/xlink}href', '')[:500] if license_node is not None else ''
        return {'full_text': text[:MAX_TEXT], 'full_text_characters': len(text),
                'full_text_source_sha256': hashlib.sha256(content).hexdigest(),
                'full_text_truncated': len(text) > MAX_TEXT, 'full_text_status': 'retrieved',
                'content_scope': 'open_access_full_text_excerpt', 'license': license_text,
                'license_url': license_url}
    except (etree.XMLSyntaxError, ValueError):
        fail('RESEARCH_FULLTEXT_INVALID')
