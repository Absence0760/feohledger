"""PO-match exceptions block payment only for over-billing, and lift on their own.

`po_mismatch` and `quality_hold` are payment-blocking
(`api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES`). Two consequences are pinned
here against real Postgres rows:

* **Only over-billing holds.** An invoice billing the share of a split or
  blanket PO that has actually arrived is partial billing and must stay
  payable; one billing beyond what arrived must not be.
  `MatchResult.billed_beyond_receipt` is the test, measured in exact Decimal
  against the received slice of the PO.
* **A refresh that no longer finds the problem closes the row**
  (`invoice_warnings._close_cleared_po_exceptions`), with an append-only
  `exception.resolved` audit row and no actor — otherwise a short receipt that
  later completed would hold the payable until someone noticed a stale queue
  row. The one exception is the segregation line: past approval, re-pointing an
  invoice at a different PO does not clear a hold raised against the old one.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.procurement import GoodsReceipt, GRLineItem, POLineItem, PurchaseOrder
from app.models.quality_inspection import QualityInspection
from app.models.workflow import AuditLog
from app.services.invoice_warnings import refresh_warnings
from app.services.payment_runs import blocked_invoice_ids
from app.services.po_matching import match_invoice_to_po

TENANT = "a"


async def _refresh(s, inv, org_settings=None):
    """Refresh with org settings, as every in-app caller that decides does —
    without them the refresh may raise findings but never clears one."""
    await refresh_warnings(s, inv, org_settings={} if org_settings is None else org_settings)


async def _default_entity_id(session):
    return (await session.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _add_po(session, org_id, entity_id, *, po_number, total, qty):
    po = PurchaseOrder(
        po_number=po_number,
        total=Decimal(total),
        currency="USD",
        status="open",
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(po)
    await session.flush()
    session.add(POLineItem(po_id=po.id, description="Widget", quantity=Decimal(qty)))
    await session.flush()
    return po


async def _add_gr(session, org_id, entity_id, po_id, *, received):
    gr = GoodsReceipt(
        gr_number=f"GR-{uuid.uuid4().hex[:8]}",
        po_id=po_id,
        status="received",
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(gr)
    await session.flush()
    session.add(GRLineItem(gr_id=gr.id, description="Widget", quantity_received=Decimal(received)))
    await session.flush()
    return gr


async def _add_invoice(session, org_id, entity_id, *, po_number, amount, status=None):
    inv = Invoice(
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        vendor_name="Acme",
        amount=Decimal(amount),
        currency="USD",
        po_number=po_number,
        status=status or InvoiceStatus.ready_for_review,
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(inv)
    await session.flush()
    return inv


async def _rows(session, invoice_id, exception_type="po_mismatch"):
    return (
        (
            await session.execute(
                select(APException)
                .where(
                    APException.invoice_id == invoice_id,
                    APException.exception_type == exception_type,
                )
                .order_by(APException.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _resolved_audit(session, correlation_id):
    return (
        (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.correlation_id == correlation_id,
                    AuditLog.action == "exception.resolved",
                )
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------- #
# Matcher: billing measured against what arrived
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_split_po_first_delivery_billed_on_its_own_is_payable(realdb):
    """Four of ten units in, invoice for four tenths of the PO. The amount leg
    reads -60 % (it compares with the whole PO) and the receipt leg reads
    partial — and neither is over-billing, so no row may hold it."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-SPLIT-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", qty="10")
        await _add_gr(s, org_id, ent, po.id, received="4")
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="400.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv)
        assert match.status == "mismatch"  # -60 % against the whole PO
        assert match.received_value == Decimal("400")
        assert match.billed_beyond_receipt is False

        await _refresh(s, inv)
        await s.commit()

        assert await _rows(s, inv.id) == []
        assert await blocked_invoice_ids(s, [inv.id]) == set()
        codes = [w.get("code") for w in inv.warnings or []]
        assert "po_amount_variance" in codes  # the reviewer still sees it


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "beyond"),
    [
        ("420.00", False),  # +5 % over the 400 that arrived: inside tolerance
        ("420.01", True),  # one cent past the band
    ],
)
async def test_billed_beyond_receipt_uses_the_exact_tolerance_boundary(realdb, amount, beyond):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-BND-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1000.00", qty="10")
        await _add_gr(s, org_id, ent, po.id, received="4")
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount=amount)
        await s.commit()

        match = await match_invoice_to_po(s, inv)
    assert match.billed_beyond_receipt is beyond


