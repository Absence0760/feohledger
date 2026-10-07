"""Extraction pipeline logging — PII-in-logs regression guard.

The invoice-extraction pipeline sits at the bottom of the vision/OCR adapters,
GL validation, and duplicate detection. A broadly-caught exception there can
carry extracted invoice PII (vendor tax id / bank details / address) in its
message — an adapter builds ``ExtractionResult.error`` from the raw provider
``{exc}`` / ``resp.text`` (see ``extraction_adapters/*``). The failure handlers
in ``services/extraction.py`` and ``services/extraction_dispatch.py`` must
therefore log the exception CLASS name only, never the raw message string.

These tests drive the failure path with a sentinel exception whose message
contains a fake bank number and assert:
  (a) the failure is logged at ERROR with the exception's class name present, and
  (b) that sentinel never appears anywhere in the captured log text.

Everything that touches the network or DB is mocked, so the tests are hermetic.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the real database module up front, at collection time. The dispatch
# test below patches `sqlalchemy.ext.asyncio.async_sessionmaker` globally —
# `extraction_dispatch` imports it inside the function under test, so there is
# no module-local name to patch instead. If `app.database` were first imported
# inside that window, its module-level `_default_control_session_factory` would
# be built from the MagicMock and stay a plain lambda for the rest of the
# process, so the next realdb test to call `.configure(bind=…)` on it would die
# with `AttributeError: 'function' object has no attribute 'configure'`.
# Importing here binds the real factory before any patch can be active.
import app.database  # noqa: F401  (imported for its module-level side effect)

# A fake bank number embedded in the sentinel exception message. If the raw
# exception message leaks into any log record, this string proves it.
_PII_SENTINEL = "BANK-999888777"


class _VisionSDKError(Exception):
    """Stand-in for a vision/OCR SDK exception that echoes request/response
    content (i.e. invoice PII) into its message."""


def _sentinel_exc() -> _VisionSDKError:
    return _VisionSDKError(
        f"vision provider 500: account={_PII_SENTINEL} routing=021000021 tax_id=12-3456789"
    )


def _make_invoice(*, status_value="pending"):
    """Minimal Invoice stand-in with the attrs run_extraction touches."""
    from app.models.invoice import InvoiceStatus

    return SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
        file_key="invoices/test.pdf",
        status=InvoiceStatus(status_value),
        amount=None,
        vendor_name="Acme Corp",
        # `Invoice.vendor_id` is a real column on the ORM row this namespace
        # stands in for, and `run_extraction` caches it before its `try` (the
        # re-extraction `skip_vendor_match` guard restores it). A stand-in
        # missing it fails with AttributeError before the failure path under
        # test is ever reached. Mirrors `test_extraction_usage_placement.py`.
        vendor_id=None,
        invoice_number="INV-001",
        warnings=None,
        po_match=None,
    )


def _make_db(re_fetch_invoice=None):
    """Tenant DB AsyncMock; execute().scalar_one_or_none() -> re_fetch_invoice."""
    db = AsyncMock()
    db.add = MagicMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=re_fetch_invoice)
    db.execute.return_value = execute_result
    return db


def _patch_failing_internals(exc: Exception) -> ExitStack:
    """Mock every external call in run_extraction up to the adapter, and make
    ``adapter.extract`` raise ``exc``. Also neutralise the downstream failure-
    handler calls (exception record + workflow transition) so the test isolates
    run_extraction's own logging behaviour."""
    stack = ExitStack()

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"PDF content")}
    stack.enter_context(patch("boto3.client", return_value=mock_s3))

    exploding_adapter = MagicMock()
    exploding_adapter.provider_name = "mock"
    exploding_adapter.extract = AsyncMock(side_effect=exc)
    stack.enter_context(
        patch(
            "app.services.extraction_adapters.get_extraction_adapter",
            return_value=exploding_adapter,
        )
    )

    stack.enter_context(patch("app.services.rag.extract_invoice_text", return_value="txt"))
    stack.enter_context(patch("app.services.rag.retrieve_similar", AsyncMock(return_value=[])))

    # Failure-handler collaborators — the exception record legitimately keeps
    # str(exc) in the DB (never logs); mock it out so it can't add log noise.
    stack.enter_context(patch("app.services.exception_service.create_exception", AsyncMock()))
    stack.enter_context(patch("app.services.extraction.transition_invoice", AsyncMock()))
    stack.enter_context(
        patch("app.services.extraction.get_workflow_instance", AsyncMock(return_value=None))
    )
    return stack


