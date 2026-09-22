"""PO matching service — match invoices against purchase orders and goods receipts.

Supports 2-way (invoice vs PO), 3-way (invoice vs PO vs GR), and 4-way
(invoice vs PO vs GR vs Quality Inspection) matching.
"""

from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.invoice import Invoice
from app.models.procurement import GoodsReceipt, PurchaseOrder, po_currency_code
from app.models.quality_inspection import QualityInspection
from app.tenant import apply_entity_scope

# Goods-receipt statuses that record a delivery which was undone or never
# happened. `GoodsReceipt.status` is a free-form `String(30)` written by the
# receiving side (seed data and the ERP feed both use "received"), so this is an
# EXCLUSION list, not an allowlist: an unrecognised-but-live status such as
# "partially_received" must keep counting, whereas silently counting a cancelled
# one is what let a reversed delivery satisfy the 3-way quantity leg.
#
# Compared case-insensitively; both the US and UK spellings are listed because
# the column is free-form and neither is normalised on the way in.
CANCELLED_GR_STATUSES = frozenset({"cancelled", "canceled", "void", "voided", "reversed"})


def _to_decimal(value, default: Decimal = Decimal("0")) -> Decimal:
    """Coerce a money/percent value to exact Decimal via str() (never Decimal(float)).

    Floats are bridged through ``str`` so a config literal like ``5.1`` lands as
    ``Decimal('5.1')`` rather than the binary-float artefact. Unparseable /
    ``None`` values fall back to ``default``."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


#: `MatchResult.currency_check` values. The currency leg of the amount control:
#:
#: ``same``       both codes known and equal — the amounts were compared.
#: ``different``  both known and unequal — the amounts were NOT compared, and
#:                the match is a ``mismatch``: an invoice for EUR 1,000 is not
#:                "within tolerance" of a USD 1,000 order, and no variance
#:                between them means anything.
#: ``unknown``    one side records no code (in practice the PO: a pre-0099 row
#:                with no requisition behind it, or an ERP that stated none).
#:                A missing code cannot prove a mismatch, so the amounts are
#:                compared at face value, exactly as before the column existed —
#:                but the result says the currency was NOT verified, the modal
#:                shows that beside the PO total, and the exception agents treat
#:                it as unproven and escalate rather than adjust or approve
#:                (decisions §197).
CURRENCY_SAME = "same"
CURRENCY_DIFFERENT = "different"
CURRENCY_UNKNOWN = "unknown"


def compare_currencies(invoice_currency: object, po_currency: object) -> str:
    """The currency leg for one invoice/PO pair — see the constants above.

    Both sides are normalised through `po_currency_code`, so `"eur"` and
    `"EUR "` agree and a malformed code counts as absent rather than as a
    currency that happens to differ.
    """
    inv = po_currency_code(invoice_currency)
    po = po_currency_code(po_currency)
    if inv is None or po is None:
        return CURRENCY_UNKNOWN
    return CURRENCY_SAME if inv == po else CURRENCY_DIFFERENT


def _labelled(amount: Decimal, currency: str | None) -> str:
    """`1234.50 EUR` — or the bare figure when no code is known.

    `issues` is rendered verbatim, so the code is spelled out rather than
    symbolised; it used to print `$` on both figures whatever either was in.
    """
    figure = f"{amount:.2f}"
    return f"{figure} {currency}" if currency else figure


def format_quantity(value: Decimal) -> str:
    """Render a quantity for a human-readable issue string.

    ``normalize()`` drops the ``Numeric(12, 4)`` trailing zeros (``12.0000`` →
    ``12``) and ``:f`` keeps the result in plain notation — ``normalize()``
    alone turns ``100`` into ``1E+2``.
    """
    return f"{value.normalize():f}"


def _json_safe(value):
    """Recursively convert Decimals to JSON-serialisable floats.

    ``MatchResult`` carries exact Decimal money fields in memory (money is never
    float), but ``invoice.po_match`` is a JSONB column whose default serialiser
    can't encode Decimal. This renders the persisted display artefact back to the
    numeric wire shape every downstream reader (UI modal, analytics) expects,
    while every arithmetic/comparison upstream stayed in exact Decimal."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


