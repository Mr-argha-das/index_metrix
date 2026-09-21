"""Shared fixtures: isolated Feather data dir, local PDF server, TestClient."""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Environment MUST be set before importing the app (settings are cached).
# ---------------------------------------------------------------------------
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="bot-indexer-test-")
os.environ.update(
    {
        "APP_ENV": "test",
        "DATA_DIR": _TEST_DATA_DIR,
        "SESSION_SECRET": "test-session-secret-0123456789abcdef",
        "ADMIN_EMAIL": "admin@example.com",
        "ADMIN_PASSWORD": "Admin12345!",
        "PUBLIC_BASE_URL": "http://testserver",
        "INDEXER_CONCURRENCY": "2",
        "MAX_RETRIES": "3",
        "RETRY_BACKOFF_BASE": "0.2",
        "HTTP_TIMEOUT": "5",
        "CONNECT_TIMEOUT": "3",
        # the whole suite fetches from one local host — lift the per-host
        # outbound rate limit (it still applies in production defaults)
        "FETCH_RATE_PER_MINUTE": "1000",
        "FETCH_HOST_DELAY_SECONDS": "0",
        "FETCH_CONCURRENCY_PER_HOST": "5",
        "GOOGLE_SEARCH_CONSOLE_ENABLED": "false",
        "BING_WEBMASTER_ENABLED": "false",
        "OBSERVATION_ENABLED": "false",
        # the suite performs many requests from the same test IP; keep the
        # per-IP limiters out of the way (dedicated tests verify them)
        "LOGIN_RATE_LIMIT": "100000",
        "API_RATE_LIMIT_PER_MINUTE": "1000000",
        "API_MUTATION_RATE_LIMIT_PER_MINUTE": "1000000",
        # the repo .env sets SECURE_CLIENT=true for the HTTPS live preview;
        # tests run over plain http, so force lax, non-secure cookies
        "SECURE_CLIENT": "false",
    }
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from scripts.dev_pdf_server import make_app as make_pdf_app  # noqa: E402

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Admin12345!"
USER_EMAIL = "user@example.com"
USER_PASSWORD = "User12345!"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def pdf_server_url():
    """Run the dev PDF server in a background thread; yield its base URL."""
    import aiohttp.web

    port = _free_port()
    loop = asyncio.new_event_loop()

    async def _start():
        app = make_pdf_app(port)
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, "127.0.0.1", port)
        await site.start()

    loop.run_until_complete(_start())
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    # wait until it answers
    import urllib.request

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/docs/report.pdf", timeout=2)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"

    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


@pytest.fixture(scope="session")
def client(pdf_server_url):  # noqa: F841 - ensures pdf server is up first
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def db_dir() -> str:
    return _TEST_DATA_DIR


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def get_csrf(c) -> str:
    """Ensure a csrf cookie exists (set on first HTML response)."""
    if "csrf" not in c.cookies:
        c.get("/login")
    return c.cookies.get("csrf")


def login(c, email: str, password: str, new: bool = False):
    if new:
        c.cookies.clear()
    csrf = get_csrf(c)
    resp = c.post(
        "/api/auth/login",
        json={"email": email, "password": password},
        headers={"X-CSRF-Token": csrf},
    )
    return resp


def admin_client(client):
    login(client, ADMIN_EMAIL, ADMIN_PASSWORD, new=True)
    return client


def fresh_client():
    """A second TestClient against the already-running app (own cookie jar)
    — for logging in as a different user without clobbering the main
    session on the shared ``client``."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def user_client(client):
    login(client, USER_EMAIL, USER_PASSWORD, new=True)
    return client


def wait_for(client, path, pred, timeout: float = 30.0, interval: float = 0.25):
    """Poll an endpoint until pred(json) is true (queue workers progress
    on the portal event loop between requests)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = client.get(path)
        last = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
        try:
            if pred(last):
                return last
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {path}; last: {str(last)[:400]}")


def make_pdf_bytes(text: str = "hello world test document content", title: str = "Test Doc") -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=11, fontname="helv")
    doc.set_metadata({"title": title, "author": "Test Author", "subject": "test"})
    return doc.tobytes()
