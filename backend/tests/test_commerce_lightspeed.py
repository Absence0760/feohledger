"""Lightspeed Retail (X-Series) commerce adapter (decisions §273).

Runs the adapter against ``tools/fake-commerce/providers/lightspeed.py`` in
process (no container) through ``use_fake_commerce``. The shared contract is
``tests/commerce_contract.py``; the cases below pin what is Lightspeed's own:
the domain-prefix rule, the stock-order write path, idempotency by the
consignment ``reference`` (replay, resume, refusal) and the error mapping.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

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
    lightspeed,
)
from app.services.commerce_adapters import base as commerce_base
from app.services.commerce_adapters.base import FRACTIONAL_QUANTITY
from app.services.commerce_adapters.registry import adapter_class, build_adapter
from tests.commerce_contract import (
    ContractCase,
    assert_commerce_contract,
    assert_maps_http_errors,
    use_fake_commerce,
)

TOKEN = "fake-lightspeed-token"
CONFIG = {"provider": "lightspeed", "domain_prefix": "fakeshop", "access_token": TOKEN}
BAD_CONFIG = {**CONFIG, "access_token": "wrong-token-value"}
MAIN = "ls-outlet-main"
PRODUCT = "ls-prod-1"


@pytest.fixture
def fake(monkeypatch):
    with use_fake_commerce(monkeypatch) as module:
        yield module


def _provider(fake):
    return fake.PROVIDERS["lightspeed"]


def _adapter(config=None):
    return build_adapter(config or CONFIG)


def _level(fake, item_id=PRODUCT, location_id=MAIN) -> Decimal:
    return _provider(fake).BOOK.available(item_id, location_id)


def _adjustment(delta="3", key="feoh:line-1:receive", **kw) -> InventoryAdjustment:
    return InventoryAdjustment(
        item_id=kw.pop("item_id", PRODUCT),
        location_id=kw.pop("location_id", MAIN),
        delta=Decimal(delta),
        idempotency_key=key,
        sku="LS-SKU-1",
        **kw,
    )


async def _arm(fake, **hook):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app), base_url="http://fake-commerce"
    ) as client:
        assert (await client.post("/lightspeed/__fail-next", json=hook)).status_code == 200


# --------------------------------------------------------------------------- #
# The shared contract
# --------------------------------------------------------------------------- #


async def test_lightspeed_honours_the_contract(fake):
    async def read_available(item_id: str, location_id: str) -> Decimal:
        return _level(fake, item_id, location_id)

    await assert_commerce_contract(
        ContractCase(
            provider="lightspeed",
            config=CONFIG,
            bad_auth_config=BAD_CONFIG,
            known_sku="LS-SKU-1",
            unmapped_sku="NO-SUCH-SKU",
            location_id=MAIN,
            unknown_location_id="ls-outlet-nowhere",
            read_available=read_available,
        )
    )


# --------------------------------------------------------------------------- #
# Spec and configuration
# --------------------------------------------------------------------------- #


def test_spec_is_registered_whole_unit_and_its_prefix_is_a_destination():
    cls = adapter_class("lightspeed")
    assert cls is lightspeed.LightspeedCommerceAdapter
    assert cls.spec.supports_fractional_quantities is False
    assert "domain_prefix" in cls.spec.destination_keys
    assert {f["name"] for f in cls.spec.fields if f["secret"]} == {"access_token"}
    # Both field labels already exist in the frontend catalogues.
    assert {f["name"] for f in cls.spec.fields} <= catalog.KNOWN_FIELD_LABELS
    assert "access_token" in catalog.secret_keys()


@pytest.mark.parametrize(
    "prefix",
    [
        "evil.example.com",
        "fakeshop.retail.lightspeed.app",
        "https://fakeshop",
        "fake shop",
        "fakeshop:443",
        "-fakeshop",
        "fakeshop/",
        "a" * 64,
    ],
)
async def test_domain_prefix_must_be_a_bare_label(fake, prefix):
    with pytest.raises(CommerceConfigError) as err:
        await _adapter({**CONFIG, "domain_prefix": prefix}).test_connection()
    assert prefix not in str(err.value), "the rejected value is echoed back"
    assert TOKEN not in str(err.value)


async def test_domain_prefix_is_case_insensitive(fake):
    await _adapter({**CONFIG, "domain_prefix": "FakeShop"}).test_connection()


async def test_missing_token_is_a_config_error(fake):
    with pytest.raises(CommerceConfigError, match="access_token"):
        await _adapter({"provider": "lightspeed", "domain_prefix": "fakeshop"}).test_connection()


async def test_real_host_is_built_from_the_prefix(monkeypatch):
    """No override: the request goes to <prefix>.retail.lightspeed.app over https."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"id": "r"}})

    async def public(url: str) -> None:
        return None

    monkeypatch.setattr(commerce_base.settings, "commerce_api_base_override", "")
    monkeypatch.setattr("app.utils.url_safety.assert_public_url_async", public)
    commerce_base.set_transport_override(httpx.MockTransport(handler))
    try:
        await _adapter().test_connection()
    finally:
        commerce_base.set_transport_override(None)
    assert str(seen[0].url) == (
        f"https://fakeshop.retail.lightspeed.app/api/{lightspeed.API_VERSION}/retailer"
    )
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"


