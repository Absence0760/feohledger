"""The DSAR export's cap contract and its complete bundle (docs/decisions.md §369).

``POST /api/privacy/dsar`` caps every high-volume section at
``privacy_export.MAX_EXPORT_ROWS`` — a vendor's invoices and payments included,
which until §369 were returned whole in a response built in memory on the event
loop. A cap alone would shortchange the subject (GDPR Art. 15 owes every row), so
the response says whether it is complete and points at
``GET /api/privacy/dsar/{request_id}/bundle``, which streams every section
uncapped. Covered here:

* a large vendor's export is complete — every invoice and payment reaches the
  downloaded bundle, at the real cap;
* the contract: a truncated inline section names itself in
  ``truncated_sections``, keeps its true total, and the full bundle carries the
  same sections with ``total == returned`` and ``truncated: false``;
* the download writes its own PII-free audit row, after the body is rendered;
* auth: admin only, tenant-scoped, a DSAR export's id only;
* the streamed renderer itself (no DB): chunked, uncapped, parseable.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services import privacy_export


async def _seed_vendor(tenant_mk, org_id, *, invoices: int) -> tuple[uuid.UUID, set[str], set[str]]:
    """A vendor with ``invoices`` invoices, each with one payment.

    Returns the vendor id and the invoice / payment id sets the bundle must hold.
    """
    vendor_id = uuid.uuid4()
    invoice_ids: set[str] = set()
    payment_ids: set[str] = set()
    async with tenant_mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=org_id,
                name="Volume Supplier Ltd",
                code=f"V-VOL-{vendor_id.hex[:6]}",
                email="ap@volumesupplier.test",
                tax_id="98-7654321",
                bank_details={"account": "000999888", "routing": "021000021"},
                status="active",
            )
        )
        rows = []
        for n in range(invoices):
            iid = uuid.uuid4()
            invoice_ids.add(str(iid))
            rows.append(
                Invoice(
                    id=iid,
                    correlation_id=uuid.uuid4(),
                    organization_id=org_id,
                    invoice_number=f"VOL-{n:05d}",
                    vendor_name="Volume Supplier Ltd",
                    vendor_id=vendor_id,
                    amount=Decimal("10.01"),
                    currency="USD",
                    status=InvoiceStatus.approved,
                )
            )
        s.add_all(rows)
        await s.flush()
        payments = []
        for inv in rows:
            pid = uuid.uuid4()
            payment_ids.add(str(pid))
            payments.append(
                Payment(
                    id=pid,
                    invoice_id=inv.id,
                    amount=Decimal("10.01"),
                    method="ach",
                    status="completed",
                )
            )
        s.add_all(payments)
        await s.commit()
    return vendor_id, invoice_ids, payment_ids


async def _export(c, vendor_id: uuid.UUID) -> dict:
    resp = await c.post(
        "/api/privacy/dsar",
        json={"subject_type": "vendor_contact", "identifier": str(vendor_id)},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# A large vendor's export is complete
# ---------------------------------------------------------------------------


async def test_a_large_vendors_export_is_complete_through_the_full_bundle(realdb):
    """Past the real cap, the inline response is cut and SAYS so, and the
    download holds every invoice and payment — not a tally of the rest."""
    over = privacy_export.MAX_EXPORT_ROWS + 25
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, invoice_ids, payment_ids = await _seed_vendor(tenant_mk, org_id, invoices=over)

    async with realdb.client(key="a", role="admin") as c:
        body = await _export(c, vendor_id)
        data = body["data"]
        assert body["complete"] is False
        assert {"related_invoices", "related_payments"} <= set(body["truncated_sections"])
        for section in ("related_invoices", "related_payments"):
            assert data[section]["total"] == over, section
            assert data[section]["returned"] == privacy_export.MAX_EXPORT_ROWS, section
            assert data[section]["truncated"] is True, section
            assert body["record_counts"][section] == over, section
        # The legacy tallies are the true totals, never the capped page length.
        assert data["counts"]["invoices"] == over
        assert data["counts"]["payments"] == over
        assert body["full_bundle_url"] == f"/api/privacy/dsar/{body['request_id']}/bundle"

        full = await c.get(body["full_bundle_url"])

    assert full.status_code == 200, full.text
    assert full.headers["content-type"].startswith("application/json")
    assert "attachment" in full.headers["content-disposition"]
    assert full.headers["cache-control"] == "no-store"
    bundle = full.json()
    assert bundle["complete"] is True
    assert bundle["request_id"] == body["request_id"]
    got_invoices = bundle["data"]["related_invoices"]
    got_payments = bundle["data"]["related_payments"]
    assert {i["id"] for i in got_invoices["items"]} == invoice_ids
    assert {p["id"] for p in got_payments["items"]} == payment_ids
    assert got_invoices["total"] == got_invoices["returned"] == over
    assert got_invoices["truncated"] is False
    assert got_invoices["items"][0]["amount"] == "10.01"  # string-Decimal, as inline
    assert bundle["data"]["counts"]["invoices"] == over
    assert bundle["record_counts"]["related_invoices"] == over
    assert bundle["record_counts"]["related_payments"] == over


# ---------------------------------------------------------------------------
# The cap contract
# ---------------------------------------------------------------------------


async def test_every_inline_section_is_capped_and_the_full_bundle_is_not(realdb, monkeypatch):
    """With the cap at 2 and the stream chunk at 2, every high-volume section of
    a five-invoice vendor is cut inline, named in ``truncated_sections``, and
    whole in the download — which streams in more than one cursor chunk."""
    monkeypatch.setattr(privacy_export, "MAX_EXPORT_ROWS", 2)
    monkeypatch.setattr(privacy_export, "STREAM_CHUNK_ROWS", 2)
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, invoice_ids, payment_ids = await _seed_vendor(tenant_mk, org_id, invoices=5)

    async with realdb.client(key="a", role="admin") as c:
        body = await _export(c, vendor_id)
        full = await c.get(body["full_bundle_url"])
    assert full.status_code == 200, full.text
    bundle = full.json()

    inline = body["data"]
    assert inline["related_invoices"]["items"] == bundle["data"]["related_invoices"]["items"][:2]
    assert set(body["truncated_sections"]) == {"related_invoices", "related_payments"}
    # The download has the inline response's shape, section for section.
    assert set(bundle["data"]) == set(inline)
    assert bundle["record_counts"] == body["record_counts"]
    for section in body["truncated_sections"]:
        got = bundle["data"][section]
        assert got["truncated"] is False, section
        assert got["total"] == got["returned"] == len(got["items"]) == 5, section
    assert {p["id"] for p in bundle["data"]["related_payments"]["items"]} == payment_ids
    assert {i["id"] for i in bundle["data"]["related_invoices"]["items"]} == invoice_ids


async def test_a_small_subject_is_complete_inline(realdb):
    """Under the cap nothing is cut: ``complete`` is true, no section is named,
    and the bundle URL is still offered (it is the same data, as a file)."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor(tenant_mk, org_id, invoices=2)

    async with realdb.client(key="a", role="admin") as c:
        body = await _export(c, vendor_id)
    assert body["complete"] is True
    assert body["truncated_sections"] == []
    assert body["full_bundle_url"].endswith(f"/{body['request_id']}/bundle")
    for section in ("related_invoices", "related_payments", "contracts", "virtual_cards"):
        assert body["data"][section]["truncated"] is False, section
    assert body["data"]["documents"]["truncated"] is False


