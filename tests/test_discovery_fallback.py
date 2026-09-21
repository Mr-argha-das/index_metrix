"""Normal discovery is not an indexing request and never supplies index evidence."""
from urllib.parse import quote
from xml.etree import ElementTree as ET

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from conftest import admin_client, get_csrf, wait_for


def guest(client):
    c = TestClient(client.app)
    c.portal = client.portal
    return c


def submit(client, url):
    admin_client(client)
    response = client.post('/api/index/validate', json={'urls': [url]},
                           headers={'X-CSRF-Token': get_csrf(client)})
    assert response.status_code == 202, response.text
    record = wait_for(client, '/api/index/status?url=' + quote(url, safe=''),
                      lambda row: row.get('referenceSubmissionStatus') == 'UNSUPPORTED'
                      or row.get('submissionStatus') == 'VALIDATION_FAILED')
    return record


@pytest.mark.parametrize('source', ['/docs/report.pdf?fallback=pdf', '/blog/article'])
def test_publication_uses_normal_discovery_not_google_api(client, pdf_server_url, source, monkeypatch):
    class ForbiddenGoogle:
        def __getattr__(self, name):
            raise AssertionError('Fallback must not call any Google operation')
    monkeypatch.setattr(client.app.state.queue, 'gsc', ForbiddenGoogle())
    record = submit(client, pdf_server_url + source)
    assert record['submissionStatus'] == 'DISCOVERY_SUBMITTED'
    assert record['referenceSubmissionStatus'] == 'UNSUPPORTED'
    assert record['referenceSubmissionMechanism'] == 'normal-discovery'
    assert record['referenceSubmissionResult']['googleRequestMade'] is False
    assert record['referenceCrawlStatus'] == 'UNKNOWN'
    assert record['externalCrawlStatus'] == 'FETCH_CHECKED'
    assert record['externalDiscoveryStatus'] == 'DISCOVERY_PENDING'
    assert record['referenceIndexStatus'] == record['externalIndexStatus'] == 'UNKNOWN'
    assert set(record['discoveryChannels']) == {'reference-page', 'internal-links', 'sitemap', 'rss'}
    public = guest(client)
    url = record['referencePath']
    response = public.get(url)
    assert response.status_code == public.head(url).status_code == 200
    assert not public.head(url).content
    dom = BeautifulSoup(response.text, 'html.parser')
    assert dom.find('a', href=pdf_server_url + source)
    assert dom.find('a', href='/references')
    assert dom.find('meta', attrs={'name': 'robots'})['content'] == 'index,follow'
    assert dom.find('link', rel='canonical')['href'] == record['canonical']
    library = BeautifulSoup(public.get('/references').text, 'html.parser')
    assert library.find('a', href=url)
    for endpoint in ('/sitemap.xml', '/rss.xml'):
        xml = public.get(endpoint)
        ET.fromstring(xml.text)
        assert record['referencePage'] in xml.text
    if source == '/blog/article':
        assert record['type'] == 'HTML' and record['classification'] == 'HTML'
        assert record['pages'] is None and record['textLength'] >= 120
        assert record['sha256'] and record['htmlMetadata']['canonical'].endswith('/blog/article')
        assert 'Open Original Web Page' in response.text
        assert 'Validated PDF' not in response.text and '%PDF-' not in response.text
        assert 'not real content' not in response.text
    else:
        assert record['type'] == 'PDF' and record['pages'] == 1
        assert 'View Original PDF' in response.text


@pytest.mark.parametrize('path', ['/blog/challenge', '/blog/noindex', '/blog/thin', '/docs/fake.pdf'])
def test_no_thin_challenge_noindex_or_fake_pdf_publication(client, pdf_server_url, path):
    record = submit(client, pdf_server_url + path + '?fallback=reject')
    assert record['submissionStatus'] == 'VALIDATION_FAILED'
    assert record['referencePage'] is None
    assert record['externalIndexStatus'] == record['referenceIndexStatus'] == 'UNKNOWN'


def test_public_library_get_head_home_and_pagination(client, monkeypatch):
    pages = [dict(slug=f'public-reference-{i}', title=f'Reference {i}', description='Useful document metadata.',
                  published_at='2026-09-21T10:00:00Z') for i in range(101)]
    async def rows(*args, **kwargs):
        return pages
    monkeypatch.setattr(client.app.state.repos.pages, 'all', rows)
    public = guest(client)
    for num, count in ((1, 50), (2, 50), (3, 1)):
        response = public.get(f'/references?page={num}')
        assert response.status_code == 200
        assert len(BeautifulSoup(response.text, 'html.parser').select('article h2 a')) == count
        head = public.head(f'/references?page={num}')
        assert head.status_code == 200 and not head.content
        assert head.headers['content-length'] == response.headers['content-length']
    assert 'rel="next"' in public.get('/references').text
    assert 'rel="prev"' in public.get('/references?page=2').text
    assert public.get('/references?page=4').status_code == 404
    assert public.get('/', follow_redirects=False).headers['location'] == '/references'
    assert '/references' in public.get('/robots.txt').text


