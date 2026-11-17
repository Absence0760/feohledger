"""PO matching service — match invoices against purchase orders and goods receipts.

Supports 2-way (invoice vs PO), 3-way (invoice vs PO vs GR), and 4-way
(invoice vs PO vs GR vs Quality Inspection) matching.
"""

import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.invoice import Invoice
from app.models.procurement import (
    CANCELLED_PO_STATUSES,
    UNISSUED_PO_STATUS,
    GoodsReceipt,
    PurchaseOrder,
    po_currency_code,
)
from app.models.quality_inspection import QualityInspection
from app.services.invoice_warning_catalog import po_match_issue
from app.services.matching_rules import resolve_match_rule
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


def _amount_mismatch_issue(
    invoice_amount: Decimal,
    invoice_currency: str | None,
    po_total: Decimal,
    po_currency: str | None,
    variance_pct: Decimal,
) -> dict:
    """The out-of-tolerance issue, in whichever of three sentences is honest.

    Only reached when the currencies are not DIFFERENT, so either both codes
    are known and equal, or one is missing. A figure is labelled only with a
    code that is its own: a PO recording no currency gets a bare figure (it used
    to print `$` on both figures whatever either was in), and an invoice whose
    own code is invalid takes the PO's label away too.
    """
    pct = f"{variance_pct:+.1f}"
    if invoice_currency is None:
        return po_match_issue(
            "amount_mismatch_currency_unknown",
            invoiceAmount=f"{invoice_amount:.2f}",
            poTotal=f"{po_total:.2f}",
            variancePct=pct,
        )
    if po_currency is None:
        return po_match_issue(
            "amount_mismatch_po_currency_unknown",
            invoiceAmount=invoice_amount,
            poTotal=f"{po_total:.2f}",
            currency=invoice_currency,
            variancePct=pct,
        )
    return po_match_issue(
        "amount_mismatch",
        invoiceAmount=invoice_amount,
        poTotal=po_total,
        currency=invoice_currency,
        variancePct=pct,
    )


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
    #: Set when the PO's last ERP sync could not state its total exactly
    #: (`PurchaseOrder.total_unverifiable_reason`, an
    #: `erp_adapters.base.PoSkipReason` value). The amounts were then NOT
    #: compared: `status` is `mismatch` and the variance fields are `None`.
    po_total_unverifiable_reason: str | None = None
    #: The PO is cancelled or voided (`CANCELLED_PO_STATUSES`): nothing was
    #: compared and the invoice is held.
    po_cancelled: bool = False
    #: The PO is one the ERP has not approved or issued yet
    #: (`UNISSUED_PO_STATUS`, decisions §394): nothing was compared and the
    #: invoice is held, as for a cancelled one.
    po_not_issued: bool = False
    gr_id: str | None = None
    #: A multi-PO split's legs, when the match read the invoice as one
    #: (``Invoice.split_po_ids`` — see ``_split_legs``); empty for a single-PO
    #: match, whose PO is ``po_id``. ``po_id`` and ``gr_id`` stay ``None`` /
    #: the newest receipt across the legs on a split; every money and quantity
    #: figure is the legs' combined one.
    po_ids: list[str] = field(default_factory=list)

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
    #: Every inspection the match read: the one ``inspection_id`` names on a
    #: single PO, one per inspected leg on a split. A hold the 4-way leg
    #: raised may close only when each of them may lift it
    #: (``invoice_warnings._inspection_clears_hold``).
    inspection_ids: list[str] = field(default_factory=list)

    # 3-way: received quantity EXCEEDS what was ordered. Additive to the
    # persisted `invoice.po_match` shape — `status` deliberately keeps its four
    # values (`no_po` / `matched` / `mismatch` / `partial`), since `mismatch` is
    # owned by the AMOUNT control and an over-receipt with an in-tolerance
    # amount is a receiving discrepancy, not a billing one. It rides `issues`,
    # which the invoice modal renders in the PO-match panel.
    over_receipt: bool = False
    #: 3-way quantities, aggregated across every live goods receipt for the PO.
    #: Populated only when the PO carries line items AND at least one receipt
    #: carries received lines — the same condition that computes the partial /
    #: over-receipt legs. Exact `Decimal`, rendered to numbers by
    #: `to_json_dict()` like every other figure here.
    ordered_quantity: Decimal | None = None
    received_quantity: Decimal | None = None
    #: 3-way, SHORT receipt only: the slice of ``po_total`` the received units
    #: cover (``po_total × received / ordered``), in the invoice's currency.
    #: ``None`` when nothing is short, or the two currencies differ.
    received_value: Decimal | None = None
    #: The invoice bills MORE than ``received_value`` by more than the match
    #: tolerance — paying it would pay for goods that have not arrived. This,
    #: not a short receipt as such, is what opens a payment-blocking
    #: ``po_mismatch``: an invoice for just the shipment that did arrive (a
    #: split or blanket PO billed delivery by delivery) is partial BILLING, and
    #: never blocks (``invoice_warnings._refresh_po_match``).
    billed_beyond_receipt: bool = False

    #: One `{code, params, message}` entry per finding, built by
    #: `invoice_warning_catalog.po_match_issue` — the client localizes on
    #: `code` and renders `message` only as the fallback. A match persisted
    #: before the catalogue covered issues holds bare English strings here, and
    #: nothing backfills them (the next `refresh_warnings` re-derives the row),
    #: so every reader of `invoice.po_match.issues` accepts both shapes.
    issues: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_json_dict(self) -> dict:
        """JSON-safe dict for the ``invoice.po_match`` JSONB column."""
        return _json_safe(asdict(self))

    def matched_po_ids(self) -> list[str]:
        """Every PO the match read receipts and inspections on: a split's
        legs, else the single ``po_id`` (empty when no PO was found)."""
        if self.po_ids:
            return list(self.po_ids)
        return [self.po_id] if self.po_id else []


