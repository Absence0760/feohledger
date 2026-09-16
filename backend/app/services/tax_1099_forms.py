"""1099-NEC / 1099-MISC form generation.

Turns the per-vendor aggregation from ``tax_1099.build_1099_report`` into
form-ready payloads and renders them as PDFs (reportlab, same pattern as
``remittance_pdf``).

Scope (documented in ``docs/tax-1099.md``):

  - **Boxes come from the aggregation, not from the caller.** A vendor's
    reportable total is split across boxes by ``tax_1099.allocate_boxes``
    (invoice GL account → box, via the per-org mapping), and this module
    renders the boxes belonging to the requested form: NEC box 1, or the
    populated MISC boxes (rents / royalties / other income / medical /
    attorney gross proceeds) with a form total beneath them. Filing a
    multi-category vendor's whole total in one box is the mis-report that
    removed the previous single-box simplification.
  - ``misc_box`` survives only as the box a *hand-built* row (one carrying
    no allocation — every row the aggregation produces carries one) is
    rendered into, and as the label shown when a form has no populated box.
  - We render the *information* on an IRS-styled form, not a pixel-perfect
    red-ink Copy A (which must be filed electronically or on official
    scannable stock anyway). The PDF is the payer's / recipient's working
    copy + the basis for the e-file payload.

Pure functions: builders take loaded rows, the renderer takes a context and
returns PDF bytes. No DB queries here — the route loads the data.

**Blocking, and offloaded by the caller.** "No DB" is not "no I/O": the
brand-logo embed (``branding.build_logo_flowable``) does a blocking DNS lookup
and a blocking ``httpx.Client`` GET, and ReportLab lays the document out on the
CPU. Every route therefore calls the renderer through ``await
asyncio.to_thread(render_1099_pdf, ctx)`` rather than on the event loop;
``tests/test_pdf_render_offloaded.py`` is the drift guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.branding import (
    PLATFORM_ACCENT_COLOR,
    BrandContext,
    build_logo_flowable,
    get_brand_context,
)
from app.services.tax_1099 import (
    BOX_CATALOG,
    FORM_MISC,
    FORM_NEC,
    VendorReportRow,
    box_total_for_form,
)
from app.utils.dates import utc_today

__all__ = [
    "FORM_MISC",
    "FORM_NEC",
    "Form1099Box",
    "Form1099Context",
    "build_form_context",
    "mask_tin",
    "render_1099_pdf",
]

_VALID_FORMS = frozenset({FORM_NEC, FORM_MISC})

# MISC box labels, derived from the one catalog in ``tax_1099`` rather than
# restated — a box that can be mapped to must be a box that can be printed.
_MISC_BOX_LABELS = {
    box.number: box.display_label for box in BOX_CATALOG.values() if box.form_type == FORM_MISC
}
_DEFAULT_MISC_BOX = "3"


@dataclass(frozen=True)
class Form1099Box:
    """One printed box line: its label and the money in it. Decimal, never
    float."""

    label: str
    amount: Decimal


@dataclass(frozen=True)
class Form1099Context:
    """Everything the form PDF renderer needs, pre-loaded by the route.

    All money is Decimal. ``recipient_tin_masked`` is the display string
    (e.g. ``**-***6789``) — the renderer never receives the full TIN, so a
    rendered working copy can be downloaded without exposing the full TIN in
    the PDF text layer beyond the masked form."""

    tax_year: int
    form_type: str
    payer_name: str
    payer_tin_masked: str | None
    payer_address: str | None
    recipient_name: str
    recipient_tin_masked: str | None
    recipient_address: str | None
    # The single-box view, retained because it is what a caller building a
    # context by hand supplies, and what the renderer prints when a form has
    # no populated boxes. ``box_amount`` is the FORM's total.
    box_label: str
    box_amount: Decimal
    generated_at: date
    # The per-box breakdown for this form. Empty means "render the single
    # ``box_label`` / ``box_amount`` line" — the hand-built-row path.
    boxes: tuple[Form1099Box, ...] = ()
    # Resolved tenant brand for the header. Defaults to the platform brand so a
    # call site that doesn't pass one still renders.
    brand: BrandContext = field(default_factory=lambda: get_brand_context(None))


def mask_tin(tin: str | None) -> str | None:
    """Mask all but the last 4 digits for display: ``12-3456789`` →
    ``**-***6789``. Returns None when there's no TIN."""
    if not tin:
        return None
    digits = [c for c in tin if c.isdigit()]
    if len(digits) < 4:
        return "****"
    last4 = "".join(digits[-4:])
    # Preserve the separator shape of the original, masking every digit
    # except the trailing four.
    out = []
    masked_so_far = 0
    total_to_mask = len(digits) - 4
    for c in tin:
        if c.isdigit():
            if masked_so_far < total_to_mask:
                out.append("*")
                masked_so_far += 1
            else:
                out.append(c)
        else:
            out.append(c)
    masked = "".join(out)
    return masked if masked else f"***{last4}"


