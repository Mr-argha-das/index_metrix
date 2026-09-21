"""Regression/acceptance tests for the INDEX MATRIX public and private contracts."""
import asyncio
import hashlib
import time
from datetime import timedelta
from urllib.parse import quote
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from conftest import admin_client, get_csrf, wait_for, make_pdf_bytes
from app.config import Settings
from app.database.feather_store import Database
from app.database.repositories import Repos
from app.pdf.analyzer import analyze_pdf, check_signature
from app.pdf.fetcher import FetchError, FetchResult, HostRateLimiter, SafeFetcher
from app.pdf.validator import normalize_url, URLValidationError
from app.publishing.pages import base_slug_for, make_slug
from app.queue.manager import QueueManager
from app.utils import utcnow, utcnow_iso


def guest_client(client):
    guest = TestClient(client.app)
    # A second cookie jar, but the same server event loop. Async database
    # locks must not be acquired from an unrelated TestClient portal thread.
    guest.portal = client.portal
    return guest


def publish(client, pdf_server_url, tag):
    admin_client(client)
    url = f"{pdf_server_url}/docs/report.pdf?contract={tag}"
    response = client.post("/api/index/validate", json={"urls": [url]},
                           headers={"X-CSRF-Token": get_csrf(client)})
    assert response.status_code == 202, response.text
    record = wait_for(client, "/api/index/status?url=" + quote(url, safe=""),
                      lambda r: r.get("submissionStatus") == "DISCOVERY_SUBMITTED")
    return url, record


@pytest.mark.parametrize("path,ctype", [("/sitemap.xml", "application/xml"),
                                         ("/rss.xml", "application/rss+xml"),
                                         ("/robots.txt", "text/plain")])
def test_public_get_and_head(client, path, ctype):
    guest = guest_client(client)
    get, head = guest.get(path), guest.head(path)
    assert get.status_code == head.status_code == 200
    assert head.content == b""
    assert get.headers["content-type"].startswith(ctype)
    assert head.headers["content-type"] == get.headers["content-type"]
    assert head.headers["content-length"] == get.headers["content-length"]


def test_live_reference_quality_and_discovery(client, pdf_server_url):
    from bs4 import BeautifulSoup
    url, record = publish(client, pdf_server_url, "quality")
    guest = guest_client(client)
    path = "/pdf/" + record["referenceId"]
    get, head = guest.get(path), guest.head(path)
    assert get.status_code == head.status_code == 200
    assert not head.content
    assert get.headers["content-type"].startswith("text/html")
    soup = BeautifulSoup(get.text, "html.parser")
    assert soup.title.text and soup.h1.text
    assert soup.find("meta", attrs={"name": "robots"})["content"] == "index,follow"
    assert soup.find("link", rel="canonical")["href"] == record["referencePage"]
    link = soup.find("a", href=url, target="_blank")
    assert "noopener" in link["rel"]
    assert record["sha256"] in get.text
    assert record["sourceDomain"] in get.text
    assert " ".join(record["excerpt"].split())[:50] in " ".join(soup.get_text().split())
    assert soup.find("meta", property="og:title")
    assert record["discoveryStatus"] == "DISCOVERY_PENDING"
    assert record["crawlStatus"] == "FETCH_CHECKED"
    assert record["indexStatus"] == record["sourceIndexStatus"] == "UNKNOWN"
    assert record["discoveryChannels"] == ["reference-page", "sitemap", "rss"]
    assert record["robotsCheck"][0]["allowed"]
    sitemap = ET.fromstring(guest.get("/sitemap.xml").text)
    locations = [el.text for el in sitemap.iter() if el.tag.endswith("loc")]
    assert record["referencePage"] in locations
    assert url not in locations
    rss = ET.fromstring(guest.get("/rss.xml").text)
    assert record["referencePage"] in [el.text for el in rss.findall("./channel/item/link")]
    # This is only a googlebot-like HTTP request, not verification of a crawler.
    spoof = guest.get(path, headers={"User-Agent": "Googlebot"})
    assert spoof.status_code == 200
    assert client.get("/api/index/status", params={"url": url}).json()["indexStatus"] == "UNKNOWN"
    for method in (guest.get, guest.head):
        assert method("/pdf/missing-contract-id").status_code == 404
    assert client.get("/api/index/status/" + quote(url, safe="")).json()["id"] == record["id"]


