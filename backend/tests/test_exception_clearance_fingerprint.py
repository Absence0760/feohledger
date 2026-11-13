"""A person's clearance of a detector finding survives a refresh (decisions §366).

§286 stopped `refresh_warnings` re-opening a `duplicate` / `fraud_flag` a person
had resolved or dismissed while the invoice was unchanged. Every other type
`_ensure_exception` raises still came back on the next refresh, including the
payment-blocking `line_total_mismatch`, `po_mismatch` and `quality_hold`: a
reviewer who accepted a line variance saw the hold return the next time anyone
saved the invoice. §366 stamps every type with its own facts
(`invoice_warnings.current_finding_fingerprint`), so:

* a cleared finding stays cleared across a refresh with unchanged facts;
* it is raised again when a fact its rule reads changes (a line total, the
  header money, the PO, a receipt, an inspection);
* a verdict given while the facts had moved, even if they moved back before
  the click, binds nothing (the stamp is voided);
* the decision still writes its append-only audit row, and a suppressed
  refresh writes nothing.

`duplicate` / `fraud_flag` are pinned in `test_erp_export_blocking_exceptions.py`
and keep their §286 hash byte for byte (pinned below).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceLineItem, InvoiceStatus
from app.models.procurement import GoodsReceipt, GRLineItem, POLineItem, PurchaseOrder
from app.models.quality_inspection import QualityInspection
from app.models.workflow import AuditLog
from app.services.invoice_warnings import refresh_warnings
from app.services.payment_runs import blocked_invoice_ids

TENANT = "a"


# --------------------------------------------------------------------------- #
# Every payment-blocking type is accounted for
# --------------------------------------------------------------------------- #

#: Where each payment-blocking type's "a person's clearance survives a refresh"
#: is pinned. A new member of the set fails here until someone decides it.
_COVERED_BY = {
    "duplicate": "test_erp_export_blocking_exceptions.py",
    "fraud_flag": "test_erp_export_blocking_exceptions.py",
    "line_total_mismatch": "this module",
    "po_mismatch": "this module",
    "quality_hold": "this module",
}

#: Payment-blocking types `refresh_warnings` never raises, so no refresh can
#: re-open them and there is nothing to suppress.
_NOT_RAISED_BY_REFRESH = {
    # Opened on a PAYMENT by the settlement / reconciler / ERP-sync paths
    # through `create_exception`, never by `_ensure_exception`.
    "payment_reconciliation",
}


def test_every_payment_blocking_type_is_covered():
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES

    assert set(PAYMENT_BLOCKING_EXCEPTION_TYPES) == set(_COVERED_BY) | _NOT_RAISED_BY_REFRESH
    assert not set(_COVERED_BY) & _NOT_RAISED_BY_REFRESH


def test_not_raised_by_refresh_really_is_not():
    """`payment_reconciliation` is never passed to `_ensure_exception`."""
    import inspect

    from app.services import invoice_warnings

    source = inspect.getsource(invoice_warnings)
    for exception_type in _NOT_RAISED_BY_REFRESH:
        assert f'"{exception_type}"' not in source


# --------------------------------------------------------------------------- #
# Pure: what each type's fingerprint reads
# --------------------------------------------------------------------------- #


def _inv(**overrides):
    base = dict(
        id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        vendor_name="Acme",
        invoice_number="INV-1",
        amount=Decimal("1037.00"),
        currency="USD",
        subtotal=None,
        tax_amount=None,
        shipping_amount=None,
        discount_amount=None,
        invoice_date=date(2026, 10, 1),
        due_date=None,
        remit_to_address=None,
        description=None,
        payment_method=None,
        po_number="PO-1",
        entity_id=uuid.uuid4(),
        gl_account="6000",
        contract_id=None,
        po_match={"status": "mismatch", "po_total": 1000.0, "details": {"variance": 37.0}},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_header_fingerprint_keeps_its_0116_payload():
    """Stamps written since migration 0116 must still compare equal."""
    from app.services.invoice_warnings import _FINDING_INPUT_FIELDS, finding_fingerprint

    inv = _inv()
    payload = {"code": "duplicate_invoice_number"}
    for f in _FINDING_INPUT_FIELDS:
        value = getattr(inv, f, None)
        payload[f] = format(value.normalize(), "f") if isinstance(value, Decimal) else value
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    assert finding_fingerprint(inv, "duplicate_invoice_number") == expected


def test_line_total_fingerprint_reads_the_lines_and_the_header_money():
    from app.services.invoice_warnings import line_total_fingerprint

    inv = _inv()
    base = line_total_fingerprint(inv, Decimal("900.00"))
    assert line_total_fingerprint(inv, Decimal("900")) == base, "one amount, two spellings"
    assert line_total_fingerprint(inv, Decimal("950.00")) != base
    assert line_total_fingerprint(_inv(tax_amount=Decimal("37.00")), Decimal("900")) != base
    assert line_total_fingerprint(inv, None) != base
    # An edit the reconciliation does not read leaves it alone.
    assert line_total_fingerprint(_inv(invoice_number="INV-2"), Decimal("900")) == base


def test_po_match_fingerprint_reads_the_persisted_match():
    from app.services.invoice_warnings import po_match_fingerprint

    inv = _inv()
    base = po_match_fingerprint(inv, "po_mismatch")
    # JSONB hands the match back with floats; in memory it may hold Decimals
    # or ints. One fact, one fingerprint.
    same = _inv(
        po_match={"status": "mismatch", "po_total": Decimal("1000"), "details": {"variance": 37}}
    )
    assert po_match_fingerprint(same, "po_mismatch") == base
    moved = _inv(po_match={"status": "mismatch", "po_total": 900.0, "details": {"variance": 137.0}})
    assert po_match_fingerprint(moved, "po_mismatch") != base
    assert po_match_fingerprint(_inv(amount=Decimal("1100.00")), "po_mismatch") != base
    assert po_match_fingerprint(inv, "quality_hold") != base, "keyed on the type"


def test_po_match_fingerprint_ignores_wording_and_unhashed_fields():
    """A reworded catalogue sentence or a new MatchResult field must not
    re-raise every cleared hold after a deploy; a different issue must."""
    from app.services.invoice_warnings import po_match_fingerprint

    def _with(issues, **extra):
        return _inv(po_match={"status": "mismatch", "po_total": 1000.0, "issues": issues, **extra})

    issue = {"code": "amount_variance", "params": {"pct": "23.7"}, "message": "Over by 23.7%"}
    base = po_match_fingerprint(_with([issue]), "po_mismatch")
    reworded = dict(issue, message="Billed 23.7% above the PO")
    assert po_match_fingerprint(_with([reworded]), "po_mismatch") == base
    assert po_match_fingerprint(_with([issue], some_new_field=True), "po_mismatch") == base
    other = dict(issue, params={"pct": "30.0"})
    assert po_match_fingerprint(_with([other]), "po_mismatch") != base
    tighter = _with([issue], details={"tolerance_pct": 1.0})
    assert po_match_fingerprint(tighter, "po_mismatch") != base


def test_price_variance_fingerprint_ignores_the_drifting_history():
    from app.services.invoice_warnings import price_variance_fingerprint

    inv = _inv()
    lines = [{"item_code": "W-1", "description": "Widget", "unit_price": Decimal("12.50")}]
    base = price_variance_fingerprint(inv, lines)
    assert price_variance_fingerprint(inv, [dict(lines[0], unit_price=Decimal("12.5"))]) == base
    assert price_variance_fingerprint(inv, [dict(lines[0], unit_price=Decimal("13"))]) != base
    assert price_variance_fingerprint(_inv(currency="EUR"), lines) != base


def test_contract_fingerprint_reads_which_terms_not_the_running_spend():
    from app.services.invoice_warnings import contract_fingerprint

    inv = _inv(contract_id=uuid.uuid4())
    over = [{"code": "contract_spend_limit_exceeded", "params": {"cumulativeSpend": "5100"}}]
    base = contract_fingerprint(inv, over)
    grown = [{"code": "contract_spend_limit_exceeded", "params": {"cumulativeSpend": "7300"}}]
    assert contract_fingerprint(inv, grown) == base
    terminated = [*over, {"code": "contract_terminated", "params": {}}]
    assert contract_fingerprint(inv, terminated) != base


def test_vendor_fingerprint_reads_the_vendor_only():
    from app.services.invoice_warnings import vendor_fingerprint

    inv = _inv()
    assert vendor_fingerprint(_inv(vendor_id=inv.vendor_id, amount=Decimal("1"))) == (
        vendor_fingerprint(inv)
    )
    assert vendor_fingerprint(_inv()) != vendor_fingerprint(inv)


# --------------------------------------------------------------------------- #
# Real Postgres helpers
# --------------------------------------------------------------------------- #


async def _refresh(s, inv):
    await refresh_warnings(s, inv, org_settings={})
    await s.commit()


async def _default_entity_id(s):
    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _add_invoice(s, org_id, entity_id, *, amount, po_number=None):
    inv = Invoice(
        invoice_number=f"CLR-{uuid.uuid4().hex[:8]}",
        vendor_name="Acme",
        amount=Decimal(amount),
        currency="USD",
        po_number=po_number,
        status=InvoiceStatus.ready_for_review,
        organization_id=org_id,
        entity_id=entity_id,
    )
    s.add(inv)
    await s.flush()
    return inv


async def _add_lines(s, invoice_id, totals):
    rows = []
    for n, total in enumerate(totals, start=1):
        row = InvoiceLineItem(
            invoice_id=invoice_id, line_number=n, description=f"Line {n}", total=Decimal(total)
        )
        s.add(row)
        rows.append(row)
    await s.flush()
    return rows


async def _add_po(s, org_id, entity_id, *, po_number, total, qty, received):
    po = PurchaseOrder(
        po_number=po_number,
        total=Decimal(total),
        currency="USD",
        status="open",
        organization_id=org_id,
        entity_id=entity_id,
    )
    s.add(po)
    await s.flush()
    s.add(POLineItem(po_id=po.id, description="Widget", quantity=Decimal(qty)))
    gr = GoodsReceipt(
        gr_number=f"GR-{uuid.uuid4().hex[:8]}",
        po_id=po.id,
        status="received",
        organization_id=org_id,
        entity_id=entity_id,
    )
    s.add(gr)
    await s.flush()
    s.add(GRLineItem(gr_id=gr.id, description="Widget", quantity_received=Decimal(received)))
    await s.flush()
    return po, gr


async def _rows(s, invoice_id, exception_type):
    return list(
        (
            await s.execute(
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


async def _statuses(s, invoice_id, exception_type):
    """Read from the table, not the identity map."""
    return list(
        (
            await s.execute(
                select(APException.status)
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


async def _person_dismisses(s, inv, exception_type, actor_id):
    """The queue's dismiss, through `record_decision`, as a named person."""
    from app.services.exception_lifecycle import record_decision

    (row,) = [
        r for r in await _rows(s, inv.id, exception_type) if r.status in ("open", "escalated")
    ]
    await record_decision(
        s,
        exception=row,
        action="dismiss",
        resolution="Checked against the paper invoice.",
        actor_id=actor_id,
        actor_name="Reviewer",
        invoice=inv,
        org_settings={},
    )
    await s.commit()
    return row