# --------------------------------------------------------------------------- #
# Locations and SKUs
# --------------------------------------------------------------------------- #


async def test_deleted_outlets_are_not_offered(fake):
    ids = {loc.id for loc in await _adapter().list_locations()}
    assert ids == {"ls-outlet-main", "ls-outlet-warehouse"}


async def test_outlets_are_read_across_pages(fake, monkeypatch):
    monkeypatch.setattr(lightspeed, "PAGE_SIZE", 1)
    ids = {loc.id for loc in await _adapter().list_locations()}
    assert ids == {"ls-outlet-main", "ls-outlet-warehouse"}


async def test_resolve_sku_matches_case_insensitively(fake):
    item = await _adapter().resolve_sku("ls-sku-1")
    assert item.item_id == PRODUCT
    assert item.sku == "ls-sku-1"
    assert item.title == "Wilson Pro Staff 97"


async def test_a_sku_on_two_products_is_unmapped_not_guessed(fake):
    with pytest.raises(CommerceUnmappedSkuError, match="More than one"):
        await _adapter().resolve_sku("LS-DUP")


async def test_a_deleted_product_does_not_resolve(fake):
    with pytest.raises(CommerceUnmappedSkuError):
        await _adapter().resolve_sku("LS-GONE")


async def test_a_blank_sku_does_not_call_out(fake):
    with pytest.raises(CommerceUnmappedSkuError):
        await _adapter().resolve_sku("  ")
    assert _provider(fake).state()["calls"] == []


# --------------------------------------------------------------------------- #
# The write path: stock orders
# --------------------------------------------------------------------------- #


async def test_a_receipt_is_a_received_supplier_order_carrying_the_key(fake):
    result = await _adapter().adjust_available(_adjustment("4"))
    assert _level(fake) == Decimal("4")
    [order] = _provider(fake).state()["consignments"]
    assert order["id"] == result.provider_reference
    assert order["type"] == "SUPPLIER"
    assert order["status"] == "RECEIVED"
    assert order["reference"] == "feoh:line-1:receive"
    assert order["name"] == lightspeed.NOTE_RECEIVED
    assert order["outlet_id"] == MAIN
    assert order["products"][0]["count"] == "4"
    assert order["products"][0]["received"] == "4"
    assert result.replayed is False


async def test_a_cancellation_is_a_sent_return(fake):
    await _adapter().adjust_available(_adjustment("5"))
    result = await _adapter().adjust_available(
        _adjustment("-2", key="feoh:line-1:reverse", reason="receipt_cancelled")
    )
    assert _level(fake) == Decimal("3")
    order = next(
        c for c in _provider(fake).state()["consignments"] if c["id"] == result.provider_reference
    )
    assert (order["type"], order["status"]) == ("RETURN", "SENT")
    assert order["name"] == lightspeed.NOTE_CANCELLED
    assert order["products"][0]["count"] == "2"
    assert "received" not in order["products"][0]


async def test_a_replay_writes_nothing(fake):
    first = await _adapter().adjust_available(_adjustment("2"))
    calls_before = len(_provider(fake).state()["calls"])
    again = await _adapter().adjust_available(_adjustment("2"))
    assert again.replayed is True
    assert again.provider_reference == first.provider_reference
    assert _level(fake) == Decimal("2")
    writes = [
        c for c in _provider(fake).state()["calls"][calls_before:] if not c.startswith("GET ")
    ]
    assert writes == []


async def test_a_half_built_order_is_resumed_not_duplicated(fake):
    """The status PUT fails after the order and its line exist: the retry finishes it."""
    await _arm(fake, status=503, method="PUT", path_contains="/consignments/")
    with pytest.raises(CommerceTransientError):
        await _adapter().adjust_available(_adjustment("6"))
    assert _level(fake) == Decimal("0")

    result = await _adapter().adjust_available(_adjustment("6"))
    orders = _provider(fake).state()["consignments"]
    assert len(orders) == 1, "the retry created a second stock order"
    assert orders[0]["id"] == result.provider_reference
    assert orders[0]["status"] == "RECEIVED"
    assert _level(fake) == Decimal("6")


