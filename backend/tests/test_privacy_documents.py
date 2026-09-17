"""The object-storage leg of the privacy surface — issues #423 / #424 / #425.

Three things are proved here, and they are the three the shared traversal exists
to keep honest:

  * **Masking.** ``utils/bank_masking`` reduces a bank blob for disclosure with
    an ALLOWLIST, so a key nobody anticipated is masked rather than published.
  * **The retain/delete split.** ``services/privacy_documents`` deletes the
    documents whose sole subject is the erased party (W-9/W-8, supplier-authored
    chat attachments, the Positive Pay file) and leaves the transaction evidence
    the money trail keeps (invoice PDF, contract document, expense receipt,
    archived vendor statement) exactly where it is.
  * **Pointer hygiene.** A deleted object's DB pointer is nulled; a failed delete
    leaves the pointer so the next run retries.

``storage._delete_object`` is stubbed rather than driven against MinIO: the
question under test is which keys the traversal decides to delete, and the
offload contract of the primitive itself is already pinned by
``tests/test_storage_nonblocking.py``.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.models.contract import Contract
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment, PaymentRun
from app.models.positive_pay import PositivePayFile
from app.models.supplier_chat import ChatAuthorRole, SupplierChatMessage, SupplierChatThread
from app.models.vendor import Vendor
from app.models.vendor_statement_recon import VendorStatementReconciliation
from app.services import privacy_documents, storage
from app.services.privacy_documents import (
    DELETE,
    RETAIN,
    collect_subject_documents,
    delete_subject_documents,
    documents_manifest,
)
from app.utils.bank_masking import (
    mask_bank_details,
    mask_beneficial_owner_data,
    mask_secret,
)

# ---------------------------------------------------------------------------
# Masking (issue #423) — pure
# ---------------------------------------------------------------------------


def test_mask_secret_keeps_only_the_last_four():
    assert mask_secret("000111222") == "****1222"
    assert mask_secret("021000021") == "****0021"
    # Too short to have a meaningful tail — masking three of three digits would
    # not be a mask.
    assert mask_secret("12") == "****"
    assert mask_secret(None) is None
    assert mask_secret("") is None


def test_mask_bank_details_masks_every_named_secret():
    masked = mask_bank_details(
        {
            "account_number": "000111222",
            "routing_number": "021000021",
            "wire_routing_number": "026009593",
            "iban": "GB29NWBK60161331926819",
        }
    )
    assert masked["account_number"] == "****1222"
    assert masked["routing_number"] == "****0021"
    assert masked["wire_routing_number"] == "****9593"
    assert masked["iban"] == "****6819"
    assert masked["_masked"] is True
    assert masked["_masked_keys"] == [
        "account_number",
        "iban",
        "routing_number",
        "wire_routing_number",
    ]


def test_mask_bank_details_passes_display_keys_through():
    masked = mask_bank_details(
        {"bank_name": "First National", "country": "US", "account_last4": "1222"}
    )
    assert masked["bank_name"] == "First National"
    assert masked["country"] == "US"
    assert masked["account_last4"] == "1222"
    assert masked["_masked_keys"] == []


def test_mask_bank_details_fails_closed_on_an_unknown_key():
    """The allowlist is the point: a key this module has never seen is MASKED.

    The bug this replaces reduced by a denylist, so a newly-added secret-shaped
    key would have been exported verbatim. Here the failure direction is the
    safe one — an unanticipated display key merely renders as `****` until
    someone lists it.
    """
    masked = mask_bank_details({"account_reference": "SECRET-987654"})
    assert masked["account_reference"] == "****7654"
    assert masked["_masked_keys"] == ["account_reference"]


def test_mask_bank_details_none_stays_none():
    assert mask_bank_details(None) is None
    assert mask_bank_details({}) is None


def test_mask_beneficial_owner_data_withholds_third_party_identity_fields():
    masked = mask_beneficial_owner_data(
        {
            "owners": [
                {
                    "name": "Jane Doe",
                    "ownership_percentage": 51,
                    "country": "US",
                    "passport_number": "X1234567",
                    "date_of_birth": "1980-01-01",
                }
            ]
        }
    )
    owner = masked["owners"][0]
    assert owner == {"name": "Jane Doe", "ownership_percentage": 51, "country": "US"}
    assert masked["_withheld_keys"] == ["date_of_birth", "passport_number"]
    # Nothing of the withheld values survives anywhere in the payload.
    assert "X1234567" not in str(masked)


def test_mask_beneficial_owner_data_withholds_an_unrecognised_shape_entirely():
    masked = mask_beneficial_owner_data({"ubo_tax_id": "12-3456789"})
    assert "12-3456789" not in str(masked)
    assert masked["_masked"] is True


# ---------------------------------------------------------------------------
# The traversal (issues #424 / #425) — against real Postgres
# ---------------------------------------------------------------------------

W9_KEY = "org/tax-forms/v/w9/w9.pdf"
INVOICE_KEY = "org/inv/invoice.pdf"
CONTRACT_KEY = "org/contracts/c/msa.pdf"
STATEMENT_KEY = "org/vendor-statements/r/statement.pdf"
SUPPLIER_ATTACHMENT_KEY = "org/chat/i/m1/supplier-note.pdf"
AP_ATTACHMENT_KEY = "org/chat/i/m2/ap-note.pdf"
POSITIVE_PAY_KEY = "org/positive-pay/p/check-issue.csv"


async def _seed_document_graph(tenant_mk, org_id):
    """A vendor carrying one document of every kind the traversal can reach."""
    vendor_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    run_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    supplier_msg_id = uuid.uuid4()
    ap_msg_id = uuid.uuid4()
    pp_id = uuid.uuid4()
    contract_id = uuid.uuid4()
    recon_id = uuid.uuid4()

    async with tenant_mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=org_id,
                name="Docs Supplier Ltd",
                code="V-DOC",
                email="docs@supplier.test",
                tax_id="98-7654321",
                bank_details={"account_number": "000111222"},
                w9_file_key=W9_KEY,
                status="active",
            )
        )
        s.add(PaymentRun(id=run_id, organization_id=org_id, status="executed"))
        s.add(
            Invoice(
                id=invoice_id,
                correlation_id=uuid.uuid4(),
                organization_id=org_id,
                invoice_number="INV-DOC-1",
                vendor_name="Docs Supplier Ltd",
                vendor_id=vendor_id,
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.paid,
                file_key=INVOICE_KEY,
            )
        )
        await s.flush()
        s.add(
            Payment(
                id=uuid.uuid4(),
                invoice_id=invoice_id,
                payment_run_id=run_id,
                amount=Decimal("100.00"),
                method="ach",
                status="completed",
            )
        )
        s.add(
            PositivePayFile(
                id=pp_id,
                organization_id=org_id,
                payment_run_id=run_id,
                file_type="check_issue",
                bank_format="csv",
                status="generated",
                item_count=1,
                total_amount=Decimal("100.00"),
                content_hash="a" * 64,
                file_key=POSITIVE_PAY_KEY,
                account_last4="1222",
            )
        )
        s.add(
            Contract(
                id=contract_id,
                organization_id=org_id,
                contract_number="C-1",
                vendor_id=vendor_id,
                file_key=CONTRACT_KEY,
            )
        )
        s.add(
            VendorStatementReconciliation(
                id=recon_id,
                organization_id=org_id,
                vendor_id=vendor_id,
                vendor_name="Docs Supplier Ltd",
                statement_date=date(2026, 1, 31),
                file_key=STATEMENT_KEY,
            )
        )
        s.add(SupplierChatThread(id=thread_id, organization_id=org_id, invoice_id=invoice_id))
        await s.flush()
        s.add(
            SupplierChatMessage(
                id=supplier_msg_id,
                thread_id=thread_id,
                author_role=ChatAuthorRole.supplier,
                body="Here is the certificate you asked for",
                attachments=[
                    {
                        "file_key": SUPPLIER_ATTACHMENT_KEY,
                        "filename": "supplier-note.pdf",
                        "content_type": "application/pdf",
                        "size": 10,
                    }
                ],
            )
        )
        s.add(
            SupplierChatMessage(
                id=ap_msg_id,
                thread_id=thread_id,
                author_role=ChatAuthorRole.ap_team,
                body="Our remittance advice",
                attachments=[
                    {
                        "file_key": AP_ATTACHMENT_KEY,
                        "filename": "ap-note.pdf",
                        "content_type": "application/pdf",
                        "size": 10,
                    }
                ],
            )
        )
        await s.commit()
    return {
        "vendor_id": vendor_id,
        "invoice_id": invoice_id,
        "positive_pay_id": pp_id,
        "supplier_msg_id": supplier_msg_id,
        "ap_msg_id": ap_msg_id,
    }


async def test_traversal_splits_sole_subject_documents_from_shared_evidence(realdb):
    """The whole design decision, asserted key by key."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    seeded = await _seed_document_graph(tenant_mk, org_id)

    async with tenant_mk() as s:
        docs = await collect_subject_documents(
            subject_type="vendor_contact",
            subject_id=seeded["vendor_id"],
            organization_id=org_id,
            tenant_db=s,
        )

    by_key = {d.file_key: d for d in docs}
    assert set(by_key) == {
        W9_KEY,
        INVOICE_KEY,
        CONTRACT_KEY,
        STATEMENT_KEY,
        SUPPLIER_ATTACHMENT_KEY,
        AP_ATTACHMENT_KEY,
        POSITIVE_PAY_KEY,
    }

    # Sole subject, not transaction evidence → deleted.
    assert by_key[W9_KEY].disposition == DELETE
    assert by_key[SUPPLIER_ATTACHMENT_KEY].disposition == DELETE
    assert by_key[POSITIVE_PAY_KEY].disposition == DELETE

    # Evidence behind a retained money row → retained, with a stated reason.
    for key in (INVOICE_KEY, CONTRACT_KEY, STATEMENT_KEY, AP_ATTACHMENT_KEY):
        assert by_key[key].disposition == RETAIN, key
        assert by_key[key].reason, key


