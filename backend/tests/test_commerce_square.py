"""The Square commerce adapter (roadmap Priority 14 item 3; decisions §268).

Held to the shared contract against tools/fake-commerce's ``square`` module,
in process, plus what is Square's own: the pinned ``Square-Version``, the
environment → host choice, exact decimal quantities, the ``IN_STOCK → NONE``
reversal, Square's ``errors[].category`` mapping, and the replay of a key whose
body changed (``IDEMPOTENCY_KEY_REUSED``) by its ``reference_id``.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from app.config import settings
from app.services.commerce_adapters import (
    CommerceAuthError,
    CommerceConfigError,
    CommerceRateLimitedError,
    CommerceRejectedError,
    CommerceTransientError,
    CommerceUnknownLocationError,
    CommerceUnmappedSkuError,
    InventoryAdjustment,
    catalog,
    square,
)
from app.services.commerce_adapters import base as commerce_base
from app.services.commerce_adapters.registry import adapter_class
from tests.commerce_contract import (
    ContractCase,
    assert_commerce_contract,
    assert_maps_http_errors,
    use_fake_commerce,
)

TOKEN = "fake-square-token"
CONFIG = {"provider": "square", "environment": "sandbox", "access_token": TOKEN}
BAD = {**CONFIG, "access_token": "wrong-square-token"}


@pytest.fixture
def fake(monkeypatch):
    with use_fake_commerce(monkeypatch) as state:
        yield state


def _adapter(config=None):
    return adapter_class("square")(dict(config or CONFIG))


async def _available(fake, item_id: str, location_id: str) -> Decimal:
    levels = fake.provider_state("square")["levels"]
    return Decimal(levels.get(f"{item_id}@{location_id}", "0"))


def _adj(delta: str, key: str = "feoh:line-1:receive", **kw) -> InventoryAdjustment:
    return InventoryAdjustment(
        item_id=kw.pop("item_id", "VAR-TEE-M"),
        location_id=kw.pop("location_id", "L-MAIN"),
        delta=Decimal(delta),
        idempotency_key=key,
        sku="SQ-TEE-M",
        **kw,
    )


async def _arm(fake, status: int) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake.app)) as client:
        resp = await client.post("http://fake-commerce/square/__fail-next", json={"status": status})
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


async def test_square_honours_the_contract(fake):
    async def read(item_id, location_id):
        return await _available(fake, item_id, location_id)

    await assert_commerce_contract(
        ContractCase(
            provider="square",
            config=CONFIG,
            bad_auth_config=BAD,
            known_sku="SQ-TEE-M",
            unmapped_sku="NO-SUCH-SKU",
            location_id="L-MAIN",
            unknown_location_id="L-NOWHERE",
            read_available=read,
        )
    )


# --------------------------------------------------------------------------- #
# Catalogue entry
# --------------------------------------------------------------------------- #


def test_spec_is_credentials_with_a_secret_token_and_environment_as_destination():
    spec = square.SPEC
    assert spec.auth == "credentials"
    assert spec.supports_fractional_quantities is True
    fields = {f["name"]: f for f in spec.fields}
    assert fields["access_token"]["secret"] is True
    # Sandbox first: the form's default must never be the live host.
    assert fields["environment"]["options"] == ["sandbox", "production"]
    assert "environment" in spec.destination_keys
    assert "environment" in catalog.destination_keys()
    assert set(fields) <= catalog.KNOWN_FIELD_LABELS


def test_switching_environment_drops_the_stored_token():
    stored = {**CONFIG, "environment": "sandbox", "access_token": "stored-token"}
    merged = catalog.merge_commerce_update(
        stored, {"provider": "square", "environment": "production", "access_token": ""}
    )
    assert not merged.get("access_token")


# --------------------------------------------------------------------------- #
# Requests as Square sees them
# --------------------------------------------------------------------------- #


class _Recorder(httpx.AsyncBaseTransport):
    """Wraps the fake's transport and keeps every request."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)


@pytest.fixture
def recorded(fake):
    recorder = _Recorder(commerce_base._TRANSPORT_OVERRIDE)
    commerce_base.set_transport_override(recorder)
    yield recorder


async def test_every_call_pins_the_square_version_and_bearer_token(recorded):
    adapter = _adapter()
    await adapter.test_connection()
    await adapter.list_locations()
    await adapter.resolve_sku("SQ-TEE-M")
    await adapter.adjust_available(_adj("1"))
    assert len(recorded.requests) == 4
    for req in recorded.requests:
        assert req.headers["Square-Version"] == square.SQUARE_VERSION
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert [r.url.path for r in recorded.requests] == [
        "/square/v2/merchants/me",
        "/square/v2/locations",
        "/square/v2/catalog/search",
        "/square/v2/inventory/changes/batch-create",
    ]


