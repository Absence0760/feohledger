"""`services/pdf_text.pdf_markup` — every PDF can draw the names it is given.

Every PDF here sets text in ReportLab's built-in Helvetica, a WinAnsi (cp1252)
Type 1 font. Before `pdf_markup`, a vendor name beyond Latin-1 rendered as
filled boxes — ``Ōsaka Seimitsu Kōgyō 株式会社`` came out ``■saka Seimitsu
K■gy■ ■■■■`` on the remittance advice emailed to that supplier — and a long ERP
invoice number in a bare table cell never wrapped, running through the next
column. The seed's names were all ASCII and short, so nothing saw either; the
production-shaped fixture (`scripts/fixtures/realistic_rows.json`, #575 § 2) is
what these tests render with.

Text assertions go through PyMuPDF, which reports the font each glyph was drawn
in: a character drawn in Helvetica that Helvetica has no glyph for IS the box.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pymupdf

from app.services.pdf_text import (
    CJK_FONT,
    HANGUL_FONT,
    UNICODE_FONT,
    ensure_fonts,
    font_for,
    pdf_markup,
)
from scripts.seed_realistic import load_realistic_rows

ROWS = load_realistic_rows()
NON_ASCII = ROWS["non_ascii_vendor_name"]
NEW_SUPPLIER = ROWS["tax_exclusive_invoice"]["vendor_name"]
LONG_NUMBER = ROWS["invoice_numbers"][0]


def _spans(pdf: bytes) -> list[tuple[str, str]]:
    """(text, font name) for every span PyMuPDF finds."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        out = []
        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        out.append((span["text"], span["font"]))
        return out
    finally:
        doc.close()


def _text(pdf: bytes) -> str:
    return "".join(t for t, _ in _spans(pdf))


def _assert_every_glyph_has_a_face(pdf: bytes) -> None:
    """No character outside cp1252 was drawn in a Helvetica face."""
    for text, font in _spans(pdf):
        if "Helvetica" not in font:
            continue
        for ch in text:
            assert font_for(ch) is None, f"{ch!r} drawn in {font} (no glyph) in {text!r}"


# ── the markup itself ──────────────────────────────────────────────────────


def test_latin1_text_is_only_escaped():
    # Byte-identical to the per-module `_escape` it replaced, so a document
    # whose text Helvetica can draw renders exactly as before.
    assert pdf_markup("Société <Générale> & Cie — “Zürich”") == (
        "Société &lt;Générale&gt; &amp; Cie — “Zürich”"
    )
    assert pdf_markup(None) == ""


def test_each_script_falls_back_to_a_face_that_has_it():
    assert pdf_markup("Ōsaka") == f'<font name="{UNICODE_FONT}">Ō</font>saka'
    assert pdf_markup("株式会社") == f'<font name="{CJK_FONT}">株式会社</font>'
    assert pdf_markup("カタカナ") == f'<font name="{CJK_FONT}">カタカナ</font>'
    assert pdf_markup("삼성") == f'<font name="{HANGUL_FONT}">삼성</font>'
    assert pdf_markup("Москва") == f'<font name="{UNICODE_FONT}">Москва</font>'
    # Escaping still applies inside a fallback run.
    assert pdf_markup("Ō&<") == f'<font name="{UNICODE_FONT}">Ō</font>&amp;&lt;'


def test_the_bundled_face_has_every_non_cjk_glyph_the_fixture_uses():
    from reportlab.pdfbase import pdfmetrics

    ensure_fonts()
    cmap = pdfmetrics.getFont(UNICODE_FONT).face.charToGlyph
    for value in (NON_ASCII, NEW_SUPPLIER, ROWS["assignee_name"]):
        for ch in value:
            if font_for(ch) == UNICODE_FONT:
                assert ord(ch) in cmap, ch


# ── every renderer, with the production-shaped names ───────────────────────


