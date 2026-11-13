"""Real-DB coverage for the bank-reconciliation router
(``app/api/bank_reconciliation.py``).

Before this router existed, ``app/services/bank_reconciliation.py`` — the CSV
importer + payment matcher — had a migration, a model, a docs page, and its
own service-level test suite (``test_bank_reconciliation.py``), but **no HTTP
route ever mounted it** (see ``git log -- app/api/bank_reconciliation.py``:
no such file existed before this change). The whole feature was unreachable
by any user or the frontend. This suite proves the wiring: upload → persisted
statement + matched transactions, list/detail, manual match review, delete,
and RBAC — end-to-end against the live test tenants. The parse/match math
itself is owned by the (separately tested) pure service.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.bank_reconciliation import BankStatement, BankTransaction
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.services.csv_import import MAX_CSV_IMPORT_SIZE

_TODAY = date.today()


async def _default_entity_id(s):
    return (
        await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


async def _add_payment(
    mk,
    org_id,
    *,
    amount="500.00",
    provider_payment_id=None,
    reference=None,
    submitted_at=None,
    vendor_name="Acme Supplies",
    status="completed",
    currency="USD",
) -> str:
    async with mk() as s:
        entity_id = await _default_entity_id(s)
        vendor = Vendor(organization_id=org_id, name=vendor_name, entity_id=entity_id)
        s.add(vendor)
        await s.flush()
        inv = Invoice(
            organization_id=org_id,
            entity_id=entity_id,
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name=vendor_name,
            vendor_id=vendor.id,
            amount=Decimal(amount),
            currency=currency,
            invoice_date=_TODAY,
            due_date=_TODAY + timedelta(days=30),
            status=InvoiceStatus.paid,
        )
        s.add(inv)
        await s.flush()
        pay = Payment(
            invoice_id=inv.id,
            entity_id=entity_id,
            amount=Decimal(amount),
            method="ach",
            status=status,
            provider_payment_id=provider_payment_id,
            reference=reference,
            submitted_at=submitted_at or datetime.now(UTC),
        )
        s.add(pay)
        await s.commit()
        await s.refresh(pay)
        return str(pay.id)


def _csv(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode()


# ---------------------------------------------------------------------------
# Upload — happy path + matching
# ---------------------------------------------------------------------------


async def test_upload_matches_by_reference_and_persists_audit(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(mk, org_id, amount="500.00", provider_payment_id="TRACE-123")

    csv_body = _csv(
        "Date,Amount,Reference,Description",
        f"{_TODAY.isoformat()},-500.00,TRACE-123,Vendor ACH",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****1234",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_format"] == "csv"
    assert body["file_key"] is None  # raw-file storage deferred, like vendor-statement-recon
    assert body["transaction_count"] == 1
    assert body["matched_count"] == 1
    tx = body["transactions"][0]
    assert tx["matched_payment_id"] == payment_id
    assert tx["match_method"] == "provider_id"
    assert tx["match_confidence"] == 100.0
    assert tx["direction"] == "debit"
    assert tx["amount"] == 500.0

    # Persisted, not just shaped in the response.
    async with mk() as s:
        stmt = (
            await s.execute(select(BankStatement).where(BankStatement.id == uuid.UUID(body["id"])))
        ).scalar_one()
        assert stmt.matched_count == 1
        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "bank_reconciliation.imported",
                    AuditLog.entity_id == stmt.id,
                )
            )
        ).scalar_one()
        assert audit.entity_type == "bank_statement"


async def test_upload_credits_are_skipped_not_matched(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_payment(mk, org_id, amount="500.00", provider_payment_id="TRACE-999")

    csv_body = _csv(
        "Date,Amount,Reference,Description",
        f"{_TODAY.isoformat()},250.00,TRACE-OTHER,Incoming refund",  # positive = credit
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****1234",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["matched_count"] == 0
    assert body["transactions"][0]["direction"] == "credit"
    assert body["transactions"][0]["matched_payment_id"] is None


async def test_upload_malformed_csv_returns_422(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****1234",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("bad.csv", b"not,a,valid,header\r\n", "text/csv")},
        )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Import idempotency + upload size cap
# ---------------------------------------------------------------------------


async def test_uploading_the_same_file_twice_returns_the_first_statement(realdb):
    """A double-click / retried upload used to create a SECOND statement whose
    `matched_count` was 0 — every payment on it had already been claimed by the
    first import — so the duplicate read as "this statement didn't reconcile"
    rather than "you imported this twice". Deduped on
    (org, account_identifier, sha256(body)): the retry gets the original back
    with 200, and only one row exists."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(mk, org_id, amount="512.00", provider_payment_id="TRACE-DUP1")

    account = f"Dedupe ****{uuid.uuid4().hex[:4]}"
    csv_body = _csv(
        "Date,Amount,Reference,Description",
        f"{_TODAY.isoformat()},-512.00,TRACE-DUP1,Vendor ACH",
    )
    payload = {
        "account_identifier": account,
        "period_start": (_TODAY - timedelta(days=30)).isoformat(),
        "period_end": _TODAY.isoformat(),
        "currency": "USD",
    }
    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.post(
            "/api/bank-reconciliation/upload",
            data=payload,
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        second = await c.post(
            "/api/bank-reconciliation/upload",
            data=payload,
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    # The idempotent reply is the REAL result, not a hollow zero-match echo.
    assert second.json()["matched_count"] == 1
    assert second.json()["transactions"][0]["matched_payment_id"] == payment_id

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(BankStatement).where(BankStatement.account_identifier == account)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].content_hash is not None


async def test_a_different_file_on_the_same_account_still_imports(realdb):
    """Dedupe is on the file's CONTENT, not the account: a genuine second
    statement for the same account (next period) must still import."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_payment(mk, org_id, amount="61.00")

    account = f"Dedupe ****{uuid.uuid4().hex[:4]}"
    payload = {
        "account_identifier": account,
        "period_start": (_TODAY - timedelta(days=30)).isoformat(),
        "period_end": _TODAY.isoformat(),
        "currency": "USD",
    }
    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.post(
            "/api/bank-reconciliation/upload",
            data=payload,
            files={
                "file": (
                    "jan.csv",
                    _csv("Date,Amount,Description", f"{_TODAY.isoformat()},-61.00,January"),
                    "text/csv",
                )
            },
        )
        second = await c.post(
            "/api/bank-reconciliation/upload",
            data=payload,
            files={
                "file": (
                    "feb.csv",
                    _csv("Date,Amount,Description", f"{_TODAY.isoformat()},-62.00,February"),
                    "text/csv",
                )
            },
        )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first.json()["id"]


async def test_two_concurrent_uploads_cannot_both_claim_one_payment(realdb):
    """The handler's retry loop, driven for real.

    Two DIFFERENT statements uploaded at once, each carrying a line that
    references the SAME payment. Each request's matcher builds its `claimed`
    set before the other commits, so both believe the payment is free — the one
    guard the application layer cannot win. `uq_bank_transactions_matched_payment`
    settles it, and the loser rolls back and re-runs its whole import rather
    than 500ing and losing every other line on its file: on the second pass its
    matcher sees the winner's claim and leaves that line unmatched.

    Asserted on the durable outcome: both files imported, exactly one claimant.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(mk, org_id, amount="909.00", provider_payment_id="TRACE-RACE")

    async def _upload(tag: str):
        body = _csv(
            "Date,Amount,Reference,Description",
            f"{_TODAY.isoformat()},-909.00,TRACE-RACE,Wire {tag}",
            # A second, unrelated line — the point of retrying rather than
            # 500ing is that the loser doesn't lose THIS one too.
            f"{_TODAY.isoformat()},-{tag}.00,,Other line {tag}",
        )
        async with realdb.client(key="a", role="ap_manager") as client:
            resp = await client.post(
                "/api/bank-reconciliation/upload",
                data={
                    "account_identifier": f"Race ****{tag}",
                    "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                    "period_end": _TODAY.isoformat(),
                    "currency": "USD",
                },
                files={"file": (f"statement-{tag}.csv", body, "text/csv")},
            )
            return resp.status_code, resp.json()

    results = await asyncio.gather(_upload("11"), _upload("22"))
    codes = sorted(code for code, _ in results)
    assert codes == [201, 201], f"both imports should land, got {codes}"

    # Both files kept all their lines — the loser re-ran, it didn't lose the file.
    for _, body in results:
        assert body["transaction_count"] == 2
        assert len(body["transactions"]) == 2

    async with mk() as s:
        claimants = (
            (
                await s.execute(
                    select(BankTransaction).where(
                        BankTransaction.matched_payment_id == uuid.UUID(payment_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(claimants) == 1, "one payment was claimed by two bank transactions"
        # And exactly one audit row per statement — a retried attempt must not
        # double-audit the import.
        audits = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "bank_reconciliation.imported")
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 2, f"expected one audit row per statement, got {len(audits)}"


async def test_upload_over_the_size_cap_is_rejected(realdb):
    """`await file.read()` was unbounded, so any authenticated manager could
    buffer an arbitrarily large body into process memory before a single check
    ran. The read is now capped and aborts mid-stream."""
    oversized = b"Date,Amount,Description\r\n" + (b"x" * (MAX_CSV_IMPORT_SIZE + 1024))
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9020",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("huge.csv", oversized, "text/csv")},
        )
    assert resp.status_code == 413, resp.text


# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------


async def test_list_and_get_detail(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_payment(mk, org_id, amount="100.00")

    csv_body = _csv("Date,Amount,Description", f"{_TODAY.isoformat()},-100.00,Some payment")
    async with realdb.client(key="a", role="ap_manager") as c:
        create_resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****5555",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        stmt_id = create_resp.json()["id"]

        list_resp = await c.get(
            "/api/bank-reconciliation", params={"account_identifier": "Operating ****5555"}
        )
        assert list_resp.status_code == 200
        list_body = list_resp.json()
        assert any(item["id"] == stmt_id for item in list_body["items"])
        # List omits the transaction detail (avoids an N+1 payload on the index).
        assert list_body["items"][0]["transactions"] is None

        detail_resp = await c.get(f"/api/bank-reconciliation/{stmt_id}")
        assert detail_resp.status_code == 200
        detail_body = detail_resp.json()
        assert len(detail_body["transactions"]) == 1


async def test_get_unknown_statement_is_404(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get(f"/api/bank-reconciliation/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Manual match review
# ---------------------------------------------------------------------------


async def test_resolve_transaction_sets_and_clears_manual_match(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    # Same amount as the bank line — this is the ORDINARY manual match, where
    # the human is supplying an identity the matcher couldn't infer, not
    # overriding an amount discrepancy (see
    # test_manual_resolve_cannot_stamp_a_wrong_amount_as_reconciled for that).
    # `submitted_at` is put well outside the matcher's ±5-day window so the
    # upload legitimately lands unmatched and there is something to resolve.
    payment_id = await _add_payment(
        mk,
        org_id,
        amount="321.00",
        submitted_at=datetime.now(UTC) - timedelta(days=60),
    )

    csv_body = _csv("Date,Amount,Description", f"{_TODAY.isoformat()},-321.00,Unmatched wire")
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9999",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        stmt_id = up.json()["id"]
        tx_id = up.json()["transactions"][0]["id"]
        assert up.json()["matched_count"] == 0

        resolve = await c.post(
            f"/api/bank-reconciliation/{stmt_id}/transactions/{tx_id}/resolve",
            json={"matched_payment_id": payment_id},
        )
        assert resolve.status_code == 200, resolve.text
        body = resolve.json()
        assert body["matched_count"] == 1
        tx = body["transactions"][0]
        assert tx["matched_payment_id"] == payment_id
        assert tx["match_method"] == "manual"
        assert tx["match_confidence"] == 100.0
        # Amounts agree → genuinely reconciled, zero variance.
        assert tx["is_reconciled"] is True
        assert Decimal(str(tx["variance_amount"])) == Decimal("0.00")
        assert Decimal(str(tx["matched_payment_amount"])) == Decimal("321.00")

        # Clear it back to unmatched.
        cleared = await c.post(
            f"/api/bank-reconciliation/{stmt_id}/transactions/{tx_id}/resolve",
            json={"matched_payment_id": None},
        )
        assert cleared.status_code == 200
        cleared_body = cleared.json()
        assert cleared_body["matched_count"] == 0
        assert cleared_body["transactions"][0]["matched_payment_id"] is None
        assert cleared_body["transactions"][0]["match_method"] is None

    async with mk() as s:
        audits = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "bank_reconciliation.transaction_resolved",
                        AuditLog.entity_id == uuid.UUID(stmt_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 2  # set + clear


async def test_resolve_refuses_a_credit_transaction(realdb):
    """A payment is money we SENT, so only a bank debit can clear one. A credit
    of equal magnitude used to classify cleanly, count toward `matched_count`,
    and drop the payment out of all three `/outstanding` buckets — buckets 2
    and 3 both require `direction == "debit"`, so an uncleared payment silently
    left the month-end worksheet."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(
        mk,
        org_id,
        amount="777.00",
        submitted_at=datetime.now(UTC) - timedelta(days=60),
    )

    # A POSITIVE amount is a credit (money in) on the importer's convention.
    csv_body = _csv("Date,Amount,Description", f"{_TODAY.isoformat()},777.00,Vendor refund")
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****7777",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        assert up.status_code == 201, up.text
        stmt_id = up.json()["id"]
        tx = up.json()["transactions"][0]
        assert tx["direction"] == "credit"

        resp = await c.post(
            f"/api/bank-reconciliation/{stmt_id}/transactions/{tx['id']}/resolve",
            json={"matched_payment_id": payment_id},
        )
        assert resp.status_code == 409, resp.text
        assert "credit" in resp.json()["detail"]

        # And the payment is still on the worksheet.
        outstanding = await c.get("/api/bank-reconciliation/outstanding")
        assert outstanding.status_code == 200
        assert any(p["payment_id"] == payment_id for p in outstanding.json()["uncleared_payments"])


async def test_resolve_malformed_payment_id_is_422_not_500(realdb):
    """`matched_payment_id` is schema-typed `uuid.UUID`; the router used to call
    `uuid.UUID(...)` on a plain `str` with no handler, so a malformed id was a
    500."""
    csv_body = _csv("Date,Amount,Description", f"{_TODAY.isoformat()},-12.00,X")
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****0042",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        stmt_id = up.json()["id"]
        tx_id = up.json()["transactions"][0]["id"]

        resp = await c.post(
            f"/api/bank-reconciliation/{stmt_id}/transactions/{tx_id}/resolve",
            json={"matched_payment_id": "not-a-uuid"},
        )
    assert resp.status_code == 422, resp.text


async def test_resolve_unknown_payment_is_404(realdb):
    csv_body = _csv("Date,Amount,Description", f"{_TODAY.isoformat()},-50.00,X")
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****0001",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        stmt_id = up.json()["id"]
        tx_id = up.json()["transactions"][0]["id"]

        resp = await c.post(
            f"/api/bank-reconciliation/{stmt_id}/transactions/{tx_id}/resolve",
            json={"matched_payment_id": str(uuid.uuid4())},
        )
    assert resp.status_code == 404


async def test_resolve_refuses_to_double_claim_a_payment(realdb):
    """A Payment can be matched to at most one BankTransaction — the same
    invariant the automatic matcher enforces. Manually resolving a second
    transaction onto an already-matched payment must be refused (409), not
    silently steal/duplicate the match."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(mk, org_id, amount="42.00")

    csv_body = _csv(
        "Date,Amount,Description",
        f"{_TODAY.isoformat()},-11.00,First",
        f"{_TODAY.isoformat()},-12.00,Second",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****7777",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        stmt_id = up.json()["id"]
        tx1_id, tx2_id = (t["id"] for t in up.json()["transactions"])

        first = await c.post(
            f"/api/bank-reconciliation/{stmt_id}/transactions/{tx1_id}/resolve",
            json={"matched_payment_id": payment_id},
        )
        assert first.status_code == 200, first.text

        second = await c.post(
            f"/api/bank-reconciliation/{stmt_id}/transactions/{tx2_id}/resolve",
            json={"matched_payment_id": payment_id},
        )
        assert second.status_code == 409, second.text

        # The first transaction's match is untouched by the refused attempt.
        detail = await c.get(f"/api/bank-reconciliation/{stmt_id}")
        by_id = {t["id"]: t for t in detail.json()["transactions"]}
        assert by_id[tx1_id]["matched_payment_id"] == payment_id
        assert by_id[tx2_id]["matched_payment_id"] is None


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_cascades_transactions_and_audits(realdb):
    mk = realdb.sessionmaker("a")
    csv_body = _csv(
        "Date,Amount,Description",
        f"{_TODAY.isoformat()},-10.00,A",
        f"{_TODAY.isoformat()},-20.00,B",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****4242",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        stmt_id = up.json()["id"]

        del_resp = await c.delete(f"/api/bank-reconciliation/{stmt_id}")
        assert del_resp.status_code == 204

        get_resp = await c.get(f"/api/bank-reconciliation/{stmt_id}")
        assert get_resp.status_code == 404

    async with mk() as s:
        remaining = (
            (
                await s.execute(
                    select(BankTransaction).where(
                        BankTransaction.statement_id == uuid.UUID(stmt_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []
        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "bank_reconciliation.deleted",
                    AuditLog.entity_id == uuid.UUID(stmt_id),
                )
            )
        ).scalar_one()
        assert audit.entity_type == "bank_statement"


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


async def test_ap_clerk_can_read_but_not_upload_or_delete(realdb):
    csv_body = _csv("Date,Amount,Description", f"{_TODAY.isoformat()},-10.00,A")
    async with realdb.client(key="a", role="ap_clerk") as c:
        list_resp = await c.get("/api/bank-reconciliation")
        assert list_resp.status_code == 200

        upload_resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****4242",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        assert upload_resp.status_code == 403

        delete_resp = await c.delete(f"/api/bank-reconciliation/{uuid.uuid4()}")
        assert delete_resp.status_code == 403


# ---------------------------------------------------------------------------
# Amount variance — a matched reference is not a reconciled payment
# ---------------------------------------------------------------------------


async def test_upload_flags_reference_hit_with_wrong_amount_as_amount_mismatch(realdb):
    """End-to-end proof of the defect the matcher fix closes: the bank debited
    a different amount than the payment authorises, on a line carrying that
    payment's own trace number. It must come back LINKED (traceable, and no
    other line can claim the payment) but NOT reconciled — `matched_count`
    stays 0, the row carries the signed variance, and the statement reports
    the mismatch so it is visible without opening every transaction."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(mk, org_id, amount="5000.00", provider_payment_id="TRACE-AM1")

    csv_body = _csv(
        "Date,Amount,Reference,Description",
        f"{_TODAY.isoformat()},-50000.00,TRACE-AM1,Wire out",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9001",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["matched_count"] == 0  # linked, but NOT cleared
    assert body["amount_mismatch_count"] == 1

    tx = body["transactions"][0]
    assert tx["matched_payment_id"] == payment_id
    assert tx["match_method"] == "amount_mismatch"
    assert tx["is_reconciled"] is False
    assert Decimal(str(tx["matched_payment_amount"])) == Decimal("5000.00")
    # Positive = the bank took MORE than we authorised.
    assert Decimal(str(tx["variance_amount"])) == Decimal("45000.00")


async def test_statement_list_surfaces_amount_mismatch_count(realdb):
    """A discrepancy you must open every statement to notice is a discrepancy
    nobody notices — the index carries the count too."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_payment(mk, org_id, amount="700.00", provider_payment_id="TRACE-AM2")

    account = f"Listing ****{uuid.uuid4().hex[:4]}"
    csv_body = _csv(
        "Date,Amount,Reference,Description",
        f"{_TODAY.isoformat()},-950.00,TRACE-AM2,Wire out",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": account,
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        assert up.status_code == 201, up.text
        listing = await c.get(f"/api/bank-reconciliation?account_identifier={account}")

    assert listing.status_code == 200
    item = listing.json()["items"][0]
    assert item["amount_mismatch_count"] == 1
    assert item["matched_count"] == 0


async def test_manual_resolve_cannot_stamp_a_wrong_amount_as_reconciled(realdb):
    """A human pointing a bank line at a payment is telling us WHICH payment it
    is, not that the amounts agree. The classification stays derived from the
    amounts, so a clerk cannot click past the altered-amount signal — and the
    audit row records the variance they accepted."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(mk, org_id, amount="100.00")

    csv_body = _csv(
        "Date,Amount,Description",
        f"{_TODAY.isoformat()},-99999.00,Mystery debit",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9003",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        assert up.status_code == 201, up.text
        stmt_id = up.json()["id"]
        tx_id = up.json()["transactions"][0]["id"]

        resolved = await c.post(
            f"/api/bank-reconciliation/{stmt_id}/transactions/{tx_id}/resolve",
            json={"matched_payment_id": payment_id},
        )

    assert resolved.status_code == 200, resolved.text
    tx = resolved.json()["transactions"][0]
    assert tx["matched_payment_id"] == payment_id
    assert tx["match_method"] == "amount_mismatch"
    assert tx["is_reconciled"] is False
    assert resolved.json()["matched_count"] == 0
    assert Decimal(str(tx["variance_amount"])) == Decimal("99899.00")

    async with mk() as s:
        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "bank_reconciliation.transaction_resolved",
                    AuditLog.entity_id == uuid.UUID(stmt_id),
                )
            )
        ).scalar_one()
        assert audit.details["match_method"] == "amount_mismatch"
        # Exact string, not a float — this row is the durable record of the gap.
        assert audit.details["variance_amount"] == "99899.00"


async def test_upload_flags_a_debit_against_a_failed_payment_as_status_conflict(realdb):
    """End-to-end proof of the second half of the matcher fix. The bank moved
    money on a line carrying the trace number of a payment our books call
    `failed` — our records and the bank's disagree about whether it went out at
    all. That used to come back `provider_id` / confidence 100 / reconciled,
    converting the discrepancy into a sign-off. It must stay linked but
    classified `status_conflict`, out of `matched_count`, and visible on the
    statement's `discrepancy_count` and in `/outstanding`."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(
        mk, org_id, amount="640.00", provider_payment_id="TRACE-SC1", status="failed"
    )

    csv_body = _csv(
        "Date,Amount,Reference,Description",
        f"{_TODAY.isoformat()},-640.00,TRACE-SC1,Wire out",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9010",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        assert resp.status_code == 201, resp.text
        outstanding = await c.get("/api/bank-reconciliation/outstanding")

    body = resp.json()
    assert body["matched_count"] == 0
    assert body["amount_mismatch_count"] == 0  # the amounts DO agree
    assert body["discrepancy_count"] == 1

    tx = body["transactions"][0]
    assert tx["matched_payment_id"] == payment_id
    assert tx["match_method"] == "status_conflict"
    assert tx["is_reconciled"] is False
    assert tx["matched_payment_status"] == "failed"

    # Linked lines drop out of `unmatched_debits` and a `failed` payment was
    # never in `uncleared_payments` — the discrepancy bucket is the ONLY place
    # this can surface, so it has to be there.
    row = next(d for d in outstanding.json()["discrepancies"] if d["payment_id"] == payment_id)
    assert row["classification"] == "status_conflict"
    assert row["payment_status"] == "failed"
    assert row["variance_amount"] is None  # the amounts agree; nothing to report


async def test_upload_flags_a_debit_in_another_currency_as_currency_mismatch(realdb):
    """`BankTransaction.currency` was never compared, so a EUR 1,000 debit
    reconciled a USD 1,000 payment — two different sums of money signed off as
    one. Linked (the reference identifies it) but `currency_mismatch`, and NO
    variance is reported: subtracting across currencies isn't money."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(
        mk, org_id, amount="1000.00", provider_payment_id="TRACE-CC1", currency="USD"
    )

    csv_body = _csv(
        "Date,Amount,Reference,Description",
        f"{_TODAY.isoformat()},-1000.00,TRACE-CC1,SEPA out",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9011",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "EUR",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["matched_count"] == 0
    assert body["discrepancy_count"] == 1

    tx = body["transactions"][0]
    assert tx["matched_payment_id"] == payment_id
    assert tx["match_method"] == "currency_mismatch"
    assert tx["is_reconciled"] is False
    assert tx["currency"] == "EUR"
    assert tx["matched_payment_currency"] == "USD"
    assert tx["variance_amount"] is None


@pytest.mark.parametrize(
    ("currency", "code", "params"),
    [
        (None, "statement_currency_required", {}),
        ("", "statement_currency_required", {}),
        ("EURO", "statement_currency_invalid", {"value": "EURO"}),
        ("US$", "statement_currency_invalid", {"value": "US$"}),
        # Letters, but not ASCII: `isalpha()` admitted it (decisions §381).
        ("ÄÖÜ", "statement_currency_invalid", {"value": "ÄÖÜ"}),
    ],
)
async def test_upload_refuses_a_blank_or_malformed_statement_currency(
    realdb, currency, code, params
):
    """The statement's currency is never defaulted to USD (decisions §377): an
    omitted or blank field and a malformed code are each a coded 422, and no
    statement is written."""
    data = {
        "account_identifier": "Operating ****7001",
        "period_start": (_TODAY - timedelta(days=30)).isoformat(),
        "period_end": _TODAY.isoformat(),
    }
    if currency is not None:
        data["currency"] = currency
    csv_body = _csv("Date,Amount,Description", f"{_TODAY.isoformat()},-5.00,x")
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/bank-reconciliation/upload",
            data=data,
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert (detail["code"], detail["params"]) == (code, params)
    async with realdb.sessionmaker("a")() as s:
        written = await s.execute(
            select(BankStatement).where(BankStatement.account_identifier == "Operating ****7001")
        )
        assert written.scalars().all() == []


async def test_upload_normalizes_a_lowercase_statement_currency(realdb):
    csv_body = _csv("Date,Amount,Description", f"{_TODAY.isoformat()},-5.00,lower")
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****7002",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": " gbp ",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["currency"] == "GBP"


async def test_manual_resolve_cannot_stamp_a_failed_payment_as_reconciled(realdb):
    """The manual path runs the SAME classifier as the matcher, so a clerk
    cannot resolve a bank line onto a payment our books say never went out and
    have it counted as cleared."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(mk, org_id, amount="333.00", status="voided")

    csv_body = _csv(
        "Date,Amount,Description",
        f"{_TODAY.isoformat()},-333.00,Mystery debit",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9012",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        assert up.status_code == 201, up.text
        # The auto-matcher refused it outright (a heuristic never links a
        # non-dispatched payment), so the line arrives unmatched.
        assert up.json()["transactions"][0]["matched_payment_id"] is None
        stmt_id = up.json()["id"]
        tx_id = up.json()["transactions"][0]["id"]

        resolved = await c.post(
            f"/api/bank-reconciliation/{stmt_id}/transactions/{tx_id}/resolve",
            json={"matched_payment_id": payment_id},
        )

    assert resolved.status_code == 200, resolved.text
    tx = resolved.json()["transactions"][0]
    assert tx["matched_payment_id"] == payment_id
    assert tx["match_method"] == "status_conflict"
    assert tx["is_reconciled"] is False
    assert resolved.json()["matched_count"] == 0


async def test_duplicate_payment_reference_does_not_500_the_import(realdb):
    """`Payment.reference` carries no unique constraint and the virtual-card
    path stamps a derived, deliberately non-unique value. The reference lookup
    used `scalar_one_or_none()`, so a duplicate raised `MultipleResultsFound`
    and 500'd the whole import — every other line on the file lost with it.

    The ambiguous reference must now simply not match — and never resolve to
    one of the two arbitrarily, which would credit the wrong invoice — while
    the rest of the statement still imports and still reconciles normally.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    dup_ref = f"CARD-LITHIC-{uuid.uuid4().hex[:4]}"
    # Identical amounts, so the amount+date fallback is ambiguous too and
    # nothing can legitimately claim the line.
    await _add_payment(mk, org_id, amount="1111.11", reference=dup_ref)
    await _add_payment(mk, org_id, amount="1111.11", reference=dup_ref)
    good_payment_id = await _add_payment(mk, org_id, amount="4242.00")

    csv_body = _csv(
        "Date,Amount,Reference,Description",
        f"{_TODAY.isoformat()},-1111.11,{dup_ref},Ambiguous",
        f"{_TODAY.isoformat()},-4242.00,,Clean line",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9005",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    by_ref = {t["reference"]: t for t in body["transactions"]}
    assert by_ref[dup_ref]["matched_payment_id"] is None
    # The clean line still reconciled — one bad reference doesn't poison the file.
    assert by_ref[None]["matched_payment_id"] == good_payment_id


# ---------------------------------------------------------------------------
# Outstanding items — the org-wide close view
# ---------------------------------------------------------------------------


async def test_outstanding_reports_uncleared_unmatched_and_mismatched(realdb):
    """`GET /outstanding` is the only surface that answers "across everything
    we imported, what has still not cleared" — the question month-end asks.
    All three buckets in one pass, each counted and totalled exactly.

    A payment linked to an `amount_mismatch` line is accounted for in the
    mismatch bucket, so it must NOT also appear as uncleared.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    uncleared_id = await _add_payment(mk, org_id, amount="1500.00")
    mismatched_id = await _add_payment(mk, org_id, amount="200.00", provider_payment_id="TRACE-OS1")

    csv_body = _csv(
        "Date,Amount,Reference,Description",
        # Names the mismatched payment, but at the wrong amount.
        f"{_TODAY.isoformat()},-275.50,TRACE-OS1,Wire out",
        # No payment behind this one at all.
        f"{_TODAY.isoformat()},-88.00,,Unknown debit",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9006",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        assert up.status_code == 201, up.text
        resp = await c.get("/api/bank-reconciliation/outstanding")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    uncleared_ids = {p["payment_id"] for p in body["uncleared_payments"]}
    assert uncleared_id in uncleared_ids
    # Accounted for in the mismatch bucket — must not be double-reported.
    assert mismatched_id not in uncleared_ids

    mismatch = next(m for m in body["discrepancies"] if m["payment_id"] == mismatched_id)
    assert mismatch["classification"] == "amount_mismatch"
    assert Decimal(str(mismatch["bank_amount"])) == Decimal("275.50")
    assert Decimal(str(mismatch["payment_amount"])) == Decimal("200.00")
    assert Decimal(str(mismatch["variance_amount"])) == Decimal("75.50")

    unmatched_refs = [d["amount"] for d in body["unmatched_debits"]]
    assert Decimal("88.00") in [Decimal(str(a)) for a in unmatched_refs]
    assert body["unmatched_debit_count"] >= 1


async def test_outstanding_older_than_days_excludes_recent_payments(realdb):
    """The age filter is what makes this a close tool rather than a live feed:
    a payment submitted this morning is not yet "outstanding"."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    fresh_id = await _add_payment(mk, org_id, amount="4321.00", submitted_at=datetime.now(UTC))
    stale_id = await _add_payment(
        mk,
        org_id,
        amount="8765.00",
        submitted_at=datetime.now(UTC) - timedelta(days=45),
    )

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/bank-reconciliation/outstanding?older_than_days=30")

    assert resp.status_code == 200, resp.text
    ids = {p["payment_id"] for p in resp.json()["uncleared_payments"]}
    assert stale_id in ids
    assert fresh_id not in ids
    assert resp.json()["older_than_days"] == 30


async def test_outstanding_older_than_days_boundary_is_inclusive(realdb):
    """The boundary itself, not just a comfortable margin either side: a
    payment sent exactly N days ago IS outstanding at `older_than_days=N`, and
    one sent a day later is not. The filter runs in SQL against a UTC-
    normalised date, so an off-by-one here would silently drop or add a day's
    worth of items from a close report."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    now = datetime.now(UTC)
    on_boundary = await _add_payment(
        mk, org_id, amount="7001.00", submitted_at=now - timedelta(days=10)
    )
    inside_boundary = await _add_payment(
        mk, org_id, amount="7002.00", submitted_at=now - timedelta(days=9)
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/bank-reconciliation/outstanding?older_than_days=10")

    ids = {p["payment_id"] for p in resp.json()["uncleared_payments"]}
    assert on_boundary in ids, "a payment sent exactly N days ago must be outstanding"
    assert inside_boundary not in ids

    row = next(p for p in resp.json()["uncleared_payments"] if p["payment_id"] == on_boundary)
    assert row["days_outstanding"] == 10


async def test_outstanding_excludes_a_cleanly_reconciled_payment(realdb):
    """A payment a statement genuinely cleared must drop out of the outstanding
    list entirely — otherwise the report never shrinks and stops being read."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(mk, org_id, amount="6543.00", provider_payment_id="TRACE-OS2")

    csv_body = _csv(
        "Date,Amount,Reference,Description",
        f"{_TODAY.isoformat()},-6543.00,TRACE-OS2,Vendor ACH",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9007",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        assert up.json()["matched_count"] == 1, up.text
        resp = await c.get("/api/bank-reconciliation/outstanding")

    ids = {p["payment_id"] for p in resp.json()["uncleared_payments"]}
    assert payment_id not in ids


async def test_outstanding_limit_truncates_rows_but_never_the_totals(realdb):
    """`?limit` exists to bound the payload, not the arithmetic. Counts and
    totals come from SQL aggregates over the whole set, so a capped page can
    never understate what is outstanding — a close report that quietly reports
    less money than is really open is worse than no report."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    for amount in ("101.00", "102.00", "103.00"):
        await _add_payment(mk, org_id, amount=amount)

    async with realdb.client(key="a", role="ap_manager") as c:
        capped = await c.get("/api/bank-reconciliation/outstanding?limit=1")
        full = await c.get("/api/bank-reconciliation/outstanding")

    assert capped.status_code == 200, capped.text
    capped_body = capped.json()
    full_body = full.json()
    assert len(capped_body["uncleared_payments"]) == 1
    assert len(full_body["uncleared_payments"]) > 1
    # Same count, same money — only the row list differs.
    assert capped_body["uncleared_count"] == full_body["uncleared_count"]
    assert capped_body["uncleared_totals"] == full_body["uncleared_totals"]
    # And the totals are exact decimal strings per currency, not one blended
    # float: `Payment.amount` is invoice-currency, so a cross-currency sum would
    # be denominated in nothing real.
    by_currency = {t["currency"]: Decimal(t["total"]) for t in capped_body["uncleared_totals"]}
    assert sum(by_currency.values()) >= Decimal("306.00")
    for total in capped_body["uncleared_totals"]:
        assert isinstance(total["total"], str)
        assert Decimal(total["total"]).as_tuple().exponent == -2


async def test_concurrent_resolve_cannot_claim_one_payment_twice(realdb):
    """The "already matched to another transaction" guard is a read-then-write.
    Two concurrent resolves pointing DIFFERENT transactions at the SAME payment
    both read "not claimed", both pass, and both commit — the payment counts as
    cleared twice, which is exactly the double-count the guard exists to stop.

    The payment row is now locked FOR UPDATE before the check, so every
    claimant serialises on it: the second blocks until the first commits, then
    sees the claim and 409s. Asserted on the durable outcome — exactly one
    transaction ends up holding the payment.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(mk, org_id, amount="55.00")

    csv_body = _csv(
        "Date,Amount,Description",
        f"{_TODAY.isoformat()},-55.00,First line",
        f"{_TODAY.isoformat()},-55.00,Second line",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9008",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
        assert up.status_code == 201, up.text
        stmt_id = up.json()["id"]
        tx_ids = [t["id"] for t in up.json()["transactions"]]
        # Start from a clean slate so both transactions are free to claim.
        for tx_id in tx_ids:
            await c.post(
                f"/api/bank-reconciliation/{stmt_id}/transactions/{tx_id}/resolve",
                json={"matched_payment_id": None},
            )

    async def _claim(tx_id: str):
        async with realdb.client(key="a", role="ap_manager") as client:
            resp = await client.post(
                f"/api/bank-reconciliation/{stmt_id}/transactions/{tx_id}/resolve",
                json={"matched_payment_id": payment_id},
            )
            return resp.status_code

    statuses = await asyncio.gather(_claim(tx_ids[0]), _claim(tx_ids[1]))
    assert sorted(statuses) == [200, 409], f"expected one winner and one 409, got {statuses}"

    async with mk() as s:
        claimants = (
            (
                await s.execute(
                    select(BankTransaction).where(
                        BankTransaction.matched_payment_id == uuid.UUID(payment_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(claimants) == 1, "one payment was claimed by two bank transactions"


async def test_a_second_claim_on_one_payment_is_impossible_at_the_db_layer(realdb):
    """The application enforces "one payment, one bank transaction" twice — the
    matcher's `claimed` set and `/resolve`'s row-locked check — but neither
    survives two concurrent `/upload`s, which each read their `claimed` set
    before either commits. `uq_bank_transactions_matched_payment` (migration
    0081) is the backstop, and this asserts it exists and BITES: a direct write
    that bypasses every application check still cannot persist a second
    claimant."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    payment_id = await _add_payment(mk, org_id, amount="77.00", provider_payment_id="TRACE-UQ1")

    csv_body = _csv(
        "Date,Amount,Reference,Description",
        f"{_TODAY.isoformat()},-77.00,TRACE-UQ1,Claimed",
        f"{_TODAY.isoformat()},-77.00,,Second line",
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        up = await c.post(
            "/api/bank-reconciliation/upload",
            data={
                "account_identifier": "Operating ****9030",
                "period_start": (_TODAY - timedelta(days=30)).isoformat(),
                "period_end": _TODAY.isoformat(),
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
    assert up.status_code == 201, up.text
    rows = up.json()["transactions"]
    claimed = next(t for t in rows if t["matched_payment_id"] == payment_id)
    free = next(t for t in rows if t["matched_payment_id"] is None)
    assert claimed["id"] != free["id"]

    # Straight to the DB, past the router's checks entirely.
    async with mk() as s:
        tx = (
            await s.execute(
                select(BankTransaction).where(BankTransaction.id == uuid.UUID(free["id"]))
            )
        ).scalar_one()
        tx.matched_payment_id = uuid.UUID(payment_id)
        with pytest.raises(IntegrityError):
            await s.commit()
        await s.rollback()

    async with mk() as s:
        claimants = (
            (
                await s.execute(
                    select(BankTransaction).where(
                        BankTransaction.matched_payment_id == uuid.UUID(payment_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(claimants) == 1


async def test_outstanding_readable_by_ap_clerk(realdb):
    """Read-role gated like the rest of the router. (`test_rbac.py`'s coverage
    gate is what proves the route requires auth at all.)"""
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.get("/api/bank-reconciliation/outstanding")).status_code == 200


async def test_outstanding_totals_are_grouped_per_currency_never_blended(realdb):
    """`Payment.amount` is invoice-currency, so one `SUM` across a
    multi-currency tenant produces a figure denominated in nothing real — and
    the frontend then prints it under a single symbol, showing the wrong one.

    This is the same rule `amount_mismatch_net_variance` already states for
    subtraction ("a cross-currency subtraction isn't money"), applied to
    addition. The blended figure must appear nowhere in the response."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_payment(mk, org_id, amount="100.00", currency="USD")
    await _add_payment(mk, org_id, amount="250.00", currency="EUR")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/bank-reconciliation/outstanding")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    totals = {t["currency"]: t["total"] for t in body["uncleared_totals"]}
    assert totals.get("USD") == "100.00"
    assert totals.get("EUR") == "250.00"
    # The blended sum must not appear anywhere — not as a total, not as a
    # leftover field.
    assert "uncleared_total" not in body
    assert "350.00" not in json.dumps(body)

    # Every row says which currency its amount is in, so the UI cannot fall back
    # to the org's reporting currency and render the wrong symbol. (Row `amount`
    # is a `MoneyAmount`, which this codebase serialises as a JSON number — see
    # `backend/app/schemas/money.py`; the whole-set TOTALS above are the exact
    # decimal strings, which is what a user actually reads.)
    rows = {Decimal(str(r["amount"])): r["currency"] for r in body["uncleared_payments"]}
    assert rows.get(Decimal("100.00")) == "USD"
    assert rows.get(Decimal("250.00")) == "EUR"


async def test_outstanding_search_narrows_counts_and_rows_together(realdb):
    """A search term is applied in SQL, on the same WHERE the aggregates read.

    A client-side filter over the returned rows cannot see a row past `?limit`,
    so the user would be told nothing matched while the bucket header counted
    the whole set — the failure `frontend/CLAUDE.md` § Search forbids and
    `tests/test_paginated_list_search_legs.py` enforces."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _add_payment(mk, org_id, amount="410.00", vendor_name="Zephyr Metalworks")
    await _add_payment(mk, org_id, amount="420.00", vendor_name="Borealis Freight")

    async with realdb.client(key="a", role="ap_manager") as c:
        hit = await c.get("/api/bank-reconciliation/outstanding?search=zephyr")
        miss = await c.get("/api/bank-reconciliation/outstanding?search=nothingmatchesthis")
    assert hit.status_code == 200, hit.text
    hit_body = hit.json()

    names = [r["vendor_name"] for r in hit_body["uncleared_payments"]]
    assert names == ["Zephyr Metalworks"]
    # The COUNT narrows with the rows — this is the whole point.
    assert hit_body["uncleared_count"] == 1
    # …and so does the money, so the header can't overstate a filtered view.
    assert [t["total"] for t in hit_body["uncleared_totals"]] == ["410.00"]
    # Case-insensitive: the term was lower-case, the vendor is not.

    miss_body = miss.json()
    assert miss_body["uncleared_payments"] == []
    assert miss_body["uncleared_count"] == 0
    assert miss_body["uncleared_totals"] == []


async def test_net_variance_is_grouped_per_currency_too(realdb):
    """The last bucket that summed across currencies.

    An `amount_mismatch` line is same-currency by construction — a currency
    difference is classified `currency_mismatch` and excluded from the
    subtraction — but two mismatch lines in two DIFFERENT currencies still
    cannot be added. Leaving this one ungrouped made it the single figure on a
    page that disclaims cross-currency summing everywhere else."""
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/bank-reconciliation/outstanding")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The scalar is gone; what remains is a per-currency list of exact strings.
    assert "amount_mismatch_net_variance" not in body
    variances = body["amount_mismatch_net_variances"]
    assert isinstance(variances, list)
    for entry in variances:
        assert set(entry) == {"currency", "total"}
        assert isinstance(entry["total"], str)
        # Signed: a negative net is legitimate (we authorised more than the
        # bank took), so the assertion is on exactness, not on sign.
        assert Decimal(entry["total"]).as_tuple().exponent == -2
