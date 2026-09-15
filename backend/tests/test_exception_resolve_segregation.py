"""Segregation of duties on the exception queue — the control, end to end.

Clearing a payment-BLOCKING exception (``duplicate`` / ``fraud_flag`` /
``line_total_mismatch`` / ``payment_reconciliation``) is the human sign-off that
lets a payment run pay the invoice, and invoice approval gates on none of them.
Until this, ``POST /api/exceptions/{id}/resolve`` and ``/bulk/resolve`` gated on
``require_roles(ADMIN, AP_MANAGER)`` and nothing else — so the actor a flag was
raised against could stand it down, including the one actor the app had already
decided must not approve the same invoice.

Two axes, both NULL-permissive, verified here on real Postgres over real HTTP
because both are data-layer facts:

* **the payable's implicated actors** — ``Invoice.uploaded_by_id`` ∪
  ``Invoice.segregation_actor_ids``, the set ``approval_chain`` already refuses
  the *approval* on. This is the axis that binds in volume.
* **the raiser** — ``exceptions.raised_by_user_id``. NULL at every detector and
  sweep; supplied by the vendor bank-change approval, which is the case the set
  above cannot reach (the approver is not the invoices' uploader) and the open
  end of the BEC chain ``docs/authentication.md`` recorded.

Also pinned here, because each is a way the control could be wrong rather than
merely absent: ``escalate`` stays open (it is the refused actor's exit, and an
escalated row still blocks the run); non-blocking types are out of scope
(clearing one releases nothing); the org opt-out is an explicit ``false`` and
nothing else; ``/bulk/resolve`` refuses per ROW and never 409s the batch; and an
agent inherits the triggering human's refusal instead of laundering around it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.models.exception import Exception as APException
from app.models.invoice import Invoice
from app.models.organization import Organization

TENANT = "a"

BLOCKING = "fraud_flag"
NON_BLOCKING = "po_mismatch"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _make_invoice(mk, org_id, *, number, uploaded_by_id=None, actor_set=None):
    inv = Invoice(
        id=uuid.uuid4(),
        organization_id=org_id,
        invoice_number=number,
        vendor_name="Globex Corporation",
        amount=Decimal("4200.00"),
        currency="USD",
        status="ready_for_review",
        uploaded_by_id=uploaded_by_id,
        segregation_actor_ids=actor_set,
    )
    async with mk() as s:
        s.add(inv)
        await s.commit()
    return inv


async def _open_exception(
    mk,
    org_id,
    invoice,
    *,
    exception_type=BLOCKING,
    raised_by_user_id=None,
):
    from app.services.exception_service import create_exception

    async with mk() as s:
        row = await s.get(Invoice, invoice.id) if invoice is not None else None
        exc = await create_exception(
            s,
            exception_type=exception_type,
            severity="error",
            description="detector output",
            organization_id=org_id,
            invoice=row,
            raised_by_user_id=raised_by_user_id,
        )
        exc_id = exc.id
        await s.commit()
    return exc_id


async def _status(mk, exc_id):
    async with mk() as s:
        row = await s.get(APException, exc_id)
        return row.status


async def _set_org_settings(realdb, settings: dict):
    ctrl_mk = realdb.control_sessionmaker()
    async with ctrl_mk() as s:
        await s.execute(
            update(Organization)
            .where(Organization.id == realdb.info(TENANT).org_id)
            .values(settings=settings)
        )
        await s.commit()


# ---------------------------------------------------------------------------
# The implicated-actor axis — the invoice's own uploader / shaper.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_uploader_cannot_resolve_a_payment_blocking_exception(realdb):
    """The invoice's uploader is already refused its APPROVAL. Leaving them the
    fraud flag that blocks its payment run was the asymmetry."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-001", uploaded_by_id=info.users["admin"]
    )
    exc_id = await _open_exception(mk, info.org_id, inv)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Looked fine to me."},
        )

    assert res.status_code == 403, res.text
    assert "Segregation of duties" in res.json()["detail"]
    # PII / third-party guard: the sentence is about the CALLER and names nobody.
    assert "Globex" not in res.text
    assert str(info.users["admin"]) not in res.text
    assert await _status(mk, exc_id) == "open", "a refused decision must not mutate the row"


