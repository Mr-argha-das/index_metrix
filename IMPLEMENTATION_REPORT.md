# INDEX MATRIX — implementation and verification report

**Date:** 2026-09-21 UTC  
**Checkout:** `Mr-argha-das/index_metrix`  
**Branch:** `arena/01a0c3af-index-metrix`

## Outcome

Implemented incrementally on the existing **FastAPI/Python + Feather + Jinja/vanilla-JS** application. No replacement database, Node server, frontend redesign, ownership workaround, IndexNow key, or indexing guarantee was introduced. Existing submissions, analyzer, authentication, users, queue, monitoring, publishing and optional integrations remain.

**Local acceptance passes; production acceptance remains pending.** The available checkout is not the `aditi_Workd` repository named in the request. No production deployment was performed. Production requests from this sandbox failed during TLS negotiation, before HTTP; they cannot establish production GET or HEAD behavior.

## 1. Files changed and purpose

| File | Change |
|---|---|
| `.env.example` | INDEX MATRIX name and documented outbound per-host settings. |
| `README.md` | Updated architecture/semantics, API contract, Python deployment, limits, verification and single-process warning. |
| `app/config.py` | INDEX MATRIX default name; configurable one-second host delay; production refuses private targets. |
| `app/database/feather_store.py` | Add submission/channel/check/HTML/source-evidence fields, page content fingerprint and persisted retry deadline. Existing data migrates by adding columns. |
| `app/database/repositories.py` | Atomic bulk insert per table; fix descending-sort handling. |
| `app/database/migrations.py` | Backfill states/channels; demote legacy false indexing claims with audit events; retain existing page slugs. |
| `app/main.py` | Mount status API; bodyless HEAD responses; dashboard counters; admin-only API docs; startup repair; allow preview embedding only on preview hosts. |
| `app/monitoring/index_api.py` **new** | Authenticated validation/status API and admin-only audited evidence endpoint; explicit source/reference resource separation. |
| `app/monitoring/status.py` | Real GSC response shape; remove `Published` and `IndexingRequested` as indexing proof; record reference URL/target with crawl/index evidence. |
| `app/monitoring/observation.py` | Optional observations now use robots-aware safe fetching; reflected query text is not a search-result observation. |
| `app/integrations/google_search_console.py` | Fix token-method collision, sites-list endpoint, inspection request/response shape, sitemap PUT endpoint, exact property and URL-prefix authorization. |
| `app/pdf/validator.py` | Reject credential-bearing/malformed/control-character URLs; preserve scheme-specific nondefault ports; normalize hostname/default ports/fragments. |
| `app/pdf/fetcher.py` | Robots checks per redirect origin, conservative denial/failure handling, per-host spacing, fail-closed DNS resolver, preserved redirect chain, early size cap, bounded technical probes. |
| `app/pdf/analyzer.py` | Reject empty/password-protected PDFs; recognize short extractable text; isolated parser invocation with wall deadline. |
| `app/pdf/analysis_worker.py` **new** | Linux parser subprocess: 512 MiB address space, 30-second CPU cap, no core dump; bounded input and result metadata. |
| `app/pdf/html.py` **new** | Bounded HTML title, description, canonical, robots, excerpt and text-length diagnostics. HTML remains non-PDF. |
| `app/pdf/routes.py` | Up to 10,000 URLs per batch; normalized deduplication under intake lock; bulk persistence; DNS deferred to workers; bounded upload read; fix invalid HTTPException construction. |
| `app/queue/manager.py` | Recover PENDING/RUNNING/retrying and orphan intake; persist/schedule deadlines; bulk jobs; bounded workers; serialize jobs for the same resource; cancel timers on shutdown. |
| `app/queue/worker.py` | Explicit validation/discovery states and FETCH_CHECKED; record robots/HTML/check metadata; isolated parsing; persist retries; DONE/FAILED outcomes; retain independent evidence during reprocessing/probes. |
| `app/publishing/pages.py` | Filename + normalized-URL hash IDs independent of mutable titles; preserve existing slugs/publication dates; update modified content metadata without artificial timestamp churn. |
| `app/publishing/routes.py` | Public GET/HEAD for pages, sitemap/chunks, RSS, robots; validate sitemap chunk bounds; honor disabled feeds; private robots disallows. |
| `app/publishing/rss.py` | Fix pre-existing malformed XML concatenation; XML-safe text; real publication/build dates; no fabricated legacy dates. |
| `app/templates.py` | New state colors; preview-host embedding without removing normal frame protection. |
| `app/templates/dashboard.html` | Clarify that the index column describes reference pages. |
| `app/templates/pdf_detail.html` | Explicit PDF validity and full SHA-256 in analyzer view. |
| `app/templates/pdf_page.html` | Explicit `index,follow`, useful validation/content metadata, extraction-unavailable message, original link with `noopener`. |
| `app/templates/urls.html` | HTTP/type/domain/reference/discovery/crawl/source-and-reference-index/last-check columns; accurate intake description. |
| `static/js/app.js` | Labels for FETCH_CHECKED, search-engine crawl evidence, DONE, DISCOVERED and NOT_SUBMITTED. |
| `static/js/dashboard.js` | All requested independent counters and honest explanatory labels. |
| `static/js/urls.js` | Render new table fields, source/reference index separation and HTML diagnostics. |
| `requirements.txt` | Declare `cryptography`, needed by the existing service-account JWT integration. |
| `tests/conftest.py` | Disable outbound host delay only in isolated tests. |
| `tests/test_monitoring.py` | Correct legacy expectations: own fetch is FETCH_CHECKED, requested/published is not indexed. |
| `tests/test_queue.py` | Assert successful jobs use DONE. |
| `tests/test_index_contract.py` **new** | Public GET/HEAD, HTML/XML quality, evidence scope, privacy, stable IDs, robots, redirects, download limits, 3,000-URL intake/restart/concurrency, GSC wire contract and parser tests. |
| `IMPLEMENTATION_REPORT.md` **new** | This concrete verification and limitation record. |

