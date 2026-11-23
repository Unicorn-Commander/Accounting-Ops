"""Outbound-request guard for URLs a workspace (not the operator) chose.

Any URL that arrives from a tenant — a SimpleFIN claim / access URL, a BYOK
``base_url`` — is an SSRF vector: a signed-in user can own a workspace
(self-serve onboarding makes them ``owner``), so "admin-only" is not a trust
boundary. Every such URL goes through this module at WRITE time and at USE
time.

Policy
------
* ``https`` only, a hostname is required.
* Every address the host resolves to must be public. Refused: loopback,
  RFC1918, link-local (incl. 169.254.169.254 metadata), 100.64.0.0/10 (CGNAT),
  192.0.0.0/24, 0.0.0.0/8, reserved / broadcast, multicast, IPv6 loopback /
  unspecified / ULA / link-local / site-local / multicast, and IPv4 embedded in
  IPv6 (mapped, NAT64, 6to4) when the embedded address is itself refused.
  If ANY resolved address is refused, the whole host is refused.
* No redirects (``follow_redirects=False``) and no environment proxies.
* DNS-rebinding: :class:`GuardedAsyncTransport` re-resolves and re-checks at
  connect time and connects to the CHECKED IP, so the address that was checked
  is the address that is dialled. TLS SNI / certificate checks still use the
  original hostname.

Use :func:`assert_public_https_url` before a request (refuses with no request
sent) and :func:`guarded_async_client` to make it.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

__all__ = (
    "OutboundURLRefused",
    "GuardedAsyncTransport",
    "assert_public_https_url",
    "check_url_static",
    "guarded_async_client",
    "is_public_ip",
)


class OutboundURLRefused(ValueError):
    """The URL (or an address it resolves to) is not an allowed outbound target.

    The message is safe to show the caller: it never carries credentials.
    """


_BLOCKED_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address | str) -> bool:
    """True only for a globally routable unicast address."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not is_public_ip(embedded):
            return False
    if any(ip.version == net.version and ip in net for net in _BLOCKED_NETS):
        return False
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified or ip.is_loopback:
        return False
    if ip.is_link_local or ip.is_private:
        return False
    return ip.is_global


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse ``host`` as an IP, including legacy IPv4 forms (``2130706433``,
    ``0x7f.1``) that ``getaddrinfo`` would happily accept."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def check_url_static(url: str) -> tuple[str, int]:
    """Checks that need no DNS: scheme, host, IP-literal policy, local names.

    Returns ``(hostname, port)``. Raises :class:`OutboundURLRefused`.
    """
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError as exc:
        raise OutboundURLRefused("URL is malformed") from exc
    if parts.scheme.lower() != "https":
        raise OutboundURLRefused("URL must use https")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise OutboundURLRefused("URL has no host")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise OutboundURLRefused("URL host is a local name")
    ip = _literal_ip(host)
    if ip is not None and not is_public_ip(ip):
        raise OutboundURLRefused("URL host is not a public address")
    return host, port or 443


async def _resolve(host: str, port: int) -> list[str]:
    """getaddrinfo → unique addresses, in resolver order. Patched in tests."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in out:
            out.append(addr)
    return out


async def resolve_public(host: str, port: int) -> list[str]:
    """Resolve ``host`` and return its addresses, refusing if any is not public."""
    ip = _literal_ip(host)
    if ip is not None:
        if not is_public_ip(ip):
            raise OutboundURLRefused("URL host is not a public address")
        return [str(ip)]
    try:
        addrs = await _resolve(host, port)
    except OSError as exc:
        raise OutboundURLRefused("URL host does not resolve") from exc
    if not addrs:
        raise OutboundURLRefused("URL host does not resolve")
    for addr in addrs:
        if not is_public_ip(addr):
            raise OutboundURLRefused("URL host resolves to a non-public address")
    return addrs


async def assert_public_https_url(url: str) -> None:
    """Full check (static + DNS). Raise :class:`OutboundURLRefused` on refusal."""
    host, port = check_url_static(url)
    await resolve_public(host, port)


# The real socket backend. A module attribute so tests can swap in a recording
# mock and prove which address (if any) was dialled.
_new_inner_backend = httpcore.AnyIOBackend


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    """Resolve + check at connect time, then dial the checked IP (pinning)."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner = inner or _new_inner_backend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        addrs = await resolve_public(host.strip("[]"), port)
        last: Exception | None = None
        for addr in addrs:
            try:
                return await self._inner.connect_tcp(
                    addr,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        assert last is not None
        raise last

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        raise OutboundURLRefused("unix sockets are not an allowed outbound target")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class GuardedAsyncTransport(httpx.AsyncHTTPTransport):
    """httpx transport that only dials public addresses, re-checked per connect."""

    def __init__(self, *, network_backend: httpcore.AsyncNetworkBackend | None = None) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            http1=True,
            http2=False,
            network_backend=_GuardedBackend(network_backend),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme != "https":
            raise OutboundURLRefused("URL must use https")
        check_url_static(str(request.url))
        return await super().handle_async_request(request)


# Same shape as the openai SDK default (connect 5s, else 600s) for LLM streams.
LLM_CLIENT_TIMEOUT = httpx.Timeout(600.0, connect=5.0)


def guarded_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` for tenant-chosen URLs: guarded, no redirects,
    no environment proxies. Extra kwargs (``timeout``...) pass through."""
    kwargs.pop("follow_redirects", None)
    kwargs.pop("trust_env", None)
    return httpx.AsyncClient(
        transport=GuardedAsyncTransport(),
        follow_redirects=False,
        trust_env=False,
        **kwargs,
    )