def test_authenticated_evidence_is_scoped(client, pdf_server_url):
    url, record = publish(client, pdf_server_url, "evidence")
    headers = {"X-CSRF-Token": get_csrf(client)}
    assert client.post("/api/index/evidence", headers=headers, json={
        "url": url, "indexed": True, "source": "sitemap", "details": "submission accepted"
    }).status_code == 400
    response = client.post("/api/index/evidence", headers=headers, json={
        "url": url, "indexed": True, "source": "operator-confirmed",
        "details": "Test operator independently confirmed the exact source URL."
    })
    assert response.status_code == 200
    assert response.json()["target"] == "source-url"
    state = client.get("/api/index/status", params={"url": url}).json()
    assert state["sourceIndexStatus"] == "INDEXED"
    assert state["indexStatus"] == "UNKNOWN"
    response = client.post("/api/index/evidence", headers=headers, json={
        "url": record["referencePage"], "indexed": False, "source": "operator-confirmed",
        "details": "Test independent inspection reports reference page not indexed."
    })
    assert response.status_code == 200
    state = client.get("/api/index/status", params={"url": url}).json()
    assert state["indexStatus"] == "NOT_INDEXED"
    assert state["sourceIndexStatus"] == "INDEXED"
    assert state["crawlStatus"] == "FETCH_CHECKED"


@pytest.mark.parametrize("path", ["/api/index/status", "/api/pdfs", "/api/queue", "/api/system/status", "/api/docs", "/api/openapi.json"])
def test_private_api_requires_auth(client, path):
    assert guest_client(client).get(path).status_code == 401


@pytest.mark.parametrize("path", ["/data/pdfs.feather", "/data/jobs.feather", "/.env", "/app/main.py", "/server.js"])
def test_private_files_not_served(client, path):
    assert guest_client(client).get(path).status_code == 404


def test_guest_cannot_write_evidence(client):
    assert guest_client(client).post("/api/index/evidence", json={
        "url": "https://example.com/a.pdf", "indexed": True,
        "source": "operator-confirmed", "details": "independent operator check"
    }).status_code == 401


def test_dedup_in_batch_and_sequential(client, pdf_server_url):
    admin_client(client)
    url = pdf_server_url + "/docs/report.pdf?contract=duplicates"
    headers = {"X-CSRF-Token": get_csrf(client)}
    response = client.post("/api/index/validate", headers=headers, json={"urls": [url, url + "#part", url]})
    data = response.json()
    assert len(data["accepted"]) == 1 and len(data["duplicates"]) == 2
    response = client.post("/api/index/validate", headers=headers, json={"urls": [url]})
    assert not response.json()["accepted"]
    assert response.json()["duplicates"][0]["existing_pdf_id"] == data["accepted"][0]["pdf_id"]


@pytest.mark.parametrize("url", ["http://x.com:bad/a", "https://[bad/", "https://u:p@x.com/a", "file:///a", "http://x.com/\x00a"])
def test_malformed_urls_are_validation_errors(url):
    with pytest.raises(URLValidationError):
        normalize_url(url)


def test_stable_id_uses_url_not_mutable_metadata():
    a = normalize_url("HTTPS://EXAMPLE.COM:443/docs/file.pdf#p1")
    b = normalize_url("https://example.com/docs/file.pdf")
    assert a == b
    assert make_slug(base_slug_for("Old title", a), a) == make_slug(base_slug_for("New title", b), b)
    assert normalize_url("http://example.com:443/a").startswith("http://example.com:443/")
    assert normalize_url("https://example.com:80/a").startswith("https://example.com:80/")


def test_pdf_magic_sha_pages_text_and_scanned():
    data = make_pdf_bytes(text="Actual extractable document text with meaningful metadata.")
    analysis = analyze_pdf(data)
    assert check_signature(data) and analysis.ok
    assert analysis.page_count == 1 and analysis.classification == "TEXT_PDF"
    assert len(hashlib.sha256(data).hexdigest()) == 64
    assert analysis.text_length > 0
    assert analyze_pdf(make_pdf_bytes(text="")).classification == "SCANNED_OR_EMPTY_PDF"
    assert not analyze_pdf(b"%PDF- definitely corrupt").ok
    assert not check_signature(b"<html>challenge</html>")