No runtime databases, queue data, generated PDFs, credentials or dependency directories are included in the Git changes.

## 2. API contract

Existing `/api/pdfs` endpoints remain available.

| Method / route | Access | Behavior |
|---|---|---|
| `POST /api/index/validate` | Authenticated | `{ "urls": ["https://publisher.org/file.pdf"] }`; HTTP **202**, accepted job IDs, duplicates, malformed inputs. No remote fetch before response. |
| `GET /api/index/status` | Authenticated, ownership-filtered | Paginated records and counts; `page`, `per_page` (maximum 1,000). |
| `GET /api/index/status?url=<encoded-url>` | Same | One source/reference record; missing record 404. |
| `GET /api/index/status/<encoded-url>` | Same | Equivalent path form, decoded once by ASGI. |
| `POST /api/index/evidence` | **Admin only**, existing CSRF rules | Audited `operator-confirmed` evidence with strict boolean and required details. |

Example evidence payload:

```json
{
  "url": "https://publisher.org/file.pdf",
  "indexed": true,
  "source": "operator-confirmed",
  "details": "Describe the independently obtained evidence for this exact URL."
}
```

**Evidence target matters:** a source URL updates `sourceIndexStatus`; an INDEX MATRIX reference URL updates `indexStatus`. Search Console inspections are restricted to our configured origin and a genuinely authorized URL-prefix/domain property. Crawl evidence identifies its reference-page URL. Ordinary fetch checks identify the source URL.

`operator-confirmed` is an authenticated operator attestation, **not automatic verification of its truth**. Arbitrary evidence source names such as `sitemap`, `rss` or `googlebot-request` are rejected. The event records the operator, target, details and timestamp.

## 3. Public routes / HEAD fix

All are unauthenticated:

- `GET` / `HEAD /pdf/{slug}` — HTML; unknown slug 404.
- `GET` / `HEAD /sitemap.xml` — XML or sitemap index.
- `GET` / `HEAD /sitemap-{chunk}.xml` — XML; invalid/out-of-range chunk 404.
- `GET` / `HEAD /rss.xml` — RSS XML.
- `GET` / `HEAD /robots.txt` — plain text.