async def _audit(s, correlation_id, action):
    return list(
        (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.correlation_id == correlation_id, AuditLog.action == action
                )
            )
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------- #
# line_total_mismatch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_dismissed_line_variance_survives_a_refresh_with_unchanged_facts(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = await _add_invoice(s, info.org_id, ent, amount="1037.00")
        await _add_lines(s, inv.id, ["500.00", "400.00"])
        await s.commit()
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "line_total_mismatch") == ["open"]
        assert await blocked_invoice_ids(s, [inv.id]) == {inv.id}

        row = await _person_dismisses(s, inv, "line_total_mismatch", info.users["ap_manager"])
        assert row.finding_fingerprint, "a person's verdict on the raised state binds"
        (dismissed,) = await _audit(s, inv.correlation_id, "exception.dismissed")
        assert dismissed.actor_id == info.users["ap_manager"]
        assert dismissed.entity_id == row.id
        raised_before = len(await _audit(s, inv.correlation_id, "exception.raised"))

        # A refresh for an unrelated edit, and another with nothing changed.
        await s.execute(
            update(Invoice).where(Invoice.id == inv.id).values(description="Q4 widgets")
        )
        await s.commit()
        await s.refresh(inv)
        await _refresh(s, inv)
        await _refresh(s, inv)

        assert await _statuses(s, inv.id, "line_total_mismatch") == ["dismissed"]
        assert await blocked_invoice_ids(s, [inv.id]) == set()
        assert len(await _audit(s, inv.correlation_id, "exception.raised")) == raised_before
        # The reviewer still sees the variance on the invoice itself.
        assert "line_total_mismatch" in [w.get("code") for w in inv.warnings or []]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["line", "header"])
