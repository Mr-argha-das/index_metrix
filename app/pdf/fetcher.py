"""Safe outbound HTTP fetcher for remote PDFs.

Security properties
-------------------
* **DNS pinning against rebinding** — before connecting, the host is resolved
  and every address validated (see :mod:`app.pdf.validator`); the validated IP
  list is then *pinned* into an aiohttp resolver for that connection, so the
  TCP connection can never land on a different (private) address than the one
  we validated. TLS SNI/Host headers still use the original hostname.
* **Redirects** — followed manually, each hop re-validated (scheme, host,
  private IPs), with a hard cap on hop count.
* **Timeouts** — configurable connect (10 s default) and read (30 s default).
* **Size caps** — body is streamed and aborted once the cap (35 MB default)
  is exceeded; headers are limited in size.
* **Per-host rate limiting** — sliding window + per-host concurrency cap.
* **Honest identity** — the User-Agent identifies this application
  (``BOT-INDEXER/1.0``). We never impersonate Googlebot or any other
  search-engine crawler; a fetch by our server is a *technical probe*, not a
  search-engine crawl.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp

from .validator import URLValidationError, ValidatedURL, validate_url

log = logging.getLogger("bot_indexer.fetcher")

MAX_HEADER_SIZE = 16 * 1024  # bytes; aiohttp raises on bigger headers


class FetchError(Exception):
    """A fetch failed. ``code`` classifies the failure for the UI."""

    def __init__(self, message: str, code: str = "FETCH_ERROR", status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.retryable = retryable


class PinnedResolver(aiohttp.resolver.ThreadedResolver):
    """Resolver that returns only previously validated IPs for pinned hosts.

    Subclasses the threaded stdlib resolver (no aiodns dependency). When a
    host is pinned, only the validated IP list is returned — this closes the
    DNS-rebinding window between our check and the actual connection.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pins: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _family_of(ip: str) -> int:
        return socket.AF_INET6 if ":" in ip else socket.AF_INET

    async def pin(self, host: str, ips: list[str]) -> None:
        async with self._lock:
            self._pins[host.lower().rstrip(".")] = [
                {"host": ip, "family": self._family_of(ip)} for ip in ips
            ]

    async def unpinned(self, host: str) -> None:
        async with self._lock:
            self._pins.pop(host.lower().rstrip("."), None)

    async def resolve(self, host: str, port: int, family: int) -> list[dict]:
        host_key = host.lower().rstrip(".")
        pinned = self._pins.get(host_key)
        if pinned:
            return [
                {"host": p["host"], "port": port, "family": p["family"], "proto": 0}
                for p in pinned
            ]
        raise OSError("Unvalidated DNS resolution refused")


class HostRateLimiter:
    """Sliding-window per-host request rate limit + concurrency cap."""

    def __init__(self, per_minute: int = 10, per_host_concurrency: int = 2, delay: float = 1.0):
        self.delay = delay
        self.host_delays: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self.per_minute = per_minute
        self.per_host_concurrency = per_host_concurrency
        self._hits: dict[str, deque] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    def _sem(self, host: str) -> asyncio.Semaphore:
        sem = self._sems.get(host)
        if sem is None:
            sem = asyncio.Semaphore(self.per_host_concurrency)
            self._sems[host] = sem
        return sem

    async def acquire(self, host: str) -> None:
        await self._sem(host).acquire()
        try:
            async with self._host_locks.setdefault(host, asyncio.Lock()):
                dq = self._hits.setdefault(host, deque())
                now = time.monotonic()
                while dq and dq[0] <= now - 60:
                    dq.popleft()
                wait = max(0, self._last.get(host, 0) + max(self.delay, self.host_delays.get(host, 0)) - now)
                if len(dq) >= self.per_minute:
                    wait = max(wait, dq[0] + 60 - now)
                await asyncio.sleep(wait)
                now = time.monotonic()
                while dq and dq[0] <= now - 60:
                    dq.popleft()
                dq.append(now)
                self._last[host] = now
        except BaseException:
            self.release(host)
            raise

    def release(self, host: str) -> None:
        sem = self._sems.get(host)
        if sem is not None:
            sem.release()


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    content_type: str
    content: bytes
    content_length: int
    redirect_chain: list[str] = field(default_factory=list)
    response_time_ms: int = 0
    fetched_at: str = ""
    robots_checks: list[dict] = field(default_factory=list)


