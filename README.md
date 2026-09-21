# BOT INDEXER

**Production-ready FastAPI platform for third-party PDF URL validation, publishing, discovery and monitoring.**

You submit external PDF URLs. BOT INDEXER fetches them safely (SSRF-protected), validates and analyzes the PDF, publishes a useful SEO-complete dedicated page **on our own site** (with a clear link to the original file), exposes the pages through `sitemap.xml` + `rss.xml` so search engines can discover them, and honestly tracks discovery/crawl/index states — never claiming indexing without authoritative evidence.

---

## What it does (and what it deliberately does not)

| Stage | Behaviour |
|---|---|
| **Validation** | http/https only; scheme, host and IP checks; **SSRF protection on every redirect hop** (localhost, private, loopback, link-local, metadata `169.254.169.254`, internal name blocks); DNS-rebinding defence (pinned resolver); max 5 redirects; 10 s connect / 30 s read timeouts; 35 MB cap |
| **Fetch** | Honest user agent `BOT-INDEXER/1.0` — our fetch is a *technical fetch*, never labelled as a Googlebot crawl |
| **Analysis** | `%PDF-` magic-byte signature check (HTML masquerading as PDF is rejected), PyMuPDF structure/metadata extraction (title, author, pages, text), SHA-256 fingerprint, TEXT_PDF vs SCANNED_OR_EMPTY_PDF classification |
| **Publishing** | One dedicated page per valid PDF at `/pdf/<slug>` — metadata, first-page preview excerpt, canonical URL, Open Graph, **valid JSON-LD** (schema.org `WebPage`/`DigitalDocument`), and a plain-HTML “View Original PDF” link (`rel="noopener nofollow"`, no JS). The file is **not re-hosted** — we never copy the document, we link to it |
| **Discovery** | `sitemap.xml` (our pages only, real `lastmod`, never third-party URLs), `rss.xml` (real `pubDate`), `robots.txt` declaring the sitemap |
| **Queue** | In-process async queue, concurrency ≤ 5, 3 retries with exponential backoff (5 s × 2ⁿ, capped 300 s), crash recovery on restart |
| **Monitoring** | **Technical Server Probe** (explicitly labelled “NOT evidence of any Google crawl”), Search Console URL Inspection for *authorized properties only*; index status changes **only** with recorded authoritative evidence |
| **Integrations** | Google Search Console (JWT RS256 service account, **no Indexing API** as a generic submission tool) and Bing Webmaster — both optional; when absent they report `NOT CONFIGURED`, they never fabricate results |

### Honest status model

- `discovery_status`: `DISCOVERY_PENDING` until an authorized property confirms discovery — we cannot force or verify discovery for third-party domains.
- `crawl_status`: stays `CRAWL_UNKNOWN` unless an authorized Search Console property reports a crawl. **Our own probe never sets crawl status.**
- `index_status`: `INDEXED` / `NOT_INDEXED` **only** from authorized Search Console evidence (with `index_evidence` JSON: source, coverage state, checked-at). Third-party domains: `INDEX_UNKNOWN` with an explicit reason. The “Indexed” KPI counts only records with valid evidence.
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

### Development without production settings

The bundled `.env` sets `APP_ENV=development` and `ALLOW_PRIVATE_TARGETS=true` so you can point the app at a local PDF server:

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

139 tests, all passing:

| File | Covers |
|---|---|
| `test_validation.py` | URL normalization, SSRF matrix (17 blocked hosts incl. IPv6/CGNAT/metadata, internal name suffixes), fetcher redirect validation, redirect loop, honest user agent |
| `test_feather.py` | atomic writes, no temp leftovers, persistence across restart, backups/restore, corruption → clear error + file preserved, schema migration with data preserved |
| `test_auth.py` | login/logout/me, bootstrap idempotency, login brute-force limiting, CSRF enforcement, role-based 403s, own-vs-others data isolation |
| `test_users.py` | creation validation (email/password/role/duplicates), disable/enable, password reset invalidates sessions, self-protection, last-admin guard |
| `test_pipeline.py` | end-to-end publish + metadata, duplicate detection, same-content flagging, 404/403/fake-HTML/timeout paths, retry endpoint, delete removes page + sitemap entry, user ownership |
| `test_publishing.py` | sitemap (own pages only, disabled → 404, deletion removes), RSS (`pubDate`, `guid`), robots, public page without auth, stable unique slugs |
| `test_monitoring.py` | probe honesty labels, probe-never-sets-crawl invariant, fake GSC client → INDEXED/NOT_INDEXED with evidence, ambiguous → UNKNOWN, not-configured → UNKNOWN |
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
