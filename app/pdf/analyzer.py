"""PDF inspection with PyMuPDF (pymupdf/fitz).

Safely parses an in-memory PDF byte string (PyMuPDF does not execute embedded
JavaScript). Extracts technical metadata, a bounded amount of text for
classification/preview, and a coarse language estimate.

Classification:
* TEXT_PDF              — extractable text present
* SCANNED_OR_EMPTY_PDF  — pages but no meaningful extractable text
* INVALID_PDF           — bytes do not parse as PDF
* ERROR                 — unexpected analysis failure
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pymupdf  # PyMuPDF (the ``fitz`` alias is deprecated)

log = logging.getLogger("bot_indexer.analyzer")

MAX_TEXT_PAGES = 300          # never extract text from more than this many pages
FIRST_PAGE_PREVIEW_LIMIT = 2000  # chars kept for the dedicated page
TEXT_PDF_MIN_CHARS = 1       # below this we call it scanned/empty

_PDF_SIGNATURE = b"%PDF-"

# Tiny English word list for the coarse language estimate (not a real NLP
# model — the result is labelled an *estimate*).
_ENGLISH_HINTS = {
    "the", "and", "for", "with", "that", "this", "are", "was", "were", "from",
    "have", "has", "had", "not", "you", "your", "our", "will", "would", "can",
    "shall", "may", "might", "must", "should", "document", "page", "report",
    "section", "article", "form", "request", "information", "please", "note",
    "date", "name", "address", "email", "total", "amount", "signature",
    "agreement", "terms", "conditions", "policy", "procedure", "guidelines",
}


@dataclass
class AnalysisResult:
    ok: bool
    error: str | None = None
    signature_ok: bool = False
    page_count: int | None = None
    title: str | None = None
    author: str | None = None
    subject: str | None = None
    creator: str | None = None
    producer: str | None = None
    creation_date: str | None = None
    modification_date: str | None = None
    text_length: int | None = None
    first_page_text: str | None = None
    language: str | None = None  # coarse estimate: "en" | "und"
    classification: str = "ERROR"
    details: dict = field(default_factory=dict)


def check_signature(data: bytes) -> bool:
    """The PDF header must be at the start of the file, with only a small
    amount of stray leading whitespace allowed (some producers emit it)."""
    if not data:
        return False
    stripped = data.lstrip(b" \t\r\n\x00")
    if len(data) - len(stripped) > 1024:
        return False
    return stripped.startswith(_PDF_SIGNATURE)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def estimate_language(text: str) -> str:
    if not text:
        return "und"
    sample = text[:20000].lower()
    words = [w for w in sample.split() if w.isalpha()]
    if not words:
        return "und"
    hits = sum(1 for w in words if w in _ENGLISH_HINTS)
    ratio = hits / max(len(words), 1)
    # Latin-script content with a reasonable English-word hit rate
    if ratio >= 0.03:
        return "en"
    return "und"


def analyze_pdf(data: bytes) -> AnalysisResult:
    sig_ok = check_signature(data)
    if not sig_ok:
        return AnalysisResult(
            ok=False,
            error="The remote file does not start with the PDF signature (%PDF-). "
            "It is not a valid PDF document.",
            signature_ok=False,
            classification="INVALID_PDF",
        )

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        log.warning("PDF parse failed: %s", exc)
        return AnalysisResult(
            ok=False,
            error=f"The file has a PDF signature but could not be parsed: {exc}",
            signature_ok=True,
            classification="INVALID_PDF",
        )

    try:
        if doc.needs_pass:
            return AnalysisResult(ok=False, signature_ok=True, classification="INVALID_PDF", error="Encrypted PDF requires authentication; no password bypass attempted.")
        page_count = doc.page_count
        if not page_count:
            return AnalysisResult(ok=False, signature_ok=True, classification="INVALID_PDF", error="PDF has no pages.")
        # PyMuPDF >=1.24: `metadata` property; older versions: get_metadata()
        meta = doc.metadata if hasattr(doc, "metadata") else (doc.get_metadata() or {})
        meta = meta or {}

        def _date(value):
            raw = _clean(meta.get(value))
            return raw

        text_parts: list[str] = []
        total_text_len = 0
        for i in range(min(page_count, MAX_TEXT_PAGES)):
            try:
                text = doc[i].get_text("text") or ""
            except Exception:  # noqa: BLE001 - a damaged page should not kill analysis
                continue
            total_text_len += len(text.strip())
            if i == 0:
                text_parts.append(text[:FIRST_PAGE_PREVIEW_LIMIT])
            elif len("".join(text_parts)) < FIRST_PAGE_PREVIEW_LIMIT * 4:
                text_parts.append(text[:FIRST_PAGE_PREVIEW_LIMIT // 2])

        full_sample = "".join(text_parts)
        text_length = total_text_len
        first_page_text = _clean(text_parts[0]) if text_parts else None
        language = estimate_language(full_sample)

        if page_count == 0:
            classification = "INVALID_PDF"
        elif text_length >= TEXT_PDF_MIN_CHARS:
            classification = "TEXT_PDF"
        else:
            classification = "SCANNED_OR_EMPTY_PDF"

        return AnalysisResult(
            ok=True,
            signature_ok=True,
            page_count=page_count,
            title=_clean(meta.get("title")),
            author=_clean(meta.get("author")),
            subject=_clean(meta.get("subject")),
            creator=_clean(meta.get("creator")),
            producer=_clean(meta.get("producer")),
            creation_date=_date("creationDate"),
            modification_date=_date("modDate"),
            text_length=text_length,
            first_page_text=first_page_text,
            language=language,
            classification=classification,
            details={
                "pages_scanned": min(page_count, MAX_TEXT_PAGES),
                "encrypted": bool(doc.needs_pass),
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("PDF analysis failed: %s", exc)
        return AnalysisResult(
            ok=False,
            error=f"PDF analysis failed: {exc}",
            signature_ok=True,
            classification="ERROR",
        )
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass


async def analyze_pdf_isolated(data: bytes) -> AnalysisResult:
    """Bound native-parser memory/CPU independently of the web server."""
    import asyncio
    import json
    import sys

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.pdf.analysis_worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), timeout=45)
        if process.returncode:
            return AnalysisResult(ok=False, error="PDF parser failed or exceeded its CPU/memory limit.")
        return AnalysisResult(**json.loads(output))
    except asyncio.TimeoutError:
        return AnalysisResult(ok=False, error="PDF analysis exceeded its 45-second deadline.")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
