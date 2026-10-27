"""Tests for remittance PDF generation.

PDF inspection is shallow on purpose — we assert on the byte signature,
the embedded payment reference + vendor name, and that the renderer
doesn't blow up on edge cases like XML-special vendor names. Full
visual fidelity belongs in an explicit smoke run, not a unit test.

Text assertions go through PyMuPDF (already a backend dep) since
ReportLab compresses content streams by default — grepping the raw
bytes wouldn't find anything.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pymupdf  # PyMuPDF, already pinned in backend/pyproject.toml


def _extract_text(pdf_bytes: bytes) -> str:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _ctx(**overrides):
    from app.services.remittance_pdf import RemittanceContext, RemittanceLine

    base = dict(
        payer_name="Acme Inc.",
        payer_address="100 Main Street, Springfield, IL 62701",
        vendor_name="Office Supplies Co.",
        vendor_address="50 Vendor Way, Springfield, IL 62701",
        payment_date=datetime(2026, 5, 10, 10, 0),
        payment_method="ach",
        payment_reference="MOCK-ACH-ABCD1234",
        payment_amount=Decimal("1234.56"),
        currency="USD",
        lines=[
            RemittanceLine(
                invoice_number="INV-2026-001",
                description="Q2 office supplies",
                amount=Decimal("1234.56"),
            )
        ],
        notes=None,
    )
    base.update(overrides)
    return RemittanceContext(**base)


def test_pdf_starts_with_pdf_signature():
    """Every well-formed PDF starts with `%PDF-` — proves reportlab actually
    rendered something rather than e.g. raising silently into our buffer."""
    from app.services.remittance_pdf import render_remittance_pdf

    pdf = render_remittance_pdf(_ctx())
    assert pdf.startswith(b"%PDF-")
    assert pdf.endswith(b"%%EOF\n") or pdf.endswith(b"%%EOF")


def test_pdf_embeds_payment_reference_and_vendor():
    """The renderer should put the payment reference and vendor name in the
    PDF stream so a vendor opening it sees the right context."""
    from app.services.remittance_pdf import render_remittance_pdf

    pdf = render_remittance_pdf(_ctx())
    text = _extract_text(pdf)
    assert "MOCK-ACH-ABCD1234" in text
    assert "Office Supplies Co." in text
    assert "INV-2026-001" in text
    assert "Acme Inc." in text  # payer surfaces too


def test_pdf_handles_xml_special_vendor_name():
    """A vendor named `<Manufacturing>` would crash reportlab if we passed
    raw XML to Paragraph. _escape() should keep it clean."""
    from app.services.remittance_pdf import render_remittance_pdf

    pdf = render_remittance_pdf(_ctx(vendor_name="<Test & Sons> Co."))
    assert pdf.startswith(b"%PDF-")
    # PyMuPDF decodes the entities back to plain text on extraction.
    text = _extract_text(pdf)
    assert "<Test & Sons> Co." in text


def test_pdf_renders_with_optional_notes():
    from app.services.remittance_pdf import render_remittance_pdf

    pdf = render_remittance_pdf(_ctx(notes="Paid 2 days early — discount applied"))
    assert "discount applied" in _extract_text(pdf)


def test_pdf_renders_with_multiple_lines():
    """Multi-line case — currently unused (one Payment row = one invoice)
    but the renderer is built to handle it for the future grouping path."""
    from app.services.remittance_pdf import RemittanceLine, render_remittance_pdf

    ctx = _ctx(
        payment_amount=Decimal("3000.00"),
        lines=[
            RemittanceLine("INV-001", "First invoice", Decimal("1000.00")),
            RemittanceLine("INV-002", "Second invoice", Decimal("2000.00")),
        ],
    )
    pdf = render_remittance_pdf(ctx)
    text = _extract_text(pdf)
    assert "INV-001" in text
    assert "INV-002" in text
    assert "USD 3,000.00" in text  # total surfaces formatted


def test_pdf_handles_negative_amount():
    """Stress-test the formatter — refund / reversal could legitimately
    surface as a negative amount on a future row shape."""
    from app.services.remittance_pdf import render_remittance_pdf

    pdf = render_remittance_pdf(_ctx(payment_amount=Decimal("-500.00")))
    assert pdf.startswith(b"%PDF-")
    assert "-USD 500.00" in _extract_text(pdf)


def test_download_header_survives_a_processor_supplied_reference():
    """`Payment.reference` is free text the processor supplies, and it names the
    downloaded file.

    Starlette latin-1-encodes header values, so interpolating a non-ASCII
    reference raised `UnicodeEncodeError` out of the ASGI app rather than
    returning the PDF, and a `"` broke out of the quoted string. Asserts the
    header the route now builds is both latin-1 encodable and unambiguous.
    """
    from app.utils.http import content_disposition_attachment

    for reference in (
        "Zahlung-Überweisung-2026",  # non-latin-1 → used to 500 the download
        'A"B',  # quote → used to break the quoted-string syntax
        "réf/../../etc/passwd",  # separators must not survive into the fallback
    ):
        header = content_disposition_attachment(f"remittance-{reference}.pdf")
        # The bug was an unhandled encode error on the way out of the app.
        header.encode("latin-1")
        assert header.startswith("attachment; ")
        # A bare `filename=` cannot carry these, so the UTF-8 form must be there.
        assert "filename*=UTF-8''" in header

        ascii_fallback = header.split('"')[1]
        # Neither may survive into the legacy parameter: a quote would break out
        # of the quoted string, a slash is a path separator.
        assert '"' not in ascii_fallback
        assert "/" not in ascii_fallback