async def test_a_dismissed_line_variance_reopens_when_its_facts_change(realdb, change):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = await _add_invoice(s, info.org_id, ent, amount="1037.00")
        lines = await _add_lines(s, inv.id, ["500.00", "400.00"])
        await s.commit()
        await _refresh(s, inv)
        await _person_dismisses(s, inv, "line_total_mismatch", info.users["ap_manager"])

        if change == "line":
            lines[1].total = Decimal("450.00")
        else:
            inv.amount = Decimal("1137.00")
        await s.commit()
        await _refresh(s, inv)

        assert await _statuses(s, inv.id, "line_total_mismatch") == ["dismissed", "open"]
        assert await blocked_invoice_ids(s, [inv.id]) == {inv.id}


@pytest.mark.asyncio
async def test_a_line_edit_reverted_before_the_dismissal_binds_nothing(realdb):
    """Raised at S1; a line is edited to S2 while the reviewer reads the queue
    (a refresh sees it and voids the stamp), then put back before the click.
    The reviewer may have judged S2, so the dismissal suppresses nothing."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = await _add_invoice(s, info.org_id, ent, amount="1037.00")
        lines = await _add_lines(s, inv.id, ["500.00", "400.00"])
        await s.commit()
        await _refresh(s, inv)

        for total in ("450.00", "400.00"):  # S2, then back to S1
            lines[1].total = Decimal(total)
            await s.commit()
            await _refresh(s, inv)
        assert await _statuses(s, inv.id, "line_total_mismatch") == ["open"]

        row = await _person_dismisses(s, inv, "line_total_mismatch", info.users["ap_manager"])
        assert row.finding_fingerprint is None
        assert len(await _audit(s, inv.correlation_id, "exception.dismissed")) == 1
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "line_total_mismatch") == ["dismissed", "open"]


@pytest.mark.asyncio
async def test_a_close_no_person_made_never_suppresses_a_line_variance(realdb):
    from app.services.exception_lifecycle import record_decision

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = await _add_invoice(s, info.org_id, ent, amount="1037.00")
        await _add_lines(s, inv.id, ["500.00", "400.00"])
        await s.commit()
        await _refresh(s, inv)
        (row,) = await _rows(s, inv.id, "line_total_mismatch")
        await record_decision(
            s,
            exception=row,
            action="resolve",
            resolution="Closed by an agent.",
            actor_id=info.users["ap_manager"],
            actor_name="agent",
            invoice=inv,
            via="agent",
            org_settings={},
        )
        assert row.finding_fingerprint is None
        await s.commit()
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "line_total_mismatch") == ["resolved", "open"]


# --------------------------------------------------------------------------- #
# po_mismatch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_dismissed_po_variance_survives_a_refresh_and_reopens_on_a_po_change(realdb):
    """+23.7 % over a fully received PO: held. The reviewer accepts it; the next
    refreshes leave it accepted. The PO total then changes (an ERP sync, an
    edit): the match moves, and the hold is back for a person."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-CLR-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po, _ = await _add_po(
            s, info.org_id, ent, po_number=number, total="1000.00", qty="10", received="10"
        )
        inv = await _add_invoice(s, info.org_id, ent, amount="1237.00", po_number=number)
        await s.commit()
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "po_mismatch") == ["open"]

        await _person_dismisses(s, inv, "po_mismatch", info.users["ap_manager"])
        assert len(await _audit(s, inv.correlation_id, "exception.dismissed")) == 1
        await _refresh(s, inv)
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "po_mismatch") == ["dismissed"]
        assert await blocked_invoice_ids(s, [inv.id]) == set()

        po.total = Decimal("900.00")
        await s.commit()
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "po_mismatch") == ["dismissed", "open"]
        assert await blocked_invoice_ids(s, [inv.id]) == {inv.id}