class SafeFetcher:
    def __init__(self, settings, rate_limiter: HostRateLimiter | None = None):
        self.settings = settings
        self.rate_limiter = rate_limiter or HostRateLimiter(
            settings.fetch_rate_per_minute, settings.fetch_concurrency_per_host, settings.fetch_host_delay_seconds
        )
        self._robots_cache: dict[str, tuple] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._resolver = PinnedResolver()
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=300,
                connect=self.settings.connect_timeout,
                sock_connect=self.settings.connect_timeout,
                sock_read=self.settings.http_timeout,
            )
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    resolver=self._resolver,
                    use_dns_cache=False,
                    force_close=True,
                    ssl=True,
                ),
                timeout=timeout,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------

    async def fetch(
        self,
        url: str,
        max_bytes: int | None = None,
        method: str = "GET",
        probe_only: bool = False,
        _robots_request: bool = False,
    ) -> FetchResult:
        """Fetch a URL with SSRF protection on every redirect hop.

        ``probe_only`` reads at most 64 KB (enough for signature + headers).
        """
        settings = self.settings
        max_bytes = max_bytes or (
            64 * 1024 if probe_only else settings.max_pdf_size_mb * 1024 * 1024
        )
        current = url
        chain: list[str] = []
        robots_checks = []
        deadline_redirects = settings.max_redirects
        session = await self._get_session()

        for hop in range(deadline_redirects + 1):
            try:
                validated: ValidatedURL = await asyncio.to_thread(
                    validate_url, current, allow_private=settings.allow_private_targets
                )
            except URLValidationError as exc:
                kind = "redirect" if hop > 0 else "URL"
                raise FetchError(
                    f"{kind.capitalize()} blocked by SSRF protection: {exc}",
                    code="SSRF_BLOCKED",
                    retryable=False,
                ) from exc
            if not _robots_request:
                robots_checks.append(await self.check_robots(validated.url))
            await self.rate_limiter.acquire(validated.host)
            try:
                await self._resolver.pin(validated.host, validated.resolved_ips)
                result = await self._do_request(
                    session, validated.url, method, max_bytes, probe_only
                )
            finally:
                self.rate_limiter.release(validated.host)

            if result.status in (301, 302, 303, 307, 308):
                location = result.headers.get("location") or result.headers.get("Location")
                if not location:
                    raise FetchError(
                        f"Redirect response {result.status} without Location header.",
                        code="REDIRECT_ERROR",
                        status=result.status,
                        retryable=False,
                    )
                next_url = urljoin(validated.url, location.strip())
                chain.append(validated.url)
                if hop >= deadline_redirects:
                    raise FetchError(
                        f"Too many redirects (max {deadline_redirects}).",
                        code="REDIRECT_LOOP",
                        status=result.status,
                        retryable=False,
                    )
                current = next_url
                continue
            result.url = url
            result.redirect_chain = chain
            result.robots_checks = robots_checks
            return result

        raise FetchError("Redirect limit exhausted.", code="REDIRECT_LOOP", retryable=False)

    async def check_robots(self, url: str) -> dict:
        """Conservative robots policy. Never bypass denial/unavailable controls.

        404/410 means no policy; 401/403 denies; 429/5xx/network failure
        retries later. Uses the same size/redirect/SSRF boundaries as content.
        """
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_locks.setdefault(origin, asyncio.Lock()):
            cached = self._robots_cache.get(origin)
            if not cached or cached[0] < time.monotonic():
                response = await self.fetch(origin + "/robots.txt", max_bytes=512 * 1024, _robots_request=True)
                parser = RobotFileParser()
                if response.status in (404, 410):
                    parser.allow_all = True
                elif response.status in (401, 403):
                    parser.disallow_all = True
                elif response.status == 200 and response.content_type in ("text/plain", ""):
                    parser.parse(response.content.decode("utf-8", errors="replace").splitlines())
                else:
                    raise FetchError("robots.txt unavailable or not a usable policy; resource fetch deferred.",
                                     code="ROBOTS_UNAVAILABLE", status=response.status, retryable=True)
                cached = (time.monotonic() + 3600, parser, response.status, _iso_now())
                if len(self._robots_cache) >= 512:
                    self._robots_cache.pop(next(iter(self._robots_cache)))
                self._robots_cache[origin] = cached
            _, parser, status, checked = cached
        allowed = parser.can_fetch(self.settings.fetch_user_agent, url)
        if not allowed:
            raise FetchError("robots.txt disallows this URL for our fetcher.", code="ROBOTS_DENIED", retryable=False)
        crawl_delay = parser.crawl_delay(self.settings.fetch_user_agent)
        if crawl_delay:
            self.rate_limiter.host_delays[parts.hostname] = float(crawl_delay)
        return {"url": origin + "/robots.txt", "status": status, "allowed": True, "checkedAt": checked}

    async def _do_request(
        self,
        session: aiohttp.ClientSession,
        url: str,
        method: str,
        max_bytes: int,
        probe_only: bool,
    ) -> FetchResult:
        headers = {"User-Agent": self.settings.fetch_user_agent, "Accept": "*/*"}
        started = time.monotonic()
        try:
            async with session.request(
                method, url, headers=headers, allow_redirects=False
            ) as resp:
                status = resp.status
                hdrs = {k: v for k, v in resp.headers.items()}
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                content_length_hdr = resp.headers.get("Content-Length")
                if status in (301, 302, 303, 307, 308) or method == "HEAD":
                    return FetchResult(url, str(resp.url), status, hdrs, content_type, b"", 0)
                if not probe_only and content_length_hdr and content_length_hdr.isdigit() and int(content_length_hdr) > max_bytes:
                    raise FetchError("Response exceeds maximum download size.", code="TOO_LARGE", status=status, retryable=False)
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if probe_only:
                        chunk = chunk[:max(0, max_bytes - total)]
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(
                            f"Response exceeds maximum size of "
                            f"{self.settings.max_pdf_size_mb} MB.",
                            code="TOO_LARGE",
                            status=status,
                            retryable=False,
                        )
                    chunks.append(chunk)
                    if probe_only and total >= max_bytes:
                        break
                elapsed_ms = int((time.monotonic() - started) * 1000)
                return FetchResult(
                    url=url,
                    final_url=str(resp.url),
                    status=status,
                    headers=hdrs,
                    content_type=content_type,
                    content=b"".join(chunks),
                    content_length=(
                        int(content_length_hdr)
                        if content_length_hdr and content_length_hdr.isdigit()
                        else total
                    ),
                    redirect_chain=[],
                    response_time_ms=elapsed_ms,
                    fetched_at=_iso_now(),
                )
        except FetchError:
            raise
        except aiohttp.ClientConnectionError as exc:
            raise FetchError(
                f"Could not connect to the remote server: {_explain(exc)}",
                code="CONNECTION_FAILED",
                retryable=True,
            ) from exc
        except asyncio.TimeoutError:
            raise FetchError(
                f"Request timed out after {self.settings.http_timeout}s. "
                "The remote server is slow or unavailable.",
                code="TIMEOUT",
                retryable=True,
            ) from None
        except ssl.SSLError as exc:
            raise FetchError(
                f"TLS/SSL error while contacting the remote server: {_explain(exc)}",
                code="TLS_ERROR",
                retryable=False,
            ) from exc
        except aiohttp.ClientSSLError as exc:
            raise FetchError(
                f"TLS/SSL error while contacting the remote server: {_explain(exc)}",
                code="TLS_ERROR",
                retryable=False,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise FetchError(
                f"Fetch failed: {_explain(exc)}",
                code="FETCH_ERROR",
                retryable=True,
            ) from exc


def _explain(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text[:300]


def _iso_now() -> str:
    from ..utils import utcnow_iso

    return utcnow_iso()