# --------------------------------------------------------------------------- #
# Auto-close when the evidence catches up
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hold_lifts_when_the_rest_of_the_delivery_arrives(realdb):
    """Full PO billed with six of ten units in: held. (Not a round figure, so
    the round-amount fraud rule stays out of the payable's blocking set.) The remaining four arrive
    and the next refresh closes the row — audited, actor-less — and the invoice
    is payable again."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-LIFT-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1037.00", qty="10")
        await _add_gr(s, org_id, ent, po.id, received="6")
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1037.00")
        await s.commit()

        await _refresh(s, inv)
        await s.commit()
        (row,) = await _rows(s, inv.id)
        assert row.status == "open"
        assert row.description_code == "po_partial_receipt"
        assert await blocked_invoice_ids(s, [inv.id]) == {inv.id}

        await _add_gr(s, org_id, ent, po.id, received="4")
        await s.commit()
        await _refresh(s, inv)
        await s.commit()

        (row,) = await _rows(s, inv.id)
        assert row.status == "resolved"
        assert row.resolved_by == "PO match"
        assert await blocked_invoice_ids(s, [inv.id]) == set()

        (audit,) = await _resolved_audit(s, inv.correlation_id)
        assert audit.actor_id is None
        assert audit.entity_id == row.id
        assert audit.details["via"] == "po_match_refresh"
        assert audit.details["old_status"] == "open"
        assert audit.details["payment_blocking"] is True


@pytest.mark.asyncio
async def test_an_escalated_hold_lifts_too(realdb):
    """Escalation hands the decision to someone else; it does not make a finding
    the matcher no longer reports any truer."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-ESC-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        await _add_po(s, org_id, ent, po_number=number, total="1000.00", qty="10")
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1500.00")
        await s.commit()
        await _refresh(s, inv)
        (row,) = await _rows(s, inv.id)
        row.status = "escalated"
        await s.commit()

        inv.amount = Decimal("1000.00")  # corrected before approval
        await _refresh(s, inv)
        await s.commit()

        (row,) = await _rows(s, inv.id)
        assert row.status == "resolved"


@pytest.mark.asyncio
async def test_quality_hold_lifts_when_a_newer_inspection_passes(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-QH-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number=number, total="1037.00", qty="10")
        gr = await _add_gr(s, org_id, ent, po.id, received="10")
        s.add(
            QualityInspection(
                inspection_number=f"QI-{uuid.uuid4().hex[:6]}",
                po_id=po.id,
                gr_id=gr.id,
                result="fail",
                inspected_date=date(2026, 10, 1),
                organization_id=org_id,
                entity_id=ent,
            )
        )
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1037.00")
        await s.commit()

        await _refresh(s, inv)
        await s.commit()
        (row,) = await _rows(s, inv.id, "quality_hold")
        assert row.status == "open"

        s.add(
            QualityInspection(
                inspection_number=f"QI-{uuid.uuid4().hex[:6]}",
                po_id=po.id,
                gr_id=gr.id,
                result="pass",
                inspected_date=date(2026, 10, 2),
                source="qms",
                organization_id=org_id,
                entity_id=ent,
            )
        )
        await s.commit()
        await _refresh(s, inv)
        await s.commit()

        (row,) = await _rows(s, inv.id, "quality_hold")
        assert row.status == "resolved"
        assert await blocked_invoice_ids(s, [inv.id]) == set()


@pytest.mark.asyncio
async def test_a_hold_whose_finding_persists_stays_open(realdb):
    """Re-running the refresh on an unchanged over-billed invoice must neither
    close the row nor open a second one."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-KEEP-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        await _add_po(s, org_id, ent, po_number=number, total="1000.00", qty="10")
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1500.00")
        await s.commit()
        await _refresh(s, inv)
        await s.commit()
        await _refresh(s, inv)
        await s.commit()

        rows = await _rows(s, inv.id)
        assert [r.status for r in rows] == ["open"]


# --------------------------------------------------------------------------- #
# The segregation line: re-pointing the PO after approval
# --------------------------------------------------------------------------- #


async def _hold_then_relink(s, org_id, ent, *, status):
    """A hold raised against a PO that doesn't exist, then the invoice is
    re-pointed at a real PO it matches."""
    good = f"PO-GOOD-{uuid.uuid4().hex[:6]}"
    await _add_po(s, org_id, ent, po_number=good, total="1000.00", qty="10")
    inv = await _add_invoice(
        s, org_id, ent, po_number=f"PO-NOPE-{uuid.uuid4().hex[:6]}", amount="1000.00"
    )
    await s.commit()
    await _refresh(s, inv)
    await s.commit()
    (row,) = await _rows(s, inv.id)
    assert row.description_code == "po_not_found"

    inv.status = status
    inv.po_number = good
    await _refresh(s, inv)
    await s.commit()
    return inv


@pytest.mark.asyncio
async def test_relinking_the_po_after_approval_does_not_release_the_payment(realdb):
    """Past approval nobody re-reviews the invoice, so a refresh against a
    DIFFERENT PO must not clear a hold raised against the old one — that would
    let whoever edited `po_number` also release the money. It stays for a
    human, whom `segregation_refusal` then vets."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = await _hold_then_relink(s, org_id, ent, status=InvoiceStatus.approved)

        (row,) = await _rows(s, inv.id)
        assert row.status == "open"
        assert await blocked_invoice_ids(s, [inv.id]) == {inv.id}


