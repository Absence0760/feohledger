"""`GET /api/dashboard`'s processing-time tile, against a real Postgres.

The tile (avg / median / p95 days from upload to approval and to payment) used
to be built by streaming every `invoice.approved` audit row, every completed
payment, and then every matching invoice into Python — the last as an
`Invoice.id IN (...)` with ONE BIND PARAMETER PER INVOICE. asyncpg refuses a
statement carrying more than 32 767 of them; a broad `except` around the block
swallowed the error, so every tenant past roughly 33k approved invoices saw a
tile of zeros, with nothing in any log.

It now asks Postgres for (days rounded to 0.1, invoice count) per leg and
reduces that with `analytics.processing_time_from_day_counts`. These tests pin:

* the figures equal the row-at-a-time reference on the same fixtures —
  including which approval counts for an invoice approved twice (the first),
  and that the entity selector scopes both legs;
* the request's SHAPE does not grow with the data — the same number of
  statements and no statement whose bind-parameter count tracks the number of
  invoices. That is the property the 32 767 ceiling punished, asserted
  structurally rather than by seeding 33k rows.

`docs/decisions.md` §218.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.workflow import AuditLog
from app.services.analytics import compute_processing_time_metrics
from tests.query_counter import QueryCounter

pytestmark = pytest.mark.asyncio

TENANT = "a"
BASE = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


async def _default_entity_id(session) -> uuid.UUID:
    return (
        await session.execute(select(Entity.id).where(Entity.is_default.is_(True)))
    ).scalar_one()


async def _seed(mk, org_id, *, n: int, entity_id=None, offset: int = 0) -> list[SimpleNamespace]:
    """`n` invoices, each with one or two approvals and (for most) a completed
    payment. Returns the reference rows `compute_processing_time_metrics`
    takes, built from the same timestamps the database now holds.

    Durations are chosen to hit 0.05-day ties (multiples of 72 minutes), the
    case where rounding conventions disagree."""
    ref: list[SimpleNamespace] = []
    async with mk() as s:
        ent = entity_id or await _default_entity_id(s)
        for i in range(offset, offset + n):
            created = BASE + timedelta(hours=i)
            first_approved = created + timedelta(minutes=72 * (i % 9 + 1))
            paid_at = created + timedelta(days=3, minutes=36 * i) if i % 4 else None
            inv = Invoice(
                organization_id=org_id,
                entity_id=ent,
                invoice_number=f"PT-{i:04d}",
                vendor_name="Processing Time Vendor",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.paid if paid_at else InvoiceStatus.approved,
                uploaded_by_id=None,  # seeded history, no employee author
                created_at=created,
            )
            s.add(inv)
            await s.flush()
            s.add(
                AuditLog(
                    organization_id=org_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv.id,
                    created_at=first_approved,
                )
            )
            if i % 3 == 0:
                # Re-approved later (rejected and reworked): the tile measures
                # time to the FIRST approval, as the paid leg measures time to
                # the first completed payment.
                s.add(
                    AuditLog(
                        organization_id=org_id,
                        action="invoice.approved",
                        entity_type="invoice",
                        entity_id=inv.id,
                        created_at=first_approved + timedelta(days=9),
                    )
                )
            if paid_at:
                s.add(
                    Payment(
                        invoice_id=inv.id,
                        entity_id=ent,
                        amount=Decimal("100.00"),
                        status="completed",
                        method="ach",
                        completed_at=paid_at,
                    )
                )
            ref.append(
                SimpleNamespace(created_at=created, approved_at=first_approved, paid_at=paid_at)
            )
        await s.commit()
    return ref


def _expected(ref) -> dict:
    pt = compute_processing_time_metrics(ref)
    return {
        "avg_upload_to_approval_days": float(pt.avg_upload_to_approval_days),
        "median_upload_to_approval_days": float(pt.median_upload_to_approval_days),
        "p95_upload_to_approval_days": float(pt.p95_upload_to_approval_days),
        "avg_upload_to_paid_days": float(pt.avg_upload_to_paid_days),
        "median_upload_to_paid_days": float(pt.median_upload_to_paid_days),
        "p95_upload_to_paid_days": float(pt.p95_upload_to_paid_days),
        "count_approval_leg": pt.count_approval_leg,
        "count_paid_leg": pt.count_paid_leg,
    }


# Creates a second entity: multi-entity is plan-gated (docs/decisions.md §258).
@pytest.mark.plan("scale")
async def test_processing_time_matches_the_reference_and_scopes_by_entity(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as c:
        r = await c.post("/api/entities", json={"name": "PT Sub", "slug": "pt-sub"})
        assert r.status_code == 201, r.text
        sub_id = r.json()["id"]
        default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])

    default_ref = await _seed(mk, info.org_id, n=23)
    sub_ref = await _seed(mk, info.org_id, n=11, entity_id=uuid.UUID(sub_id), offset=500)

    # Sanity on the fixture itself: the two entities' distributions differ, so
    # a scope that leaked would change the numbers rather than hide.
    assert _expected(default_ref) != _expected(default_ref + sub_ref)

    async with realdb.client(key=TENANT, role="admin") as c:
        consolidated = (await c.get("/api/dashboard")).json()["processing_time"]
        scoped = (await c.get("/api/dashboard", headers={"X-Entity-ID": default_id})).json()[
            "processing_time"
        ]

    assert consolidated == _expected(default_ref + sub_ref)
    assert scoped == _expected(default_ref)
    assert scoped["count_approval_leg"] == 23, "an invoice approved twice was counted twice"


async def test_processing_time_request_shape_does_not_grow_with_the_data(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)

    await _seed(mk, info.org_id, n=6)
    async with realdb.client(key=TENANT, role="admin") as c:
        await c.get("/api/dashboard")  # warm: first-request setup is not the dashboard
        with QueryCounter() as small:
            r_small = await c.get("/api/dashboard")

    await _seed(mk, info.org_id, n=60, offset=1000)
    async with realdb.client(key=TENANT, role="admin") as c:
        await c.get("/api/dashboard")
        with QueryCounter() as big:
            r_big = await c.get("/api/dashboard")

    assert r_small.status_code == 200, r_small.text
    assert r_big.status_code == 200, r_big.text
    assert r_small.json()["processing_time"]["count_approval_leg"] == 6
    assert r_big.json()["processing_time"]["count_approval_leg"] == 66

    assert len(small) == len(big), (
        f"GET /api/dashboard issued {len(small)} statements over 6 invoices but "
        f"{len(big)} over 66 — something now asks once per row.\n" + "\n".join(big.statements)
    )
    assert big.max_params == small.max_params, (
        "a dashboard statement's bind-parameter count grew with the data "
        f"({small.max_params} -> {big.max_params}): an IN-list built from a previous "
        "result. asyncpg refuses past 32 767 parameters, which is exactly how the "
        "processing-time tile went to zero on large tenants."
    )
    # Nothing streams the audit log into Python any more: the approval leg is
    # aggregated where it is read.
    assert not big.matching(r"^SELECT audit_log\.entity_id, audit_log\.created_at FROM audit_log")
