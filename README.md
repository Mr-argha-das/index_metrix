> **Google capability decision (2026-09-21):** there is no generic public API to request indexing of arbitrary owned URLs. INDEX MATRIX uses normal discovery: public reference pages, the new `/references` HTML library, sitemap, RSS and robots. Direct request-indexing is explicitly `UNSUPPORTED`; no Google request is made by publication. See [GOOGLE_DISCOVERY.md](GOOGLE_DISCOVERY.md) for current official sources, API/scopes/quotas distinctions, implementation and verification. Earlier implementation reports are historical.

# INDEX MATRIX

> **Current publication mode: fictional job demos.** New validated submissions create `/jobs/<number>` with saved, clearly labelled fictional details and a separate original-source link. Applications are disabled; no real vacancy, “official job PDF” or Google JobPosting eligibility is asserted. Existing `/pdf/<slug>` references remain unchanged. See [DEMO_JOBS.md](DEMO_JOBS.md) for behavior, compatibility and verification.

**FastAPI platform for third-party PDF and HTML resource validation, publishing, discovery and monitoring.**

You submit external PDF or HTML/blog URLs. INDEX MATRIX fetches them safely (SSRF-protected), validates and analyzes the resource, publishes a useful SEO-complete dedicated page **on our own site** (with a clear link to the original file), exposes the pages through the public `/references` library, `sitemap.xml` + `rss.xml` so search engines can discover them, and honestly tracks discovery/crawl/index states — never claiming indexing without authoritative evidence.

---

## What it does (and what it deliberately does not)

| Stage | Behaviour |
|---|---|
| **Validation** | http/https only; scheme, host and IP checks; **SSRF protection on every redirect hop** (localhost, private, loopback, link-local, metadata `169.254.169.254`, internal name blocks); DNS-rebinding defence (pinned resolver); max 5 redirects; 10 s connect / 30 s read timeouts; 35 MB cap |
| **Fetch** | Honest user agent `BOT-INDEXER/1.0` — our fetch is a *technical fetch*, never labelled as a Googlebot crawl |
| **Analysis** | `%PDF-` magic-byte signature check (HTML masquerading as PDF is rejected), PyMuPDF structure/metadata extraction (title, author, pages, text), SHA-256 fingerprint, TEXT_PDF vs SCANNED_OR_EMPTY_PDF classification; useful HTML title/metadata/text extraction without executing scripts |
| **Publishing** | One owned page per valid PDF or useful HTML article (new fictional demos at `/jobs/<number>`; legacy references at `/pdf/<slug>`) — metadata, first-page preview excerpt, canonical URL, Open Graph, **valid JSON-LD** (schema.org `WebPage`/`DigitalDocument`), and a plain-HTML original-resource link (`rel="noopener"`, no JS). The file is **not re-hosted** — we never copy the document, we link to it |
| **Discovery** | Public paginated `/references` HTML links; `sitemap.xml` (our pages only, real `lastmod`, never third-party URLs), `rss.xml` (real `pubDate`), `robots.txt` declaring the sitemap |
| **Queue** | In-process async queue, concurrency ≤ 5, 3 retries with exponential backoff (5 s × 2ⁿ, capped 300 s), crash recovery on restart |
| **Monitoring** | **Technical Server Probe** (explicitly labelled “NOT evidence of any Google crawl”), Search Console URL Inspection for *authorized properties only*; index status changes **only** with recorded authoritative evidence |
| **Integrations** | Google Search Console (JWT RS256 service account, **no Indexing API** as a generic submission tool) and Bing Webmaster — both optional; when absent they report `NOT CONFIGURED`, they never fabricate results |

### Honest status model

The API now exposes independent `referenceSubmissionStatus`, `referenceCrawlStatus`, `referenceIndexStatus`, `externalDiscoveryStatus`, `externalCrawlStatus` and `externalIndexStatus`. The normal-discovery fallback marks direct arbitrary-URL request-indexing **UNSUPPORTED**, never QUEUED/ACCEPTED. Source fetching is not reference crawl evidence; reference indexing does not establish external indexing. Older fields below remain for compatibility.

- `discovery_status`: `DISCOVERY_PENDING` until an authorized property confirms discovery — we cannot force or verify discovery for third-party domains.
- `crawl_status`: ordinary HTTP checks produce `FETCH_CHECKED`, **not** search-engine evidence. Authorized Search Console crawl timestamps can produce `SEARCH_ENGINE_CRAWL_EVIDENCE` for the reference page.
- `index_status`: `INDEXED` / `NOT_INDEXED` **only** from authorized Search Console evidence or audited admin operator attestations (with `index_evidence` JSON: source, coverage state, checked-at). Third-party domains: `INDEX_UNKNOWN` with an explicit reason. The “Indexed” KPI counts only records with valid evidence.
- We never spoof Googlebot, never label our fetches as Google crawls, and never use the Google Indexing API to submit generic PDFs.

