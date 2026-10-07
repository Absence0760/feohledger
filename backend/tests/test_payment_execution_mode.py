"""Record-only payment mode — FeohLedger must not "pay" for a tenant with no rail.

Issue #517 / `docs/decisions.md` §251. With no processor configured,
`settings.payments` resolves to the `mock` adapter, which reports every payment
`completed` without moving money: an Execute click would flip invoices to
`paid` with nothing sent. `services/payment_execution_mode` resolves a tenant
to `record_only` (explicitly, on an unknown mode, or — fail-closed — whenever a
deployed environment would otherwise dispatch to `mock`), and every dispatching
endpoint refuses through `api/payments.refuse_record_only`.
"""

from __future__ import annotations

import inspect
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.services.payment_execution_mode import (
    MODE_PROCESSOR,
    MODE_RECORD_ONLY,
    REASON_CONFIGURED,
    REASON_NO_PROCESSOR_DEPLOYED,
    REASON_UNKNOWN_MODE,
    resolve_execution_mode,
)

# ── The resolver (pure) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payments", "deployed", "mode", "reason"),
    [
        # Local dev / CI: the mock is honest there — guard rail 7.
        (None, False, MODE_PROCESSOR, REASON_CONFIGURED),
        ({"provider": "mock"}, False, MODE_PROCESSOR, REASON_CONFIGURED),
        # Deployed with no real processor: never dispatch to the mock.
        (None, True, MODE_RECORD_ONLY, REASON_NO_PROCESSOR_DEPLOYED),
        ({"provider": "mock"}, True, MODE_RECORD_ONLY, REASON_NO_PROCESSOR_DEPLOYED),
        # ... not even when the admin asked for processor mode.
        (
            {"provider": "mock", "mode": "processor"},
            True,
            MODE_RECORD_ONLY,
            REASON_NO_PROCESSOR_DEPLOYED,
        ),
        # A real processor in a deployed environment dispatches.
        ({"provider": "modern_treasury"}, True, MODE_PROCESSOR, REASON_CONFIGURED),
        # Explicit record-only wins over any processor, anywhere.
        (
            {"provider": "modern_treasury", "mode": "record_only"},
            True,
            MODE_RECORD_ONLY,
            REASON_CONFIGURED,
        ),
        ({"mode": "record_only"}, False, MODE_RECORD_ONLY, REASON_CONFIGURED),
        # An unknown mode never guesses towards moving money.
        (
            {"provider": "modern_treasury", "mode": "Processor"},
            True,
            MODE_RECORD_ONLY,
            REASON_UNKNOWN_MODE,
        ),
        ({"mode": "rail"}, False, MODE_RECORD_ONLY, REASON_UNKNOWN_MODE),
    ],
)
def test_resolution(payments, deployed, mode, reason):
    settings = {} if payments is None else {"payments": payments}
    resolved = resolve_execution_mode(settings, deployed=deployed)
    assert (resolved.mode, resolved.reason) == (mode, reason)


def test_malformed_payments_block_is_treated_as_unconfigured():
    assert resolve_execution_mode({"payments": "oops"}, deployed=True).record_only
    assert not resolve_execution_mode({"payments": "oops"}, deployed=False).record_only


def test_every_dispatching_route_resolves_through_the_refusal():
    """The refusal lives in `_require_payment_adapter`, so a dispatching route is
    covered exactly when it resolves its adapter there. Pin that each one does —
    a new dispatcher that calls `get_payment_adapter` directly would bypass the
    record-only gate and fail here."""
    from app.api import payments

    assert "refuse_record_only(org)" in inspect.getsource(payments._require_payment_adapter)
    for fn in (
        payments.execute_payment_run,
        payments.resume_payment_run,
        payments.retry_failed_payments,
        payments.release_compliance_hold,
    ):
        assert "_require_payment_adapter(org)" in inspect.getsource(fn), fn.__name__
    assert "refuse_record_only(org)" in inspect.getsource(payments.create_payment)


# ── Over a real DB ──────────────────────────────────────────────────────


async def _set_payments(realdb, org_id, payments: dict) -> None:
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, org_id)
        settings = dict(org.settings or {})
        settings["payments"] = payments
        org.settings = settings
        await s.commit()