@pytest.mark.asyncio
async def test_the_uploader_cannot_dismiss_it_either(realdb):
    """``dismiss`` is the other terminal verb, and it is the cheaper one to
    reach — a refusal that covered only ``resolve`` would be a one-word bypass."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-002", uploaded_by_id=info.users["admin"]
    )
    exc_id = await _open_exception(mk, info.org_id, inv)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "dismiss", "resolution": "Not a real flag."},
        )
    assert res.status_code == 403, res.text
    assert await _status(mk, exc_id) == "open"


@pytest.mark.asyncio
async def test_a_material_editor_in_the_actor_set_is_refused_too(realdb):
    """``violates_segregation`` keys on a SET (decisions §152), and the queue
    reads the same set rather than re-deriving a narrower rule from the uploader
    column alone."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk,
        info.org_id,
        number="INV-SOD-003",
        uploaded_by_id=info.users["ap_manager"],
        actor_set=[str(info.users["admin"])],
    )
    exc_id = await _open_exception(mk, info.org_id, inv)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Cleared."},
        )
    assert res.status_code == 403, res.text
    assert await _status(mk, exc_id) == "open"


@pytest.mark.asyncio
async def test_the_refused_actor_can_still_escalate(realdb):
    """Escalation is the exit, not a bypass: an ``escalated`` row still blocks a
    payment run (``blocking_exception_types`` excludes only resolved/dismissed),
    so handing the decision on is exactly what the control wants. Refusing it
    would trap the row with no way out but a role change."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-004", uploaded_by_id=info.users["admin"]
    )
    exc_id = await _open_exception(mk, info.org_id, inv)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={
                "action": "escalate",
                "resolution": "I uploaded this; someone else please check.",
            },
        )
    assert res.status_code == 200, res.text
    assert await _status(mk, exc_id) == "escalated"

    # And the escalation is still blocking, which is what makes it safe to allow.
    from app.api.payments import blocking_exception_types

    async with mk() as s:
        blocked = await blocking_exception_types(s, [inv.id])
    assert blocked.get(inv.id), "an escalated blocking exception must still stop a run"


@pytest.mark.asyncio
async def test_a_different_user_resolves_it_normally(realdb):
    """The control is 'someone else decides', not 'nobody decides'."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-005", uploaded_by_id=info.users["ap_manager"]
    )
    exc_id = await _open_exception(mk, info.org_id, inv)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Called the vendor back on a known number."},
        )
    assert res.status_code == 200, res.text
    assert await _status(mk, exc_id) == "resolved"


# ---------------------------------------------------------------------------
# NULL permissive — the branch the whole design rests on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_system_raised_exception_on_a_system_invoice_stays_resolvable(realdb):
    """Both axes NULL: email intake / inbound PEPPOL / portal submit leave
    ``uploaded_by_id`` NULL, and every detector and sweep leaves
    ``raised_by_user_id`` NULL. Failing closed here would make the queue
    unworkable for three ingestion channels — the mirror of the trap decisions
    §131 documents for invoice approval."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(mk, info.org_id, number="INV-SOD-006", uploaded_by_id=None)
    exc_id = await _open_exception(mk, info.org_id, inv, raised_by_user_id=None)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Confirmed distinct payable."},
        )
    assert res.status_code == 200, res.text
    assert await _status(mk, exc_id) == "resolved"


@pytest.mark.asyncio
async def test_an_invoice_less_exception_stays_resolvable(realdb):
    """A Positive Pay ``not_on_file`` cheque has no invoice, so there is no
    implicated set to read at all."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    exc_id = await _open_exception(mk, info.org_id, None)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Bank confirmed the stop-payment."},
        )
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# Scope — only the types whose clearing releases money.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_uploader_may_clear_a_non_blocking_exception(realdb):
    """Clearing a ``po_mismatch`` releases nothing — approval does not gate on
    it and neither does a payment run — so a refusal there would be friction
    with no control behind it. Scope is ``is_payment_blocking``, the one
    definition of 'clearing this lets money move'."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-007", uploaded_by_id=info.users["admin"]
    )
    exc_id = await _open_exception(mk, info.org_id, inv, exception_type=NON_BLOCKING)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Amended the PO."},
        )
    assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# The opt-out.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_explicit_org_opt_out_permits_self_clearing(realdb):
    """A two-person AP team can have nobody else to work the queue, which is why
    this was a control-*design* call. The escape hatch is deliberate, per-org,
    and has to be written down."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-008", uploaded_by_id=info.users["admin"]
    )
    exc_id = await _open_exception(mk, info.org_id, inv)
    await _set_org_settings(realdb, {"exceptions": {"require_segregation": False}})

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Single-operator account."},
        )
    assert res.status_code == 200, res.text


