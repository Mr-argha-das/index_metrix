# INDEX MATRIX

INDEX MATRIX is a focused vacancy publishing and Google indexing workflow.

## Core flow

```
CSV / XLSX vacancy sheet
        ↓
Add URL
        ↓
Create public /jobs/<number> page
        ↓
Expose employer job_details / apply_url as normal links
        ↓
Google Indexing API notification
        ↓
Automatic retry for transient failures
        ↓
Search Console crawl / index evidence
        ↓
Dashboard
```

## Vacancy sheet

Supported columns:

- `title`
- `company`
- `company_url`
- `job_details`
- `apply_url`
- `description`
- `qualifications`
- `employment_type`
- `city`
- `region`
- `country`
- `date_posted`
- `valid_through`

`job_details` is an employer/source URL. It is shown as a normal followable link on the internal job page. The internal `/jobs/<number>` URL is the URL submitted to Google's indexing notification system.

## Dashboard

The dashboard shows:

- total and open vacancies
- Google notification acceptance
- queued / automatic retry state
- crawl evidence
- index evidence
- company and location
- job details and application URLs
- complete vacancy fields
- retry attempts and queue errors

Google accepting a notification is not treated as proof that a page is indexed.

## Storage

Application data is persisted through the Feather database layer.

## Run

```bash
python run.py
```

Or:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Required production pieces

Configure the public HTTPS origin, authentication, Feather data directory, and Google service-account/indexing settings in the environment before enabling production indexing.

Sitemap and RSS remain available as additional discovery channels for the public `/jobs/` pages.
