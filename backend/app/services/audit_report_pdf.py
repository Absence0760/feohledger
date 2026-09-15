"""SOX audit-trail PDF report generation (for external auditors).

The auditor export (``GET /api/audit/export``) already produces JSON + CSV of the
tenant ``audit_log`` scoped to one invoice or a date range. This module adds the
formatted-PDF dialect an external auditor expects: a cover/header, a summary of
event counts grouped by action, and the chronological trail table.

Pure-function, mirroring ``remittance_pdf``: it takes the already-loaded export
entries (``AuditExportEntry``) plus the issuing-org profile and the scope/period
metadata, and returns the PDF bytes. No DB queries — the HTTP route loads
the rows and wraps the bytes in a ``Response``.

**Blocking, and offloaded by the caller.** "No DB" is not "no I/O": the
brand-logo embed (``branding.build_logo_flowable``) does a blocking DNS lookup
and a blocking ``httpx.Client`` GET, and ReportLab lays the document out on the
CPU. Every route therefore calls this through ``await
asyncio.to_thread(render_..., ctx)`` rather than on the event loop;
``tests/test_pdf_render_offloaded.py`` is the drift guard.

PII discipline: this renderer NEVER reaches into a regulated value. It renders
EXACTLY what the export entry already carries (action, entity type/id, actor
name/email, correlation id, created_at, and the ``details`` blob) and nothing
more — it does not re-query or enrich. So the PDF exposes precisely the same data
the JSON + CSV dialects already do, no broader. The audit-write helpers keep
banking/tax-id *values* out of ``details`` (``log_access`` records field-NAMES;
``build_field_diff`` records before/after for non-regulated fields and serialises
money as string-Decimal) — the PII guarantee lives at write time, and this
renderer is faithful to it by adding nothing of its own.
"""

from __future__ import annotations

from collections import Counter
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

from app.schemas.audit import AuditExportEntry
from app.services.branding import (
    PLATFORM_ACCENT_COLOR,
    BrandContext,
    build_logo_flowable,
    get_brand_context,
)


@dataclass
class AuditReportContext:
    """Everything the PDF renderer needs. Pre-loaded by the route so the PDF
    function stays sync + DB-free.

    ``scope`` is "invoice" or "range"; ``scope_label`` is the human description
    of the period or invoice the report covers ("Jan 1 2026 – Mar 31 2026" or
    "Invoice INV-1042"). ``generated_by_name`` / ``generated_by_email`` identify
    the requesting auditor (printed in the header). None of these are regulated.
    """

    org_name: str
    scope: str
    scope_label: str
    generated_at: datetime
    generated_by_name: str
    generated_by_email: str
    entries: list[AuditExportEntry]
    # Resolved tenant brand for the header. Defaults to the platform brand so a
    # call site that doesn't pass one still renders.
    brand: BrandContext = field(default_factory=lambda: get_brand_context(None))