@pytest.mark.asyncio
async def test_an_unrelated_exceptions_block_does_not_disable_the_control(realdb):
    """Default ON: only an explicit ``false`` turns it off. An org that has
    configured auto-assignment and SLAs in the same settings block — which is
    the common case — keeps the control."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-009", uploaded_by_id=info.users["admin"]
    )
    exc_id = await _open_exception(mk, info.org_id, inv)
    await _set_org_settings(realdb, {"exceptions": {"default_sla_hours": 24}})

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "nope"},
        )
    assert res.status_code == 403, res.text


# ---------------------------------------------------------------------------
# The raiser axis — the vendor bank-change approver.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_bank_change_approver_cannot_clear_the_flag_they_raised(realdb):
    """The one case the implicated-actor axis cannot reach: the approver is not
    the uploader of the invoices their approval re-points. ``fraud_flag`` on each
    in-queue invoice is the compensating control, and until this it was 'not a
    second control against the same actor'."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    # Someone else entirely uploaded the payable — only the raiser axis can bite.
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-010", uploaded_by_id=info.users["ap_clerk"]
    )
    exc_id = await _open_exception(mk, info.org_id, inv, raised_by_user_id=info.users["admin"])

    async with realdb.client(key=TENANT, role="admin") as c:
        refused = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "I approved the change; it's fine."},
        )
    assert refused.status_code == 403, refused.text
    assert "raised this exception" in refused.json()["detail"]
    assert await _status(mk, exc_id) == "open"

    # A different approver clears it, which is the whole point.
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        allowed = await c.post(
            f"/api/exceptions/{exc_id}/resolve",
            json={"action": "resolve", "resolution": "Verified by callback to a known number."},
        )
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_the_bank_change_route_stamps_the_approver_as_the_raiser(realdb):
    """Provoke the real raise path rather than passing the kwarg by hand: the
    stamp is what makes the refusal above reachable in production."""
    from app.api.vendors import _flag_payable_invoices_for_bank_change
    from app.models.vendor import Vendor

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=info.org_id,
                name="Globex Corporation",
                bank_details={"account_number": "12345678"},
            )
        )
        await s.commit()

    inv = Invoice(
        id=uuid.uuid4(),
        organization_id=info.org_id,
        invoice_number="INV-SOD-011",
        vendor_id=vendor_id,
        vendor_name="Globex Corporation",
        amount=Decimal("9000.00"),
        currency="USD",
        status="approved",
        uploaded_by_id=info.users["ap_clerk"],
    )
    async with mk() as s:
        s.add(inv)
        await s.commit()

    async with mk() as s:
        vendor = await s.get(Vendor, vendor_id)
        await _flag_payable_invoices_for_bank_change(s, vendor=vendor, actor_id=info.users["admin"])
        await s.commit()

    async with mk() as s:
        raised = (
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == inv.id,
                        APException.exception_type == "fraud_flag",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(raised) == 1
    assert raised[0].raised_by_user_id == info.users["admin"], (
        "the approver whose act re-pointed the money must be recorded, or they "
        "can clear every flag it raised"
    )


# ---------------------------------------------------------------------------
# /bulk/resolve — per row, never the batch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_resolve_refuses_per_row_and_still_clears_the_rest(realdb):
    """A refusal is another per-row ``skipped`` reason, alongside ``not_found``
    and ``already_*``. Taking down a whole batch because one row is refused is a
    defect this repo already shipped once on the payment queue."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    mine = await _make_invoice(
        mk, info.org_id, number="INV-SOD-020", uploaded_by_id=info.users["admin"]
    )
    theirs = await _make_invoice(
        mk, info.org_id, number="INV-SOD-021", uploaded_by_id=info.users["ap_manager"]
    )
    system = await _make_invoice(mk, info.org_id, number="INV-SOD-022", uploaded_by_id=None)
    exc_mine = await _open_exception(mk, info.org_id, mine)
    exc_theirs = await _open_exception(mk, info.org_id, theirs)
    exc_system = await _open_exception(mk, info.org_id, system)
    missing = uuid.uuid4()

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            "/api/exceptions/bulk/resolve",
            json={
                "ids": [str(exc_mine), str(exc_theirs), str(exc_system), str(missing)],
                "action": "resolve",
                "resolution": "Bulk sweep after a dedup tuning pass.",
            },
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["updated"] == 2
    reasons = {row["id"]: row["reason"] for row in body["skipped"]}
    assert reasons[str(exc_mine)] == "segregation_implicated"
    assert reasons[str(missing)] == "not_found"
    assert len(reasons) == 2

    assert await _status(mk, exc_mine) == "open"
    assert await _status(mk, exc_theirs) == "resolved"
    assert await _status(mk, exc_system) == "resolved"


@pytest.mark.asyncio
async def test_bulk_escalate_is_never_refused(realdb):
    """The bulk verbs share the route, so the escalate exemption has to hold
    there too — otherwise an operator's only escape from a refused row is to
    open it one at a time."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-023", uploaded_by_id=info.users["admin"]
    )
    exc_id = await _open_exception(mk, info.org_id, inv)

    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(
            "/api/exceptions/bulk/resolve",
            json={
                "ids": [str(exc_id)],
                "action": "escalate",
                "resolution": "Mine — handing these on.",
            },
        )
    assert res.status_code == 200, res.text
    assert res.json()["updated"] == 1
    assert await _status(mk, exc_id) == "escalated"