FastAPI does not implicitly add HEAD to GET API routes. These routes now explicitly register both methods. The ASGI response wrapper strips all HEAD response bodies while preserving status and representation headers, including errors.

## 4. Queue behavior

- Feather-backed persistent URL and job records; **one table write per batch**, not one per URL.
- Up to **10,000 URLs** per request; 3,000-URL intake and persistence exercised in tests.
- Syntax normalization/deduplication at intake; DNS, SSRF, robots, HTTP and parsing in workers.
- Defaults: **2 workers**, configurable up to 5; **1-second** spacing and **10 outbound requests/minute/host**, up to 2 requests in flight/host.
- Robots crawl delay can further reduce a host's rate. Rate waits do not consume transient-failure retries.
- Three retries by default: 5, 10, 20 seconds, exponentially increasing with a 300-second cap if configured for more retries.
- `next_attempt_at` persists across restarts; PENDING, interrupted RUNNING and RETRY_WAITING jobs resume.
- Jobs: `PENDING`, `RUNNING`, `RETRY_WAITING`, `DONE`, `FAILED`, `CANCELLED`. Legacy COMPLETED rows remain readable/countable.
- Duplicate normalized submissions do not launch duplicate jobs. Different URLs with identical content are flagged by SHA-256.
- Workers serialize tasks for the same stored resource. No thousands-of-simultaneous-fetches fan-out.

## 5. PDF and HTML processing

1. Normalize and validate HTTP(S); reject credentials, malformed URLs and private/internal targets in production.
2. Resolve/check every address and pin validated DNS; repeat checks for redirects (maximum five).
3. Fetch robots safely for each resource origin. Respect denial; do not bypass authentication, CAPTCHA, WAF or malformed/challenge policies. Missing robots (404/410) permits the fetch; unavailable policies defer/fail conservatively.
4. Stream with timeouts and **35 MiB default** download cap. Record HTTP/type/final URL, real downloaded size and redirect chain.
5. Require `%PDF-`, then parse structure. Calculate SHA-256 and extract title/metadata, page count and available text.
6. Native parsing runs outside the web server, limited to **512 MiB address space / 30 seconds CPU / 45 seconds wall time**. Password-protected PDFs are not decrypted.
7. Classify `TEXT_PDF` or `SCANNED_OR_EMPTY_PDF`; text extraction inspects up to 300 pages. No OCR or fabricated content.
8. A non-PDF HTML response stores diagnostic title/description/canonical/robots/text metadata (first 2 MiB), then fails PDF validation. It is never published as a PDF.

## 6. Reference pages, sitemap, RSS and robots

- New reference IDs: sanitized URL filename + **12-character normalized-URL hash**. Mutable metadata titles cannot change the ID. Existing published slugs are retained.
- Persist metadata across the existing PDF/page tables; the status API joins them into a reference record with source/final URLs, type/title/description/domain, size/pages/SHA/text, canonical and timestamps.
- Public HTML includes meaningful title/H1, original publisher link, source domain, content type, size, pages, hash, validation/publication dates, excerpt or explicit unavailable message, self-canonical, `index,follow`, OG and JSON-LD.
- No third-party files are rehosted, no publisher ownership is claimed, and no third-party website is modified.
- Sitemap dynamically lists **only INDEX MATRIX reference pages**, using real page `updated_at`. Existing 50,000-entry chunking remains.
- RSS dynamically lists recent reference pages with titles, links, GUIDs, descriptions and actual publication dates. Default 50 items, configurable to 1,000. It is a recent-change feed, not a complete archive; sitemap is the complete discovery inventory.
- Identical reprocessing preserves publication and page-update dates. Real content/title changes update page metadata without changing its original publication date.
- Robots allows `/pdf/`, `/sitemap.xml`, `/rss.xml`; disallows `/api/`, `/admin/`, `/data/`, `/login`.
- Disabling sitemap/RSS intentionally returns 404. Public defaults are enabled.

## 7. Honest status semantics

