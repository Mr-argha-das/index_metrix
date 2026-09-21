# Fictional demo job pages

## Selected behavior

The operator explicitly selected **random, clearly labelled fictional/demo jobs**, not actual vacancy extraction. New successfully validated PDF/HTML submissions now publish at `/jobs/<number>` (for example, `/jobs/123`). Failed validation still does not publish a page.

Every new page prominently says **“Fictional demo — not a real vacancy.”** Its title and description also identify it as fictional. It includes generated examples of:

- Role/title, fictional company and department
- Description, responsibilities and skills
- Experience, location, work mode and employment type
- Hypothetical salary, qualification and sample benefits
- An Apply section explicitly stating that applications are not accepted; its button is disabled

No CV form, fee, real employer affiliation, real vacancy or verified eligibility is asserted. The generated job details are **not** claimed to come from the submitted source document.

## Source link and ordinary discovery

The original normalized PDF/article URL remains an ordinary server-rendered `<a href>` link with `rel="noopener"`. It does not require JavaScript to follow. The source link is clearly separate from the fictional job, and the page says it is **not an official document for that fictional role**. Its link text is “View Original PDF” or “Read Original Article”—never an invented claim such as “Official Job PDF”.

Actual source metadata, title, excerpt and fingerprint remain visible in their own source section. Sitemap, RSS and `/references` link to the new canonical `/jobs/<number>` URLs. Robots allows `/jobs/`. GET and HEAD are public; missing/invalid job numbers return 404.

These links are discovery hints, not proof or a guarantee of crawling/indexing. No Google Indexing API call, `JobPosting` schema, fake job eligibility, automatic Google submission, or new Google credentials were introduced. Optional manual URL Inspection remains a read-only monitoring operation. Direct arbitrary-URL request-indexing remains UNSUPPORTED, and reference/external indexing evidence remains independent.

## Persistence, variation and compatibility

- Each profile is generated **once**, at publication, and saved in `pages.demo_job` JSON. It is not regenerated on GET, HEAD, refresh, retry or restart.
- Public job numbers come from an atomic, persistent counter in the existing private Feather settings store. Numbers are not reused after deletion; crash-related gaps are allowed.
- Titles/roles may repeat. A varied fixed-seed 100-profile regression sample checks substantial variation **without counting IDs**, but there is **no production guarantee that duplicates are below 1%**.
- Source revalidation can update genuine source metadata/fingerprints without changing the fictional profile, ID or original publication time. Unchanged revalidation does not refresh lastmod.
- Existing source-only pages at `/pdf/<slug>` remain unchanged. They are not silently converted into fictional jobs.
- The `/pdf/<slug>` alias for a **new demo page** returns a 308 redirect to its `/jobs/<number>` canonical URL.
- Existing normalized-URL deduplication remains: resubmitting an already published source does not create another page just to obtain another demo.
- Schema 4 adds nullable `page_kind`, `job_number` and `demo_job` fields to `pages`. The existing `jobs` database table still means **queue jobs**; it has not been replaced or repurposed.

The authenticated status API adds `referencePath`, `jobId`, `pageKind`, `isFictionalDemo` and `sourceTitle`. `referencePage`/`canonical` use the current public origin and correct route. The older `referenceId` slug is retained for compatibility.

## Implementation

- `app/publishing/demo_jobs.py`: coherent, explicitly fictional profile generator.
- `app/publishing/pages.py`: stable public path helper and persisted new-page generation.
- `app/database/feather_store.py`, `migrations.py`, `repositories.py`: additive schema and atomic monotonic counter.
- `app/publishing/routes.py`: public numeric job routes, legacy alias redirects, robots allowance.
- `app/publishing/sitemap.py`, `rss.py`: correct canonical URLs and honest feed wording.
- `app/templates/demo_job_details.html`, `pdf_page.html`: fictional details, disabled applications and separately attributed source metadata.
- Library, source-detail, sitemap/RSS admin templates and URLs frontend: route-aware links.
- Submission page: explains the fictional mode before submission.
- `app/monitoring/index_api.py`: explicit page kind, numeric ID, path and source title.
- `tests/test_demo_jobs.py`: route/HEAD/redirect, disclosure, link, no-JobPosting, persistence, legacy compatibility, numbering and variation tests.

## Verification and limits

Full Python suite: **230 passed**, two existing dependency deprecation warnings. Frontend auth regressions: **8 passed**. Python compilation, frontend JavaScript syntax and diff whitespace checks passed.

The tests run the real publishing pipeline against local PDF/HTML fixtures. They verify actual public GET/HEAD responses, source links, canonical/OG URLs, sitemap/RSS/library URLs, no fictional applications, no Google publication calls and independent UNKNOWN index states. Persistence tests reopen the Feather database and assert stable profiles and non-recycled counters.

The main preview was restarted without changing login credentials or enabling private-target fetching. No production deployment was performed. An attempted public W3C sample download failed during TLS connection in this sandbox; no successful public Internet download or Google indexing is claimed. Submit a reachable, valid source to generate a new demo job page.
