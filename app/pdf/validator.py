"""URL normalisation + SSRF validation.

The server fetches arbitrary user-supplied URLs, so this module is a security
boundary. Rules:

* only ``http://`` and ``https://`` are allowed (rejects ``javascript:``,
  ``file:``, ``data:``, ``ftp:``, ...)
* IP-literal hosts are validated directly
* hostname hosts are resolved (``getaddrinfo``) and **every** returned address
  must be public — loopback, private, link-local (incl. the 169.254.169.254
  cloud-metadata range), CGNAT, reserved and multicast ranges are all rejected
* common internal hostnames (``localhost``, ``*.local``, ``*.internal``, ...)
  are rejected before any DNS lookup
* every redirect hop is re-validated by the fetcher

``allow_private_targets`` (test/dev only) relaxes the private-address rule so
the test-suite can use a local PDF server. It is forced on in ``APP_ENV=test``
and must never be used in production.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = ("http", "https")

BLOCKED_HOSTNAMES = {
    "localhost",
    "0.0.0.0",
    "metadata",
    "metadata.google.internal",
    "ip6-localhost",
    "ip6-loopback",
    "ip6-localnet",
}

BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".example",
)

# RFC 6052's well-known /96 NAT64 prefix encodes an IPv4 destination in
# its low 32 bits. Some Python versions mark the IPv6 wrapper reserved,
# even for a public destination. Do not generalize this exception to other
# translation/transition prefixes or skip validation of the embedded IPv4.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")

# Networks that Python's ipaddress module does not flag for us but that must
# never be fetched from a browser-facing URL field.
_EXTRA_BLOCKED_NETS = [
    ipaddress.ip_network("100.64.0.0/10"),   # CGNAT / shared address space
    ipaddress.ip_network("198.18.0.0/15"),   # benchmarking
    ipaddress.ip_network("2001:db8::/32"),   # documentation
    ipaddress.ip_network("64:ff9b:1::/48"),  # network-local NAT64; not the public /96
]


class URLValidationError(ValueError):
    """Human-readable URL/SSRF validation failure."""


@dataclass
class ValidatedURL:
    url: str
    scheme: str
    host: str
    port: int
    resolved_ips: list[str] = field(default_factory=list)


def normalize_url(raw: str) -> str:
    """Normalise a user-supplied URL string. Raises URLValidationError."""
    if raw is None:
        raise URLValidationError("URL is empty.")
    url = raw.strip().strip("<>")
    if not url:
        raise URLValidationError("URL is empty.")
    if len(url) > 2048:
        raise URLValidationError("URL is too long (max 2048 characters).")
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise URLValidationError("URL contains illegal control characters.")
    if " " in url:
        # tolerate a single pasted "title <url>" style entry
        candidate = url.rsplit(" ", 1)[-1].strip()
        if candidate.startswith("http"):
            url = candidate
        else:
            raise URLValidationError(
                "URLs must not contain spaces. Paste one URL per line."
            )
    if not url.lower().startswith(("http://", "https://")):
        raise URLValidationError(
            f"Only http:// and https:// URLs are supported (got: '{url[:40]}...'). "
            "javascript:, file:, data: and other schemes are rejected."
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise URLValidationError("Malformed host or port.") from exc
    if parts.username is not None or parts.password is not None:
        raise URLValidationError("URLs containing credentials are not supported.")
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise URLValidationError("Only http and https schemes are allowed.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise URLValidationError("URL has no host.")
    # rebuild without fragment, with explicit port when non-default
    # (IPv6 literals keep their brackets)
    host_netloc = f"[{host}]" if ":" in host else host
    port = parts.port
    netloc = host_netloc
    if port is not None and port != (443 if parts.scheme.lower() == "https" else 80):
        netloc = f"{host_netloc}:{port}"
    normalized = urlunsplit(
        (parts.scheme.lower(), netloc, parts.path or "/", parts.query, "")
    )
    return normalized


def _host_is_blocked_name(host: str) -> str | None:
    h = host.lower().rstrip(".")
    if h in BLOCKED_HOSTNAMES:
        return "host is a reserved internal name"
    for suffix in BLOCKED_SUFFIXES:
        if h.endswith(suffix):
            return f"host uses the internal suffix '{suffix}'"
    return None


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip.version == 6 and ip in _NAT64_WELL_KNOWN:
        target = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        reason = _ip_is_blocked(target)
        if reason or not target.is_global:
            return f"NAT64 destination is {reason or 'non-public IPv4'} ({target})"
        return None
    if ip.is_unspecified:
        return "unspecified address (0.0.0.0 / ::)"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_private:
        return "private address"
    if ip.is_link_local:
        return "link-local address (cloud metadata range)"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_multicast:
        return "multicast address"
    for net in _EXTRA_BLOCKED_NETS:
        if ip.version == net.version and ip in net:
            return "shared/benchmark address space"
    if ip.version == 6 and (ip.sixtofour or ip.teredo):
        # sixtofour/teredo could tunnel into private space — be conservative
        return "special IPv6 transition address"
    return None


def validate_host(host: str, allow_private: bool = False) -> list[str]:
    """Validate a hostname/IP literal. Returns the list of usable public IPs.

    Raises URLValidationError when the host must not be fetched.
    """
    host = host.lower().rstrip(".")
    blocked = _host_is_blocked_name(host)
    if blocked:
        raise URLValidationError(f"URL blocked: {blocked}.")

    # IPv4-mapped / bracketed IPv6
    literal = host
    if host.startswith("[") and host.endswith("]"):
        literal = host[1:-1]

    try:
        addr = ipaddress.ip_address(literal)
    except ValueError:
        addr = None  # not an IP literal — treat as hostname

    if addr is not None:
        if not allow_private:
            reason = _ip_is_blocked(addr)
            if reason:
                raise URLValidationError(f"URL blocked: {reason} ({addr}).")
        return [str(addr)]

    # hostname: resolve ALL addresses and validate each
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise URLValidationError(f"DNS resolution failed for '{host}': {exc.strerror or exc}") from exc

    ips: list[str] = []
    for info in infos:
        ip_str = info[4][0].split("%")[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if not allow_private:
            reason = _ip_is_blocked(addr)
            if reason:
                raise URLValidationError(
                    f"URL blocked: host '{host}' resolves to a {reason} ({ip_str})."
                )
        if ip_str not in ips:
            ips.append(ip_str)
    if not ips:
        raise URLValidationError(f"Host '{host}' did not resolve to any address.")
    return ips


def validate_url(url: str, allow_private: bool = False) -> ValidatedURL:
    """Full validation: scheme + host + DNS resolution + private-IP checks."""
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if host.startswith("["):
        host = host[1:-1]
    ips = validate_host(host, allow_private=allow_private)
    return ValidatedURL(
        url=normalized, scheme=parts.scheme, host=host, port=port, resolved_ips=ips
    )
