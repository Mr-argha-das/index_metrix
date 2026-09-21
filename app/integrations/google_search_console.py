"""Google Search Console integration (optional).

HARD RULES implemented here:

* Only properties returned by the authorized account's
  ``/v1/properties`` list may be inspected. If the configured property is not
  in that list the result is ``NOT_AUTHORIZED`` — we never pretend a
  third-party property is verified.
* Results come straight from the URL Inspection / Sitemap APIs; the raw
  values are preserved as evidence. When the API is unavailable or silent,
  the status is UNKNOWN. Nothing is ever fabricated.

The Google Indexing API is deliberately **not** implemented: it only supports
blog posts and video content types, and is never appropriate for arbitrary
third-party PDFs.

Auth: service-account JWT (RS256) → OAuth2 token, plain HTTP over
``searchconsole.googleapis.com`` / ``www.googleapis.com`` (no vendor SDK).
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import httpx

log = logging.getLogger("bot_indexer.gsc")

SCOPE = "https://www.googleapis.com/auth/webmasters"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GSC_API = "https://searchconsole.googleapis.com"
WEBMASTERS_V3 = "https://www.googleapis.com/webmasters/v3"


class GSCConfigError(Exception):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_service_account(raw: str) -> dict:
    if not raw:
        raise GSCConfigError("No Google service account configured.")
    raw = raw.strip()
    try:
        if raw.startswith("{"):
            return json.loads(raw)
        import os

        path = raw
        if not os.path.isabs(path):
            candidates = [path, os.path.join(os.getcwd(), path)]
            for c in candidates:
                if os.path.exists(c):
                    path = c
                    break
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError) as exc:
        raise GSCConfigError(f"Could not load Google service account: {exc}") from exc


class GoogleSearchConsole:
    def __init__(self, service_account_raw: str, base_url: str = ""):
        self.sa = _load_service_account(service_account_raw)
        self.base_url = (base_url or "").rstrip("/")
        self._token: str | None = None
        self._token_exp: float = 0.0
        for key in ("client_email", "private_key", "token_uri"):
            if key not in self.sa:
                raise GSCConfigError(f"Service account JSON missing '{key}'.")

    # -- OAuth2 ----------------------------------------------------------------

    def _make_jwt(self) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": self.sa["client_email"],
            "scope": SCOPE,
            "aud": self.sa.get("token_uri", TOKEN_URL),
            "exp": now + 3600,
            "iat": now,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode())
        )
        key = self.sa["private_key"]
        if not key.startswith("-----BEGIN"):
            key = (
                "-----BEGIN PRIVATE KEY-----\n"
                + key
                + "\n-----END PRIVATE KEY-----"
            )
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        private_key = load_pem_private_key(key.encode(), password=None)
        signature = private_key.sign(
            signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        return signing_input + "." + _b64url(signature)

    async def _token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        jwt = self._make_jwt()
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                self.sa.get("token_uri", TOKEN_URL),
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": jwt,
                },
            )
        if resp.status_code != 200:
            raise GSCConfigError(f"OAuth token exchange failed (HTTP {resp.status_code}).")
        data = resp.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3600))
        return self._token

    async def _get(self, url: str) -> tuple[int, Any]:
        token = await self._token()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:500]
        return resp.status_code, body

    async def _post(self, url: str, body: dict | str, is_json: bool = True) -> tuple[int, Any]:
        token = await self._token()
        headers = {"Authorization": f"Bearer {token}"}
        if is_json:
            headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json=body if is_json else None,
                data=None if is_json else body,
                headers=headers,
            )
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text[:500]

    # -- properties ---------------------------------------------------------------

    async def list_properties(self) -> list[str]:
        """The properties the authorized account can actually inspect."""
        status, body = await self._get(f"{GSC_API}/v1/properties")
        if status != 200:
            raise GSCConfigError(f"properties list failed (HTTP {status}): {body}")
        return [p.get("siteUrl", "") for p in body.get("propertySite", []) if p.get("siteUrl")]

    def is_authorized(self, properties: list[str], property_url: str) -> bool:
        """Normalize both sides (https://, sc-domain:) and compare."""
        def norm(u: str) -> str:
            u = (u or "").strip().rstrip("/")
            if u.startswith("sc-domain:"):
                return "domain:" + u.split(":", 1)[1].lower()
            if u.startswith(("http://", "https://")):
                from urllib.parse import urlsplit

                host = urlsplit(u).hostname or ""
                return "url:" + host.lower()
            return u.lower()

        target = norm(property_url)
        return any(norm(p) == target for p in properties)

    # -- URL Inspection (our own pages only) ----------------------------------------

    async def inspect_url(self, site: str, url: str) -> dict:
        """Call URL Inspection for ``url`` under ``site``.

        The caller must verify ``site`` is an authorized property first.
        """
        from urllib.parse import quote

        site_enc = quote(site, safe="")
        status, body = await self._post(
            f"{GSC_API}/v1/urlInspection/index/{site_enc}/inspect",
            {"inspectionUrl": {"url": url}},
        )
        if status == 200:
            return body
        if status == 404:
            return {"error": f"Property or URL not found (HTTP 404)."}
        if status == 403:
            return {"error": "Not authorized for this property (HTTP 403)."}
        return {"error": f"Inspection failed (HTTP {status}): {body}"}

    async def inspect_own_page(self, page_url: str) -> dict:
        """Inspect one of OUR pages: verify the property is authorized, then
        inspect. Returns a dict with 'error' set on any failure."""
        try:
            properties = await self.list_properties()
        except GSCConfigError as exc:
            return {"error": str(exc)}
        from urllib.parse import urlsplit

        host = urlsplit(page_url).hostname or ""
        # Try URL-prefix property first, then domain property.
        for site in (f"https://{host}/", f"http://{host}/", f"sc-domain:{host}"):
            if not self.is_authorized(properties, site):
                continue
            result = await self.inspect_url(site, page_url)
            if "error" in result and "404" in str(result.get("error")):
                continue
            return result
        return {
            "error": (
                f"Property for host '{host}' is not in the authorized property list. "
                "NOT_AUTHORIZED — no inspection performed."
            )
        }

    # -- sitemaps -------------------------------------------------------------------

    async def sitemap_status(self, site: str) -> dict:
        from urllib.parse import quote

        site_enc = quote(site, safe="")
        status, body = await self._get(f"{GSC_API}/v1/properties/{site_enc}/sitemaps")
        if status != 200:
            return {"error": f"HTTP {status}: {body}"}
        return {"sitemaps": body.get("sitemap", [])}

    async def submit_sitemap(self, site: str, sitemap_url: str) -> dict:
        """Submit a sitemap via the (legacy) webmasters v3 sitemap insert
        endpoint; same OAuth scope. Only for authorized ``site``."""
        from urllib.parse import quote

        site_enc = quote(site, safe="")
        status, body = await self._post(
            f"{WEBMASTERS_V3}/sitemaps/{site_enc}?sitemapUrl={quote(sitemap_url, safe='')}",
            body="",
            is_json=False,
        )
        if status in (200, 201):
            return {"ok": True, "response": body}
        return {"error": f"HTTP {status}: {body}"}