async def test_the_full_bundle_is_masked(realdb):
    """The download never carries a full account number — an unmasked
    disclosure stays behind the POST's permission + step-up gate."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor(tenant_mk, org_id, invoices=1)

    async with realdb.client(key="a", role="admin") as c:
        body = await _export(c, vendor_id)
        full = await c.get(body["full_bundle_url"])
    assert full.status_code == 200, full.text
    assert "000999888" not in full.text
    bundle = full.json()
    assert bundle["banking_disclosure"] == "masked"
    assert bundle["data"]["vendor"]["banking_disclosure"] == "masked"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def test_the_download_writes_its_own_pii_free_audit_row(realdb, monkeypatch):
    monkeypatch.setattr(privacy_export, "MAX_EXPORT_ROWS", 1)
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    admin_id = realdb.info("a").users["admin"]
    vendor_id, _, _ = await _seed_vendor(tenant_mk, org_id, invoices=3)

    async with realdb.client(key="a", role="admin") as c:
        body = await _export(c, vendor_id)
        full = await c.get(body["full_bundle_url"])
    assert full.status_code == 200, full.text
    request_id = uuid.UUID(body["request_id"])

    async with tenant_mk() as s:
        export_row = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "privacy.dsar_export",
                    AuditLog.entity_id == request_id,
                )
            )
        ).scalar_one()
        # The routine export row records whether it was whole, by section NAME.
        assert export_row.details["complete"] is False
        assert "related_invoices" in export_row.details["truncated_sections"]

        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "privacy.dsar_export.full_bundle",
                        AuditLog.entity_id == request_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id == admin_id
    assert row.entity_type == "data_subject_request"
    assert row.details["subject_type"] == "vendor_contact"
    assert row.details["subject_id"] == str(vendor_id)
    assert row.details["banking_disclosure"] == "masked"
    assert row.details["record_counts"]["related_invoices"] == 3
    rendered = str(row.details)
    for pii in ("volumesupplier", "98-7654321", "000999888", "Volume Supplier"):
        assert pii not in rendered, pii


async def test_each_download_is_audited(realdb):
    """Two downloads of one request are two disclosures, so two rows."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor(tenant_mk, org_id, invoices=1)

    async with realdb.client(key="a", role="admin") as c:
        body = await _export(c, vendor_id)
        for _ in range(2):
            assert (await c.get(body["full_bundle_url"])).status_code == 200

    async with tenant_mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "privacy.dsar_export.full_bundle",
                        AuditLog.entity_id == uuid.UUID(body["request_id"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_the_download_is_admin_only(realdb):
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor(tenant_mk, org_id, invoices=1)
    async with realdb.client(key="a", role="admin") as c:
        url = (await _export(c, vendor_id))["full_bundle_url"]

    for role in ("ap_clerk", "ap_manager", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            resp = await c.get(url)
        assert resp.status_code == 403, (role, resp.status_code)
        assert "Volume Supplier" not in resp.text

    async with realdb.client(key="a", role=None) as c:
        resp = await c.get(url)
    assert resp.status_code == 401


async def test_another_tenants_request_id_is_not_found(realdb):
    """Tenant B's admin cannot pull tenant A's bundle by its request id."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor(tenant_mk, org_id, invoices=1)
    async with realdb.client(key="a", role="admin") as c:
        url = (await _export(c, vendor_id))["full_bundle_url"]

    async with realdb.client(key="b", role="admin") as c:
        resp = await c.get(url)
    assert resp.status_code == 404
    assert "Volume Supplier" not in resp.text


async def test_only_a_dsar_export_id_opens_a_bundle(realdb):
    """An erasure request's id, or an id that names nothing, is the same 404."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor(tenant_mk, org_id, invoices=1)

    async with realdb.client(key="a", role="admin") as c:
        erased = await c.post(
            "/api/privacy/erasure",
            json={"subject_type": "vendor_contact", "identifier": str(vendor_id), "confirm": True},
        )
        assert erased.status_code == 200, erased.text
        erasure_id = erased.json()["request_id"]

        for rid in (erasure_id, str(uuid.uuid4())):
            resp = await c.get(f"/api/privacy/dsar/{rid}/bundle")
            assert resp.status_code == 404, rid


# ---------------------------------------------------------------------------
# The streamed renderer, without a database
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, rows):
        self._rows = rows

    def __aiter__(self):
        async def gen():
            for r in self._rows:
                yield r

        return gen()


class _FakeTenantDB:
    """Answers ``stream()`` only: the renderer must read sections off a cursor,
    never through ``execute()`` (which would materialise every row)."""

    def __init__(self, rows: int):
        self.rows = rows
        self.streamed: list = []

    async def stream(self, query):
        self.streamed.append(query)
        return _FakeStream(
            [SimpleNamespace(id=uuid.uuid4(), amount=Decimal("1.10")) for _ in range(self.rows)]
        )


async def test_the_renderer_streams_every_row_in_chunks(monkeypatch):
    monkeypatch.setattr(privacy_export, "STREAM_CHUNK_ROWS", 10)
    sections = [
        privacy_export._Section(
            name="related_invoices",
            query=select(Invoice.id),
            serialize=lambda r: {"id": str(r.id), "amount": privacy_export._jsonable(r.amount)},
        )
    ]
    plan = privacy_export._BundlePlan(
        head={"vendor": {"name": "x"}, "portal_users": []},
        documents=[],
        sections=sections,
        tail=lambda totals: {"counts": {"invoices": totals["related_invoices"]}},
    )

    async def fake_plan(**_kw):
        return plan

    monkeypatch.setattr(privacy_export, "_plan_bundle", fake_plan)
    db = _FakeTenantDB(rows=95)
    summary: dict = {}
    chunks = [
        c
        async for c in privacy_export.stream_full_dsar_bundle(
            subject_type="vendor_contact",
            subject_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            control_db=None,
            tenant_db=db,
            envelope={"request_id": "r-1"},
            summary=summary,
        )
    ]
    # Rendered in bounded pieces, not one string.
    assert len(chunks) > 95 // 10
    bundle = json.loads("".join(chunks))
    assert bundle["complete"] is True
    assert bundle["banking_disclosure"] == "masked"
    section = bundle["data"]["related_invoices"]
    assert section["total"] == section["returned"] == len(section["items"]) == 95
    assert section["items"][0]["amount"] == "1.10"
    assert bundle["data"]["counts"] == {"invoices": 95}
    assert bundle["record_counts"]["related_invoices"] == 95
    assert summary["record_counts"] == bundle["record_counts"]
    # Read through the cursor, with a fetch size set.
    assert len(db.streamed) == 1
    assert db.streamed[0].get_execution_options()["yield_per"] == 10


async def test_a_run_that_dies_part_way_never_parses(monkeypatch):
    """The closing braces come last, so a truncated body is not a bundle."""
    sections = [
        privacy_export._Section(
            name="related_invoices",
            query=select(Invoice.id),
            serialize=lambda r: {"id": str(r.id)},
        )
    ]
    plan = privacy_export._BundlePlan(head={}, documents=[], sections=sections)

    async def fake_plan(**_kw):
        return plan

    monkeypatch.setattr(privacy_export, "_plan_bundle", fake_plan)
    chunks = []
    async for c in privacy_export.stream_full_dsar_bundle(
        subject_type="vendor_contact",
        subject_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        control_db=None,
        tenant_db=_FakeTenantDB(rows=3),
        envelope={},
    ):
        chunks.append(c)
    whole = "".join(chunks)
    json.loads(whole)
    with pytest.raises(json.JSONDecodeError):
        json.loads("".join(chunks[:-1]))
