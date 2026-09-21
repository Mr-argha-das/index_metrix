"""Bing Webmaster Tools integration (optional).

Only operates on properties returned by the API's own ``/sites`` list for the
configured API key. A property is reported as verified only when Bing says so
— we never pretend a third-party property is verified.

REST API (https://www.bing.com/webmasters/api):
* ``GET    /sites``                       → verified sites for this key
* ``POST   /sites/{site}/sitemap``        → submit a sitemap
* ``POST   /sites/{site}/urls``           → submit a batch of URLs
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("bot_indexer.bing")

BING_API = "https://www.bing.com/webmasters/api"


class BingConfigError(Exception):
    pass


class BingWebmaster:
    def __init__(self, api_key: str, base_url: str = ""):
        if not api_key:
            raise BingConfigError("No Bing Webmaster API key configured.")
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": self.api_key,
        }

    async def _request(self, method: str, path: str, json_body: dict | None = None) -> tuple[int, Any]:
        url = f"{BING_API}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, url, json=json_body, headers=self._headers()
            )
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text[:500]
        return resp.status_code, body

    async def list_sites(self) -> list[str]:
        status, body = await self._request("GET", "/sites")
        if status != 200:
            raise BingConfigError(f"GET /sites failed (HTTP {status}): {body}")
        if isinstance(body, list):
            return [s.get("siteUrl", "") for s in body if s.get("siteUrl")]
        return []

    def is_authorized(self, sites: list[str], property_url: str) -> bool:
        def norm(u: str) -> str:
            u = (u or "").strip().rstrip("/")
            return u.lower()

        target = norm(property_url)
        return any(norm(s) == target or norm(s) == target + "/" for s in sites)

    async def submit_sitemap(self, site: str, sitemap_url: str) -> dict:
        site_enc = quote(site, safe="")
        status, body = await self._request(
            "POST", f"/sites/{site_enc}/sitemap", {"sitemap": sitemap_url}
        )
        if status in (200, 201):
            return {"ok": True, "response": body}
        return {"error": f"HTTP {status}: {body}"}

    async def submit_urls(self, site: str, urls: list[str]) -> dict:
        if not urls:
            return {"error": "No URLs supplied."}
        site_enc = quote(site, safe="")
        status, body = await self._request(
            "POST", f"/sites/{site_enc}/urls", {"urls": urls[:10]}
        )
        if status in (200, 201):
            return {"ok": True, "response": body}
        return {"error": f"HTTP {status}: {body}"}

    async def test_connection(self) -> dict:
        try:
            sites = await self.list_sites()
        except BingConfigError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "sites": sites}
