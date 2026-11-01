"""Approval sends the invoice to the ERP when its workflow says so (decisions §304).

Found live against Xero: tenant `acme`'s default workflow had the ERP export
step's `auto_send_on_approval: true`, a manager approved an invoice, and it sat
at `approved` until someone clicked "Send to ERP". Nothing in the backend read
the flag.

Pinned here, on real Postgres over real HTTP:

* approval through `/approve` (and `/complete`'s amount floor) starts the push
  when the frozen snapshot has an enabled `erp_export` step with the flag set
  and the org has a LIVE ERP: `sending_to_erp`, an `invoice.erp_submitted`
  row marked `trigger: auto_send_on_approval`, one dispatch;
* nothing happens with the flag off or absent, the step disabled, no ERP, the
  mock ERP, or no workflow instance;
* the SNAPSHOT decides, never the live definition, in both directions;
* the dispatch runs after the approval has committed, and an approval that
  rolls back sends nothing;
* a gate refusal (an open duplicate, §286; the plan, §258) leaves the invoice
  `approved`, the approval answers 200, and an `invoice.erp_auto_send_refused`
  row records the coded refusal;
* a second run cannot dispatch twice — the transition under the row lock is
  the guard;
* the manual doors share the helper, so a refusal there is unchanged.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.workflow import AuditLog, WorkflowInstance
from app.services.erp_send import (
    AUTO_SEND_REFUSED_ACTION,
    AUTO_SEND_TRIGGER,
    auto_send_configured,
)
from app.services.post_commit import drain_post_commit

TENANT = "a"
LIVE_ERP = {"type": "netsuite", "integration_method": "direct", "account_id": "1234567"}
MOCK_ERP = {"type": "mock", "integration_method": "direct"}
MERGE_ERP = {"type": "sage_intacct", "integration_method": "merge_dev"}


def _snapshot(*, auto_send: object = True, enabled: bool = True, approval: dict | None = None):
    erp_config: dict = {"export_format": "json"}
    if auto_send is not None:
        erp_config["auto_send_on_approval"] = auto_send
    return {
        "steps": [
            {
                "number": 2,
                "type": "approval",
                "enabled": True,
                "config": {"required": True, **(approval or {})},
            },
            {"number": 3, "type": "erp_export", "enabled": enabled, "config": erp_config},
        ]
    }


# ---------------------------------------------------------------------------
# the predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (_snapshot(), True),
        (_snapshot(auto_send=False), False),
        # The builder renders a missing key as the switch OFF.
        (_snapshot(auto_send=None), False),
        # Only the literal True: a truthy string is not a decision anyone made.
        (_snapshot(auto_send="true"), False),
        (_snapshot(enabled=False), False),
        ({"steps": [{"type": "approval", "enabled": True, "config": {}}]}, False),
        (None, False),
        ({}, False),
        ({"steps": "not-a-list-of-steps"}, False),
    ],
)
def test_auto_send_configured_reads_only_an_enabled_step_with_the_literal_flag(snapshot, expected):
    assert auto_send_configured(snapshot) is expected


async def test_saving_a_workflow_without_the_flag_stores_it_off(realdb):
    """Every save runs a step through `ErpExportStepConfig` and stores the dump,
    so the schema default IS what an unset switch becomes. It was `True`: an
    admin who changed an approval threshold on a workflow the builder showed
    with the switch off armed the push without seeing it (decisions §304)."""
    steps = [
        {"number": 2, "type": "approval", "name": "Approval", "enabled": True, "config": {}},
        {
            "number": 3,
            "type": "erp_export",
            "name": "ERP",
            "enabled": True,
            "config": {"export_format": "json"},
        },
    ]
    async with realdb.client(key=TENANT, role="admin") as c:
        created = await c.post("/api/workflows", json={"name": "No flag", "steps": steps})
        assert created.status_code == 201, created.text
        stored = created.json()["steps_config"]
        assert auto_send_configured(stored) is False
        [erp_step] = [s for s in stored["steps"] if s["type"] == "erp_export"]
        assert erp_step["config"]["auto_send_on_approval"] is False

        # Setting it is a choice the save keeps.
        steps[1]["config"]["auto_send_on_approval"] = True
        patched = await c.patch(f"/api/workflows/{created.json()['id']}", json={"steps": steps})
        assert patched.status_code == 200, patched.text
        assert auto_send_configured(patched.json()["steps_config"]) is True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _write_erp(realdb, erp: dict | None) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info(TENANT).org_id)
        merged = dict(org.settings or {})
        if erp is None:
            merged.pop("erp", None)
        else:
            merged["erp"] = erp
        org.settings = merged
        flag_modified(org, "settings")
        await s.commit()


async def _seed(
    realdb,
    *,
    snapshot: dict | None,
    status: InvoiceStatus = InvoiceStatus.ready_for_review,
    amount: str = "1234.56",
    number: str | None = None,
    uploaded_by_id: uuid.UUID | None = None,
    inv_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """An invoice awaiting review whose frozen workflow is ``snapshot``
    (``None`` → no workflow instance at all). No uploader, so segregation of
    duties lets any manager approve it."""
    from app.services.workflow_engine import get_or_create_workflow_definition

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    inv_id = inv_id or uuid.uuid4()
    async with mk() as s:
        inv = Invoice(
            id=inv_id,
            organization_id=info.org_id,
            invoice_number=number or f"AUTO-{inv_id.hex[:8]}",
            vendor_name="Northwind Supplies",
            amount=Decimal(amount),
            currency="USD",
            status=status,
            uploaded_by_id=uploaded_by_id,
        )
        s.add(inv)
        await s.flush()
        if snapshot is not None:
            defn = await get_or_create_workflow_definition(s, info.org_id)
            s.add(
                WorkflowInstance(
                    correlation_id=inv.correlation_id,
                    definition_id=defn.id,
                    invoice_id=inv_id,
                    current_step=1,
                    state="active",
                    state_data={},
                    steps_config_snapshot=snapshot,
                )
            )
        await s.commit()
    return inv_id


def _uid(realdb, role: str) -> uuid.UUID:
    return uuid.UUID(str(realdb.info(TENANT).users[role]))


async def _status(realdb, inv_id) -> InvoiceStatus:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.get(Invoice, inv_id)).status


async def _audit(realdb, inv_id, action: str) -> list[AuditLog]:
    async with realdb.sessionmaker(TENANT)() as s:
        rows = await s.execute(
            select(AuditLog).where(AuditLog.entity_id == inv_id, AuditLog.action == action)
        )
        return list(rows.scalars())


@pytest.fixture
def dispatched(monkeypatch, realdb):
    """Record each dispatch, plus the invoice's status as ANOTHER session reads
    it at that moment — proof the transition committed before the dispatch."""
    calls: list[tuple[uuid.UUID, uuid.UUID | None, InvoiceStatus]] = []

    async def _record(invoice_id, org_id, actor_id, **_kwargs):
        calls.append((invoice_id, actor_id, await _status(realdb, invoice_id)))

    monkeypatch.setattr("app.services.erp_send.dispatch_erp", _record)
    return calls


async def _approve(realdb, inv_id, role: str = "ap_manager"):
    async with realdb.client(key=TENANT, role=role) as c:
        res = await c.post(f"/api/invoices/{inv_id}/approve", json={})
    await drain_post_commit()
    return res


# ---------------------------------------------------------------------------
# approval starts the push
# ---------------------------------------------------------------------------


@pytest.mark.plan("growth")
async def test_approval_sends_to_a_live_erp_when_the_snapshot_says_so(realdb, dispatched):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed(realdb, snapshot=_snapshot())

    res = await _approve(realdb, inv_id)

    assert res.status_code == 200, res.text
    # The approver is answered with the approval; the push is its own transaction.
    assert res.json()["status"] == "approved"
    approver_id = _uid(realdb, "ap_manager")
    assert dispatched == [(inv_id, approver_id, InvoiceStatus.sending_to_erp)]
    assert await _status(realdb, inv_id) == InvoiceStatus.sending_to_erp

    [submitted] = await _audit(realdb, inv_id, "invoice.erp_submitted")
    assert submitted.details["trigger"] == AUTO_SEND_TRIGGER
    assert submitted.details["old_status"] == "approved"
    assert submitted.actor_id == approver_id
    # The approval's own row is untouched and comes first.
    [approved] = await _audit(realdb, inv_id, "invoice.approved")
    assert approved.created_at <= submitted.created_at


@pytest.mark.plan("growth")
@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(auto_send=False),
        _snapshot(auto_send=None),
        _snapshot(enabled=False),
        None,  # no workflow instance: there is no snapshot to say so
    ],
    ids=["flag-off", "flag-absent", "step-disabled", "no-instance"],
)
async def test_approval_does_not_send_when_the_snapshot_does_not_say_so(
    realdb, dispatched, snapshot
):
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed(realdb, snapshot=snapshot)

    res = await _approve(realdb, inv_id)

    assert res.status_code == 200, res.text
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved
    assert await _audit(realdb, inv_id, "invoice.erp_submitted") == []
    assert await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION) == []


@pytest.mark.parametrize("erp", [None, MOCK_ERP], ids=["no-erp", "mock-erp"])
async def test_approval_does_not_send_without_a_live_erp(realdb, dispatched, erp):
    """The mock ERP is a local stand-in, and no ERP has nothing to push to."""
    await _write_erp(realdb, erp)
    inv_id = await _seed(realdb, snapshot=_snapshot())

    res = await _approve(realdb, inv_id)

    assert res.status_code == 200, res.text
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved
    assert await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION) == []


@pytest.mark.plan("growth")
async def test_the_snapshot_decides_not_the_live_definition(realdb, dispatched):
    """Editing the definition after the invoice was created changes nothing for
    it — in either direction."""
    from app.models.workflow import WorkflowDefinition

    await _write_erp(realdb, LIVE_ERP)
    frozen_off = await _seed(realdb, snapshot=_snapshot(auto_send=False))
    frozen_on = await _seed(realdb, snapshot=_snapshot(auto_send=True))

    # The live default now says the opposite of each snapshot.
    async with realdb.sessionmaker(TENANT)() as s:
        defn = (
            await s.execute(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.organization_id == realdb.info(TENANT).org_id,
                    WorkflowDefinition.is_default.is_(True),
                )
            )
        ).scalar_one()
        defn.steps_config = _snapshot(auto_send=True)
        await s.commit()
    assert (await _approve(realdb, frozen_off)).status_code == 200
    assert dispatched == []

    async with realdb.sessionmaker(TENANT)() as s:
        defn = await s.get(WorkflowDefinition, defn.id)
        defn.steps_config = _snapshot(auto_send=False)
        await s.commit()
    assert (await _approve(realdb, frozen_on)).status_code == 200
    assert [call[0] for call in dispatched] == [frozen_on]


@pytest.mark.plan("growth")
async def test_the_amount_floor_auto_approval_sends_too(realdb, dispatched):
    """`/complete`'s auto-approve below a floor is an approval like any other."""
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed(
        realdb,
        snapshot=_snapshot(approval={"auto_approve_below": "5000"}),
        status=InvoiceStatus.new,
    )

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/invoices/{inv_id}/complete")
    await drain_post_commit()

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "approved"
    assert [call[0] for call in dispatched] == [inv_id]
    assert await _status(realdb, inv_id) == InvoiceStatus.sending_to_erp