async def test_receive_is_none_to_in_stock_with_our_key_and_reference(recorded):
    await _adapter().adjust_available(_adj("4", key="feoh:abc:receive"))
    body = json.loads(recorded.requests[-1].content)
    assert body["idempotency_key"] == "feoh:abc:receive"
    adj = body["changes"][0]["adjustment"]
    assert (adj["from_state"], adj["to_state"]) == ("NONE", "IN_STOCK")
    assert adj["reference_id"] == "feoh:abc:receive"
    assert adj["from_location_id"] == adj["to_location_id"] == "L-MAIN"
    assert adj["quantity"] == "4"
    assert adj["occurred_at"].endswith("Z")


async def test_reversal_is_in_stock_to_none_with_a_positive_quantity(fake, recorded):
    adapter = _adapter()
    await adapter.adjust_available(_adj("5", key="k:receive"))
    result = await adapter.adjust_available(_adj("-2", key="k:reverse", reason="receipt_cancelled"))
    adj = json.loads(recorded.requests[-1].content)["changes"][0]["adjustment"]
    assert (adj["from_state"], adj["to_state"]) == ("IN_STOCK", "NONE")
    assert adj["quantity"] == "2"
    assert result.available_after == Decimal("3")


async def test_fractions_go_over_exactly_and_are_never_rounded(fake, recorded):
    result = await _adapter().adjust_available(_adj("2.5000"))
    assert (
        json.loads(recorded.requests[-1].content)["changes"][0]["adjustment"]["quantity"] == "2.5"
    )
    assert result.available_after == Decimal("2.5")
    assert await _available(fake, "VAR-TEE-M", "L-MAIN") == Decimal("2.5")


def test_quantity_formatting():
    assert square.square_quantity(Decimal("3")) == "3"
    assert square.square_quantity(Decimal("10")) == "10"
    assert square.square_quantity(Decimal("-0.12345")) == "0.12345"
    assert square.square_quantity(Decimal("1.2500")) == "1.25"
    with pytest.raises(CommerceRejectedError):
        square.square_quantity(Decimal("0.123456"))


def test_an_overlong_key_is_hashed_into_squares_limit():
    long_key = "feoh:" + "x" * 200
    out = square.square_idempotency_key(long_key)
    assert len(out) <= square.MAX_IDEMPOTENCY_KEY
    assert out == square.square_idempotency_key(long_key)
    assert square.square_idempotency_key("feoh:short:receive") == "feoh:short:receive"


async def test_available_after_is_read_from_the_response_counts(fake):
    adapter = _adapter()
    first = await adapter.adjust_available(_adj("3", key="a"))
    second = await adapter.adjust_available(_adj("4", key="b"))
    assert (first.available_after, second.available_after) == (Decimal("3"), Decimal("7"))


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #


async def test_identical_resend_returns_squares_original_response(fake):
    adapter = _adapter()
    first = await adapter.adjust_available(_adj("3"))
    again = await adapter.adjust_available(_adj("3"))
    assert again.provider_reference == first.provider_reference
    assert await _available(fake, "VAR-TEE-M", "L-MAIN") == Decimal("3")


async def test_a_reused_key_with_a_changed_body_is_found_by_reference_id(fake, monkeypatch):
    """A retry later carries a new occurred_at; Square refuses the key; we find the original."""
    adapter = _adapter()
    first = await adapter.adjust_available(_adj("3", key="feoh:line-9:receive"))

    monkeypatch.setattr(
        square,
        "datetime",
        type("_Later", (), {"now": staticmethod(lambda tz: datetime(2099, 1, 1, tzinfo=tz))}),
    )
    retried = await adapter.adjust_available(_adj("3", key="feoh:line-9:receive"))
    assert retried.provider_reference == first.provider_reference
    assert retried.replayed is True
    assert await _available(fake, "VAR-TEE-M", "L-MAIN") == Decimal("3")


async def test_a_reused_key_with_no_matching_adjustment_is_refused_not_assumed(fake, monkeypatch):
    adapter = _adapter()
    await adapter.adjust_available(_adj("3", key="feoh:line-7:receive"))
    # The original is gone from Square's history view (or under another item).
    fake.PROVIDERS["square"]._CHANGES.clear()
    monkeypatch.setattr(
        square,
        "datetime",
        type("_Later", (), {"now": staticmethod(lambda tz: datetime(2099, 1, 1, tzinfo=tz))}),
    )
    with pytest.raises(CommerceRejectedError) as err:
        await adapter.adjust_available(_adj("3", key="feoh:line-7:receive"))
    assert err.value.retryable is False
    assert "already used" in str(err.value)


# --------------------------------------------------------------------------- #
# SKU resolution and locations
# --------------------------------------------------------------------------- #


async def test_resolve_sku_returns_the_variation(fake):
    ref = await _adapter().resolve_sku("SQ-TEE-M")
    assert (ref.item_id, ref.sku, ref.title) == ("VAR-TEE-M", "SQ-TEE-M", "Tee, medium")


