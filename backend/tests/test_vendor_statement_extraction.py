"""Unit coverage for ``services/vendor_statement_extraction`` — the bridge from
the extraction pipeline to the vendor-statement reconciliation engine.

The important properties, none of which are about matching (the engine owns
that and is tested separately):

* money crosses the boundary as ``Decimal``, parsed by the SAME rules the CSV
  path uses — a model returning ``(250.00)`` means a credit, not a positive;
* every failure fails CLOSED with a PII-free reason, never a partial run;
* an adapter's provider error text never reaches the user-facing message;
* the org's configured provider is what reads the document — the same
  platform/BYOK resolution invoices use.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.services import vendor_statement_extraction as vse
from app.services.extraction_adapters.base import (
    STATEMENT_REASON_NO_LINES,
    STATEMENT_REASON_NO_TEXT_LAYER,
    STATEMENT_REASON_NOT_SUPPORTED,
    STATEMENT_REASON_PROVIDER_ERROR,
    ExtractionAdapter,
    StatementExtractionResult,
    StatementLineExtraction,
)


class _StubAdapter(ExtractionAdapter):
    provider_name = "stub"

    def __init__(self, result=None, raises: Exception | None = None):
        super().__init__({})
        self._result = result
        self._raises = raises
        self.seen: dict | None = None

    async def extract_statement(self, file_bytes=b"", file_key="", mime_type="application/pdf"):
        self.seen = {"file_bytes": file_bytes, "file_key": file_key, "mime_type": mime_type}
        if self._raises is not None:
            raise self._raises
        return self._result


ORG = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _use(monkeypatch, adapter):
    async def resolve(org_settings, *, organization_id):
        return adapter

    monkeypatch.setattr(vse, "resolve_statement_adapter", resolve)
    return adapter


def _ok(lines):
    return StatementExtractionResult(
        available=True, success=True, lines=lines, overall_confidence=0.9, provider="stub"
    )


# --------------------------------------------------------------------------- #
# PDF detection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload,filename,content_type,expected",
    [
        (b"%PDF-1.7\n...", "statement.bin", "application/octet-stream", True),
        (b"anything", "statement.PDF", "application/octet-stream", True),
        (b"anything", "s.bin", "application/pdf; charset=binary", True),
        (b"invoice,amount\nINV-1,10", "s.csv", "text/csv", False),
        (b"", None, None, False),
    ],
)
def test_looks_like_pdf(payload, filename, content_type, expected):
    assert vse.looks_like_pdf(payload, filename=filename, content_type=content_type) is expected


# --------------------------------------------------------------------------- #
# Normalisation — the money boundary
# --------------------------------------------------------------------------- #


def test_normalize_parses_money_and_dates_exactly():
    lines = vse.normalize_extracted_lines(
        _ok(
            [
                StatementLineExtraction("INV-1", "2026-01-15", "1,200.00", "open", 0.9),
                StatementLineExtraction("INV-2", "01/20/2026", "$850.50", None, 0.9),
                # A credit, parenthesised the way a statement prints it.
                StatementLineExtraction("INV-3", "2026-02-01", "(250.00)", None, 0.9),
            ]
        )
    )
    assert [ln.invoice_number for ln in lines] == ["INV-1", "INV-2", "INV-3"]
    assert [ln.amount for ln in lines] == [
        Decimal("1200.00"),
        Decimal("850.50"),
        Decimal("-250.00"),
    ]
    assert all(isinstance(ln.amount, Decimal) for ln in lines)
    assert lines[0].invoice_date == date(2026, 1, 15)
    assert lines[1].invoice_date == date(2026, 1, 20)


def test_normalize_reads_a_european_statement_under_one_document_convention():
    """The convention is resolved across the whole document, not per line.

    `1.200` is ambiguous alone — a thousands group or a three-decimal value —
    but the sibling `1.234,56` proves the statement is European, so it reads as
    1200 rather than 1.2. And `850,00` is 850.00, not the 85000 the old
    unconditional comma-strip produced.
    """
    lines = vse.normalize_extracted_lines(
        _ok(
            [
                StatementLineExtraction("INV-1", "15.01.2026", "1.234,56", "open", 0.9),
                StatementLineExtraction("INV-2", "20.01.2026", "850,00", None, 0.9),
                StatementLineExtraction("INV-3", "01.02.2026", "1.200", None, 0.9),
                StatementLineExtraction("INV-4", "05.02.2026", "(250,00)", None, 0.9),
            ]
        )
    )
    assert [ln.amount for ln in lines] == [
        Decimal("1234.56"),
        Decimal("850.00"),
        Decimal("1200"),
        Decimal("-250.00"),
    ]
    assert all(isinstance(ln.amount, Decimal) for ln in lines)
    assert lines[0].invoice_date == date(2026, 1, 15)
    assert lines[1].invoice_date == date(2026, 1, 20)


def test_normalize_us_statement_is_unaffected_by_the_convention_pass():
    """The same document-level pass must leave the US reading exactly as it was
    — `1,200` is 1200, not 1.2."""
    lines = vse.normalize_extracted_lines(
        _ok(
            [
                StatementLineExtraction("INV-1", "2026-01-15", "1,234.56", None, 0.9),
                StatementLineExtraction("INV-2", "2026-01-20", "1,200", None, 0.9),
            ]
        )
    )
    assert [ln.amount for ln in lines] == [Decimal("1234.56"), Decimal("1200")]


def test_normalize_keeps_a_line_whose_date_is_unreadable():
    """A date we can't parse must not cost us the line — the engine's second
    matching leg tolerates a missing date, but it can't match a line we
    dropped."""
    lines = vse.normalize_extracted_lines(
        _ok([StatementLineExtraction("INV-4", "sometime in January", "100.00")])
    )
    assert len(lines) == 1
    assert lines[0].invoice_date is None
    assert lines[0].amount == Decimal("100.00")


def test_normalize_drops_rows_with_nothing_to_match_on():
    lines = vse.normalize_extracted_lines(
        _ok(
            [
                StatementLineExtraction(None, "2026-01-15", "not-a-number"),
                StatementLineExtraction("  ", None, None),
                StatementLineExtraction("INV-5", None, "10.00"),
            ]
        )
    )
    assert [ln.invoice_number for ln in lines] == ["INV-5"]


def test_normalize_records_provenance_on_the_raw_payload():
    lines = vse.normalize_extracted_lines(
        _ok([StatementLineExtraction("INV-6", "2026-01-15", "10.00", "open", 0.77)])
    )
    assert lines[0].raw["source"] == "extraction"
    assert lines[0].raw["confidence"] == 0.77


# --------------------------------------------------------------------------- #
# extract_statement_lines — the fail-closed contract
# --------------------------------------------------------------------------- #


async def test_extract_statement_lines_happy_path(monkeypatch):
    adapter = _use(
        monkeypatch,
        _StubAdapter(_ok([StatementLineExtraction("INV-1", "2026-01-15", "1200.00")])),
    )
    lines, meta = await vse.extract_statement_lines(
        org_settings={}, organization_id=ORG, file_bytes=b"%PDF-1.4", file_key="k.pdf"
    )
    assert [ln.amount for ln in lines] == [Decimal("1200.00")]
    assert meta == {
        "method": "ai_extraction",
        "provider": "stub",
        "confidence": 0.9,
        "line_count": 1,
        # A model-backed adapter isn't asked to report its own skips, so 0 here
        # honestly means "not measured" rather than "read everything".
        "skipped_ambiguous": 0,
    }
    assert adapter.seen["file_key"] == "k.pdf"


async def test_extract_statement_lines_carries_the_ambiguous_skip_count(monkeypatch):
    """The offline reader's refused rows reach the run's provenance meta.

    Without this the clerk sees a short run and no signal that the supplier's
    own rows were read and declined.
    """
    result = _ok([StatementLineExtraction("INV-1", "2026-01-15", "1200.00")])
    result.skipped_ambiguous = 3
    _use(monkeypatch, _StubAdapter(result))
    _lines, meta = await vse.extract_statement_lines(
        org_settings={}, organization_id=ORG, file_bytes=b"%PDF-1.4"
    )
    assert meta["line_count"] == 1
    assert meta["skipped_ambiguous"] == 3


async def test_unsupported_provider_refuses_instead_of_creating_an_empty_run(monkeypatch):
    _use(
        monkeypatch,
        _StubAdapter(
            StatementExtractionResult(available=False, reason=STATEMENT_REASON_NOT_SUPPORTED)
        ),
    )
    with pytest.raises(vse.StatementExtractionError) as exc:
        await vse.extract_statement_lines(
            org_settings={}, organization_id=ORG, file_bytes=b"%PDF-1.4"
        )
    assert exc.value.reason == STATEMENT_REASON_NOT_SUPPORTED
    assert "CSV" in exc.value.message


async def test_provider_error_text_never_reaches_the_user_message(monkeypatch, caplog):
    _use(
        monkeypatch,
        _StubAdapter(
            StatementExtractionResult(
                available=True,
                success=False,
                reason=STATEMENT_REASON_PROVIDER_ERROR,
                provider="stub",
                error="Claude API error 401: {'error': 'invalid x-api-key'}",
            )
        ),
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(vse.StatementExtractionError) as exc:
            await vse.extract_statement_lines(
                org_settings={}, organization_id=ORG, file_bytes=b"%PDF-1.4"
            )
    assert "x-api-key" not in exc.value.message
    assert exc.value.reason == STATEMENT_REASON_PROVIDER_ERROR
    # It IS logged — the operator needs it, the caller must not see it.
    assert any("x-api-key" in r.getMessage() for r in caplog.records)


async def test_adapter_exception_is_contained(monkeypatch):
    """An adapter is contractually best-effort; a bug in one must become a 422,
    never a 500 on the upload."""
    _use(monkeypatch, _StubAdapter(raises=RuntimeError("boom")))
    with pytest.raises(vse.StatementExtractionError) as exc:
        await vse.extract_statement_lines(
            org_settings={}, organization_id=ORG, file_bytes=b"%PDF-1.4"
        )
    assert exc.value.reason == STATEMENT_REASON_PROVIDER_ERROR
    assert "boom" not in exc.value.message


async def test_scan_with_no_text_layer_surfaces_an_actionable_message(monkeypatch):
    _use(
        monkeypatch,
        _StubAdapter(
            StatementExtractionResult(available=True, reason=STATEMENT_REASON_NO_TEXT_LAYER)
        ),
    )
    with pytest.raises(vse.StatementExtractionError) as exc:
        await vse.extract_statement_lines(
            org_settings={}, organization_id=ORG, file_bytes=b"%PDF-1.4"
        )
    assert exc.value.reason == STATEMENT_REASON_NO_TEXT_LAYER
    assert "scan" in exc.value.message.lower()


async def test_success_with_only_unusable_rows_is_a_refusal_not_an_empty_run(monkeypatch):
    """The adapter claimed success but nothing survived normalisation. Creating
    a run here would assert the supplier listed nothing — which would read as
    "we owe them nothing"."""
    _use(monkeypatch, _StubAdapter(_ok([StatementLineExtraction(None, None, "n/a")])))
    with pytest.raises(vse.StatementExtractionError) as exc:
        await vse.extract_statement_lines(
            org_settings={}, organization_id=ORG, file_bytes=b"%PDF-1.4"
        )
    assert exc.value.reason == STATEMENT_REASON_NO_LINES


# --------------------------------------------------------------------------- #
# Adapter resolution — the org's own configured provider
# --------------------------------------------------------------------------- #


async def test_resolve_uses_the_orgs_byok_provider(monkeypatch):
    from app.services import provider_credentials

    async def no_sealed_secrets(org_id, block, *, db=None):
        return {}

    monkeypatch.setattr(provider_credentials, "load_secrets", no_sealed_secrets)
    adapter = await vse.resolve_statement_adapter(
        {"extraction": {"program_type": "byok", "provider": "mock"}}, organization_id=ORG
    )
    assert adapter.provider_name == "mock"


async def test_resolve_fails_closed_when_the_sealed_key_cannot_be_opened(monkeypatch):
    """A BYOK org whose sealed key the store cannot open gets the statement
    422 path (nothing sent, no 500), and the message names no value."""
    from app.services import provider_credentials
    from app.services.credential_crypto import CredentialCryptoError

    async def broken_store(org_id, block, *, db=None):
        raise CredentialCryptoError("credential store unavailable")

    monkeypatch.setattr(provider_credentials, "load_secrets", broken_store)
    with pytest.raises(vse.StatementExtractionError) as exc:
        await vse.resolve_statement_adapter(
            {"extraction": {"program_type": "byok", "provider": "claude_vision"}},
            organization_id=ORG,
        )
    assert exc.value.reason == STATEMENT_REASON_PROVIDER_ERROR
    assert exc.value.message == provider_credentials.CREDENTIALS_UNAVAILABLE_DETAIL


async def test_resolve_defaults_to_the_platform_provider(monkeypatch):
    """Platform mode with a key resolves to the platform adapter, as it always has."""
    from app.services import extraction as ext

    monkeypatch.setattr(ext.settings, "extraction_provider", "")
    monkeypatch.setattr(ext.settings, "anthropic_api_key", "sk-ant-real-key")
    adapter = await vse.resolve_statement_adapter({}, organization_id=ORG)
    assert adapter.provider_name == "claude_vision"


async def test_resolve_falls_back_to_the_offline_reader_on_a_keyless_dev_box(monkeypatch):
    """The local-first half: no platform key locally → the offline text reader.

    This is what makes a PDF statement upload work on a fresh clone; before it,
    the same upload POSTed to api.anthropic.com with an empty key. Precedence
    itself is covered by `test_extraction_provider_resolution.py`.
    """
    from app.services import extraction as ext

    monkeypatch.setattr(ext.settings, "extraction_provider", "")
    monkeypatch.setattr(ext.settings, "anthropic_api_key", "")
    monkeypatch.setattr(ext.settings, "environment", "development")
    adapter = await vse.resolve_statement_adapter({}, organization_id=ORG)
    assert adapter.provider_name == "mock"