| Event | State / meaning |
|---|---|
| Intake | `submissionStatus=RECEIVED`, `discoveryStatus=NOT_SUBMITTED`, index unknown. |
| PDF validated | `VALIDATED`; actual signature/structure/metadata checked. |
| Validation exhausted/failed | `VALIDATION_FAILED`; no newly published PDF page. |
| Reference + discovery channels exposed | `DISCOVERY_SUBMITTED`; channels `reference-page`, `sitemap`, `rss`; discovery remains `DISCOVERY_PENDING`. This is exposure, not an external engine receipt. |
| Ordinary server fetch | `FETCH_CHECKED`, target source URL; **not** search-engine crawl evidence. |
| Authorized GSC crawl timestamp | `SEARCH_ENGINE_CRAWL_EVIDENCE`, target reference page. |
| Independent reference discovery evidence | `DISCOVERED`; never inferred from a sitemap/RSS request. |
| GSC or admin operator evidence | `INDEXED` or `NOT_INDEXED` for the exact evidence target. Otherwise `UNKNOWN`. |

Legacy internal `INDEX_UNKNOWN` / `CRAWL_UNKNOWN` remain compatible with existing views; the new status API normalizes them to `UNKNOWN`. No claim is made that a spoofable Googlebot User-Agent verifies Googlebot. No IndexNow implementation/key-generation mechanism was added.

## 8. Tests and syntax checks

Executed in the repository's Python virtual environment after installing `requirements.txt`:

| Check | Concrete result |
|---|---|
| Full suite: `.venv/bin/python -m pytest -o addopts='' -q` | **195 passed**, 2 dependency deprecation warnings; 36.85 seconds. |
| API/regression subset: auth, users, pipeline, publishing, queue, monitoring, system, index-contract | **143 passed**, 2 warnings; 34.72 seconds. |
| PDF/resource subset: validation, pipeline, index-contract filtered for PDF/HTML/signature/SHA/page/third-party/malformed/stable/robots/download | **26 passed**, 73 deselected, 2 warnings; 4.18 seconds. |
| `.venv/bin/python -m compileall -q app run.py scripts tests` | Pass. |
| `node --check static/js/app.js`, `dashboard.js`, `urls.js` | Pass. |
| `git diff --check` | Pass. |

Requested Node commands were attempted, not silently skipped:

- `npm install`, `npm test`, `npm run test:api`, `npm run test:pdf`: **ENOENT, no package.json** (exit 254).
- `node --check server.js`, `index-engine.js`, `index-status.js`, `reference-pages.js`: files do not exist in this Python repository (exit 1).
- No dummy Node wrappers were introduced to manufacture passing results.

The A–T coverage requested is provided by existing validation/pipeline/publishing/queue tests plus `test_index_contract.py`: URL and PDF validation; real SHA; pages; HTML extraction; publication; stable IDs; parsed sitemap/RSS; robots; public GET/HEAD; independent transitions; retry; duplicates; third-party origin and redirect handling.

Additional tests exercise robots denial **before content fetch**, source/reference evidence isolation, 3,000 jobs persisted and reopened, 3,000 normalized URLs accepted without DNS during intake, retry deadlines after reopen, measured concurrency, host spacing, download/probe size limits, malformed/credential URLs, real GSC wire shapes/property permissions, unchanged timestamps and private file denial.

The two warnings concern Starlette's test-client httpx/AnyIO compatibility interfaces; they are not test failures. The external sample test permits clean resource unavailability; its passing result is **not** a claim that the MSU sample is currently a valid/reachable PDF.

## 9. Running-server curl and pipeline results

Started Uvicorn on `0.0.0.0:8000` and a separate test PDF origin on port 8899. Only this development fixture enabled private targets; production startup refuses that setting.

Submitted the fixture URL plus a fragment duplicate:

```text
POST /api/index/validate -> 202 Accepted
accepted: 1 URL / job 1
duplicates: 1
referenceId: report-f4a8cf6e2b90
HTTP: 200
Content-Type: application/pdf
pages: 1
sizeBytes: 1663
SHA-256: 6f4cb24b68f166ba4b67c373bfd6e1b157b38405bdf59135b767d247d92c9df7
submissionStatus: DISCOVERY_SUBMITTED
discoveryStatus: DISCOVERY_PENDING
crawlStatus: FETCH_CHECKED
indexStatus: UNKNOWN
sourceIndexStatus: UNKNOWN
```