async def test_an_ambiguous_sku_is_unmapped_not_guessed(fake):
    with pytest.raises(CommerceUnmappedSkuError) as err:
        await _adapter().resolve_sku("SQ-DUP")
    assert "More than one" in str(err.value)


async def test_inactive_locations_are_listed_inactive(fake):
    locations = {loc.id: loc for loc in await _adapter().list_locations()}
    assert locations["L-MAIN"].active is True
    assert locations["L-CLOSED"].active is False


async def test_unknown_location_maps_to_unknown_location(fake):
    with pytest.raises(CommerceUnknownLocationError) as err:
        await _adapter().adjust_available(_adj("1", location_id="L-NOWHERE"))
    assert "INVALID_REQUEST_ERROR/NOT_FOUND" in str(err.value)


async def test_unknown_catalog_object_is_a_plain_rejection(fake):
    with pytest.raises(CommerceRejectedError) as err:
        await _adapter().adjust_available(_adj("1", item_id="VAR-NOPE"))
    assert not isinstance(err.value, CommerceUnknownLocationError)
    assert err.value.retryable is False


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    [
        (401, CommerceAuthError, False),
        (429, CommerceRateLimitedError, True),
        (500, CommerceTransientError, True),
        (503, CommerceTransientError, True),
    ],
)
async def test_square_failures_map_to_typed_errors(fake, status, error_type, retryable):
    await _arm(fake, status)
    err = await assert_maps_http_errors(lambda: _adapter().adjust_available(_adj("1")))
    assert isinstance(err, error_type)
    assert err.retryable is retryable
    # Square's `detail` never reaches the admin-visible message; the token neither.
    assert "secret-detail" not in str(err)
    assert TOKEN not in str(err)


def _response(status: int, body) -> httpx.Response:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Response(status, content=content)


@pytest.mark.parametrize(
    ("status", "category", "code", "error_type"),
    [
        (400, "AUTHENTICATION_ERROR", "ACCESS_TOKEN_EXPIRED", CommerceAuthError),
        (400, "RATE_LIMIT_ERROR", "RATE_LIMITED", CommerceRateLimitedError),
        (400, "API_ERROR", "INTERNAL_SERVER_ERROR", CommerceTransientError),
        (403, "INVALID_REQUEST_ERROR", "INSUFFICIENT_SCOPES", CommerceAuthError),
        (400, "INVALID_REQUEST_ERROR", "INVALID_VALUE", CommerceRejectedError),
    ],
)
def test_category_wins_over_status(status, category, code, error_type):
    err = square.square_failure(
        "step", _response(status, {"errors": [{"category": category, "code": code}]})
    )
    assert type(err) is error_type


def test_a_non_constant_code_or_a_body_is_never_echoed():
    err = square.square_failure(
        "step",
        _response(
            400,
            {
                "errors": [
                    {
                        "category": "INVALID_REQUEST_ERROR",
                        "code": "x <script>",
                        "detail": "SKU-SECRET",
                    }
                ]
            },
        ),
    )
    assert str(err) == "step failed (HTTP 400, INVALID_REQUEST_ERROR)"
    assert type(square.square_failure("step", _response(502, b"<html>gateway</html>"))) is (
        CommerceTransientError
    )


async def test_a_dropped_connection_is_transient_and_names_no_url(fake):
    class _Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError(f"cannot reach {request.url}")

    commerce_base.set_transport_override(_Down())
    err = await assert_maps_http_errors(lambda: _adapter().test_connection())
    assert isinstance(err, CommerceTransientError)
    assert "://" not in str(err)
    assert "fake-commerce" not in str(err)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


async def test_environment_picks_the_square_host(monkeypatch):
    monkeypatch.setattr(settings, "commerce_api_base_override", "")
    seen: list[str] = []

    async def fake_assert(url):
        seen.append(url)

    monkeypatch.setattr("app.utils.url_safety.assert_public_url_async", fake_assert)
    assert await _adapter({**CONFIG, "environment": "production"})._base() == (
        "https://connect.squareup.com"
    )
    assert await _adapter({**CONFIG, "environment": "sandbox"})._base() == (
        "https://connect.squareupsandbox.com"
    )


async def test_a_missing_or_unknown_environment_fails_closed(fake):
    with pytest.raises(CommerceConfigError):
        await _adapter({"provider": "square", "access_token": TOKEN}).test_connection()
    with pytest.raises(CommerceConfigError):
        await _adapter({**CONFIG, "environment": "staging"}).test_connection()


async def test_a_missing_token_fails_closed_before_any_request(recorded):
    with pytest.raises(CommerceConfigError):
        await _adapter({"provider": "square", "environment": "sandbox"}).test_connection()
    assert recorded.requests == []
