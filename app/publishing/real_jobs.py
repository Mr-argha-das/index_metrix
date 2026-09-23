"""Operator-authored, attested real vacancies; never converted from random demos."""
from datetime import date, datetime, time, timezone
from html import escape
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..utils import json_loads, utcnow


class RealJobIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=3, max_length=160)
    company: str = Field(min_length=2, max_length=160)
    company_url: str = Field(max_length=2000)
    apply_url: str = Field(max_length=2000)
    description: str = Field(min_length=200, max_length=20000)
    qualifications: str = Field(min_length=30, max_length=5000)
    employment_type: Literal["FULL_TIME", "PART_TIME", "CONTRACTOR", "TEMPORARY", "INTERN", "VOLUNTEER", "PER_DIEM", "OTHER"]
    city: str = Field(min_length=2, max_length=120)
    region: str = Field(min_length=2, max_length=120)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    date_posted: date
    valid_through: date
    authorized_real_vacancy: Literal[True]

    @field_validator("company_url", "apply_url")
    @classmethod
    def public_https(cls, value):
        from ..pdf.validator import normalize_url, _host_is_blocked_name
        import re
        # No outbound request; reject unsafe URL forms, literal/internal targets.
        # DNS is not fetched here because these are links, not server fetch targets.
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Use a public HTTPS link without credentials or fragments.")
        try:
            normalized = normalize_url(value)
            host = urlsplit(normalized).hostname or ""
            if _host_is_blocked_name(host) or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,63}", host) or host.endswith((".invalid", ".test")):
                raise ValueError()
            return normalized
        except Exception:
            raise ValueError("Use a valid public HTTPS URL.") from None

    @field_validator("title", "company", "description", "qualifications", "city", "region")
    @classmethod
    def plain_text(cls, value):
        if "<" in value or ">" in value or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise ValueError("Enter plain text, not HTML or control characters.")
        return value

    @model_validator(mode="after")
    def dates(self):
        today = utcnow().date()
        if self.date_posted > today or self.valid_through < today or self.valid_through < self.date_posted:
            raise ValueError("Use the actual original posting date (not future) and an unexpired closing date.")
        return self


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
