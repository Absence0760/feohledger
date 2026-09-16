"""Branded analytics-report PDF generation.

The analytics export surface (``GET /api/analytics/export/{report}``) serves the
invoice-register / vendor-spend / payment-register / aging-snapshot /
cashflow-forecast reports as CSV. This module adds the ``?format=pdf`` dialect:
the same already-serialised rows, rendered as a branded, tabular PDF carrying the
tenant's logo / product name / accent color in the header — the white-label ask
for the CFO/finance export surface.

Pure-function, mirroring ``remittance_pdf`` / ``audit_report_pdf``: it takes the
already-loaded rows (CSV cell strings — the exact same data the CSV dialect
emits, so the PDF is never broader than the CSV) plus the resolved tenant brand,
and returns the PDF bytes. No DB queries — the HTTP route loads the rows and
wraps the bytes in a ``Response``.

**Blocking, and offloaded by the caller.** "No DB" is not "no I/O": the
brand-logo embed (``branding.build_logo_flowable``) does a blocking DNS lookup
and a blocking ``httpx.Client`` GET, and ReportLab lays the document out on the
CPU. Every route therefore calls this through
``await asyncio.to_thread(render_..., ctx)`` rather than on the event loop;
``tests/test_pdf_render_offloaded.py`` is the drift guard.

The logo embed is best-effort + bounded (``build_logo_flowable``): any failure
(no URL, oversized, timeout, undecodable) falls back to the product-name text, so
branding can never break PDF generation. PII discipline: the header is brand
chrome only (product name + logo + accent); the table body is exactly the CSV
cells the route already produced — no enrichment, nothing new.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.branding import (
    PLATFORM_ACCENT_COLOR,
    BrandContext,
    build_logo_flowable,
    get_brand_context,
)


@dataclass
class AnalyticsReportContext:
    """Everything the PDF renderer needs. Pre-loaded by the route so the PDF
    function stays sync + DB-free.

    ``title`` is the human report name ("Invoice Register"); ``header`` is the
    CSV column header row; ``rows`` are the CSV data rows (each a list of cell
    strings — the exact cells the CSV exporter produced). ``org_name`` /
    ``period_label`` are printed in the cover block.
    """

    title: str
    org_name: str
    period_label: str
    generated_at: datetime
    header: list[str]
    rows: list[list[str]]
    # Resolved tenant brand for the header. Defaults to the platform brand so a
    # call site that doesn't pass one still renders.
    brand: BrandContext = field(default_factory=lambda: get_brand_context(None))
    # Optional footer note — e.g. the report-builder export's row-cap
    # truncation notice ("Results truncated at 1000 rows — ..."). Rendered
    # below the table when set; omitted entirely otherwise, so every existing
    # caller that doesn't pass one renders byte-for-byte the same as before.
    note: str | None = None


def render_analytics_report_pdf(ctx: AnalyticsReportContext) -> bytes:
    """Render an analytics report to a (multi-page, landscape) PDF, return bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(LETTER),
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        title=f"{ctx.title} — {ctx.org_name}",
    )
    brand = ctx.brand
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9, leading=12)
    cell = ParagraphStyle("cell", parent=body, fontSize=7.5, leading=9.5)
    h_title = ParagraphStyle(
        "title",
        parent=styles["Heading1"],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0f172a"),
        spaceAfter=4,
    )
    h_section = ParagraphStyle(
        "section",
        parent=styles["Heading2"],
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=8,
        spaceAfter=4,
    )
    h_brand = ParagraphStyle(
        "brand",
        parent=styles["Heading2"],
        fontSize=12,
        leading=15,
        textColor=_brand_color(brand.accent_color),
        spaceAfter=6,
    )

    story = []

    # ---- Branded cover / header ----------------------------------------
    # `BrandContext.mark`: logo when configured + embeddable, the platform mark
    # beside the name while the product keeps the platform's name, else the name
    # alone in the accent color.
    logo = build_logo_flowable(
        brand, max_width_pt=2.2 * inch, max_height_pt=0.55 * inch, name_style=h_brand
    )
    if logo is not None:
        story.append(logo)
        story.append(Spacer(1, 0.06 * inch))
    else:
        story.append(Paragraph(_escape(brand.product_name), h_brand))
    story.append(Paragraph(_escape(ctx.title), h_title))
    story.append(Paragraph(_escape(ctx.org_name), body))
    story.append(Paragraph(f"Period: <b>{_escape(ctx.period_label)}</b>", body))
    story.append(
        Paragraph(
            f"Generated: <b>{ctx.generated_at.strftime('%B %d, %Y %H:%M UTC')}</b>",
            body,
        )
    )
    story.append(Spacer(1, 0.2 * inch))

    # ---- Data table -----------------------------------------------------
    story.append(Paragraph(f"{_escape(ctx.title)} ({len(ctx.rows)} rows)", h_section))
    if not ctx.rows:
        story.append(Paragraph("No rows in scope.", body))
        doc.build(story)
        return buf.getvalue()

    table_data = [[Paragraph(f"<b>{_escape(h)}</b>", cell) for h in ctx.header]]
    for row in ctx.rows:
        table_data.append([Paragraph(_escape(c), cell) for c in row])

    table = Table(table_data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#475569")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)

    if ctx.note:
        h_note = ParagraphStyle(
            "note",
            parent=body,
            fontSize=8.5,
            textColor=colors.HexColor("#b45309"),
            spaceBefore=8,
        )
        story.append(Paragraph(f"<b>Note:</b> {_escape(ctx.note)}", h_note))

    doc.build(story)
    return buf.getvalue()


def _brand_color(hex_value: str):
    """Resolve a validated brand hex into a ReportLab color, falling back to the
    platform accent if the value is somehow unparseable."""
    try:
        return colors.HexColor(hex_value)
    except Exception:  # noqa: BLE001 — never let a color literal break the PDF.
        return colors.HexColor(PLATFORM_ACCENT_COLOR)


def _escape(s: str) -> str:
    """ReportLab's Paragraph parses the string as inline XML — escape <, >, &
    so a cell value containing those characters doesn't break the doc."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