@pytest.mark.asyncio
async def test_a_po_change_reverted_before_the_dismissal_binds_nothing(realdb):
    """The PO-side timed variant. Raised at match M1; the PO total moves (the
    refresh persists M2, keeping the stamp, since the void reads the match the
    PREVIOUS refresh saved), then moves back (that refresh compares M2 with the
    stamp and voids it). The reviewer may have judged M2, so the dismissal binds
    nothing and the next refresh raises the hold again."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-CLR-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po, _ = await _add_po(
            s, info.org_id, ent, po_number=number, total="1000.00", qty="10", received="10"
        )
        inv = await _add_invoice(s, info.org_id, ent, amount="1237.00", po_number=number)
        await s.commit()
        await _refresh(s, inv)

        for total in ("900.00", "1000.00"):  # M2, then back to M1
            po.total = Decimal(total)
            await s.commit()
            await _refresh(s, inv)
        assert await _statuses(s, inv.id, "po_mismatch") == ["open"]

        row = await _person_dismisses(s, inv, "po_mismatch", info.users["ap_manager"])
        assert row.finding_fingerprint is None
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "po_mismatch") == ["dismissed", "open"]


@pytest.mark.asyncio
async def test_a_dismissal_between_a_po_change_and_its_revert_binds_nothing(realdb):
    """A decision taken after the refresh that persisted M2 compares against M2,
    not the M1 the row was raised on, so it binds nothing either."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-CLR-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po, _ = await _add_po(
            s, info.org_id, ent, po_number=number, total="1000.00", qty="10", received="10"
        )
        inv = await _add_invoice(s, info.org_id, ent, amount="1237.00", po_number=number)
        await s.commit()
        await _refresh(s, inv)
        po.total = Decimal("900.00")
        await s.commit()
        await _refresh(s, inv)

        row = await _person_dismisses(s, inv, "po_mismatch", info.users["ap_manager"])
        assert row.finding_fingerprint is None
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "po_mismatch") == ["dismissed", "open"]