# ---------------------------------------------------------------------------
# The predicate, unit-level. No DB — these pin the reading of the setting and
# the shape of the vocabulary, which the HTTP cases above exercise but cannot
# enumerate.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "settings,expected",
    [
        (None, True),
        ({}, True),
        ({"exceptions": None}, True),
        ({"exceptions": {}}, True),
        ({"exceptions": {"require_segregation": True}}, True),
        # A truthy-looking string is NOT an opt-out. Settings arrive as JSON from
        # an operator-facing form, and `"false"` is the classic way a boolean
        # arrives as text — reading it as False would disable a SOC 2 control by
        # typo. Only the real `false` counts.
        ({"exceptions": {"require_segregation": "false"}}, True),
        ({"exceptions": {"require_segregation": 0}}, True),
        ({"exceptions": {"require_segregation": None}}, True),
        ({"exceptions": {"require_segregation": False}}, False),
    ],
)
def test_only_an_explicit_false_disables_the_control(settings, expected):
    from app.services.exception_lifecycle import exception_segregation_enabled

    assert exception_segregation_enabled(settings) is expected


def test_escalate_is_not_a_clearing_action():
    """Load-bearing rather than an omission: an escalated row still blocks a
    payment run, and escalation is the refused actor's only exit."""
    from app.services.exception_lifecycle import CLEARING_ACTIONS, RESOLUTION_STATUSES

    assert CLEARING_ACTIONS == {"resolve", "dismiss"}
    assert "escalate" in RESOLUTION_STATUSES, "the verb still exists; it is just never refused"
    assert CLEARING_ACTIONS < set(RESOLUTION_STATUSES)