async def match_invoice_to_po(
    db: AsyncSession,
    invoice: Invoice,
    tolerance_pct: Decimal | float | int | str = Decimal("5.0"),
    require_inspection: bool = False,
) -> MatchResult:
    """Match an invoice against POs, goods receipts, and quality inspections.

    1. Find PO by po_number on the invoice — or, for a multi-PO split whose
       reference still names the legs in ``split_po_ids``, match those legs
       combined (``_split_legs`` / ``_match_split``)
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

    tolerance = _to_decimal(tolerance_pct, Decimal("5.0"))

    # A multi-PO split (decisions §433): its combined reference ("PO-A,PO-B")
    # names no single PO, so the lookup below would read `no_po` and re-raise
    # the hold the split agent resolved, on every later edit.
    legs = await _split_legs(db, invoice)
    if legs is not None:
        return await _match_split(
            db, invoice, legs, tolerance=tolerance, require_inspection=require_inspection
        )

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
    # `id` breaks a `created_at` tie (transaction time: rows a sync wrote in one
    # transaction share it), so the pick never rests on physical row order.
    po_query = po_query.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc()).limit(1)

    po_result = await db.execute(po_query)
    po = po_result.scalar_one_or_none()

    if not po:
        result.status = "no_po"
        result.issues.append(po_match_issue("po_not_found", poNumber=invoice.po_number))
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
    result.match_type = "2-way"
    result.po_total_unverifiable_reason = po.total_unverifiable_reason or None
    _amount_leg(
        result,
        cancelled=po.status in CANCELLED_PO_STATUSES,
        not_issued=po.status == UNISSUED_PO_STATUS,
        invoice_amount=invoice_amount,
        invoice_currency=invoice_currency,
        po_total=po_total,
        tolerance=tolerance,
    )

    # 3-way match: check for goods receipts. A PO can have SEVERAL goods receipts
    # (the normal partial-delivery case — a PO filled by several shipments, each
    # a separate GR). Fetch them ALL (newest first) rather than a single row via
    # `scalar_one_or_none()`, which raised MultipleResultsFound on >1 GR and
    # crashed the matcher. The received-quantity comparison sums across every GR
    # (so a PO fully filled by two shipments is `matched`, not falsely `partial`);
    # the newest GR is the representative row for `gr_id` + the inspection leg.
    # Which receipts count is `_live_receipts`.
    grs, all_receipts_cancelled = await _live_receipts(db, po, entity_id)
    gr = grs[0] if grs else None

    if gr or all_receipts_cancelled:
        result.match_type = "3-way"
        result.gr_id = str(gr.id) if gr else None

        # Compare received vs ordered quantity, aggregating receipts across ALL
        # live GRs for the PO. Only when the PO has line items and at least one
        # GR carries received lines (an empty GR header has nothing to verify)
        # — or every receipt was cancelled, which is zero received.
        quantities = _receipt_quantities(po, grs, all_receipts_cancelled)
        if quantities is not None:
            po_qty_total, gr_qty_total = quantities
            result.ordered_quantity = po_qty_total
            result.received_quantity = gr_qty_total

            if po_qty_total > 0 and gr_qty_total < po_qty_total:
                result.issues.append(_partial_receipt_issue(po_qty_total, gr_qty_total))
                if result.status == "matched":
                    result.status = "partial"
                # Does the invoice bill beyond what arrived? Same exact-Decimal
                # tolerance as the amount leg, measured against the received
                # slice of the PO rather than its whole total. Skipped when the
                # currencies differ or the PO's total is unverifiable — each is
                # already a mismatch with no figure to measure against.
                if (
                    result.currency_check != CURRENCY_DIFFERENT
                    and not result.po_total_unverifiable_reason
                ):
                    received_value = po_total * gr_qty_total / po_qty_total
                    result.received_value = received_value
                    result.billed_beyond_receipt = _bills_beyond(
                        invoice_amount, received_value, tolerance
                    )
            elif po_qty_total > 0 and gr_qty_total > po_qty_total:
                # Over-receipt. The short side was flagged from the start; the
                # long side never was, so more units booked in than were ever
                # ordered passed the 3-way leg in silence — and an over-delivery
                # is how an invoice for quantities nobody authorised gets its
                # supporting receipt. Surfaced as an issue (the invoice modal
                # renders `issues` in its PO-match panel) plus the additive `over_receipt`
                # flag; `status` is left to the amount control, which is the
                # gate that decides whether the invoice is payable.
                result.over_receipt = True
                result.issues.append(_over_receipt_issue(po_qty_total, gr_qty_total))

    # 4-way match: the inspection of these goods (`_latest_inspection_for`).
    inspection = await _latest_inspection_for(db, po, gr)

    if inspection is not None:
        result.match_type = "4-way"
        result.inspection_ids = [str(inspection.id)]
        _record_inspection(result, inspection)
        _inspection_verdict(result, inspection)
    elif require_inspection and po is not None:
        result.inspection_required = True
        result.issues.append(po_match_issue("inspection_required_missing"))

    # Decimal values here are rendered to numbers by `to_json_dict()` at the
    # JSONB boundary — the arithmetic that produced them was exact Decimal.
    result.details = _match_details(
        result,
        invoice_amount=invoice_amount,
        tolerance=tolerance,
        has_gr=gr is not None,
        all_receipts_cancelled=all_receipts_cancelled,
        has_inspection=inspection is not None,
    )

    return result


def _amount_leg(
    result: MatchResult,
    *,
    cancelled: bool,
    not_issued: bool,
    invoice_amount: Decimal,
    invoice_currency: str | None,
    po_total: Decimal,
    tolerance: Decimal,
) -> None:
    """The 2-way leg: set ``result.status`` and its amount fields from the PO
    (or a split's legs, combined). ``result.currency_check``,
    ``po_currency`` and ``po_total_unverifiable_reason`` are already set."""
    if cancelled:
        # The order was called off in the ERP (or here): it authorizes nothing,
        # so no invoice passes the amount control against it, whatever the
        # figures say. Held for a person, like the currency guard.
        result.amount_variance = None
        result.amount_variance_pct = None
        result.within_tolerance = False
        result.status = "mismatch"
        result.po_cancelled = True
        result.issues.append(po_match_issue("po_cancelled"))
    elif not_issued:
        # Nobody in the ERP has approved or issued the order yet (a draft, or
        # one awaiting approval): it authorizes nothing, so matching against it
        # would pass the amount control on a draft (decisions §394). Held for
        # a person like a cancelled PO, with no variance — a 0 would read as a
        # perfect match — until the ERP issues it.
        result.amount_variance = None
        result.amount_variance_pct = None
        result.within_tolerance = False
        result.status = "mismatch"
        result.po_not_issued = True
        result.issues.append(po_match_issue("po_not_issued"))
    elif result.po_total_unverifiable_reason:
        # The ERP still has this PO, but its last sync could not state the
        # total exactly (decisions §323), so `po.total` is a figure from an
        # earlier sync the ERP no longer vouches for. Comparing against it
        # would pass or fail the amount control on a number nobody stands
        # behind — the match refuses, like the currency guard below, and the
        # warnings layer holds the invoice for a person. Nor is the figure
        # stored on `invoice.po_match`, which every API reader of the invoice
        # gets: a display path sends null for it (§323).
        result.po_total = None
        result.amount_variance = None
        result.amount_variance_pct = None
        result.within_tolerance = False
        result.status = "mismatch"
        result.issues.append(po_match_issue("po_total_unverifiable"))
    elif result.currency_check == CURRENCY_DIFFERENT:
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
            po_match_issue(
                "currency_mismatch",
                invoiceCurrency=invoice_currency,
                poCurrency=result.po_currency,
            )
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
                _amount_mismatch_issue(
                    invoice_amount, invoice_currency, po_total, result.po_currency, variance_pct
                )
            )
        else:
            result.status = "matched"


def _bills_beyond(invoice_amount: Decimal, received_value: Decimal, tolerance: Decimal) -> bool:
    """Whether the invoice asks for more than ``received_value`` by more than
    the match tolerance — exact Decimal, like the amount leg."""
    if received_value > 0:
        over_pct = (invoice_amount - received_value) / received_value * 100
        return over_pct > tolerance
    return invoice_amount > 0


def _partial_receipt_issue(ordered: Decimal, received: Decimal) -> dict:
    pct_received = (received / ordered) * Decimal(100)
    return po_match_issue("partial_receipt", receivedPct=f"{pct_received:.0f}")


def _over_receipt_issue(ordered: Decimal, received: Decimal) -> dict:
    return po_match_issue(
        "over_receipt",
        receivedQuantity=format_quantity(received),
        orderedQuantity=format_quantity(ordered),
        excessQuantity=format_quantity(received - ordered),
    )


async def _live_receipts(
    db: AsyncSession, po: PurchaseOrder, entity_id
) -> tuple[list[GoodsReceipt], bool]:
    """The PO's live goods receipts (newest first, the representative row
    first), and whether it has receipts that are ALL cancelled.

    CANCELLED receipts are excluded. The sum ignored `GoodsReceipt.status`
    entirely, so a delivery that was cancelled or reversed still counted as
    goods in hand: it kept the receipt leg at 100%, held the invoice at
    `matched` instead of `partial`, and — because the whole point of the
    3-way control is "don't pay for what didn't arrive" — cleared an invoice
    for goods the business had explicitly recorded as not received.

    A PO whose receipts are ALL cancelled is a 3-way match with nothing
    received — not a 2-way match. It once fell back to 2-way on the reading
    "there is no receipt evidence", but a cancelled receipt IS evidence: the
    business recorded that the goods did not arrive. Reading it as absence let
    cancelling the only receipt turn an over-billed invoice into a clean 2-way
    `matched`, which closed its payment hold — and receipts can be cancelled
    in the app, by the person who keyed the invoice (decisions §262).

    Same strict entity scope as the PO lookup — a receipt booked under a
    different subsidiary must never satisfy this invoice's 3-way leg.
    """
    gr_query = (
        select(GoodsReceipt)
        .where(
            GoodsReceipt.po_id == po.id,
            func.lower(func.coalesce(GoodsReceipt.status, "")).notin_(CANCELLED_GR_STATUSES),
        )
        .options(selectinload(GoodsReceipt.line_items))
        .order_by(GoodsReceipt.created_at.desc(), GoodsReceipt.id.desc())
    )
    gr_query = apply_entity_scope(gr_query, GoodsReceipt, entity_id)
    grs = list((await db.execute(gr_query)).scalars().all())
    if grs:
        return grs, False
    any_receipt = apply_entity_scope(
        select(GoodsReceipt.id).where(GoodsReceipt.po_id == po.id).limit(1),
        GoodsReceipt,
        entity_id,
    )
    return grs, (await db.execute(any_receipt)).scalar_one_or_none() is not None


def _receipt_quantities(
    po: PurchaseOrder, grs: list[GoodsReceipt], all_cancelled: bool
) -> tuple[Decimal, Decimal] | None:
    """``(ordered, received)`` for one PO, received summed across every live
    receipt — or ``None`` when there is nothing to compare: the PO has no
    lines, or no receipt carries received lines (an empty GR header has
    nothing to verify). Every receipt cancelled is zero received."""
    if not po.line_items or not (all_cancelled or any(g.line_items for g in grs)):
        return None
    ordered = sum((_to_decimal(li.quantity) for li in po.line_items), Decimal("0"))
    received = sum(
        (_to_decimal(li.quantity_received) for g in grs for li in g.line_items),
        Decimal("0"),
    )
    return ordered, received


async def _latest_inspection_for(
    db: AsyncSession, po: PurchaseOrder, gr: GoodsReceipt | None
) -> QualityInspection | None:
    """The quality inspection of this PO's goods. Prefer one tied to the
    goods receipt (the goods we actually received); fall back to one tied to
    the PO. Take the most recent.

    The fallback has to run even when a GR EXISTS, which is the part that was
    missing: `gr is not None` used to make the GR-scoped lookup the ONLY one,
    so an inspection recorded against the PO alone became invisible the moment
    any receipt was booked. `qms_sync` produces exactly that row whenever the
    QMS knows the PO number but not the GR number (`_resolve_gr_id` returns
    None and `gr_id` stays NULL), and `POST /api/inspections` accepts a
    PO-only body too. The control then failed OPEN in the worst direction: a
    `fail` verdict on rejected goods never reached the matcher, the invoice
    read a clean `3-way` `matched`, and nothing raised the `quality_hold`
    exception — the goods are refused and the supplier is paid anyway.

    The fallback is restricted to inspections with NO `gr_id` of their own —
    a genuinely PO-level inspection. A row belonging to a *different*
    shipment of the same PO also carries this `po_id`, and letting that stand
    in for the receipt we're matching would substitute one masking bug for
    another (shipment 2 passed, so shipment 1 reads inspected).
    """

    async def _latest(*conditions):
        q = (
            select(QualityInspection)
            .where(*conditions)
            .order_by(QualityInspection.created_at.desc(), QualityInspection.id.desc())
            .limit(1)
        )
        return (await db.execute(q)).scalar_one_or_none()

    inspection = None
    if gr is not None:
        inspection = await _latest(QualityInspection.gr_id == gr.id)
    if inspection is None:
        inspection = await _latest(
            QualityInspection.po_id == po.id, QualityInspection.gr_id.is_(None)
        )
    return inspection


def _record_inspection(result: MatchResult, inspection: QualityInspection) -> None:
    """Carry ``inspection`` as the match's representative verdict."""
    result.inspection_id = str(inspection.id)
    result.inspection_result = inspection.result
    result.inspection_accepted_quantity = (
        float(inspection.accepted_quantity) if inspection.accepted_quantity is not None else None
    )
    result.inspection_deviation_notes = inspection.deviation_notes