def test_reference_evidence_cannot_change_external_states(client, pdf_server_url):
    row = submit(client, pdf_server_url + '/blog/article?evidence=scoped')
    headers = {'X-CSRF-Token': get_csrf(client)}
    result = client.post('/api/index/evidence', headers=headers, json={
        'url': row['referencePage'], 'indexed': True, 'source': 'operator-confirmed',
        'details': 'Test independent evidence of this specific owned reference page.'})
    assert result.status_code == 200
    after = client.get('/api/index/status', params={'url': row['sourceUrl']}).json()
    assert after['referenceIndexStatus'] == 'INDEXED'
    assert after['externalIndexStatus'] == 'UNKNOWN'
    assert after['externalCrawlStatus'] == 'FETCH_CHECKED'
    assert after['externalDiscoveryStatus'] == 'DISCOVERY_PENDING'
    assert after['referenceSubmissionStatus'] == 'UNSUPPORTED'


@pytest.mark.asyncio
async def test_gsc_evidence_updates_only_reference(tmp_path):
    from app.database.feather_store import Database
    from app.database.repositories import Repos
    from app.monitoring.status import apply_gsc_evidence
    db = Database(str(tmp_path)); db.init()
    repos = Repos(db)
    row = await repos.pdfs.insert(normalized_url='https://thirdparty.org/article',
        reference_index_status='UNKNOWN', external_index_status='UNKNOWN',
        external_discovery_status='DISCOVERY_PENDING', external_crawl_status='FETCH_CHECKED')
    await repos.pages.insert(pdf_id=row['id'], page_url='https://v1.indexmetrix.com/pdf/article')
    await apply_gsc_evidence(repos, row['id'], {'inspectionResult': {'indexStatusResult': {
        'verdict': 'PASS', 'coverageState': 'Submitted and indexed', 'lastCrawlTime': '2026-09-20T10:00:00Z'}}})
    result = await repos.pdfs.get(row['id'])
    assert result['reference_index_status'] == 'INDEXED'
    assert result['reference_crawl_status'] == 'SEARCH_ENGINE_CRAWL_EVIDENCE'
    assert result['external_index_status'] == 'UNKNOWN'
    assert result['external_discovery_status'] == 'DISCOVERY_PENDING'
    assert result['external_crawl_status'] == 'FETCH_CHECKED'


def test_default_origin_and_origin_validation():
    from app.config import Settings
    settings = Settings(_env_file=None, public_base_url='https://v1.indexmetrix.com/')
    assert settings.public_base_url == 'https://v1.indexmetrix.com'
    for invalid in ('https://v1.indexmetrix.com/path', 'https://u:p@v1.indexmetrix.com', 'https://v1.indexmetrix.com?q=x'):
        with pytest.raises(ValueError):
            Settings(_env_file=None, public_base_url=invalid)


def test_google_fallback_has_no_google_network_or_secret_dependencies():
    import inspect
    from app.publishing import discovery
    source = inspect.getsource(discovery)
    assert 'httpx' not in source and 'aiohttp' not in source
    assert 'GOOGLE_CLIENT_SECRET' not in source
    assert 'googleRequestMade' in source


@pytest.mark.parametrize('directive', [
    '<meta name="ROBOTS" content="NOINDEX">',
    '<meta name="robots" content="index,follow"><meta name="robots" content="noindex">',
    '<meta name="robots" content="index,follow"><meta name="googlebot" content="none">',
])
def test_html_restrictive_robot_directives_win(directive):
    from app.pdf.html import extract_html
    result = extract_html((f'<html><head><title>Real source title</title>{directive}</head>'
                           '<body><main>' + 'Useful source text about document metadata. ' * 10
                           + '</main></body></html>').encode(), 'https://example.org/article')
    assert not result['publishable']
    assert 'noindex' in result['rejectionReason']


@pytest.mark.asyncio
@pytest.mark.parametrize('real_evidence', [True, False])
async def test_migration_keeps_reference_proof_separate_and_is_idempotent(tmp_path, real_evidence):
    from app.database.feather_store import Database
    from app.database.repositories import Repos
    from app.database.migrations import repair_status_semantics
    from app.utils import json_dumps, json_loads
    db = Database(str(tmp_path)); db.init()
    repos = Repos(db)
    row = await repos.pdfs.insert(normalized_url='https://external.example/old.pdf',
        index_status='INDEXED', source_index_status='INDEX_UNKNOWN', http_status=200,
        crawl_status='CRAWL_CHECKED', discovery_status='DISCOVERED',
        index_evidence=json_dumps({'source': 'Google Search Console',
                                  'coverage_state': 'Submitted and indexed' if real_evidence else 'Published'}),
        crawl_evidence=json_dumps({'source': 'Google Search Console', 'last_crawl_time': '2026-09-20T10:00:00Z'}))
    page = await repos.pages.insert(pdf_id=row['id'], slug='keep-this-slug',
        page_url='https://v1.indexmetrix.com/pdf/keep-this-slug', sitemap_included=True, rss_included=True,
        published_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:00:00Z')
    page = await repos.pages.get(page['id'])
    await repair_status_semantics(repos)
    first = await repos.pdfs.get(row['id'])
    assert first['reference_index_status'] == ('INDEXED' if real_evidence else 'UNKNOWN')
    assert first['reference_crawl_status'] == 'SEARCH_ENGINE_CRAWL_EVIDENCE'
    assert first['external_index_status'] == 'UNKNOWN'
    assert first['external_crawl_status'] == 'FETCH_CHECKED'
    assert first['external_discovery_status'] == 'DISCOVERY_PENDING'
    assert first['reference_submission_status'] == 'UNSUPPORTED'
    assert json_loads(first['reference_submission_result'])['googleRequestMade'] is False
    assert json_loads(first['discovery_channels']).count('internal-links') == 1
    await repair_status_semantics(repos)
    assert await repos.pdfs.get(row['id']) == first
    assert await repos.pages.get(page['id']) == page