@pytest.mark.asyncio
async def test_relinking_the_po_before_approval_closes_the_stale_hold(realdb):
    """Before approval the corrected invoice still goes to an approver who did
    not create it, so a typo'd PO number fixed in review clears its own hold."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = await _hold_then_relink(s, org_id, ent, status=InvoiceStatus.ready_for_review)

        (row,) = await _rows(s, inv.id)
        assert row.status == "resolved"


@pytest.mark.asyncio
async def test_a_row_being_decided_is_left_for_its_decider(realdb):
    """The agent coordinator marks the row it is resolving (`deciding`); a
    refresh its resolver triggers must leave that row for the agent to record,
    so the resolution is written once and names the agent's triggering actor."""
    from app.services.exception_lifecycle import deciding

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-DEC-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        await _add_po(s, org_id, ent, po_number=number, total="1000.00", qty="10")
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1500.00")
        await s.commit()
        await _refresh(s, inv)
        await s.commit()
        (row,) = await _rows(s, inv.id)

        inv.amount = Decimal("1000.00")
        with deciding(row.id):
            await _refresh(s, inv)
        await s.commit()

        (row,) = await _rows(s, inv.id)
        assert row.status == "open"
        assert await _resolved_audit(s, inv.correlation_id) == []


async def _quality_hold_then_pass(s, org_id, ent, *, uploader, recorder, status, source="manual"):
    """A failed inspection holds the invoice; then a `pass` is recorded —
    hand-entered by `recorder` unless `source` says otherwise."""
    number = f"PO-QSOD-{uuid.uuid4().hex[:6]}"
    po = await _add_po(s, org_id, ent, po_number=number, total="1037.00", qty="10")
    gr = await _add_gr(s, org_id, ent, po.id, received="10")
    s.add(
        QualityInspection(
            inspection_number=f"QI-{uuid.uuid4().hex[:6]}",
            po_id=po.id,
            gr_id=gr.id,
            result="fail",
            inspected_date=date(2026, 10, 1),
            source="qms",
            organization_id=org_id,
            entity_id=ent,
        )
    )
    inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1037.00")
    inv.uploaded_by_id = uploader
    await s.commit()
    await _refresh(s, inv)
    await s.commit()
    (row,) = await _rows(s, inv.id, "quality_hold")
    assert row.status == "open"

    s.add(
        QualityInspection(
            inspection_number=f"QI-{uuid.uuid4().hex[:6]}",
            po_id=po.id,
            gr_id=gr.id,
            result="pass",
            inspected_date=date(2026, 10, 2),
            source=source,
            recorded_by_user_id=recorder,
            organization_id=org_id,
            entity_id=ent,
        )
    )
    inv.status = status
    await s.commit()
    await _refresh(s, inv)
    await s.commit()
    (row,) = await _rows(s, inv.id, "quality_hold")
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [InvoiceStatus.ready_for_review, InvoiceStatus.approved])
async def test_a_pass_the_uploader_recorded_themselves_does_not_release_the_hold(realdb, status):
    """An inspection can be typed in by hand. If the person implicated in the
    invoice records the `pass` that would clear its quality hold, the refresh
    leaves the row for a human — otherwise recording a pass is a self-release."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    uploader = uuid.uuid4()
    async with mk() as s:
        ent = await _default_entity_id(s)
        row = await _quality_hold_then_pass(
            s, org_id, ent, uploader=uploader, recorder=uploader, status=status
        )
    assert row.status == "open"


@pytest.mark.asyncio
async def test_a_pass_someone_else_recorded_releases_the_hold(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        row = await _quality_hold_then_pass(
            s,
            org_id,
            ent,
            uploader=uuid.uuid4(),
            recorder=uuid.uuid4(),
            status=InvoiceStatus.approved,
        )
    assert row.status == "resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "recorder"),
    [
        ("manual", None),  # typed in, recorder unknown
        (None, None),  # predates the source column: unknown
    ],
)
async def test_a_pass_of_unknown_provenance_does_not_release_the_hold(realdb, source, recorder):
    """Fail closed: a verdict nobody can attribute is not evidence a detector
    may release money on."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        row = await _quality_hold_then_pass(
            s,
            org_id,
            ent,
            uploader=uuid.uuid4(),
            recorder=recorder,
            status=InvoiceStatus.approved,
            source=source,
        )
    assert row.status == "open"


