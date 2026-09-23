"""Operator-authored, attested real vacancies; never converted from random demos."""
from datetime import date, datetime, time, timezone
from html import escape
from urllib.parse import urlsplit

from ..utils import json_loads, utcnow


def job_data(page):
    data = json_loads(page.get("real_job"), {}) or {}
    # Backward compatibility for vacancies created before the Job Details field.
    if not data.get("job_details") and data.get("apply_url"):
        data["job_details"] = data["apply_url"]
    return data


def is_open(page):
    if page.get("page_kind") != "real-job" or page.get("job_status") != "OPEN":
        return False
    try:
        return date.fromisoformat(job_data(page)["valid_through"]) >= utcnow().date()
    except (ValueError, KeyError, TypeError):
        return False


def discoverable(page):
    return page.get("page_kind") != "real-job" or is_open(page)


def http_url(value):
    """Return only ordinary HTTP(S) links for public anchor attributes."""
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
            return str(value).strip()
    except ValueError:
        pass
    return ""


def schema_for(page, url):
    """Return JobPosting only when the operator supplied verified job facts."""
    job = job_data(page)
    if job.get("structured_data_verified") is not True or job.get("source_only") is True:
        return None
    details_url = http_url(job.get("job_details") or job.get("apply_url"))
    description = (
        "<p>" + escape(str(job.get("description") or "")).replace("\n", "<br>") + "</p>"
        "<p><strong>Qualifications:</strong> "
        + escape(str(job.get("qualifications") or ""))
        + "</p>"
    )
    schema = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "url": url,
        "title": job.get("title", ""),
        "description": description,
        "datePosted": job.get("date_posted", ""),
        "validThrough": "",
        "employmentType": job.get("employment_type", ""),
        "hiringOrganization": {
            "@type": "Organization",
            "name": job.get("company", ""),
            "sameAs": http_url(job.get("company_url")),
        },
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": job.get("city", ""),
                "addressRegion": job.get("region", ""),
                "addressCountry": job.get("country", ""),
            },
        },
        "identifier": {
            "@type": "PropertyValue",
            "name": job.get("company", ""),
            "value": str(page.get("job_number", "")),
        },
    }
    try:
        schema["validThrough"] = datetime.combine(
            date.fromisoformat(str(job["valid_through"])),
            time(23, 59, 59),
            timezone.utc,
        ).isoformat()
    except (ValueError, KeyError, TypeError):
        schema.pop("validThrough", None)

    return schema
