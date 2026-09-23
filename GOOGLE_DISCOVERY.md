> **2026-09-23 scoped addition:** [GOOGLE_INDEXING.md](GOOGLE_INDEXING.md) documents the separate operator-authored real-vacancy flow and its eligible Indexing API notifications. The source-reference/generated-demo restrictions below still apply to those resources. Existing demos are not converted or sent to Google. This document's earlier implementation/test counts are historical.

# Google indexing capability decision and implemented fallback

> Subsequent publication-format change: new submissions now use clearly labelled fictional demo pages at `/jobs/<number>`. Existing source references are retained. See [DEMO_JOBS.md](DEMO_JOBS.md). The no-generic-indexing-API decision below is unchanged; fictional jobs are not eligible job postings.

**Official documentation checked: 2026-09-21.**\
**Expected owned property:** `https://v1.indexmetrix.com/`\
**Configured production origin:** `PUBLIC_BASE_URL=https://v1.indexmetrix.com`

## Decision: no generic arbitrary-URL request-indexing API

Google does **not document an officially supported public API equivalent to the Search Console UI's “Request indexing” action for arbitrary pages**, even on a verified property you own.

- [Ask Google to recrawl your URLs](https://developers.google.com/search/docs/crawling-indexing/ask-google-to-recrawl) describes the **manual URL Inspection tool** for a few URLs and **sitemaps** for many URLs. The manual action requires an owner or full user. Repeated requests do not accelerate crawling.
- The [Search Console API reference](https://developers.google.com/webmaster-tools/v1/api_reference_index) documents Search Analytics, Sitemaps, Sites and URL Inspection. The URL Inspection API has an `index.inspect` operation; there is no generic request-indexing operation in that API.
- [URL Inspection API](https://developers.google.com/webmaster-tools/v1/urlInspection.index/inspect) reports the version known to Google's index. It does not submit a URL for indexing or perform a live indexability test.
- [Indexing API supported content](https://developers.google.com/search/apis/indexing-api/v3/using-api) is explicitly restricted to pages with **JobPosting** or **BroadcastEvent embedded in VideoObject**. An ordinary PDF/blog reference page is not eligible merely because it is owned by INDEX MATRIX.

For arbitrary reference pages, therefore: **API name, endpoint, OAuth scope, authentication flow, quotas and permissions for a generic indexing request are all not applicable—no such supported operation was identified.** Verification of `v1.indexmetrix.com` does not change the content restrictions.

No private endpoint, browser automation, Search Console UI automation, CAPTCHA bypass, fake verification, or fabricated eligible structured data has been implemented.

## Official mechanisms that do exist

These are different operations; they must not be conflated.

### 1. Search Console API — Sitemaps: submit (discovery only)

| Item | Official capability |
|---|---|
| Content | A sitemap/feed describing the property's URLs, including ordinary web pages; not a direct request-indexing operation for each URL. |
| Endpoint | `PUT https://www.googleapis.com/webmasters/v3/sites/{encodedSiteUrl}/sitemaps/{encodedSitemapUrl}` |
| Request body | None. |
| OAuth scope | `https://www.googleapis.com/auth/webmasters` |
| Authentication | OAuth 2.0 bearer access token. The existing optional repository client uses a server-side service-account JWT/token exchange; the account must actually have access to the property. |
| Property permissions | Owner or full user for sitemap submission; ownership alone in application configuration is not authorization. |
| Published limits | “All other resources”: 20 queries/second and 200 queries/minute per user; 100,000,000 queries/day per project. Actual project quota/permission responses still apply. |
| Success meaning | The sitemap submission request succeeded. It does **not** prove Google fetched the sitemap, crawled a listed page, or indexed it. |

Sources: [sitemaps.submit](https://developers.google.com/webmaster-tools/v1/sitemaps/submit), [authorization/scopes](https://developers.google.com/webmaster-tools/v1/how-tos/authorizing), [usage limits](https://developers.google.com/webmaster-tools/limits), [permissions table](https://support.google.com/webmasters/answer/7687615?hl=en), [service-account OAuth](https://developers.google.com/identity/protocols/oauth2/service-account).

The existing optional manual sitemap integration remains. **This change does not introduce automatic Google submission jobs.** The selected fallback advertises the sitemap through robots.txt and normal HTML discovery; it needs no OAuth credentials.

### 2. Search Console API — URL Inspection (monitoring only)

| Item | Official capability |
|---|---|
| Content | A URL under a property the authenticated account can inspect. |
| Endpoint | `POST https://searchconsole.googleapis.com/v1/urlInspection/index:inspect` |
| Request | `{"inspectionUrl":"https://v1.indexmetrix.com/pdf/<id>","siteUrl":"https://v1.indexmetrix.com/"}` |
| Scopes | `https://www.googleapis.com/auth/webmasters` **or** `https://www.googleapis.com/auth/webmasters.readonly` |
| Authentication/access | OAuth 2.0 bearer token and authorized property access; use the owner/full-user account for the existing site's inspection workflow. No authority over unrelated third-party domains is inferred. |
| Limits | Per property: 2,000 requests/day and 600 requests/minute. Per project: 10,000,000/day and 15,000/minute. |
| Meaning | Read the status of the version in Google's index; not a submission, request to crawl, or request to index. |

Sources: [inspect](https://developers.google.com/webmaster-tools/v1/urlInspection.index/inspect), [limits](https://developers.google.com/webmaster-tools/limits), [permissions](https://support.google.com/webmasters/answer/7687615?hl=en).

The existing optional, operator-triggered queued inspection remains available. No automatic inspection or new Google quota consumption was added to the publication fallback.

### 3. Indexing API — restricted; NOT used by INDEX MATRIX

| Item | Official capability |
|---|---|
| Eligible content | `JobPosting`, or `BroadcastEvent` embedded in `VideoObject`. Not ordinary PDFs, blog posts or reference pages. |
| Endpoint | `POST https://indexing.googleapis.com/v3/urlNotifications:publish` |
| Scope | `https://www.googleapis.com/auth/indexing` |
| Authentication | OAuth service-account access token. |
| Permissions | Verify the actual site; grant the service account delegated owner status for that site. |
| Initial testing quotas | 200 publish requests/day/project; 180 metadata requests/minute/project; 380 total requests/minute/project. Daily publish quota resets at midnight Pacific Time. Approval/resource provisioning is required for usage beyond initial onboarding/testing; consult actual approved quotas. |
| Meaning | An accepted notification is not proof of indexing. |

Sources: [supported content/endpoint](https://developers.google.com/search/apis/indexing-api/v3/using-api), [authentication/ownership/scope](https://developers.google.com/search/apis/indexing-api/v3/prereqs), [quota and approval](https://developers.google.com/search/apis/indexing-api/v3/quota-pricing) (documentation updated 2026-07-16).

These credentials, scopes and API calls were **not added**. Do not relabel reference pages as job postings or livestreams to evade the restrictions.

## Fallback implemented in this repository

This implements Google's documented normal-discovery route:

1. Safely validate/analyze the source resource in the existing bounded persistent queue.
2. Publish a useful, public reference page at `/pdf/<stable-id>` with its **own** canonical and real source metadata/excerpt/link.
3. Link it from **`/references`**, a public, server-rendered HTML library with ordinary `<a href>` links, stable ID ordering and 50 entries per page.
4. Provide ordinary previous/next links through the entire library. Reference pages link back to the library. Anonymous `/` visitors are redirected to the library; operator sign-in/dashboard remain available.
5. Continue generating `/sitemap.xml` from persisted owned reference pages, with real modification times. Third-party URLs are not inserted as our sitemap URLs.
6. Continue `/rss.xml` with real publication dates and links to reference pages. RSS is the supported feed option; no Atom/WebSub implementation was added.
7. Advertise `Sitemap: https://v1.indexmetrix.com/sitemap.xml` through robots.txt; keep `/pdf/`, `/references`, sitemap and RSS crawlable while APIs/data remain protected.
8. Record direct automated request-indexing as **UNSUPPORTED**, with `googleRequestMade: false`. Normal discovery remains pending until independently evidenced.

Official sources: [build/submit sitemaps, robots advertisement and RSS/Atom formats](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap), [crawlable internal/external links](https://developers.google.com/search/docs/crawling-indexing/links-crawlable).

Google explicitly describes sitemap submission as a **hint**, not a guarantee that it will fetch the sitemap or crawl its URLs. Neither RSS nor internal links guarantee indexing.

### Resource support and safety

The interrupted HTML work was completed rather than leaving a broken pipeline:

- PDF validation/analysis and all existing protections remain.
- Useful HTML/blog content can now publish through the same reference-page route, accurately labeled HTML—not PDF.
- HTML extracts title, description, canonical, robots, text length and a bounded excerpt; no scripts execute. Publication requires a title and at least 120 extractable text characters.
- Challenge/login-wall heuristics, HTML `noindex` directives, thin responses and HTML masquerading as an expected `.pdf` are rejected rather than worked around.
- SSRF, per-host pacing, bounded download, robots.txt enforcement and retry behavior continue through the existing fetcher/queue.
- New fields are additive to the existing Feather schema; no database replacement.

### Separate states

New explicit fields accompany backward-compatible legacy API fields:

```text
referenceSubmissionStatus: UNSUPPORTED (direct arbitrary-URL request-indexing)
referenceSubmissionMechanism: normal-discovery
referenceSubmissionResult.googleRequestMade: false
referenceCrawlStatus: UNKNOWN
referenceIndexStatus: UNKNOWN
externalDiscoveryStatus: DISCOVERY_PENDING
externalCrawlStatus: FETCH_CHECKED
externalIndexStatus: UNKNOWN
```

`FETCH_CHECKED` describes our own source request. It is not search-engine crawl evidence. A reference-page GSC result or operator attestation changes reference evidence only; it does not change the external URL's discovery, crawl or index states. External index evidence remains a separate, explicitly targeted operator attestation. Unsupported indexing operations are not enqueued, retried or represented as accepted.

## Configuration and authentication

Normal discovery requires only the existing publication/server configuration, including:

```dotenv
PUBLIC_BASE_URL=https://v1.indexmetrix.com
SITEMAP_ENABLED=true
RSS_ENABLED=true
GOOGLE_SEARCH_CONSOLE_ENABLED=false
GOOGLE_SERVICE_ACCOUNT_JSON=
```

**No Google client ID, client secret, refresh token or API key is required for this fallback. No new Google credential environment variables were added.**

If an operator separately uses the existing optional Search Console features, enable the Search Console API in the relevant Google Cloud project, securely provision the existing service-account credentials on the server, and grant that principal actual owner/full-user access to `https://v1.indexmetrix.com/`. Use `GOOGLE_SEARCH_CONSOLE_ENABLED=true` and `GOOGLE_SERVICE_ACCOUNT_JSON` pointing to the protected secret. Never commit the file or expose it publicly. This does not authorize inspections/submissions for arbitrary third-party domains.

## Files changed in this follow-up

| Files | Changes |
|---|---|
| `app/publishing/discovery.py` (new) | No-network fallback policy and persistent unsupported-operation reason. |
| `app/publishing/routes.py`, `app/templates/reference_library.html` (new) | Public GET/HEAD library, paginated HTML link discovery, robots allowance. |
| `app/publishing/pages.py`, `app/templates/pdf_page.html`, `app/publishing/rss.py` | Accurate HTML/PDF reference metadata and labels, internal links, RSS text. |
| `app/pdf/html.py`, `app/queue/worker.py`, `app/pdf/routes.py` | Complete useful HTML publication; retain PDF rejection safeguards; record separate states, redirects and declared size; no Google task creation. |
| `app/database/feather_store.py`, `app/database/migrations.py` | Add/backfill independent resource states and fallback fields without resetting index evidence or published slugs. |
| `app/monitoring/resource_states.py` (new), `app/monitoring/index_api.py`, `app/monitoring/status.py` | Separate reference/external status projection, evidence isolation and new submission counters. |
| `app/config.py` | Validate the public URL as an origin; keep the production default unchanged. No new Google settings retained. |
| `app/main.py`, `app/templates/login.html` | Public library entry path; retain operator login/dashboard; include HTML in validated counts. |
| `app/templates/urls.html`, `static/js/urls.js`, `static/js/dashboard.js`, `static/js/app.js` | Explicit submission and reference/external state columns/counters; clear unsupported-operation label. |
| `scripts/dev_pdf_server.py` | HTML/article/challenge/noindex/thin development fixtures. |
| `tests/test_discovery_fallback.py` (new), `tests/test_index_contract.py` | Publication/no-Google-request tests, HTML safeguards, link graph/pagination, state isolation; updated internal-link channel assertion. |
| `README.md`, `IMPLEMENTATION_REPORT.md`, `GOOGLE_DISCOVERY.md` | Current behavior, official capability decision, verification and boundaries. |

No new API endpoints are necessary. Existing `/api/index/validate`, status and evidence endpoints now expose the explicit state fields. The new public endpoint is `GET`/`HEAD /references`.

## Concrete verification

- Full Python suite: **211 passed**, two existing dependency deprecation warnings, **39.76 seconds**.
- Focused fallback suite: **16 passed**, two warnings, **4.46 seconds**.
- Python `compileall`, all frontend JS `node --check`, and `git diff --check`: passed.
- `npm test`: fails with ENOENT because this repository has no `package.json`. Requested checks for `server.js`, `index-engine.js`, `index-status.js`, `reference-pages.js` fail because those Node server files do not exist. No `google-submission.js` was introduced. Python modules are the actual server implementation.
- Tests use a Google stub that raises on any access: both PDF and HTML publication still succeed, establishing that the fallback does not invoke Google integration operations.
- Tests assert reference indexing evidence leaves external discovery/crawl/index states unchanged.

### Running-server HTTP checks

Started the current code on `0.0.0.0:8001`, with a separate local test origin on port 8898 and **`PUBLIC_BASE_URL=https://v1.indexmetrix.com`**. Private fetch targets were enabled only for these local fixtures, not production.

Two URLs were submitted and processed:

| Type | Reference ID | Submission | Reference index | External index |
|---|---|---|---|---|
| PDF | `report-f4a0d5875093` | UNSUPPORTED, normal discovery active | UNKNOWN | UNKNOWN |
| HTML | `article-8a1541e58a8c` | UNSUPPORTED, normal discovery active | UNKNOWN | UNKNOWN |

Both report `googleRequestMade: false`, `referenceCrawlStatus: UNKNOWN` and `externalCrawlStatus: FETCH_CHECKED`.

Local GET **and** HEAD returned 200 for:

- `/references`
- `/robots.txt`
- `/sitemap.xml`
- `/rss.xml`
- `/pdf/report-f4a0d5875093`
- `/pdf/article-8a1541e58a8c`

HEAD bodies were empty. XML parsed successfully and included both production-origin reference URLs. HTML assertions verified title, H1, canonical, `index,follow`, publisher links and internal navigation. Anonymous `/api/index/status` returned 401. Actual curl GET/HEAD checks were also recorded locally. After stopping and restarting the actual server process, all six GET/HEAD route pairs still returned 200; both reference IDs, canonical URLs, publication/lastmod timestamps and independent states persisted unchanged.

This proves local behavior with the production origin configured; it is **not a production deployment** or proof of Google discovery.

### Production verification remains blocked

GET and HEAD were attempted for these paths on `https://v1.indexmetrix.com`:

`/robots.txt`, `/sitemap.xml`, `/rss.xml`, `/references`, `/pdf/anonymous-431ecf` (the previously supplied production ID).

All ten attempts returned **curl exit 35** before any HTTP response:

```text
OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to v1.indexmetrix.com:443
```

No deployment was performed. No production 200, production HTML, deployed robots match, Google crawl, or indexing success is claimed. Rerun after deployment from a network that can reach the production TLS endpoint, using a known ID returned by that deployment's status API.

## Remaining boundaries

- This follow-up implements the requested **fallback**, not a generic Google indexing integration or automatic sitemap API scheduler.
- Discovery cannot guarantee indexing or predict when Google will visit.
- Existing optional GSC credentials/live permission/quotas were not exercised against a real Google account. No claim that the property is verified was made by the application on the basis of its configured URL.
- Single-process Feather storage, no OCR, bounded HTML/PDF extraction, and RSS's recent-item limit remain as documented in README.
- Publication quality checks are conservative heuristics, not full semantic or anti-abuse classification. Operators remain responsible for legitimate, useful submissions.
