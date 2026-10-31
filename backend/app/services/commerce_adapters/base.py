"""The commerce / inventory platform adapter interface (decisions §273).

FeohLedger records what was ordered, received, billed and paid; the business's
commerce platform (Shopify, Lightspeed, Square, Cin7, WooCommerce) records what
is on the shelf (decisions §260). A goods receipt is where the two meet: each
received line adds its quantity to the platform's *available* stock at the
mapped location, and cancelling the receipt takes it back off. That one push is
the whole surface — no stock levels, valuation or sales are read back into the
AP side.

An adapter does four things, all async and all against the provider's API:

* :meth:`CommerceAdapter.test_connection` — the credentials work;
* :meth:`CommerceAdapter.list_locations` — the stock locations an admin maps to;
* :meth:`CommerceAdapter.resolve_sku` — our SKU → the provider's item id;
* :meth:`CommerceAdapter.adjust_available` — add a signed delta, idempotently.

Failures are typed (:class:`CommerceError` and its subclasses): the sync service
reads ``code`` and ``retryable`` and never the message's wording, and the
message itself is stored on the sync row and shown to an admin, so it must
carry no credential, URL query or response body (:func:`http_failure`).

A provider declares itself with a :class:`CommerceProviderSpec` — key, label,
auth kind, form fields, which fields say where credentials are sent — and
registers with ``registry.register_commerce_adapter``. See
``backend/docs/commerce-integration.md`` § How to add a provider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

import httpx

from app.config import settings

#: How long one provider call may take. A post-commit push and the retry sweep
#: both run off the request path, but a hung socket still ties up a task.
DEFAULT_TIMEOUT_SECONDS = 15.0

#: The longest message a sync row keeps. Provider messages are paraphrased by
#: the adapter, never copied from a response body.
MAX_ERROR_MESSAGE = 300

AuthKind = Literal["credentials", "oauth"]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class CommerceError(Exception):
    """A provider call that did not do what was asked.

    ``code`` is a stable machine reason (stored on the sync row, localized by
    the web client); ``retryable`` says whether the same call may succeed later
    unchanged. The message is admin-visible: name the step and the cause, never
    a credential, a URL with its query, or the provider's response body.
    """

    code: ClassVar[str] = "provider_error"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message[:MAX_ERROR_MESSAGE])
        if code is not None:
            # An instance-level override (e.g. `fractional_quantity` on a
            # rejection) keeps the class's retryability.
            self.code = code  # type: ignore[misc]


class CommerceConfigError(CommerceError):
    """The stored configuration is incomplete or invalid (missing field, bad host)."""

    code = "config_invalid"


class CommerceAuthError(CommerceError):
    """The provider refused the credentials (401 / 403). Re-enter them; no retry."""

    code = "auth_failed"


class CommerceUnmappedSkuError(CommerceError):
    """The provider has no item for this SKU. Recorded as a skip, not a failure."""

    code = "unmapped_sku"


class CommerceUnknownLocationError(CommerceError):
    """The mapped location does not exist (or is inactive) at the provider."""

    code = "unknown_location"


class CommerceRejectedError(CommerceError):
    """The provider understood the call and refused it (a 4xx other than auth)."""

    code = "rejected"


class CommerceTransientError(CommerceError):
    """Timeout, connection failure, 5xx — the same call may succeed later."""

    code = "transient"
    retryable = True


class CommerceRateLimitedError(CommerceTransientError):
    """429 — retry later."""

    code = "rate_limited"


#: The ``code`` a non-integral delta is refused with by a provider that counts
#: whole units only (``CommerceProviderSpec.supports_fractional_quantities``).
FRACTIONAL_QUANTITY = "fractional_quantity"


def http_failure(step: str, response: httpx.Response) -> CommerceError:
    """The typed error for a non-2xx provider response.

    Maps by status alone and names only the step and the status code: provider
    error bodies can echo the request (SKUs, store names, sometimes tokens), so
    the body is never copied into the message.
    """
    status = response.status_code
    message = f"{step} failed (HTTP {status})"
    if status in (401, 403):
        return CommerceAuthError(message)
    if status == 429:
        return CommerceRateLimitedError(message)
    if status in (408, 425) or status >= 500:
        return CommerceTransientError(message)
    return CommerceRejectedError(message)


def transport_failure(step: str, exc: httpx.HTTPError) -> CommerceTransientError:
    """The typed error for a request that never got a response.

    The exception CLASS only: an httpx error's text carries the request URL,
    which for some providers carries a credential in its query.
    """
    return CommerceTransientError(f"{step} failed ({type(exc).__name__})")


def require_integral(delta: Decimal) -> int:
    """``delta`` as a whole number, or a non-retryable refusal.

    For providers that count whole units: a receipt line of ``2.5`` must not be
    rounded into the shelf count, and silently truncating it would leave the
    platform half a unit adrift with no record of why.
    """
    if delta != delta.to_integral_value():
        raise CommerceRejectedError(
            "This platform counts whole units only; the received quantity has a fraction.",
            code=FRACTIONAL_QUANTITY,
        )
    return int(delta)


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CommerceLocation:
    """A stock location at the provider (a store, a warehouse)."""

    id: str
    name: str
    active: bool = True


@dataclass(frozen=True)
class CommerceItemRef:
    """What a SKU resolved to: the provider's id for the stocked item.

    ``item_id`` is whatever :meth:`CommerceAdapter.adjust_available` takes —
    Shopify's ``InventoryItem`` gid, Square's catalog object id, and so on.
    """

    item_id: str
    sku: str
    title: str | None = None


@dataclass(frozen=True)
class InventoryAdjustment:
    """One signed change to an item's available quantity at a location.

    ``delta`` is never zero: positive adds received stock, negative takes back a
    cancelled receipt. ``idempotency_key`` is stable per receipt line and
    direction — the adapter must make a repeated call with the same key change
    the provider's stock at most once (a native idempotency key where the API
    has one, otherwise the provider's own reference/note field checked before
    writing).
    """

    item_id: str
    location_id: str
    delta: Decimal
    idempotency_key: str
    sku: str | None = None
    #: ``received`` or ``receipt_cancelled`` — for the provider's reason / note.
    reason: str = "received"
    #: Which push of this key this is, 1-based (the sync row's ``attempts`` after
    #: its claim). Above 1, an earlier push may have reached the provider without
    #: our hearing back (a timeout after the write), so an adapter whose
    #: idempotency cannot be relied on forever — a native key the provider
    #: forgets — checks for the earlier write before writing again.
    attempt: int = 1


@dataclass(frozen=True)
class InventoryAdjustmentResult:
    """What the provider recorded. ``provider_reference`` is non-empty."""

    provider_reference: str
    #: The available quantity after the change, when the API returns it.
    available_after: Decimal | None = None
    #: The provider recognised the idempotency key and applied nothing new.
    replayed: bool = False


@dataclass(frozen=True)
class CommerceProviderSpec:
    """A provider's catalogue entry, declared beside its adapter.

    ``fields`` are built with ``commerce_adapters.catalog.field`` (name, label
    key, secret, required, options). ``destination_keys`` names every field
    that says WHERE the credentials go (a shop domain, a store URL, an account
    id that becomes a hostname, an environment that picks the API host): a save
    that changes one drops the sealed secrets (decisions §271).
    """

    key: str
    label: str
    auth: AuthKind
    fields: tuple[dict[str, Any], ...]
    docs_url: str
    regions: tuple[str, ...] = ()
    destination_keys: frozenset[str] = frozenset()
    #: Whether the platform holds fractional stock (Square does; Shopify does not).
    supports_fractional_quantities: bool = False
    #: Shown in the dropdown but not selectable until the adapter is real.
    selectable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "auth": self.auth,
            "regions": list(self.regions),
            "fields": [dict(f) for f in self.fields],
            "docs_url": self.docs_url,
            "supports_fractional_quantities": self.supports_fractional_quantities,
        }


# --------------------------------------------------------------------------- #
# The adapter
# --------------------------------------------------------------------------- #

#: Tests swap the HTTP transport (an in-process fake-commerce app) through
#: :func:`set_transport_override`; production leaves it ``None``.
_TRANSPORT_OVERRIDE: httpx.AsyncBaseTransport | None = None


def set_transport_override(transport: httpx.AsyncBaseTransport | None) -> None:
    """Route every adapter's HTTP through ``transport`` (tests only)."""
    global _TRANSPORT_OVERRIDE
    _TRANSPORT_OVERRIDE = transport