def test_every_refusal_code_has_a_message():
    """The codes are the wire contract for `/bulk/resolve`'s per-row reason, and
    the messages are what the single-row 403 says. A code with no message would
    fall back to the wrong sentence."""
    from app.services.exception_lifecycle import (
        REFUSAL_IMPLICATED,
        REFUSAL_MESSAGES,
        REFUSAL_RAISER,
        refusal_message,
    )

    assert set(REFUSAL_MESSAGES) == {REFUSAL_RAISER, REFUSAL_IMPLICATED}
    for code, message in REFUSAL_MESSAGES.items():
        assert refusal_message(code) == message
        assert message.startswith("Segregation of duties:")
        # Every message tells the caller the exit exists.
        assert "Escalate" in message


def test_the_scope_is_the_payment_run_gate_and_nothing_else():
    """`is_payment_blocking` reads `api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES`,
    so there is no second classification to keep in step — adding a type to that
    tuple extends this control for free. Pinned so a refactor that forks the
    definition is caught."""
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES
    from app.services.exception_lifecycle import EXCEPTION_TYPES, is_payment_blocking

    in_scope = {t for t in EXCEPTION_TYPES if is_payment_blocking(t)}
    assert in_scope == set(PAYMENT_BLOCKING_EXCEPTION_TYPES)
    assert in_scope, "a vacuous scope would make the whole control inert"


# ---------------------------------------------------------------------------
# The other two doors through `record_decision`. Both must stay unaffected —
# a refusal on either is an outage, not a control.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_supplier_resubmission_still_clears_its_own_rejection(realdb):
    """``api/portal`` resolves the ``review_rejected`` exception when the
    supplier re-uploads, with ``actor_id=None`` (the actor is a tenant-scoped
    ``VendorUser``, who holds no control-plane identity). Two independent
    reasons keep it permissive — the type is not payment-blocking and the actor
    is NULL — and a refusal here would strand every resubmitted invoice."""
    from app.services.exception_lifecycle import record_decision

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-030", uploaded_by_id=info.users["admin"]
    )
    exc_id = await _open_exception(
        mk, info.org_id, inv, exception_type="review_rejected", raised_by_user_id=None
    )

    async with mk() as s:
        exc = await s.get(APException, exc_id)
        row = await s.get(Invoice, inv.id)
        status = await record_decision(
            s,
            exception=exc,
            action="resolve",
            resolution="Superseded by supplier resubmission",
            actor_id=None,
            actor_name="Supplier portal",
            invoice=row,
        )
        await s.commit()
    assert status == "resolved"


@pytest.mark.asyncio
async def test_releasing_a_compliance_hold_still_clears_its_exception(realdb):
    """``payments._resolve_compliance_hold_exception`` passes the releasing
    operator. ``payment_compliance_hold`` is not payment-blocking — clearing the
    row does not touch ``payment.status``, so it releases nothing; the control on
    that money is ``POST /payments/{id}/compliance/release``, which is gated on
    ``payment.execute`` and re-runs the full compliance gate. Refusing the queue
    row would only desynchronise the queue from the payment."""
    from app.api.payments import _resolve_compliance_hold_exception

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv = await _make_invoice(
        mk, info.org_id, number="INV-SOD-031", uploaded_by_id=info.users["admin"]
    )
    exc_id = await _open_exception(
        mk, info.org_id, inv, exception_type="payment_compliance_hold", raised_by_user_id=None
    )

    async with mk() as s:
        row = await s.get(Invoice, inv.id)
        await _resolve_compliance_hold_exception(
            s,
            invoice=row,
            actor_id=info.users["admin"],
            actor_name="admin",
            resolution="released",
        )
        await s.commit()
    assert await _status(mk, exc_id) == "resolved"