@pytest.mark.asyncio
async def test_an_erp_po_sync_refresh_re_raises_a_cleared_hold_when_the_po_moved(realdb):
    """§323's hold-only sync: a PO the ERP changed moves the match, so the
    sync's refresh raises the cleared hold again rather than honouring it."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-CLR-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po, _ = await _add_po(
            s, info.org_id, ent, po_number=number, total="1000.00", qty="10", received="10"
        )
        inv = await _add_invoice(s, info.org_id, ent, amount="1237.00", po_number=number)
        await s.commit()
        await _refresh(s, inv)
        await _person_dismisses(s, inv, "po_mismatch", info.users["ap_manager"])

        po.total = Decimal("950.00")
        await s.commit()
        await refresh_warnings(s, inv, org_settings={}, erp_po_sync={"erp_type": "mock"})
        await s.commit()
        assert await _statuses(s, inv.id, "po_mismatch") == ["dismissed", "open"]
        assert await blocked_invoice_ids(s, [inv.id]) == {inv.id}


@pytest.mark.asyncio
async def test_a_dismissed_po_variance_reopens_when_the_invoice_amount_changes(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-CLR-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        await _add_po(
            s, info.org_id, ent, po_number=number, total="1000.00", qty="10", received="10"
        )
        inv = await _add_invoice(s, info.org_id, ent, amount="1237.00", po_number=number)
        await s.commit()
        await _refresh(s, inv)
        await _person_dismisses(s, inv, "po_mismatch", info.users["ap_manager"])

        inv.amount = Decimal("1337.00")
        await s.commit()
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "po_mismatch") == ["dismissed", "open"]


@pytest.mark.asyncio
async def test_a_dismissed_receipt_hold_reopens_when_a_receipt_changes(realdb):
    """Billed in full with six of ten units in: held on the receipt leg. The
    reviewer accepts it (the goods are on the dock); a receipt is then
    cancelled, so the received slice shrinks and the hold is back."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-CLR-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po, _ = await _add_po(
            s, info.org_id, ent, po_number=number, total="1037.00", qty="10", received="4"
        )
        extra = GoodsReceipt(
            gr_number=f"GR-{uuid.uuid4().hex[:8]}",
            po_id=po.id,
            status="received",
            organization_id=info.org_id,
            entity_id=ent,
        )
        s.add(extra)
        await s.flush()
        s.add(GRLineItem(gr_id=extra.id, description="Widget", quantity_received=Decimal("2")))
        inv = await _add_invoice(s, info.org_id, ent, amount="1037.00", po_number=number)
        await s.commit()
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "po_mismatch") == ["open"]

        await _person_dismisses(s, inv, "po_mismatch", info.users["ap_manager"])
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "po_mismatch") == ["dismissed"]

        extra.status = "cancelled"
        await s.commit()
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "po_mismatch") == ["dismissed", "open"]


