"""Admin-only secret upload and explicit enablement for real-job notifications."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Literal

from .. import templates
from ..auth.routes import require_admin, require_admin_page
from ..queue.indexing import ENABLED_KEY, enabled, wake_waiting
from ..utils import utcnow_iso
from .google_indexing import (MAX_KEY_BYTES, GoogleIndexingClient, IndexingError,
                              credential_path, load_credentials, store_credentials, validate_credentials)

router = APIRouter()


@router.get('/google-indexing', include_in_schema=False)
async def settings_page(request: Request, user=Depends(require_admin_page)):
    return templates.render(request, 'google_indexing.html', {})


@router.get('/api/google-indexing')
async def status(request: Request, user=Depends(require_admin)):
    manager = request.app.state.queue
    try:
        key = load_credentials(manager.settings)
        identity = {"configured": True, "clientEmail": key['client_email'], "projectId": key['project_id']}
    except IndexingError:
        identity = {"configured": False, "clientEmail": None, "projectId": None}
    return {**identity, "enabled": await enabled(manager), "origin": manager.settings.public_base_url,
            "dailyLimit": manager.settings.google_indexing_daily_limit, "minuteLimit": manager.settings.google_indexing_minute_limit,
            "note": "Only operator-attested real jobs. ACCEPTED is not INDEXED. Existing demos and external resources are excluded."}


@router.post('/api/google-indexing/credentials')
async def upload(request: Request, user=Depends(require_admin)):
    # Raw JSON upload is streamed and bounded BEFORE JSON parsing or persistence.
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_KEY_BYTES:
            raise HTTPException(413, 'Service-account JSON exceeds 32 KiB.')
        raw.extend(chunk)
    manager = request.app.state.queue
    async with manager.indexing_lock:
        try:
            validate_credentials(bytes(raw))
            # Pause durably BEFORE replacement (including across a server crash).
            await manager.repos.settings.set_key(ENABLED_KEY, 'false')
            store_credentials(manager.settings, bytes(raw))
        except IndexingError as exc:
            raise HTTPException(400, str(exc)) from None
        await manager.repos.events.add('INDEXING_CREDENTIALS_REPLACED', 'Protected Google credentials replaced; notifications paused until ownership is checked again.', user_id=user['id'])
    return await status(request, user)


class EnableIn(BaseModel):
    approved_api_usage: Literal[True]


@router.post('/api/google-indexing/enable')
async def enable(request: Request, payload: EnableIn, user=Depends(require_admin)):
    manager = request.app.state.queue
    async with manager.indexing_lock:
        await manager.repos.settings.set_key(ENABLED_KEY, 'false')
        try:
            if not manager.settings.public_base_url.startswith('https://'):
                raise IndexingError('Configure PUBLIC_BASE_URL with your public HTTPS origin before enabling notifications.')
            key = load_credentials(manager.settings)
            site = await GoogleIndexingClient(key, manager.settings.public_base_url).verify_owner()
        except IndexingError as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception:
            raise HTTPException(502, 'Google ownership verification unavailable. No notifications were enabled.') from None
        await manager.repos.settings.set_key(ENABLED_KEY, 'true')
        await manager.repos.events.add('INDEXING_ENABLED', 'Operator confirmed approved API use; delegated site ownership verified. Indexing is not guaranteed.', user_id=user['id'], metadata={'property': site, 'checkedAt': utcnow_iso()})
        await wake_waiting(manager)
    return await status(request, user)


@router.post('/api/google-indexing/pause')
async def pause(request: Request, user=Depends(require_admin)):
    manager = request.app.state.queue
    async with manager.indexing_lock:
        await manager.repos.settings.set_key(ENABLED_KEY, 'false')
    return await status(request, user)


@router.delete('/api/google-indexing/credentials')
async def remove(request: Request, user=Depends(require_admin)):
    manager = request.app.state.queue
    async with manager.indexing_lock:
        await manager.repos.settings.set_key(ENABLED_KEY, 'false')
        credential_path(manager.settings).unlink(missing_ok=True)
        await manager.repos.events.add('INDEXING_CREDENTIALS_REMOVED', 'Local key removed; revoke the key separately in Google Cloud if needed.', user_id=user['id'])
    return await status(request, user)