@pytest.mark.asyncio
async def test_a_qms_pass_releases_the_hold_whoever_uploaded(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    uploader = uuid.uuid4()
    async with mk() as s:
        ent = await _default_entity_id(s)
        row = await _quality_hold_then_pass(
            s,
            org_id,
            ent,
            uploader=uploader,
            recorder=None,
            status=InvoiceStatus.approved,
            source="qms",
        )
    assert row.status == "resolved"


@pytest.mark.asyncio
async def test_without_org_settings_a_refresh_never_clears_a_hold(realdb):
    """A refresh with no org settings judges under the platform default rule
    (5 %). The org's 1 % vendor rule raised this hold on a 3 % over-billing; a
    default-rule refresh no longer sees it, and must not take that as leave to
    release the payment."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-NOSET-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        await _add_po(s, org_id, ent, po_number=number, total="1000.00", qty="10")
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1030.00")
        await s.commit()
        strict = {"matching": {"tolerance_pct": 1}}
        await _refresh(s, inv, org_settings=strict)
        await s.commit()
        (row,) = await _rows(s, inv.id)
        assert row.status == "open"

        await refresh_warnings(s, inv)  # no org settings — e.g. the portal path
        await s.commit()
        (row,) = await _rows(s, inv.id)
        assert row.status == "open"

        await _refresh(s, inv, org_settings=strict)  # the org's rule still finds it
        await s.commit()
        (row,) = await _rows(s, inv.id)
        assert row.status == "open"


@pytest.mark.asyncio
async def test_a_pre_approval_relink_is_held_when_approval_allows_self_approval(
    realdb, monkeypatch
):
    """The pre-approval relaxation rests on an approver who did not create the
    invoice reviewing the correction. An approval step with
    `require_segregation: false` removes that reviewer, so the stale hold stays
    for the queue (whose own segregation is independent of the workflow)."""
    from app.services import review

    async def _no_segregation(db, invoice, instance=None):
        return {"require_segregation": False}

    monkeypatch.setattr(review, "resolve_approval_config", _no_segregation)
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = await _hold_then_relink(s, org_id, ent, status=InvoiceStatus.ready_for_review)
        (row,) = await _rows(s, inv.id)
        assert row.status == "open"


@pytest.mark.asyncio
async def test_a_refresh_that_still_finds_the_decided_finding_marks_it_refound(realdb):
    """An agent relinks or corrects the invoice and the refresh still finds a
    PO mismatch. The finding folds into the very row being decided, so the
    `Decision` is marked and the coordinator escalates instead of resolving."""
    from app.services.exception_lifecycle import deciding

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-REF-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        await _add_po(s, org_id, ent, po_number=number, total="1000.00", qty="10")
        inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1500.00")
        await s.commit()
        await _refresh(s, inv)
        await s.commit()
        (row,) = await _rows(s, inv.id)

        with deciding(row.id) as decision:
            await _refresh(s, inv)
        assert decision.refound is True

        inv.amount = Decimal("1000.00")
        with deciding(row.id) as decision:
            await _refresh(s, inv)
        assert decision.refound is False


_LOOSE_SERVICES = {
    "matching": {"tolerance_pct": 1, "commodity_rules": {"SERVICES": {"tolerance_pct": 10}}}
}


async def _over_billed_then_recoded(s, org_id, ent, *, status):
    number = f"PO-GL-{uuid.uuid4().hex[:6]}"
    await _add_po(s, org_id, ent, po_number=number, total="1000.00", qty="10")
    inv = await _add_invoice(s, org_id, ent, po_number=number, amount="1030.00")
    await s.commit()
    await _refresh(s, inv, org_settings=_LOOSE_SERVICES)
    await s.commit()
    (row,) = await _rows(s, inv.id)
    assert row.status == "open"
    inv.status = status
    inv.gl_account = "SERVICES"  # editable on an approved invoice
    await _refresh(s, inv, org_settings=_LOOSE_SERVICES)
    await s.commit()
    (row,) = await _rows(s, inv.id)
    return row


@pytest.mark.asyncio
async def test_recoding_the_gl_after_approval_does_not_release_the_payment(realdb):
    """The match rule is chosen by vendor and header GL code, and `gl_account`
    stays editable once approved. Re-coding a 3 % over-billing to a commodity
    with a 10 % rule makes the finding vanish under the invoice's own rule — so
    past approval a close also needs the finding gone under the strictest rule
    any GL code could pick, and here it is not."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        row = await _over_billed_then_recoded(s, org_id, ent, status=InvoiceStatus.approved)
    assert row.status == "open"


@pytest.mark.asyncio
async def test_recoding_the_gl_before_approval_follows_the_new_rule(realdb):
    """Before approval the re-coded invoice still goes to an approver who did
    not create it, so the commodity's rule applies as configured."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        row = await _over_billed_then_recoded(s, org_id, ent, status=InvoiceStatus.ready_for_review)
    assert row.status == "resolved"
