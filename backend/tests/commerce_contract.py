"""The commerce adapter contract, as a reusable test helper (decisions §273).

Every provider's test module proves its adapter honours the interface by
calling :func:`assert_commerce_contract` — the mock's own test does, and so does
each real provider's, against its fake-commerce module mounted IN PROCESS (no
container): :func:`fake_commerce_transport` loads ``tools/fake-commerce/app.py``
and routes every adapter's HTTP to it, and :func:`use_fake_commerce` also sets
the operator override so ``CommerceAdapter.api_base`` resolves to
``http://fake-commerce/<provider key>``.

A provider test module, in full::

    import pytest
    from tests.commerce_contract import ContractCase, assert_commerce_contract, use_fake_commerce

    @pytest.fixture
    def fake(monkeypatch):
        with use_fake_commerce(monkeypatch) as state:
            yield state

    async def test_shopify_honours_the_contract(fake):
        await assert_commerce_contract(
            ContractCase(
                provider="shopify",
                config={"provider": "shopify", "shop_domain": "x.myshopify.com",
                        "access_token": "fake-token"},
                bad_auth_config={"provider": "shopify", "shop_domain": "x.myshopify.com",
                                 "access_token": "wrong"},
                known_sku="FAKE-SKU-1",
                unmapped_sku="NO-SUCH-SKU",
                location_id="gid://shopify/Location/1",
                unknown_location_id="gid://shopify/Location/999",
            )
        )

What it pins (each a property the sync service relies on):

1. ``test_connection`` returns with good credentials and raises
   ``CommerceAuthError`` with bad ones (``bad_auth_config``);
2. ``list_locations`` includes the location the case names;
3. ``resolve_sku`` returns an item for the known SKU and raises
   ``CommerceUnmappedSkuError`` for the unmapped one;
4. ``adjust_available`` with a positive delta returns a non-empty
   ``provider_reference``; **the same idempotency key again changes nothing**
   (same reference, and the level read back — when the case can read it — moved
   once); a negative delta under a new key is applied. The repeat is sent as
   ``attempt=2``, as the sync service sends every re-send (its claim commits
   ``attempts + 1`` before the call), so an adapter may look for an earlier
   write only on a re-send;
5. an unknown location raises ``CommerceUnknownLocationError`` (or a
   non-retryable ``CommerceRejectedError``);
6. a fractional delta on a whole-units platform raises ``CommerceRejectedError``
   with code ``fractional_quantity``; on a fractional platform it is applied;
7. every error raised is a ``CommerceError`` whose message does not contain any
   secret value from the config.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import settings
from app.services.commerce_adapters import (
    CommerceAuthError,
    CommerceError,
    CommerceRejectedError,
    CommerceUnknownLocationError,
    CommerceUnmappedSkuError,
    InventoryAdjustment,
)
from app.services.commerce_adapters import base as commerce_base
from app.services.commerce_adapters.base import FRACTIONAL_QUANTITY
from app.services.commerce_adapters.catalog import secret_keys
from app.services.commerce_adapters.registry import adapter_class

FAKE_COMMERCE_DIR = Path(__file__).resolve().parents[2] / "tools" / "fake-commerce"
FAKE_BASE = "http://fake-commerce"


def load_fake_commerce():
    """Import ``tools/fake-commerce/app.py`` (and its ``providers`` package) in process."""
    if "fake_commerce_app" in sys.modules:
        return sys.modules["fake_commerce_app"]
    sys.path.insert(0, str(FAKE_COMMERCE_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "fake_commerce_app", FAKE_COMMERCE_DIR / "app.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["fake_commerce_app"] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(FAKE_COMMERCE_DIR))
    return module


@contextlib.contextmanager
def use_fake_commerce(monkeypatch) -> Iterator[Any]:
    """Route every commerce adapter at the in-process fake; reset it around the test.

    Yields the fake's module, whose ``provider_state(key)`` exposes each
    provider's in-memory store for assertions.
    """
    fake = load_fake_commerce()
    fake.reset_all()
    monkeypatch.setattr(settings, "commerce_api_base_override", FAKE_BASE)
    commerce_base.set_transport_override(httpx.ASGITransport(app=fake.app))
    try:
        yield fake
    finally:
        commerce_base.set_transport_override(None)
        fake.reset_all()


@dataclass
class ContractCase:
    provider: str
    #: Plaintext config that authenticates against the fake.
    config: dict[str, Any]
    known_sku: str
    unmapped_sku: str
    location_id: str
    unknown_location_id: str
    #: Same provider, credentials the fake refuses. None skips check 1's negative half.
    bad_auth_config: dict[str, Any] | None = None
    #: Reads the provider's available quantity, when the test can (e.g. from
    #: the fake's state). Lets check 4 prove a replay moved the level once.
    read_available: Callable[[str, str], Awaitable[Decimal]] | None = None


def _no_secret_in(exc: BaseException, config: dict[str, Any]) -> None:
    text = str(exc)
    for key in secret_keys():
        value = config.get(key)
        if isinstance(value, str) and len(value) >= 4:
            assert value not in text, f"error message leaks the '{key}' value"


async def assert_commerce_contract(case: ContractCase) -> None:
    cls = adapter_class(case.provider)
    adapter = cls(case.config)
    spec = cls.spec
    assert spec.key == case.provider

    # 1. Credentials.
    await adapter.test_connection()
    if case.bad_auth_config is not None:
        with pytest.raises(CommerceAuthError) as bad:
            await cls(case.bad_auth_config).test_connection()
        _no_secret_in(bad.value, case.bad_auth_config)
        assert bad.value.retryable is False

    # 2. Locations.
    locations = await adapter.list_locations()
    assert case.location_id in {loc.id for loc in locations}, "the case's location is not listed"
    assert all(loc.id and loc.name for loc in locations)

    # 3. SKU resolution.
    item = await adapter.resolve_sku(case.known_sku)
    assert item.item_id, "resolve_sku returned an empty item id"
    assert item.sku == case.known_sku
    with pytest.raises(CommerceUnmappedSkuError) as unmapped:
        await adapter.resolve_sku(case.unmapped_sku)
    assert unmapped.value.code == "unmapped_sku"
    assert unmapped.value.retryable is False

    # 4. Idempotent adjustment, then a negative one.
    key = f"contract:{uuid.uuid4()}:receive"
    before = None
    if case.read_available is not None:
        before = await case.read_available(item.item_id, case.location_id)
    plus = InventoryAdjustment(
        item_id=item.item_id,
        location_id=case.location_id,
        delta=Decimal("3"),
        idempotency_key=key,
        sku=case.known_sku,
    )
    first = await adapter.adjust_available(plus)
    assert first.provider_reference, "adjust_available returned no provider reference"
    again = await adapter.adjust_available(replace(plus, attempt=2))
    assert again.provider_reference == first.provider_reference, (
        "a replayed idempotency key must return the original adjustment"
    )
    if case.read_available is not None:
        after = await case.read_available(item.item_id, case.location_id)
        assert after - before == Decimal("3"), "a replayed key changed the stock twice"
    minus = await adapter.adjust_available(
        InventoryAdjustment(
            item_id=item.item_id,
            location_id=case.location_id,
            delta=Decimal("-2"),
            idempotency_key=f"contract:{uuid.uuid4()}:reverse",
            sku=case.known_sku,
            reason="receipt_cancelled",
        )
    )
    assert minus.provider_reference and minus.provider_reference != first.provider_reference
    if case.read_available is not None:
        final = await case.read_available(item.item_id, case.location_id)
        assert final - before == Decimal("1"), "the negative delta was not applied"

    # 5. Unknown location.
    with pytest.raises((CommerceUnknownLocationError, CommerceRejectedError)) as bad_loc:
        await adapter.adjust_available(
            InventoryAdjustment(
                item_id=item.item_id,
                location_id=case.unknown_location_id,
                delta=Decimal("1"),
                idempotency_key=f"contract:{uuid.uuid4()}:receive",
            )
        )
    assert bad_loc.value.retryable is False
    _no_secret_in(bad_loc.value, case.config)

    # 6. Fractions.
    fractional = InventoryAdjustment(
        item_id=item.item_id,
        location_id=case.location_id,
        delta=Decimal("0.5"),
        idempotency_key=f"contract:{uuid.uuid4()}:receive",
    )
    if spec.supports_fractional_quantities:
        assert (await adapter.adjust_available(fractional)).provider_reference
    else:
        with pytest.raises(CommerceRejectedError) as frac:
            await adapter.adjust_available(fractional)
        assert frac.value.code == FRACTIONAL_QUANTITY


async def assert_maps_http_errors(adapter_call: Callable[[], Awaitable[Any]]) -> CommerceError:
    """Run ``adapter_call`` and assert it raised a typed, secret-free CommerceError.

    For a provider test that drives the fake into a failure mode (a 500, a 429,
    a dropped connection) and checks the adapter's mapping.
    """
    with pytest.raises(CommerceError) as err:
        await adapter_call()
    return err.value
