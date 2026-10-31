"""SSRF guard for server-side fetches of caller / admin-supplied URLs.

Several surfaces let an org admin store a URL that the backend then fetches
server-side: the white-label `logo_url` (embedded into generated PDFs — a fetch
ANY user can trigger via a PDF export), the Slack / Teams chat-notification
`webhook_url` (posted on every approval event), and the ERP / enrichment
adapter `base_url`. Without a guard, an admin could point any of these at an
internal address — most dangerously the cloud metadata endpoint
`169.254.169.254` — and read back instance credentials or probe the internal
network (SSRF).

`assert_public_url` resolves the host and rejects any address that isn't
publicly routable (private / loopback / link-local / reserved / multicast /
unspecified), in both IPv4 and IPv6. It rejects if ANY resolved address is
unsafe, which also defeats a DNS record that mixes a public and an internal IP.
A hostname that doesn't resolve is left alone — the connection fails naturally
at request time, and rejecting on a transient DNS miss would be its own bug.

**Two entry points, one policy.** `socket.getaddrinfo` is a *blocking* call: on
an event loop it stalls every other request for the duration of the lookup (a
DNS timeout is seconds, not milliseconds), and this guard sits in front of the
Slack/Teams approval post, the enrichment adapters and the D365 ERP adapter —
all `async def`. `assert_public_url_async` / `is_public_url_async` are the
awaitable twins for those call sites; they resolve through
``loop.getaddrinfo`` (the same thing ``services/webhooks/url_guard`` does) and
share every policy decision with the sync pair via `_evaluate` — the sync and
async forms cannot drift into two different answers. Sync callers (the PDF
logo embed, which already runs in a worker thread) keep using the sync pair.

**A check alone does not stop DNS rebinding; the pinned clients do.** The
guards above resolve the host, and then ``httpx`` resolves it *again* when it
opens the socket. A record with a short TTL can answer the guard with a public
address and the connection with ``127.0.0.1`` / ``169.254.169.254``, so the
check passes and the request still lands inside. :func:`pinned_async_client`
(and the blocking :func:`pinned_client`) close that window: their transport
resolves the host ONCE per connection, judges every address it got back by the
same policy, and opens the TCP connection to one of those vetted IP literals,
so there is no second lookup left to rebind. The request URL is untouched, so
the ``Host`` header, TLS SNI and certificate verification all still name the
hostname (httpcore passes ``server_hostname`` from the URL to ``start_tls``,
independent of the address ``connect_tcp`` dialled). Use one at every site that
sends a request to an admin- or customer-supplied host; keep the up-front
``assert_public_url_async`` where a caller wants a clean config error before
any I/O.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx


class UnsafeUrlError(ValueError):
    """Raised when a URL is not an http(s) URL to a publicly routable host."""


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # `is_private` already covers RFC1918 + ULA (fc00::/7); the rest catch the
    # cloud metadata link-local (169.254.169.254 / fe80::), loopback, reserved,
    # multicast, and 0.0.0.0/::.
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return True
    # IPv4-mapped IPv6 (e.g. ::ffff:169.254.169.254) — unwrap and re-check.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and _is_unsafe_ip(mapped):
        return True
    return False


def _parse_target(url: str) -> tuple[str, int] | None:
    """Validate scheme/host and judge a literal-IP host without any DNS.

    Returns ``(host, port)`` when a name lookup is still needed, or ``None``
    when the URL is already settled (literal IP that passed). Raises
    ``UnsafeUrlError`` on a bad scheme / missing host / unsafe literal IP.
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError("URL scheme must be http or https")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL has no host")

    # A literal IP in the URL is checked directly (no DNS).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_unsafe_ip(literal):
            raise UnsafeUrlError("URL points at a non-public address")
        return None

    return host, parsed.port or (443 if parsed.scheme == "https" else 80)


def _assert_addresses_public(infos) -> None:
    """Reject if ANY address in a ``getaddrinfo`` result is non-public."""
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_unsafe_ip(ip):
            raise UnsafeUrlError("URL resolves to a non-public address")


def assert_public_url(url: str) -> None:
    """Raise ``UnsafeUrlError`` unless ``url`` is an http(s) URL whose host
    resolves only to publicly routable addresses.

    Blocking — never call this from a coroutine; use
    :func:`assert_public_url_async` there.
    """
    target = _parse_target(url)
    if target is None:
        return
    host, port = target
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Unresolvable — let the connection fail naturally rather than reject a
        # legitimate host that's momentarily unresolvable.
        return
    _assert_addresses_public(infos)


