"""Receipt → commerce platform sync, end to end (decisions §268). Real Postgres.

Pinned here:

  * nothing is planned for an org with no platform connected;
  * a receipt plans one row per line in its own transaction and pushes after
    commit — synced lines move the mock platform's stock, a line with no SKU is
    skipped (`no_sku`), an unknown SKU is skipped (`unmapped_sku`), a missing
    location is skipped (`no_location`);
  * a replayed receipt (same Idempotency-Key) plans nothing new;
  * cancelling takes back exactly what landed, and voids what never did;
  * a retryable failure is retried by the sweep with backoff and stops at the
    attempt cap; a manual retry re-sends under the same key;
  * the SKU override and the per-entity location are honoured;
  * a provider switch, a disconnect and a fractional quantity on a whole-unit
    platform fail with stable codes rather than pushing somewhere wrong;
  * the settings API: admin-only, configuration only (secrets sealed, refused
    here, dropped on a destination change), PATCH refused,
    audited; the syncs read is entity-scoped.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.models.commerce import CommerceInventorySync
from app.models.entity import Entity
from app.models.procurement import POLineItem, PurchaseOrder
from app.models.workflow import AuditLog
from app.services import commerce_sync
from app.services.commerce_adapters import mock_adapter
from app.services.post_commit import drain_post_commit
from app.utils.dates import utc_today

TENANT = "a"


@pytest.fixture(autouse=True)
def _wire(realdb, monkeypatch):
    """Point the post-commit push at the harness's per-loop tenant engine."""
    makers: dict[str, object] = {}

    def _maker(db_name: str):
        key = "a" if db_name == realdb.info("a").db_name else "b"
        if key not in makers:
            makers[key] = realdb.sessionmaker(key)
        return makers[key]

    monkeypatch.setattr(commerce_sync, "tenant_sessionmaker", _maker)
    mock_adapter.reset()
    yield
    mock_adapter.reset()


async def _connect(realdb, *, location="mock-main", key=TENANT, extra=None):
    async with realdb.client(key=key, role="admin") as c:
        resp = await c.put(
            "/api/commerce/settings", json={"settings": {"provider": "mock", **(extra or {})}}
        )
        assert resp.status_code == 200, resp.text
        if location is not None:
            resp = await c.put("/api/commerce/locations", json={"location_id": location})
            assert resp.status_code == 200, resp.text


async def _po(realdb, lines, *, key=TENANT, entity_id=None):
    """lines: (description, quantity, sku). Returns (po_id, [line ids])."""
    async with realdb.sessionmaker(key)() as s:
        default = select(Entity.id).where(Entity.is_default)
        ent = entity_id or (await s.execute(default)).scalar_one()
        po = PurchaseOrder(
            po_number=f"PO-CS-{uuid.uuid4().hex[:6]}",
            total=Decimal("100"),
            currency="USD",
            status="open",
            organization_id=realdb.info(key).org_id,
            entity_id=ent,
        )
        s.add(po)
        await s.flush()
        ids = []
        for desc, qty, sku in lines:
            li = POLineItem(po_id=po.id, description=desc, quantity=Decimal(qty), sku=sku)
            s.add(li)
            await s.flush()
            ids.append(li.id)
        await s.commit()
        return po.id, ids


async def _receive(realdb, po_id, line_ids, qtys, *, key=TENANT, idem=None):
    headers = {"Idempotency-Key": idem} if idem else {}
    async with realdb.client(key=key, role="ap_clerk") as c:
        resp = await c.post(
            "/api/goods-receipts",
            json={
                "po_id": str(po_id),
                "received_date": utc_today().isoformat(),
                "lines": [
                    {"po_line_item_id": str(li), "quantity_received": q}
                    for li, q in zip(line_ids, qtys, strict=True)
                ],
            },
            headers=headers,
        )
    assert resp.status_code in (200, 201), resp.text
    await drain_post_commit()
    return resp.json()


async def _rows(realdb, gr_id, *, key=TENANT) -> dict[tuple[str, str], CommerceInventorySync]:
    async with realdb.sessionmaker(key)() as s:
        rows = (
            (
                await s.execute(
                    select(CommerceInventorySync).where(
                        CommerceInventorySync.goods_receipt_id == uuid.UUID(gr_id)
                    )
                )
            )
            .scalars()
            .all()
        )
    return {(str(r.gr_line_item_id), r.direction): r for r in rows}


def _line_id(receipt: dict, po_line_id) -> str:
    lines = receipt["line_items"]
    return next(li["id"] for li in lines if li["po_line_item_id"] == str(po_line_id))


