"""NAT64 public reachability must not provide a tunnel around SSRF checks."""
import ipaddress
import socket

import pytest

from app.config import Settings
from app.pdf.fetcher import FetchError, FetchResult, SafeFetcher
from app.pdf.validator import URLValidationError, validate_host, validate_url


PREFIX = int(ipaddress.IPv6Address('64:ff9b::'))


def translated(ipv4):
    return str(ipaddress.IPv6Address(PREFIX | int(ipaddress.IPv4Address(ipv4))))


def dns_answers(monkeypatch, *addresses):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM,
         socket.IPPROTO_TCP, '', (ip, 0, 0, 0) if ':' in ip else (ip, 0)) for ip in addresses])


@pytest.mark.parametrize('ipv4', ['35.9.37.60', '8.8.8.8', '1.1.1.1'])
def test_nat64_literal_allows_only_public_destination(ipv4):
    ip = translated(ipv4)
    assert validate_host(ip, allow_private=False) == [ip]
    checked = validate_url(f'https://[{ip}]/file.pdf', allow_private=False)
    assert checked.resolved_ips == [ip]  # retain IPv6 for the NAT64 network


@pytest.mark.parametrize('addresses', [
    ['64:ff9b::2309:253c'],
    ['35.9.37.60', '64:ff9b::2309:253c'],
    ['64:ff9b::2309:253c', '35.9.37.60'],
])
def test_reported_msu_dns_answer_passes_without_private_bypass(monkeypatch, addresses):
    dns_answers(monkeypatch, *addresses)
    checked = validate_url('http://www.egr.msu.edu/decs/file.pdf', allow_private=False)
    assert checked.resolved_ips == addresses
    assert checked.host == 'www.egr.msu.edu'


@pytest.mark.parametrize('ipv4', [
    '0.0.0.0', '0.0.0.1', '127.0.0.1', '10.0.0.1', '172.16.0.1',
    '192.168.1.1', '169.254.169.254', '100.64.0.1', '100.100.100.200',
    '198.18.0.1', '192.0.2.1', '198.51.100.1', '203.0.113.1',
    '224.0.0.1', '240.0.0.1', '255.255.255.255',
])
def test_nat64_private_and_special_destinations_are_blocked(monkeypatch, ipv4):
    ip = translated(ipv4)
    with pytest.raises(URLValidationError, match='NAT64 destination'):
        validate_host(ip, allow_private=False)
    # A safe A record cannot hide a malicious translated AAAA answer.
    dns_answers(monkeypatch, '35.9.37.60', ip)
    with pytest.raises(URLValidationError, match='NAT64 destination'):
        validate_host('attacker.invalid', allow_private=False)


@pytest.mark.parametrize('address', [
    '64:ff9b:1::2309:253c', '64:ff9b:0:1::2309:253c',
    '::ffff:127.0.0.1', '2002:7f00:1::', 'fd00::2309:253c',
])
def test_no_blanket_exemption_for_other_ipv6_forms(address):
    with pytest.raises(URLValidationError):
        validate_host(address, allow_private=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('redirect', [False, True])
async def test_fetcher_keeps_nat64_dns_pinning_and_blocks_private_redirect(monkeypatch, redirect):
    dns_answers(monkeypatch, '64:ff9b::2309:253c')
    settings = Settings(_env_file=None, app_env='development', allow_private_targets=False,
                        fetch_host_delay_seconds=0)
    fetcher = SafeFetcher(settings)
    requests = []
    async def robots(url):
        return {'allowed':True}  # isolate redirect/pinning policy; no network calls
    async def request(session, url, method, maximum, probe):
        pins = await fetcher._resolver.resolve('www.egr.msu.edu', 443, socket.AF_UNSPEC)
        assert pins[0]['host'] == '64:ff9b::2309:253c'
        assert pins[0]['family'] == socket.AF_INET6
        requests.append(url)
        return FetchResult(url=url, final_url=url, status=302 if redirect else 200,
            headers={'Location':'http://[64:ff9b::a9fe:a9fe]/metadata'} if redirect else {},
            content=b'%PDF-test', content_type='application/pdf', content_length=9)
    monkeypatch.setattr(fetcher, 'check_robots', robots)
    monkeypatch.setattr(fetcher, '_do_request', request)
    try:
        if redirect:
            with pytest.raises(FetchError) as exc:
                await fetcher.fetch('https://www.egr.msu.edu/file.pdf')
            assert exc.value.code == 'SSRF_BLOCKED'
            assert 'NAT64 destination' in exc.value.message
        else:
            assert (await fetcher.fetch('https://www.egr.msu.edu/file.pdf')).status == 200
        assert len(requests) == 1
    finally:
        await fetcher.close()