def test_html_extraction():
    from app.pdf.html import extract_html
    result = extract_html(b'<html><title>Useful title</title><meta name="description" content="Description"><meta name="robots" content="noindex"><link rel="canonical" href="/original"><body>Actual text<script>not content</script></body></html>', "https://source.org/doc")
    assert result["title"] == "Useful title" and result["description"] == "Description"
    assert result["canonical"] == "https://source.org/original" and result["robots"] == "noindex"
    assert "Actual text" in result["excerpt"] and "not content" not in result["excerpt"]


@pytest.mark.asyncio
async def test_robots_blocks_resource_before_request(monkeypatch):
    from app.pdf.validator import ValidatedURL
    import app.pdf.fetcher as module
    monkeypatch.setattr(module, "validate_url", lambda url, **kw: ValidatedURL(url, "https", "publisher.org", 443, ["8.8.8.8"]))
    fetcher = SafeFetcher(Settings(app_env="test", fetch_host_delay_seconds=0))
    calls = []
    async def request(session, url, method, max_bytes, probe_only):
        calls.append(url)
        return FetchResult(url, url, 200, {}, "text/plain", b"User-agent: *\nDisallow: /blocked/\n", 43)
    monkeypatch.setattr(fetcher, "_do_request", request)
    try:
        with pytest.raises(FetchError, match="robots.txt disallows"):
            await fetcher.fetch("https://publisher.org/blocked/file.pdf")
        assert calls == ["https://publisher.org/robots.txt"]
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_third_party_redirect_metadata_and_robots(monkeypatch):
    from app.pdf.validator import ValidatedURL
    import app.pdf.fetcher as module
    from urllib.parse import urlsplit
    monkeypatch.setattr(module, "validate_url", lambda url, **kw: ValidatedURL(url, "https", urlsplit(url).hostname, 443, ["8.8.8.8"]))
    fetcher = SafeFetcher(Settings(app_env="test", fetch_host_delay_seconds=0))
    data = make_pdf_bytes()
    async def request(session, url, method, max_bytes, probe_only):
        if url.endswith("robots.txt"):
            return FetchResult(url, url, 404, {}, "text/plain", b"", 0)
        if "publisher.org" in url:
            return FetchResult(url, url, 302, {"Location": "https://cdn.org/a.pdf"}, "", b"", 0)
        return FetchResult(url, url, 200, {}, "application/pdf", data, len(data))
    monkeypatch.setattr(fetcher, "_do_request", request)
    try:
        result = await fetcher.fetch("https://publisher.org/a.pdf")
        assert result.final_url == "https://cdn.org/a.pdf"
        assert result.redirect_chain == ["https://publisher.org/a.pdf"]
        assert len(result.robots_checks) == 2 and analyze_pdf(result.content).ok
    finally:
        await fetcher.close()


@pytest.mark.asyncio
async def test_host_delay_enforced():
    limiter = HostRateLimiter(1000, 2, delay=0.04)
    hits = []
    async def hit():
        await limiter.acquire("publisher.org")
        hits.append(time.monotonic())
        limiter.release("publisher.org")
    await asyncio.gather(hit(), hit(), hit())
    assert hits[1] - hits[0] >= 0.035 and hits[2] - hits[1] >= 0.035


@pytest.mark.asyncio
async def test_persistent_pending_retry_deadline_and_bounded_concurrency(tmp_path, monkeypatch):
    from app.queue import worker
    db = Database(str(tmp_path)); db.init()
    repos = Repos(db)
    settings = Settings(app_env="test", indexer_concurrency=2)
    manager = QueueManager(repos, settings)
    jobs = await manager.enqueue_pipelines(list(range(1, 7)))
    due = (utcnow() + timedelta(seconds=0.25)).isoformat()
    await repos.jobs.update(jobs[0]["id"], status="RETRY_WAITING", next_attempt_at=due)
    # Re-open actual Feather files rather than reusing the in-memory queue.
    restored = Database(str(tmp_path)); restored.init()
    next_manager = QueueManager(Repos(restored), settings)
    active = 0; peak = 0; seen = {}; finished = asyncio.Event()
    async def execute(manager, job_id):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        seen[job_id] = utcnow()
        await asyncio.sleep(0.03)
        active -= 1
        if len(seen) == 6:
            finished.set()
    monkeypatch.setattr(worker, "process_job", execute)
    await next_manager.start()
    try:
        await asyncio.wait_for(finished.wait(), 3)
    finally:
        await next_manager.stop()
    assert len(seen) == 6 and peak == 2
    from app.utils import parse_iso
    assert seen[jobs[0]["id"]] >= parse_iso(due)


