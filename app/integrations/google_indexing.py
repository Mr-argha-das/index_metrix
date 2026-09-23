"""Official Indexing API for attested real jobs only. No generic URL submission.

Uses the existing cryptography/httpx stack, not deprecated oauth2client.
Never log/store tokens, JWTs, raw Google bodies or uploaded keys in API data.
"""
import base64
import json
import os
import re
import tempfile
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .google_search_console import GoogleSearchConsole

TOKEN_URL = "https://oauth2.googleapis.com/token"
ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications:publish"
SITES_URL = "https://www.googleapis.com/webmasters/v3/sites"
SCOPES = "https://www.googleapis.com/auth/indexing https://www.googleapis.com/auth/webmasters.readonly"
MAX_KEY_BYTES = 32768


class IndexingError(Exception):
    def __init__(self, message, *, retryable=False, status=None, retry_after=0, sent=False):
        super().__init__(message)
        self.retryable, self.status, self.retry_after, self.sent = retryable, status, retry_after, sent


def validate_credentials(raw: bytes) -> dict:
    try:
        if len(raw) > MAX_KEY_BYTES:
            raise ValueError()
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("type") != "service_account":
            raise ValueError()
        if data.get("token_uri") != TOKEN_URL or data.get("universe_domain", "googleapis.com") != "googleapis.com":
            raise ValueError()
        if not re.fullmatch(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.iam\.gserviceaccount\.com", data.get("client_email", "")):
            raise ValueError()
        if not re.fullmatch(r"[a-z0-9-]{3,100}", data.get("project_id", "")):
            raise ValueError()
        key = serialization.load_pem_private_key(data["private_key"].encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError()
        return {k: data[k] for k in ("type", "project_id", "client_email", "private_key", "token_uri")}
    except Exception:
        # Deliberately no input values, JSON parse context or PEM in errors.
        raise IndexingError("Invalid service-account JSON. A Google service account with an RSA key (2048+ bits) and official token URI is required.") from None


def credential_path(settings):
    path = Path(settings.data_dir).resolve() / "secrets" / "google-indexing.json"
    static = Path(__file__).resolve().parents[2] / "static"
    if path.is_relative_to(static) or path.parent.is_symlink() or path.is_symlink():
        raise IndexingError("Unsafe credentials storage configuration.")
    return path


def store_credentials(settings, raw):
    data = validate_credentials(raw)
    path = credential_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".key-")
    try:
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return data


def load_credentials(settings):
    path = credential_path(settings)
    if not path.exists():
        raise IndexingError("No Indexing API service account uploaded.")
    try:
        with path.open("rb") as file:
            return validate_credentials(file.read(MAX_KEY_BYTES + 1))
    except OSError:
        raise IndexingError("Cannot read protected Indexing API credentials.") from None


def retry_delay(value):
    try:
        return min(86400, max(0, int(value)))
    except (ValueError, TypeError):
        try:
            from email.utils import parsedate_to_datetime
            return min(86400, max(0, parsedate_to_datetime(value).timestamp() - time.time()))
        except Exception:
            return 0


class GoogleIndexingClient:
    def __init__(self, credentials, base_url):
        self.credentials, self.base_url = credentials, base_url.rstrip("/")
        self.token, self.expires = None, 0

    async def access_token(self):
        if self.token and time.time() < self.expires - 60:
            return self.token
        def b64(value):
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
        now = int(time.time())
        claims = {"iss": self.credentials["client_email"], "scope": SCOPES, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}
        unsigned = b64(b'{"alg":"RS256","typ":"JWT"}') + "." + b64(json.dumps(claims).encode())
        key = serialization.load_pem_private_key(self.credentials["private_key"].encode(), password=None)
        signed = unsigned + "." + b64(key.sign(unsigned.encode(), padding.PKCS1v15(), hashes.SHA256()))
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = await client.post(TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": signed})
        self.check_response(response, "OAuth authentication")
        try:
            data = response.json()
            token = data["access_token"]
            if not isinstance(token, str) or not token:
                raise ValueError()
            self.token, self.expires = token, time.time() + min(int(data.get("expires_in", 3600)), 3600)
            return token
        except (KeyError, ValueError, TypeError):
            raise IndexingError("Google returned an invalid OAuth response.") from None

    @staticmethod
    def check_response(response, action, sent=False):
        if response.status_code != 200:
            raise IndexingError(f"{action} failed (HTTP {response.status_code}). Check API enablement, ownership and quota in Google Cloud.",
                                retryable=response.status_code == 429 or response.status_code >= 500,
                                status=response.status_code, retry_after=retry_delay(response.headers.get("Retry-After")), sent=sent)

    async def verify_owner(self):
        token = await self.access_token()
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = await client.get(SITES_URL, headers={"Authorization": "Bearer " + token})
        self.check_response(response, "Search Console ownership check")
        try:
            entries = response.json().get("siteEntry", [])
            for entry in entries:
                site = entry.get("siteUrl", "")
                if entry.get("permissionLevel") == "siteOwner" and GoogleSearchConsole.property_contains(site, self.base_url + "/"):
                    return site
        except (ValueError, TypeError, AttributeError):
            raise IndexingError("Invalid Search Console ownership response.") from None
        raise IndexingError("Service account is not an owner of the configured public origin. Add it as a delegated owner in Search Console.")

    async def publish(self, url, notification_type):
        # Defense in depth: no caller may turn this into arbitrary URL submission.
        if notification_type not in ("URL_UPDATED", "URL_DELETED") or not re.fullmatch(re.escape(self.base_url) + r"/jobs/[1-9][0-9]*", url):
            raise IndexingError("Notification must target an owned numeric job page.")
        await self.verify_owner()
        token = await self.access_token()
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
                response = await client.post(ENDPOINT, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                                             json={"url": url, "type": notification_type})
        except httpx.HTTPError:
            raise IndexingError("Notification attempted but no confirmed Google response; retry may repeat a notification.", retryable=True, sent=True) from None
        self.check_response(response, "Indexing notification", sent=True)
        # Do not persist arbitrary reflected response content or call this INDEXED.
        try:
            body = response.json()
            latest = body.get("urlNotificationMetadata", {}).get("latestUpdate" if notification_type == "URL_UPDATED" else "latestRemove", {})
            stamp = latest.get("notifyTime")
            if stamp and not re.fullmatch(r"[0-9TZ:.+\-]{10,40}", str(stamp)):
                stamp = None
        except (ValueError, AttributeError, TypeError):
            stamp = None
        return {"httpStatus": 200, "notifyTime": stamp, "googleRequestMade": True,
                "message": "Google accepted the notification. Crawling and indexing are not confirmed."}
