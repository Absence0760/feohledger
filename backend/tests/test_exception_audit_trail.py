"""The AP exception lifecycle writes append-only ``audit_log`` rows.

An exception is a control, not a note: ``duplicate`` / ``fraud_flag`` /
``line_total_mismatch`` block a payment run, and invoice approval gates on none
of them — so clearing one is the human sign-off that lets money move. The
``exceptions`` table can't be the record of that (it is mutable and single-
valued, and it is not shipped to the SOC 2 WORM store), so every lifecycle event
goes through ``services/exception_lifecycle`` into ``audit_log``.

Covered here:
  * ``exception.raised`` on the shared create chokepoint, correlation-keyed to
    the invoice so it lands on that invoice's SOX trail;
  * an invoice-less exception (Positive Pay never-issued cheque) still audits,
    self-correlated;
  * the ``payment_blocking`` flag tracks the real payment-run gate;
  * ``details`` carries no generated description text;
  * a human resolve / escalate / dismiss over the API writes its row, naming the
    actor, and a bulk action writes one per row;
  * an autonomous agent decision writes the SAME row shape, marked ``via:agent``;
  * the whole lifecycle survives a later mutation of the exception row — that's
    the point of putting it in ``audit_log``;
  * assignment is audited too, id-only;
  * every mutating endpoint is entity-scoped exactly like the reads, so a
    subsidiary's queue can't be cleared (or enumerated) from another entity.

Uses the real-Postgres harness so the rows are read back from the same tenant DB
the API writes to.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice
from app.models.workflow import AuditLog

TENANT = "a"


async def _make_invoice(mk, org_id, *, number="INV-EXCAUDIT-001"):
    inv = Invoice(
        id=uuid.uuid4(),
        organization_id=org_id,
        invoice_number=number,
        vendor_name="Globex Corporation",
        amount=Decimal("1234.56"),
        currency="USD",
        status="new",
    )
    async with mk() as s:
        s.add(inv)
        await s.commit()
    return inv


async def _audit_rows(mk, *, correlation_id=None, action=None):
    query = select(AuditLog)
    if correlation_id is not None:
        query = query.where(AuditLog.correlation_id == correlation_id)
    if action is not None:
        query = query.where(AuditLog.action == action)
    async with mk() as s:
        return (await s.execute(query.order_by(AuditLog.created_at))).scalars().all()


# ---------------------------------------------------------------------------
# exception.raised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_exception_writes_raised_audit_row_on_the_invoice_trail(realdb):
    from app.services.exception_service import create_exception

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(mk, org_id)

    async with mk() as s:
        row = await s.get(Invoice, inv.id)
        exc = await create_exception(
            s,
            exception_type="duplicate",
            severity="error",
            description="Looks like a duplicate of INV-EXCAUDIT-001 from Globex Corporation",
            organization_id=org_id,
            invoice=row,
        )
        exc_id = exc.id
        await s.commit()

    # Correlation-keyed to the INVOICE — this is what puts the row on
    # GET /api/audit/invoice/{id} next to invoice.approved / invoice.rejected.
    rows = await _audit_rows(mk, correlation_id=inv.correlation_id, action="exception.raised")
    assert len(rows) == 1
    entry = rows[0]
    assert entry.entity_type == "exception"
    assert entry.entity_id == exc_id
    assert entry.actor_id is None, "a detector opened this, not a person"

    details = entry.details
    assert details["exception_id"] == str(exc_id)
    assert details["exception_type"] == "duplicate"
    assert details["severity"] == "error"
    assert details["invoice_id"] == str(inv.id)
    assert details["new_status"] == "open"
    # `duplicate` is one of the three types that blocks a payment run.
    assert details["payment_blocking"] is True
    # The generated description can name a vendor; the exception row holds it,
    # the trail does not duplicate it.
    assert "description" not in details
    assert "Globex" not in str(details)


@pytest.mark.asyncio
async def test_non_blocking_exception_type_is_flagged_as_such(realdb):
    from app.services.exception_service import create_exception

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(mk, org_id, number="INV-EXCAUDIT-002")

    async with mk() as s:
        row = await s.get(Invoice, inv.id)
        await create_exception(
            s,
            exception_type="po_mismatch",
            severity="warning",
            description="PO total differs",
            organization_id=org_id,
            invoice=row,
        )
        await s.commit()

    rows = await _audit_rows(mk, correlation_id=inv.correlation_id, action="exception.raised")
    assert len(rows) == 1
    assert rows[0].details["payment_blocking"] is False


@pytest.mark.asyncio
async def test_payment_blocking_flag_tracks_the_real_gate():
    """The audit flag must be derived from the payment-run gate, not a copy.

    If someone adds a type to PAYMENT_BLOCKING_EXCEPTION_TYPES, the trail's
    `payment_blocking` has to follow automatically — a second hardcoded list
    would silently under-report which decisions unblocked money.
    """
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES
    from app.services.exception_lifecycle import is_payment_blocking

    for exception_type in PAYMENT_BLOCKING_EXCEPTION_TYPES:
        assert is_payment_blocking(exception_type) is True
    assert is_payment_blocking("po_mismatch") is False
    assert is_payment_blocking("totally_unknown_type") is False


@pytest.mark.asyncio
async def test_invoice_less_exception_audits_under_its_own_correlation(realdb):
    """A Positive Pay `not_on_file` cheque has no invoice to correlate to — the
    exception's own id groups its lifecycle rows instead of losing them."""
    from app.services.exception_service import create_exception

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)

    async with mk() as s:
        exc = await create_exception(
            s,
            exception_type="fraud_flag",
            severity="error",
            description="Cheque 9911 cleared but was never issued",
            organization_id=org_id,
        )
        exc_id = exc.id
        await s.commit()

    rows = await _audit_rows(mk, correlation_id=exc_id, action="exception.raised")
    assert len(rows) == 1
    assert rows[0].entity_id == exc_id
    assert rows[0].details["invoice_id"] is None
    assert rows[0].details["payment_blocking"] is True


