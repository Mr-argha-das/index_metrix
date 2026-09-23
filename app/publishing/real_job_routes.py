"""Real vacancies are entered/reviewed by admins, separately from URL intake."""
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import templates
from ..auth.routes import require_admin, require_admin_page
from ..integrations.google_indexing import IndexingError
from ..queue.indexing import ensure_notification
from ..utils import json_dumps, json_loads, utcnow_iso
from .pages import public_page_path, public_page_url
from .real_jobs import RealJobIn, is_open, job_data, schema_for

router = APIRouter()


def projection(page, settings):
    return {"id": page['id'], "number": page['job_number'], "path": public_page_path(page),
            "url": public_page_url(settings.public_base_url, page), "job": job_data(page),
            "status": 'OPEN' if is_open(page) else 'CLOSED', "publishedAt": page['published_at'],
            "notificationStatus": page.get('indexing_status') or 'NOT_REQUESTED',
            "notificationResult": json_loads(page.get('indexing_result'), {}) or {},
            "indexStatus": "UNKNOWN"}


async def require_real_page(request, page_id):
    page = await request.app.state.repos.pages.get(page_id)
    if not page or page.get('page_kind') != 'real-job':
        raise HTTPException(404, 'Real vacancy not found. Demo jobs cannot be converted or submitted through this endpoint.')
    return page


@router.get('/real-jobs', include_in_schema=False)
async def admin_page(request: Request, user=Depends(require_admin_page)):
    return templates.render(request, 'real_jobs.html', {})


@router.get('/api/real-jobs')
async def list_jobs(request: Request, user=Depends(require_admin)):
    rows = await request.app.state.repos.pages.find(lambda p: p.get('page_kind') == 'real-job')
    return {'items': [projection(p, request.app.state.settings) for p in reversed(rows)]}


@router.post('/api/real-jobs', status_code=201)
async def create_job(request: Request, payload: RealJobIn, user=Depends(require_admin)):
    manager = request.app.state.queue
    data = payload.model_dump(mode='json')
    serialized = json_dumps(data)
    async with manager.indexing_lock:
        duplicates = await manager.repos.pages.find(lambda p: p.get('page_kind') == 'real-job' and p.get('real_job') == serialized)
        if duplicates:
            return {'duplicate': True, **projection(duplicates[0], manager.settings)}
        number = await manager.repos.settings.reserve_counter('internal_next_demo_job')
        now = utcnow_iso()
        page = await manager.repos.pages.insert(page_kind='real-job', job_number=number, slug=f'real-job-{number}',
            title=payload.title, description=payload.description[:300], real_job=serialized, job_status='OPEN',
            reviewed_by=user['id'], reviewed_at=now, indexing_revision=1, indexing_status='QUEUED',
            page_url=manager.settings.public_base_url + f'/jobs/{number}', published_at=now, updated_at=now,
            sitemap_included=manager.settings.sitemap_enabled, rss_included=manager.settings.rss_enabled)
        await ensure_notification(manager, page)
        await manager.repos.events.add('REAL_JOB_PUBLISHED', 'Operator-attested real vacancy published; notification queued, not indexed.', user_id=user['id'], metadata={'pageId': page['id']})
    return projection(page, manager.settings)


@router.put('/api/real-jobs/{page_id}')
async def update_job(page_id: int, request: Request, payload: RealJobIn, user=Depends(require_admin)):
    manager = request.app.state.queue
    async with manager.indexing_lock:
        page = await require_real_page(request, page_id)
        if not is_open(page):
            raise HTTPException(409, 'Closed vacancies cannot be silently reopened. Publish a separately reviewed new opening.')
        data = payload.model_dump(mode='json')
        if data['date_posted'] != job_data(page)['date_posted']:
            raise HTTPException(400, 'The original posting date is immutable; do not manufacture freshness.')
        if data != job_data(page):
            page = await manager.repos.pages.update(page_id, real_job=json_dumps(data), title=payload.title, description=payload.description[:300],
                reviewed_by=user['id'], reviewed_at=utcnow_iso(), updated_at=utcnow_iso(), indexing_revision=page['indexing_revision'] + 1)
            await ensure_notification(manager, page)
    return projection(await manager.repos.pages.get(page_id), manager.settings)


@router.post('/api/real-jobs/{page_id}/close')
async def close_job(page_id: int, request: Request, user=Depends(require_admin)):
    manager = request.app.state.queue
    async with manager.indexing_lock:
        page = await require_real_page(request, page_id)
        if page.get('job_status') != 'CLOSED':
            page = await manager.repos.pages.update(page_id, job_status='CLOSED', indexing_revision=page['indexing_revision'] + 1,
                                                   updated_at=utcnow_iso(), sitemap_included=False, rss_included=False)
        await ensure_notification(manager, page)
    return projection(await manager.repos.pages.get(page_id), manager.settings)


@router.post('/api/real-jobs/{page_id}/retry')
async def retry_job(page_id: int, request: Request, user=Depends(require_admin)):
    manager = request.app.state.queue
    async with manager.indexing_lock:
        page = await require_real_page(request, page_id)
        try:
            await ensure_notification(manager, page, retry=True)
        except IndexingError as exc:
            raise HTTPException(400, str(exc)) from None
    return projection(await manager.repos.pages.get(page_id), manager.settings)


def render_real_job(request, page):
    active = is_open(page)
    url = public_page_url(request.app.state.settings.public_base_url, page)
    return templates.render(request, 'real_job_page.html', {
        'job': job_data(page), 'page': page, 'active': active, 'canonical_url': url,
        'job_schema': schema_for(page, url) if active else None,
    })