def _inspection_verdict(result: MatchResult, inspection: QualityInspection) -> None:
    """Fold one inspection's verdict into ``status`` and ``issues``: ``fail``
    is a mismatch, ``partial`` a partial match, ``pass`` changes nothing."""
    if inspection.result == "fail":
        result.status = "mismatch"
        if inspection.deviation_notes:
            issue = po_match_issue("inspection_failed_notes", notes=inspection.deviation_notes)
        else:
            issue = po_match_issue("inspection_failed")
        result.issues.append(issue)
    elif inspection.result == "partial":
        if result.status == "matched":
            result.status = "partial"
        if inspection.accepted_quantity is not None:
            issue = po_match_issue(
                "partial_acceptance",
                acceptedQuantity=f"{float(inspection.accepted_quantity):g}",
            )
        else:
            issue = po_match_issue("partial_acceptance_unquantified")
        result.issues.append(issue)


def _match_details(
    result: MatchResult,
    *,
    invoice_amount: Decimal,
    tolerance: Decimal,
    has_gr: bool,
    all_receipts_cancelled: bool,
    has_inspection: bool,
) -> dict:
    return {
        "match_type": result.match_type,
        "po_total": result.po_total,
        "invoice_amount": invoice_amount,
        "variance": result.amount_variance,
        "variance_pct": result.amount_variance_pct,
        "currency_check": result.currency_check,
        "po_total_unverifiable_reason": result.po_total_unverifiable_reason,
        "po_cancelled": result.po_cancelled,
        "po_not_issued": result.po_not_issued,
        "tolerance_pct": tolerance,
        "within_tolerance": result.within_tolerance,
        "has_gr": has_gr,
        "all_receipts_cancelled": all_receipts_cancelled,
        "over_receipt": result.over_receipt,
        "billed_beyond_receipt": result.billed_beyond_receipt,
        "has_inspection": has_inspection,
        "inspection_result": result.inspection_result,
    }