@pytest.mark.asyncio
async def test_extraction_failure_logs_class_name_not_raw_message(caplog):
    """run_extraction's failure handler logs the exception CLASS at ERROR and
    never interpolates the raw (PII-bearing) message."""
    from app.services.extraction import run_extraction

    invoice = _make_invoice()
    re_fetched = _make_invoice()
    re_fetched.id = invoice.id
    re_fetched.organization_id = invoice.organization_id
    db = _make_db(re_fetch_invoice=re_fetched)

    with caplog.at_level(logging.DEBUG, logger="app.services.extraction"):
        with _patch_failing_internals(_sentinel_exc()):
            # run_extraction handles its own exception internally (no re-raise).
            await run_extraction(db, invoice)

    extraction_errors = [
        r
        for r in caplog.records
        if r.name == "app.services.extraction" and r.levelno == logging.ERROR
    ]
    assert extraction_errors, "expected an ERROR log from the extraction failure handler"

    # (a) the exception CLASS name is present in the failure message.
    assert any("_VisionSDKError" in r.getMessage() for r in extraction_errors), (
        "failure log must identify the exception class"
    )

    # (b) the raw PII-bearing message never reaches any captured log record.
    assert _PII_SENTINEL not in caplog.text, "raw exception message (with PII) leaked into the logs"


@pytest.mark.asyncio
async def test_extraction_failure_does_not_log_adapter_error_body(caplog):
    """A returned (not raised) adapter failure must not echo result.error —
    adapters build it from the raw provider exception / response body."""
    from app.services.extraction import run_extraction
    from app.services.extraction_adapters.base import ExtractionResult

    failing = ExtractionResult(success=False, error=f"provider body: {_PII_SENTINEL}")

    invoice = _make_invoice()
    re_fetched = _make_invoice()
    re_fetched.id = invoice.id
    re_fetched.organization_id = invoice.organization_id
    db = _make_db(re_fetch_invoice=re_fetched)

    stack = ExitStack()
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"PDF content")}
    stack.enter_context(patch("boto3.client", return_value=mock_s3))
    adapter = MagicMock()
    adapter.provider_name = "mock"
    adapter.extract = AsyncMock(return_value=failing)
    stack.enter_context(
        patch("app.services.extraction_adapters.get_extraction_adapter", return_value=adapter)
    )
    stack.enter_context(patch("app.services.rag.extract_invoice_text", return_value="txt"))
    stack.enter_context(patch("app.services.rag.retrieve_similar", AsyncMock(return_value=[])))
    stack.enter_context(patch("app.services.exception_service.create_exception", AsyncMock()))
    stack.enter_context(patch("app.services.extraction.transition_invoice", AsyncMock()))
    stack.enter_context(
        patch("app.services.extraction.get_workflow_instance", AsyncMock(return_value=None))
    )

    with caplog.at_level(logging.DEBUG, logger="app.services.extraction"):
        with stack:
            await run_extraction(db, invoice)

    assert _PII_SENTINEL not in caplog.text, "adapter result.error body leaked into the logs"


@pytest.mark.asyncio
async def test_dispatch_run_local_logs_class_name_not_raw_message(caplog):
    """extraction_dispatch._run_local's defensive handler logs the exception
    CLASS at ERROR (when run_extraction itself raises) and never the raw
    message.

    _run_local opens a control session (needs ``org.db_name``) then a tenant
    session (needs the invoice). We return a single "universal" row from every
    mocked session — it carries both ``db_name`` and a non-``pending`` status so
    the ``_fail_invoice_safely`` fallback finds nothing to transition — which
    makes the mock independent of the exact session-open order.
    """
    from app.models.invoice import InvoiceStatus
    from app.services import extraction_dispatch as ed

    universal = SimpleNamespace(
        id=uuid.uuid4(),
        db_name="feoh_test",
        settings={},
        status=InvoiceStatus("failed"),  # not 'pending' → fallback no-ops
        file_key="invoices/test.pdf",
    )

    class _FakeSession:
        def __init__(self, scalar):
            self._scalar = scalar
            self.add = MagicMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *a, **k):
            r = MagicMock()
            r.scalar_one_or_none = MagicMock(return_value=self._scalar)
            return r

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def close(self):
            # `_run_local` hands its control connection back before extracting.
            return None

    fake_engine = MagicMock()
    fake_engine.dispose = AsyncMock()

    stack = ExitStack()
    stack.enter_context(
        patch("sqlalchemy.ext.asyncio.create_async_engine", return_value=fake_engine)
    )
    stack.enter_context(
        patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker",
            MagicMock(return_value=lambda: _FakeSession(universal)),
        )
    )
    stack.enter_context(
        patch("app.services.extraction.run_extraction", AsyncMock(side_effect=_sentinel_exc()))
    )

    with caplog.at_level(logging.DEBUG, logger="app.services.extraction_dispatch"):
        with stack:
            await ed._run_local(universal.id, universal.id, uuid.uuid4())

    dispatch_errors = [
        r
        for r in caplog.records
        if r.name == "app.services.extraction_dispatch" and r.levelno == logging.ERROR
    ]
    assert dispatch_errors, "expected an ERROR log from the dispatch failure handler"
    assert any("_VisionSDKError" in r.getMessage() for r in dispatch_errors), (
        "dispatch failure log must identify the exception class"
    )
    assert _PII_SENTINEL not in caplog.text, (
        "raw exception message (with PII) leaked into the dispatch logs"
    )
