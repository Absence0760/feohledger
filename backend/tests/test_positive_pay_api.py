"""Real-DB coverage for the Positive Pay router (``app/api/positive_pay.py``).

Exercises check-issue generation (+ idempotency on the (run, format) slot), the
ACH-authorization file, list / detail / download (with the cross-tenant download
gate), return processing (raising a deduped ``fraud_flag`` Exception on an
altered cheque + a standalone invoice-less one for a never-issued cheque), and
the RBAC read/write split — end-to-end against the live test tenants. The formatter +
classifier math is owned by the separately-tested ``positive_pay`` service; here
we prove the HTTP surface wires it through with exact ``Numeric`` money and the
PII invariant (no account number in the audit trail).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services import storage

_TODAY = date.today()

# A full account number — must NEVER appear in the audit trail.
_ACCOUNT_NUMBER = "9876543210"


async def _default_entity_id(s):
    return (
        await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


async def _set_check_account(realdb, org_id, account=_ACCOUNT_NUMBER, company="Acme Corp"):
    """Stamp the originating cheque account + company name on the control-plane
    Organization.settings (the router reads them from there)."""
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        settings["company"] = {**(settings.get("company") or {}), "name": company}
        settings["payments"] = {
            **(settings.get("payments") or {}),
            "check_account_number": account,
        }
        org.settings = settings
        await s.commit()


async def _clear_settings(realdb, org_id):
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        settings.pop("payments", None)
        settings.pop("company", None)
        org.settings = settings
        await s.commit()


async def _add_vendor(
    mk,
    org_id,
    *,
    name="Globex Industrial",
    bank_details=None,
    status="active",
    payments_blocked=False,
):
    async with mk() as s:
        v = Vendor(
            organization_id=org_id,
            name=name,
            status=status,
            bank_details=bank_details,
            payments_blocked=payments_blocked,
            entity_id=await _default_entity_id(s),
        )
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return str(v.id)


async def _add_invoice(mk, org_id, *, vendor_id, invoice_number, amount="1000.00", currency="USD"):
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=await _default_entity_id(s),
            invoice_number=invoice_number,
            vendor_name="Globex Industrial",
            vendor_id=uuid.UUID(vendor_id),
            amount=Decimal(amount),
            currency=currency,
            invoice_date=_TODAY,
            due_date=_TODAY + timedelta(days=30),
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return str(inv.id)


async def _add_check_run(mk, org_id, *, invoice_id, check_number="CHK1001", amount="1000.00"):
    """Create an executed PaymentRun with one completed check-method Payment."""
    async with mk() as s:
        entity_id = await _default_entity_id(s)
        run = PaymentRun(
            organization_id=org_id,
            entity_id=entity_id,
            status="executed",
            total_amount=Decimal(amount),
            executed_at=datetime.now(UTC),
        )
        s.add(run)
        await s.flush()
        pay = Payment(
            entity_id=entity_id,
            invoice_id=uuid.UUID(invoice_id),
            payment_run_id=run.id,
            amount=Decimal(amount),
            method="check",
            status="completed",
            reference=check_number,
        )
        s.add(pay)
        await s.commit()
        await s.refresh(run)
        return str(run.id)


# ---------------------------------------------------------------------------
# check-issue generation + idempotency
# ---------------------------------------------------------------------------


async def test_generate_check_issue_creates_file_and_audits(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        vendor_id = await _add_vendor(mk, org_id)
        invoice_id = await _add_invoice(
            mk, org_id, vendor_id=vendor_id, invoice_number="INV-1", amount="1234.56"
        )
        run_id = await _add_check_run(
            mk, org_id, invoice_id=invoice_id, check_number="CHK1001", amount="1234.56"
        )

        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.post(
                f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={"bank_format": "csv"}
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["file_type"] == "check_issue"
        assert body["item_count"] == 1
        assert body["total_amount"] == 1234.56
        assert body["account_last4"] == "3210"
        assert body["payment_run_id"] == run_id
        assert body["file_key"] is not None

        async with mk() as s:
            audit = (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "positive_pay.check_issue_generated",
                        AuditLog.entity_id == uuid.UUID(body["id"]),
                    )
                )
            ).scalar_one()
            assert audit.entity_type == "positive_pay_file"
            # PII invariant — no full account number in the audit details.
            assert _ACCOUNT_NUMBER not in str(audit.details)
    finally:
        await _clear_settings(realdb, org_id)


async def _set_reporting_currency(realdb, org_id, currency: str | None) -> None:
    """Set/clear ``Organization.settings.reporting_currency`` (control plane)."""
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        if currency is None:
            settings.pop("reporting_currency", None)
        else:
            settings["reporting_currency"] = currency
        org.settings = settings
        await s.commit()


async def test_generate_check_issue_stamps_the_cheques_currency_not_the_reporting_one(realdb):
    """The file's ``total_amount`` sums ``Payment.amount``, which is denominated
    in each INVOICE's currency — so that is the code the file carries, stored
    and served back, even when the org reports in something else.

    It used to stamp the org's reporting currency: a USD cheque run for a
    EUR-reporting org came back as a EUR file, and ``/positive-pay`` rendered
    the dollar total under a euro sign.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    await _set_reporting_currency(realdb, org_id, "EUR")
    try:
        vendor_id = await _add_vendor(mk, org_id)
        invoice_id = await _add_invoice(
            mk, org_id, vendor_id=vendor_id, invoice_number="INV-USD", currency="usd"
        )
        run_id = await _add_check_run(mk, org_id, invoice_id=invoice_id, check_number="CHKUSD1")

        async with realdb.client(key="a", role="ap_manager") as c:
            gen = await c.post(f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={})
            assert gen.status_code == 201, gen.text
            # Upper-cased: `usd` on the invoice row is the same currency.
            assert gen.json()["currency"] == "USD"
            file_id = gen.json()["id"]

            # List + detail round-trip the stored currency.
            listed = await c.get("/api/positive-pay")
            row = next(f for f in listed.json()["items"] if f["id"] == file_id)
            assert row["currency"] == "USD"
            detail = await c.get(f"/api/positive-pay/{file_id}")
            assert detail.json()["currency"] == "USD"

        # Persisted on the DB row.
        async with mk() as s:
            from app.models.positive_pay import PositivePayFile

            stored = (
                await s.execute(
                    select(PositivePayFile).where(PositivePayFile.id == uuid.UUID(file_id))
                )
            ).scalar_one()
            assert stored.currency == "USD"
    finally:
        await _set_reporting_currency(realdb, org_id, None)
        await _clear_settings(realdb, org_id)