def build_form_context(
    *,
    row: VendorReportRow,
    full_tax_id: str | None,
    tax_year: int,
    form_type: str,
    payer_name: str,
    payer_tax_id: str | None,
    payer_address: str | None,
    recipient_address: str | None,
    misc_box: str = _DEFAULT_MISC_BOX,
    brand: BrandContext | None = None,
) -> Form1099Context:
    """Build a render context for one vendor's 1099.

    ``row`` is the aggregation row; ``full_tax_id`` is passed only so we can
    mask it here (it is never stored on the context in full).

    The printed boxes come from ``row.box_allocations``, narrowed to the boxes
    that belong on ``form_type`` — a vendor with rent and contractor spend gets
    a MISC form showing only the rent and a NEC form showing only the
    contractor money, never the whole reportable total on both. A row carrying
    no allocation (hand-built, or produced before allocation existed) falls
    back to the previous behaviour: the whole reportable total in the single
    requested box."""
    if form_type not in _VALID_FORMS:
        raise ValueError(f"Unsupported form type: {form_type}")

    if form_type == FORM_NEC:
        default_label = "Box 1 — Nonemployee compensation"
    else:
        default_label = _MISC_BOX_LABELS.get(misc_box, _MISC_BOX_LABELS[_DEFAULT_MISC_BOX])

    boxes = tuple(
        Form1099Box(
            label=BOX_CATALOG[a.box].display_label if a.box in BOX_CATALOG else a.label,
            amount=a.amount,
        )
        for a in row.box_allocations
        if a.form_type == form_type
    )
    # Exact-Decimal sum of the printed boxes — the same figure
    # ``box_total_for_form`` gives the filing path, so the working copy and the
    # e-filed amount can never disagree.
    box_amount = box_total_for_form(row, form_type)
    box_label = boxes[0].label if len(boxes) == 1 else default_label

    return Form1099Context(
        tax_year=tax_year,
        form_type=form_type,
        payer_name=payer_name,
        payer_tin_masked=mask_tin(payer_tax_id),
        payer_address=payer_address,
        recipient_name=row.vendor_name,
        recipient_tin_masked=mask_tin(full_tax_id if full_tax_id is not None else row.tax_id),
        recipient_address=recipient_address,
        box_label=box_label,
        box_amount=box_amount,
        generated_at=utc_today(),
        boxes=boxes,
        brand=brand if brand is not None else get_brand_context(None),
    )


def _brand_color(hex_value: str):
    """Resolve a validated brand hex into a ReportLab color, falling back to the
    platform accent if the value is somehow unparseable."""
    try:
        return colors.HexColor(hex_value)
    except Exception:  # noqa: BLE001 — never let a color literal break the PDF.
        return colors.HexColor(PLATFORM_ACCENT_COLOR)


def _money(amount: Decimal) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_1099_pdf(ctx: Form1099Context) -> bytes:
    """Render a single 1099-NEC / 1099-MISC working copy to PDF bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        title=f"{ctx.form_type} ({ctx.tax_year}) — {ctx.recipient_name}",
    )
    brand = ctx.brand
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, leading=13)
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
        spaceAfter=2,
    )
    h_brand = ParagraphStyle(
        "brand",
        parent=styles["Heading2"],
        fontSize=13,
        leading=16,
        textColor=_brand_color(brand.accent_color),
        spaceAfter=8,
    )

    story = []
    # Branded header (`BrandContext.mark`: logo, platform mark + name, or the name
    # alone in the accent color).
    logo = build_logo_flowable(
        brand, max_width_pt=2.4 * inch, max_height_pt=0.6 * inch, name_style=h_brand
    )
    if logo is not None:
        story.append(logo)
        story.append(Spacer(1, 0.08 * inch))
    else:
        story.append(Paragraph(_escape(brand.product_name), h_brand))
    story.append(Paragraph(f"Form {ctx.form_type}", h_title))
    story.append(
        Paragraph(
            f"Tax year <b>{ctx.tax_year}</b> · This is a working copy — file Copy A "
            "electronically or on official scannable stock.",
            ParagraphStyle("sub", parent=body, fontSize=8, textColor=colors.HexColor("#94a3b8")),
        )
    )
    story.append(Spacer(1, 0.2 * inch))

    payer_block = [Paragraph("PAYER", label), Paragraph(_escape(ctx.payer_name), body)]
    if ctx.payer_address:
        payer_block.append(Paragraph(_escape(ctx.payer_address), body))
    if ctx.payer_tin_masked:
        payer_block.append(Paragraph(f"TIN: {_escape(ctx.payer_tin_masked)}", body))

    recipient_block = [
        Paragraph("RECIPIENT", label),
        Paragraph(_escape(ctx.recipient_name), body),
    ]
    if ctx.recipient_address:
        recipient_block.append(Paragraph(_escape(ctx.recipient_address), body))
    recipient_block.append(
        Paragraph(f"TIN: {_escape(ctx.recipient_tin_masked or 'NOT ON FILE')}", body)
    )

    parties = Table([[payer_block, recipient_block]], colWidths=[3.4 * inch, 3.4 * inch])
    parties.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(parties)
    story.append(Spacer(1, 0.3 * inch))

    # One row per populated box, then a total row once there is more than one
    # — a preparer signing the form has to see both the split and the figure
    # it adds up to. A context with no boxes (hand-built row) keeps the
    # single-line rendering.
    if ctx.boxes:
        data = [
            [Paragraph(f"<b>{_escape(b.label)}</b>", body), _money(b.amount)] for b in ctx.boxes
        ]
    else:
        data = [[Paragraph(f"<b>{_escape(ctx.box_label)}</b>", body), _money(ctx.box_amount)]]
    total_row_index = None
    if len(data) > 1:
        total_row_index = len(data)
        data.append(
            [
                Paragraph(f"<b>Total — Form {_escape(ctx.form_type)}</b>", body),
                _money(ctx.box_amount),
            ]
        )

    amount_table = Table(data, colWidths=[5.0 * inch, 1.8 * inch])
    style = [
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]
    if total_row_index is not None:
        style.append(
            (
                "LINEABOVE",
                (0, total_row_index),
                (-1, total_row_index),
                0.9,
                colors.HexColor("#94a3b8"),
            )
        )
        style.append(
            ("BACKGROUND", (0, total_row_index), (-1, total_row_index), colors.HexColor("#f8fafc"))
        )
    amount_table.setStyle(TableStyle(style))
    story.append(amount_table)

    doc.build(story)
    return buf.getvalue()