@pytest.mark.asyncio
async def test_thousands_persist_as_pending_without_network(tmp_path):
    db = Database(str(tmp_path)); db.init()
    queue = QueueManager(Repos(db), Settings(app_env="test"))
    jobs = await queue.enqueue_pipelines(list(range(1, 3001)))
    assert len(jobs) == 3000 and queue.queue.qsize() == 3000
    reopened = Database(str(tmp_path)); reopened.init()
    assert await Repos(reopened).jobs.count() == 3000
    assert all(j["status"] == "PENDING" for j in jobs)


def test_gsc_real_wire_shape_and_non_evidence():
    from app.monitoring.status import classify_gsc_index_state
    real = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS", "coverageState": "Submitted and indexed"}}}
    assert classify_gsc_index_state(real)[0] == "INDEXED"
    for coverage in ("IndexingRequested", "Published", "Sitemap submitted", "RSS submitted"):
        assert classify_gsc_index_state({"inspectionResult": {"indexStatusResult": {"coverageState": coverage}}})[0] == "INDEX_UNKNOWN"


def test_gsc_property_scope():
    from app.integrations.google_search_console import GoogleSearchConsole
    contains = GoogleSearchConsole.property_contains
    assert contains("https://indexmetrix.com/", "https://indexmetrix.com/pdf/a")
    assert not contains("https://indexmetrix.com/", "https://indexmetrix.com.evil.org/pdf/a")
    assert not contains("https://indexmetrix.com/", "http://indexmetrix.com/pdf/a")
    assert not contains("https://indexmetrix.com/private/", "https://indexmetrix.com/pdf/a")
    assert contains("sc-domain:indexmetrix.com", "https://v1.indexmetrix.com/pdf/a")


def test_rss_dates_are_not_fabricated():
    from app.publishing.rss import render_rss
    settings = Settings(app_env="test")
    legacy = dict(slug="legacy", title="Missing timestamp")
    assert "<pubDate>" not in render_rss([legacy], settings)
    assert "<lastBuildDate>" not in render_rss([], settings)


def test_robots_protects_private_paths(client):
    text = guest_client(client).get("/robots.txt").text
    for path in ("/pdf/", "/sitemap.xml", "/rss.xml"):
        assert "Allow: " + path in text
    for path in ("/api/", "/admin/", "/data/"):
        assert "Disallow: " + path in text


@pytest.mark.asyncio
async def test_bulk_intake_3000_urls_does_not_resolve_dns(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.pdf.routes import _submit_urls
    import app.pdf.routes as module
    db = Database(str(tmp_path)); db.init()
    repos = Repos(db)
    settings = Settings(app_env="test")
    queue = QueueManager(repos, settings)
    def forbidden(*args, **kwargs):
        raise AssertionError("DNS must be deferred to queue workers")
    monkeypatch.setattr(module, "validate_url", forbidden)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repos=repos, settings=settings, queue=queue)))
    urls = [f"https://publisher.org/docs/{i}.pdf" for i in range(3000)]
    result = await _submit_urls(request, urls + [urls[0] + "#page=2"], {"id": 1, "role": "ADMIN"})
    assert len(result["accepted"]) == 3000 and len(result["duplicates"]) == 1
    assert await repos.pdfs.count() == await repos.jobs.count() == 3000
    assert not result["invalid"]


@pytest.mark.asyncio
async def test_legacy_false_index_evidence_repaired(tmp_path):
    from app.database.migrations import repair_status_semantics
    from app.utils import json_dumps
    db = Database(str(tmp_path)); db.init()
    repos = Repos(db)
    pdf = await repos.pdfs.insert(index_status="INDEXED", status="RECEIVED", discovery_status="DISCOVERY_PENDING",
        index_evidence=json_dumps({"source": "Google Search Console", "coverage_state": "IndexingRequested"}))
    await repair_status_semantics(repos)
    record = await repos.pdfs.get(pdf["id"])
    assert record["index_status"] == "INDEX_UNKNOWN"
    assert record["submission_status"] == "RECEIVED"
    assert record["discovery_status"] == "NOT_SUBMITTED"


