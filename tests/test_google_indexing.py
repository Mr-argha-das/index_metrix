"""No live Google calls: actual HTTP payloads exercised using MockTransport."""
import asyncio
import json
import stat
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.database.feather_store import Database
from app.database.repositories import Repos
from app.integrations.google_indexing import (ENDPOINT, SCOPES, SITES_URL, TOKEN_URL, GoogleIndexingClient,
    IndexingError, credential_path, store_credentials, validate_credentials)
from app.publishing.real_jobs import RealJobIn, is_open
from app.queue.indexing import (ENABLED_KEY, QUOTA_KEY, ensure_notification, process_notification,
                               quota_delay, reconcile)
from app.queue.manager import JOB_INDEX_NOTIFY, QueueManager
from app.utils import json_dumps, json_loads, utcnow, utcnow_iso
from conftest import admin_client, get_csrf, wait_for
from test_discovery_fallback import guest, submit


@pytest.fixture(scope='module')
def key_bytes():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return json.dumps({'type': 'service_account', 'project_id': 'unit-test-project',
        'client_email': 'test@unit-test-project.iam.gserviceaccount.com', 'token_uri': TOKEN_URL,
        'private_key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()}).encode()


def vacancy(**overrides):
    return dict(title='Backend Engineer', company='Test Organization', company_url='https://example.org/',
                apply_url='https://example.org/careers/apply', description='Build and maintain backend applications. Work with our engineering team to design APIs, test changes, review code, document releases and investigate customer issues. Work is performed at our Jaipur office during regular business hours.',
                qualifications='Experience with Python, SQL, API design, testing and collaborative software development.',
                employment_type='FULL_TIME', city='Jaipur', region='Rajasthan', country='IN',
                date_posted=(utcnow().date() - timedelta(days=2)).isoformat(), valid_through=(utcnow().date() + timedelta(days=30)).isoformat(),
                authorized_real_vacancy=True) | overrides


@pytest.fixture
def manager(tmp_path):
    settings = Settings(_env_file=None, data_dir=str(tmp_path), public_base_url='https://v1.indexmetrix.com')
    db = Database(str(tmp_path)); db.init()
    return QueueManager(Repos(db), settings)


async def real_page(manager, **overrides):
    n = await manager.repos.settings.reserve_counter('internal_next_demo_job')
    return await manager.repos.pages.insert(page_kind='real-job', job_number=n, real_job=json_dumps(vacancy()),
        reviewed_by=1, job_status='OPEN', indexing_revision=1, published_at=utcnow_iso(), **overrides)


@pytest.mark.parametrize('change', [
    {'authorized_real_vacancy': False}, {'title': '<script>evil</script>'}, {'description': 'too short'},
    {'apply_url': 'javascript:alert(1)'}, {'apply_url': 'https://127.0.0.1/apply'},
    {'apply_url': 'https://localhost/apply'}, {'company_url': 'https://user:pass@example.org/'},
    {'date_posted': (utcnow().date() + timedelta(days=1)).isoformat()},
    {'valid_through': (utcnow().date() - timedelta(days=1)).isoformat()},
    {'isFictional': False}, {'notification_url': 'https://unowned.org/'},
])
def test_real_vacancy_validation_rejects_invalid_or_injected_data(change):
    with pytest.raises(ValueError):
        RealJobIn.model_validate(vacancy(**change))


def test_credentials_permissions_and_no_arbitrary_token_endpoint(manager, key_bytes):
    data = store_credentials(manager.settings, key_bytes)
    path = credential_path(manager.settings)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not list(path.parent.glob('.key-*'))
    data['token_uri'] = 'https://attacker.example/token'
    with pytest.raises(IndexingError):
        store_credentials(manager.settings, json.dumps(data).encode())
    assert path.read_bytes()  # invalid replacement does not destroy old key
    for raw in (b'[]', b'not-json SECRET', b'x' * 32769, b'{"private_key":"PRIVATE_SECRET"}'):
        with pytest.raises(IndexingError) as error:
            validate_credentials(raw)
        assert 'PRIVATE_SECRET' not in str(error.value)


@pytest.mark.asyncio
async def test_official_http_flow_checks_ownership_and_sends_correct_json(monkeypatch, key_bytes):
    calls = []
    def handler(request):
        calls.append(request)
        if str(request.url) == TOKEN_URL:
            from urllib.parse import parse_qs
            import base64
            jwt = parse_qs(request.content.decode())['assertion'][0]
            encoded = jwt.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            assert claims['scope'] == SCOPES and claims['aud'] == TOKEN_URL
            return httpx.Response(200, json={'access_token': 'TEST_TOKEN', 'expires_in': 3600})
        assert request.headers['authorization'] == 'Bearer TEST_TOKEN'
        if str(request.url) == SITES_URL:
            return httpx.Response(200, json={'siteEntry': [{'siteUrl': 'https://v1.indexmetrix.com/', 'permissionLevel': 'siteOwner'}]})
        assert str(request.url) == ENDPOINT
        assert request.headers['content-type'] == 'application/json'
        assert json.loads(request.content) == {'url': 'https://v1.indexmetrix.com/jobs/1', 'type': 'URL_UPDATED'}
        return httpx.Response(200, json={'urlNotificationMetadata': {'latestUpdate': {'notifyTime': '2026-09-23T10:00:00Z'}}})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.integrations.google_indexing.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    result = await GoogleIndexingClient(validate_credentials(key_bytes), 'https://v1.indexmetrix.com').publish('https://v1.indexmetrix.com/jobs/1', 'URL_UPDATED')
    assert len(calls) == 3 and result['httpStatus'] == 200
    assert result['notifyTime'] == '2026-09-23T10:00:00Z'
    assert 'TEST_TOKEN' not in str(result) and 'not confirmed' in result['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('permission,site', [('siteFullUser','https://v1.indexmetrix.com/'), ('siteOwner','https://evil.org/'), ('siteOwner','https://v1.indexmetrix.com.attacker.org/')])
async def test_owner_or_origin_mismatch_never_notifies(monkeypatch, key_bytes, permission, site):
    def handler(request):
        assert str(request.url) != ENDPOINT
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={'access_token':'test'})
        return httpx.Response(200, json={'siteEntry':[{'siteUrl':site, 'permissionLevel':permission}]})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.integrations.google_indexing.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(IndexingError, match='not an owner'):
        await GoogleIndexingClient(validate_credentials(key_bytes), 'https://v1.indexmetrix.com').publish('https://v1.indexmetrix.com/jobs/1','URL_UPDATED')


@pytest.mark.asyncio
@pytest.mark.parametrize('url,kind', [('https://unowned.org/jobs/1','URL_UPDATED'), ('https://v1.indexmetrix.com/pdf/test','URL_UPDATED'), ('https://v1.indexmetrix.com/jobs/1?bad=1','URL_UPDATED'), ('https://v1.indexmetrix.com/jobs/1','OTHER')])
async def test_client_rejects_unowned_nonjob_urls_before_auth(key_bytes, url, kind):
    with pytest.raises(IndexingError, match='owned numeric'):
        await GoogleIndexingClient(validate_credentials(key_bytes), 'https://v1.indexmetrix.com').publish(url, kind)


@pytest.mark.asyncio
async def test_queue_dedupe_paused_and_accepted_is_not_indexed(manager, key_bytes, monkeypatch):
    page = await real_page(manager)
    first = await ensure_notification(manager, page)
    assert (await ensure_notification(manager, page))['id'] == first['id']
    await process_notification(manager, first['id'])
    assert (await manager.repos.pages.get(page['id']))['indexing_status'] == 'NOT_CONFIGURED'
    assert (await manager.repos.jobs.get(first['id']))['attempts'] == 0
    store_credentials(manager.settings, key_bytes)
    await manager.repos.settings.set_key(ENABLED_KEY, 'true')
    await manager.repos.jobs.update(first['id'], next_attempt_at=None)
    calls = []
    async def publish(self, url, kind):
        calls.append((url, kind)); return {'httpStatus':200,'googleRequestMade':True}
    monkeypatch.setattr(GoogleIndexingClient, 'publish', publish)
    await process_notification(manager, first['id'])
    await process_notification(manager, first['id'])
    assert len(calls) == 1
    assert (await manager.repos.pages.get(page['id']))['indexing_status'] == 'ACCEPTED'
    assert (await manager.repos.jobs.get(first['id']))['status'] == 'DONE'
    assert (await ensure_notification(manager, page, retry=True))['id'] == first['id']
    assert len(await manager.repos.pdfs.all()) == 0  # no fake source/index evidence
    await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,retryable', [(429,True),(503,True),(403,False),(400,False)])
async def test_retry_after_and_permanent_failures(manager, key_bytes, monkeypatch, status, retryable):
    page = await real_page(manager)
    job = await ensure_notification(manager, page)
    store_credentials(manager.settings, key_bytes)
    await manager.repos.settings.set_key(ENABLED_KEY,'true')
    async def fail(*args):
        raise IndexingError(f'Notification HTTP {status}', retryable=retryable, status=status, retry_after=123, sent=True)
    monkeypatch.setattr(GoogleIndexingClient, 'publish', fail)
    before = time.time()
    await process_notification(manager, job['id'])
    saved = await manager.repos.jobs.get(job['id'])
    assert saved['status'] == ('RETRY_WAITING' if retryable else 'FAILED')
    assert saved['attempts'] == 1
    if retryable:
        from app.utils import parse_iso
        assert parse_iso(saved['next_attempt_at']).timestamp() >= before + 122
    result = json_loads((await manager.repos.pages.get(page['id']))['indexing_result'])
    assert result['httpStatus'] == status and result['googleRequestMade']
    await manager.stop()


@pytest.mark.asyncio
async def test_quota_ledger_survives_restart_and_key_rotation(manager, key_bytes):
    manager.settings.google_indexing_daily_limit = 2
    manager.settings.google_indexing_minute_limit = 1
    now = time.time()
    assert await quota_delay(manager, now) == 0
    assert 59 <= await quota_delay(manager, now + .1) <= 60
    assert await quota_delay(manager, now + 61) == 0
    db = Database(manager.settings.data_dir); db.init()
    reopened = QueueManager(Repos(db), manager.settings)
    store_credentials(manager.settings, key_bytes)
    assert await quota_delay(reopened, now + 120) > 86000
    assert len(json_loads((await reopened.repos.settings.get_key(QUOTA_KEY))['value'])) == 2


@pytest.mark.asyncio
async def test_reconcile_recovers_orphan_and_expiry_without_submitting_demo(manager):
    page = await real_page(manager)
    demo = await manager.repos.pages.insert(page_kind='demo-job', job_number=999, demo_job='{"isFictional":true}')
    await reconcile(manager)
    jobs = await manager.repos.jobs.all()
    assert len(jobs) == 1
    with pytest.raises(IndexingError):
        await ensure_notification(manager, demo)
    expired = vacancy(valid_through=(utcnow().date()-timedelta(days=1)).isoformat())
    await manager.repos.pages.update(page['id'], real_job=json_dumps(expired))
    await reconcile(manager)
    page = await manager.repos.pages.get(page['id'])
    assert page['job_status'] == 'CLOSED' and not is_open(page)
    assert not page['sitemap_included'] and not page['rss_included']
    jobs = await manager.repos.jobs.all()
    assert len(jobs) == 2 and json_loads(jobs[-1]['payload'])['type'] == 'URL_DELETED'
    await process_notification(manager, jobs[0]['id'])
    assert (await manager.repos.jobs.get(jobs[0]['id']))['status'] == 'CANCELLED'


def test_real_job_ui_publish_schema_dedupe_and_close(client):
    admin_client(client)
    headers = {'X-CSRF-Token':get_csrf(client)}
    response = client.post('/api/real-jobs', json=vacancy(), headers=headers)
    assert response.status_code == 201, response.text
    row = response.json()
    duplicate = client.post('/api/real-jobs', json=vacancy(), headers=headers).json()
    assert duplicate['duplicate'] and duplicate['id'] == row['id']
    public = guest(client)
    page = public.get(row['path']); assert page.status_code == 200
    assert public.head(row['path']).status_code == 200 and public.head(row['path']).content == b''
    doc = BeautifulSoup(page.text, 'html.parser')
    schema = json.loads(doc.find('script',type='application/ld+json').string)
    assert schema['@type'] == 'JobPosting' and schema['title'] == vacancy()['title']
    assert schema['hiringOrganization']['name'] == vacancy()['company']
    assert doc.find('a', href=vacancy()['apply_url']).text == 'Apply with employer'
    assert row['url'] == doc.find('link',rel='canonical')['href']
    assert public.get('/pdf/real-job-' + str(row['number']), follow_redirects=False).status_code == 308
    for route in ('/references','/sitemap.xml','/rss.xml'):
        assert row['path'] in public.get(route).text
    assert row['indexStatus'] == 'UNKNOWN'
    assert client.get('/real-jobs').status_code == client.get('/google-indexing').status_code == 200
    update = client.put('/api/real-jobs/'+str(row['id']), json=vacancy(title='Senior Backend Engineer'), headers=headers)
    assert update.status_code == 200
    assert public.get(row['path']).text.count('Senior Backend Engineer') >= 2
    bad = client.put('/api/real-jobs/'+str(row['id']), json=vacancy(date_posted=utcnow().date().isoformat()), headers=headers)
    assert bad.status_code == 400
    assert client.post(f"/api/real-jobs/{row['id']}/close", headers=headers).status_code == 200
    closed = public.get(row['path'])
    assert 'noindex,follow' in closed.text and 'JobPosting' not in closed.text and 'Apply with employer' not in closed.text
    for route in ('/references','/sitemap.xml','/rss.xml'):
        assert row['path'] not in public.get(route).text
    assert client.put('/api/real-jobs/'+str(row['id']), json=vacancy(), headers=headers).status_code == 409


def test_upload_no_secret_disclosure_csrf_and_permission(client, key_bytes):
    public = guest(client)
    assert public.get('/api/google-indexing').status_code == 401
    assert public.post('/api/google-indexing/credentials', content=key_bytes).status_code in (401,403)
    admin_client(client)
    assert client.post('/api/google-indexing/credentials', content=key_bytes).status_code == 403
    headers={'X-CSRF-Token':get_csrf(client), 'Content-Type':'application/json'}
    assert client.post('/api/google-indexing/credentials', content=b'x'*32769, headers=headers).status_code == 413
    response = client.post('/api/google-indexing/credentials', content=key_bytes, headers=headers)
    assert response.status_code == 200 and response.json()['configured'] and not response.json()['enabled']
    secret = json.loads(key_bytes)['private_key'].splitlines()[1]
    for route in ('/api/google-indexing','/google-indexing','/api/settings','/api/logs','/api/system/status'):
        reply = client.get(route)
        assert secret not in reply.text and 'BEGIN PRIVATE KEY' not in reply.text
    assert client.get('/api/google-indexing').headers['cache-control'] == 'no-store'
    for route in ('/data/secrets/google-indexing.json','/secrets/google-indexing.json','/static/../data/secrets/google-indexing.json'):
        assert public.get(route).status_code == 404
    assert client.post('/api/google-indexing/enable', json={'approved_api_usage':True},headers=headers).status_code == 400  # test HTTP origin
    assert client.delete('/api/google-indexing/credentials',headers=headers).status_code == 200
    assert not credential_path(client.app.state.settings).exists()


def test_normal_resource_and_demo_can_never_use_real_job_endpoints(client, pdf_server_url):
    row = submit(client, pdf_server_url+'/docs/report.pdf?indexing-ineligible=1')
    page = client.portal.call(client.app.state.repos.pages.find, lambda p:p.get('pdf_id') == row['id'])[0]
    headers={'X-CSRF-Token':get_csrf(client)}
    for action in ('retry','close'):
        assert client.post(f"/api/real-jobs/{page['id']}/{action}",headers=headers).status_code == 404
    assert client.put(f"/api/real-jobs/{page['id']}",json=vacancy(),headers=headers).status_code == 404
    assert row['referenceSubmissionResult']['googleRequestMade'] is False


@pytest.mark.asyncio
async def test_indexing_state_changes_do_not_manufacture_publication_freshness(manager, key_bytes, monkeypatch):
    old = '2024-01-02T03:04:05+00:00'
    page = await real_page(manager, updated_at=old)
    job = await ensure_notification(manager, page)
    await process_notification(manager, job['id'])
    assert (await manager.repos.pages.get(page['id']))['updated_at'] == old
    store_credentials(manager.settings, key_bytes)
    await manager.repos.settings.set_key(ENABLED_KEY, 'true')
    await manager.repos.jobs.update(job['id'], next_attempt_at=None)
    async def accepted(*args):
        return {'httpStatus':200, 'googleRequestMade':True}
    monkeypatch.setattr(GoogleIndexingClient, 'publish', accepted)
    await process_notification(manager, job['id'])
    saved = await manager.repos.pages.get(page['id'])
    assert saved['updated_at'] == old and saved['published_at'] == page['published_at']
    await manager.stop()


@pytest.mark.asyncio
async def test_restart_resumes_interrupted_notification_without_extra_deduped_job(manager, key_bytes, monkeypatch):
    page = await real_page(manager)
    job = await ensure_notification(manager, page)
    store_credentials(manager.settings, key_bytes)
    await manager.repos.settings.set_key(ENABLED_KEY, 'true')
    await manager.repos.jobs.update(job['id'], status='RUNNING', attempts=1)
    calls = []
    async def accepted(self, url, kind):
        calls.append((url,kind)); return {'httpStatus':200,'googleRequestMade':True}
    monkeypatch.setattr(GoogleIndexingClient, 'publish', accepted)
    db = Database(manager.settings.data_dir); db.init()
    reopened = QueueManager(Repos(db), manager.settings)
    await reopened.start()
    try:
        for _ in range(100):
            if (await reopened.repos.jobs.get(job['id']))['status'] == 'DONE': break
            await asyncio.sleep(.01)
        assert len(calls) == 1
        assert len(await reopened.repos.jobs.all()) == 1
        assert (await reopened.repos.jobs.get(job['id']))['attempts'] == 2
    finally:
        await reopened.stop()


@pytest.mark.asyncio
async def test_cancel_is_visible_and_explicit_retry_requeues_once(manager):
    page = await real_page(manager)
    job = await ensure_notification(manager,page)
    assert await manager.cancel(job['id'])
    assert (await manager.repos.pages.get(page['id']))['indexing_status'] == 'CANCELLED'
    await reconcile(manager)
    assert (await manager.repos.jobs.get(job['id']))['status'] == 'CANCELLED'
    assert (await ensure_notification(manager,page,retry=True))['status'] == 'PENDING'
    assert len(await manager.repos.jobs.all()) == 1


@pytest.mark.asyncio
async def test_permanent_or_unexpected_errors_never_persist_tokens(manager, key_bytes, monkeypatch):
    page=await real_page(manager); job=await ensure_notification(manager,page)
    store_credentials(manager.settings,key_bytes)
    await manager.repos.settings.set_key(ENABLED_KEY,'true')
    async def fail(*args):
        raise ValueError('PRIVATE_SECRET / TOKEN_SECRET')
    monkeypatch.setattr(GoogleIndexingClient,'publish',fail)
    await process_notification(manager,job['id'])
    assert 'PRIVATE_SECRET' not in str(await manager.repos.pages.get(page['id']))
    assert 'TOKEN_SECRET' not in str(await manager.repos.jobs.get(job['id']))
    assert (await manager.repos.jobs.get(job['id']))['status']=='FAILED'


def test_enable_ownership_upload_pause_and_auto_notification_api(client, monkeypatch, key_bytes):
    admin_client(client)
    settings=client.app.state.settings
    headers={'X-CSRF-Token':get_csrf(client)}
    calls=[]
    async def owner(self): return settings.public_base_url+'/'
    async def accepted(self,url,kind):
        calls.append((url,kind)); return {'httpStatus':200,'googleRequestMade':True,'message':'Accepted, not indexed.'}
    monkeypatch.setattr(GoogleIndexingClient,'verify_owner',owner)
    monkeypatch.setattr(GoogleIndexingClient,'publish',accepted)
    monkeypatch.setattr(settings,'public_base_url','https://v1.indexmetrix.com')
    monkeypatch.setattr(client.app.state.queue.settings,'public_base_url','https://v1.indexmetrix.com')
    # HTTPS cookies would not flow to the HTTP TestClient; use authenticated bearer.
    # Existing cookie name is resolved from auth constants instead of assuming it.
    from app.auth.routes import SESSION_COOKIE
    headers['Authorization']='Bearer '+client.cookies.get(SESSION_COOKIE)
    try:
        assert client.post('/api/google-indexing/credentials',content=key_bytes,headers=headers).status_code==200
        assert client.post('/api/google-indexing/enable',json={'approved_api_usage':True},headers=headers).status_code==200
        row=client.post('/api/real-jobs',json=vacancy(title='Integration Test Engineer'),headers=headers).json()
        result=wait_for(client,'/api/real-jobs',lambda r:any(p['id']==row['id'] and p['notificationStatus']=='ACCEPTED' for p in r['items']))
        current=next(p for p in result['items'] if p['id']==row['id'])
        assert current['indexStatus']=='UNKNOWN' and current['notificationResult']['googleRequestMade']
        assert (row['url'],'URL_UPDATED') in calls
        assert client.post(f"/api/real-jobs/{row['id']}/close",headers=headers).status_code==200
        wait_for(client,'/api/real-jobs',lambda r:any(p['id']==row['id'] and p['notificationStatus']=='ACCEPTED' and p['notificationResult'].get('type')=='URL_DELETED' for p in r['items']))
        assert (row['url'],'URL_DELETED') in calls
        # Replacement always pauses, even for the same identity/key.
        replaced=client.post('/api/google-indexing/credentials',content=key_bytes,headers=headers)
        assert replaced.status_code==200 and not replaced.json()['enabled']
    finally:
        client.delete('/api/google-indexing/credentials',headers=headers)


def test_nonadmin_cannot_manage_keys_real_jobs_or_see_notification_jobs(client):
    from conftest import fresh_client, login
    admin_client(client)
    csrf={'X-CSRF-Token':get_csrf(client)}
    email='indexing-reader@example.com'
    response=client.post('/api/users', json={'name':'Indexing Reader','email':email,'password':'Reader12345!','role':'USER'},headers=csrf)
    assert response.status_code in (200,201), response.text
    other=fresh_client()
    assert login(other,email,'Reader12345!').status_code==200
    headers={'X-CSRF-Token':get_csrf(other)}
    for path in ('/api/google-indexing','/api/real-jobs'):
        assert other.get(path).status_code==403
    for path in ('/api/google-indexing/enable','/api/google-indexing/credentials','/api/real-jobs'):
        assert other.post(path,json=vacancy(),headers=headers).status_code==403
    assert other.get('/real-jobs').status_code==403
    assert other.get('/google-indexing').status_code==403
    assert not any(j['job_type']==JOB_INDEX_NOTIFY for j in other.get('/api/queue').json()['jobs'])
