#!/usr/bin/env python
"""Local test PDF server for development & acceptance testing.

Serves a generated PDF with realistic metadata, a redirect chain, a 404,
a 429 and an HTML-masquerading-as-PDF endpoint so the whole pipeline
(validation, redirects, error handling) can be exercised offline.

    python scripts/dev_pdf_server.py --port 8899
"""
from __future__ import annotations

import argparse
import asyncio

import aiohttp.web
import pymupdf

PAGE_TEXT = (
    "Renewal Request Document\n\n"
    "This document is used to request a renewal for the registered party.\n"
    "Please complete all sections below, attach the supporting evidence, and "
    "submit the form before the expiry date stated on your registration.\n\n"
    "Section 1 - Applicant Information\n"
    "Name, address, contact details and the registration number must match "
    "the records held by the registrar. Discrepancies cause processing delays.\n\n"
    "Section 2 - Renewal Period\n"
    "Select the renewal period. Fees are calculated based on the period "
    "selected and the class of registration.\n\n"
    "Section 3 - Declaration\n"
    "The applicant declares that the information provided is true and "
    "complete, and accepts the conditions of continued registration.\n"
)


def make_pdf_bytes() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    # layout: title + wrapped body
    rect = pymupdf.Rect(72, 90, page.rect.width - 72, page.rect.height - 72)
    page.insert_textbox(rect, PAGE_TEXT, fontsize=11, fontname="helv")
    doc.set_metadata(
        {
            "title": "Renew Request Form 2026",
            "author": "DECS Testing Office",
            "subject": "Registration renewal request",
            "creator": "dev_pdf_server",
            "producer": "PyMuPDF",
            "creationDate": "D:20260901120000",
            "modDate": "D:20260915093000",
        }
    )
    return doc.tobytes()


_app_port: int = 8899


def make_app(port: int = 8899) -> aiohttp.web.Application:
    global _app_port
    _app_port = port
    app = aiohttp.web.Application()
    pdf = make_pdf_bytes()

    async def serve_pdf(_request):
        return aiohttp.web.Response(
            body=pdf, content_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="renew-request.pdf"'},
        )

    async def serve_octet(_request):
        # same file but with a lying/missing content type — signature must win
        return aiohttp.web.Response(body=pdf, content_type="application/octet-stream")

    async def redirect_once(_request):
        raise aiohttp.web.HTTPFound("/docs/report.pdf")

    async def redirect_loop(_request):
        raise aiohttp.web.HTTPFound("/docs/loop-2")

    async def redirect_loop2(_request):
        raise aiohttp.web.HTTPFound("/docs/loop.pdf")

    async def not_found(_request):
        raise aiohttp.web.HTTPNotFound(text="no such file")

    async def forbidden(_request):
        raise aiohttp.web.HTTPForbidden(text="denied")

    async def rate_limited(_request):
        raise aiohttp.web.HTTPTooManyRequests(text="slow down")

    async def ssrf_redirect(_request):
        # 302 to an INTERNAL hostname — must be blocked on redirect re-validation
        raise aiohttp.web.HTTPFound(f"http://localhost:{_app_port}/docs/report.pdf")

    async def html_not_pdf(_request):
        return aiohttp.web.Response(
            body=b"<html><body><h1>Not a PDF</h1></body></html>",
            content_type="text/html",
        )

    async def huge_pdf(_request):
        # 40 MB of junk — must be rejected by the size cap
        return aiohttp.web.Response(body=b"%PDF-1.4\n" + b"\0" * (40 * 1024 * 1024), content_type="application/pdf")

    async def html_article(request):
        variant = request.match_info["variant"]
        title = "Browser safety and document references"
        robots = "noindex" if variant == "noindex" else "index,follow"
        text = ("This article explains how public references describe documents while preserving "
                "the original publisher's ownership. Accurate metadata, ordinary links and "
                "honest discovery states help readers evaluate the original source. " * 3)
        if variant == "thin":
            text = "Click this article."
        if variant == "challenge":
            title = "Verify you are human"
        return aiohttp.web.Response(text=f'''<!doctype html><html><head><title>{title}</title>
            <meta name="description" content="Practical notes about safe resource discovery.">
            <meta name="robots" content="{robots}"><link rel="canonical" href="/blog/article"></head>
            <body><main><h1>{title}</h1><p>{text}</p></main><script>not real content</script></body></html>''',
            content_type="text/html")

    app.router.add_get("/blog/{variant}", html_article)
    app.router.add_get("/docs/report.pdf", serve_pdf)
    app.router.add_get("/docs/report-octetstream.pdf", serve_octet)
    app.router.add_get("/docs/redirect.pdf", redirect_once)
    app.router.add_get("/docs/loop.pdf", redirect_loop)
    app.router.add_get("/docs/loop-2", redirect_loop2)
    app.router.add_get("/docs/ssrf-redirect", ssrf_redirect)
    app.router.add_get("/docs/missing.pdf", not_found)
    app.router.add_get("/docs/forbidden.pdf", forbidden)
    app.router.add_get("/docs/slow.pdf", rate_limited)
    app.router.add_get("/docs/fake.pdf", html_not_pdf)
    app.router.add_get("/docs/huge.pdf", huge_pdf)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    aiohttp.web.run_app(make_app(args.port), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