def test_remittance_draws_a_non_ascii_vendor_and_wraps_a_long_invoice_number():
    from app.services.remittance_pdf import (
        RemittanceContext,
        RemittanceLine,
        render_remittance_pdf,
    )

    pdf = render_remittance_pdf(
        RemittanceContext(
            payer_name="Acme Corp",
            payer_address=None,
            vendor_name=NON_ASCII,
            vendor_address="Königsallee 92a, 40212 Düsseldorf",
            payment_date=datetime(2026, 10, 1, tzinfo=UTC),
            payment_method="ach",
            payment_reference=ROWS["payment_reference"],
            payment_amount=Decimal("1785.00"),
            currency="USD",
            lines=[RemittanceLine(LONG_NUMBER, "Prüfprotokoll & Versand", Decimal("1785.00"))],
        )
    )
    _assert_every_glyph_has_a_face(pdf)
    text = _text(pdf)
    assert "株式会社" in text and "Kōgyō" in text
    # The description cell used to be a bare string of ESCAPED markup, so an
    # ampersand printed as the literal `&amp;`.
    assert "Prüfprotokoll & Versand" in text and "&amp;" not in text
    # The invoice number is in a wrapping Paragraph now: it lands on more than
    # one line of its 1.6-inch column instead of overprinting Description.
    number_lines = [t for t, _ in _spans(pdf) if t and t in LONG_NUMBER]
    assert len(number_lines) >= 2, number_lines
    assert "".join(number_lines) == LONG_NUMBER


def test_audit_report_draws_non_ascii_actor_and_org():
    from app.schemas.audit import AuditExportEntry
    from app.services.audit_report_pdf import AuditReportContext, render_audit_report_pdf

    entry = AuditExportEntry(
        id="1",
        correlation_id="00000000-0000-0000-0000-000000000002",
        actor_id=None,
        actor_name=ROWS["manager"]["name"],
        actor_email=ROWS["manager"]["email"],
        action="vendor.created",
        entity_type="vendor",
        entity_id="00000000-0000-0000-0000-000000000001",
        details={"name": NON_ASCII},
        created_at="2026-10-01T00:00:00Z",
    )
    pdf = render_audit_report_pdf(
        AuditReportContext(
            org_name=NON_ASCII,
            scope="period",
            scope_label="2026-10",
            generated_at=datetime(2026, 10, 2, tzinfo=UTC),
            generated_by_name=ROWS["assignee_name"],
            generated_by_email="x@example.com",
            entries=[entry],
        )
    )
    _assert_every_glyph_has_a_face(pdf)
    assert "株式会社" in _text(pdf)


def test_analytics_report_draws_non_ascii_cells():
    from app.services.analytics_report_pdf import (
        AnalyticsReportContext,
        render_analytics_report_pdf,
    )

    pdf = render_analytics_report_pdf(
        AnalyticsReportContext(
            title="Vendor spend",
            org_name="Acme Corp",
            period_label="2026-10",
            generated_at=datetime(2026, 10, 2, tzinfo=UTC),
            header=["vendor_name", "total"],
            rows=[[NON_ASCII, "1785.00"], [NEW_SUPPLIER, "1.00"]],
        )
    )
    _assert_every_glyph_has_a_face(pdf)
    text = _text(pdf)
    assert "株式会社" in text and "Ångström" in text


def test_1099_form_draws_a_non_ascii_recipient():
    from app.services.tax_1099_forms import Form1099Context, render_1099_pdf

    pdf = render_1099_pdf(
        Form1099Context(
            tax_year=2026,
            form_type="1099-NEC",
            payer_name="Acme Corp",
            payer_tin_masked="***6789",
            payer_address=None,
            recipient_name=NON_ASCII,
            recipient_tin_masked=None,
            recipient_address="Königsallee 92a, 40212 Düsseldorf",
            box_label="Box 1 — Nonemployee compensation",
            box_amount=Decimal("1785.00"),
            generated_at=date(2026, 10, 2),
        )
    )
    _assert_every_glyph_has_a_face(pdf)
    assert "Kōgyō" in _text(pdf)