async def _seed_draft_run(mk, org_id) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    inv_id, run_id, pay_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=f"MODE-{uuid.uuid4().hex[:8]}",
                vendor_name="Mode Vendor",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.approved,
            )
        )
        s.add(
            PaymentRun(
                id=run_id,
                organization_id=org_id,
                status="draft",
                total_amount=Decimal("100.00"),
                initiated_by=None,
            )
        )
        await s.flush()
        s.add(
            Payment(
                id=pay_id,
                invoice_id=inv_id,
                payment_run_id=run_id,
                amount=Decimal("100.00"),
                method="ach",
                status="pending",
                correlation_id=uuid.uuid4(),
            )
        )
        await s.commit()
    return inv_id, run_id, pay_id


@pytest.mark.asyncio
async def test_record_only_tenant_cannot_execute_a_run(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    await _set_payments(realdb, info.org_id, {"mode": "record_only"})
    inv_id, run_id, pay_id = await _seed_draft_run(mk, info.org_id)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(f"/api/payments/runs/{run_id}/execute")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "payments_record_only"
    assert detail["params"]["reason"] == REASON_CONFIGURED

    # Nothing was claimed or dispatched.
    async with mk() as s:
        assert (await s.get(PaymentRun, run_id)).status == "draft"
        assert (await s.get(Payment, pay_id)).status == "pending"
        assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.approved


@pytest.mark.asyncio
async def test_deployed_tenant_on_the_mock_cannot_execute(realdb):
    """The fail-closed case: no setting at all, in a deployed environment."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    await _set_payments(realdb, info.org_id, {"provider": "mock"})
    _, run_id, pay_id = await _seed_draft_run(mk, info.org_id)

    from app.config import settings as app_settings

    with patch.object(app_settings, "environment", "production"):
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(f"/api/payments/runs/{run_id}/execute")
            mode = await c.get("/api/payments/execution-mode")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["params"]["reason"] == REASON_NO_PROCESSOR_DEPLOYED
    assert mode.json() == {"mode": "record_only", "reason": REASON_NO_PROCESSOR_DEPLOYED}
    async with mk() as s:
        assert (await s.get(Payment, pay_id)).status == "pending"


@pytest.mark.asyncio
async def test_record_only_tenant_cannot_book_a_standalone_payment(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    await _set_payments(realdb, info.org_id, {"mode": "record_only"})
    inv_id, _, _ = await _seed_draft_run(mk, info.org_id)
    other_inv = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=other_inv,
                organization_id=info.org_id,
                invoice_number="MODE-STANDALONE",
                vendor_name="Mode Vendor",
                amount=Decimal("80.00"),
                currency="USD",
                status=InvoiceStatus.approved,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/payments", json={"invoice_id": str(other_inv)})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "payments_record_only"
    async with mk() as s:
        rows = (
            (await s.execute(select(Payment).where(Payment.invoice_id == other_inv)))
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.asyncio
async def test_execution_mode_endpoint_reports_processor_in_local_dev(realdb):
    info = realdb.info("a")
    await _set_payments(realdb, info.org_id, {"provider": "mock"})
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/payments/execution-mode")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"mode": "processor", "reason": REASON_CONFIGURED}


@pytest.mark.asyncio
async def test_an_unknown_mode_is_refused_at_save(realdb):
    async with realdb.client(key="a", role="admin") as c:
        bad = await c.patch("/api/organization", json={"settings": {"payments": {"mode": "rails"}}})
        good = await c.patch(
            "/api/organization", json={"settings": {"payments": {"mode": "record_only"}}}
        )
    assert bad.status_code == 422, bad.text
    assert "payments.mode" in bad.json()["detail"]
    assert good.status_code == 200, good.text


@pytest.mark.asyncio
async def test_record_only_tenant_cannot_mint_a_virtual_card(realdb):
    """A minted card is spendable — the run's card leg is refused on a
    record-only tenant, so the direct mint must be too."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id, _, _ = await _seed_draft_run(mk, info.org_id)
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, info.org_id)
        org.settings = {
            **(org.settings or {}),
            "payments": {"mode": "record_only"},
            "cards": {"enabled": True},
        }
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/cards/generate", json={"invoice_ids": [str(inv_id)]})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "payments_record_only"