async def test_an_order_whose_line_never_landed_gets_its_line(fake):
    await _arm(fake, status=500, method="POST", path_contains="/products")
    with pytest.raises(CommerceTransientError):
        await _adapter().adjust_available(_adjustment("2"))
    await _adapter().adjust_available(_adjustment("2"))
    [order] = _provider(fake).state()["consignments"]
    assert len(order["products"]) == 1
    assert _level(fake) == Decimal("2")


async def test_an_order_cancelled_in_lightspeed_is_not_resent(fake):
    adapter = _adapter()
    adjustment = _adjustment("2")
    consignment_id = await adapter._create_consignment("SUPPLIER", adjustment)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app), base_url="http://fake-commerce"
    ) as client:
        resp = await client.put(
            f"/lightspeed/api/{lightspeed.API_VERSION}/consignments/{consignment_id}",
            json={"data": {"status": "CANCELLED"}},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 200
    with pytest.raises(CommerceRejectedError, match="cancelled") as err:
        await adapter.adjust_available(adjustment)
    assert err.value.retryable is False
    assert _level(fake) == Decimal("0")


async def test_the_idempotency_lookup_pages(fake, monkeypatch):
    for n in range(3):
        await _adapter().adjust_available(_adjustment("1", key=f"feoh:line-{n}:receive"))
    monkeypatch.setattr(lightspeed, "PAGE_SIZE", 1)
    again = await _adapter().adjust_available(_adjustment("1", key="feoh:line-0:receive"))
    assert again.replayed is True
    assert _level(fake) == Decimal("3")


async def test_a_lookup_past_the_page_bound_refuses_rather_than_risk_a_duplicate(fake, monkeypatch):
    for n in range(3):
        await _adapter().adjust_available(_adjustment("1", key=f"feoh:line-{n}:receive"))
    monkeypatch.setattr(lightspeed, "PAGE_SIZE", 1)
    monkeypatch.setattr(lightspeed, "MAX_SCAN_PAGES", 2)
    with pytest.raises(CommerceRejectedError, match="Too many") as err:
        await _adapter().adjust_available(_adjustment("1", key="feoh:line-new:receive"))
    assert err.value.retryable is False
    assert _level(fake) == Decimal("3")
    assert len(_provider(fake).state()["consignments"]) == 3


async def test_a_fraction_is_refused_before_any_call(fake):
    with pytest.raises(CommerceRejectedError) as err:
        await _adapter().adjust_available(_adjustment("1.5"))
    assert err.value.code == FRACTIONAL_QUANTITY
    assert _provider(fake).state()["calls"] == []


async def test_a_deleted_outlet_is_an_unknown_location(fake):
    with pytest.raises(CommerceUnknownLocationError):
        await _adapter().adjust_available(_adjustment("1", location_id="ls-outlet-closed"))


async def test_the_key_for_one_direction_never_satisfies_the_other(fake):
    """A receive and its reverse share a line id; each finds only its own order type."""
    await _adapter().adjust_available(_adjustment("3", key="feoh:line-9:receive"))
    reverse = await _adapter().adjust_available(_adjustment("-3", key="feoh:line-9:reverse"))
    assert reverse.replayed is False
    assert _level(fake) == Decimal("0")


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, CommerceAuthError),
        (403, CommerceAuthError),
        (429, CommerceRateLimitedError),
        (500, CommerceTransientError),
        (503, CommerceTransientError),
        (422, CommerceRejectedError),
    ],
)
async def test_http_errors_map_to_the_base_types(fake, status, error):
    await _arm(fake, status=status, path_contains="/search")
    err = await assert_maps_http_errors(lambda: _adapter().resolve_sku("LS-SKU-1"))
    assert type(err) is error
    assert TOKEN not in str(err)
    assert "injected" not in str(err), "the provider's body was copied into the message"


async def test_a_dropped_connection_is_transient_and_names_no_url(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(commerce_base.settings, "commerce_api_base_override", "http://fake")
    commerce_base.set_transport_override(httpx.MockTransport(handler))
    try:
        err = await assert_maps_http_errors(lambda: _adapter().test_connection())
    finally:
        commerce_base.set_transport_override(None)
    assert isinstance(err, CommerceTransientError)
    assert "http" not in str(err).lower().replace("(connecterror)", "")


async def test_a_bad_token_on_the_write_path_is_an_auth_error(fake):
    with pytest.raises(CommerceAuthError):
        await _adapter(BAD_CONFIG).adjust_available(_adjustment("1"))
    assert _level(fake) == Decimal("0")
