"""Bounded HTML/blog extraction. Never executes scripts or invents content."""
from urllib.parse import urljoin
from bs4 import BeautifulSoup


def extract_html(content: bytes, url: str) -> dict:
    soup = BeautifulSoup(content[:2 * 1024 * 1024], "html.parser")
    def meta(name):
        tag = soup.find("meta", attrs={"name": lambda n: n and n.lower() == name})
        return (tag.get("content", "")[:2000] if tag else None)
    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else None
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
              "Insufficient extractable article text (minimum 120 characters and a title)." if not title or len(text) < 120 else None)
    return dict(type="HTML", title=title, description=description, canonical=canonical,
                robots=robots[:2000] if robots else None, excerpt=text[:2000], textLength=len(text),
                extractionLimited=len(content) > 2 * 1024 * 1024,
                publishable=reason is None, rejectionReason=reason)