async def assert_public_url_async(url: str) -> None:
    """Awaitable :func:`assert_public_url` — identical policy, non-blocking DNS.

    The name resolution goes through ``loop.getaddrinfo``, so a slow or timing-out
    lookup suspends only this coroutine instead of stalling the whole worker.
    """
    target = _parse_target(url)
    if target is None:
        return
    host, port = target
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        # Same fail-open-on-DNS-miss rule as the sync form (see above).
        return
    _assert_addresses_public(infos)


def is_public_url(url: str | None) -> bool:
    """Boolean form of :func:`assert_public_url` — ``False`` for None / empty /
    non-http(s) / internal-resolving URLs. Use at best-effort fetch sites that
    fail soft (return None / skip) rather than raising.

    Blocking — see :func:`is_public_url_async` for coroutine call sites.
    """
    if not url:
        return False
    try:
        assert_public_url(url)
        return True
    except UnsafeUrlError:
        return False


async def is_public_url_async(url: str | None) -> bool:
    """Awaitable :func:`is_public_url` — identical verdict, non-blocking DNS."""
    if not url:
        return False
    try:
        await assert_public_url_async(url)
        return True
    except UnsafeUrlError:
        return False


# ---------------------------------------------------------------------------
# Pinned transports: resolve once, vet every address, dial the vetted IP
# ---------------------------------------------------------------------------

#: ``(host, port) -> [ip, ...]``: resolves AND vets, raising to refuse. The
#: exception propagates out of the ``httpx`` call unchanged (httpx only remaps
#: httpcore's own exceptions), so a call site can raise its own error type.
AsyncResolver = Callable[[str, int], Awaitable[list[str]]]
SyncResolver = Callable[[str, int], list[str]]

_DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


def _unique_addresses(infos) -> list[str]:
    """The distinct addresses of a ``getaddrinfo`` result, in resolver order."""
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen


async def _lookup_async(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to: the pinned path's ONE lookup.

    Module-level seam so tests can script the answers (a rebinding record).
    Raises ``OSError`` / ``UnicodeError`` on resolution failure.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return _unique_addresses(infos)


def _lookup_sync(host: str, port: int) -> list[str]:
    """Blocking twin of :func:`_lookup_async` (worker-thread callers only)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return _unique_addresses(infos)


def _vet(host: str, addresses: list[str], *, allow_private: bool) -> list[str]:
    """Apply the guard's policy to a resolved address list.

    Every address must pass, not only the one dialled: the same
    any-unsafe-rejects rule as :func:`_assert_addresses_public`, so a record
    mixing a public and an internal IP is refused rather than raced.
    """
    if not addresses:
        raise httpcore.ConnectError("host did not resolve to any address")
    if not allow_private:
        for addr in addresses:
            try:
                # A scoped IPv6 literal (fe80::1%en0) carries its zone after '%'.
                ip = ipaddress.ip_address(addr.split("%", 1)[0])
            except ValueError:
                raise UnsafeUrlError("URL resolves to an unparseable address") from None
            if _is_unsafe_ip(ip):
                raise UnsafeUrlError("URL resolves to a non-public address")
    return addresses


def _literal(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


async def resolve_public_addresses(
    host: str, port: int, *, allow_private: bool = False
) -> list[str]:
    """The vetted addresses to dial for ``host``: the default pinned resolver.

    A literal-IP host is judged without DNS. A host that does not resolve
    raises ``httpcore.ConnectError`` (surfacing as ``httpx.ConnectError``): the
    connection fails naturally, which is what the unpinned guard's
    fail-open-on-DNS-miss rule always amounted to. Any non-public address
    raises :class:`UnsafeUrlError`. ``allow_private=True`` skips the address
    judgement only (the connection is still pinned to what was resolved), for a
    call site that legitimately reaches loopback outside a deployed environment
    (the local Keycloak IdP).
    """
    literal = _literal(host)
    if literal is not None:
        return _vet(host, [literal], allow_private=allow_private)
    try:
        addresses = await _lookup_async(host, port)
    except (OSError, UnicodeError) as exc:
        raise httpcore.ConnectError("could not resolve host") from exc
    return _vet(host, addresses, allow_private=allow_private)


def resolve_public_addresses_sync(
    host: str, port: int, *, allow_private: bool = False
) -> list[str]:
    """Blocking twin of :func:`resolve_public_addresses`, identical policy."""
    literal = _literal(host)
    if literal is not None:
        return _vet(host, [literal], allow_private=allow_private)
    try:
        addresses = _lookup_sync(host, port)
    except (OSError, UnicodeError) as exc:
        raise httpcore.ConnectError("could not resolve host") from exc
    return _vet(host, addresses, allow_private=allow_private)


class PinnedAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """Dials only addresses its resolver vetted, never a hostname.

    ``connect_tcp`` is the one place httpcore turns a host into a socket, and it
    runs once per new connection (a redirect to another host opens a new one,
    so it is vetted too). The vetted addresses are tried in resolver order;
    each was judged, so falling back between them is safe. TLS is not this
    class's concern: httpcore calls ``start_tls`` on the returned stream with
    ``server_hostname`` taken from the request URL.
    """

    def __init__(
        self, resolve: AsyncResolver, inner: httpcore.AsyncNetworkBackend | None = None
    ) -> None:
        self._resolve = resolve
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._resolve(host, port)
        last: Exception = httpcore.ConnectError("host did not resolve to any address")
        for addr in addresses:
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
        raise last

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not reachable through a pinned client")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Blocking twin of :class:`PinnedAsyncNetworkBackend`."""

    def __init__(self, resolve: SyncResolver, inner: httpcore.NetworkBackend | None = None):
        self._resolve = resolve
        self._inner = inner or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        addresses = self._resolve(host, port)
        last: Exception = httpcore.ConnectError("host did not resolve to any address")
        for addr in addresses:
            try:
                return self._inner.connect_tcp(
                    addr,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise last

    def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("unix sockets are not reachable through a pinned client")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class PinnedAsyncTransport(httpx.AsyncHTTPTransport):
    """An ``AsyncHTTPTransport`` whose pool dials through the pinned backend.

    ``AsyncHTTPTransport`` keeps all of its state in ``self._pool``, so this
    builds that pool directly, with the ``network_backend`` the parent's
    constructor does not expose, instead of building and discarding one. No
    proxy and no unix socket: a proxy would do the resolution itself, and the
    pin would mean nothing.
    """

    def __init__(
        self,
        resolve: AsyncResolver,
        *,
        verify: Any = True,
        http2: bool = False,
        limits: httpx.Limits = _DEFAULT_LIMITS,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=http2,
            network_backend=PinnedAsyncNetworkBackend(resolve, backend),
        )


class PinnedTransport(httpx.HTTPTransport):
    """Blocking twin of :class:`PinnedAsyncTransport`."""

    def __init__(
        self,
        resolve: SyncResolver,
        *,
        verify: Any = True,
        limits: httpx.Limits = _DEFAULT_LIMITS,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=verify),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            network_backend=PinnedNetworkBackend(resolve, backend),
        )


#: Each would route around the pinned transport: a proxy (explicit, or from the
#: environment via ``trust_env``) resolves the host itself, and a custom
#: transport or mount replaces the pinned one outright.
_REFUSED_CLIENT_KWARGS = ("transport", "mounts", "proxy", "trust_env")


def _check_client_kwargs(kwargs: dict[str, Any]) -> None:
    clash = [k for k in _REFUSED_CLIENT_KWARGS if k in kwargs]
    if clash:
        raise TypeError(f"a pinned client does not accept {', '.join(clash)}")


def pinned_async_client(
    *,
    resolve: AsyncResolver | None = None,
    allow_private: bool = False,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that cannot be DNS-rebound past the SSRF guard.

    The one shared factory for requests to an admin- or customer-supplied host.
    ``resolve`` swaps the address policy (outbound webhooks pass their stricter
    ``url_guard`` rule); by default it is :func:`resolve_public_addresses` with
    ``allow_private`` passed through. Every other keyword goes to
    ``httpx.AsyncClient``, except ``verify``, ``http2`` and ``limits``, which
    belong to the transport. ``trust_env`` is forced off so an ``HTTPS_PROXY``
    in the environment cannot take over resolution.
    """
    _check_client_kwargs(kwargs)
    if resolve is None:

        async def resolve(host: str, port: int) -> list[str]:
            return await resolve_public_addresses(host, port, allow_private=allow_private)

    transport = PinnedAsyncTransport(
        resolve,
        verify=kwargs.pop("verify", True),
        http2=kwargs.pop("http2", False),
        limits=kwargs.pop("limits", _DEFAULT_LIMITS),
    )
    return httpx.AsyncClient(transport=transport, trust_env=False, **kwargs)


def pinned_client(
    *,
    resolve: SyncResolver | None = None,
    allow_private: bool = False,
    **kwargs: Any,
) -> httpx.Client:
    """Blocking twin of :func:`pinned_async_client`, for worker-thread callers
    only (the PDF logo embed): its lookup and connect block."""
    _check_client_kwargs(kwargs)
    if resolve is None:

        def resolve(host: str, port: int) -> list[str]:
            return resolve_public_addresses_sync(host, port, allow_private=allow_private)

    transport = PinnedTransport(
        resolve,
        verify=kwargs.pop("verify", True),
        limits=kwargs.pop("limits", _DEFAULT_LIMITS),
    )
    return httpx.Client(transport=transport, trust_env=False, **kwargs)