async def test_erasure_deletes_only_the_delete_dispositioned_objects(realdb, monkeypatch):
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    seeded = await _seed_document_graph(tenant_mk, org_id)

    deleted: list[str] = []

    async def _fake_delete(key: str) -> None:
        deleted.append(key)

    monkeypatch.setattr(storage, "_delete_object", _fake_delete)

    async with tenant_mk() as s:
        docs = await collect_subject_documents(
            subject_type="vendor_contact",
            subject_id=seeded["vendor_id"],
            organization_id=org_id,
            tenant_db=s,
        )
        outcome = await delete_subject_documents(docs)

    assert sorted(deleted) == sorted([W9_KEY, SUPPLIER_ATTACHMENT_KEY, POSITIVE_PAY_KEY])
    assert outcome.deleted == 3
    assert outcome.retained == 4
    assert outcome.failed == 0
    # The invoice PDF was never handed to storage at all.
    assert INVOICE_KEY not in deleted


async def test_erasure_nulls_the_pointer_of_every_deleted_object(realdb, monkeypatch):
    """After erasure, no row points at a key that no longer exists."""
    from app.services.privacy_erasure import erase_vendor_contact

    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    seeded = await _seed_document_graph(tenant_mk, org_id)

    async def _fake_delete(key: str) -> None:
        return None

    monkeypatch.setattr(storage, "_delete_object", _fake_delete)

    async with tenant_mk() as s:
        result = await erase_vendor_contact(
            subject_id=seeded["vendor_id"],
            organization_id=org_id,
            tenant_db=s,
        )
        await s.commit()

    assert result.documents_deleted == 3
    assert result.documents_retained == 4

    async with tenant_mk() as s:
        vendor = await s.get(Vendor, seeded["vendor_id"])
        assert vendor.w9_file_key is None

        pp = await s.get(PositivePayFile, seeded["positive_pay_id"])
        assert pp.file_key is None
        assert pp.meta["file_erased_reason"] == "data_subject_erasure"
        # The PII-free evidence the row exists to carry is untouched.
        assert pp.content_hash == "a" * 64
        assert pp.account_last4 == "1222"
        assert pp.total_amount == Decimal("100.00")

        supplier_msg = await s.get(SupplierChatMessage, seeded["supplier_msg_id"])
        assert supplier_msg.attachments is None

        # Shared evidence untouched — pointer AND object.
        invoice = await s.get(Invoice, seeded["invoice_id"])
        assert invoice.file_key == INVOICE_KEY
        ap_msg = await s.get(SupplierChatMessage, seeded["ap_msg_id"])
        assert ap_msg.attachments[0]["file_key"] == AP_ATTACHMENT_KEY


