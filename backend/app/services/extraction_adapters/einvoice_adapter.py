"""Structured e-invoice extraction adapter (UBL 2.1 / Factur-X / ZUGFeRD).

Unlike the vision adapters this is a deterministic, pure-local parser — no LLM,
no network, no API key. Every present field is emitted at confidence 1.0, which
naturally trips the extraction step's ``auto_approve_threshold`` (default 0.95)
for trusted machine-readable invoices.

Field values are emitted as **strings** (Decimals stringified, dates
isoformat'd, payment-means mapped to canonical tokens) so the existing
``extraction._apply_extraction`` cleaners re-parse them into Decimal/date at the
DB boundary — zero new persistence path.

PII guard: ``raw_response`` carries only the format + root tag (no party tax
ids / addresses); validation errors name field paths only, never values.
"""

from __future__ import annotations

import asyncio

from app.services.e_invoice import (
    EInvoiceDocument,
    EInvoiceValidationError,
    parse_e_invoice,
)
from app.services.e_invoice.payment_means import payment_means_to_method
from app.services.extraction_adapters.base import (
    ExtractedField,
    ExtractedLineItem,
    ExtractionAdapter,
    ExtractionResult,
)
from app.services.extraction_adapters.dispatcher import register_extraction_adapter


def _decimal_str(value) -> str | None:
    return None if value is None else str(value)


def _join_address(party) -> str | None:
    parts: list[str] = list(party.address_lines)
    for extra in (party.city, party.postal_code, party.country_code):
        if extra:
            parts.append(extra)
    joined = ", ".join(p for p in parts if p)
    return joined or None


def _f(value) -> ExtractedField:
    """Wrap a present value at confidence 1.0; absent → default (None, 0.0)."""
    if value is None or value == "":
        return ExtractedField(None)
    return ExtractedField(str(value), 1.0)


@register_extraction_adapter("einvoice")
class EInvoiceExtractionAdapter(ExtractionAdapter):
    provider_name = "einvoice"

    async def extract(
        self,
        file_bytes: bytes = b"",
        file_key: str = "",
        mime_type: str = "application/pdf",
        file_url: str = "",
    ) -> ExtractionResult:
        try:
            # A hybrid PDF is opened with PyMuPDF to find its CII attachment:
            # run the parse on a worker thread (services/pdf_reader).
            doc = await asyncio.to_thread(parse_e_invoice, file_bytes, mime_type, file_key)
        except EInvoiceValidationError as exc:
            # Field-named errors only — never the offending value (PII).
            return ExtractionResult(
                success=False,
                provider="einvoice",
                error="; ".join(f"{e.field}: {e.code}" for e in exc.errors),
            )
        except ValueError:
            # Not a structured e-invoice. In practice detect() runs first in
            # run_extraction, so this is a defensive path.
            return ExtractionResult(
                success=False,
                provider="einvoice",
                error="not a structured e-invoice",
            )

        return self._to_result(doc)

    def _to_result(self, doc: EInvoiceDocument) -> ExtractionResult:
        grand_total = (
            doc.payable_amount if doc.payable_amount is not None else doc.tax_inclusive_amount
        )

        # The code→token table is shared with the OUTBOUND mapper
        # (`e_invoice/payment_means.py`) so the two directions can't drift and
        # an exported document reads back as what it was. The downstream
        # `_normalize_payment_method` cleaner re-validates against the dropdown.
        payment_method = payment_means_to_method(doc.payment_means_code)

        # tax_rate only when the document carries exactly one distinct rate.
        distinct_rates = {t.rate for t in doc.taxes if t.rate is not None}
        tax_rate = next(iter(distinct_rates)) if len(distinct_rates) == 1 else None

        line_items = [
            ExtractedLineItem(
                line_number=idx + 1,
                item_code=_f(line.item_code),
                description=_f(line.description),
                quantity=_f(_decimal_str(line.quantity)),
                unit_price=_f(_decimal_str(line.unit_price)),
                tax=_f(_decimal_str(line.tax_amount)),
                total=_f(_decimal_str(line.line_total)),
            )
            for idx, line in enumerate(doc.lines)
        ]

        return ExtractionResult(
            success=True,
            overall_confidence=1.0,
            invoice_number=_f(doc.invoice_number),
            vendor_name=_f(doc.seller.name),
            vendor_tax_id=_f(doc.seller.tax_id),
            vendor_address=_f(_join_address(doc.seller)),
            amount=_f(_decimal_str(grand_total)),
            currency=_f(doc.currency),
            subtotal=_f(_decimal_str(doc.line_extension_amount)),
            tax_amount=_f(_decimal_str(doc.tax_total)),
            tax_rate=_f(_decimal_str(tax_rate)),
            discount_amount=_f(_decimal_str(doc.allowance_total)),
            shipping_amount=_f(_decimal_str(doc.charge_total)),
            invoice_date=_f(doc.issue_date.isoformat() if doc.issue_date else None),
            due_date=_f(doc.due_date.isoformat() if doc.due_date else None),
            payment_terms=_f(doc.payment_terms_note),
            po_number=_f(doc.order_reference),
            reference_number=_f(doc.buyer_reference),
            payment_method=_f(payment_method),
            bill_to_address=_f(_join_address(doc.buyer)),
            line_items=line_items,
            raw_response={
                "e_invoice_format": doc.source_format.value,
                "root_tag": doc.raw_xml_root_tag,
            },
            provider="einvoice",
        )

    async def test_connection(self) -> bool:
        # Pure / local — always available.
        return True