@pytest.mark.plan("growth")
async def test_an_extraction_auto_approval_runs_the_auto_send_after_its_commit(
    realdb, monkeypatch, dispatched
):
    """The extraction worker's auto-approve is an approval too. It calls the
    auto-send itself once its own commit returns (its post-commit work runs
    inline while the committing session still holds the worker's one
    connection). Runs the real `run_extraction` with the mock reader."""
    from app.services.extraction import run_extraction

    await _write_erp(realdb, LIVE_ERP)
    monkeypatch.setattr(
        "app.services.extraction_dispatch.dispatch_extraction", _no_extraction_dispatch
    )
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        created = await c.post(
            "/api/invoices",
            json={"vendor": "Northwind Supplies", "invoice_number": "AUTO-EXT-1", "amount": "50"},
        )
        assert created.status_code == 201, created.text
        inv_id = uuid.UUID(created.json()["id"])
        attached = await c.post(
            f"/api/invoices/{inv_id}/file",
            files={"file": ("invoice.pdf", b"%PDF-1.4 auto send", "application/pdf")},
        )
        assert attached.status_code == 201, attached.text

    approve_anything = _snapshot()
    approve_anything["steps"].insert(
        0,
        {
            "number": 1,
            "type": "extraction",
            "enabled": True,
            "config": {"auto_approve_enabled": True, "auto_approve_threshold": 0.0},
        },
    )
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        inst = (
            await s.execute(select(WorkflowInstance).where(WorkflowInstance.invoice_id == inv_id))
        ).scalar_one()
        inst.steps_config_snapshot = approve_anything
        await s.commit()

    async with mk() as s:
        row = await s.get(Invoice, inv_id)
        await run_extraction(
            s,
            row,
            actor_id=_uid(realdb, "ap_manager"),
            org_settings={
                "extraction": {"program_type": "byok", "provider": "mock"},
                "erp": LIVE_ERP,
            },
        )
        # The caller's objects are its own: the send ran on another session.
        assert row.status is InvoiceStatus.approved

    [auto_approved] = await _audit(realdb, inv_id, "invoice.auto_approved")
    assert auto_approved.details["auto_approved"] is True
    # The mock reader returns a round 1500.00, which the fraud rules flag, and
    # an open `fraud_flag` refuses the push (§286). That the auto-send saw the
    # flag the extraction committed is what proves it ran after that commit,
    # and the refusal left the auto-approval standing.
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved
    [refused] = await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION)
    assert refused.details["refusal"] == {
        "code": "exception_blocks_erp_export",
        "params": {"exception_type": "fraud_flag"},
    }
    assert refused.actor_id == _uid(realdb, "ap_manager")


