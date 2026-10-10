"""run_extraction auto-routes structured files to the einvoice adapter.

A UBL / Factur-X file overrides the org's configured adapter; a plain PDF
still uses the configured (mock) adapter. Structured invoices persist Decimal
amounts and auto-approve at confidence 1.0 when the workflow enables it.

Follows the mocked-DB pattern from test_extraction_gl_validation.py.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import fitz
import pytest

_FIX = Path(__file__).parent / "fixtures" / "e_invoice"
_UBL = (_FIX / "ubl_invoice.xml").read_bytes()
_CII = (_FIX / "cii_invoice.xml").read_bytes()


def _facturx_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page()
    doc.embfile_add("factur-x.xml", _CII, filename="factur-x.xml")
    return doc.tobytes()


def _make_invoice():
    from app.models.invoice import InvoiceStatus

    return SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=None,
        file_key="invoices/inv.xml",
        status=InvoiceStatus("pending"),
        amount=None,
        vendor_name=None,
        invoice_number=None,
        invoice_date=None,
        due_date=None,
        payment_terms=None,
        payment_method=None,
        po_number=None,
        description=None,
        vendor_address=None,
        vendor_tax_id=None,
        reference_number=None,
        bill_to_address=None,
        remit_to_address=None,
        subtotal=None,
        tax_amount=None,
        tax_rate=None,
        discount_amount=None,
        shipping_amount=None,
        currency="USD",
        gl_account=None,
        cost_center=None,
        vendor_id=None,
        warnings=None,
        po_match=None,
        approval_date=None,
        approved_by=None,
    )


def _make_db():
    db = AsyncMock()
    db.add = MagicMock()
    generic = MagicMock()
    generic.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    generic.scalar_one_or_none = MagicMock(return_value=None)
    # No row for the locked re-read before an approval: decide from the double.
    generic.one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(side_effect=[generic] * 60)
    return db


def _patch_internals(file_bytes: bytes, *, captured_results: list, captured_status: list):
    """Patch everything run_extraction reaches EXCEPT the adapter machinery,
    so the real auto-detect + real einvoice adapter (or mock) run."""
    stack = ExitStack()

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: file_bytes)}
    stack.enter_context(patch("boto3.client", return_value=mock_s3))

    stack.enter_context(patch("app.services.rag.extract_invoice_text", return_value=""))
    stack.enter_context(patch("app.services.rag.retrieve_similar", AsyncMock(return_value=[])))
    stack.enter_context(patch("app.services.rag.build_few_shot_prompt", return_value=""))
    stack.enter_context(patch("app.services.rag.neighbors_to_metadata", return_value=[]))

    fake_vendor = SimpleNamespace(id=uuid.uuid4())
    stack.enter_context(
        patch(
            "app.services.vendor_matching.match_and_link_vendor",
            AsyncMock(return_value=(fake_vendor, "matched")),
        )
    )
    stack.enter_context(
        patch("app.services.vendor_priors.apply_priors_to_invoice", AsyncMock(return_value=[]))
    )
    stack.enter_context(
        patch(
            "app.services.duplicate_detection.find_semantic_duplicates",
            AsyncMock(return_value=[]),
        )
    )
    stack.enter_context(
        patch("app.services.duplicate_detection.matches_to_warning", return_value=None)
    )
    stack.enter_context(patch("app.services.invoice_warnings.refresh_warnings", AsyncMock()))
    stack.enter_context(patch("app.services.extraction.advance_workflow", AsyncMock()))

    # Capture the InvoiceExtractionResult method (which adapter ran) and the
    # final transition status.
    async def fake_transition(db, invoice, target_status, **kwargs):
        captured_status.append(target_status)

    stack.enter_context(
        patch("app.services.extraction.transition_invoice", AsyncMock(side_effect=fake_transition))
    )

    return stack


def _extraction_method(db) -> str | None:
    """Pull the persisted InvoiceExtractionResult.method off db.add calls."""
    from app.models.invoice import InvoiceExtractionResult

    for call in db.add.call_args_list:
        obj = call.args[0]
        if isinstance(obj, InvoiceExtractionResult):
            return obj.method
    return None


def _auto_approve_instance():
    """A workflow instance whose extraction step auto-approves at >= 0.95."""
    snapshot = {
        "steps": [
            {
                "type": "extraction",
                "config": {"auto_approve_enabled": True, "auto_approve_threshold": 0.95},
            },
            {"type": "approval", "config": {}},
        ]
    }
    return SimpleNamespace(id=uuid.uuid4(), steps_config_snapshot=snapshot, state="running")


@pytest.mark.asyncio
async def test_ubl_routes_to_einvoice_adapter_and_persists_decimals():
    from app.services.extraction import run_extraction

    invoice = _make_invoice()
    invoice.file_key = "invoices/inv.xml"
    db = _make_db()
    statuses: list = []

    instance = _auto_approve_instance()
    with _patch_internals(_UBL, captured_results=[], captured_status=statuses):
        with patch(
            "app.services.extraction.get_workflow_instance", AsyncMock(return_value=instance)
        ):
            await run_extraction(db, invoice, actor_id=uuid.uuid4())

    from app.models.invoice import InvoiceStatus

    # Routed to einvoice, NOT mock/vision.
    assert _extraction_method(db) == "einvoice"
    # Decimal amounts persisted on the invoice.
    assert invoice.amount == Decimal("1190.00")
    assert isinstance(invoice.amount, Decimal)
    assert invoice.subtotal == Decimal("1000.00")
    assert invoice.tax_amount == Decimal("190.00")
    assert invoice.currency == "EUR"
    assert invoice.po_number == "PO-7788"
    assert invoice.vendor_tax_id == "DE123456789"
    assert invoice.invoice_date == date(2024, 3, 15)
    # Confidence 1.0 trips the 0.95 auto-approve threshold.
    assert statuses[-1] == InvoiceStatus.approved
    assert invoice.approved_by == "system (auto-approve)"


@pytest.mark.asyncio
async def test_cii_standalone_routes_to_einvoice_adapter():
    """A standalone UN/CEFACT CII .xml file (not embedded in a PDF) must
    auto-route through the einvoice adapter and persist Decimal amounts —
    exercising the DetectedFormat.CII_XML branch + extract_mime_type path in
    run_extraction, which the UBL and Factur-X tests don't cover."""
    from app.services.extraction import run_extraction

    invoice = _make_invoice()
    invoice.file_key = "invoices/cii.xml"
    db = _make_db()
    statuses: list = []

    with _patch_internals(_CII, captured_results=[], captured_status=statuses):
        with patch("app.services.extraction.get_workflow_instance", AsyncMock(return_value=None)):
            await run_extraction(db, invoice, actor_id=uuid.uuid4())

    assert _extraction_method(db) == "einvoice"
    assert invoice.amount == Decimal("1440.00")
    assert isinstance(invoice.amount, Decimal)
    assert invoice.currency == "EUR"