class CommerceAdapter(ABC):
    """One commerce platform. Built from ``settings.commerce`` with secrets decrypted."""

    spec: ClassVar[CommerceProviderSpec]

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config or {})

    @property
    def key(self) -> str:
        return self.spec.key

    # -- the interface ---------------------------------------------------- #

    @abstractmethod
    async def test_connection(self) -> None:
        """Return when the credentials work; raise a :class:`CommerceError` when not."""

    @abstractmethod
    async def list_locations(self) -> list[CommerceLocation]:
        """Every stock location the credentials can see."""

    @abstractmethod
    async def resolve_sku(self, sku: str) -> CommerceItemRef:
        """The provider's stocked item for ``sku``.

        Raises :class:`CommerceUnmappedSkuError` when there is none, and when
        there is more than one: picking one of two items that share a SKU would
        put stock on a shelf nobody chose.
        """

    @abstractmethod
    async def adjust_available(self, adjustment: InventoryAdjustment) -> InventoryAdjustmentResult:
        """Add ``adjustment.delta`` (signed) to the available quantity, idempotently.

        Raise :class:`CommerceUnknownLocationError` for a location the provider
        does not know, :class:`CommerceRejectedError` with code
        :data:`FRACTIONAL_QUANTITY` for a fraction the platform cannot hold
        (:func:`require_integral`), and the transport / HTTP errors through
        :func:`transport_failure` / :func:`http_failure`.
        """

    # -- helpers for providers -------------------------------------------- #

    def require(self, name: str) -> str:
        """A required config value, or :class:`CommerceConfigError` naming the field."""
        value = self.config.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise CommerceConfigError(f"{self.spec.label} setting '{name}' is missing.")
        return str(value).strip()

    def http_client(self, **kwargs: Any) -> httpx.AsyncClient:
        """An ``httpx.AsyncClient`` with the family's timeout (and the test transport).

        Outside the operator override, the client is PINNED
        (``url_safety.pinned_async_client``): a provider's host is often built
        from tenant config (a shop domain, a store URL), and :meth:`api_base`'s
        check alone would let a rebinding DNS record answer it with a public
        address and httpx's own lookup with an internal one. A refusal at
        connect time raises :class:`CommerceConfigError`, as the check does.
        """
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT_SECONDS)
        if _TRANSPORT_OVERRIDE is not None:
            kwargs["transport"] = _TRANSPORT_OVERRIDE
            return httpx.AsyncClient(**kwargs)
        if settings.commerce_api_base_override.strip():
            # Operator-controlled (local dev + e2e; a deployed env refuses to
            # boot with it set): fake-commerce on localhost, so not pinned.
            return httpx.AsyncClient(**kwargs)
        from app.utils.url_safety import (
            UnsafeUrlError,
            pinned_async_client,
            resolve_public_addresses,
        )

        label = self.spec.label

        async def resolve(host: str, port: int) -> list[str]:
            try:
                return await resolve_public_addresses(host, port)
            except UnsafeUrlError:
                raise CommerceConfigError(
                    f"{label} address does not resolve to a public host."
                ) from None

        return pinned_async_client(resolve=resolve, **kwargs)

    async def api_base(self, default: str) -> str:
        """The API root to call: the operator override, else ``default``.

        ``FEOH_COMMERCE_API_BASE_OVERRIDE`` (local dev + e2e only; a deployed
        environment refuses to boot with it set) points every provider at
        fake-commerce, each under its own key: ``<override>/<provider key>``.
        Otherwise ``default`` — built from tenant config (a shop domain, a
        store URL) — must be https and resolve to a public address, because an
        admin-supplied host is a server-side request target (SSRF).
        """
        override = settings.commerce_api_base_override.strip().rstrip("/")
        if override:
            return f"{override}/{self.spec.key}"
        base = default.rstrip("/")
        if urlsplit(base).scheme.lower() != "https":
            raise CommerceConfigError(f"{self.spec.label} must be reached over https.")
        from app.utils.url_safety import UnsafeUrlError, assert_public_url_async

        try:
            await assert_public_url_async(base)
        except UnsafeUrlError:
            raise CommerceConfigError(
                f"{self.spec.label} address does not resolve to a public host."
            ) from None
        return base


@dataclass
class ConnectionReport:
    """``POST /api/commerce/test``'s answer (no credential, no response body)."""

    success: bool
    code: str | None = None
    message: str = ""
    locations: list[CommerceLocation] = field(default_factory=list)