async def _no_extraction_dispatch(*_args, **_kwargs) -> None:
    return None


@pytest.mark.plan("growth")
async def test_a_partial_chain_approval_does_not_send(realdb, dispatched):
    """Only the approval that finalises the invoice can start the push."""
    await _write_erp(realdb, LIVE_ERP)
    manager = str(_uid(realdb, "ap_manager"))
    cfo = str(_uid(realdb, "cfo"))
    chain = {
        "approver_strategy": "chain",
        "approval_chain": [
            {"approver_ids": [manager], "required_approvals": 1},
            {"approver_ids": [cfo], "required_approvals": 1},
        ],
    }
    inv_id = await _seed(realdb, snapshot=_snapshot(approval=chain))

    first = await _approve(realdb, inv_id, role="ap_manager")
    assert first.status_code == 200, first.text
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review
    assert dispatched == []

    second = await _approve(realdb, inv_id, role="cfo")
    assert second.status_code == 200, second.text
    assert [call[0] for call in dispatched] == [inv_id]


# ---------------------------------------------------------------------------
# a refusal never undoes the approval
# ---------------------------------------------------------------------------


@pytest.mark.plan("growth")
async def test_an_open_duplicate_refuses_the_auto_send_and_the_invoice_stays_approved(
    realdb, dispatched
):
    from app.services.exception_service import create_exception

    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed(realdb, snapshot=_snapshot())
    async with realdb.sessionmaker(TENANT)() as s:
        await create_exception(
            s,
            exception_type="duplicate",
            severity="warning",
            description="detector output",
            organization_id=realdb.info(TENANT).org_id,
            invoice=await s.get(Invoice, inv_id),
        )
        await s.commit()

    res = await _approve(realdb, inv_id)

    assert res.status_code == 200, res.text
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved
    assert len(await _audit(realdb, inv_id, "invoice.approved")) == 1
    assert await _audit(realdb, inv_id, "invoice.erp_submitted") == []
    [refused] = await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION)
    assert refused.details["refusal"] == {
        "code": "exception_blocks_erp_export",
        "params": {"exception_type": "duplicate"},
    }
    assert refused.details["status_code"] == 409
    assert refused.details["trigger"] == AUTO_SEND_TRIGGER
    # PII guard: the row names a fixed type, never the vendor.
    assert "Northwind" not in str(refused.details)


