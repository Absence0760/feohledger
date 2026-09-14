"""Post-extraction self-correction — pure-Python invariant checks.

Verifies arithmetic consistency, date ordering, and line-item math after
extraction.  Violations lower the confidence on suspect fields and add
warnings to the invoice so the reviewer knows what to double-check.

No LLM call — this is a cheap sanity pass that catches the most common
extraction mistakes (transposed digits, missed tax line, swapped dates).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.services.decimal_convention import AmountConvention
from app.services.extraction_adapters.base import ExtractionResult
from app.services.invoice_warning_catalog import warning

# Tolerances for "approximately equal" checks.
TOTAL_TOLERANCE = Decimal("0.02")  # 2 %
LINE_ITEM_TOLERANCE = Decimal("0.01")  # 1 %

# How much to dock confidence on a field implicated in a violation.
CONFIDENCE_PENALTY = 0.2


@dataclass
class SelfCorrectionReport:
    violations: list[dict] = field(default_factory=list)
    confidence_penalties: dict[str, float] = field(default_factory=dict)

    @property
    def corrected(self) -> bool:
        return len(self.violations) > 0


def _to_decimal(val: str | None, convention: AmountConvention | None = None) -> Decimal | None:
    """Read one of the model's money strings the SAME way the invoice row did.

    Delegates to ``extraction._clean_decimal``, which owns the sentinel
    filtering, the currency/whitespace/percent stripping and — the part that
    matters here — the decimal-convention rules. This used to strip every comma
    itself, so on a European document it disagreed with the values actually
    written to the invoice: ``1.234,56`` was unparseable (the arithmetic check
    silently skipped) while ``850,00`` read as ``85000``. A checker that reads
    the numbers differently from the writer isn't checking anything.
    """
    from app.services.extraction import _clean_decimal

    return _clean_decimal(val, convention)


def _parse_date(val: str | None) -> date | None:
    if val is None:
        return None
    try:
        return date.fromisoformat(str(val).strip())
    except (ValueError, TypeError):
        return None


def _penalize(
    result: ExtractionResult,
    report: SelfCorrectionReport,
    field_name: str,
) -> None:
    """Lower confidence on *field_name* and record the penalty."""
    fld = getattr(result, field_name, None)
    if fld is None:
        return
    old = fld.confidence
    fld.confidence = max(0.0, fld.confidence - CONFIDENCE_PENALTY)
    if old != fld.confidence:
        report.confidence_penalties[field_name] = round(fld.confidence - old, 2)


def _approx_eq(a: Decimal, b: Decimal, tolerance: Decimal) -> bool:
    """True when *a* and *b* are within *tolerance* (relative to *b*)."""
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) <= tolerance


# ------------------------------------------------------------------
# Individual invariant checks
# ------------------------------------------------------------------


def _check_total_reconciliation(
    result: ExtractionResult,
    report: SelfCorrectionReport,
    convention: AmountConvention | None = None,
) -> None:
    """subtotal + tax + shipping − discount ≈ amount."""
    amount = _to_decimal(result.amount.value, convention)
    if amount is None or amount == 0:
        return  # nothing to reconcile

    subtotal = _to_decimal(result.subtotal.value, convention) or Decimal(0)
    tax = _to_decimal(result.tax_amount.value, convention) or Decimal(0)
    shipping = _to_decimal(result.shipping_amount.value, convention) or Decimal(0)
    discount = _to_decimal(result.discount_amount.value, convention) or Decimal(0)

    # If subtotal is missing, we can't reconcile — skip silently.
    if result.subtotal.value is None:
        return

    expected = subtotal + tax + shipping - discount
    if not _approx_eq(expected, amount, TOTAL_TOLERANCE):
        report.violations.append(
            {
                # The violation IS the warning payload — `run_extraction` forwards
                # it onto `invoice.warnings` verbatim, and it is ALSO persisted into
                # the `priors_metadata` JSONB, so every value has to be
                # JSON-serialisable. Building it through the catalogue is what makes
                # that true: `warning()` coerces each param by its declared kind, so
                # no raw `Decimal` or `date` reaches the column.
                **warning(
                    "self_correction_total_reconciliation",
                    "warning",
                    subtotal=subtotal,
                    tax=tax,
                    shipping=shipping,
                    discount=discount,
                    expected=expected,
                    amount=amount,
                ),
                "check": "total_reconciliation",
                "fields_affected": [
                    "amount",
                    "subtotal",
                    "tax_amount",
                    "shipping_amount",
                    "discount_amount",
                ],
            }
        )
        for f in ("amount", "subtotal", "tax_amount"):
            _penalize(result, report, f)


def _check_date_ordering(
    result: ExtractionResult,
    report: SelfCorrectionReport,
    convention: AmountConvention | None = None,
) -> None:
    """due_date should be >= invoice_date."""
    inv_date = _parse_date(result.invoice_date.value)
    due = _parse_date(result.due_date.value)
    if inv_date is None or due is None:
        return

    if due < inv_date:
        report.violations.append(
            {
                **warning(
                    "self_correction_date_ordering",
                    "warning",
                    dueDate=due,
                    invoiceDate=inv_date,
                ),
                "check": "date_ordering",
                "fields_affected": ["due_date", "invoice_date"],
            }
        )
        _penalize(result, report, "due_date")


def _check_line_items_sum(
    result: ExtractionResult,
    report: SelfCorrectionReport,
    convention: AmountConvention | None = None,
) -> None:
    """sum(line_items.total) ≈ amount."""
    if not result.line_items:
        return
    amount = _to_decimal(result.amount.value, convention)
    if amount is None or amount == 0:
        return

    li_sum = Decimal(0)
    for li in result.line_items:
        t = _to_decimal(li.total.value, convention)
        if t is not None:
            li_sum += t

    if li_sum == 0:
        return  # no totals on line items — skip

    if not _approx_eq(li_sum, amount, TOTAL_TOLERANCE):
        report.violations.append(
            {
                **warning(
                    "self_correction_line_items_sum",
                    "warning",
                    lineItemsTotal=li_sum,
                    amount=amount,
                ),
                "check": "line_items_sum",
                "fields_affected": ["amount"],
            }
        )
        _penalize(result, report, "amount")


def _check_line_item_math(
    result: ExtractionResult,
    report: SelfCorrectionReport,
    convention: AmountConvention | None = None,
) -> None:
    """quantity × unit_price ≈ total for each line item."""
    for i, li in enumerate(result.line_items):
        qty = _to_decimal(li.quantity.value, convention)
        price = _to_decimal(li.unit_price.value, convention)
        total = _to_decimal(li.total.value, convention)
        if qty is None or price is None or total is None or total == 0:
            continue

        expected = qty * price
        if not _approx_eq(expected, total, LINE_ITEM_TOLERANCE):
            report.violations.append(
                {
                    **warning(
                        "self_correction_line_item_math",
                        "info",
                        lineNumber=i + 1,
                        quantity=qty,
                        unitPrice=price,
                        expected=expected,
                        total=total,
                    ),
                    "check": "line_item_math",
                    "fields_affected": [f"line_items[{i}].total"],
                }
            )
            li.total.confidence = max(0.0, li.total.confidence - CONFIDENCE_PENALTY)
            report.confidence_penalties[f"line_items[{i}].total"] = round(-CONFIDENCE_PENALTY, 2)


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


async def run_self_correction(
    result: ExtractionResult,
    org_settings: dict | None = None,
) -> SelfCorrectionReport:
    """Run all invariant checks on an extraction result.

    Returns a report with any violations found and confidence penalties
    applied.  The caller is responsible for attaching warnings to the
    invoice and storing correction metadata.
    """
    extraction_cfg = (org_settings or {}).get("extraction", {})
    if not extraction_cfg.get("self_correction_enabled", True):
        return SelfCorrectionReport()

    from app.services.extraction import extraction_amount_convention

    report = SelfCorrectionReport()
    # One document, one decimal convention — resolved from the same money
    # tokens `_apply_extraction` read, so the checker and the writer agree.
    convention = extraction_amount_convention(result)

    _check_total_reconciliation(result, report, convention)
    _check_date_ordering(result, report, convention)
    _check_line_items_sum(result, report, convention)
    _check_line_item_math(result, report, convention)

    # Recompute overall confidence after penalties.
    #
    # The recompute averages only the 5 key fields, which can be HIGHER than the
    # adapter's original overall (computed across the full field set, possibly
    # dragged down by many uncertain non-key fields). A self-correction that
    # FOUND violations must never *raise* the confidence the auto-approve gate
    # reads — that would make a flagged-suspect extraction more eligible for
    # touchless approval. Clamp to the original so a violation can only lower
    # (or hold) it, never increase it.
    if report.corrected:
        fields = [
            result.invoice_number,
            result.vendor_name,
            result.amount,
            result.invoice_date,
            result.due_date,
        ]
        confidences = [f.confidence for f in fields if f.value is not None]
        if confidences:
            recomputed = sum(confidences) / len(confidences)
            result.overall_confidence = min(result.overall_confidence, recomputed)

    return report