@dataclass
class MatchResult:
    """Result of PO matching for an invoice.

    Money fields (``po_total``, ``amount_variance``, ``amount_variance_pct``) are
    exact ``Decimal`` — the tolerance gate and every variance figure are computed
    and compared in Decimal, never float. Use ``to_json_dict()`` to persist onto
    the ``invoice.po_match`` JSONB column (Decimals rendered back to numbers)."""

    match_type: str = "none"  # "none", "2-way", "3-way", "4-way"
    status: str = "no_po"  # "no_po", "matched", "mismatch", "partial"

    po_id: str | None = None
    po_number: str | None = None
    po_total: Decimal | None = None
    #: The code `po_total` is in — the PO's own, `None` when it records none.
    #: A client labels `po_total` with THIS, never with the invoice's currency.
    po_currency: str | None = None
    #: `same` / `different` / `unknown` (see `compare_currencies`); `None` only
    #: when no PO was found.
    currency_check: str | None = None
    gr_id: str | None = None

    #: invoice - PO, in the invoice's currency. `None` when the two are in
    #: DIFFERENT currencies: no figure is the difference between EUR and USD
    #: amounts, and a 0 would read as a perfect match.
    amount_variance: Decimal | None = Decimal("0")
    amount_variance_pct: Decimal | None = Decimal("0")
    within_tolerance: bool = False

    # 4-way: quality inspection leg
    inspection_id: str | None = None
    inspection_result: str | None = None  # 'pass' | 'fail' | 'partial' or None
    inspection_accepted_quantity: float | None = None
    inspection_required: bool = False
    #: The inspection's own deviation notes, when it failed. Carried on the
    #: result so `invoice_warnings` can parameterize the quality-hold sentence
    #: from the record instead of cutting a substring out of `issues`.
    inspection_deviation_notes: str | None = None

    # 3-way: received quantity EXCEEDS what was ordered. Additive to the
    # persisted `invoice.po_match` shape — `status` deliberately keeps its four
    # values (`no_po` / `matched` / `mismatch` / `partial`), since `mismatch` is
    # owned by the AMOUNT control and an over-receipt with an in-tolerance
    # amount is a receiving discrepancy, not a billing one. It rides `issues`,
    # which the invoice modal renders verbatim.
    over_receipt: bool = False
    #: 3-way quantities, aggregated across every live goods receipt for the PO.
    #: Populated only when the PO carries line items AND at least one receipt
    #: carries received lines — the same condition that computes the partial /
    #: over-receipt legs. Exact `Decimal`, rendered to numbers by
    #: `to_json_dict()` like every other figure here.
    ordered_quantity: Decimal | None = None
    received_quantity: Decimal | None = None

    issues: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_json_dict(self) -> dict:
        """JSON-safe dict for the ``invoice.po_match`` JSONB column."""
        return _json_safe(asdict(self))


