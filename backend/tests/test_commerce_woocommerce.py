"""WooCommerce commerce adapter (decisions §273; commerce-integration.md § WooCommerce).

Runs the shared contract against tools/fake-commerce's ``woocommerce`` module in
process, then the provider-specific behaviour: the store URL is an SSRF-guarded
https destination, credentials ride Basic auth only, redirects are refused,
``manage_stock`` must be on, variations resolve (directly and through the
legacy fallback), duplicates and commas never guess, and the read → write
adjustment is idempotent through the ``_feoh_inventory_syncs`` product meta.
"""

from __future__ import annotations

import json
from decimal import Decimal

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
)
from app.services.commerce_adapters import woocommerce as woo
from app.services.commerce_adapters.registry import adapter_class
from tests.commerce_contract import (
    ContractCase,
    assert_commerce_contract,
    assert_maps_http_errors,
    use_fake_commerce,
)

KEY = "ck_fake_woocommerce"
SECRET = "cs_fake_woocommerce"
CONFIG = {
    "provider": "woocommerce",
    "store_url": "https://shop.example.com",
    "consumer_key": KEY,
    "consumer_secret": SECRET,
}


@pytest.fixture
def fake(monkeypatch):
    with use_fake_commerce(monkeypatch) as module:
        yield module


def _fake_woo(fake):
    return fake.PROVIDERS["woocommerce"]


def _product(fake, product_id: int) -> dict:
    return fake.provider_state("woocommerce")["products"][str(product_id)]


def _adapter(**overrides) -> woo.WooCommerceAdapter:
    return woo.WooCommerceAdapter({**CONFIG, **overrides})


def _adjust(item_id: str, delta: str, key: str = "feoh:line-1:receive") -> InventoryAdjustment:
    return InventoryAdjustment(
        item_id=item_id,
        location_id=woo.STORE_LOCATION_ID,
        delta=Decimal(delta),
        idempotency_key=key,
    )


# --------------------------------------------------------------------------- #
# Contract + catalogue
# --------------------------------------------------------------------------- #


async def test_woocommerce_honours_the_contract(fake):
    async def read_available(item_id: str, _location: str) -> Decimal:
        return Decimal(_product(fake, int(item_id.split("/")[-1]))["stock_quantity"] or 0)

    await assert_commerce_contract(
        ContractCase(
            provider="woocommerce",
            config=CONFIG,
            bad_auth_config={**CONFIG, "consumer_secret": "cs_wrong_secret"},
            known_sku="WOO-SIMPLE-1",
            unmapped_sku="NO-SUCH-SKU",
            location_id=woo.STORE_LOCATION_ID,
            unknown_location_id="warehouse-2",
            read_available=read_available,
        )
    )
    # Every call authenticated in the header; none put a credential in a URL.
    assert fake.provider_state("woocommerce")["query_auth_requests"] == 0


def test_spec_marks_secrets_and_the_store_url_as_destination():
    cls = adapter_class("woocommerce")
    assert cls is woo.WooCommerceAdapter
    fields = {f["name"]: f for f in cls.spec.fields}
    assert fields["consumer_key"]["secret"] and fields["consumer_secret"]["secret"]
    assert not fields["store_url"]["secret"]
    assert "store_url" in catalog.destination_keys()
    assert {"consumer_key", "consumer_secret"} <= catalog.secret_keys()
    assert cls.spec.supports_fractional_quantities is False
    # Every field already has a label in the frontend catalogues.
    assert set(fields) <= catalog.KNOWN_FIELD_LABELS


async def test_one_synthetic_location(fake):
    locations = await _adapter().list_locations()
    assert [(loc.id, loc.name) for loc in locations] == [("store", "Store")]


# --------------------------------------------------------------------------- #
# Destination: https, public host, no credentials in the URL, no redirects
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "store_url",
    [
        "http://shop.example.com",
        "https://user:pw@shop.example.com",
        "https://shop.example.com/?consumer_key=x",
        "https://shop.example.com/#frag",
        "ftp://shop.example.com",
        "https://",
    ],
)
async def test_store_url_shape_is_refused_before_any_call(fake, store_url):
    with pytest.raises(CommerceConfigError):
        await _adapter(store_url=store_url).test_connection()
    assert fake.provider_state("woocommerce")["requests"] == []


@pytest.mark.parametrize(
    "store_url",
    ["https://127.0.0.1", "https://169.254.169.254/latest", "https://10.0.0.5/shop"],
)
async def test_store_url_must_be_a_public_host(monkeypatch, store_url):
    # The real path, no fake-commerce override: api_base runs the SSRF check.
    monkeypatch.setattr(settings, "commerce_api_base_override", "")
    with pytest.raises(CommerceConfigError) as err:
        await _adapter(store_url=store_url).test_connection()
    assert "public host" in str(err.value)