async def test_generate_check_issue_names_no_currency_when_the_cheques_disagree(realdb):
    """A legacy run predating the single-currency guard can hold a USD and a
    EUR cheque; its total is a quantity in neither, so the file names no
    currency rather than borrowing one (decisions §79/§82)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        vendor_id = await _add_vendor(mk, org_id)
        usd = await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="INV-MIX-USD")
        eur = await _add_invoice(
            mk, org_id, vendor_id=vendor_id, invoice_number="INV-MIX-EUR", currency="EUR"
        )
        run_id = await _add_check_run(mk, org_id, invoice_id=usd, check_number="CHKMIX1")
        async with mk() as s:
            s.add(
                Payment(
                    entity_id=await _default_entity_id(s),
                    invoice_id=uuid.UUID(eur),
                    payment_run_id=uuid.UUID(run_id),
                    amount=Decimal("500.00"),
                    method="check",
                    status="completed",
                    reference="CHKMIX2",
                )
            )
            await s.commit()

        async with realdb.client(key="a", role="ap_manager") as c:
            gen = await c.post(f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={})
        assert gen.status_code == 201, gen.text
        assert gen.json()["item_count"] == 2
        assert gen.json()["currency"] is None
    finally:
        await _clear_settings(realdb, org_id)


async def test_generate_ach_authorization_defaults_currency_usd(realdb):
    """No reporting currency set → the file falls back to the platform default."""
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        async with realdb.client(key="a", role="ap_manager") as c:
            gen = await c.post("/api/positive-pay/ach-authorization", json={})
        assert gen.status_code == 201, gen.text
        assert gen.json()["currency"] == "USD"
    finally:
        await _clear_settings(realdb, org_id)


async def test_generate_check_issue_is_idempotent(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        vendor_id = await _add_vendor(mk, org_id)
        invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="INV-2")
        run_id = await _add_check_run(mk, org_id, invoice_id=invoice_id, check_number="CHK2001")

        async with realdb.client(key="a", role="ap_manager") as c:
            first = await c.post(f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={})
            second = await c.post(f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={})
        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]

        async with mk() as s:
            from app.models.positive_pay import PositivePayFile

            rows = (
                (
                    await s.execute(
                        select(PositivePayFile).where(
                            PositivePayFile.payment_run_id == uuid.UUID(run_id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
    finally:
        await _clear_settings(realdb, org_id)


async def test_generate_check_issue_404_unknown_run(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/positive-pay/payment-runs/{uuid.uuid4()}/check-issue", json={})
    assert resp.status_code == 404


async def _add_draft_check_run(mk, org_id, *, invoice_id, amount="1000.00"):
    """A DRAFT payment run: no `executed_at`, and its cheque payment is still
    `pending` with a NULL reference — i.e. nothing has been issued to anyone."""
    async with mk() as s:
        entity_id = await _default_entity_id(s)
        run = PaymentRun(
            organization_id=org_id,
            entity_id=entity_id,
            status="draft",
            total_amount=Decimal(amount),
            executed_at=None,
        )
        s.add(run)
        await s.flush()
        pay = Payment(
            entity_id=entity_id,
            invoice_id=uuid.UUID(invoice_id),
            payment_run_id=run.id,
            amount=Decimal(amount),
            method="check",
            status="pending",
            reference=None,
        )
        s.add(pay)
        await s.commit()
        await s.refresh(run)
        return str(run.id)


async def test_generate_check_issue_refuses_an_unexecuted_run(realdb):
    """A draft run has issued no cheques: its payments are `pending` with no
    reference, so the file's `issued_map` would persist EMPTY — and since the
    (run, format) slot is claimed for good, it could never be regenerated after
    execution. Return processing would then classify every genuinely-issued
    cheque `not_on_file` and raise a fraud_flag against it. 422, and nothing
    persisted."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        vendor_id = await _add_vendor(mk, org_id)
        invoice_id = await _add_invoice(
            mk, org_id, vendor_id=vendor_id, invoice_number="INV-DRAFT-1"
        )
        run_id = await _add_draft_check_run(mk, org_id, invoice_id=invoice_id)

        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.post(
                f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={"bank_format": "csv"}
            )
        assert resp.status_code == 422, resp.text
        assert "executed" in resp.json()["detail"].lower()

        # No file row (and therefore no permanently-claimed idempotency slot).
        async with mk() as s:
            from app.models.positive_pay import PositivePayFile

            count = (
                await s.execute(
                    select(func.count()).where(PositivePayFile.payment_run_id == uuid.UUID(run_id))
                )
            ).scalar()
            assert count == 0
    finally:
        await _clear_settings(realdb, org_id)