---

## Stack

- **Python 3.11 · FastAPI · Uvicorn** — API + server-rendered pages
- **PyMuPDF** — PDF signature validation, metadata, text extraction
- **pandas + pyarrow (Feather)** — **primary persistence**: `data/*.feather`, one file per table, atomic writes (temp + `fsync` + `os.replace`), in-process async lock, automatic backups (`data/backups/`, pruned to 20), schema migration on boot, clear `DatabaseCorruptedError` with recovery guidance (files are never silently deleted). **No SQLite/PostgreSQL.**
- **argon2** password hashing, token-hashed sessions, CSRF double-submit, per-IP rate limits
- **Vanilla JS + Jinja2 + Fetch** dashboard (dark/light theme, 5 s polling, no framework)
- **aiohttp** SSRF-protected fetcher with pinned DNS resolution and per-host rate limiting

---

## Quick start

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# configure (see .env.example for all options)
cp .env.example .env
#  - SESSION_SECRET: long random value (openssl rand -hex 32) in production
#  - ADMIN_PASSWORD: first-run admin password
#  - PUBLIC_BASE_URL: https URL for production

.venv/bin/python run.py          # serves on :8000
```

On first startup an admin account is created from `ADMIN_EMAIL` / `ADMIN_PASSWORD` (bootstrap runs exactly once). Log in at `http://localhost:8000/login`.

Embedded previews support browsers that block cookies and browser storage: the client keeps the session in memory and uses single-use, session-bound handoffs for protected page navigation. Authenticated handoff pages are private/no-store and remove the short-lived grant from the address bar. No reusable session token is placed in a URL. Logout revokes outstanding grants tied to that session. When all persistent storage is blocked, a full reload can require signing in again; opening the preview in a separate tab avoids many embedded-browser restrictions.

Frontend authentication regressions (no npm dependencies): `node --test tests/frontend_auth.test.js` — **8 tests** covering blocked storage, navigation, logout, handoff failure and redirect safety.


### Development without production settings

For local fixtures only, explicitly set `APP_ENV=development` and `ALLOW_PRIVATE_TARGETS=true`. Neither a `.env` nor production credentials are bundled:

```bash
.venv/bin/python scripts/dev_pdf_server.py --port 8899
# submit http://127.0.0.1:8899/docs/report.pdf  (valid)
#   /docs/redirect.pdf   (302 → report.pdf)
#   /docs/fake.pdf       (HTML masquerading as PDF → rejected)
#   /docs/loop.pdf       (redirect loop → REDIRECT_LOOP)
#   /docs/missing.pdf    (404) · /docs/forbidden.pdf (403) · /docs/huge.pdf (40 MB)
```

> `ALLOW_PRIVATE_TARGETS` **defeats SSRF protection** and must never be enabled in production (the app refuses to boot in production mode with it on).

### Optional integrations

```bash
# Google Search Console (authorized properties only; used for index evidence + sitemap ping)
export GOOGLE_SEARCH_CONSOLE_ENABLED=true
export GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service-account.json   # or inline JSON

# Bing Webmaster
export BING_WEBMASTER_ENABLED=true
export BING_API_KEY=...
```

