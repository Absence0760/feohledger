"""Mock extraction adapter for development and testing."""

import uuid
from datetime import timedelta

from app.services import pdf_reader
from app.services.extraction_adapters.base import (
    STATEMENT_REASON_EMPTY_FILE,
    STATEMENT_REASON_NO_LINES,
    STATEMENT_REASON_NO_TEXT_LAYER,
    ExtractedField,
    ExtractedLineItem,
    ExtractionAdapter,
    ExtractionResult,
    StatementExtractionResult,
)
from app.services.extraction_adapters.dispatcher import register_extraction_adapter
from app.services.extraction_adapters.statement_extraction import scan_statement_text
from app.utils.dates import utc_today


@register_extraction_adapter("mock")
class MockExtractionAdapter(ExtractionAdapter):
    provider_name = "mock"

    async def extract(
        self,
        file_bytes: bytes = b"",
        file_key: str = "",
        mime_type: str = "application/pdf",
        file_url: str = "",
    ) -> ExtractionResult:
        today = utc_today()
        return ExtractionResult(
            success=True,
            overall_confidence=0.95,
            invoice_number=ExtractedField(f"EXT-{uuid.uuid4().hex[:8].upper()}", 0.98),
            vendor_name=ExtractedField("Extracted Vendor Inc", 0.95),
            vendor_address=ExtractedField("123 Vendor St, Suite 200, Austin, TX 78701", 0.88),
            vendor_tax_id=ExtractedField("12-3456789", 0.92),
            amount=ExtractedField("1500.00", 0.99),
            currency=ExtractedField("USD", 0.99),
            subtotal=ExtractedField("1350.00", 0.97),
            tax_amount=ExtractedField("150.00", 0.96),
            tax_rate=ExtractedField("10.00", 0.90),
            invoice_date=ExtractedField(today.isoformat(), 0.97),
            due_date=ExtractedField((today + timedelta(days=30)).isoformat(), 0.95),
            payment_terms=ExtractedField("Net 30", 0.93),
            payment_method=ExtractedField("ach", 0.80),
            reference_number=ExtractedField(f"REF-{uuid.uuid4().hex[:6].upper()}", 0.85),
            description=ExtractedField(f"Extracted from file: {file_key or 'unknown'}", 0.90),
            suggested_gl_account=ExtractedField("6100", 0.75),
            suggested_cost_center=ExtractedField("ADMIN", 0.70),
            line_items=[
                ExtractedLineItem(
                    line_number=1,
                    description=ExtractedField("Professional services", 0.92),
                    quantity=ExtractedField("1", 0.95),
                    unit_price=ExtractedField("1350.00", 0.97),
                    tax=ExtractedField("150.00", 0.93),
                    total=ExtractedField("1500.00", 0.98),
                ),
            ],
            provider="mock",
        )

    async def extract_statement(
        self,
        file_bytes: bytes = b"",
        file_key: str = "",
        mime_type: str = "application/pdf",
    ) -> StatementExtractionResult:
        """Read a supplier statement offline — no network, no credential.

        Unlike :meth:`extract`, this does NOT return a fixture: a fabricated
        open item is money a clerk would then chase, and the reconciliation run
        it lands in is a review queue, not a demo. It reads the document's own
        text layer and gives up loudly when there isn't one (a scan needs a
        vision provider). That's what makes `pnpm dev` able to run the whole
        PDF-statement path against a real supplier PDF with nothing configured.
        """
        if not file_bytes:
            return StatementExtractionResult(
                available=True, provider=self.provider_name, reason=STATEMENT_REASON_EMPTY_FILE
            )

        is_pdf = (
            mime_type == "application/pdf"
            or file_key.lower().endswith(".pdf")
            or file_bytes[:5] == b"%PDF-"
        )
        if is_pdf:
            text = await pdf_reader.pdf_text(file_bytes)
        else:
            try:
                text = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                text = None

        if not text or not text.strip():
            return StatementExtractionResult(
                available=True, provider=self.provider_name, reason=STATEMENT_REASON_NO_TEXT_LAYER
            )

        scan = scan_statement_text(text)
        if not scan.lines:
            # Nothing bookable. The ambiguous count is deliberately dropped
            # here: with no lines the run never gets created, so there is no
            # provenance panel to carry it, and the 422 the router raises is a
            # static PII-free string keyed off the reason code.
            return StatementExtractionResult(
                available=True, provider=self.provider_name, reason=STATEMENT_REASON_NO_LINES
            )

        confidences = [ln.confidence for ln in scan.lines]
        return StatementExtractionResult(
            available=True,
            success=True,
            lines=scan.lines,
            overall_confidence=sum(confidences) / len(confidences),
            skipped_ambiguous=scan.ambiguous_skips,
            provider=self.provider_name,
        )

    async def test_connection(self) -> bool:
        return True
