"""Bounded diagnostic extraction for HTML responses, never mislabeled PDF.

HTML is recorded for diagnostics only. This PDF publisher does not create
HTML reference pages or fabricate PDF content from an HTML response.
"""
from urllib.parse import urljoin
from bs4 import BeautifulSoup


def extract_html(content: bytes, url: str) -> dict:
    soup = BeautifulSoup(content[:2 * 1024 * 1024], "html.parser")
    def meta(name):
        tag = soup.find("meta", attrs={"name": lambda n: n and n.lower() == name})
        return (tag.get("content", "")[:2000] if tag else None)
    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else None
    description, robots = meta("description"), meta("robots")
    canonical = soup.find("link", rel="canonical")
    canonical = urljoin(url, canonical.get("href", "")) if canonical else None
    for node in soup(["script", "style", "nav", "footer", "noscript"]):
        node.decompose()
    text = " ".join(soup.stripped_strings)
    return dict(type="HTML", title=title, description=description, canonical=canonical,
                robots=robots, excerpt=text[:2000], textLength=len(text),
                extractionLimited=len(content) > 2 * 1024 * 1024)