async def test_nothing_is_planned_without_a_connected_platform(realdb):
    po_id, (li,) = await _po(realdb, [("Racket", "2", "WPS-97")])
    receipt = await _receive(realdb, po_id, [li], ["2"])
    assert await _rows(realdb, receipt["id"]) == {}


async def test_a_receipt_pushes_each_mapped_line_and_skips_the_rest(realdb):
    await _connect(realdb)
    po_id, (sku_li, plain_li, unmapped_li, zero_li) = await _po(
        realdb,
        [
            ("Racket", "10", "WPS-97"),
            ("Free text", "1", None),
            ("Mystery", "1", "UNMAPPED-9"),
            ("Grip", "5", "GRIP-1"),
        ],
    )
    lines = [sku_li, plain_li, unmapped_li, zero_li]
    receipt = await _receive(realdb, po_id, lines, ["4", "1", "1", "0"])
    rows = await _rows(realdb, receipt["id"])

    synced = rows[(_line_id(receipt, sku_li), "receive")]
    assert synced.status == "synced"
    assert synced.quantity_delta == Decimal("4")
    assert synced.provider_reference and synced.provider_reference.startswith("mock-adj-")
    assert synced.provider_item_id == "mock-item-WPS-97"
    assert synced.provider_location_id == "mock-main"
    assert synced.idempotency_key == f"feoh:{synced.gr_line_item_id}:receive"
    assert mock_adapter.available("mock-item-WPS-97", "mock-main") == Decimal("4")

    assert rows[(_line_id(receipt, plain_li), "receive")].reason == "no_sku"
    assert rows[(_line_id(receipt, unmapped_li), "receive")].status == "skipped"
    assert rows[(_line_id(receipt, unmapped_li), "receive")].reason == "unmapped_sku"
    assert rows[(_line_id(receipt, zero_li), "receive")].reason == "zero_quantity"

    async with realdb.sessionmaker(TENANT)() as s:
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(
                        AuditLog.entity_id == uuid.UUID(receipt["id"]),
                        AuditLog.action.like("commerce_inventory.%"),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sorted(actions) == sorted(
        ["commerce_inventory.synced"] + ["commerce_inventory.skipped"] * 3
    )


async def test_a_replayed_receipt_plans_nothing_new(realdb):
    await _connect(realdb)
    po_id, (li,) = await _po(realdb, [("Racket", "10", "WPS-97")])
    key = f"idem-{uuid.uuid4()}"
    first = await _receive(realdb, po_id, [li], ["3"], idem=key)
    again = await _receive(realdb, po_id, [li], ["3"], idem=key)
    assert again["id"] == first["id"]
    assert len(await _rows(realdb, first["id"])) == 1
    assert mock_adapter.available("mock-item-WPS-97", "mock-main") == Decimal("3")


async def test_cancelling_takes_back_what_landed_and_voids_what_did_not(realdb):
    await _connect(realdb)
    po_id, (good, unmapped) = await _po(
        realdb, [("Racket", "10", "WPS-97"), ("Mystery", "1", "UNMAPPED-1")]
    )
    receipt = await _receive(realdb, po_id, [good, unmapped], ["6", "1"])
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(f"/api/goods-receipts/{receipt['id']}/cancel")
    assert resp.status_code == 200, resp.text
    await drain_post_commit()

    rows = await _rows(realdb, receipt["id"])
    reverse = rows[(_line_id(receipt, good), "reverse")]
    assert reverse.status == "synced"
    assert reverse.quantity_delta == Decimal("-6")
    assert reverse.provider_location_id == "mock-main"
    assert mock_adapter.available("mock-item-WPS-97", "mock-main") == Decimal("0")

    voided = rows[(_line_id(receipt, unmapped), "receive")]
    assert voided.status == "cancelled" and voided.reason == "receipt_cancelled"
    assert rows[(_line_id(receipt, unmapped), "reverse")].reason == "nothing_to_reverse"

    # A voided receive can no longer be retried into the shelf count.
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/commerce/syncs/{voided.id}/retry")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "commerce_sync_not_retryable"


async def test_no_location_skips_then_a_mapped_retry_pushes(realdb):
    await _connect(realdb, location=None)
    po_id, (li,) = await _po(realdb, [("Racket", "10", "WPS-97")])
    receipt = await _receive(realdb, po_id, [li], ["2"])
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")]
    assert (row.status, row.reason) == ("skipped", "no_location")

    async with realdb.client(key=TENANT, role="admin") as c:
        assert (
            await c.put("/api/commerce/locations", json={"location_id": "mock-warehouse"})
        ).status_code == 200
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(f"/api/commerce/syncs/{row.id}/retry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    await drain_post_commit()
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")]
    assert row.status == "synced" and row.provider_location_id == "mock-warehouse"
    assert mock_adapter.available("mock-item-WPS-97", "mock-warehouse") == Decimal("2")


async def test_a_sku_override_beats_the_lookup(realdb):
    await _connect(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.put(
            "/api/commerce/sku-mappings",
            json={"sku": "UNMAPPED-ALIAS", "provider_item_id": "mock-item-REAL"},
        )
        assert resp.status_code == 200, resp.text
        listed = (await c.get("/api/commerce/sku-mappings")).json()
    assert [m["sku"] for m in listed["items"]] == ["UNMAPPED-ALIAS"]
    po_id, (li,) = await _po(realdb, [("Aliased", "3", "UNMAPPED-ALIAS")])
    receipt = await _receive(realdb, po_id, [li], ["3"])
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")]
    assert row.status == "synced" and row.provider_item_id == "mock-item-REAL"


async def test_an_entity_location_beats_the_org_default(realdb):
    async with realdb.sessionmaker(TENANT)() as s:
        sub = Entity(
            name=f"Sub {uuid.uuid4().hex[:4]}",
            slug=f"sub-{uuid.uuid4().hex[:6]}",
            organization_id=realdb.info(TENANT).org_id,
            is_default=False,
        )
        s.add(sub)
        await s.commit()
        sub_id = sub.id
    await _connect(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.put(
            "/api/commerce/locations",
            json={"location_id": "mock-main", "entity_locations": {str(sub_id): "mock-warehouse"}},
        )
        assert resp.status_code == 200, resp.text
        bad = await c.put(
            "/api/commerce/locations",
            json={"location_id": "mock-main", "entity_locations": {str(uuid.uuid4()): "x"}},
        )
        assert bad.status_code == 422
    po_id, (li,) = await _po(realdb, [("Racket", "1", "WPS-97")], entity_id=sub_id)
    receipt = await _receive(realdb, po_id, [li], ["1"])
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")]
    assert row.provider_location_id == "mock-warehouse"
    assert row.entity_id == sub_id


async def test_a_transient_failure_is_retried_with_backoff_up_to_the_cap(realdb, monkeypatch):
    monkeypatch.setattr(settings, "commerce_sync_max_attempts", 2)
    await _connect(realdb)
    po_id, (li,) = await _po(realdb, [("Flaky", "1", "TRANSIENT-1")])
    receipt = await _receive(realdb, po_id, [li], ["1"])
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")]
    assert (row.status, row.reason, row.retryable, row.attempts) == ("failed", "transient", True, 1)

    config = {"provider": "mock", "location_id": "mock-main"}
    db_name = realdb.info(TENANT).db_name
    org_id = realdb.info(TENANT).org_id
    # Not yet due: the backoff after one attempt is a minute.
    await commerce_sync.sweep_tenant(org_id, db_name, config)
    assert (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")].attempts == 1

    async def _age():
        async with realdb.sessionmaker(TENANT)() as s:
            await s.execute(
                update(CommerceInventorySync)
                .where(CommerceInventorySync.id == row.id)
                .values(last_attempt_at=datetime.now(UTC) - timedelta(hours=1))
            )
            await s.commit()

    await _age()
    await commerce_sync.sweep_tenant(org_id, db_name, config)
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")]
    assert row.attempts == 2
    # At the cap the sweep stops; the final failure is audited once.
    await _age()
    await commerce_sync.sweep_tenant(org_id, db_name, config)
    assert (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")].attempts == 2
    async with realdb.sessionmaker(TENANT)() as s:
        failed_audits = (
            (
                await s.execute(
                    select(AuditLog.id).where(
                        AuditLog.entity_id == uuid.UUID(receipt["id"]),
                        AuditLog.action == "commerce_inventory.failed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(failed_audits) == 1


async def test_a_provider_switch_and_a_disconnect_fail_closed(realdb, monkeypatch):
    await _connect(realdb, location=None)
    po_id, (li,) = await _po(realdb, [("Racket", "1", "WPS-97")])
    receipt = await _receive(realdb, po_id, [li], ["1"])
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")]
    db_name = realdb.info(TENANT).db_name
    org_id = realdb.info(TENANT).org_id

    # Planned for `mock`; pushed under another provider → refused, not misrouted.
    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            update(CommerceInventorySync)
            .where(CommerceInventorySync.id == row.id)
            .values(status="pending", provider="shopify")
        )
        await s.commit()
    await commerce_sync.push_syncs(
        org_id=org_id, tenant_db_name=db_name, config={"provider": "mock"}, sync_ids=[row.id]
    )
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")]
    assert (row.status, row.reason, row.retryable) == ("failed", "provider_changed", False)

    async with realdb.sessionmaker(TENANT)() as s:
        await s.execute(
            update(CommerceInventorySync)
            .where(CommerceInventorySync.id == row.id)
            .values(status="pending", provider="mock")
        )
        await s.commit()
    await commerce_sync.push_syncs(
        org_id=org_id, tenant_db_name=db_name, config=None, sync_ids=[row.id]
    )
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")]
    assert row.reason == "not_configured"


async def test_a_fraction_on_a_whole_unit_platform_is_refused(realdb, monkeypatch):
    whole = dataclasses.replace(
        mock_adapter.MockCommerceAdapter.spec, supports_fractional_quantities=False
    )
    monkeypatch.setattr(mock_adapter.MockCommerceAdapter, "spec", whole)
    await _connect(realdb)
    po_id, (li,) = await _po(realdb, [("Rope", "10", "ROPE-1")])
    receipt = await _receive(realdb, po_id, [li], ["2.5"])
    row = (await _rows(realdb, receipt["id"]))[(_line_id(receipt, li), "receive")]
    assert (row.status, row.reason, row.retryable) == ("failed", "fractional_quantity", False)
    assert mock_adapter.available("mock-item-ROPE-1", "mock-main") == Decimal("0")


# --------------------------------------------------------------------------- #
# The settings API
# --------------------------------------------------------------------------- #


async def test_settings_are_admin_only_and_patch_cannot_write_them(realdb):
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        assert (await c.get("/api/commerce/settings")).status_code == 403
        assert (
            await c.put("/api/commerce/settings", json={"settings": {"provider": "mock"}})
        ).status_code == 403
        assert (await c.get("/api/commerce/providers")).status_code == 403
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.patch(
            "/api/organization", json={"settings": {"commerce": {"provider": "mock"}}}
        )
        assert resp.status_code == 422
        unknown = await c.put("/api/commerce/settings", json={"settings": {"provider": "nope"}})
        assert unknown.status_code == 422
        assert unknown.json()["detail"]["code"] == "commerce_unknown_provider"
        providers = (await c.get("/api/commerce/providers")).json()
        assert "mock" in {p["key"] for p in providers["providers"]}


async def test_a_save_is_audited_and_test_reports_locations(realdb):
    await _connect(realdb)
    async with realdb.client(key=TENANT, role="admin") as c:
        got = (await c.get("/api/commerce/settings")).json()
        assert got["configured"] is True
        assert got["settings"]["provider"] == "mock"
        tested = (await c.post("/api/commerce/test")).json()
        assert tested["success"] is True
        assert {loc["id"] for loc in tested["locations"]} == {"mock-main", "mock-warehouse"}
        locs = (await c.get("/api/commerce/locations")).json()
        assert locs["available"] is True and locs["location_id"] == "mock-main"
        oauth = await c.get("/api/commerce/oauth/mock/authorize")
        assert oauth.status_code == 501
        assert oauth.json()["detail"]["code"] == "commerce_oauth_not_available"
        org = (await c.get("/api/organization")).json()
        assert org["settings"]["commerce"]["provider"] == "mock"
        assert (await c.delete("/api/commerce/settings")).status_code == 204
        assert (await c.get("/api/commerce/settings")).json()["configured"] is False
    async with realdb.sessionmaker(TENANT)() as s:
        actions = set(
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.action.like("organization.commerce_%"))
                )
            )
            .scalars()
            .all()
        )
    assert {
        "organization.commerce_updated",
        "organization.commerce_locations_updated",
        "organization.commerce_disconnected",
    } <= actions


async def test_the_syncs_read_is_scoped_to_the_tenant(realdb):
    await _connect(realdb)
    po_id, (li,) = await _po(realdb, [("Racket", "1", "WPS-97")])
    receipt = await _receive(realdb, po_id, [li], ["1"])
    async with realdb.client(key=TENANT, role="cfo") as c:
        resp = await c.get(f"/api/commerce/syncs?goods_receipt_id={receipt['id']}")
    assert resp.status_code == 200
    (item,) = resp.json()["items"]
    assert item["status"] == "synced" and item["quantity_delta"] == "1"
    assert item["can_retry"] is False
    async with realdb.client(key="b", role="admin") as c:
        other = await c.get(f"/api/commerce/syncs?goods_receipt_id={receipt['id']}")
        retry = await c.post(f"/api/commerce/syncs/{item['id']}/retry")
    assert other.status_code == 404
    assert retry.status_code == 404
    async with realdb.client(key=TENANT, role="cfo") as c:
        assert (await c.post(f"/api/commerce/syncs/{item['id']}/retry")).status_code == 403


# --------------------------------------------------------------------------- #
# Secrets live in the sealed store (decisions §266, §273)
# --------------------------------------------------------------------------- #


@pytest.fixture
def sealed_shop(monkeypatch):
    """A throwaway provider with one secret and one destination field, its secret
    admitted to the sealed store for this test only. Its adapter records the
    config it was built with, so a test can see which secret reached it."""
    from app.services import provider_credentials
    from app.services.commerce_adapters import registry
    from app.services.commerce_adapters.base import (
        CommerceAdapter,
        CommerceLocation,
        CommerceProviderSpec,
    )
    from app.services.commerce_adapters.catalog import field

    seen: list[dict] = []

    class SealedShop(CommerceAdapter):
        async def test_connection(self) -> None:
            seen.append(dict(self.config))

        async def list_locations(self):
            return [CommerceLocation(id="loc-1", name="Main")]

        async def resolve_sku(self, sku):
            raise NotImplementedError

        async def adjust_available(self, adjustment):
            raise NotImplementedError

    spec = CommerceProviderSpec(
        key="sealedshop",
        label="Sealed Shop",
        auth="credentials",
        fields=(field("shop_domain"), field("access_token", secret=True)),
        docs_url="https://example.invalid/docs",
        destination_keys=frozenset({"shop_domain"}),
    )
    monkeypatch.setattr(registry, "_REGISTRY", dict(registry.registered_adapters()))
    registry.register_commerce_adapter("sealedshop", spec)(SealedShop)
    monkeypatch.setitem(
        provider_credentials.SECRET_FIELDS,
        "commerce",
        provider_credentials.SECRET_FIELDS["commerce"] | {"access_token"},
    )
    return seen


async def test_commerce_secrets_are_sealed_refused_in_settings_and_dropped_on_a_move(
    realdb, sealed_shop
):
    async with realdb.client(key=TENANT, role="admin") as c:
        refused = await c.put(
            "/api/commerce/settings",
            json={"settings": {"provider": "sealedshop", "access_token": "tok-plain"}},
        )
        assert refused.status_code == 422
        assert refused.json()["detail"]["code"] == "commerce_secret_in_settings"
        assert "tok-plain" not in refused.text

        saved = await c.put(
            "/api/commerce/settings",
            json={"settings": {"provider": "sealedshop", "shop_domain": "a.example.com"}},
        )
        assert saved.status_code == 200, saved.text
        sealed = await c.put(
            "/api/organization/credentials/commerce", json={"set": {"access_token": "tok-1"}}
        )
        assert sealed.status_code == 200, sealed.text
        assert sealed.json()["commerce"] == ["access_token"]
        view = (await c.get("/api/commerce/settings")).json()
        assert view["secrets_set"] == ["access_token"]
        assert "access_token" not in view["settings"]
        assert "tok-1" not in (await c.get("/api/organization")).text

        # The saved connection is tested with the sealed secret; a form naming a
        # NEW destination is tested only with what it carries itself.
        assert (await c.post("/api/commerce/test")).json()["success"] is True
        assert sealed_shop[-1]["access_token"] == "tok-1"
        await c.post(
            "/api/commerce/test",
            json={"provider": "sealedshop", "shop_domain": "evil.example.com"},
        )
        assert "access_token" not in sealed_shop[-1]
        await c.post(
            "/api/commerce/test",
            json={"provider": "sealedshop", "shop_domain": "a.example.com"},
        )
        assert sealed_shop[-1]["access_token"] == "tok-1"

        # Pointing the shop elsewhere drops the token, audited.
        moved = await c.put(
            "/api/commerce/settings",
            json={"settings": {"provider": "sealedshop", "shop_domain": "b.example.com"}},
        )
        assert moved.status_code == 200
        assert moved.json()["secrets_set"] == []

        # Disconnect clears both halves.
        await c.put(
            "/api/organization/credentials/commerce", json={"set": {"access_token": "tok-2"}}
        )
        assert (await c.delete("/api/commerce/settings")).status_code == 204
        status = (await c.get("/api/organization/credentials")).json()
        assert status["commerce"] == []

    async with realdb.sessionmaker(TENANT)() as s:
        reasons = [
            (r.details or {}).get("reason")
            for r in (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "organization.credentials_updated")
                )
            ).scalars()
        ]
    assert "commerce_destination_changed" in reasons
    assert "commerce_disconnected" in reasons
