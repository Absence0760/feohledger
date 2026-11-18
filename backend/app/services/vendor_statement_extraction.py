"""PDF supplier-statement intake — the bridge from the extraction pipeline to
the reconciliation engine.

A supplier that sends its statement of open items as a PDF used to have to be
transcribed by hand: the CSV and pasted-lines paths both shipped, the PDF one
didn't. This module closes that by routing the PDF through the **existing**
extraction pipeline — the org's own configured adapter, resolved by the same
platform-vs-BYOK rules invoices use — rather than standing up a second parser
with its own provider config and its own failure modes.

What lives here is only the glue:

* pick the adapter (:func:`resolve_statement_adapter`),
* call the optional ``extract_statement`` capability, never letting a provider
  failure escape as a 500,
* normalise the adapter's RAW strings into the reconciliation engine's
  ``Decimal`` / ``date`` dataclasses.

The matching itself is untouched: the normalised lines go into the same pure
``vendor_statement_recon.reconcile`` the CSV and manual paths feed, so a PDF run
and a CSV run are the same run.

**Fail closed.** Every failure — provider can't do it, provider errored,
document has no readable open items — raises :class:`StatementExtractionError`,
which the router turns into a 422. Nothing in here ever produces a partial or
inferred statement line: on this feature an invented open item is money a clerk
then chases the supplier for.

See ``backend/docs/vendor-statement-reconciliation.md`` § PDF intake.
"""

from __future__ import annotations

import logging
import uuid

from app.services import vendor_statement_recon as recon
from app.services.extraction_adapters.base import (
    STATEMENT_REASON_EMPTY_FILE,
    STATEMENT_REASON_NO_LINES,
    STATEMENT_REASON_NO_TEXT_LAYER,
    STATEMENT_REASON_NOT_SUPPORTED,
    STATEMENT_REASON_PROVIDER_ERROR,
    STATEMENT_REASON_PROVIDER_UNKNOWN,
    STATEMENT_REASON_UNREADABLE,
    ExtractionAdapter,
    StatementExtractionResult,
)
from app.utils.dates import resolve_day_first_preference

logger = logging.getLogger(__name__)

# Reason code → the message the user actually sees. Every string here is
# PII-free and static by construction: an adapter's own error text (which may
# echo a provider response body) is logged, never surfaced.
_REASON_MESSAGES = {
    STATEMENT_REASON_NOT_SUPPORTED: (
        "The extraction provider configured for this organization cannot read PDF "
        "statements. Upload the statement as a CSV, or configure a provider that can."
    ),
    STATEMENT_REASON_EMPTY_FILE: "The uploaded statement file is empty.",
    STATEMENT_REASON_NO_TEXT_LAYER: (
        "This statement is a scan with no readable text, and the configured extraction "
        "provider could not read it. Upload a CSV, or configure a vision provider."
    ),
    STATEMENT_REASON_NO_LINES: (
        "No open items could be read from this document. Check that it is a statement "
        "of open items, or upload the statement as a CSV."
    ),
    STATEMENT_REASON_PROVIDER_ERROR: (
        "The extraction provider could not be reached. Try again, or upload the statement as a CSV."
    ),
    STATEMENT_REASON_UNREADABLE: (
        "The extraction provider returned a response that could not be read. Try "
        "again, or upload the statement as a CSV."
    ),
    STATEMENT_REASON_PROVIDER_UNKNOWN: (
        "The extraction provider configured for this organization is not recognised. "
        "An administrator must correct it in Settings, or upload the statement as a CSV."
    ),
}

_FALLBACK_MESSAGE = "The statement document could not be read. Upload it as a CSV instead."

# PDF magic bytes. Checked alongside the declared content type and the filename
# because a browser will happily post a PDF as `application/octet-stream`, and
# feeding a PDF to the CSV parser produces a baffling error instead of routing
# it to extraction.
_PDF_MAGIC = b"%PDF-"


class StatementExtractionError(ValueError):
    """A statement document could not be turned into statement lines.

    Carries a PII-free ``reason`` code plus the user-facing ``message``; the
    router raises 422 with the message. Never carries provider output.
    """

    def __init__(self, reason: str, message: str | None = None):
        self.reason = reason
        self.message = message or _REASON_MESSAGES.get(reason, _FALLBACK_MESSAGE)
        super().__init__(self.message)


def looks_like_pdf(
    file_bytes: bytes, *, filename: str | None = None, content_type: str | None = None
) -> bool:
    """Is this upload a PDF? Magic bytes first, then the declared type/name."""
    if file_bytes[: len(_PDF_MAGIC)] == _PDF_MAGIC:
        return True
    if (content_type or "").split(";")[0].strip().lower() == "application/pdf":
        return True
    return (filename or "").lower().endswith(".pdf")