def render_audit_report_pdf(ctx: AuditReportContext) -> bytes:
    """Render the SOX audit trail to a (multi-page, landscape) PDF and return the
    bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(LETTER),
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        title=f"SOX Audit Trail — {ctx.org_name}",
    )
    brand = ctx.brand
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9, leading=12)
    cell = ParagraphStyle("cell", parent=body, fontSize=7.5, leading=9.5)
    label = ParagraphStyle(
        "label",
        parent=body,
        textColor=colors.HexColor("#6b7280"),
        fontSize=8,
        leading=10,
        spaceAfter=2,
    )
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

    # ---- Cover / header -------------------------------------------------
    # Branded header (`BrandContext.mark`: logo, platform mark + name, or the name
    # alone in the accent color).
    logo = build_logo_flowable(
        brand, max_width_pt=2.2 * inch, max_height_pt=0.55 * inch, name_style=h_brand
    )
    if logo is not None:
        story.append(logo)
        story.append(Spacer(1, 0.06 * inch))
    else:
        story.append(Paragraph(_escape(brand.product_name), h_brand))
    story.append(Paragraph("SOX Audit Trail", h_title))
    story.append(Paragraph(_escape(ctx.org_name), body))
    story.append(
        Paragraph(
            f"Scope: <b>{_escape(ctx.scope_label)}</b>",
            body,
        )
    )
    story.append(
        Paragraph(
            f"Generated: <b>{ctx.generated_at.strftime('%B %d, %Y %H:%M UTC')}</b> · "
            f"By: <b>{_escape(ctx.generated_by_name)}</b> "
            f"({_escape(ctx.generated_by_email)})",
            body,
        )
    )
    story.append(Spacer(1, 0.2 * inch))

    # ---- Summary --------------------------------------------------------
    story.append(Paragraph("Summary", h_section))
    story.append(Paragraph(f"Total events: <b>{len(ctx.entries)}</b>", body))

    counts = Counter(e.action for e in ctx.entries)
    if counts:
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph("Events by action", label))
        summary_data = [["Action", "Count"]]
        # Sort by count desc, then action asc for stable, readable output.
        for action, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            summary_data.append([_escape(action), str(count)])
        summary_table = Table(summary_data, colWidths=[4.0 * inch, 1.2 * inch])
        summary_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#475569")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("FONTSIZE", (0, 1), (-1, -1), 8),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#fafafa")],
                    ),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#cbd5e1")),
                ]
            )
        )
        story.append(summary_table)

    # ---- Chronological trail -------------------------------------------
    story.append(Paragraph("Audit Trail", h_section))
    if not ctx.entries:
        story.append(Paragraph("No audit events in scope.", body))
        doc.build(story)
        return buf.getvalue()

    header = [
        "Timestamp",
        "Action",
        "Entity",
        "Entity ID",
        "Actor",
        "Correlation",
        "Details",
    ]
    table_data = [[Paragraph(f"<b>{h}</b>", cell) for h in header]]
    for e in ctx.entries:
        actor = e.actor_name or ""
        if e.actor_email:
            actor = f"{actor}\n{e.actor_email}" if actor else e.actor_email
        table_data.append(
            [
                Paragraph(_escape(e.created_at), cell),
                Paragraph(_escape(e.action), cell),
                Paragraph(_escape(e.entity_type), cell),
                Paragraph(_escape(_short(e.entity_id)), cell),
                Paragraph(_escape(actor), cell),
                Paragraph(_escape(_short(e.correlation_id)), cell),
                Paragraph(_escape(_details_str(e.details)), cell),
            ]
        )

    trail = Table(
        table_data,
        colWidths=[
            1.5 * inch,
            1.6 * inch,
            0.9 * inch,
            1.1 * inch,
            1.7 * inch,
            1.1 * inch,
            1.7 * inch,
        ],
        repeatRows=1,
    )
    trail.setStyle(
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
    story.append(trail)

    doc.build(story)
    return buf.getvalue()


def _brand_color(hex_value: str):
    """Resolve a validated brand hex into a ReportLab color, falling back to the
    platform accent if the value is somehow unparseable."""
    try:
        return colors.HexColor(hex_value)
    except Exception:  # noqa: BLE001 — never let a color literal break the PDF.
        return colors.HexColor(PLATFORM_ACCENT_COLOR)


def _short(value: str | None) -> str:
    """Shorten a UUID for the table (first 8 chars) — full value lives in the
    JSON/CSV export. Non-UUID values pass through untouched."""
    if not value:
        return ""
    s = str(value)
    if len(s) >= 8 and s.count("-") == 4:
        return s[:8] + "…"
    return s


def _details_str(details: dict | None) -> str:
    """Render the ``details`` dict to a compact ``k=v`` string.

    This faithfully reproduces whatever the audit row stored — the same blob the
    JSON/CSV export dialects already emit. The PII guarantee is upheld at write
    time (``log_access`` stores field-NAMES; ``build_field_diff`` keeps regulated
    *values* out and serialises money as string-Decimal), not here: this renderer
    deliberately adds no enrichment of its own."""
    if not details:
        return ""
    parts = []
    for k, v in details.items():
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def _escape(s: str) -> str:
    """ReportLab's Paragraph parses the string as inline XML — escape <, >, &
    so a value containing those characters doesn't break the doc."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
