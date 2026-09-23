# Real jobs + Google Indexing API

Implemented 2026-09-23 after the operator explicitly selected **real jobs + automatic Indexing API**. This is a scoped addition, not a generic indexing API for PDF/HTML submissions. Existing generated profiles and source-reference pages remain ineligible and are never automatically converted into real vacancies.

## Operator workflow

1. Set `PUBLIC_BASE_URL` to your actual public HTTPS origin (deployment: `https://v1.indexmetrix.com`). Run a single application process against the Feather data directory, as required by the existing storage/queue architecture.
2. In Google Cloud, enable **Indexing API** and **Search Console API** in the service-account project. Obtain Google's required usage approval/quota; the initial 200/day quota is for onboarding/testing, not a claim of production approval.
3. Verify site ownership in Search Console and add the service account's `client_email` as a **delegated owner**. A full/restricted user is not enough for this integration. Either an authorized covering domain property or the exact covering URL-prefix property works.
4. Sign in as an admin. Open **Google Indexing API** (`/google-indexing`), select the service-account JSON file, and upload it. Confirm approved API usage and click **Verify ownership & enable**. Upload alone does not enable delivery. Credentials are checked locally on upload; live ownership is verified on enable and again before every notification.
5. Open **Real jobs** (`/real-jobs`). Enter actual employer, role, full responsibilities, qualifications, real on-site location, original employer posting date, closing date and a genuine HTTPS application link. Confirm that you are authorized and the vacancy is real/open. The application does not independently prove the truth of this attestation; Google eligibility and content policies still apply.
6. Publish. The public page at `/jobs/<number>` includes matching `JobPosting` JSON-LD and a normal **Apply with employer** link. It is added to owned sitemap/RSS/internal links. A persistent notification is queued automatically.
7. Edit actual details to queue an update. Unchanged saves do not queue duplicate notifications and the original posting date cannot be reset. Close filled/withdrawn vacancies promptly. Closing/expiry disables Apply, removes `JobPosting`, adds `noindex`, excludes the page from discovery feeds and queues `URL_DELETED`. Expiry uses the end of the provided date in UTC. Expiry is checked in public rendering immediately and reconciled into the queue at startup/every 60 seconds.

Current real-job form supports on-site locations. It does not invent salaries, companies, remote-work eligibility or application links. Existing source URL intake still validates PDF/HTML and uses its separate generated-profile/reference flow; it does not create real jobs or call Google.

## Official API contract

Official documentation checked 2026-09-23:

- [Using the Indexing API](https://developers.google.com/search/apis/indexing-api/v3/using-api): eligible `JobPosting` or `BroadcastEvent` inside `VideoObject` only; this implementation supports real jobs only. `Content-Type: application/json` is mandatory.
- [Job posting requirements](https://developers.google.com/search/docs/appearance/structured-data/job-posting): actual open positions, truthful matching visible content/markup and a way to apply. A `/jobs/` path or randomly generated details do not confer eligibility.
- [Prerequisites](https://developers.google.com/search/apis/indexing-api/v3/prereqs): enabled API, verified site, delegated service-account ownership and OAuth.
- [Approval and quotas](https://developers.google.com/search/apis/indexing-api/v3/quota-pricing): initial 200 publish requests/day/project (updates + deletions), 180 metadata reads/minute/project, 380 total requests/minute/project; daily quota resets at midnight Pacific Time. Google may adjust quota; do not rotate accounts/projects to evade limits.

Requests:

- OAuth JWT RS256 → `POST https://oauth2.googleapis.com/token`.
- Scopes: `https://www.googleapis.com/auth/indexing` and `https://www.googleapis.com/auth/webmasters.readonly` (the latter only for the ownership check).
- Ownership check: `GET https://www.googleapis.com/webmasters/v3/sites`; require `permissionLevel: siteOwner` for a property covering the configured public origin.
- Notification: `POST https://indexing.googleapis.com/v3/urlNotifications:publish`, JSON `{"url": "<owned numeric job canonical>", "type": "URL_UPDATED"}` or `URL_DELETED`.

The user's supplied endpoint/request pattern is retained. Authentication uses the project's existing `cryptography` and asynchronous `httpx` stack rather than adding legacy `oauth2client`/blocking `httplib2`. No additional dependencies are required. No private Google endpoint, Search Console browser automation, impersonation or CAPTCHA bypass is involved.

## Status, queue and failure semantics

- `QUEUED`, `NOT_CONFIGURED`, `PAUSED`, `WAITING_QUOTA`, `RETRY_WAITING`, `FAILED`, `CANCELLED`, `ACCEPTED` describe **notification delivery only**.
- HTTP 200 → **ACCEPTED**, never **INDEXED**. Real-job API `indexStatus` stays `UNKNOWN`; this integration does not inspect the Google index. No external source status is changed by a notification.
- Stored safe result: canonical URL, notification type, last attempt/check time, HTTP status, optional Google notification time, sanitized explanation and whether that attempt reached the publish stage. A network timeout after starting publication has an uncertain delivery outcome, not proven rejection/indexing.
- Jobs use the existing `jobs.feather` queue. Real-job data and current delivery projection use added columns in `pages.feather` (schema 5); existing rows/content are not converted.
- Deduplication key: page ID + revision + notification type + canonical origin. Active/completed notifications are not resent by repeat clicks. Failed/cancelled notifications can be retried explicitly in **Real jobs**. Cancelling in Processing Queue is reflected there too.
- At startup, interrupted jobs are recovered and the page→queue crash window is reconciled. Delivery is **at least once**, not exactly once: a crash after remote acceptance but before local persistence may repeat the notification.
- Local conservative limits: 10 attempted sends/minute and 200/rolling 24 hours by default (`GOOGLE_INDEXING_MINUTE_LIMIT`, `GOOGLE_INDEXING_DAILY_LIMIT`). Counts persist before network calls and survive restarts and key replacement. This is intentionally more conservative than Google's Pacific-midnight reset; other clients sharing your Google project still consume its quota. Lower these limits if needed.
- 429/5xx/network errors retry with bounded exponential backoff; `Retry-After` is honored up to 24 hours. Maximum four attempts per notification before manual retry is required. 400/401/403, invalid setup or lost ownership are failures, not success. Quota/configuration waiting does not consume delivery attempts.
- Delivery-status changes do not change public `updated_at`, sitemap `lastmod` or publication dates. Only actual page edits/closure do.

## Credential security

- Only authenticated admins can upload/replace/remove a key, enable delivery or publish/edit real jobs. Mutations use the existing CSRF/session protection.
- Upload is raw JSON from the selected file, streamed with a 32 KiB limit. Only a Google service-account object with a valid RSA private key (2048+ bits), expected email/project shape and the fixed official token URI is accepted. User-supplied token URLs cannot redirect signed assertions to an attacker.
- Credentials reside in `<DATA_DIR>/secrets/google-indexing.json`: directory mode `0700`, file mode `0600`, atomic replacement. Storage under the public static directory or through a secret-path symlink is rejected. No credential download endpoint exists.
- Key data is **not encrypted by this application**. Use encrypted disks/backups and restrict server access. Protect the entire data directory, keep it outside any web-server static alias, and never commit/copy keys into public files or logs.
- Responses expose only configured/enabled state and the service-account email/project ID, never the private key, OAuth token, signed assertion or raw Google error body. Authenticated UI/API responses are non-cacheable; raw validation input is not echoed.
- Upload/replacement pauses delivery durably before writing the new key; enablement requires another ownership check. Removal pauses and deletes the local file, but does **not** revoke the key in Google Cloud or erase backups—revoke/rotate there separately.
- The existing optional Search Console integration's env credential setting remains independent. The new uploaded key is not silently shared with it.

## Verification

Verification: **330 Python tests + 14 frontend tests passed**; Python compilation, all static JavaScript syntax checks and `git diff --check` passed.

Automated tests use synthetic test keys and mocked official HTTP endpoints. They check upload permissions/size/secret redaction, admin/CSRF boundaries, exact OAuth scopes and JSON requests, property ownership, eligible public GET/HEAD/JSON-LD, publication/edit/closure, deduplication, restart recovery, expiry, permanent/transient failures, quota persistence, cancellation and no artificial freshness. Frontend tests cover status separation, escaping and file-input/error secrecy.

**No live Google request or production ownership/approval has been verified in the sandbox.** An operator must complete the above setup; successful mock tests are not evidence of Google crawling or indexing.