@pytest.mark.plan("growth")
async def test_a_live_card_refuses_the_auto_send_and_the_invoice_stays_approved(realdb, dispatched):
    """Decisions §309 reaches the auto path through the same gate: an invoice
    FeohLedger is already paying (here a live virtual card, written directly
    since no rail mints before approval) is not pushed on approval."""
    from app.models.virtual_card import VirtualCard

    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed(realdb, snapshot=_snapshot())
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            VirtualCard(
                organization_id=realdb.info(TENANT).org_id,
                invoice_id=inv_id,
                card_provider="mock",
                provider_card_id=f"mock-{inv_id.hex[:8]}",
                amount_limit=Decimal("1234.56"),
                currency="USD",
                status="active",
            )
        )
        await s.commit()

    res = await _approve(realdb, inv_id)

    assert res.status_code == 200, res.text
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved
    assert await _audit(realdb, inv_id, "invoice.erp_submitted") == []
    [refused] = await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION)
    assert refused.details["refusal"] == {
        "code": "payment_blocks_erp_export",
        "params": {"claim": "virtual_card"},
    }
    assert refused.details["status_code"] == 409


@pytest.mark.plan("growth")
async def test_a_blocked_vendor_refuses_the_auto_send_and_the_invoice_stays_approved(
    realdb, dispatched
):
    """The vendor gate reaches the auto path through the same gate: a vendor
    blocked from payment is not booked in the ERP on approval, and the refusal
    is recorded with its fixed `hold`, never a vendor field."""
    from app.models.vendor import Vendor

    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed(realdb, snapshot=_snapshot())
    async with realdb.sessionmaker(TENANT)() as s:
        vendor = Vendor(
            organization_id=realdb.info(TENANT).org_id,
            name="Northwind Supplies",
            status="active",
            payments_blocked=True,
            payments_blocked_reason="manually blocked by AP",
        )
        s.add(vendor)
        await s.flush()
        (await s.get(Invoice, inv_id)).vendor_id = vendor.id
        await s.commit()

    res = await _approve(realdb, inv_id)

    assert res.status_code == 200, res.text
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved
    assert await _audit(realdb, inv_id, "invoice.erp_submitted") == []
    [refused] = await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION)
    assert refused.details["refusal"] == {
        "code": "vendor_blocks_erp_export",
        "params": {"hold": "payments_blocked"},
    }
    assert refused.details["status_code"] == 409
    assert "Northwind" not in str(refused.details)


