"""Maker-checker segregation on the payment-run money path.

The user who CREATES a payment run must not be the one who EXECUTES it (the
money-movement step) or CFO-approves it. This is orthogonal to the role split:
the default `ap_manager` holds both `payment_run.approve` and `payment_execute`,
so without this identity check one user could run the whole payment lifecycle
solo. Default-on; per-org opt-out for single-operator accounts.

Pure-helper tests are DB-free; the wiring tests use `realdb` (the endpoint
functions are called directly, like test_payment_concurrency).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.services.payment_adapters import PaymentStatus
from app.services.payment_controls import check_run_segregation, run_segregation_enabled

# ── Pure helper ───────────────────────────────────────────────────────


def test_segregation_enabled_by_default():
    assert run_segregation_enabled(None) is True
    assert run_segregation_enabled({}) is True
    assert run_segregation_enabled({"provider": "mock"}) is True


def test_segregation_disabled_only_by_explicit_false():
    assert run_segregation_enabled({"require_run_segregation": False}) is False
    # Any non-False value keeps the secure default.
    assert run_segregation_enabled({"require_run_segregation": True}) is True
    assert run_segregation_enabled({"require_run_segregation": "no"}) is True


def test_check_raises_when_actor_created_the_run():
    actor = uuid.uuid4()
    with pytest.raises(HTTPException) as ei:
        check_run_segregation(actor, actor, {}, action="execute")
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "payment_run_segregation"
    assert "execute" in ei.value.detail["message"]


def test_check_passes_for_a_different_actor():
    check_run_segregation(uuid.uuid4(), uuid.uuid4(), {}, action="execute")  # no raise


def test_check_skips_when_opted_out():
    actor = uuid.uuid4()
    # Same actor, but the org opted out → allowed.
    check_run_segregation(actor, actor, {"require_run_segregation": False}, action="execute")


def test_check_skips_when_initiated_by_is_null():
    # Legacy run with no recorded creator — nothing to compare against.
    check_run_segregation(None, uuid.uuid4(), {}, action="approve")


def test_action_verb_appears_in_message():
    actor = uuid.uuid4()
    with pytest.raises(HTTPException) as ei:
        check_run_segregation(actor, actor, {}, action="approve")
    assert ei.value.detail["code"] == "payment_run_segregation"
    assert "approve" in ei.value.detail["message"]


# ── Endpoint wiring (realdb) ──────────────────────────────────────────


def _user(uid: uuid.UUID):
    return SimpleNamespace(id=uid, full_name="Tester", roles=["admin"])


def _org(org_id: uuid.UUID, *, settings: dict | None = None):
    return SimpleNamespace(
        id=org_id,
        name="PyTest",
        slug="pytesta",
        settings=settings if settings is not None else {"payments": {"provider": "mock"}},
    )


async def _seed_invoice(mk, org_id, *, amount=Decimal("100.00")) -> SimpleNamespace:
    inv_id = uuid.uuid4()
    corr = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme Corp",
                amount=amount,
                currency="USD",
                status=InvoiceStatus.approved,
                organization_id=org_id,
                correlation_id=corr,
            )
        )
        await s.commit()
    return SimpleNamespace(id=inv_id, correlation_id=corr)


async def _seed_draft_run(mk, org_id, *, initiated_by, requires_cfo=False) -> uuid.UUID:
    inv = await _seed_invoice(mk, org_id)
    run_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=org_id,
                status="draft",
                total_amount=Decimal("100.00"),
                initiated_by=initiated_by,
                requires_cfo_approval=requires_cfo,
            )
        )
        await s.flush()
        s.add(
            Payment(
                id=uuid.uuid4(),
                invoice_id=inv.id,
                payment_run_id=run_id,
                amount=Decimal("100.00"),
                method="ach",
                status="pending",
                correlation_id=inv.correlation_id,
            )
        )
        await s.commit()
    return run_id


def _mock_adapter():
    async def _create_payment(payload):
        return SimpleNamespace(
            success=True,
            status=PaymentStatus.completed,
            provider_payment_id="px_1",
            reference="REF-1",
            failure_reason=None,
        )

    return SimpleNamespace(provider_name="mock", create_payment=_create_payment)


@pytest.mark.asyncio
async def test_execute_refuses_the_user_who_created_the_run(realdb):
    from app.api.payments import execute_payment_run

    info = realdb.info("a")
    org_id = info.org_id
    creator = info.users["admin"]
    mk = realdb.sessionmaker("a")
    run_id = await _seed_draft_run(mk, org_id, initiated_by=creator)

    async with realdb.sessionmaker("a")() as db:
        with pytest.raises(HTTPException) as ei:
            await execute_payment_run(
                run_id=run_id, db=db, org=_org(org_id), user=_user(creator), entity_id=None
            )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "payment_run_segregation"
    assert "execute" in ei.value.detail["message"]

    # Nothing moved: the run is still draft.
    async with mk() as s:
        run = (await s.execute(select_run(run_id))).scalar_one()
        assert run.status == "draft"


@pytest.mark.asyncio
async def test_execute_proceeds_for_a_different_actor(realdb):
    from app.api.payments import execute_payment_run

    info = realdb.info("a")
    org_id = info.org_id
    mk = realdb.sessionmaker("a")
    # Creator differs from the executing admin → maker-checker satisfied.
    run_id = await _seed_draft_run(mk, org_id, initiated_by=info.users["ap_manager"])

    async with realdb.sessionmaker("a")() as db:
        with (
            patch("app.api.payments.get_payment_adapter", return_value=_mock_adapter()),
            patch("app.api.payments.transition_invoice", new_callable=AsyncMock),
            patch("app.services.payment_erp_sync.dispatch_payment_sync", new_callable=AsyncMock),
        ):
            res = await execute_payment_run(
                run_id=run_id,
                db=db,
                org=_org(org_id),
                user=_user(info.users["admin"]),
                entity_id=None,
            )
    assert res["status"] in ("completed", "submitted", "partial")


@pytest.mark.asyncio
async def test_execute_opt_out_allows_same_actor(realdb):
    from app.api.payments import execute_payment_run

    info = realdb.info("a")
    org_id = info.org_id
    creator = info.users["admin"]
    mk = realdb.sessionmaker("a")
    run_id = await _seed_draft_run(mk, org_id, initiated_by=creator)

    opted_out = _org(
        org_id, settings={"payments": {"provider": "mock", "require_run_segregation": False}}
    )
    async with realdb.sessionmaker("a")() as db:
        with (
            patch("app.api.payments.get_payment_adapter", return_value=_mock_adapter()),
            patch("app.api.payments.transition_invoice", new_callable=AsyncMock),
            patch("app.services.payment_erp_sync.dispatch_payment_sync", new_callable=AsyncMock),
        ):
            res = await execute_payment_run(
                run_id=run_id, db=db, org=opted_out, user=_user(creator), entity_id=None
            )
    # Same actor, but the org opted out → execution proceeds.
    assert res["status"] in ("completed", "submitted", "partial")


@pytest.mark.asyncio
async def test_cfo_approve_refuses_the_user_who_created_the_run(realdb):
    from app.api.payments import approve_payment_run

    info = realdb.info("a")
    org_id = info.org_id
    creator = info.users["admin"]
    mk = realdb.sessionmaker("a")
    run_id = await _seed_draft_run(mk, org_id, initiated_by=creator, requires_cfo=True)

    async with realdb.sessionmaker("a")() as db:
        with pytest.raises(HTTPException) as ei:
            await approve_payment_run(
                run_id=run_id, db=db, org=_org(org_id), user=_user(creator), entity_id=None
            )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "payment_run_segregation"
    assert "approve" in ei.value.detail["message"]


@pytest.mark.asyncio
async def test_resume_refuses_the_user_who_created_the_run(realdb):
    """Resuming a stuck `executing` run dispatches real payments exactly like
    /execute — same maker-checker gate. Without it, a run's own initiator
    could wait for (or force) it into `executing` and resume-execute their
    own run solo, after already being refused at /execute."""
    from app.api.payments import resume_payment_run

    info = realdb.info("a")
    org_id = info.org_id
    creator = info.users["admin"]
    mk = realdb.sessionmaker("a")
    run_id = await _seed_draft_run(mk, org_id, initiated_by=creator)

    # Simulate a crash mid-execute: the run is stuck `executing` with its
    # payment still `pending` (exactly resume's documented use case).
    async with mk() as s:
        run = (await s.execute(select_run(run_id))).scalar_one()
        run.status = "executing"
        await s.commit()

    async with realdb.sessionmaker("a")() as db:
        with pytest.raises(HTTPException) as ei:
            await resume_payment_run(
                run_id=run_id, db=db, org=_org(org_id), user=_user(creator), entity_id=None
            )
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "payment_run_segregation"
    assert "execute" in ei.value.detail["message"]

    # Nothing moved: the payment is still pending, the run still executing.
    async with mk() as s:
        run = (await s.execute(select_run(run_id))).scalar_one()
        assert run.status == "executing"


@pytest.mark.asyncio
async def test_resume_proceeds_for_a_different_actor(realdb):
    from app.api.payments import resume_payment_run

    info = realdb.info("a")
    org_id = info.org_id
    mk = realdb.sessionmaker("a")
    # Creator differs from the resuming admin → maker-checker satisfied.
    run_id = await _seed_draft_run(mk, org_id, initiated_by=info.users["ap_manager"])
    async with mk() as s:
        run = (await s.execute(select_run(run_id))).scalar_one()
        run.status = "executing"
        await s.commit()

    async with realdb.sessionmaker("a")() as db:
        with (
            patch("app.api.payments.get_payment_adapter", return_value=_mock_adapter()),
            patch("app.api.payments.transition_invoice", new_callable=AsyncMock),
            patch("app.services.payment_erp_sync.dispatch_payment_sync", new_callable=AsyncMock),
        ):
            res = await resume_payment_run(
                run_id=run_id,
                db=db,
                org=_org(org_id),
                user=_user(info.users["admin"]),
                entity_id=None,
            )
    assert res["status"] in ("completed", "submitted", "partial")


def select_run(run_id):
    from sqlalchemy import select

    return select(PaymentRun).where(PaymentRun.id == run_id)
