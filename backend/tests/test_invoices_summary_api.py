"""Real-DB coverage for the audit-summary endpoints.

Covers `GET /api/invoices/{id}/summary` + `POST .../summary/regenerate`
against two live test tenants: 200 + response shape, 404 for unknown invoice,
tenant isolation (other tenant's invoice → 404), and the central
fingerprint-freshness invariant — the summary is cached and only regenerated
when a new audit-log row moves the fingerprint.

Auth/RBAC for the new routes is auto-covered by `test_rbac.py` (the routes
carry `require_roles(...)` deps and are on neither `PUBLIC_BY_DESIGN` nor `ALTERNATE_AUTH`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog


async def _add_invoice(mk, org_id, *, number="INV-SUM-1") -> tuple[str, uuid.UUID]:
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Acme Hosting",
            amount=Decimal("4200.00"),
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return str(inv.id), inv.correlation_id


async def _add_audit_row(mk, org_id, correlation_id, *, action, details=None):
    async with mk() as s:
        s.add(
            AuditLog(
                correlation_id=correlation_id,
                organization_id=org_id,
                actor_id=None,
                action=action,
                entity_type="invoice",
                entity_id=None,
                details=details or {},
            )
        )
        await s.commit()


# ---------------------------------------------------------------------------
# shape + 404
# ---------------------------------------------------------------------------


async def test_summary_returns_shape(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    inv_id, corr = await _add_invoice(mk, org_id)
    await _add_audit_row(
        mk, org_id, corr, action="invoice.approved", details={"to_status": "approved"}
    )

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get(f"/api/invoices/{inv_id}/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["text"], str) and body["text"]
    assert "stale" in body
    assert body["stale"] is False
    assert "generated_at" in body
    # Template (no api key in local dev) — mentions the invoice number.
    assert "INV-SUM-1" in body["text"]


async def test_summary_unknown_invoice_404(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get(f"/api/invoices/{uuid.uuid4()}/summary")
    assert resp.status_code == 404


async def test_summary_tenant_isolation(realdb):
    """An invoice created in tenant A must 404 when requested with tenant B's
    session (the tenant DB simply doesn't contain it)."""
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    inv_id, _ = await _add_invoice(mk_a, org_a, number="INV-ISO-1")

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get(f"/api/invoices/{inv_id}/summary")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# fingerprint freshness (the core invariant)
# ---------------------------------------------------------------------------


async def test_summary_is_cached_until_audit_log_changes(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    inv_id, corr = await _add_invoice(mk, org_id, number="INV-FP-1")
    await _add_audit_row(mk, org_id, corr, action="invoice.uploaded", details={"to_status": "new"})

    async with realdb.client(key="a", role="ap_clerk") as c:
        first = await c.get(f"/api/invoices/{inv_id}/summary")
        assert first.status_code == 200, first.text
        gen1 = first.json()["generated_at"]

        # No new audit rows → cached, same generated_at, no regeneration.
        second = await c.get(f"/api/invoices/{inv_id}/summary")
        assert second.status_code == 200
        assert second.json()["generated_at"] == gen1

        # Append a new audit row → fingerprint moves → regenerated.
        await _add_audit_row(
            mk, org_id, corr, action="invoice.approved", details={"to_status": "approved"}
        )
        third = await c.get(f"/api/invoices/{inv_id}/summary")
        assert third.status_code == 200
        assert third.json()["generated_at"] != gen1


async def test_summary_persisted_on_meta(realdb):
    """The generated summary is cached on `invoices.meta['audit_summary']`
    with a source fingerprint."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    inv_id, corr = await _add_invoice(mk, org_id, number="INV-META-1")
    await _add_audit_row(mk, org_id, corr, action="invoice.uploaded")

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get(f"/api/invoices/{inv_id}/summary")
        assert resp.status_code == 200

    async with mk() as s:
        inv = await s.get(Invoice, uuid.UUID(inv_id))
        cached = (inv.meta or {}).get("audit_summary")
        assert cached is not None
        assert cached["text"]
        fp = cached["source_fingerprint"]
        assert fp["count"] == 1


async def test_regenerate_forces_new_generation(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    inv_id, corr = await _add_invoice(mk, org_id, number="INV-REGEN-1")
    await _add_audit_row(mk, org_id, corr, action="invoice.uploaded")

    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.get(f"/api/invoices/{inv_id}/summary")
        assert first.status_code == 200
        gen1 = first.json()["generated_at"]

        # Force regenerate even though the fingerprint hasn't changed.
        regen = await c.post(f"/api/invoices/{inv_id}/summary/regenerate", json={})
        assert regen.status_code == 200, regen.text
        assert regen.json()["generated_at"] != gen1


async def test_regenerate_forbidden_for_clerk(realdb):
    """Regenerate is manager/admin only — a clerk gets 403."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    inv_id, corr = await _add_invoice(mk, org_id, number="INV-REGEN-RBAC")
    await _add_audit_row(mk, org_id, corr, action="invoice.uploaded")

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/invoices/{inv_id}/summary/regenerate", json={})
    assert resp.status_code == 403


async def test_summary_cache_fill_never_rolls_back_a_concurrent_edit(realdb):
    """The cache fill preserves `updated_at` so opening an invoice doesn't
    bump its version. It used to do that by writing the version it READ back
    unconditionally — so an edit committed between that read and that write
    kept its new amount but lost its new version, and an approver still
    holding the old version passed the stale-approval check against figures
    they never saw (decisions §263; caught by the stale-approval e2e). The
    write is now a compare-and-swap: a row that moved is left alone."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.services.audit_summary import get_or_build_summary

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    inv_id, _ = await _add_invoice(mk, org_id, number="INV-SUM-RACE")
    inv_uuid = uuid.UUID(inv_id)

    async with mk() as reader, realdb.control_sessionmaker()() as control:
        # The summary request has read the invoice...
        # Loaded the way the endpoint loads it (`get_invoice`).
        stale = (
            await reader.execute(
                select(Invoice)
                .where(Invoice.id == inv_uuid)
                .options(selectinload(Invoice.extraction_results))
            )
        ).scalar_one()
        seen_version = stale.updated_at

        # ...when someone else's edit commits.
        async with mk() as editor:
            row = (await editor.execute(select(Invoice).where(Invoice.id == inv_uuid))).scalar_one()
            row.amount = Decimal("9500.00")
            await editor.commit()
            await editor.refresh(row)
            edited_version = row.updated_at
        assert edited_version != seen_version

        result = await get_or_build_summary(reader, control, stale)
        assert result["text"], "the summary is still served"

    async with mk() as s:
        after = (await s.execute(select(Invoice).where(Invoice.id == inv_uuid))).scalar_one()
    assert after.amount == Decimal("9500.00")
    assert after.updated_at == edited_version, "the edit's version must survive"