# ---------------------------------------------------------------------------
# list / detail / download + cross-tenant gate
# ---------------------------------------------------------------------------


async def test_list_detail_and_download(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        vendor_id = await _add_vendor(mk, org_id)
        invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="INV-3")
        run_id = await _add_check_run(mk, org_id, invoice_id=invoice_id, check_number="CHK3001")

        async with realdb.client(key="a", role="ap_manager") as c:
            file_id = (
                await c.post(f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={})
            ).json()["id"]

        # cfo can read.
        async with realdb.client(key="a", role="cfo") as c:
            listing = (await c.get("/api/positive-pay?file_type=check_issue")).json()
            assert any(i["id"] == file_id for i in listing["items"])
            assert listing["total"] >= 1

            detail = await c.get(f"/api/positive-pay/{file_id}")
            assert detail.status_code == 200

            dl = await c.get(f"/api/positive-pay/{file_id}/download")
            assert dl.status_code == 200
            assert dl.headers["content-type"].startswith("text/csv")
            text = dl.content.decode("utf-8")
            assert "check_number,payee,amount,issue_date,account_number" in text
            assert "CHK3001" in text
    finally:
        await _clear_settings(realdb, org_id)


async def test_summary_is_whole_set_counts_items_and_returns(realdb):
    """`GET /api/positive-pay/summary` counts the whole matching set by status,
    sums `item_count` across it (`itemsExported`) and sums each file's
    `meta.return_summary.{amount_mismatches,not_on_file}` (`returnsFlagged`) —
    the figures the page reduced over the loaded page. Honours the list's
    file_type / status filters."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        vendor_id = await _add_vendor(mk, org_id)
        inv_a = await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="PPS-A")
        inv_b = await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="PPS-B")
        run_a = await _add_check_run(mk, org_id, invoice_id=inv_a, check_number="PPSA001")
        run_b = await _add_check_run(mk, org_id, invoice_id=inv_b, check_number="PPSB001")

        async with realdb.client(key="a", role="ap_manager") as c:
            fa = (
                await c.post(f"/api/positive-pay/payment-runs/{run_a}/check-issue", json={})
            ).json()["id"]
            await c.post(f"/api/positive-pay/payment-runs/{run_b}/check-issue", json={})
            # Flag returns on file A: one altered, one never-issued.
            await c.post(
                f"/api/positive-pay/{fa}/process-return",
                json={
                    "presented_items": [
                        {"check_number": "PPSA001", "amount": "9500.00"},
                        {"check_number": "PPSX999", "amount": "1.00"},
                    ]
                },
            )

            body = (await c.get("/api/positive-pay/summary?file_type=check_issue")).json()
            assert body["total"] == 2
            assert body["items_exported"] == 2  # one check per file
            assert body["returns_flagged"] == 2  # 1 amount_mismatch + 1 not_on_file
            assert sum(body["by_status"].values()) == 2

            # ACH-authorization filter → none of the check-issue files.
            ach = (await c.get("/api/positive-pay/summary?file_type=ach_authorization")).json()
            assert ach["total"] == 0
            assert ach["items_exported"] == 0

        async with realdb.client(key="a", role="cfo") as c:
            assert (await c.get("/api/positive-pay/summary")).status_code == 200
    finally:
        await _clear_settings(realdb, org_id)


async def test_download_cross_tenant_404(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        vendor_id = await _add_vendor(mk, org_id)
        invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="INV-4")
        run_id = await _add_check_run(mk, org_id, invoice_id=invoice_id, check_number="CHK4001")
        async with realdb.client(key="a", role="ap_manager") as c:
            file_id = (
                await c.post(f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={})
            ).json()["id"]
        # Tenant b cannot fetch tenant a's file — scoped 404 (no enumeration).
        async with realdb.client(key="b", role="ap_manager") as c:
            resp = await c.get(f"/api/positive-pay/{file_id}/download")
        assert resp.status_code == 404
    finally:
        await _clear_settings(realdb, org_id)


# ---------------------------------------------------------------------------
# ACH authorization
# ---------------------------------------------------------------------------


async def test_generate_ach_authorization(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    # One ACH-capable vendor + one without bank details (skipped).
    await _add_vendor(
        mk,
        org_id,
        name="ACH Vendor",
        bank_details={"routing_number": "021000021", "account_number": "111222333"},
    )
    await _add_vendor(mk, org_id, name="No-Bank Vendor")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/positive-pay/ach-authorization", json={"bank_format": "csv"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["file_type"] == "ach_authorization"
    assert body["payment_run_id"] is None
    assert body["item_count"] == 1

    async with realdb.client(key="a", role="ap_manager") as c:
        dl = await c.get(f"/api/positive-pay/{body['id']}/download")
    text = dl.content.decode("utf-8")
    assert "vendor_name,routing_number,account_number,status" in text
    assert "ACH Vendor" in text
    assert "No-Bank Vendor" not in text


async def test_generate_ach_authorization_excludes_payments_blocked_vendor(realdb):
    """A sanctions-blocked vendor (``payments_blocked=True``) must never land on the
    ACH-authorization allowlist handed to the bank, even when its general ``status``
    is still ``active`` — the two flags are independent (#184)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_vendor(
        mk,
        org_id,
        name="Blocked ACH Vendor",
        bank_details={"routing_number": "021000021", "account_number": "444555666"},
        status="active",
        payments_blocked=True,
    )
    await _add_vendor(
        mk,
        org_id,
        name="Clean ACH Vendor",
        bank_details={"routing_number": "021000021", "account_number": "111222333"},
        status="active",
        payments_blocked=False,
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post("/api/positive-pay/ach-authorization", json={"bank_format": "csv"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["item_count"] == 1

    async with realdb.client(key="a", role="ap_manager") as c:
        dl = await c.get(f"/api/positive-pay/{body['id']}/download")
    text = dl.content.decode("utf-8")
    assert "Clean ACH Vendor" in text
    assert "Blocked ACH Vendor" not in text


# ---------------------------------------------------------------------------
# process return → fraud_flag Exception + dedupe + unmatched
# ---------------------------------------------------------------------------


async def test_process_return_raises_fraud_including_standalone_not_on_file(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        vendor_id = await _add_vendor(mk, org_id)
        invoice_id = await _add_invoice(
            mk, org_id, vendor_id=vendor_id, invoice_number="INV-5", amount="500.00"
        )
        run_id = await _add_check_run(
            mk, org_id, invoice_id=invoice_id, check_number="CHK5001", amount="500.00"
        )

        async with realdb.client(key="a", role="ap_manager") as c:
            file_id = (
                await c.post(f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={})
            ).json()["id"]

            # Present an ALTERED amount (500 → 900) + a cheque we never issued.
            resp = await c.post(
                f"/api/positive-pay/{file_id}/process-return",
                json={
                    "presented_items": [
                        {"check_number": "CHK5001", "amount": "900.00"},  # altered
                        {"check_number": "CHK9999", "amount": "42.00"},  # not on file
                    ]
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["presented_count"] == 2
        assert body["amount_mismatches"] == 1
        assert body["not_on_file"] == 1
        # BOTH fraud signals raise an exception now — the never-issued cheque is
        # a standalone (invoice-less) fraud_flag, not a buried meta entry.
        assert body["exceptions_created"] == 2
        assert "unmatched" not in body
        assert body["file"]["status"] == "returned_processed"
        assert "unmatched_returns" not in (body["file"]["meta"] or {})

        async with mk() as s:
            # Invoice-scoped fraud_flag for the altered cheque.
            linked = (
                (
                    await s.execute(
                        select(APException).where(
                            APException.invoice_id == uuid.UUID(invoice_id),
                            APException.exception_type == "fraud_flag",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(linked) == 1
            assert linked[0].severity == "error"
            assert linked[0].status == "open"

            # Standalone (invoice_id IS NULL) fraud_flag for the never-issued cheque.
            standalone = (
                (
                    await s.execute(
                        select(APException).where(
                            APException.invoice_id.is_(None),
                            APException.exception_type == "fraud_flag",
                            APException.organization_id == org_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(standalone) == 1
            assert "CHK9999" in (standalone[0].description or "")
            assert standalone[0].status == "open"
            # PII invariant — no account number in either fraud description.
            assert _ACCOUNT_NUMBER not in (linked[0].description or "")
            assert _ACCOUNT_NUMBER not in (standalone[0].description or "")
            # return-processed audit row carries no account number either.
            audit = (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "positive_pay.return_processed",
                        AuditLog.entity_id == uuid.UUID(file_id),
                    )
                )
            ).scalar_one()
            assert _ACCOUNT_NUMBER not in str(audit.details)

        # The standalone fraud exception surfaces in the queue with a null invoice_id.
        async with realdb.client(key="a", role="ap_manager") as c:
            listing = (await c.get("/api/exceptions?type=fraud_flag")).json()
        standalone_rows = [
            r
            for r in listing["items"]
            if r["invoice_id"] is None and "CHK9999" in (r["description"] or "")
        ]
        assert len(standalone_rows) == 1
        assert standalone_rows[0]["invoice_number"] is None

        # Re-running the return must NOT duplicate either fraud_flag exception.
        async with realdb.client(key="a", role="ap_manager") as c:
            again = await c.post(
                f"/api/positive-pay/{file_id}/process-return",
                json={
                    "presented_items": [
                        {"check_number": "CHK5001", "amount": "900.00"},
                        {"check_number": "CHK9999", "amount": "42.00"},
                    ]
                },
            )
        assert again.status_code == 200
        assert again.json()["exceptions_created"] == 0

        async with mk() as s:
            total_fraud = (
                await s.execute(
                    select(func.count()).where(
                        APException.exception_type == "fraud_flag",
                        APException.organization_id == org_id,
                    )
                )
            ).scalar()
            assert total_fraud == 2
    finally:
        await _clear_settings(realdb, org_id)


async def test_altered_cheque_raises_even_when_the_invoice_already_has_a_fraud_flag(realdb):
    """An UNRELATED open `fraud_flag` must not absorb the bank's altered-cheque
    signal.

    The dedupe key was `(invoice_id, "fraud_flag")` — right for a re-derived
    per-invoice rule, wrong for a bank return. An invoice can already carry an
    open `fraud_flag` from something else entirely (the round-amount rule, at
    severity `info`; a settlement-amount divergence; an earlier cheque), and
    that row silently swallowed the ALTERED-cheque exception: `process-return`
    reported `amount_mismatches: 1, exceptions_created: 0` and the queue showed
    only the pre-existing description. The fraud signal Positive Pay exists to
    deliver never arrived. The key is now the description (which carries the
    cheque number + verdict), narrowed by invoice.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        vendor_id = await _add_vendor(mk, org_id, name="Prior Flag Co")
        invoice_id = await _add_invoice(
            mk, org_id, vendor_id=vendor_id, invoice_number="INV-PRIORFLAG", amount="1000.00"
        )
        run_id = await _add_check_run(
            mk, org_id, invoice_id=invoice_id, check_number="CHKPF001", amount="1000.00"
        )

        # An unrelated, lower-severity fraud_flag already open on this invoice —
        # exactly what `invoice_warnings` raises for a round amount.
        async with mk() as s:
            s.add(
                APException(
                    organization_id=org_id,
                    entity_id=await _default_entity_id(s),
                    invoice_id=uuid.UUID(invoice_id),
                    exception_type="fraud_flag",
                    severity="info",
                    status="open",
                    description="Suspicious round amount: $1000.00",
                )
            )
            await s.commit()

        async with realdb.client(key="a", role="ap_manager") as c:
            file_id = (
                await c.post(f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={})
            ).json()["id"]
            resp = await c.post(
                f"/api/positive-pay/{file_id}/process-return",
                json={"presented_items": [{"check_number": "CHKPF001", "amount": "9500.00"}]},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["amount_mismatches"] == 1
        assert resp.json()["exceptions_created"] == 1

        async with mk() as s:
            rows = (
                (
                    await s.execute(
                        select(APException).where(
                            APException.invoice_id == uuid.UUID(invoice_id),
                            APException.exception_type == "fraud_flag",
                        )
                    )
                )
                .scalars()
                .all()
            )
            # Both survive — the pre-existing info row AND the altered-cheque error.
            assert len(rows) == 2
            altered = [r for r in rows if "CHKPF001" in (r.description or "")]
            assert len(altered) == 1
            assert altered[0].severity == "error"

        # A bank redelivery of the SAME return is still deduped.
        async with realdb.client(key="a", role="ap_manager") as c:
            again = await c.post(
                f"/api/positive-pay/{file_id}/process-return",
                json={"presented_items": [{"check_number": "CHKPF001", "amount": "9500.00"}]},
            )
        assert again.json()["exceptions_created"] == 0
    finally:
        await _clear_settings(realdb, org_id)


async def test_process_return_classifies_against_persisted_snapshot_not_live_status(realdb):
    """Issue #178: a cheque issued (and appears on the file sent to the bank),
    then later VOIDED in the app, must still classify against what the bank
    was actually told — not a live re-derivation that excludes voided
    payments. Presenting it back with an altered amount must land as an
    invoice-linked `amount_mismatch`, never a standalone `not_on_file` (the
    bug: process_return re-derived the issued set live, dropped the voided
    payment out of it, and orphaned the presentment from its invoice)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_check_account(realdb, org_id)
    try:
        vendor_id = await _add_vendor(mk, org_id)
        invoice_id = await _add_invoice(
            mk, org_id, vendor_id=vendor_id, invoice_number="INV-VOID", amount="500.00"
        )
        run_id = await _add_check_run(
            mk, org_id, invoice_id=invoice_id, check_number="CHK7001", amount="500.00"
        )

        async with realdb.client(key="a", role="ap_manager") as c:
            file_id = (
                await c.post(f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={})
            ).json()["id"]

        # Void the payment AFTER the file was generated and sent to the bank —
        # the persisted issued_map snapshot must still reflect it was issued.
        async with mk() as s:
            pay = (
                await s.execute(select(Payment).where(Payment.payment_run_id == uuid.UUID(run_id)))
            ).scalar_one()
            pay.status = "voided"
            await s.commit()

        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.post(
                f"/api/positive-pay/{file_id}/process-return",
                json={"presented_items": [{"check_number": "CHK7001", "amount": "900.00"}]},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Correctly recognized as an ALTERED issued cheque, not a never-issued one.
        assert body["amount_mismatches"] == 1
        assert body["not_on_file"] == 0
        assert body["exceptions_created"] == 1

        async with mk() as s:
            # The fraud_flag must be linked to the ORIGINAL invoice, not standalone.
            linked = (
                (
                    await s.execute(
                        select(APException).where(
                            APException.invoice_id == uuid.UUID(invoice_id),
                            APException.exception_type == "fraud_flag",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(linked) == 1

            standalone_count = (
                await s.execute(
                    select(func.count()).where(
                        APException.invoice_id.is_(None),
                        APException.exception_type == "fraud_flag",
                        APException.organization_id == org_id,
                    )
                )
            ).scalar()
            assert standalone_count == 0
    finally:
        await _clear_settings(realdb, org_id)


async def test_agent_resolve_422_on_invoiceless_exception(realdb):
    """A standalone (invoice-less) fraud exception can't be agent-resolved."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        exc = APException(
            invoice_id=None,
            exception_type="fraud_flag",
            severity="error",
            status="open",
            description="Positive Pay return: check CHK-ORPHAN not on issued file",
            organization_id=org_id,
        )
        s.add(exc)
        await s.commit()
        exc_id = str(exc.id)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/exceptions/{exc_id}/agent-resolve")
    assert resp.status_code == 422
    assert "no associated invoice" in resp.json()["detail"]


async def test_process_return_422_on_ach_file(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_vendor(
        mk,
        org_id,
        name="ACH Only",
        bank_details={"routing_number": "021000021", "account_number": "555"},
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        file_id = (await c.post("/api/positive-pay/ach-authorization", json={})).json()["id"]
        resp = await c.post(
            f"/api/positive-pay/{file_id}/process-return",
            json={"presented_items": []},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_file(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_vendor(
        mk,
        org_id,
        name="ACH Del",
        bank_details={"routing_number": "021000021", "account_number": "777"},
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        created = (await c.post("/api/positive-pay/ach-authorization", json={})).json()
        file_id = created["id"]
        file_key = created["file_key"]
        # The object holds full account / routing numbers — confirm it exists,
        # then that delete purges it from MinIO (not just the DB row), so no
        # PII-bearing bytes linger at rest.
        assert (await storage.get_file(file_key))[0]
        resp = await c.delete(f"/api/positive-pay/{file_id}")
        assert resp.status_code == 204
        gone = await c.get(f"/api/positive-pay/{file_id}")
        assert gone.status_code == 404
    with pytest.raises(Exception):
        await storage.get_file(file_key)


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


async def test_clerk_cannot_generate(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="INV-RB")
    run_id = await _add_check_run(mk, org_id, invoice_id=invoice_id, check_number="CHKRB01")
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={})
    assert resp.status_code == 403


async def test_cfo_can_read_list(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/positive-pay")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Unsupported bank format
# ---------------------------------------------------------------------------


async def test_check_issue_refuses_an_unregistered_bank_format(realdb):
    """A `bank_format` we have no formatter for is a 422 — never a CSV file
    filed under the requested name.

    The dispatcher used to fall back to `csv`. Positive Pay is a fraud control
    the BANK enforces off the file we hand it, so that fallback stored a CSV
    body, stamped the row + audit trail with the requested format, burned the
    `(run, bank_format)` idempotency slot on it and returned 201 — leaving the
    tenant believing the control was in force on a file its bank cannot parse.
    Nothing must persist: no `PositivePayFile` row, and the slot stays free.
    """
    from app.models.positive_pay import PositivePayFile

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    vendor_id = await _add_vendor(mk, org_id, name="BadFormat Co")
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="INV-BF1")
    run_id = await _add_check_run(mk, org_id, invoice_id=invoice_id, check_number="CHKBF001")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/positive-pay/payment-runs/{run_id}/check-issue",
            json={"bank_format": "wells_fargo_xyz"},
        )
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        # Names the bad value and the real alternatives (caller input, never a
        # credential).
        assert "wells_fargo_xyz" in detail
        assert "csv" in detail

        # The valid format still works on the same run — the refusal did not
        # consume the idempotency slot.
        ok = await c.post(
            f"/api/positive-pay/payment-runs/{run_id}/check-issue", json={"bank_format": "csv"}
        )
        assert ok.status_code == 201, ok.text

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(PositivePayFile).where(
                        PositivePayFile.payment_run_id == uuid.UUID(run_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [r.bank_format for r in rows] == ["csv"]


async def test_ach_authorization_refuses_an_unregistered_bank_format(realdb):
    """Same refusal on the org-wide ACH file — and it lands BEFORE the vendor
    bank details are projected, so no routing/account number is rendered for a
    file that could never have been formatted correctly."""
    from app.models.positive_pay import PositivePayFile

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    await _add_vendor(
        mk,
        org_id,
        name="ACH BadFormat",
        bank_details={"routing_number": "021000021", "account_number": "555000111"},
    )
    async with mk() as s:
        before = (await s.execute(select(func.count()).select_from(PositivePayFile))).scalar() or 0

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/positive-pay/ach-authorization", json={"bank_format": "chase_fixed"}
        )
    assert resp.status_code == 422, resp.text
    assert "chase_fixed" in resp.json()["detail"]

    async with mk() as s:
        after = (await s.execute(select(func.count()).select_from(PositivePayFile))).scalar() or 0
    assert after == before