async def test_a_failed_delete_leaves_the_pointer_for_the_next_run(realdb, monkeypatch):
    """A storage failure must not be laundered into a completed erasure."""
    from app.services.privacy_erasure import erase_vendor_contact

    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    seeded = await _seed_document_graph(tenant_mk, org_id)

    async def _flaky_delete(key: str) -> None:
        if key == W9_KEY:
            raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(storage, "_delete_object", _flaky_delete)

    async with tenant_mk() as s:
        result = await erase_vendor_contact(
            subject_id=seeded["vendor_id"],
            organization_id=org_id,
            tenant_db=s,
        )
        await s.commit()

    assert result.documents_failed == 1
    assert result.documents_deleted == 2

    async with tenant_mk() as s:
        vendor = await s.get(Vendor, seeded["vendor_id"])
        # Still pointing at the object, because the object is still there.
        assert vendor.w9_file_key == W9_KEY


async def test_delete_is_attempted_once_per_key(realdb, monkeypatch):
    """Two rows referencing one object must not delete it twice."""
    calls: list[str] = []

    async def _fake_delete(key: str) -> None:
        calls.append(key)

    monkeypatch.setattr(storage, "_delete_object", _fake_delete)

    doc = privacy_documents.SubjectDocument(
        file_key="org/tax-forms/v/w9/w9.pdf",
        kind=privacy_documents.KIND_TAX_FORM,
        disposition=DELETE,
        record_type="vendor",
        record_id=str(uuid.uuid4()),
    )
    outcome = await delete_subject_documents([doc, doc])
    assert calls == ["org/tax-forms/v/w9/w9.pdf"]
    assert outcome.deleted == 1