# --------------------------------------------------------------------------- #
# quality_hold
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_dismissed_quality_hold_survives_a_refresh_and_reopens_on_a_new_inspection(
    realdb,
):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    number = f"PO-QH-{uuid.uuid4().hex[:6]}"
    async with mk() as s:
        ent = await _default_entity_id(s)
        po, gr = await _add_po(
            s, info.org_id, ent, po_number=number, total="1037.00", qty="10", received="10"
        )
        s.add(
            QualityInspection(
                inspection_number=f"QI-{uuid.uuid4().hex[:6]}",
                po_id=po.id,
                gr_id=gr.id,
                result="fail",
                inspected_date=date(2026, 10, 1),
                source="qms",
                organization_id=info.org_id,
                entity_id=ent,
            )
        )
        inv = await _add_invoice(s, info.org_id, ent, amount="1037.00", po_number=number)
        await s.commit()
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "quality_hold") == ["open"]

        await _person_dismisses(s, inv, "quality_hold", info.users["ap_manager"])
        assert len(await _audit(s, inv.correlation_id, "exception.dismissed")) == 1
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "quality_hold") == ["dismissed"]
        assert await blocked_invoice_ids(s, [inv.id]) == set()

        s.add(
            QualityInspection(
                inspection_number=f"QI-{uuid.uuid4().hex[:6]}",
                po_id=po.id,
                gr_id=gr.id,
                result="fail",
                deviation_notes="Second lot cracked too.",
                inspected_date=date(2026, 10, 2),
                source="qms",
                organization_id=info.org_id,
                entity_id=ent,
            )
        )
        await s.commit()
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "quality_hold") == ["dismissed", "open"]
        assert await blocked_invoice_ids(s, [inv.id]) == {inv.id}


# --------------------------------------------------------------------------- #
# Segregation of duties is untouched
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_uploader_still_cannot_clear_the_hold_it_would_then_keep_cleared(realdb):
    """Suppression honours only a verdict `record_decision` let through. The
    implicated uploader is refused (403) and the row stays open and blocking."""
    from fastapi import HTTPException

    from app.services.exception_lifecycle import record_decision

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    clerk = info.users["ap_clerk"]
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = await _add_invoice(s, info.org_id, ent, amount="1037.00")
        inv.uploaded_by_id = clerk
        await _add_lines(s, inv.id, ["500.00", "400.00"])
        await s.commit()
        await _refresh(s, inv)
        (row,) = await _rows(s, inv.id, "line_total_mismatch")

        with pytest.raises(HTTPException) as refused:
            await record_decision(
                s,
                exception=row,
                action="dismiss",
                resolution="Mine, and fine.",
                actor_id=clerk,
                actor_name="Clerk",
                invoice=inv,
                org_settings={},
            )
        assert refused.value.status_code == 403
        await s.rollback()
        await s.refresh(inv)
        await _refresh(s, inv)
        assert await _statuses(s, inv.id, "line_total_mismatch") == ["open"]
        assert await blocked_invoice_ids(s, [inv.id]) == {inv.id}
