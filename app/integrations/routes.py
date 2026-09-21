"""Integrations API + page (admin only for configuration).

Both providers are OPTIONAL. When unconfigured, every action returns/renders
NOT CONFIGURED instead of a fake success.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import templates
from ..auth.routes import require_admin, require_admin_page
from ..database.repositories import Repos
from ..utils import utcnow_iso

log = logging.getLogger("bot_indexer.integrations")

router = APIRouter(prefix="/api/integrations", tags=["integrations"])
page = APIRouter(tags=["integrations-pages"])


def _repos(request: Request) -> Repos:
    return request.app.state.repos


def _provider_row(request: Request, provider: str) -> dict:
    settings = request.app.state.settings
    rows = [r for r in _settings_cache(request, provider)]
    row = rows[0] if rows else None
    if provider == "google":
        configured = bool(settings.google_service_account_json)
        enabled = settings.google_search_console_enabled or (row or {}).get("status") == "CONNECTED"
    else:
        configured = bool(settings.bing_api_key)
        enabled = settings.bing_webmaster_enabled or (row or {}).get("status") == "CONNECTED"
    return {
        "provider": provider,
        "configured": configured,
        "enabled": enabled,
        "property_url": (row or {}).get("property_url") or (request.app.state.settings.public_base_url + "/"),
        "status": (row or {}).get("status") or ("NOT CONFIGURED" if not configured else "NOT CONNECTED"),
        "authorized_sites": (row or {}).get("authorized_sites") or "",
        "last_check_at": (row or {}).get("last_check_at"),
        "last_check_message": (row or {}).get("last_check_message"),
    }


def _settings_cache(request: Request, provider: str):
    cache = getattr(request.app.state, "_integ_cache", None)
    if cache is None:
        raise RuntimeError("integration cache not filled")
    return cache[provider]


async def _fill_integ_cache(request: Request) -> None:
    rows = await _repos(request).integrations.all()
    cache: dict[str, list[dict]] = {"google": [], "bing": []}
    for r in rows:
        if r.get("provider") in cache:
            cache[r["provider"]].append(r)
    request.app.state._integ_cache = cache


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


@router.get("")
async def api_integrations(request: Request, user: dict = Depends(require_admin)):
    await _fill_integ_cache(request)
    return {
        "google": _provider_row(request, "google"),
        "bing": _provider_row(request, "bing"),
    }


async def _save_provider(request: Request, provider: str, property_url: str, status: str, extra: dict | None = None) -> dict:
    repos = _repos(request)
    rows = [r for r in await repos.integrations.find(lambda r: r.get("provider") == provider)]
    fields = {
        "property_url": property_url.rstrip("/"),
        "status": status,
        **(extra or {}),
    }
    if rows:
        return await repos.integrations.update(rows[0]["id"], **fields)
    return await repos.integrations.insert(
        provider=provider,
        property_url=property_url.rstrip("/"),
        status=status,
        credentials_reference="env" if provider == "google" else "env:BING_API_KEY",
        authorized_sites="",
        last_check_at=None,
        last_check_message=None,
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )


class PropertyIn(BaseModel):
    property_url: str


# ---------------------------------------------------------------------------
# Google Search Console
# ---------------------------------------------------------------------------


@router.post("/google")
async def api_google_config(
    payload: PropertyIn, request: Request, user: dict = Depends(require_admin)
):
    await _save_provider(request, "google", payload.property_url, "NOT CONNECTED")
    await _repos(request).events.add(
        "INTEGRATION_CONFIGURED",
        f"Google Search Console property set to {payload.property_url}",
        status="INFO",
    )
    return {"ok": True}


@router.post("/google/test")
async def api_google_test(request: Request, user: dict = Depends(require_admin)):
    settings = request.app.state.settings
    if not settings.google_service_account_json:
        raise HTTPException(
            status_code=400,
            detail="NOT CONFIGURED: set GOOGLE_SERVICE_ACCOUNT_JSON to a service account "
            "with Search Console access, then restart or reload.",
        )
    from .google_search_console import GoogleSearchConsole, GSCConfigError

    try:
        gsc = GoogleSearchConsole(settings.google_service_account_json, settings.public_base_url)
        properties = await gsc.list_properties()
    except GSCConfigError as exc:
        await _save_provider(
            request, "google", settings.public_base_url + "/", "ERROR",
            {"last_check_at": utcnow_iso(), "last_check_message": str(exc)[:300]},
        )
        raise HTTPException(status_code=400, detail=str(exc))

    await _fill_integ_cache(request)
    configured_property = _provider_row(request, "google")["property_url"]
    authorized = gsc.is_authorized(properties, configured_property)
    status = "CONNECTED" if authorized else "NOT_AUTHORIZED"
    await _save_provider(
        request, "google", configured_property, status,
        {
            "last_check_at": utcnow_iso(),
            "last_check_message": (
                "Property is in the authorized list."
                if authorized
                else f"Property not in authorized list ({len(properties)} authorized)."
            ),
            "authorized_sites": ",".join(properties),
        },
    )
    return {
        "status": status,
        "authorized_properties": properties,
        "property_url": configured_property,
    }


@router.post("/google/submit-sitemap")
async def api_google_submit_sitemap(request: Request, user: dict = Depends(require_admin)):
    settings = request.app.state.settings
    if not settings.google_service_account_json:
        raise HTTPException(status_code=400, detail="NOT CONFIGURED (no service account).")
    await _fill_integ_cache(request)
    property_url = _provider_row(request, "google")["property_url"]
    from .google_search_console import GoogleSearchConsole, GSCConfigError

    try:
        gsc = GoogleSearchConsole(settings.google_service_account_json, settings.public_base_url)
        properties = await gsc.list_properties()
        if not gsc.is_authorized(properties, property_url):
            raise HTTPException(
                status_code=403,
                detail=f"NOT_AUTHORIZED: {property_url} is not in the authorized property list.",
            )
        sitemap_url = f"{settings.public_base_url}/sitemap.xml"
        result = await gsc.submit_sitemap(property_url, sitemap_url)
    except GSCConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    await _repos(request).events.add(
        "SITEMAP_SUBMITTED",
        f"Sitemap {sitemap_url} submitted to Google Search Console for {property_url}",
        status="SUCCESS",
        evidence_type="SEARCH_CONSOLE",
        metadata={"sitemap": sitemap_url, "property": property_url},
    )
    return {"ok": True, "submitted": sitemap_url}


# ---------------------------------------------------------------------------
# Bing Webmaster
# ---------------------------------------------------------------------------


@router.post("/bing")
async def api_bing_config(
    payload: PropertyIn, request: Request, user: dict = Depends(require_admin)
):
    await _save_provider(request, "bing", payload.property_url, "NOT CONNECTED")
    await _repos(request).events.add(
        "INTEGRATION_CONFIGURED",
        f"Bing Webmaster property set to {payload.property_url}",
        status="INFO",
    )
    return {"ok": True}


@router.post("/bing/test")
async def api_bing_test(request: Request, user: dict = Depends(require_admin)):
    settings = request.app.state.settings
    if not settings.bing_api_key:
        raise HTTPException(
            status_code=400, detail="NOT CONFIGURED: set BING_API_KEY, then restart or reload."
        )
    from .bing_webmaster import BingWebmaster, BingConfigError

    try:
        bing = BingWebmaster(settings.bing_api_key, settings.public_base_url)
        sites = await bing.list_sites()
    except (BingConfigError, Exception) as exc:  # noqa: BLE001
        await _save_provider(
            request, "bing", settings.public_base_url + "/", "ERROR",
            {"last_check_at": utcnow_iso(), "last_check_message": str(exc)[:300]},
        )
        raise HTTPException(status_code=400, detail=str(exc))

    await _fill_integ_cache(request)
    configured_property = _provider_row(request, "bing")["property_url"]
    authorized = bing.is_authorized(sites, configured_property)
    status = "CONNECTED" if authorized else "NOT_AUTHORIZED"
    await _save_provider(
        request, "bing", configured_property, status,
        {
            "last_check_at": utcnow_iso(),
            "last_check_message": (
                "Property is in the verified sites list."
                if authorized
                else f"Property not verified for this API key ({len(sites)} verified sites)."
            ),
            "authorized_sites": ",".join(sites),
        },
    )
    return {"status": status, "verified_sites": sites, "property_url": configured_property}


@router.post("/bing/submit-sitemap")
async def api_bing_submit_sitemap(request: Request, user: dict = Depends(require_admin)):
    settings = request.app.state.settings
    if not settings.bing_api_key:
        raise HTTPException(status_code=400, detail="NOT CONFIGURED (no API key).")
    await _fill_integ_cache(request)
    property_url = _provider_row(request, "bing")["property_url"]
    from .bing_webmaster import BingWebmaster, BingConfigError

    try:
        bing = BingWebmaster(settings.bing_api_key, settings.public_base_url)
        sites = await bing.list_sites()
        if not bing.is_authorized(sites, property_url):
            raise HTTPException(
                status_code=403,
                detail=f"NOT_AUTHORIZED: {property_url} is not a verified site for this API key.",
            )
        sitemap_url = f"{settings.public_base_url}/sitemap.xml"
        result = await bing.submit_sitemap(property_url, sitemap_url)
    except BingConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    await _repos(request).events.add(
        "SITEMAP_SUBMITTED",
        f"Sitemap {sitemap_url} submitted to Bing Webmaster Tools for {property_url}",
        status="SUCCESS",
        evidence_type="SEARCH_CONSOLE",
        metadata={"sitemap": sitemap_url, "property": property_url},
    )
    return {"ok": True, "submitted": sitemap_url}


@page.get("/integrations", include_in_schema=False)
async def integrations_page(request: Request, user: dict = Depends(require_admin_page)):
    await _fill_integ_cache(request)
    return templates.render(
        request,
        "integrations.html",
        {
            "google": _provider_row(request, "google"),
            "bing": _provider_row(request, "bing"),
        },
    )
