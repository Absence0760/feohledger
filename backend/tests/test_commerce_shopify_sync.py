"""Receipt → Shopify, end to end through the sync service (decisions §273). Real Postgres.

The adapter-level contract lives in ``test_commerce_shopify.py``; this pins the
same path the e2e spec drives, without a browser: connect Shopify through
``/api/commerce/settings``, map its location, record a receipt, and the
in-process fake's stock moves once for the line whose SKU resolves while the
SKU two variants share is skipped ``unmapped_sku``; cancelling reverses it; a
throttled push fails retryably and a retry lands it under the same key.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from app.services.post_commit import drain_post_commit
from tests.commerce_contract import use_fake_commerce
from tests.test_commerce_sync import TENANT, _line_id, _po, _receive, _rows, _wire  # noqa: F401

TOKEN = "shpat_fake_shopify_token"
LOCATION = "gid://shopify/Location/1"
TEE = "gid://shopify/InventoryItem/101"


@pytest.fixture
def fake(monkeypatch):
    with use_fake_commerce(monkeypatch) as module:
        yield module


def tee_level(fake) -> Decimal:
    levels = fake.provider_state("shopify")["levels"]
    return Decimal(levels.get(f"{TEE}@{LOCATION}", "0"))


async def _connect_shopify(realdb):
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.put(
            "/api/commerce/settings",
            json={"settings": {"provider": "shopify", "shop_domain": "feoh-test.myshopify.com"}},
        )
        assert resp.status_code == 200, resp.text
        # The token is sealed through the credentials endpoint, never settings.
        sealed = await c.put(
            "/api/organization/credentials/commerce", json={"set": {"access_token": TOKEN}}
        )
        assert sealed.status_code == 200, sealed.text
        assert TOKEN not in sealed.text  # write-only
        assert (await c.get("/api/commerce/settings")).json()["secrets_set"] == ["access_token"]
        test = await c.post("/api/commerce/test")
        assert test.status_code == 200, test.text
        body = test.json()
        assert body["success"] is True
        assert [loc["id"] for loc in body["locations"]] == [LOCATION, "gid://shopify/Location/2"]
        resp = await c.put("/api/commerce/locations", json={"location_id": LOCATION})
        assert resp.status_code == 200, resp.text


async def test_a_receipt_moves_shopify_stock_and_a_cancel_takes_it_back(realdb, fake):
    await _connect_shopify(realdb)
    po_id, (tee, dup) = await _po(realdb, [("Tee", "10", "SHOP-TEE-M"), ("Dup", "5", "SHOP-DUP")])
    receipt = await _receive(realdb, po_id, [tee, dup], ["4", "1"])

    rows = await _rows(realdb, receipt["id"])
    pushed = rows[(_line_id(receipt, tee), "receive")]
    assert pushed.status == "synced", pushed.error_message
    assert pushed.provider_item_id == TEE
    assert pushed.provider_reference.startswith("gid://shopify/InventoryAdjustmentGroup/")
    skipped = rows[(_line_id(receipt, dup), "receive")]
    assert (skipped.status, skipped.reason) == ("skipped", "unmapped_sku")
    assert tee_level(fake) == Decimal("4")

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(f"/api/goods-receipts/{receipt['id']}/cancel")
    assert resp.status_code == 200, resp.text
    await drain_post_commit()

    reverse = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, tee), "reverse")]
    assert reverse.status == "synced", reverse.error_message
    assert tee_level(fake) == Decimal("0")
    sent = fake.provider_state("shopify")["mutations"]
    assert [m["input"]["reason"] for m in sent] == ["received", "correction"]


async def test_a_throttled_push_fails_retryably_then_a_retry_lands_once(realdb, fake):
    await _connect_shopify(realdb)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app), base_url="http://fake"
    ) as client:
        # The push's first Shopify call (the precheck) is throttled, so the
        # mutation is never sent and no idempotency key is spent.
        assert (
            await client.post("/shopify/__fail-next", json={"kind": "throttled"})
        ).status_code == 200

    # SKU lookup happens first and would consume the throttle — map it instead.
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.put(
            "/api/commerce/sku-mappings",
            json={"sku": "SHOP-TEE-M", "provider_item_id": TEE},
        )
    assert resp.status_code == 200, resp.text

    po_id, (tee,) = await _po(realdb, [("Tee", "10", "SHOP-TEE-M")])
    receipt = await _receive(realdb, po_id, [tee], ["3"])
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, tee), "receive")]
    assert (row.status, row.reason) == ("failed", "rate_limited")
    assert tee_level(fake) == Decimal("0")

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(f"/api/commerce/syncs/{row.id}/retry")
    assert resp.status_code == 200, resp.text
    await drain_post_commit()
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, tee), "receive")]
    assert row.status == "synced", row.error_message
    assert tee_level(fake) == Decimal("3")
    assert fake.provider_state("shopify")["idempotency_keys"] == {row.idempotency_key: "ok"}
