"""The Shopify commerce adapter against tools/fake-commerce/providers/shopify.py.

No database and no container: the fake runs in process (``use_fake_commerce``).
The generic contract comes first; the rest pins what is Shopify's own — the
shop-domain guard (it says where the token goes), the API-version floor where
``@idempotent`` exists, exact-SKU filtering over token search, the precheck that
spends no idempotency key on a bad location, the attempt-key walk after
``IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED``, and the error mapping (THROTTLED,
ACCESS_DENIED, 401, 5xx, transport).
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
)
from app.services.commerce_adapters import base as commerce_base
from app.services.commerce_adapters.registry import adapter_class, build_adapter
from app.services.commerce_adapters.shopify import (
    DEFAULT_API_VERSION,
    MAX_KEY_ATTEMPTS,
    ShopifyCommerceAdapter,
    attempt_key,
    normalize_shop_domain,
    reference_uri,
)
from tests.commerce_contract import (
    ContractCase,
    assert_commerce_contract,
    assert_maps_http_errors,
    use_fake_commerce,
)

TOKEN = "shpat_fake_shopify_token"
READONLY = "shpat_fake_readonly_token"
LOC_MAIN = "gid://shopify/Location/1"
LOC_WAREHOUSE = "gid://shopify/Location/2"
LOC_CLOSED = "gid://shopify/Location/3"
TEE = "gid://shopify/InventoryItem/101"
MUG = "gid://shopify/InventoryItem/103"
CONFIG = {"provider": "shopify", "shop_domain": "feoh-test.myshopify.com", "access_token": TOKEN}


@pytest.fixture
def fake(monkeypatch):
    with use_fake_commerce(monkeypatch) as module:
        yield module


def adapter(**overrides) -> ShopifyCommerceAdapter:
    return build_adapter({**CONFIG, **overrides})


def shop_state(fake) -> dict:
    return fake.provider_state("shopify")


def level(fake, item: str, location: str) -> Decimal:
    return Decimal(shop_state(fake)["levels"].get(f"{item}@{location}", "0"))


async def fail_next(fake_module, **payload) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_module.app), base_url="http://fake"
    ) as client:
        resp = await client.post("/shopify/__fail-next", json=payload)
        assert resp.status_code == 200


async def configure_fake(fake_module, **payload) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_module.app), base_url="http://fake"
    ) as client:
        assert (await client.post("/shopify/__config", json=payload)).status_code == 200


def adjustment(key: str, delta: str = "2", item: str = TEE, location: str = LOC_MAIN, **kw):
    return InventoryAdjustment(
        item_id=item, location_id=location, delta=Decimal(delta), idempotency_key=key, **kw
    )


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


async def test_shopify_honours_the_commerce_contract(fake):
    async def read_available(item_id: str, location_id: str) -> Decimal:
        return level(fake, item_id, location_id)

    await assert_commerce_contract(
        ContractCase(
            provider="shopify",
            config=CONFIG,
            bad_auth_config={**CONFIG, "access_token": "shpat_wrong"},
            known_sku="SHOP-TEE-M",
            unmapped_sku="NO-SUCH-SKU",
            location_id=LOC_MAIN,
            unknown_location_id="gid://shopify/Location/999",
            read_available=read_available,
        )
    )


# --------------------------------------------------------------------------- #
# Catalogue entry
# --------------------------------------------------------------------------- #


def test_spec_declares_secret_destination_and_whole_units():
    spec = adapter_class("shopify").spec
    fields = {f["name"]: f for f in spec.fields}
    assert set(fields) == {"shop_domain", "access_token", "api_version"}
    assert fields["access_token"]["secret"] is True
    assert fields["shop_domain"]["required"] is True
    assert fields["api_version"]["required"] is False
    assert "shop_domain" in spec.destination_keys
    assert spec.supports_fractional_quantities is False
    # Every field reuses a label the frontend catalogues already carry.
    assert set(fields) <= catalog.KNOWN_FIELD_LABELS
    assert "access_token" in catalog.secret_keys()
    assert "shop_domain" in catalog.destination_keys()


def test_the_access_token_is_sealed_never_configuration():
    from app.services.provider_credentials import SECRET_FIELDS

    assert "access_token" in SECRET_FIELDS["commerce"]
    public = catalog.public_commerce_config(CONFIG)
    assert "access_token" not in public
    assert public["shop_domain"] == CONFIG["shop_domain"]


def test_changing_the_shop_domain_drops_the_stored_token():
    stored = catalog.public_commerce_config(CONFIG)
    merged = catalog.merge_commerce_update(
        stored, {"provider": "shopify", "shop_domain": "attacker.myshopify.com"}
    )
    assert "access_token" in catalog.secrets_to_drop(stored, merged)
    same = catalog.merge_commerce_update(stored, dict(stored))
    assert catalog.secrets_to_drop(stored, same) == frozenset()


# --------------------------------------------------------------------------- #
# Where the token goes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("feoh-test.myshopify.com", "feoh-test.myshopify.com"),
        ("feoh-test", "feoh-test.myshopify.com"),
        ("https://Feoh-Test.myshopify.com/", "feoh-test.myshopify.com"),
        ("  shop1.myshopify.com ", "shop1.myshopify.com"),
    ],
)
def test_shop_domain_normalizes(raw, expected):
    assert normalize_shop_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "evil.com",
        "shop.myshopify.com.evil.com",
        "evil.com/x.myshopify.com",
        "shop.myshopify.com:8443",
        "user@shop.myshopify.com",
        "http://shop.myshopify.com",
        "shop.myshopify.com/admin",
        "a.b.myshopify.com",
        "-shop.myshopify.com",
        "shop-.myshopify.com",
        "127.0.0.1",
        "shop.myshopify.com#@evil.com",
    ],
)
def test_shop_domain_refuses_anything_but_a_myshopify_host(raw):
    with pytest.raises(CommerceConfigError):
        normalize_shop_domain(raw)


async def test_bad_shop_domain_fails_before_any_request(fake):
    with pytest.raises(CommerceConfigError):
        await adapter(shop_domain="evil.example.com").test_connection()


async def test_real_base_is_https_on_the_shop_host(monkeypatch):
    """Without the operator override the endpoint is the shop's own https host,
    and the base still goes through the public-address check."""
    seen = []

    async def fake_public(url):
        seen.append(url)

    monkeypatch.setattr(commerce_base.settings, "commerce_api_base_override", "")
    monkeypatch.setattr("app.utils.url_safety.assert_public_url_async", fake_public)
    endpoint = await adapter()._endpoint()
    assert endpoint == (
        f"https://feoh-test.myshopify.com/admin/api/{DEFAULT_API_VERSION}/graphql.json"
    )
    assert seen == ["https://feoh-test.myshopify.com"]


@pytest.mark.parametrize("version", ["2025-10", "2026-02", "latest", "unstable", "2026-1"])
async def test_api_version_must_be_a_release_with_idempotent(fake, version):
    with pytest.raises(CommerceConfigError):
        await adapter(api_version=version).test_connection()


async def test_api_version_is_configurable(fake):
    await adapter(api_version="2026-07").test_connection()


async def test_missing_token_is_a_config_error(fake):
    with pytest.raises(CommerceConfigError):
        await adapter(access_token="").test_connection()


# --------------------------------------------------------------------------- #
# Locations and SKUs
# --------------------------------------------------------------------------- #


async def test_locations_skip_inactive_and_follow_pages(fake):
    await configure_fake(fake, locations_page_size=1)
    locations = await adapter().list_locations()
    assert [(loc.id, loc.name) for loc in locations] == [
        (LOC_MAIN, "Main store"),
        (LOC_WAREHOUSE, "Warehouse"),
    ]


async def test_resolve_sku_filters_token_search_to_an_exact_match(fake):
    # The fake's search, like Shopify's, also returns SHOP-TEE-M-2.
    item = await adapter().resolve_sku("SHOP-TEE-M")
    assert item.item_id == TEE
    assert item.title == "Tee - M"


async def test_resolve_sku_refuses_an_ambiguous_sku(fake):
    with pytest.raises(CommerceUnmappedSkuError) as err:
        await adapter().resolve_sku("SHOP-DUP")
    assert "2 Shopify product variants" in str(err.value)


async def test_resolve_sku_escapes_search_syntax(fake):
    item = await adapter().resolve_sku('SHOP-"Q"')
    assert item.item_id == "gid://shopify/InventoryItem/106"


async def test_resolve_blank_sku_is_unmapped_without_a_call(fake):
    with pytest.raises(CommerceUnmappedSkuError):
        await adapter().resolve_sku("  ")


# --------------------------------------------------------------------------- #
# Adjustments
# --------------------------------------------------------------------------- #


async def test_adjust_sends_the_shopify_shape(fake):
    result = await adapter().adjust_available(adjustment("feoh:line-1:receive", "4"))
    assert result.provider_reference.startswith("gid://shopify/InventoryAdjustmentGroup/")
    assert result.available_after == Decimal("4")
    [sent] = shop_state(fake)["mutations"]
    assert sent["idempotency_key"] == "feoh:line-1:receive"
    assert sent["header_key"] == "feoh:line-1:receive"
    payload = sent["input"]
    assert payload["name"] == "available"
    assert payload["reason"] == "received"
    assert payload["referenceDocumentUri"] == reference_uri("feoh:line-1:receive")
    assert payload["referenceDocumentUri"].startswith("feohledger://inventory-sync/feoh%3A")
    assert payload["changes"] == [{"inventoryItemId": TEE, "locationId": LOC_MAIN, "delta": 4}]


async def test_cancelled_receipt_uses_the_correction_reason(fake):
    await adapter().adjust_available(adjustment("k:receive", "3"))
    await adapter().adjust_available(adjustment("k:reverse", "-3", reason="receipt_cancelled"))
    assert shop_state(fake)["mutations"][-1]["input"]["reason"] == "correction"
    assert level(fake, TEE, LOC_MAIN) == Decimal("0")


async def test_replay_returns_the_original_group_and_moves_stock_once(fake):
    first = await adapter().adjust_available(adjustment("k1", "5"))
    again = await adapter().adjust_available(adjustment("k1", "5"))
    assert again.provider_reference == first.provider_reference
    assert level(fake, TEE, LOC_MAIN) == Decimal("5")


async def test_previous_failed_attempt_walks_to_the_next_key(fake):
    await fail_next(fake, kind="ADJUST_QUANTITIES_FAILED")
    with pytest.raises(CommerceTransientError) as err:
        await adapter().adjust_available(adjustment("k2", "2"))
    assert err.value.retryable is True
    assert level(fake, TEE, LOC_MAIN) == Decimal("0")

    # The retry: key "k2" is spent (failed) → "k2#2" applies it, once.
    done = await adapter().adjust_available(adjustment("k2", "2"))
    assert level(fake, TEE, LOC_MAIN) == Decimal("2")
    # A later replay walks the same sequence and lands on the same group.
    replay = await adapter().adjust_available(adjustment("k2", "2"))
    assert replay.provider_reference == done.provider_reference
    assert level(fake, TEE, LOC_MAIN) == Decimal("2")
    assert shop_state(fake)["idempotency_keys"] == {"k2": "failed", "k2#2": "ok"}


async def test_every_attempt_key_spent_is_a_refusal(fake):
    for attempt in range(1, MAX_KEY_ATTEMPTS + 1):
        await fail_next(fake, kind="SERVICE_UNAVAILABLE")
        with pytest.raises(CommerceTransientError):
            await adapter().adjust_available(adjustment("k3", "1"))
    assert attempt_key("k3", MAX_KEY_ATTEMPTS) in shop_state(fake)["idempotency_keys"]
    with pytest.raises(CommerceRejectedError) as err:
        await adapter().adjust_available(adjustment("k3", "1"))
    assert err.value.retryable is False
    assert level(fake, TEE, LOC_MAIN) == Decimal("0")


async def test_same_key_with_other_parameters_is_refused_not_rekeyed(fake):
    await adapter().adjust_available(adjustment("k4", "2", location=LOC_MAIN))
    with pytest.raises(CommerceRejectedError) as err:
        await adapter().adjust_available(adjustment("k4", "2", location=LOC_WAREHOUSE))
    assert err.value.retryable is False
    assert level(fake, TEE, LOC_WAREHOUSE) == Decimal("0")
    assert level(fake, TEE, LOC_MAIN) == Decimal("2")


async def test_inactive_location_is_unknown_and_spends_no_key(fake):
    with pytest.raises(CommerceUnknownLocationError):
        await adapter().adjust_available(adjustment("k5", "1", location=LOC_CLOSED))
    assert shop_state(fake)["mutations"] == []


async def test_item_not_stocked_at_location_is_refused_before_the_mutation(fake):
    with pytest.raises(CommerceRejectedError) as err:
        await adapter().adjust_available(adjustment("k6", "1", item=MUG, location=LOC_WAREHOUSE))
    assert "not stocked" in str(err.value)
    assert shop_state(fake)["mutations"] == []


async def test_unknown_item_is_refused(fake):
    with pytest.raises(CommerceRejectedError):
        await adapter().adjust_available(
            adjustment("k7", "1", item="gid://shopify/InventoryItem/999")
        )


async def test_fraction_is_refused_before_any_call(fake):
    with pytest.raises(CommerceRejectedError) as err:
        await adapter().adjust_available(adjustment("k8", "1.5"))
    assert err.value.code == "fractional_quantity"
    assert shop_state(fake)["mutations"] == []


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


async def test_throttled_maps_to_rate_limited(fake):
    await fail_next(fake, kind="throttled")
    err = await assert_maps_http_errors(lambda: adapter().list_locations())
    assert isinstance(err, CommerceRateLimitedError)
    assert err.retryable is True


async def test_http_429_maps_to_rate_limited(fake):
    await fail_next(fake, kind="http", status=429)
    err = await assert_maps_http_errors(lambda: adapter().test_connection())
    assert isinstance(err, CommerceRateLimitedError)


async def test_http_5xx_maps_to_transient(fake):
    await fail_next(fake, kind="http", status=503)
    err = await assert_maps_http_errors(lambda: adapter().resolve_sku("SHOP-TEE-M"))
    assert isinstance(err, CommerceTransientError)
    assert "503" in str(err)


async def test_bad_token_is_auth_failure_without_echoing_it(fake):
    err = await assert_maps_http_errors(
        lambda: adapter(access_token="shpat_wrong_secret_value").test_connection()
    )
    assert isinstance(err, CommerceAuthError)
    assert "shpat_wrong_secret_value" not in str(err)
    assert "Invalid API key" not in str(err)  # the body is never copied


async def test_missing_scope_is_auth_failure(fake):
    err = await assert_maps_http_errors(
        lambda: adapter(access_token=READONLY).adjust_available(adjustment("k9", "1"))
    )
    assert isinstance(err, CommerceAuthError)
    assert "write_inventory" in str(err)
    assert READONLY not in str(err)


async def test_transport_failure_is_transient_and_names_no_url(fake, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("connection refused to http://fake-commerce/shopify?token=x")

    commerce_base.set_transport_override(httpx.MockTransport(boom))
    err = await assert_maps_http_errors(lambda: adapter().test_connection())
    assert isinstance(err, CommerceTransientError)
    assert str(err) == "Shopify connection test failed (ConnectError)"


async def test_unreadable_body_is_transient(fake):
    commerce_base.set_transport_override(
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>"))
    )
    err = await assert_maps_http_errors(lambda: adapter().test_connection())
    assert isinstance(err, CommerceTransientError)


async def test_other_graphql_errors_are_rejections_by_code_only(fake):
    def reply(request):
        return httpx.Response(
            200,
            json={"errors": [{"message": "secret-ish echo SKU-123", "extensions": {"code": "X"}}]},
        )

    commerce_base.set_transport_override(httpx.MockTransport(reply))
    err = await assert_maps_http_errors(lambda: adapter().test_connection())
    assert isinstance(err, CommerceRejectedError)
    assert "SKU-123" not in str(err)
