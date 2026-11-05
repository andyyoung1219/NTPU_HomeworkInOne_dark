"""Public research, open-access text, and UCI provenance without school traffic."""
import concurrent.futures
import hashlib
import json
import zipfile

import httpx
import pytest
from test_completion import CompletionAI
from test_mvp import await_task
from test_mvp import service as service
from test_p3 import p3_request

from backend.agent import completion
from backend.agent.contracts import AgentOptions, digest
from backend.integrations.research import (
    CROSSREF,
    MAX_TEXT,
    PublicResearch,
    normalize_query,
    parse_fulltext,
)
from backend.services.exports import export_extra
from backend.services.study import StudyError

ARTICLE = b'''<article><front><article-meta><permissions><license
xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="https://creativecommons.org/licenses/by/4.0/">
<license-p>CC BY 4.0</license-p></license></permissions></article-meta></front>
<body><sec><title>Results</title><p>Observed evidence from an open article.</p></sec></body></article>'''


def mock_client(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs:
        original(**kwargs, transport=httpx.MockTransport(handler)))


def responses(request):
    assert not request.headers.get('cookie')
    assert not request.headers.get('authorization')
    if request.url.host == 'api.crossref.org':
        return httpx.Response(200, json={'message': {'items': [
            {'DOI': '10.1234/example', 'title': ['Public article']}]}})
    if request.url.path.endswith('fullTextXML'):
        return httpx.Response(200, content=ARTICLE)
    return httpx.Response(200, json={'resultList': {'result': [{
        'title': 'Public article', 'pmcid': 'PMC12345', 'doi': '10.1234/example',
        'abstractText': '<p>A measured abstract.</p>', 'isOpenAccess': 'Y'}]}})


def test_arbitrary_topic_and_provider_are_snapshot_fields():
    plan = completion.CompletionPlan(objective='Compare sources', deliverable_type='report',
        research_query='renewable energy adoption in Taiwan')
    assert plan.research_query == 'renewable energy adoption in Taiwan'
    options = AgentOptions(research_query='social science', research_provider='combined', research_full_text=True)
    assert options.research_full_text
    assert 'iris' in completion.DATASETS and 'abalone' in completion.DATASETS


def test_combined_research_fulltext_hashes_citations_and_zip(service, monkeypatch):
    service.ai = CompletionAI()
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return responses(request)
    mock_client(monkeypatch, handler)
    task = service.create_task({**p3_request(service), 'allow_external_research': True,
        'agent': {'research_query': 'renewable energy', 'research_provider': 'combined',
                  'research_full_text': True, 'max_requests': 5}}, 'expanded-research')
    service.approve(task['id'], task['request_sha256'], external=True)
    result = await_task(service, task['id'])
    assert result['status'] == 'waiting_review', result.get('error')
    assert len(calls) == 3
    research = next(value for value in result['tool_results'].values() if value.get('provider') == 'PublicResearch')
    assert len(research['sources']) == 1  # Same DOI: prefer the actual OA excerpt.
    source = research['sources'][0]
    assert source['content_scope'] == 'open_access_full_text_excerpt'
    assert 'Observed evidence' in source['full_text']
    assert source['full_text_source_sha256'] == hashlib.sha256(ARTICLE).hexdigest()
    assert source['file_id'].startswith('research:')
    assert result['source_hashes'][source['file_id']] == digest(source)
    assert 'https://europepmc.org/articles/PMC12345' in result['draft']
    service.save_draft(task['id'], result['draft'], True, accept_incomplete=True)
    exported = export_extra(service, task['id'], 'zip', True)
    with zipfile.ZipFile(service.safe_path(exported['exports']['zip']['path'])) as archive:
        snapshot = json.loads(archive.read('sources/' + digest(source) + '.json'))
        assert snapshot['full_text'] == source['full_text']


def test_combined_research_preserves_usable_provider_on_network_failure(service, monkeypatch):
    def handler(request):
        if request.url.host == 'api.crossref.org':
            return httpx.Response(503)
        return responses(request)
    mock_client(monkeypatch, handler)
    task = service.create_task({**p3_request(service), 'allow_external_research': True,
        'agent': {'research_query': 'malaria', 'research_provider': 'combined', 'max_requests': 4}}, 'partial-provider')
    service.approve(task['id'], task['request_sha256'], external=True)
    result = await_task(service, task['id'])
    assert result['error'] is None
    research = next(value for value in result['tool_results'].values() if value.get('provider') == 'PublicResearch')
    assert research['issues'] == [{'provider': 'Crossref', 'code': 'RESEARCH_UNAVAILABLE'}]
    assert research['sources'][0]['provider'] == 'Europe PMC'


def test_research_reserves_each_request_and_budget_stops_before_network(service, monkeypatch):
    calls = []
    mock_client(monkeypatch, lambda request: calls.append(str(request.url)) or responses(request))
    task = service.create_task({**p3_request(service), 'allow_external_research': True,
        'agent': {'research_query': 'topic', 'research_provider': 'combined', 'max_requests': 1}}, 'new-budget')
    service.approve(task['id'], task['request_sha256'], external=True)
    result = await_task(service, task['id'])
    assert result['error'] == 'REQUEST_BUDGET_EXCEEDED'
    assert len(calls) == 1 and result['checkpoint']['requests_used'] == 1
    assert not service.ai.calls