async def match_invoice_to_po(
    db: AsyncSession,
    invoice: Invoice,
    tolerance_pct: Decimal | float | int | str = Decimal("5.0"),
    require_inspection: bool = False,
) -> MatchResult:
    """Match an invoice against POs, goods receipts, and quality inspections.

    1. Find PO by po_number on the invoice
    2. Compare amounts (2-way match) — only when the invoice and the PO are in
       the SAME currency. Different codes are a ``mismatch`` with no variance;
       a PO that records no code is compared at face value and reported as
       ``currency_check="unknown"`` (see ``compare_currencies``).
    3. If a LIVE (non-cancelled) GR exists for the PO, verify quantities
       (3-way match) — short receipt → ``partial``, over-receipt →
       ``over_receipt`` + an issue
    4. If a quality inspection exists, fold its verdict in (4-way match)

    The PO / GR lookups are confined to the invoice's own subsidiary
    (``invoice.entity_id``) — derived here rather than threaded through each call
    site, the same way ``vendor_matching.match_and_link_vendor`` does it. Two
    subsidiaries that each number their POs from ``PO-1001`` and share a supplier
    would otherwise cross-match: the ``created_at DESC LIMIT 1`` pick could return
    the OTHER entity's newer ``PO-1001`` and read ``matched``, silently passing the
    amount control against a different subsidiary's order. Scoping is STRICT (no
    ``include_shared``) — unlike a vendor or a GL account, a purchase order belongs
    to exactly one entity; an invoice with a NULL ``entity_id`` (unstamped /
    pre-multi-entity) is unscoped, so single-entity tenants are unchanged.

    Args:
        tolerance_pct: Allowed variance percentage (default 5%)
        require_inspection: When True, a PO match with no inspection record is
            flagged (inspection_required) so the warnings layer can raise a
            quality_hold.

    Returns:
        MatchResult with match status, variances, and issues
    """
    result = MatchResult()

    if not invoice.po_number:
        result.status = "no_po"
        return result

    # Find PO by number, confined to the invoice's own entity (see docstring).
    entity_id = getattr(invoice, "entity_id", None)
    po_query = (
        select(PurchaseOrder)
        .where(
            PurchaseOrder.po_number == invoice.po_number,
        )
        .options(selectinload(PurchaseOrder.line_items))
    )
    po_query = apply_entity_scope(po_query, PurchaseOrder, entity_id)

    # If invoice has a vendor_id, also match on vendor
    if invoice.vendor_id:
        po_query = po_query.where(PurchaseOrder.vendor_id == invoice.vendor_id)

    # A po_number is not unique (it may be re-used across entities, or shared by
    # two vendors when the invoice carries no vendor_id to disambiguate), so cap
    # the lookup at a single deterministic row — newest first — rather than
    # `scalar_one_or_none()`, which raises MultipleResultsFound on >1 PO and
    # takes down the whole matcher / refresh_warnings pipeline.
    po_query = po_query.order_by(PurchaseOrder.created_at.desc()).limit(1)

    po_result = await db.execute(po_query)
    po = po_result.scalar_one_or_none()

    if not po:
        result.status = "no_po"
        result.issues.append(f"PO {invoice.po_number} not found")
        return result

    result.po_id = str(po.id)
    result.po_number = po.po_number
    result.po_total = _to_decimal(po.total)
    invoice_currency = po_currency_code(invoice.currency)
    result.po_currency = po_currency_code(po.currency)
    result.currency_check = compare_currencies(invoice_currency, result.po_currency)

    # 2-way match: invoice amount vs PO total. Every figure — the variance, the
    # variance %, and the tolerance gate — is exact Decimal end-to-end; money is
    # never cast to float. On a boundary amount (e.g. exactly 5.01% over a PO) an
    # IEEE-754 residual could otherwise flip `<= tolerance_pct` and auto-match an
    # out-of-tolerance invoice (or falsely flag a clean one). The result fields
    # stay Decimal in memory; `to_json_dict()` renders them for the JSONB column.
    invoice_amount = _to_decimal(invoice.amount)
    po_total = _to_decimal(po.total)
    tolerance = _to_decimal(tolerance_pct, Decimal("5.0"))
    result.match_type = "2-way"

    if result.currency_check == CURRENCY_DIFFERENT:
        # The currency guard. Before `purchase_orders.currency` existed this
        # compared the two figures as bare numbers, so EUR 1,000 against a USD
        # 1,000 order read `matched` at 0% — the amount control passed on two
        # quantities in different units. A mismatch in the currency IS a
        # mismatch in the amount; there is no variance to report.
        result.amount_variance = None
        result.amount_variance_pct = None
        result.within_tolerance = False
        result.status = "mismatch"
        result.issues.append(
            f"Currency mismatch: invoice in {invoice_currency}, PO in {result.po_currency} "
            "— amounts not compared"
        )
    else:
        variance = invoice_amount - po_total
        if po_total > 0:
            variance_pct = (variance / po_total) * Decimal(100)
        else:
            variance_pct = Decimal(100) if invoice_amount > 0 else Decimal(0)

        result.amount_variance = variance
        result.amount_variance_pct = variance_pct
        result.within_tolerance = abs(variance_pct) <= tolerance

        if not result.within_tolerance:
            result.status = "mismatch"
            result.issues.append(
                f"Amount mismatch: invoice {_labelled(invoice_amount, invoice_currency)} vs PO "
                f"{_labelled(po_total, result.po_currency)} ({variance_pct:+.1f}%)"
            )
        else:
            result.status = "matched"

    # 3-way match: check for goods receipts. A PO can have SEVERAL goods receipts
    # (the normal partial-delivery case — a PO filled by several shipments, each
    # a separate GR). Fetch them ALL (newest first) rather than a single row via
    # `scalar_one_or_none()`, which raised MultipleResultsFound on >1 GR and
    # crashed the matcher. The received-quantity comparison sums across every GR
    # (so a PO fully filled by two shipments is `matched`, not falsely `partial`);
    # the newest GR is the representative row for `gr_id` + the inspection leg.
    #
    # CANCELLED receipts are excluded. The sum ignored `GoodsReceipt.status`
    # entirely, so a delivery that was cancelled or reversed still counted as
    # goods in hand: it kept the receipt leg at 100%, held the invoice at
    # `matched` instead of `partial`, and — because the whole point of the
    # 3-way control is "don't pay for what didn't arrive" — cleared an invoice
    # for goods the business had explicitly recorded as not received. A PO whose
    # only receipt is cancelled now falls back to a 2-way match, which is the
    # honest answer: there is no receipt evidence.
    gr_query = (
        select(GoodsReceipt)
        .where(
            GoodsReceipt.po_id == po.id,
            func.lower(func.coalesce(GoodsReceipt.status, "")).notin_(CANCELLED_GR_STATUSES),
        )
        .options(selectinload(GoodsReceipt.line_items))
        .order_by(GoodsReceipt.created_at.desc())
    )
    # Same strict entity scope as the PO lookup — a receipt booked under a
    # different subsidiary must never satisfy this invoice's 3-way leg.
    gr_query = apply_entity_scope(gr_query, GoodsReceipt, entity_id)
    grs = (await db.execute(gr_query)).scalars().all()
    gr = grs[0] if grs else None

    if gr:
        result.match_type = "3-way"
        result.gr_id = str(gr.id)

        # Compare received vs ordered quantity, aggregating receipts across ALL
        # GRs for the PO. Only when the PO has line items and at least one GR
        # carries received lines (an empty GR header has nothing to verify).
        if po.line_items and any(g.line_items for g in grs):
            po_qty_total = sum((_to_decimal(li.quantity) for li in po.line_items), Decimal("0"))
            gr_qty_total = sum(
                (_to_decimal(li.quantity_received) for g in grs for li in g.line_items),
                Decimal("0"),
            )

            result.ordered_quantity = po_qty_total
            result.received_quantity = gr_qty_total

            if po_qty_total > 0 and gr_qty_total < po_qty_total:
                pct_received = (gr_qty_total / po_qty_total) * Decimal(100)
                result.issues.append(
                    f"Partial receipt: {pct_received:.0f}% of ordered quantity received"
                )
                if result.status == "matched":
                    result.status = "partial"
            elif po_qty_total > 0 and gr_qty_total > po_qty_total:
                # Over-receipt. The short side was flagged from the start; the
                # long side never was, so more units booked in than were ever
                # ordered passed the 3-way leg in silence — and an over-delivery
                # is how an invoice for quantities nobody authorised gets its
                # supporting receipt. Surfaced as an issue (the invoice modal
                # renders `issues` verbatim) plus the additive `over_receipt`
                # flag; `status` is left to the amount control, which is the
                # gate that decides whether the invoice is payable.
                result.over_receipt = True
                over_qty = gr_qty_total - po_qty_total
                result.issues.append(
                    f"Over-receipt: {format_quantity(gr_qty_total)} received against "
                    f"{format_quantity(po_qty_total)} ordered (+{format_quantity(over_qty)})"
                )

    # 4-way match: check for a quality inspection. Prefer one tied to the
    # goods receipt (the goods we actually received); fall back to one tied
    # to the PO. Take the most recent.
    #
    # The fallback has to run even when a GR EXISTS, which is the part that was
    # missing: `gr is not None` used to make the GR-scoped lookup the ONLY one,
    # so an inspection recorded against the PO alone became invisible the moment
    # any receipt was booked. `qms_sync` produces exactly that row whenever the
    # QMS knows the PO number but not the GR number (`_resolve_gr_id` returns
    # None and `gr_id` stays NULL), and `POST /api/inspections` accepts a
    # PO-only body too. The control then failed OPEN in the worst direction: a
    # `fail` verdict on rejected goods never reached the matcher, the invoice
    # read a clean `3-way` `matched`, and nothing raised the `quality_hold`
    # exception — the goods are refused and the supplier is paid anyway.
    #
    # The fallback is restricted to inspections with NO `gr_id` of their own —
    # a genuinely PO-level inspection. A row belonging to a *different*
    # shipment of the same PO also carries this `po_id`, and letting that stand
    # in for the receipt we're matching would substitute one masking bug for
    # another (shipment 2 passed, so shipment 1 reads inspected).
    async def _latest_inspection(*conditions):
        q = (
            select(QualityInspection)
            .where(*conditions)
            .order_by(QualityInspection.created_at.desc())
            .limit(1)
        )
        return (await db.execute(q)).scalar_one_or_none()

    inspection = None
    if gr is not None:
        inspection = await _latest_inspection(QualityInspection.gr_id == gr.id)
    if inspection is None:
        inspection = await _latest_inspection(
            QualityInspection.po_id == po.id, QualityInspection.gr_id.is_(None)
        )

    if inspection is not None:
        result.match_type = "4-way"
        result.inspection_id = str(inspection.id)
        result.inspection_result = inspection.result
        if inspection.accepted_quantity is not None:
            result.inspection_accepted_quantity = float(inspection.accepted_quantity)

        result.inspection_deviation_notes = inspection.deviation_notes

        if inspection.result == "fail":
            result.status = "mismatch"
            msg = "Failed quality inspection"
            if inspection.deviation_notes:
                msg = f"{msg}: {inspection.deviation_notes}"
            result.issues.append(msg)
        elif inspection.result == "partial":
            if result.status == "matched":
                result.status = "partial"
            accepted = (
                f"{result.inspection_accepted_quantity:g}"
                if result.inspection_accepted_quantity is not None
                else "part"
            )
            result.issues.append(f"Partial acceptance: {accepted} of ordered quantity accepted")
        # result == "pass" -> no status change
    elif require_inspection and po is not None:
        result.inspection_required = True
        result.issues.append("Quality inspection required but missing")

    # Decimal values here are rendered to numbers by `to_json_dict()` at the
    # JSONB boundary — the arithmetic that produced them was exact Decimal.
    result.details = {
        "match_type": result.match_type,
        "po_total": result.po_total,
        "invoice_amount": invoice_amount,
        "variance": result.amount_variance,
        "variance_pct": result.amount_variance_pct,
        "currency_check": result.currency_check,
        "tolerance_pct": tolerance,
        "within_tolerance": result.within_tolerance,
        "has_gr": gr is not None,
        "over_receipt": result.over_receipt,
        "has_inspection": inspection is not None,
        "inspection_result": result.inspection_result,
    }

    return result