# ---------------------------------------------------------------------------
# human decisions over the API
# ---------------------------------------------------------------------------


async def _open_exception(mk, org_id, invoice, *, exception_type="duplicate"):
    from app.services.exception_service import create_exception

    async with mk() as s:
        row = await s.get(Invoice, invoice.id)
        exc = await create_exception(
            s,
            exception_type=exception_type,
            severity="error",
            description="detector output",
            organization_id=org_id,
            invoice=row,
        )
        exc_id = exc.id
        await s.commit()
    return exc_id


@pytest.mark.asyncio
async def test_resolving_over_the_api_audits_the_decision_and_names_the_actor(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(mk, org_id, number="INV-EXCAUDIT-010")
    exc_id = await _open_exception(mk, org_id, inv)
    actor_id = realdb.info(TENANT).users["admin"]

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Confirmed distinct PO; not a duplicate."},
        )
        assert res.status_code == 200, res.text

    rows = await _audit_rows(mk, correlation_id=inv.correlation_id, action="exception.resolved")
    assert len(rows) == 1
    entry = rows[0]
    assert entry.entity_type == "exception"
    assert entry.entity_id == exc_id
    assert entry.actor_id == actor_id, "the human who cleared the payment block is named"

    details = entry.details
    assert details["old_status"] == "open"
    assert details["new_status"] == "resolved"
    assert details["payment_blocking"] is True
    assert details["resolution"] == "Confirmed distinct PO; not a duplicate."
    assert details["time_to_resolution_seconds"] >= 0
    assert "via" not in details, "a human decision is not marked as agent-made"


@pytest.mark.asyncio
async def test_escalate_then_resolve_keeps_both_deciders_in_the_trail(realdb):
    """The mutable exceptions row only remembers the LAST decision — this is
    exactly why the trail lives in audit_log."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(mk, org_id, number="INV-EXCAUDIT-011")
    exc_id = await _open_exception(mk, org_id, inv, exception_type="fraud_flag")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "escalate", "resolution": "Bank details changed last week."},
        )
        assert first.status_code == 200, first.text
    async with realdb.client(key=TENANT, role="admin") as c:
        second = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "dismiss", "resolution": "Verified by callback to a known number."},
        )
        assert second.status_code == 200, second.text

    async with mk() as s:
        from app.models.exception import Exception as APException

        row = await s.get(APException, exc_id)
        # The row itself has lost the escalation entirely.
        assert row.status == "dismissed"
        assert row.resolution == "Verified by callback to a known number."

    escalations = await _audit_rows(
        mk, correlation_id=inv.correlation_id, action="exception.escalated"
    )
    dismissals = await _audit_rows(
        mk, correlation_id=inv.correlation_id, action="exception.dismissed"
    )
    assert len(escalations) == 1
    assert escalations[0].details["resolution"] == "Bank details changed last week."
    assert escalations[0].actor_id == realdb.info(TENANT).users["ap_manager"]
    # Escalation is non-terminal, so the SLA clock is still running.
    assert "time_to_resolution_seconds" not in escalations[0].details
    assert len(dismissals) == 1
    assert dismissals[0].details["old_status"] == "escalated"
    assert dismissals[0].actor_id == realdb.info(TENANT).users["admin"]


@pytest.mark.asyncio
async def test_bulk_resolve_audits_every_row(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv_a = await _make_invoice(mk, org_id, number="INV-EXCAUDIT-020")
    inv_b = await _make_invoice(mk, org_id, number="INV-EXCAUDIT-021")
    exc_a = await _open_exception(mk, org_id, inv_a)
    exc_b = await _open_exception(mk, org_id, inv_b)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            "/api/exceptions/bulk/resolve",
            json={
                "ids": [str(exc_a), str(exc_b)],
                "action": "dismiss",
                "resolution": "Semantic-dedup tuning pass",
            },
        )
        assert res.status_code == 200, res.text
        assert res.json()["updated"] == 2

    for inv, exc_id in ((inv_a, exc_a), (inv_b, exc_b)):
        rows = await _audit_rows(
            mk, correlation_id=inv.correlation_id, action="exception.dismissed"
        )
        assert len(rows) == 1, "each bulk row gets its own audit entry"
        assert rows[0].entity_id == exc_id


@pytest.mark.asyncio
async def test_resolution_note_is_capped_in_the_audit_row(realdb):
    """The exception row keeps the full note; the immutable JSONB is bounded."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(mk, org_id, number="INV-EXCAUDIT-030")
    exc_id = await _open_exception(mk, org_id, inv)
    long_note = "x" * 2000

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": long_note},
        )
        assert res.status_code == 200, res.text

    rows = await _audit_rows(mk, correlation_id=inv.correlation_id, action="exception.resolved")
    assert len(rows[0].details["resolution"]) == 500

    async with mk() as s:
        from app.models.exception import Exception as APException

        assert len((await s.get(APException, exc_id)).resolution) == 2000