def test_doi_lookup_uses_fixed_host_not_resolver_redirect(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'message': {'DOI': '10.1234/example', 'title': ['Exact paper']}})
    mock_client(monkeypatch, handler)
    source = PublicResearch(lambda: None, lambda: None).crossref('https://doi.org/10.1234/example')[0]
    assert calls[0].url.host == 'api.crossref.org'
    assert source['doi'] == '10.1234/example'
    assert not calls[0].url.params


@pytest.mark.parametrize('url', ['http://127.0.0.1/secret', 'https://evil.test/file',
    'https://www.ebi.ac.uk/europepmc/webservices/rest/../../secret',
    'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/fullTextXML?redirect=evil',
    'https://user@api.crossref.org/works'])
def test_public_requests_reject_arbitrary_targets_before_reserving(url):
    calls = []
    with pytest.raises(StudyError, match='PUBLIC_SOURCE_NOT_ALLOWED'):
        PublicResearch(lambda: calls.append(1), lambda: None).request(url)
    assert not calls


def test_fulltext_rejects_entities_and_reports_truncation():
    with pytest.raises(StudyError, match='FULLTEXT_INVALID'):
        parse_fulltext(b'<!DOCTYPE article [<!ENTITY x SYSTEM "file:///etc/passwd">]><article><body><p>&x;</p></body></article>')
    result = parse_fulltext(b'<article><body><p>' + b'a' * (MAX_TEXT + 20) + b'</p></body></article>')
    assert len(result['full_text']) == MAX_TEXT
    assert result['full_text_truncated'] is True


def test_remote_pmc_ids_cannot_create_arbitrary_fetches(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'resultList': {'result': [{
            'title': 'Unsafe record', 'pmcid': '../admin', 'isOpenAccess': 'Y'}]}})
    mock_client(monkeypatch, handler)
    source = PublicResearch(lambda: None, lambda: None).europepmc('topic', True)[0]
    assert len(calls) == 1 and source['pmcid'] == '' and not source.get('full_text')


@pytest.mark.parametrize('status,code', [(302, 'RESEARCH_UNAVAILABLE'), (429, 'RATE_LIMITED'), (403, 'PERMISSION_DENIED')])
def test_redirect_rate_and_permission_do_not_fallback_or_follow(monkeypatch, status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Location': 'http://127.0.0.1/secret'})
    mock_client(monkeypatch, handler)
    with pytest.raises(StudyError, match=code):
        PublicResearch(lambda: None, lambda: None).crossref('topic')
    assert len(calls) == 1


def test_research_response_limit(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(200, content=b'x' * 1_000_001))
    with pytest.raises(StudyError, match='RESEARCH_RESPONSE_TOO_LARGE'):
        PublicResearch(lambda: None, lambda: None).request(CROSSREF)


def test_uci_remote_data_url_must_match_selected_dataset(monkeypatch):
    mock_client(monkeypatch, lambda request: httpx.Response(200, json={
        'data': {'uci_id': 53, 'name': 'Iris', 'data_url': 'http://127.0.0.1/data.csv'}}))
    with pytest.raises(StudyError, match='DATASET_NOT_ALLOWED'):
        PublicResearch(lambda: None, lambda: None).uci_metadata(53)


def test_dynamic_uci_dataset_download_is_profiled_and_hash_bound(service, monkeypatch):
    monkeypatch.setattr(service, 'verify_identity', lambda: None)
    class UCIAI(CompletionAI):
        def begin(self, prompt):
            identifier, future = super().begin(prompt)
            value = json.loads(future.result())
            if 'dataset_catalog' in self.calls[-1]:
                value['uci_dataset_id'] = 53
                replacement = concurrent.futures.Future()
                replacement.set_result(json.dumps(value))
                return identifier, replacement
            return identifier, future
    service.ai = UCIAI()
    calls = []
    data = b'sepal,species\n1.0,a\n3.0,b\n'
    def handler(request):
        calls.append(request)
        if request.url.path == '/api/dataset':
            assert request.url.params['id'] == '53'
            return httpx.Response(200, json={'data': {'uci_id': 53, 'name': 'Iris',
                'data_url': 'https://archive.ics.uci.edu/static/public/53/data.csv'}})
        assert request.url.path == '/static/public/53/data.csv'
        return httpx.Response(200, content=data)
    mock_client(monkeypatch, handler)
    task = service.complete_assignment('2', 'dynamic-uci')
    result = await_task(service, task['id'])
    assert result['status'] == 'waiting_review', result.get('error')
    dataset = result['completion_data']['datasets'][0]
    assert len(calls) == 2 and dataset['file_id'] == 'dataset:uci-53'
    assert dataset['profile']['attributes']['sepal']['mean'] == 2.0
    assert result['source_hashes']['dataset:uci-53'] == hashlib.sha256(data).hexdigest()
    assert dataset['file_id'] in result['report_model']['sources']


def test_bad_query_never_becomes_a_request():
    for query in ['', '\x00private', 'x' * 301]:
        with pytest.raises(StudyError, match='QUERY_INVALID'):
            normalize_query(query)
