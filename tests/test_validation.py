"""URL validation & SSRF protection tests (unit-level)."""
from __future__ import annotations

import pytest

from app.pdf.validator import URLValidationError, normalize_url, validate_url, validate_host


class TestNormalization:
    def test_simple(self):
        assert normalize_url("HTTPS://Example.COM/Path.PDF") == "https://example.com/Path.PDF"

    def test_strips_fragment(self):
        assert normalize_url("https://example.com/a.pdf#page=2") == "https://example.com/a.pdf"

    def test_strips_whitespace(self):
        assert normalize_url("  https://example.com/a.pdf  ") == "https://example.com/a.pdf"

    def test_lowercase_scheme(self):
        assert normalize_url("http://EXAMPLE.com/x").startswith("http://")

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "data:text/html,<x>",
            "ftp://example.com/x.pdf",
            "gopher://example.com",
            "",
            "   ",
            "not a url",
        ],
    )
    def test_rejects_non_http(self, url):
        with pytest.raises(URLValidationError):
            normalize_url(url)

    def test_rejects_very_long(self):
        with pytest.raises(URLValidationError):
            normalize_url("https://example.com/" + "a" * 3000)


class TestSSRFBadHosts:
    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "10.0.0.1",
            "10.255.255.1",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.1",
            "169.254.169.254",  # cloud metadata
            "100.64.0.1",  # CGNAT
            "::1",
            "fe80::1",
            "fc00::1",
            "fd12::1",
        ],
    )
    def test_blocks_private_addresses(self, host):
        with pytest.raises(URLValidationError):
            validate_host(host, allow_private=False)

    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "foo.localhost",
            "internal.local",
            "db.internal",
            "host.intranet",
            "myhost.lan",
            "metadata",
        ],
    )
    def test_blocks_internal_hostnames(self, host):
        with pytest.raises(URLValidationError):
            validate_host(host, allow_private=False)

    def test_blocks_full_private_urls(self):
        with pytest.raises(URLValidationError):
            validate_url("http://192.168.1.5/secret.pdf", allow_private=False)
        with pytest.raises(URLValidationError):
            validate_url("http://[::1]/x.pdf", allow_private=False)
        with pytest.raises(URLValidationError):
            validate_url("http://localhost/admin", allow_private=False)

    def test_allows_public(self):
        ips = validate_host("8.8.8.8", allow_private=False)
        assert ips == ["8.8.8.8"]

    def test_allow_private_flag_for_tests(self):
        # the test flag (dev/test only) relaxes the private check
        ips = validate_host("127.0.0.1", allow_private=True)
        assert ips == ["127.0.0.1"]


class TestFetcherSSRF:
    """The fetcher must re-validate every redirect hop."""

    def test_fetch_blocks_redirect_to_internal_name(self, pdf_server_url):
        from app.config import get_settings
        from app.pdf.fetcher import FetchError, SafeFetcher

        settings = get_settings()

        async def run():
            fetcher = SafeFetcher(settings)
            try:
                # /docs/ssrf-redirect sends a 302 to http://localhost/...
                await fetcher.fetch(f"{pdf_server_url}/docs/ssrf-redirect")
                return None
            except FetchError as exc:
                return exc
            finally:
                await fetcher.close()

        import asyncio

        exc = asyncio.run(run())
        assert exc is not None
        assert "reserved internal name" in exc.message or "blocked" in exc.message.lower()

    def test_fetch_blocks_redirect_loop(self, pdf_server_url):
        from app.config import get_settings
        from app.pdf.fetcher import FetchError, SafeFetcher

        settings = get_settings()

        async def run():
            fetcher = SafeFetcher(settings)
            try:
                await fetcher.fetch(f"{pdf_server_url}/docs/loop.pdf")
                return None
            except FetchError as exc:
                return exc
            finally:
                await fetcher.close()

        import asyncio

        exc = asyncio.run(run())
        assert exc is not None
        assert exc.code in ("REDIRECT_LOOP", "REDIRECT_ERROR")

    def test_fetch_validates_success(self, pdf_server_url):
        from app.config import get_settings
        from app.pdf.fetcher import SafeFetcher

        settings = get_settings()

        async def run():
            fetcher = SafeFetcher(settings)
            try:
                return await fetcher.fetch(f"{pdf_server_url}/docs/report.pdf")
            finally:
                await fetcher.close()

        import asyncio

        result = asyncio.run(run())
        assert result.status == 200
        assert result.content.startswith(b"%PDF-")
        assert result.content_type == "application/pdf"

    def test_honest_user_agent(self, pdf_server_url):
        from app.config import get_settings
        from app.pdf.fetcher import SafeFetcher

        settings = get_settings()
        assert "BOT-INDEXER" in settings.fetch_user_agent
        assert "googlebot" not in settings.fetch_user_agent.lower()