# ---------------------------------------------------------------------------
# agent decisions take the same path
# ---------------------------------------------------------------------------


async def _seed_agent_po_mismatch(mk, org_id, *, invoice_amount, po_total, number):
    """An invoice in ready_for_review with a live PO to re-match against, a
    workflow instance, and an OPEN po_mismatch exception."""
    from app.models.invoice import InvoiceStatus
    from app.models.procurement import PurchaseOrder
    from app.services.exception_service import create_exception
    from app.services.workflow_engine import create_workflow_instance

    po_number = f"PO-{number}"
    async with mk() as s:
        s.add(
            PurchaseOrder(
                organization_id=org_id,
                po_number=po_number,
                total=po_total,
                # The invoice's own currency: an agent only adjusts against a PO
                # proven to be in it (decisions §197).
                currency="USD",
                status="open",
            )
        )
        await s.commit()

        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Acme Hosting",
            amount=invoice_amount,
            status=InvoiceStatus.ready_for_review,
            po_number=po_number,
            po_match={"status": "matched", "po_total": str(po_total)},
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        await create_workflow_instance(s, inv)
        exc = await create_exception(
            s,
            exception_type="po_mismatch",
            severity="warning",
            description="Amount mismatch vs PO",
            organization_id=org_id,
            invoice=inv,
        )
        await s.commit()
        return inv.id, inv.correlation_id, exc.id


@pytest.mark.asyncio
async def test_agent_auto_resolution_writes_the_same_row_marked_via_agent(realdb):
    from app.models.exception import Exception as APException
    from app.services.exception_agents.base import ACTION_AUTO_RESOLVED
    from app.services.exception_agents.coordinator import run_agent

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    actor_id = realdb.info(TENANT).users["ap_manager"]

    # 1000.00 vs a 1010.00 PO → 0.99% variance, inside the balanced band.
    _inv_id, corr, exc_id = await _seed_agent_po_mismatch(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_total=Decimal("1010.00"),
        number="INV-EXCAUDIT-040",
    )

    async with mk() as s:
        exc = await s.get(APException, exc_id)
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings={"exception_agents": {"autonomy_level": "balanced"}},
            actor_roles={"ap_manager"},
        )
        assert result.decision.action_taken == ACTION_AUTO_RESOLVED

    rows = await _audit_rows(mk, correlation_id=corr, action="exception.resolved")
    assert len(rows) == 1, "an agent resolution is auditable exactly like a human one"
    details = rows[0].details
    assert details["via"] == "agent"
    assert details["new_status"] == "resolved"
    # The row still names the human who triggered the run — the agent has no
    # identity of its own to hold accountable.
    assert rows[0].actor_id == actor_id