async def resolve_statement_adapter(
    org_settings: dict | None, *, organization_id: uuid.UUID
) -> ExtractionAdapter:
    """Return the org's configured extraction adapter, ready for a statement.

    Deliberately reuses ``extraction._resolve_extraction_config`` rather than
    re-deriving the config: that function owns the platform-vs-BYOK decision
    (which key is used, and whose) and duplicating it here would be duplicating
    a credential-selection rule — the exact kind of copy that silently rots.

    Raises :class:`StatementExtractionError` when the org names an extraction
    provider we have no adapter for.
    """
    # `get_extraction_adapter` registers the built-in adapters itself, and
    # RAISES on a provider name it has no adapter for rather than substituting
    # `mock` — see `decisions.md` §29. Translate that into the same fail-closed
    # 422 every other statement-read failure takes, so a config error can't
    # 500 an upload (`resolve_statement_adapter` is called outside the caller's
    # try block).
    from app.services.credential_crypto import CredentialCryptoError
    from app.services.extraction import _resolve_extraction_config
    from app.services.extraction_adapters import (
        UnknownExtractionProviderError,
        get_extraction_adapter,
    )
    from app.services.provider_credentials import CREDENTIALS_UNAVAILABLE_DETAIL

    try:
        config = await _resolve_extraction_config(org_settings, organization_id=organization_id)
    except CredentialCryptoError as exc:
        # A BYOK org's sealed key could not be opened: nothing was sent, and the
        # upload gets the same fail-closed 422 as every other read failure.
        logger.warning("statement extraction: sealed extraction credentials unavailable")
        raise StatementExtractionError(
            STATEMENT_REASON_PROVIDER_ERROR, CREDENTIALS_UNAVAILABLE_DETAIL
        ) from exc
    try:
        return get_extraction_adapter(config)
    except UnknownExtractionProviderError as exc:
        logger.warning("statement extraction: unregistered provider configured")
        raise StatementExtractionError(STATEMENT_REASON_PROVIDER_UNKNOWN) from exc


def normalize_extracted_lines(
    result: StatementExtractionResult,
    *,
    day_first: bool = False,
) -> list[recon.StatementLine]:
    """Turn an adapter's raw strings into the engine's typed statement lines.

    Money becomes ``Decimal`` through the same :func:`recon.parse_amount` the
    CSV path uses — so ``(250.00)``, ``$850.50`` and ``-250.00`` all mean what
    they mean on a statement, and nothing passes through a float. A row that
    ends up with neither an invoice number nor an amount has nothing to match
    on and is dropped, exactly as the CSV parser drops it.

    The decimal convention is resolved across the WHOLE document first, for the
    same reason the CSV path does it: a supplier writing ``850,00`` means
    850.00, and only the other rows can prove it.

    ``day_first`` resolves the ambiguous slashed ``invoice_date`` string the
    same way the CSV intake does (see
    ``app.utils.dates.resolve_day_first_preference``).
    """
    convention = recon.detect_amount_convention(line.amount for line in result.lines)
    lines: list[recon.StatementLine] = []
    for extracted in result.lines:
        number = (extracted.invoice_number or "").strip() or None
        amount = recon.parse_amount(extracted.amount, convention=convention)
        if number is None and amount is None:
            continue
        lines.append(
            recon.StatementLine(
                invoice_number=number,
                invoice_date=recon.parse_date(extracted.invoice_date, day_first=day_first),
                amount=amount,
                status=(extracted.status or "").strip() or None,
                raw={
                    "invoice_number": extracted.invoice_number,
                    "invoice_date": extracted.invoice_date,
                    "amount": extracted.amount,
                    "status": extracted.status,
                    "confidence": extracted.confidence,
                    "source": "extraction",
                },
            )
        )
    return lines


async def extract_statement_lines(
    *,
    org_settings: dict | None,
    organization_id: uuid.UUID,
    file_bytes: bytes,
    file_key: str = "",
    mime_type: str = "application/pdf",
) -> tuple[list[recon.StatementLine], dict]:
    """Read a PDF statement into engine-ready lines + a provenance record.

    Returns ``(statement_lines, meta)`` where ``meta`` records which provider
    read the document and how confident it was — persisted on the run so a
    reviewer can see that these lines were machine-read, and by what.

    Raises :class:`StatementExtractionError` on every failure path.
    """
    adapter = await resolve_statement_adapter(org_settings, organization_id=organization_id)

    try:
        result = await adapter.extract_statement(
            file_bytes=file_bytes, file_key=file_key, mime_type=mime_type
        )
    except Exception:
        # An adapter is contractually best-effort, but a bug in one must not
        # 500 the upload. PII-free: the provider name and nothing from the doc.
        logger.warning(
            "statement extraction raised", extra={"provider": adapter.provider_name}, exc_info=True
        )
        raise StatementExtractionError(STATEMENT_REASON_PROVIDER_ERROR) from None

    if not result.available:
        raise StatementExtractionError(result.reason or STATEMENT_REASON_NOT_SUPPORTED)

    if not result.success:
        if result.error:
            # Provider detail belongs in the log, never in the HTTP body.
            logger.warning(
                "statement extraction failed: %s",
                result.error,
                extra={"provider": result.provider, "reason": result.reason},
            )
        raise StatementExtractionError(result.reason or STATEMENT_REASON_UNREADABLE)

    lines = normalize_extracted_lines(result, day_first=resolve_day_first_preference(org_settings))
    if not lines:
        # The adapter reported success but nothing survived normalisation —
        # treat it as "no open items", not as an empty statement.
        raise StatementExtractionError(STATEMENT_REASON_NO_LINES)

    meta = {
        "method": "ai_extraction",
        "provider": result.provider,
        "confidence": round(float(result.overall_confidence), 4),
        "line_count": len(lines),
        # Rows the reader recognised as an open item but refused to book
        # (see `statement_extraction.StatementScan`). Persisted on the run so
        # the provenance panel can say the diff below is short by this many
        # supplier rows — without it a clerk whose aging-bucket statement lost
        # half its rows just sees a suspiciously short run.
        "skipped_ambiguous": int(result.skipped_ambiguous),
    }
    return lines, meta