# ---------------------------------------------------------------------------
# Multi-PO split
# ---------------------------------------------------------------------------


async def _split_legs(db: AsyncSession, invoice: Invoice) -> list[PurchaseOrder] | None:
    """The POs a multi-PO split linked this invoice to, when its reference
    still names them — else ``None`` and the single-PO lookup runs.

    The split agent (`multi_po_split_v1`) writes a combined ``po_number``
    ("PO-A,PO-B") and records the legs' ids in ``Invoice.split_po_ids``, which
    no refresh rewrites (decisions §433). The legs are read from that column,
    never re-derived from the numbers, and only while the reference agrees
    with it: every number in ``po_number`` must resolve to a PO in the set,
    under the single-PO lookup's own filters (the invoice's entity, strictly;
    its vendor when it has one; the newest row per number). A reference a
    person edited away from the split — one PO, or a number the set does not
    hold — is matched as written, so it resolves or reads ``no_po`` as any
    other reference does. The column is append-only and may over-name (a
    re-run split, or a backfill that took every same-numbered PO), so a PO in
    the set that the reference does not name is not a leg.
    """
    split = getattr(invoice, "split_po_ids", None)
    if not isinstance(split, list) or not split or not invoice.po_number:
        return None
    numbers = list(dict.fromkeys(n.strip() for n in invoice.po_number.split(",") if n.strip()))
    if len(numbers) < 2:
        return None
    ids: set[uuid.UUID] = set()
    for value in split:
        try:
            ids.add(uuid.UUID(str(value)))
        except ValueError:
            continue
    if not ids:
        return None

    query = (
        select(PurchaseOrder)
        .where(PurchaseOrder.id.in_(ids), PurchaseOrder.po_number.in_(numbers))
        .options(selectinload(PurchaseOrder.line_items))
        .order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc())
    )
    query = apply_entity_scope(query, PurchaseOrder, getattr(invoice, "entity_id", None))
    if invoice.vendor_id:
        query = query.where(PurchaseOrder.vendor_id == invoice.vendor_id)
    by_number: dict[str, PurchaseOrder] = {}
    for po in (await db.execute(query)).scalars().all():
        by_number.setdefault(po.po_number, po)
    if set(by_number) != set(numbers):
        return None
    return [by_number[n] for n in numbers]