@pytest.mark.asyncio
async def test_agent_escalation_is_audited_and_leaves_its_reason_on_the_row(realdb):
    from app.models.exception import Exception as APException
    from app.services.exception_agents.base import ACTION_ESCALATED
    from app.services.exception_agents.coordinator import run_agent

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    actor_id = realdb.info(TENANT).users["ap_manager"]

    _inv_id, corr, exc_id = await _seed_agent_po_mismatch(
        mk,
        org_id,
        invoice_amount=Decimal("1000.00"),
        po_total=Decimal("1010.00"),
        number="INV-EXCAUDIT-041",
    )

    async with mk() as s:
        exc = await s.get(APException, exc_id)
        # conservative == "off": the threshold is unreachable, so everything
        # escalates regardless of confidence.
        result = await run_agent(
            s,
            exception=exc,
            actor_id=actor_id,
            org_settings={"exception_agents": {"autonomy_level": "conservative"}},
            actor_roles={"ap_manager"},
        )
        assert result.decision.action_taken == ACTION_ESCALATED

    rows = await _audit_rows(mk, correlation_id=corr, action="exception.escalated")
    assert len(rows) == 1
    assert rows[0].details["via"] == "agent"
    assert rows[0].details["new_status"] == "escalated"

    async with mk() as s:
        row = await s.get(APException, exc_id)
        assert row.status == "escalated"
        # The human picking this up now reads WHY in the queue itself, not only
        # in the AgentDecision log — but nothing claims the row was resolved.
        assert row.resolution
        assert row.resolved_by is None
        assert row.resolved_at is None
        assert row.time_to_resolution_seconds is None


# ---------------------------------------------------------------------------
# assignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assignment_is_audited_by_id_only(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(mk, org_id, number="INV-EXCAUDIT-050")
    exc_id = await _open_exception(mk, org_id, inv)
    assignee = realdb.info(TENANT).users["ap_manager"]

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/exceptions/{exc_id}/assign", json={"user_id": str(assignee)})
        assert res.status_code == 200, res.text
        unassign = await c.post(f"/api/exceptions/{exc_id}/assign", json={"user_id": None})
        assert unassign.status_code == 200, unassign.text

    rows = await _audit_rows(mk, correlation_id=inv.correlation_id, action="exception.assigned")
    assert len(rows) == 2
    assert rows[0].details["assigned_to_user_id"] == str(assignee)
    assert rows[1].details["assigned_to_user_id"] is None
    # Names are resolvable from the control plane; the trail keeps ids.
    assert all("assigned_to" not in r.details for r in rows)
    assert all(r.actor_id == realdb.info(TENANT).users["admin"] for r in rows)


# ---------------------------------------------------------------------------
# entity scoping on the MUTATING endpoints
# ---------------------------------------------------------------------------


async def _second_entity(c) -> str:
    slug = f"exc-audit-{uuid.uuid4().hex[:8]}"
    res = await c.post("/api/entities", json={"name": "Sub Co", "slug": slug})
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _stamp_entity(mk, exc_id, entity_id):
    from app.models.exception import Exception as APException

    async with mk() as s:
        row = await s.get(APException, exc_id)
        row.entity_id = uuid.UUID(entity_id)
        await s.commit()


@pytest.mark.asyncio
async def test_resolve_and_assign_are_entity_scoped(realdb):
    """An exception belonging to another subsidiary must be unreachable by id
    from the selected entity — the same opaque 404 the detail read gives, not a
    silent cross-entity write. `duplicate` blocks a payment run, so a
    cross-entity clear would move money the caller can't even see."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(mk, org_id, number="INV-EXCAUDIT-060")
    exc_id = await _open_exception(mk, org_id, inv)

    async with realdb.client(key=TENANT, role="admin") as c:
        other = await _second_entity(c)
        default_id = next(e["id"] for e in (await c.get("/api/entities")).json() if e["is_default"])
        await _stamp_entity(mk, exc_id, other)

        headers = {"X-Entity-ID": default_id}
        blocked = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "not mine to clear"},
            headers=headers,
        )
        assert blocked.status_code == 404
        # Indistinguishable from the (already-scoped) detail read's answer.
        assert (await c.get(f"/api/exceptions/{exc_id}", headers=headers)).status_code == 404

        blocked_assign = await c.post(
            f"/api/exceptions/{exc_id}/assign",
            json={"user_id": str(realdb.info(TENANT).users["ap_manager"])},
            headers=headers,
        )
        assert blocked_assign.status_code == 404

        bulk = await c.post(
            "/api/exceptions/bulk/resolve",
            json={"ids": [str(exc_id)], "action": "dismiss", "resolution": "sweep"},
            headers=headers,
        )
        assert bulk.status_code == 200, bulk.text
        body = bulk.json()
        assert body["updated"] == 0
        assert body["skipped"] == [{"id": str(exc_id), "reason": "not_found"}]

        # Selecting the owning entity works — the scope is a filter, not a block.
        allowed = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "cleared by the owning entity"},
            headers={"X-Entity-ID": other},
        )
        assert allowed.status_code == 200, allowed.text

    rows = await _audit_rows(mk, correlation_id=inv.correlation_id, action="exception.resolved")
    assert len(rows) == 1, "only the in-scope decision happened, and it is audited"