async def test_a_plan_without_the_erp_feature_refuses_the_auto_send(realdb, dispatched):
    """A Merge.dev ERP on Free (§256/§258): the manual send would 402, so the
    auto-send records that refusal and leaves the invoice approved."""
    from app.services.billing.plan_catalog import FEATURE_ERP_MERGE

    await realdb.subscribe(TENANT, "free")
    await _write_erp(realdb, MERGE_ERP)
    inv_id = await _seed(realdb, snapshot=_snapshot())

    res = await _approve(realdb, inv_id)

    assert res.status_code == 200, res.text
    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.approved
    [refused] = await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION)
    assert refused.details["refusal"] == {
        "code": "plan_feature_required",
        "params": {"feature": FEATURE_ERP_MERGE},
    }
    assert refused.details["status_code"] == 402


# ---------------------------------------------------------------------------
# post-commit, and idempotent
# ---------------------------------------------------------------------------


@pytest.mark.plan("growth")
async def test_an_approval_that_rolls_back_sends_nothing(realdb, dispatched):
    from app.services.review import approve_invoice
    from app.services.workflow_engine import get_invoice_for_update

    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed(realdb, snapshot=_snapshot())

    async with realdb.sessionmaker(TENANT)() as s:
        inv = await get_invoice_for_update(s, inv_id)
        await approve_invoice(
            s,
            inv,
            actor_id=_uid(realdb, "ap_manager"),
            actor_name="Manager",
            actor_roles={"ap_manager"},
            org_settings={"erp": LIVE_ERP},
        )
        # Queued, not run: nothing is dispatched inside the approval transaction.
        assert dispatched == []
        await s.rollback()
    await drain_post_commit()

    assert dispatched == []
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