async def test_api_base_is_the_store_wp_json_root(monkeypatch):
    monkeypatch.setattr(settings, "commerce_api_base_override", "")

    async def public(_url: str) -> None:
        return None

    monkeypatch.setattr("app.utils.url_safety.assert_public_url_async", public)
    adapter = _adapter(store_url="https://example.com/shop/")
    assert await adapter._base() == "https://example.com/shop/wp-json/wc/v3"


async def test_a_redirect_is_refused_not_followed(fake):
    _fake_woo(fake)._STATE["fail_next"] = 301
    err = await assert_maps_http_errors(_adapter().test_connection)
    assert isinstance(err, CommerceRejectedError)
    assert err.retryable is False
    # One request, to the store; the Location was never visited.
    assert fake.provider_state("woocommerce")["requests"] == ["GET /woocommerce/system_status"]
    assert _adapter()._client().follow_redirects is False


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (401, CommerceAuthError, False),
        (403, CommerceAuthError, False),
        (429, CommerceRateLimitedError, True),
        (500, CommerceTransientError, True),
        (503, CommerceTransientError, True),
        (400, CommerceRejectedError, False),
    ],
)
async def test_http_errors_map_to_the_base_types(fake, status, expected, retryable):
    _fake_woo(fake)._STATE["fail_next"] = status
    err = await assert_maps_http_errors(lambda: _adapter().resolve_sku("WOO-SIMPLE-1"))
    assert type(err) is expected
    assert err.retryable is retryable
    assert KEY not in str(err) and SECRET not in str(err)
    assert "Injected failure" not in str(err), "a provider body must never be copied"


async def test_a_non_json_answer_is_rejected(fake, monkeypatch):
    # A WordPress site without WooCommerce (or a login page) answering 200 HTML.
    import httpx

    from app.services.commerce_adapters import base as commerce_base

    commerce_base.set_transport_override(
        httpx.MockTransport(lambda _req: httpx.Response(200, text="<html>hi</html>"))
    )
    with pytest.raises(CommerceRejectedError) as err:
        await _adapter().test_connection()
    assert "WooCommerce JSON" in str(err.value)


# --------------------------------------------------------------------------- #
# SKU resolution
# --------------------------------------------------------------------------- #


async def test_simple_product_resolves_case_insensitively(fake):
    item = await _adapter().resolve_sku("woo-simple-1")
    assert item.item_id == "101"
    assert item.sku == "woo-simple-1"


async def test_variation_sku_resolves_to_parent_slash_variation(fake):
    item = await _adapter().resolve_sku("WOO-VAR-RED")
    assert item.item_id == "200/201"
    # Found by /products?sku= alone (current WooCommerce matches variations).
    requests = fake.provider_state("woocommerce")["requests"]
    assert requests == ["GET /woocommerce/products"]


async def test_variation_fallback_when_products_search_omits_variations(fake):
    _fake_woo(fake).LEGACY_SKU_SEARCH = True
    item = await _adapter().resolve_sku("WOO-VAR-RED")
    assert item.item_id == "200/201"
    assert item.title == "Grip tape"
    assert (
        "GET /woocommerce/products/200/variations"
        in (fake.provider_state("woocommerce")["requests"])
    )


async def test_fallback_finds_nothing_for_an_unknown_sku(fake):
    _fake_woo(fake).LEGACY_SKU_SEARCH = True
    with pytest.raises(CommerceUnmappedSkuError):
        await _adapter().resolve_sku("NOPE")


async def test_fallback_scan_is_bounded(fake, monkeypatch):
    _fake_woo(fake).LEGACY_SKU_SEARCH = True
    monkeypatch.setattr(woo, "MAX_FALLBACK_PARENTS", 0)
    with pytest.raises(CommerceUnmappedSkuError):
        await _adapter().resolve_sku("WOO-VAR-RED")
    assert not any("/variations" in r for r in fake.provider_state("woocommerce")["requests"])


async def test_a_duplicated_sku_is_never_guessed(fake):
    with pytest.raises(CommerceUnmappedSkuError) as err:
        await _adapter().resolve_sku("WOO-DUP")
    assert "2 WooCommerce products share this SKU" in str(err.value)


async def test_a_comma_sku_is_not_sent_as_a_list(fake):
    with pytest.raises(CommerceUnmappedSkuError):
        await _adapter().resolve_sku("WOO-SIMPLE-1,WOO-DUP")
    assert fake.provider_state("woocommerce")["requests"] == []