Actual `curl -i` and `curl -I` were run separately:

| Local route | GET | HEAD | Content type |
|---|---:|---:|---|
| `/robots.txt` | 200 | 200 | `text/plain; charset=utf-8` |
| `/sitemap.xml` | 200 | 200 | `application/xml` |
| `/rss.xml` | 200 | 200 | `application/rss+xml` |
| `/pdf/report-f4a8cf6e2b90` | 200 | 200 | `text/html; charset=utf-8` |

HEAD response bodies were independently checked to be **0 bytes**. HTML assertions verified title, H1, `index,follow`, self-canonical, original PDF anchor and useful metadata. Sitemap and RSS parsed as XML and contained the reference page. A **googlebot-like** GET returned 200 without changing indexing state.

Anonymous access checks:

```text
/api/index/status        401
/api/pdfs                401
/data/pdfs.feather       404
/.env                    404
/app/main.py             404
/pdf/does-not-exist      404
```

After stopping and restarting the actual server against the same Feather directory, all four routes still returned **GET 200 / HEAD 200**. The same slug and unknown index states were retained.

## 10. Production curl results — NOT PASSING / NOT DEPLOYED

The following eight requests were attempted against `https://v1.indexmetrix.com`:

| Route | GET | HEAD |
|---|---|---|
| `/robots.txt` | TLS failure, curl exit 35 | TLS failure, curl exit 35 |
| `/sitemap.xml` | TLS failure, curl exit 35 | TLS failure, curl exit 35 |
| `/rss.xml` | TLS failure, curl exit 35 | TLS failure, curl exit 35 |
| `/pdf/anonymous-431ecf` (ID supplied in request) | TLS failure, curl exit 35 | TLS failure, curl exit 35 |

Exact error:

```text
curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to v1.indexmetrix.com:443
```

No HTTP status or production HTML was received. This does **not** prove an origin outage, a 405, a 404, or a successful fix; the TLS path from this sandbox could not be established. The supplied production ID's existence is also unverified.

No production deployment mechanism/target was exercised. Deploy this Python branch to the intended service, confirm the correct repository, preserve durable data and run with one worker, configure HTTPS public origin, then rerun the requested production curls using a known live slug from that deployment. Ensure the reverse proxy/CDN forwards HEAD and does not serve an old robots.txt or cached 405.

## 11. Remaining limitations / acceptance boundaries

1. **Production curl acceptance is outstanding**; no production deployment or indexing/crawling proof is claimed.
2. **Repository mismatch:** work is in the supplied `index_metrix` checkout, not a separately cloned `aditi_Workd` repository.
3. **Feather is single-process persistence**, not a transactional/distributed queue. Use one Uvicorn process and one instance per data directory. Large growing tables will cost more per update; 3,000-URL intake and concurrency were tested, not multi-day throughput at millions of records.
4. **Parser limits require Linux/POSIX `resource`.** No OCR; up to 300 pages are inspected for extractable text. Resource-limit failures are explicit analysis failures, never synthetic content.
5. **Conservative robots:** unavailable/challenge/malformed policies can prevent analysis of otherwise reachable PDFs. No access controls are bypassed. Robots policy cache is up to one hour.
6. **HTML diagnostics only:** no public HTML-reference publishing was added. The app remains a PDF reference publisher.
7. **Malformed URLs** are rejected in the intake response rather than enqueued; valid-form URLs that fail DNS/HTTP/PDF validation have persistent failure states.
8. **Operator evidence is an attestation** and can be wrong; only admins may record it. Live GSC/Bing credentials were not configured or exercised. GSC protocol/permission handling was tested with mocks. Existing optional Bing functionality remains but was not live-validated.
9. **RSS is a recent-publication feed**, not every historical record. The sitemap remains the full reference inventory.
10. The local fixture is synthetic test content on a separate local origin. Third-party origin/redirect behavior is also mocked in automated tests; neither is proof that any particular remote PDF is currently reachable or indexed.

**Acceptance summary:** implementation, automated tests and local HTTP/security/discovery checks pass. The required production deployment/curl pass remains an explicit unchecked item.