@pytest.mark.plan("growth")
async def test_a_dispatch_that_raises_lands_the_invoice_at_failed(realdb, monkeypatch):
    """The push committed `sending_to_erp` and the hand-off then raised (an SQS
    outage in lambda mode): nothing would ever run it, and `/retry-erp` takes
    only `failed`. It lands there, audited, rather than stranding."""

    async def _raise(*_args, **_kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr("app.services.erp_send.dispatch_erp", _raise)
    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed(realdb, snapshot=_snapshot())

    res = await _approve(realdb, inv_id)

    assert res.status_code == 200, res.text
    assert await _status(realdb, inv_id) == InvoiceStatus.failed
    [failed] = await _audit(realdb, inv_id, "invoice.erp_failed")
    assert failed.details["error"] == "ERP send could not be queued"
    assert failed.details["old_status"] == "sending_to_erp"


def test_an_approval_inside_a_dispatcher_scope_queues_nothing(monkeypatch):
    """Post-commit work runs inline under a dispatcher scope while the committing
    session still holds the scope's one connection, so no job is queued there."""
    from types import SimpleNamespace

    from app.services import erp_send

    queued: list = []
    monkeypatch.setattr("app.database.in_dispatch_scope", lambda: True)
    monkeypatch.setattr(
        "app.services.post_commit.enqueue_post_commit",
        lambda *a, **k: queued.append(a),
    )
    session = SimpleNamespace(info={}, bind=None)
    invoice = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
    instance = SimpleNamespace(steps_config_snapshot=_snapshot())

    erp_send.schedule_auto_send(session, invoice, instance, actor_id=None)

    assert queued == []
    assert session.info == {}


@pytest.mark.plan("growth")
async def test_a_second_run_cannot_dispatch_twice(realdb, dispatched):
    from app.services.erp_send import send_after_approval

    await _write_erp(realdb, LIVE_ERP)
    inv_id = await _seed(realdb, snapshot=_snapshot())
    assert (await _approve(realdb, inv_id)).status_code == 200
    assert len(dispatched) == 1

    # A retried job for the same approval finds `sending_to_erp` and does nothing.
    engine = realdb.sessionmaker(TENANT)().bind
    await send_after_approval(
        engine,
        invoice_id=inv_id,
        org_id=realdb.info(TENANT).org_id,
        org_settings={"erp": LIVE_ERP},
        actor_id=None,
    )

    assert len(dispatched) == 1
    assert len(await _audit(realdb, inv_id, "invoice.erp_submitted")) == 1
    assert await _audit(realdb, inv_id, AUTO_SEND_REFUSED_ACTION) == []


@pytest.mark.plan("growth")
async def test_a_bulk_approval_sends_each_invoice_once(realdb, dispatched):
    """Every row the batch approved is sent, once; the row it skipped (the
    approver uploaded it — segregation of duties) stays where it was, and its
    rolled-back savepoint does not cost the rows around it their send."""
    await _write_erp(realdb, LIVE_ERP)
    # The batch locks and walks rows in id order: the skipped row sits between
    # two approved ones, so its savepoint rolls back after one was queued.
    low, mid, high = sorted(uuid.uuid4() for _ in range(3))
    first = await _seed(realdb, snapshot=_snapshot(), inv_id=low)
    own = await _seed(
        realdb, snapshot=_snapshot(), uploaded_by_id=_uid(realdb, "ap_manager"), inv_id=mid
    )
    last = await _seed(realdb, snapshot=_snapshot(), inv_id=high)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(
            "/api/invoices/bulk/status",
            json={"ids": [str(i) for i in (first, own, last)], "status": "approved"},
        )
    await drain_post_commit()

    assert res.status_code == 200, res.text
    assert [skip["id"] for skip in res.json()["skipped"]] == [str(own)]
    assert sorted(call[0] for call in dispatched) == sorted([first, last])
    assert await _status(realdb, first) == InvoiceStatus.sending_to_erp
    assert await _status(realdb, last) == InvoiceStatus.sending_to_erp
    assert await _status(realdb, own) == InvoiceStatus.ready_for_review