@pytest.mark.asyncio
async def test_facturx_pdf_routes_to_einvoice_adapter():
    from app.services.extraction import run_extraction

    invoice = _make_invoice()
    invoice.file_key = "invoices/inv.pdf"
    db = _make_db()
    statuses: list = []

    with _patch_internals(_facturx_pdf(), captured_results=[], captured_status=statuses):
        with patch("app.services.extraction.get_workflow_instance", AsyncMock(return_value=None)):
            await run_extraction(db, invoice, actor_id=uuid.uuid4())

    assert _extraction_method(db) == "einvoice"
    assert invoice.amount == Decimal("1440.00")
    assert invoice.currency == "EUR"


@pytest.mark.asyncio
async def test_plain_pdf_still_uses_configured_adapter():
    from app.services.extraction import run_extraction

    invoice = _make_invoice()
    invoice.file_key = "invoices/scan.pdf"
    db = _make_db()
    statuses: list = []

    # A plain PDF with no embedded XML — detect returns NONE → mock adapter.
    plain = fitz.open()
    plain.new_page().insert_text((72, 72), "Just a scan")
    plain_bytes = plain.tobytes()

    # Configure the org to use the local mock adapter (no network) so the
    # non-structured path runs to completion and persists its result row.
    org_settings = {"extraction": {"program_type": "byok", "provider": "mock"}}
    with _patch_internals(plain_bytes, captured_results=[], captured_status=statuses):
        with patch("app.services.extraction.get_workflow_instance", AsyncMock(return_value=None)):
            await run_extraction(db, invoice, actor_id=uuid.uuid4(), org_settings=org_settings)

    # Detect returned NONE → no override → the configured mock adapter ran.
    method = _extraction_method(db)
    assert method == "mock"
    assert method != "einvoice"
