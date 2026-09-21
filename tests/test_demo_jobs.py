"""Fictional job examples must never impersonate vacancies or official documents."""
import asyncio
import json
import random

import pytest
from bs4 import BeautifulSoup

from app.config import Settings
from app.database.feather_store import Database
from app.database.repositories import Repos
from app.publishing.demo_jobs import generate_demo_job
from app.publishing.pages import create_page_for_pdf, public_page_path, public_page_url
from app.utils import json_loads
from test_discovery_fallback import guest, submit


@pytest.mark.parametrize('source', ['/docs/report.pdf?demo=1', '/blog/article?demo=1'])
def test_new_publication_is_persisted_explicit_demo(client, pdf_server_url, source):
    row = submit(client, pdf_server_url + source)
    assert row['pageKind'] == 'demo-job' and row['isFictionalDemo'] is True
    path = f"/jobs/{row['jobId']}"
    assert row['referencePath'] == path
    assert row['canonical'] == 'http://testserver' + path
    assert row['referenceSubmissionStatus'] == 'UNSUPPORTED'
    assert row['referenceSubmissionResult']['googleRequestMade'] is False
    assert row['referenceIndexStatus'] == row['externalIndexStatus'] == 'UNKNOWN'
    public = guest(client)
    first = public.get(path)
    assert first.status_code == 200
    head = public.head(path)
    assert head.status_code == 200 and head.content == b''
    assert head.headers['content-length'] == first.headers['content-length']
    assert public.get(path).text == first.text
    doc = BeautifulSoup(first.text, 'html.parser')
    assert 'Fictional demo' in doc.title.text and 'Fictional demo' in doc.h1.text
    assert 'Fictional demo — not a real vacancy.' in first.text
    assert 'not facts extracted from the submitted source' in first.text
    assert 'No applications are accepted' in first.text
    assert doc.select_one('#demo-apply button[disabled]')
    assert not doc.find('form')
    assert doc.find('a', href=pdf_server_url + source, target='_blank')
    assert 'noopener' in doc.find('a', href=pdf_server_url + source)['rel']
    assert 'Official Job PDF' not in first.text
    assert 'JobPosting' not in first.text and 'BroadcastEvent' not in first.text
    assert doc.find('link', rel='canonical')['href'] == row['canonical']
    assert doc.find('meta', property='og:url')['content'] == row['canonical']
    assert 'fictional' in doc.find('meta', attrs={'name': 'description'})['content'].lower()
    assert doc.find('meta', attrs={'name': 'robots'})['content'] == 'index,follow'
    assert 'Allow: /jobs/' in public.get('/robots.txt').text
    for endpoint in ('/sitemap.xml', '/rss.xml', '/references'):
        response = public.get(endpoint)
        assert path in response.text
        assert '/pdf/' + row['referenceId'] not in response.text
    for method in (public.get, public.head):
        alias = method('/pdf/' + row['referenceId'], follow_redirects=False)
        assert alias.status_code == 308 and alias.headers['location'] == path


@pytest.mark.parametrize('number', ['0', '-1', '0001', 'invalid', '999999999999999999', '12345678901234567890'])
def test_missing_or_invalid_jobs_are_404(client, number):
    public = guest(client)
    assert public.get('/jobs/' + number).status_code == 404
    assert public.head('/jobs/' + number).status_code == 404


def test_demo_generator_varies_without_inventing_real_vacancies():
    rng = random.Random(3418)
    profiles = [generate_demo_job(i, rng) for i in range(1, 101)]
    # Measure full profiles without counting the guaranteed unique identifier.
    bodies = {json.dumps({k:v for k,v in item.items() if k!='number'}, sort_keys=True) for item in profiles}
    assert len(bodies) >= 99  # fixed-seed regression sample, NOT a production guarantee
    for job in profiles:
        assert job['isFictional'] and job['acceptsApplications'] is False
        assert job['applicationStatus'] == 'DEMO_ONLY'
        assert job['company'].startswith('Demo ')
        assert job['skills'] and job['responsibilities'] and job['benefits']
        assert 'hypothetical' in job['salary']


@pytest.mark.asyncio
async def test_numbers_are_atomic_persistent_and_not_recycled(tmp_path):
    db = Database(str(tmp_path)); db.init(); repos = Repos(db)
    numbers = await asyncio.gather(*(repos.settings.reserve_counter('internal_next_demo_job') for _ in range(100)))
    assert sorted(numbers) == list(range(1,101))
    reopened = Database(str(tmp_path)); reopened.init()
    assert await Repos(reopened).settings.reserve_counter('internal_next_demo_job') == 101


@pytest.mark.asyncio
@pytest.mark.parametrize('source_title', ['Actual source title', None])
async def test_demo_is_not_regenerated_and_old_reference_pages_remain_real(tmp_path, monkeypatch, source_title):
    db = Database(str(tmp_path)); db.init(); repos = Repos(db)
    settings = Settings(_env_file=None, public_base_url='https://v1.indexmetrix.com')
    source = await repos.pdfs.insert(normalized_url='https://source.example/article', title=source_title,
                                     resource_type='HTML', sha256='a'*64, source_domain='source.example')
    first = await create_page_for_pdf(repos, settings, source, None)
    old = json_loads(first['demo_job'])
    assert first['job_number'] == 1 and old['number'] == 1
    assert public_page_url(settings.public_base_url, first) == 'https://v1.indexmetrix.com/jobs/1'
    def forbidden(*args, **kwargs):
        raise AssertionError('Refresh/retry/restart must not regenerate fictional details')
    monkeypatch.setattr('app.publishing.demo_jobs.generate_demo_job', forbidden)
    reopened = Database(str(tmp_path)); reopened.init(); repos = Repos(reopened)
    second = await create_page_for_pdf(repos, settings, source, None)
    assert first == second
    await repos.pages.delete(first['id'])
    # Deleted public URLs cannot be allocated to a different future page.
    assert await repos.settings.reserve_counter('internal_next_demo_job') == 2
    legacy = await repos.pages.insert(pdf_id=source['id'], slug='legacy-reference', title='Actual source title',
        description='Existing real reference', page_url='https://v1.indexmetrix.com/pdf/legacy-reference')
    updated = await create_page_for_pdf(repos, settings, source, None)
    assert not updated['demo_job'] and not updated['page_kind']
    assert public_page_path(updated) == '/pdf/legacy-reference'


def test_existing_legacy_pdf_page_keeps_original_route(client, monkeypatch):
    from app.database.repositories import Repos
    repos = client.app.state.repos
    original_find = repos.pages.find
    original_get = repos.pdfs.get
    async def find(predicate, *args, **kwargs):
        old = {'id': 900000, 'pdf_id': 900000, 'slug': 'legacy-demo-test', 'title': 'Real document title',
               'description': 'Metadata from the real source.', 'published_at': '2025-01-01T00:00:00Z',
               'updated_at': '2025-01-01T00:00:00Z'}
        return [old] if predicate(old) else await original_find(predicate, *args, **kwargs)
    async def get(number):
        if number == 900000:
            return {'id': number, 'title': 'Real document title', 'normalized_url': 'https://source.example/doc.pdf',
                    'original_url': 'https://source.example/doc.pdf', 'classification': 'TEXT_PDF', 'resource_type': 'PDF'}
        return await original_get(number)
    monkeypatch.setattr(repos.pages, 'find', find)
    monkeypatch.setattr(repos.pdfs, 'get', get)
    response = guest(client).get('/pdf/legacy-demo-test', follow_redirects=False)
    assert response.status_code == 200
    assert 'Fictional job demo' not in response.text
    assert 'Real document title' in response.text
