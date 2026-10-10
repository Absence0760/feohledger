"""Cin7 Core commerce adapter (decisions §268; backend/docs/commerce-integration.md § Cin7 Core).

Runs against tools/fake-commerce/providers/cin7.py in process — no container,
no database. The contract helper pins the family-wide properties; the cases
below pin what is particular to Cin7: the read-then-set stock adjustment (Cin7
takes the NEW on-hand value, not a delta), replay detection by the adjustment's
``Reference``, exact decimals on the wire, and Cin7's status mapping.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from app.services.commerce_adapters import (
    CommerceAuthError,
    CommerceRateLimitedError,
    CommerceRejectedError,
    CommerceTransientError,
    CommerceUnknownLocationError,
    CommerceUnmappedSkuError,
    InventoryAdjustment,
    catalog,
    cin7,
)
from app.services.commerce_adapters import base as commerce_base
from app.services.commerce_adapters.registry import build_adapter
from tests.commerce_contract import (
    ContractCase,
    assert_commerce_contract,
    assert_maps_http_errors,
    use_fake_commerce,
)

CONFIG = {
    "provider": "cin7",
    "account_id": "fake-cin7-account",
    "application_key": "fake-cin7-key",
}


@pytest.fixture
def fake(monkeypatch):
    with use_fake_commerce(monkeypatch) as module:
        yield module.PROVIDERS["cin7"]


def _adapter(**overrides):
    return build_adapter({**CONFIG, **overrides})


def _adjustment(
    fake, delta: str, key: str, *, item=None, location=None, reason="received", attempt=1
):
    return InventoryAdjustment(
        item_id=item or fake.PRODUCT_1,
        location_id=location or fake.MAIN,
        delta=Decimal(delta),
        idempotency_key=key,
        sku="CIN7-SKU-1",
        reason=reason,
        attempt=attempt,
    )


async def _fail_next(fake, status: int) -> None:
    fake._FAIL_NEXT.append(status)


# --------------------------------------------------------------------------- #
# The family contract
# --------------------------------------------------------------------------- #


async def test_cin7_honours_the_contract(fake):
    async def read_available(item_id: str, location_id: str) -> Decimal:
        return fake.BOOK.available(item_id, location_id)

    await assert_commerce_contract(
        ContractCase(
            provider="cin7",
            config=CONFIG,
            bad_auth_config={**CONFIG, "application_key": "wrong-key-value"},
            known_sku="CIN7-SKU-1",
            unmapped_sku="NO-SUCH-SKU",
            location_id=fake.MAIN,
            unknown_location_id="99999999-9999-4999-8999-999999999999",
            read_available=read_available,
        )
    )


def test_spec_declares_the_secret_destination_and_fractions():
    spec = cin7.SPEC
    fields = {f["name"]: f for f in spec.fields}
    assert fields["application_key"]["secret"] is True
    assert fields["account_id"]["secret"] is False
    assert fields["adjustment_account"]["required"] is False
    assert "account_id" in catalog.destination_keys()
    assert "application_key" in catalog.secret_keys()
    assert spec.supports_fractional_quantities is True
    assert catalog.provider("cin7")["available"] is True


# --------------------------------------------------------------------------- #
# Stock adjustments: Cin7 takes the new on-hand value
# --------------------------------------------------------------------------- #


async def test_adjustment_posts_on_hand_plus_delta_exactly(fake):
    fake.BOOK.levels[(fake.PRODUCT_1, fake.MAIN)] = Decimal("10")
    result = await _adapter(adjustment_account="630").adjust_available(
        _adjustment(fake, "2.75", "feoh:line-1:receive")
    )

    assert result.replayed is False
    assert result.provider_reference == fake.adjustments()[0]["TaskID"]
    assert fake.BOOK.available(fake.PRODUCT_1, fake.MAIN) == Decimal("12.75")
    posted = fake.adjustments()[0]
    assert posted["Status"] == "COMPLETED"
    assert posted["Reference"] == "feoh:line-1:receive"
    assert posted["Account"] == "630"
    assert "received" in posted["Comment"]
    (line,) = posted["Lines"]
    # The new on-hand value, as an exact JSON number (parsed by the fake with
    # parse_float=Decimal) — never a float, never a string.
    assert line["Quantity"] == Decimal("12.75")
    assert line["UnitCost"] == Decimal("42.5")
    assert line["LocationID"] == fake.MAIN


def test_decimal_bodies_are_exact_json_numbers():
    body = cin7._dumps({"Quantity": Decimal("0.1") + Decimal("0.2"), "Big": Decimal("1E+3")})
    assert json.loads(body, parse_float=Decimal) == {"Quantity": Decimal("0.3"), "Big": 1000}
    assert b'"0.3"' not in body


async def test_reversal_records_its_reason_and_takes_stock_back(fake):
    fake.BOOK.levels[(fake.PRODUCT_1, fake.MAIN)] = Decimal("5")
    await _adapter().adjust_available(
        _adjustment(fake, "-2", "feoh:line-1:reverse", reason="receipt_cancelled")
    )
    assert fake.BOOK.available(fake.PRODUCT_1, fake.MAIN) == Decimal("3")
    posted = fake.adjustments()[0]
    assert "cancelled" in posted["Comment"]
    assert posted["Account"] == ""


async def test_reversal_below_zero_is_refused_before_anything_is_written(fake):
    fake.BOOK.levels[(fake.PRODUCT_1, fake.MAIN)] = Decimal("1")
    with pytest.raises(CommerceRejectedError) as err:
        await _adapter().adjust_available(_adjustment(fake, "-2", "feoh:line-1:reverse"))
    assert err.value.retryable is False
    assert fake.adjustments() == []
    assert fake.BOOK.available(fake.PRODUCT_1, fake.MAIN) == Decimal("1")


async def test_batch_tracked_product_is_refused(fake):
    with pytest.raises(CommerceRejectedError, match="batch"):
        await _adapter().adjust_available(
            _adjustment(fake, "1", "feoh:line-2:receive", item=fake.PRODUCT_BATCH)
        )
    assert fake.adjustments() == []


async def test_a_mapped_service_product_is_refused(fake):
    with pytest.raises(CommerceRejectedError, match="service"):
        await _adapter().adjust_available(
            _adjustment(fake, "1", "feoh:line-3:receive", item=fake.PRODUCT_SERVICE)
        )


async def test_a_mapped_product_that_is_gone_is_unmapped(fake):
    with pytest.raises(CommerceUnmappedSkuError):
        await _adapter().adjust_available(
            _adjustment(
                fake, "1", "feoh:line-4:receive", item="12345678-1234-4234-8234-123456789012"
            )
        )


async def test_a_location_id_that_is_not_a_guid_is_unknown_without_a_call(fake):
    await _fail_next(fake, 500)  # would surface as transient if a call were made
    with pytest.raises(CommerceUnknownLocationError):
        await _adapter().adjust_available(
            _adjustment(fake, "1", "feoh:line-5:receive", location="main-warehouse")
        )


# --------------------------------------------------------------------------- #
# Replay detection by Reference
# --------------------------------------------------------------------------- #


async def test_a_lost_response_is_a_replay_not_a_second_adjustment(fake):
    adapter = _adapter()
    first = await adapter.adjust_available(_adjustment(fake, "4", "feoh:line-6:receive"))
    # The push landed but the result was never recorded (a crash, a timeout
    # after Cin7 applied it); the sweep reclaims the row and pushes again.
    again = await _adapter().adjust_available(
        _adjustment(fake, "4", "feoh:line-6:receive", attempt=2)
    )

    assert again.replayed is True
    assert again.provider_reference == first.provider_reference
    assert len(fake.adjustments()) == 1
    assert fake.BOOK.available(fake.PRODUCT_1, fake.MAIN) == Decimal("4")


async def test_an_adjustment_voided_in_cin7_is_still_a_replay(fake):
    first = await _adapter().adjust_available(_adjustment(fake, "4", "feoh:line-7:receive"))
    fake._ADJUSTMENTS[0]["Status"] = "VOIDED"

    again = await _adapter().adjust_available(
        _adjustment(fake, "4", "feoh:line-7:receive", attempt=2)
    )
    assert again.replayed is True and again.provider_reference == first.provider_reference
    assert len(fake.adjustments()) == 1


async def test_a_draft_with_our_reference_is_refused_for_an_admin(fake):
    fake._ADJUSTMENTS.append(
        {
            "TaskID": "d4af7000-0000-4000-8000-000000000001",
            "EffectiveDate": "2026-10-09T00:00:00",
            "StocktakeNumber": "ST-00099",
            "Status": "DRAFT",
            "Account": "",
            "Reference": "feoh:line-8:receive",
            "Comment": "",
            "Lines": [],
        }
    )
    with pytest.raises(CommerceRejectedError, match="ST-00099"):
        await _adapter().adjust_available(_adjustment(fake, "1", "feoh:line-8:receive", attempt=2))
    assert len(fake.adjustments()) == 1


async def test_a_first_push_skips_the_reference_scan(fake, monkeypatch):
    # attempt == 1 is the first push of a key ever made (the sync service
    # commits attempts + 1 before calling), so there is nothing to find and the
    # full-list scan, the expensive part under Cin7's rate limit, is skipped.
    adapter = _adapter()
    paths: list[str] = []
    original = adapter._call

    async def counting(step, method, path, **kwargs):
        paths.append(path)
        return await original(step, method, path, **kwargs)

    monkeypatch.setattr(adapter, "_call", counting)
    await adapter.adjust_available(_adjustment(fake, "1", "feoh:first:receive"))
    assert "/stockadjustmentList" not in paths
    assert len(paths) == 4  # location, product, stock level, the write

    paths.clear()
    again = await adapter.adjust_available(_adjustment(fake, "1", "feoh:first:receive", attempt=2))
    assert paths[0] == "/stockadjustmentList"
    assert again.replayed is True
    assert len(fake.adjustments()) == 1


async def test_the_replay_check_pages_the_whole_list(fake, monkeypatch):
    monkeypatch.setattr(cin7, "PAGE_LIMIT", 2)
    adapter = _adapter()
    for n in range(5):
        await adapter.adjust_available(_adjustment(fake, "1", f"feoh:page-{n}:receive"))
    # The first adjustment sits on page 1, the last on page 3 — both found.
    for n in (0, 4):
        result = await adapter.adjust_available(
            _adjustment(fake, "1", f"feoh:page-{n}:receive", attempt=2)
        )
        assert result.replayed is True
    assert len(fake.adjustments()) == 5
    assert fake.BOOK.available(fake.PRODUCT_1, fake.MAIN) == Decimal("5")


async def test_the_replay_check_refuses_past_its_page_bound(fake, monkeypatch):
    monkeypatch.setattr(cin7, "PAGE_LIMIT", 1)
    monkeypatch.setattr(cin7, "MAX_REPLAY_SCAN_PAGES", 2)
    adapter = _adapter()
    for n in range(3):  # the third push still fits: two pages of one cover two rows
        await adapter.adjust_available(_adjustment(fake, "1", f"feoh:cap-{n}:receive"))
    with pytest.raises(CommerceRejectedError, match="duplicate check"):
        await adapter.adjust_available(_adjustment(fake, "1", "feoh:cap-new:receive", attempt=2))
    assert len(fake.adjustments()) == 3


# --------------------------------------------------------------------------- #
# SKUs and locations
# --------------------------------------------------------------------------- #


async def test_resolve_sku_takes_the_exact_match_from_a_prefix_result(fake):
    item = await _adapter().resolve_sku("CIN7-SKU-1")
    assert item.item_id == fake.PRODUCT_1
    assert item.title == "Tennis racket"


async def test_resolve_sku_refuses_a_service_and_an_ambiguous_sku(fake):
    with pytest.raises(CommerceUnmappedSkuError):
        await _adapter().resolve_sku("CIN7-SERVICE")
    fake.add_product("CIN7-SKU-1", "A second racket")
    with pytest.raises(CommerceUnmappedSkuError, match="More than one"):
        await _adapter().resolve_sku("CIN7-SKU-1")


async def test_list_locations_leaves_out_fixed_asset_locations(fake):
    locations = await _adapter().list_locations()
    assert {loc.id: loc.name for loc in locations} == {
        fake.MAIN: "Main Warehouse",
        fake.SHOPFRONT: "Shopfront",
    }
    assert all(loc.active for loc in locations)


# --------------------------------------------------------------------------- #
# Status mapping, and nothing secret in an error
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "error", "retryable"),
    [
        (429, CommerceRateLimitedError, True),
        (403, CommerceAuthError, False),
        (401, CommerceAuthError, False),
        (503, CommerceTransientError, True),
        (400, CommerceRejectedError, False),
    ],
)
async def test_http_statuses_map_to_typed_errors(fake, status, error, retryable):
    await _fail_next(fake, status)
    err = await assert_maps_http_errors(lambda: _adapter().test_connection())
    assert isinstance(err, error)
    assert err.retryable is retryable
    assert f"HTTP {status}" in str(err)
    assert "fake-cin7-key" not in str(err) and "Injected" not in str(err)


async def test_a_rate_limit_mid_adjustment_writes_nothing_and_retries(fake):
    # Cin7's 60-a-minute limit lands on the location lookup, before any write.
    adapter = _adapter()
    original = adapter._location

    async def limited(location_id):
        await _fail_next(fake, 429)
        return await original(location_id)

    adapter._location = limited
    with pytest.raises(CommerceRateLimitedError):
        await adapter.adjust_available(_adjustment(fake, "1", "feoh:line-9:receive"))
    assert fake.adjustments() == []


async def test_a_dropped_connection_is_transient_and_names_no_url(fake):
    class Boom(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("connect failed: https://inventory.dearsystems.com/?k=v")

    commerce_base.set_transport_override(Boom())
    err = await assert_maps_http_errors(lambda: _adapter().test_connection())
    assert isinstance(err, CommerceTransientError) and err.retryable
    assert "dearsystems" not in str(err) and "ConnectError" in str(err)


async def test_a_missing_key_is_a_config_error_naming_the_field_only(fake):
    err = await assert_maps_http_errors(
        lambda: build_adapter({"provider": "cin7", "account_id": "a"}).test_connection()
    )
    assert err.code == "config_invalid" and "application_key" in str(err)