When unconfigured, every integration surface reports **NOT CONFIGURED** — no fake results.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -v
```

The current full suite passes **234 tests** (2026-09-21). See [GOOGLE_DISCOVERY.md](GOOGLE_DISCOVERY.md) for current verification and [IMPLEMENTATION_REPORT.md](IMPLEMENTATION_REPORT.md) for the historical implementation report.

| File | Covers |
|---|---|
| `test_validation.py` | URL normalization, SSRF matrix (17 blocked hosts incl. IPv6/CGNAT/metadata, internal name suffixes), fetcher redirect validation, redirect loop, honest user agent |
| `test_feather.py` | atomic writes, no temp leftovers, persistence across restart, backups/restore, corruption → clear error + file preserved, schema migration with data preserved |
| `test_auth.py` | login/logout/me, bootstrap idempotency, login brute-force limiting, CSRF enforcement, role-based 403s, own-vs-others data isolation |
| `test_users.py` | creation validation (email/password/role/duplicates), disable/enable, password reset invalidates sessions, self-protection, last-admin guard |
| `test_pipeline.py` | end-to-end publish + metadata, duplicate detection, same-content flagging, 404/403/fake-HTML/timeout paths, retry endpoint, delete removes page + sitemap entry, user ownership |
| `test_publishing.py` | sitemap (own pages only, disabled → 404, deletion removes), RSS (`pubDate`, `guid`), robots, public page without auth, stable unique slugs |
| `test_monitoring.py` | probe honesty labels, probe-never-sets-search-engine-crawl invariant, fake GSC client → INDEXED/NOT_INDEXED with evidence, ambiguous → UNKNOWN, not-configured → UNKNOWN |
| `test_queue.py` | queue stats/list, per-user job visibility, cancel semantics, backoff schedule, retryable-500-then-success with a flaky server |
| `test_system.py` | health, system status, log structure, settings whitelist + validation, integrations NOT CONFIGURED, JSON-LD quality, uploads |
| `test_sample_url.py` | opt-in: external sample PDF from the spec (skips gracefully when offline) |

---

## Project layout

```
app/
  main.py              app factory, middleware (CSRF, rate limits, security headers),
                       exception handlers, dashboard/health/logs/settings API
  config.py            pydantic Settings (env-driven; production guardrails)
  templates.py         Jinja2 setup + shared globals
  utils.py             slugify, hashing, JSON, secret masking
  database/            feather_store.py (atomic writes, backups, schema, corruption),
                       repositories.py, migrations.py
  auth/                argon2 security, sessions, CSRF, login rate limit, page guards
  users/               admin user management (create/update/reset/delete)
  pdf/                 validator.py (SSRF), fetcher.py (pinned resolver, rate limit),
                       analyzer.py (signature + PyMuPDF), routes.py (submit/bulk/file)
  publishing/          pages.py (dedicated pages + JSON-LD), sitemap.py, rss.py, routes.py
  queue/               manager.py (workers, backoff, crash recovery), worker.py (pipeline)
  monitoring/          status.py (evidence-based state machine), routes.py (probes/inspects)
  integrations/        google_search_console.py (JWT RS256), bing_webmaster.py, routes.py
  logging_setup.py     ring-buffer log handler with secret scrubbing
