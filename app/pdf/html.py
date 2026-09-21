"""Bounded HTML/blog extraction. Never executes scripts or invents content."""
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup


_HTML_START = re.compile(
    r"^<(?:!doctype\s+html\b|html\b|head\b|body\b|main\b|article\b)",
    re.IGNORECASE,
)


def is_html_response(content: bytes, content_type: str | None) -> bool:
    """Recognize web pages without requiring .html or a perfect MIME header.

    Sniff only a bounded prefix when a server uses a generic/missing MIME.
    Do not treat JSON, images, or arbitrary binary downloads as web pages.
    The caller gives a real PDF signature precedence over this detector.
    """
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type in ("text/html", "application/xhtml+xml"):
        return True
    if media_type not in ("", "text/plain", "application/octet-stream"):
        return False
    prefix = content[:4096].decode("utf-8-sig", errors="replace").lstrip()
    if prefix.lower().startswith("<?xml"):
        end = prefix.find("?>")
        if end < 0:
            return False
        prefix = prefix[end + 2:].lstrip()
    # Strip comments iteratively; a repeated wildcard regex could backtrack
    # exponentially on hostile input. All sniffing stays within 4 KiB.
    while prefix.startswith("<!--"):
        end = prefix.find("-->", 4)
        if end < 0:
            return False
        prefix = prefix[end + 3:].lstrip()
    return bool(_HTML_START.match(prefix))


def extract_html(content: bytes, url: str) -> dict:
    soup = BeautifulSoup(content[:2 * 1024 * 1024], "html.parser")
    def meta(name):
        tag = soup.find("meta", attrs={"name": lambda n: n and n.lower() == name})
        return (tag.get("content", "")[:2000] if tag else None)
    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else None
    # Use an actual heading when a public page omits its <title>; never invent one.
    if not title:
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True)[:300] if heading else None
    description = meta("description")
    # Restrictive directives win when publishers use multiple robots tags.
    robots = ", ".join(tag.get("content", "") for tag in soup.find_all(
        "meta", attrs={"name": lambda n: n and n.lower() in ("robots", "googlebot")})) or None
    canonical = soup.find("link", rel="canonical")
    canonical = urljoin(url, canonical.get("href", "")) if canonical else None
    if canonical and not canonical.startswith(("http://", "https://")):
        canonical = None
    for node in soup(["script", "style", "nav", "footer", "header", "noscript", "form", "head"]):
        node.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = " ".join(main.stripped_strings)
    # Conservative quality/control checks: leave challenges and login walls alone.
    lowered = ((title or "") + " " + text[:2000]).lower()
    blocked = any(marker in lowered for marker in (
        "verify you are human", "checking your browser", "enable javascript and cookies to continue",
        "attention required!", "access denied", "captcha", "sign in to continue", "log in to continue",
    ))
    noindex = any(token in (robots or "").lower().replace(",", " ").split() for token in ("noindex", "none"))
    reason = ("Access/challenge page detected; no bypass attempted." if blocked else
              "Source requests noindex; reference publication withheld." if noindex else
              "Insufficient extractable page text (minimum 120 characters and a title)." if not title or len(text) < 120 else None)
    return dict(type="HTML", title=title, description=description, canonical=canonical,
                robots=robots[:2000] if robots else None, excerpt=text[:2000], textLength=len(text),
                extractionLimited=len(content) > 2 * 1024 * 1024,
                publishable=reason is None, rejectionReason=reason)