def test_manifest_reports_the_split_without_inlining_bytes():
    docs = [
        privacy_documents.SubjectDocument(
            file_key=W9_KEY,
            kind=privacy_documents.KIND_TAX_FORM,
            disposition=DELETE,
            record_type="vendor",
            record_id="v1",
        ),
        privacy_documents.SubjectDocument(
            file_key=INVOICE_KEY,
            kind=privacy_documents.KIND_INVOICE,
            disposition=RETAIN,
            record_type="invoice",
            record_id="i1",
            reason="transaction evidence",
        ),
    ]
    manifest = documents_manifest(docs)
    assert manifest["total"] == 2
    assert manifest["deleted_on_erasure"] == 1
    assert manifest["retained_on_erasure"] == 1
    assert {d["file_key"] for d in manifest["documents"]} == {W9_KEY, INVOICE_KEY}
    # References only — no base64 payload anywhere in the manifest.
    assert all("content" not in d and "bytes" not in d for d in manifest["documents"])


@pytest.mark.parametrize("subject_type", ["nonsense", ""])
def test_unknown_subject_type_collects_nothing(subject_type):
    import asyncio

    result = asyncio.run(
        collect_subject_documents(
            subject_type=subject_type,
            subject_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            tenant_db=None,
        )
    )
    assert result == []