async def _match_split(
    db: AsyncSession,
    invoice: Invoice,
    legs: list[PurchaseOrder],
    *,
    tolerance: Decimal,
    require_inspection: bool,
) -> MatchResult:
    """Match a multi-PO split against its legs (``_split_legs``).

    * **2-way** on the legs' combined total — the comparison the split agent
      linked on. Any leg cancelled, unissued or unverifiable holds the whole
      invoice, as that PO would on its own; any leg in another currency is a
      currency mismatch; a leg recording no currency makes the comparison
      ``unknown`` (face value), as for one PO.
    * **3-way and 4-way per leg**, then combined. Quantities are never pooled
      across legs before they are judged: an over-receipt on one leg would
      otherwise hide a short receipt on another. A short leg counts for the
      share of its total its receipts cover, every other leg for its whole
      total (a leg with no receipt evidence is a 2-way leg, as one PO with no
      receipt is), and the invoice bills beyond receipt when it asks for more
      than that sum. Every leg's inspection is read: any ``fail`` is the
      representative verdict, then a required inspection missing on any leg,
      then a ``partial``.

    ``ordered_quantity`` / ``received_quantity`` are the legs' sums, except
    when one leg is short and another over: no pair of totals states both, so
    they are ``None`` and the per-leg figures are in ``issues`` and
    ``details["legs"]``.
    """
    result = MatchResult()
    entity_id = getattr(invoice, "entity_id", None)
    invoice_amount = _to_decimal(invoice.amount)
    invoice_currency = po_currency_code(invoice.currency)

    result.po_ids = [str(po.id) for po in legs]
    result.po_number = invoice.po_number
    result.match_type = "2-way"
    totals = [_to_decimal(po.total) for po in legs]
    po_total = sum(totals, Decimal("0"))
    result.po_total = po_total

    codes = [po_currency_code(po.currency) for po in legs]
    checks = [compare_currencies(invoice_currency, code) for code in codes]
    if CURRENCY_DIFFERENT in checks:
        result.currency_check = CURRENCY_DIFFERENT
        result.po_currency = codes[checks.index(CURRENCY_DIFFERENT)]
    else:
        result.currency_check = (
            CURRENCY_SAME if all(c == CURRENCY_SAME for c in checks) else CURRENCY_UNKNOWN
        )
        result.po_currency = codes[0] if len(set(codes)) == 1 else None
    result.po_total_unverifiable_reason = next(
        (po.total_unverifiable_reason for po in legs if po.total_unverifiable_reason), None
    )
    _amount_leg(
        result,
        cancelled=any(po.status in CANCELLED_PO_STATUSES for po in legs),
        not_issued=any(po.status == UNISSUED_PO_STATUS for po in legs),
        invoice_amount=invoice_amount,
        invoice_currency=invoice_currency,
        po_total=po_total,
        tolerance=tolerance,
    )

    newest_gr: GoodsReceipt | None = None
    any_receipt_evidence = False
    any_all_cancelled = False
    quantified = False
    short = over = False
    ordered_sum = received_sum = Decimal("0")
    covered = Decimal("0")
    inspected: list[QualityInspection] = []
    uninspected = 0
    leg_details: list[dict] = []

    for po, total in zip(legs, totals, strict=True):
        grs, all_cancelled = await _live_receipts(db, po, entity_id)
        gr = grs[0] if grs else None
        any_all_cancelled = any_all_cancelled or all_cancelled
        leg_value = total
        quantities = None
        if gr or all_cancelled:
            any_receipt_evidence = True
            if gr is not None and (newest_gr is None or gr.created_at > newest_gr.created_at):
                newest_gr = gr
            quantities = _receipt_quantities(po, grs, all_cancelled)
        if quantities is not None:
            ordered, received = quantities
            quantified = True
            ordered_sum += ordered
            received_sum += received
            if ordered > 0 and received < ordered:
                short = True
                result.issues.append(_partial_receipt_issue(ordered, received))
                leg_value = total * received / ordered
            elif ordered > 0 and received > ordered:
                over = True
                result.issues.append(_over_receipt_issue(ordered, received))
        covered += leg_value

        inspection = await _latest_inspection_for(db, po, gr)
        if inspection is None:
            uninspected += 1
        else:
            inspected.append(inspection)
            _inspection_verdict(result, inspection)

        leg_details.append(
            {
                "po_id": str(po.id),
                "po_number": po.po_number,
                "po_total": total,
                "po_currency": po_currency_code(po.currency),
                "has_gr": gr is not None,
                "all_receipts_cancelled": all_cancelled,
                "ordered_quantity": quantities[0] if quantities else None,
                "received_quantity": quantities[1] if quantities else None,
                "inspection_result": inspection.result if inspection is not None else None,
            }
        )

    if any_receipt_evidence:
        result.match_type = "3-way"
        result.gr_id = str(newest_gr.id) if newest_gr is not None else None
    if quantified and not (short and over):
        result.ordered_quantity = ordered_sum
        result.received_quantity = received_sum
    result.over_receipt = over
    if short:
        if result.status == "matched":
            result.status = "partial"
        if result.currency_check != CURRENCY_DIFFERENT and not result.po_total_unverifiable_reason:
            result.received_value = covered
            result.billed_beyond_receipt = _bills_beyond(invoice_amount, covered, tolerance)

    if inspected:
        result.match_type = "4-way"
        result.inspection_ids = [str(i.id) for i in inspected]
    missing_required = require_inspection and uninspected > 0
    if missing_required:
        result.inspection_required = True
        result.issues.append(po_match_issue("inspection_required_missing"))
    failed = [i for i in inspected if i.result == "fail"]
    partial = [i for i in inspected if i.result == "partial"]
    # The representative verdict the warnings layer raises on: a failure
    # first; then a required inspection missing on any leg, which must read as
    # missing (`inspection_result` None) however the other legs passed.
    if failed:
        _record_inspection(result, failed[0])
    elif not missing_required and (partial or inspected):
        _record_inspection(result, (partial or inspected)[0])

    result.details = _match_details(
        result,
        invoice_amount=invoice_amount,
        tolerance=tolerance,
        has_gr=newest_gr is not None,
        all_receipts_cancelled=any_all_cancelled,
        has_inspection=bool(inspected),
    )
    result.details["legs"] = leg_details
    return result


async def match_invoice_under_org_rules(
    db: AsyncSession, invoice: Invoice, org_settings: dict | None
) -> MatchResult:
    """:func:`match_invoice_to_po` under the org's OWN match rule for this
    invoice (per-vendor / per-commodity tolerance and ``require_inspection``,
    ``matching_rules.resolve_match_rule``).

    The one way anything that DECIDES on a match should call the matcher.
    ``po_mismatch`` blocks payment, so a caller judging "is this invoice clean"
    at the platform-default 5 % while the org's rule for the vendor is 1 % can
    clear an over-billing finding the org's own refresh would raise — which is
    how an exception agent's post-check used to read ``matched`` on an invoice
    still over-billed against its newly linked PO.
    """
    rule = resolve_match_rule(
        org_settings, vendor_id=invoice.vendor_id, gl_account=invoice.gl_account
    )
    return await match_invoice_to_po(
        db,
        invoice,
        tolerance_pct=rule.tolerance_pct,
        require_inspection=rule.require_inspection,
    )