@pytest.mark.asyncio
async def test_unchanged_revalidation_preserves_slug_and_dates(tmp_path):
    from app.publishing.pages import create_page_for_pdf
    from app.utils import sha256_hex
    db = Database(str(tmp_path)); db.init()
    repos = Repos(db); settings = Settings(app_env="test")
    data = make_pdf_bytes(text="Extracted text for a stable reference document.")
    analysis = analyze_pdf(data)
    pdf = await repos.pdfs.insert(normalized_url="https://publisher.org/file.pdf", title=analysis.title,
                                  sha256=sha256_hex(data), source_domain="publisher.org")
    first = await create_page_for_pdf(repos, settings, pdf, analysis)
    second = await create_page_for_pdf(repos, settings, pdf, analysis)
    assert first["slug"] == second["slug"]
    assert first["published_at"] == second["published_at"]
    assert first["updated_at"] == second["updated_at"]
    analysis.title = "Changed title with actual metadata update"
    third = await create_page_for_pdf(repos, settings, pdf, analysis)
    assert third["slug"] == first["slug"] and third["published_at"] == first["published_at"]
    assert third["title"] == analysis.title


@pytest.mark.asyncio
async def test_isolated_pdf_analysis():
    from app.pdf.analyzer import analyze_pdf_isolated
    result = await analyze_pdf_isolated(make_pdf_bytes())
    assert result.ok and result.page_count == 1


@pytest.mark.asyncio
async def test_gsc_wire_endpoints(monkeypatch):
    from app.integrations.google_search_console import GoogleSearchConsole, GSC_API, WEBMASTERS_V3
    gsc = GoogleSearchConsole('{"client_email":"test@example.org","private_key":"not-used","token_uri":"https://oauth2.googleapis.com/token"}', "https://indexmetrix.com")
    calls = []
    async def get(url):
        calls.append(url)
        return 200, {"siteEntry": [{"siteUrl": "https://indexmetrix.com/"}]}
    async def post(url, body):
        calls.append((url, body))
        return 200, {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}
    monkeypatch.setattr(gsc, "_get", get)
    monkeypatch.setattr(gsc, "_post", post)
    result = await gsc.inspect_own_page("https://indexmetrix.com/pdf/a")
    assert "inspectionResult" in result
    assert calls == [WEBMASTERS_V3 + "/sites", (GSC_API + "/v1/urlInspection/index:inspect",
                    {"inspectionUrl": "https://indexmetrix.com/pdf/a", "siteUrl": "https://indexmetrix.com/"})]
    calls.clear()
    assert "error" in await gsc.inspect_own_page("https://thirdparty.org/a.pdf")
    assert len(calls) == 1  # authorization lookup only, never an inspection


@pytest.mark.asyncio
async def test_download_cap_and_bounded_probe(pdf_server_url):
    fetcher = SafeFetcher(Settings(app_env="test", fetch_host_delay_seconds=0))
    try:
        with pytest.raises(FetchError) as failure:
            await fetcher.fetch(pdf_server_url + "/docs/huge.pdf")
        assert failure.value.code == "TOO_LARGE"
        probe = await fetcher.fetch(pdf_server_url + "/docs/huge.pdf", probe_only=True)
        assert probe.status == 200 and len(probe.content) <= 64 * 1024
    finally:
        await fetcher.close()


def test_rss_escapes_pdf_control_characters():
    from app.publishing.rss import render_rss
    xml = render_rss([dict(slug="escape", title='Title\x01 & < >', description='Description\x00',
                           published_at="2026-09-21T10:00:00+00:00")], Settings(app_env="test"))
    root = ET.fromstring(xml)
    assert root.find("./channel/item/title").text == "Title & < >"


def test_search_query_reflection_is_not_observation():
    from app.monitoring.observation import _contains_url
    target = "https://publisher.org/a.pdf"
    assert not _contains_url(f'<input value="{target}">', target)
    assert _contains_url(f'<a href="{target}">Actual link</a>', target)


@pytest.mark.asyncio
async def test_observation_honors_robots():
    from app.monitoring.observation import _query
    class BlockedFetcher:
        async def fetch(self, url, **kwargs):
            raise FetchError("robots.txt denied", code="ROBOTS_DENIED", retryable=False)
    state, _ = await _query(BlockedFetcher(), "test")
    assert state == "UNKNOWN"
