"""SSRF guard for outbound-webhook target URLs.

A tenant admin supplies ``target_url`` freely; without a destination check they
could point a subscription at loopback / RFC1918 / link-local (including the
AWS IMDS at 169.254.169.254) and the delivery loop would POST signed
invoice/payment payloads there. This module is the single shared validator,
enforced at BOTH boundaries:

* subscription create/update (``api/webhooks.py``) — reject early with a clean,
  non-enumerating 422, and
* immediately before every dispatch (``services/webhooks/delivery.py``) — the
  stored host is RE-resolved at send time, so a DNS record that flipped to a
  private address after create (TOCTOU / DNS rebinding) is refused and the
  delivery is marked failed.

Every address the host resolves to (A and AAAA) must be publicly routable; a
literal-IP host is judged the same way, and IPv4-mapped IPv6
(``::ffff:10.0.0.1``) is unwrapped and judged as its embedded IPv4 address.
Fail-closed: a host that does not resolve is rejected too.

The send itself is pinned. ``httpx`` would otherwise perform its OWN lookup
when it opens the socket, leaving a rebinding window between this check and
the connect. ``delivery._post`` instead uses ``url_safety.pinned_async_client``
with :func:`resolve_pinned_addresses` as its resolver: the host is resolved
once per connection, every address is judged by this module's rule, and the
socket is opened to a vetted IP literal (``Host`` / SNI / certificate check
still name the hostname). See ``backend/docs/public-api.md`` § Outbound
webhooks.

Dev escape hatch: ``FEOH_WEBHOOKS_ALLOW_PRIVATE_TARGETS=true`` skips only the
address checks (URL scheme/host shape is still enforced) so the delivery path
can be exercised locally against 127.0.0.1. Defaults to ``false`` (blocking);
never enable it in a deployed env.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from app.config import settings

# One generic message for EVERY rejection path (bad scheme, missing host, DNS
# failure, non-public address) — the response must not disclose which internal
# range or host the probe hit (non-enumerating).
REJECT_DETAIL = "target_url must be a publicly routable http(s) URL"


class WebhookTargetNotAllowed(ValueError):
    """The webhook target URL is not a publicly routable http(s) endpoint."""


def _is_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when ``addr`` must not be a webhook destination.

    ``is_global`` is ``False`` for loopback, RFC1918 private, link-local
    (169.254/16 — incl. the AWS/cloud metadata endpoint), CGNAT 100.64/10,
    unique-local ``fc00::/7``, unspecified, multicast, and reserved ranges —
    exactly the set the guard blocks, fail-closed for anything else
    non-routable. Multicast is blocked explicitly on top — the stdlib reports
    multicast ranges (``224/4``, ``ff00::/8``) as "global" because they're not
    in the IANA special-purpose registries, but a multicast group is never a
    legitimate webhook receiver. IPv4-mapped IPv6 is unwrapped first so
    ``::ffff:10.0.0.1`` is judged as ``10.0.0.1``.
    """
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return not addr.is_global or addr.is_multicast


async def _resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address (A + AAAA) ``host`` currently resolves to.

    Module-level seam so tests can monkeypatch resolution deterministically.
    Raises ``OSError`` (``socket.gaierror``) on resolution failure.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        raw = info[4][0]
        if raw in seen:
            continue
        seen.add(raw)
        addresses.append(ipaddress.ip_address(raw))
    return addresses


async def ensure_public_webhook_target(url: str) -> None:
    """Validate ``url`` as an outbound-webhook target or raise.

    Raises ``WebhookTargetNotAllowed`` (always with the generic
    ``REJECT_DETAIL`` message) when the URL is not http(s), has no host, does
    not resolve, or resolves to ANY non-public address.
    """
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(url.strip())
        host = parsed.hostname  # may raise ValueError on a malformed netloc
    except ValueError as exc:
        raise WebhookTargetNotAllowed(REJECT_DETAIL) from exc
    if parsed.scheme not in ("http", "https") or not host:
        raise WebhookTargetNotAllowed(REJECT_DETAIL)

    if settings.webhooks_allow_private_targets:
        # Explicit local-dev escape hatch (default false): only the address
        # checks are skipped — scheme/host shape is still enforced above.
        return

    await _vetted_addresses(host)


async def _vetted_addresses(
    host: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """``host``'s addresses, or ``WebhookTargetNotAllowed`` unless all are public."""
    try:
        addresses = [ipaddress.ip_address(host)]  # literal-IP host
    except ValueError:
        try:
            addresses = await _resolve_host(host)
        except (OSError, UnicodeError) as exc:
            # Resolution failure → fail closed: we can't prove it's public.
            raise WebhookTargetNotAllowed(REJECT_DETAIL) from exc

    if not addresses or any(_is_blocked(a) for a in addresses):
        raise WebhookTargetNotAllowed(REJECT_DETAIL)
    return addresses


async def resolve_pinned_addresses(host: str, port: int) -> list[str]:
    """The pinned-client resolver for a delivery: this module's rule, at connect.

    Handed to ``url_safety.pinned_async_client`` by ``delivery._post``. The
    address the socket opens to is one of the addresses returned here, so a
    record that flips between ``ensure_public_webhook_target`` and the connect
    can no longer reach an internal host. Under the dev escape hatch the
    addresses are not judged, but the connection is still pinned to them.
    """
    if settings.webhooks_allow_private_targets:
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                addresses = await _resolve_host(host)
            except (OSError, UnicodeError) as exc:
                raise WebhookTargetNotAllowed(REJECT_DETAIL) from exc
        if not addresses:
            raise WebhookTargetNotAllowed(REJECT_DETAIL)
    else:
        addresses = await _vetted_addresses(host)
    return [str(a) for a in addresses]