# --------------------------------------------------------------------------- #
# Adjusting stock
# --------------------------------------------------------------------------- #


async def test_manage_stock_off_is_a_clear_refusal(fake):
    with pytest.raises(CommerceRejectedError) as err:
        await _adapter().adjust_available(_adjust("102", "4"))
    assert err.value.code == woo.STOCK_NOT_MANAGED
    assert err.value.retryable is False
    assert "Manage stock" in str(err.value)
    assert fake.provider_state("woocommerce")["puts"] == 0


async def test_variation_stock_is_adjusted_on_the_variation(fake):
    result = await _adapter().adjust_available(_adjust("200/201", "2"))
    assert result.available_after == Decimal("7")
    assert _product(fake, 201)["stock_quantity"] == 7
    assert _product(fake, 200)["stock_quantity"] == 7  # parent untouched


async def test_parent_managed_variation_adjusts_the_parent_pool(fake):
    result = await _adapter().adjust_available(_adjust("200/202", "3"))
    assert result.available_after == Decimal("10")
    assert _product(fake, 200)["stock_quantity"] == 10
    assert _product(fake, 202)["stock_quantity"] is None


async def test_replay_is_detected_through_product_meta(fake):
    adapter = _adapter()
    first = await adapter.adjust_available(_adjust("101", "4", key="feoh:abc:receive"))
    assert first.replayed is False and first.available_after == Decimal("14")
    meta = _product(fake, 101)["meta_data"]
    assert [m["key"] for m in meta] == [woo.SYNC_META_KEY]
    assert json.loads(meta[0]["value"]) == [{"k": "feoh:abc:receive", "d": 4, "a": 14}]

    # The response was lost and the push is retried: nothing is written again.
    again = await adapter.adjust_available(_adjust("101", "4", key="feoh:abc:receive"))
    assert again.replayed is True
    assert again.provider_reference == first.provider_reference
    assert _product(fake, 101)["stock_quantity"] == 14
    assert fake.provider_state("woocommerce")["puts"] == 1

    # The reverse is a new key; the meta row is updated in place, not duplicated.
    back = await adapter.adjust_available(_adjust("101", "-4", key="feoh:abc:reverse"))
    assert back.replayed is False and back.available_after == Decimal("10")
    meta = _product(fake, 101)["meta_data"]
    assert len(meta) == 1
    assert [e["k"] for e in json.loads(meta[0]["value"])] == [
        "feoh:abc:receive",
        "feoh:abc:reverse",
    ]


async def test_recorded_keys_are_bounded(fake, monkeypatch):
    monkeypatch.setattr(woo, "MAX_RECORDED_SYNCS", 3)
    adapter = _adapter()
    for n in range(5):
        await adapter.adjust_available(_adjust("101", "1", key=f"feoh:{n}:receive"))
    entries = json.loads(_product(fake, 101)["meta_data"][0]["value"])
    assert [e["k"] for e in entries] == ["feoh:2:receive", "feoh:3:receive", "feoh:4:receive"]
    assert _product(fake, 101)["stock_quantity"] == 15


async def test_unreadable_meta_is_treated_as_empty(fake):
    _fake_woo(fake).PRODUCTS[101]["meta_data"] = [
        {"id": 77, "key": woo.SYNC_META_KEY, "value": "not json"}
    ]
    result = await _adapter().adjust_available(_adjust("101", "1"))
    assert result.available_after == Decimal("11")
    meta = _product(fake, 101)["meta_data"]
    assert [m["id"] for m in meta] == [77]  # updated by id, not appended


async def test_fraction_is_refused_before_any_call(fake):
    with pytest.raises(CommerceRejectedError) as err:
        await _adapter().adjust_available(_adjust("101", "1.5"))
    assert err.value.code == "fractional_quantity"
    assert fake.provider_state("woocommerce")["requests"] == []


async def test_unknown_location_is_refused_before_any_call(fake):
    adjustment = InventoryAdjustment(
        item_id="101", location_id="warehouse", delta=Decimal("1"), idempotency_key="k"
    )
    with pytest.raises(CommerceUnknownLocationError):
        await _adapter().adjust_available(adjustment)
    assert fake.provider_state("woocommerce")["requests"] == []


@pytest.mark.parametrize("item_id", ["abc", "101/", "/201", "101/201/3", "-1", ""])
async def test_malformed_item_id_is_rejected(fake, item_id):
    with pytest.raises(CommerceRejectedError):
        await _adapter().adjust_available(_adjust(item_id, "1"))


async def test_missing_product_is_rejected(fake):
    with pytest.raises(CommerceRejectedError) as err:
        await _adapter().adjust_available(_adjust("999", "1"))
    assert "HTTP 404" in str(err.value)