static/                vanilla JS (App.api with CSRF), CSS (dark/light), 19 Jinja2 templates
scripts/               dev_pdf_server.py, create_admin.py, restore.py
tests/                 pytest suite (see above)
data/                  runtime Feather store (git-ignored) + data/backups/
```

## Security notes

- **SSRF**: every redirect hop is re-validated (scheme, host, resolved IP, internal name suffixes); DNS is pinned per request to defeat rebinding; honest `BOT-INDEXER/1.0` user agent.
- **AuthN/AuthZ**: argon2id hashing, sha256-hashed session tokens, HttpOnly session cookie, CSRF double-submit on all state-changing requests, login brute-force limiting, ADMIN/USER role enforcement on every endpoint.
- **Cookies**: session + CSRF cookies are issued `Secure; SameSite=None` for HTTPS clients (auto-detected via scheme / `x-forwarded-proto` / preview host, or forced with `SECURE_CLIENT=true`) so the app works inside cross-site iframes such as HTTPS live previews; plain-HTTP local use keeps `SameSite=Lax`.
- **Cookie-less embedded contexts**: for browsers that block third-party cookies entirely (common in cross-site iframes), the login response also returns the session token; the UI keeps it in (partitioned) `localStorage`, sends it as `Authorization: Bearer` on API calls, and enters server-rendered pages through a one-time, 60-second handoff token (`?st=`, single use). Cross-site mutations remain blocked by CORS (no `Access-Control-Allow-*` headers) and by the fact that an attacker page cannot read the token. When a request carries no CSRF cookie at all (i.e. the context does not send cookies), the double-submit check is inapplicable and skipped; when the cookie is present it is enforced as before.
- **Data**: no passwords or secrets in logs (scrubbing verified by tests), secrets never leave the server, production boot refused with placeholder `SESSION_SECRET`.
- **Integrity**: corrupted Feather files raise `DatabaseCorruptedError` with recovery guidance — data is never silently dropped; backups are written before every destructive operation.


## INDEX MATRIX API and deployment contract

This is a **Python/FastAPI checkout**, not a Node application. There is no
`package.json` or `server.js`. Keep the existing runtime; do not deploy it with
`npm start`. The repository available for this session is `Mr-argha-das/index_metrix`.

### Authenticated submission and status

- `POST /api/index/validate` — `{ "urls": ["https://publisher.org/document.pdf"] }`;
  returns **202** with accepted job IDs, normalized duplicates and malformed inputs.
  Up to **10,000 URLs** per batch; DNS/HTTP happen only in persistent workers.
- `GET /api/index/status` — paginated records and independent-state summary;
  `page=1&per_page=100`, maximum page size 1,000.
- `GET /api/index/status?url=<encoded-url>` or `/api/index/status/<encoded-url>` — one record.
- `POST /api/index/evidence` — **admin only**, audited operator attestation:
  `{ "url": "...", "indexed": true, "source": "operator-confirmed", "details": "Describe independent evidence..." }`.
  A source PDF URL updates **sourceIndexStatus**; a reference-page URL updates
  **indexStatus**. These resources must never be conflated. This endpoint trusts
  the authenticated operator's attestation; it does not verify its truth automatically.
- Existing `/api/pdfs`, analyzer/detail, retry/delete, users, queue and integrations remain.
- API writes use the existing session/bearer authentication and CSRF protections.
  API schema/docs are now admin-only. Public health and login are intentional exceptions.

### Public discovery routes

`GET` and `HEAD` work without authentication for `/pdf/{slug}`, `/sitemap.xml`,
`/sitemap-{chunk}.xml`, `/rss.xml`, `/robots.txt`. Unknown IDs return 404.
HEAD preserves the GET status/content type/content length but emits no body.
Sitemap/RSS include **our reference pages**, never third-party URLs as sitemap entries.
RSS is valid XML and uses real publication dates; unchanged reprocessing does not
bump publication or sitemap timestamps. Existing slugs are preserved; new slugs use
URL filenames plus a 12-character normalized-URL hash, independent of mutable titles.

### Workers and safety

Use **one Uvicorn process** with this Feather store. Do not use `--workers 2`
or multiple replicas sharing the data directory: locks are in-process, not distributed.
The async queue defaults to two workers (maximum five), one-second spacing per host,
ten outbound requests/minute/host, three retries with persisted exponential-backoff
deadlines, and restart recovery of pending/interrupted work. Jobs use `PENDING`,
`RUNNING`, `RETRY_WAITING`, `DONE`, `FAILED`, `CANCELLED`; legacy `COMPLETED` rows
remain readable. Bulk intake writes each table once, not once per URL.

Remote download default: **35 MiB**. Robots policies are checked on every resource
redirect origin via the SSRF-safe fetcher. Denials are respected; unavailable/challenge
responses defer/fail rather than bypass controls. Robots responses are capped at
512 KiB and cached for up to one hour (512 origins). Native PDF parsing is isolated
in a child process with **512 MiB address space, 30s CPU and 45s wall-time limits**
on Linux. PDFs requiring passwords are not decrypted. No OCR is performed;
text extraction inspects at most 300 pages. Useful HTML/blog responses now receive bounded extraction and accurately labeled reference pages. Thin, noindex, challenge and HTML-masquerading-as-PDF responses are not published.

### Deploy and verify

Back up `DATA_DIR` before upgrading (columns are added automatically). Set a real
`SESSION_SECRET`, `APP_ENV=production`, `ALLOW_PRIVATE_TARGETS=false`, and the actual
HTTPS `PUBLIC_BASE_URL`. Preserve `DATA_DIR` on durable storage. Then:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -o addopts='' -q
.venv/bin/python -m compileall -q app run.py scripts tests
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Forward public GET **and HEAD** to FastAPI through the deployment proxy. Do not serve
the repository root or `data/` as a static directory. The public robots file is generated
by FastAPI; remove any stale proxy/CDN override or cached 405. Verify GET separately
from HEAD after deployment, including one real `/pdf/{slug}` returned by the status API.
A request with `User-Agent: Googlebot` is only a **googlebot-like HTTP test**, never
Googlebot verification or indexing evidence. See the implementation report for the
production TLS error encountered from this sandbox; production acceptance is pending.


### Diagnosing INVALID / failed sources

The URLs table displays the persisted **Failure reason** and a **View diagnostics** link. The authenticated detail page includes Last error, HTTP status, final URL and the event timeline. An HTTP rejection, connection problem, unavailable robots policy and invalid PDF bytes are different failures; an INVALID badge alone does not establish the cause or prove the URL is permanently broken. Network failures normally retry and eventually become FAILED, while a missing PDF signature yields PDF_INVALID.

A document opening through a browser or an external reader does not prove that the application's server can retrieve it. Obtain the exact stored error before changing validation or network configuration. Never disable TLS verification, SSRF checks or robots enforcement to make a status appear successful. The former MSU external sample has been replaced by an explicitly non-working URL-format placeholder rather than advertising an unverified third-party file as a working test.

Diagnostics regressions: `node --test tests/frontend_auth.test.js tests/frontend_diagnostics.test.js` (9 tests), plus `tests/test_failure_diagnostics.py` (HTTP 403/404, HTML-as-PDF, readable detail pages and escaped error text).
