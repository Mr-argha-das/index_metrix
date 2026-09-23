"""Operator-authored, attested real vacancies; never converted from random demos."""
from datetime import date, datetime, time, timezone
from html import escape
from urllib.parse import urlsplit


from ..utils import json_loads, utcnow


def job_data(page):
    return json_loads(page.get("real_job"), {}) or {}


def is_open(page):
    if page.get("page_kind") != "real-job" or page.get("job_status") != "OPEN":
        return False
    try:
        return date.fromisoformat(job_data(page)["valid_through"]) >= utcnow().date()
    except (ValueError, KeyError, TypeError):
        return False


def discoverable(page):
    return page.get("page_kind") != "real-job" or is_open(page)


def schema_for(page, url):
    """All structured facts correspond to visible, operator-provided fields."""
    job = job_data(page)
    return {
        "@context": "https://schema.org", "@type": "JobPosting", "url": url,
        "title": job["title"],
        "description": "<p>" + escape(job["description"]).replace("\n", "<br>") + "</p><p>Qualifications: " + escape(job["qualifications"]) + "</p>",
        "datePosted": job["date_posted"],
        "validThrough": datetime.combine(date.fromisoformat(job["valid_through"]), time(23, 59, 59), timezone.utc).isoformat(),
        "employmentType": job["employment_type"],
        "hiringOrganization": {"@type": "Organization", "name": job["company"], "sameAs": job["company_url"]},
        "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": job["city"], "addressRegion": job["region"], "addressCountry": job["country"]}},
        "identifier": {"@type": "PropertyValue", "name": job["company"], "value": str(page["job_number"])},
    }
