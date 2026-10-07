"""Where `run_extraction` writes the `ExtractionUsage` billing meter.

`extraction_usage` is a TENANT table. It is not in
`tenant_provisioning.CONTROL_TABLES`, no Alembic revision creates it, and
`scripts/seed.py::create_control_tables` filters to the control set — so it is
created only by `provision_tenant`, in each tenant DB. `services/billing`'s
`rollup_usage` reads it there, alongside the sibling meter `card_rebates`.

It used to be written through a control-plane session, on the strength of a
comment claiming the table lived there. It does not: the INSERT raised
`UndefinedTableError` into `run_extraction`'s own `except Exception`, which
rolled the tenant transaction back, opened an `extraction_failed` exception and
transitioned the invoice to `failed`. A SUCCESSFUL extraction was recorded as a
failure, and platform billing's primary meter was permanently zero.

The whole previous version of this file asserted that broken placement, using an
`AsyncMock` for the control session — so no test ever executed the INSERT
against a real database. `test_extraction_usage_table_is_tenant_local` is the
guard that closes that gap: it asks Postgres where the table actually is.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.extraction_adapters.base import ExtractedField, ExtractionResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_invoice(*, status_value="pending"):
    """Minimal Invoice stand-in with all attrs run_extraction touches."""
    from app.models.invoice import InvoiceStatus

    status = InvoiceStatus(status_value)
    return SimpleNamespace(
        id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),  # multi-entity P2: exception inherits invoice entity
        file_key="invoices/test.pdf",
        status=status,
        amount=None,
        vendor_name="Acme Corp",
        invoice_number="INV-001",
        invoice_date=None,
        due_date=None,
        payment_terms=None,
        payment_method=None,
        po_number=None,
        description=None,
        vendor_address=None,
        vendor_tax_id=None,
        reference_number=None,
        bill_to_address=None,
        remit_to_address=None,
        subtotal=None,
        tax_amount=None,
        tax_rate=None,
        discount_amount=None,
        shipping_amount=None,
        currency="USD",
        gl_account=None,
        cost_center=None,
        vendor_id=None,
        warnings=None,
        po_match=None,
    )


def _make_db(re_fetch_invoice=None):
    """Build a tenant DB AsyncMock whose execute().scalar_one_or_none() returns
    re_fetch_invoice (used in the failure re-fetch path).

    `db.add` is a regular MagicMock (not AsyncMock) because SQLAlchemy's
    `Session.add` is synchronous — leaving it as AsyncMock leaks an
    unawaited-coroutine RuntimeWarning per call.
    """
    db = AsyncMock()
    db.add = MagicMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=re_fetch_invoice)
    db.execute.return_value = execute_result
    return db


def _successful_extraction_result() -> ExtractionResult:
    """A minimal successful ExtractionResult."""
    return ExtractionResult(
        success=True,
        overall_confidence=0.92,
        vendor_name=ExtractedField(value="Acme Corp", confidence=0.95),
        invoice_number=ExtractedField(value="INV-001", confidence=0.99),
        amount=ExtractedField(value="1000.00", confidence=0.97),
        provider="mock",
        raw_response={"raw": True},
    )


def _failing_extraction_result() -> ExtractionResult:
    return ExtractionResult(success=False, error="AI quota exceeded")


def _patch_extraction_internals(extraction_result: ExtractionResult):
    """Return a context-manager stack that mocks every external call in run_extraction.

    run_extraction imports several modules *inside* the function body (lazy
    imports), so we patch at the final module path, not at
    'app.services.extraction.<name>'.
    """
    from contextlib import ExitStack

    stack = ExitStack()

    # S3
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"PDF content")}
    stack.enter_context(patch("boto3.client", return_value=mock_s3))

    # Extraction adapter — patch get_extraction_adapter in the adapters package
    mock_adapter = MagicMock()
    mock_adapter.provider_name = "mock"
    mock_adapter.extract = AsyncMock(return_value=extraction_result)
    stack.enter_context(
        patch(
            "app.services.extraction_adapters.get_extraction_adapter",
            return_value=mock_adapter,
        )
    )

    # RAG — patched at the module level the service imports from
    stack.enter_context(patch("app.services.rag.extract_invoice_text", return_value="invoice text"))
    stack.enter_context(patch("app.services.rag.retrieve_similar", AsyncMock(return_value=[])))
    stack.enter_context(patch("app.services.rag.build_few_shot_prompt", return_value=""))
    stack.enter_context(patch("app.services.rag.neighbors_to_metadata", return_value=[]))

    # Vendor matching — imported inside run_extraction as:
    #   from app.services.vendor_matching import match_and_link_vendor
    fake_vendor = SimpleNamespace(id=uuid.uuid4())
    stack.enter_context(
        patch(
            "app.services.vendor_matching.match_and_link_vendor",
            AsyncMock(return_value=(fake_vendor, "matched")),
        )
    )

    # Vendor priors
    stack.enter_context(
        patch(
            "app.services.vendor_priors.apply_priors_to_invoice",
            AsyncMock(return_value=[]),
        )
    )

    # Duplicate detection
    stack.enter_context(
        patch(
            "app.services.duplicate_detection.find_semantic_duplicates",
            AsyncMock(return_value=[]),
        )
    )
    stack.enter_context(
        patch("app.services.duplicate_detection.matches_to_warning", return_value=None)
    )

    # Invoice warnings
    stack.enter_context(patch("app.services.invoice_warnings.refresh_warnings", AsyncMock()))

    # Workflow engine — these are imported at the top of extraction.py so
    # patch via the extraction module's namespace.
    stack.enter_context(patch("app.services.extraction.transition_invoice", AsyncMock()))
    stack.enter_context(
        patch("app.services.extraction.get_workflow_instance", AsyncMock(return_value=None))
    )
    stack.enter_context(patch("app.services.extraction.advance_workflow", AsyncMock()))

    return stack


# ---------------------------------------------------------------------------
# The meter goes to the tenant session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_is_written_through_the_tenant_session_on_success():
    """A successful extraction adds exactly one ExtractionUsage to the tenant db."""
    from app.models.usage import ExtractionUsage
    from app.services.extraction import run_extraction

    invoice = _make_invoice()
    db = _make_db()

    with _patch_extraction_internals(_successful_extraction_result()):
        await run_extraction(db, invoice, actor_id=uuid.uuid4(), org_settings=None)

    usage_calls = [c for c in db.add.call_args_list if isinstance(c.args[0], ExtractionUsage)]
    assert len(usage_calls) == 1, "Expected exactly one ExtractionUsage on the tenant session"

    usage: ExtractionUsage = usage_calls[0].args[0]
    assert usage.invoice_id == invoice.id
    assert usage.organization_id == invoice.organization_id
    assert usage.success is True
    assert len(usage.period) == 7 and usage.period[4] == "-"


@pytest.mark.asyncio
async def test_usage_rides_the_extraction_commit_rather_than_its_own():
    """The meter is not committed separately — it lands with the extraction result.

    A separate commit is what made the old control-plane write able to fail
    independently of (and destroy) the extraction it was recording.
    """
    from app.services.extraction import run_extraction

    invoice = _make_invoice()
    db = _make_db()

    with _patch_extraction_internals(_successful_extraction_result()):
        await run_extraction(db, invoice)

    # One commit for the whole successful path, not one per side effect.
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_failed_usage_is_written_through_the_tenant_session():
    """On adapter failure a failed meter row is added to the tenant db."""
    from app.models.usage import ExtractionUsage
    from app.services.extraction import run_extraction

    invoice = _make_invoice()
    re_fetched = _make_invoice()
    re_fetched.id = invoice.id
    re_fetched.organization_id = invoice.organization_id

    db = _make_db(re_fetch_invoice=re_fetched)

    with _patch_extraction_internals(_failing_extraction_result()):
        await run_extraction(db, invoice)

    usage_calls = [c for c in db.add.call_args_list if isinstance(c.args[0], ExtractionUsage)]
    assert len(usage_calls) == 1
    assert usage_calls[0].args[0].success is False


@pytest.mark.asyncio
async def test_unexpected_error_still_writes_a_failed_meter_row():
    """A hard adapter exception still records the failed attempt."""
    from app.models.usage import ExtractionUsage
    from app.services.extraction import run_extraction

    invoice = _make_invoice()
    re_fetched = _make_invoice()
    re_fetched.id = invoice.id
    re_fetched.organization_id = invoice.organization_id

    db = _make_db(re_fetch_invoice=re_fetched)

    with _patch_extraction_internals(_successful_extraction_result()) as stack:
        exploding_adapter = MagicMock()
        exploding_adapter.provider_name = "mock"
        exploding_adapter.extract = AsyncMock(side_effect=RuntimeError("OOM"))
        stack.enter_context(
            patch(
                "app.services.extraction_adapters.get_extraction_adapter",
                return_value=exploding_adapter,
            )
        )
        await run_extraction(db, invoice)

    usage_calls = [c for c in db.add.call_args_list if isinstance(c.args[0], ExtractionUsage)]
    assert len(usage_calls) == 1
    assert usage_calls[0].args[0].success is False


def test_run_extraction_takes_no_control_plane_session():
    """The `ctrl_db` parameter is gone — re-adding it would re-open the defect.

    A signature guard rather than a behavioural one, because the failure mode was
    a caller handing in the wrong session, which no amount of mocking inside this
    module can catch.
    """
    import inspect

    from app.services.extraction import run_extraction

    params = inspect.signature(run_extraction).parameters
    assert "ctrl_db" not in params, (
        "ExtractionUsage is a tenant table; run_extraction must not take a "
        "control-plane session for it"
    )


# ---------------------------------------------------------------------------
# Where the table actually is (the guard the old file lacked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_usage_table_is_tenant_local(realdb):
    """Ask Postgres where `extraction_usage` lives, both ways round.

    The old code was written against a comment, not against the schema. This
    fails the moment either half of the placement drifts — the table appearing
    in the control plane, or vanishing from a tenant.
    """
    from sqlalchemy import text

    async with realdb.sessionmaker("a")() as s:
        tenant_reg = await s.scalar(text("select to_regclass('public.extraction_usage')"))
    assert tenant_reg is not None, "extraction_usage must exist in every tenant DB"

    async with realdb.control_sessionmaker()() as s:
        control_reg = await s.scalar(text("select to_regclass('public.extraction_usage')"))
    assert control_reg is None, (
        "extraction_usage must NOT exist in the control plane — if it does, the "
        "meter is now split across two databases and rollup_usage reads only one"
    )


@pytest.mark.asyncio
async def test_a_meter_row_round_trips_through_the_tenant_session(realdb):
    """The write run_extraction performs, executed for real.

    Under the old control-plane placement this INSERT raised
    `asyncpg.exceptions.UndefinedTableError`.
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.usage import ExtractionUsage

    org_id = realdb.info("a").org_id
    invoice_id = uuid.uuid4()
    period = datetime.now(UTC).strftime("%Y-%m")

    async with realdb.sessionmaker("a")() as s:
        s.add(
            ExtractionUsage(
                invoice_id=invoice_id,
                provider="mock",
                program_type="platform",
                period=period,
                success=True,
                organization_id=org_id,
            )
        )
        await s.commit()

    async with realdb.sessionmaker("a")() as s:
        row = await s.scalar(
            select(ExtractionUsage).where(ExtractionUsage.invoice_id == invoice_id)
        )
    assert row is not None
    assert row.success is True
    assert row.organization_id == org_id


# ---------------------------------------------------------------------------
# Token usage rides the meter row (migration 0108_extraction_usage_tokens)
# ---------------------------------------------------------------------------


def _usage():
    from app.services.extraction_adapters.base import ExtractionTokenUsage

    return ExtractionTokenUsage(
        input_tokens=2400,
        output_tokens=850,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        model="claude-sonnet-5-5",
    )


@pytest.mark.asyncio
async def test_a_successful_meter_row_carries_the_providers_token_usage():
    from app.models.usage import ExtractionUsage
    from app.services.extraction import run_extraction

    result = _successful_extraction_result()
    result.usage = _usage()
    db = _make_db()

    with _patch_extraction_internals(result):
        await run_extraction(db, _make_invoice())

    (row,) = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], ExtractionUsage)]
    assert (row.input_tokens, row.output_tokens) == (2400, 850)
    assert (row.cache_read_input_tokens, row.cache_creation_input_tokens) == (0, 0)
    assert row.model == "claude-sonnet-5-5"
    # The token columns are additive: what the row COUNTS as is unchanged.
    assert row.success is True


@pytest.mark.asyncio
async def test_a_failed_read_the_provider_still_billed_carries_its_tokens():
    """An unparseable or refused read cost tokens; the failure row must say so."""
    from app.models.usage import ExtractionUsage
    from app.services.extraction import run_extraction

    invoice = _make_invoice()
    re_fetched = _make_invoice()
    re_fetched.id = invoice.id
    re_fetched.organization_id = invoice.organization_id
    db = _make_db(re_fetch_invoice=re_fetched)

    failed = _failing_extraction_result()
    failed.usage = _usage()
    with _patch_extraction_internals(failed):
        await run_extraction(db, invoice)

    (row,) = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], ExtractionUsage)]
    assert row.success is False
    assert row.output_tokens == 850
    assert row.model == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_an_adapter_with_no_usage_report_leaves_the_token_columns_empty():
    from app.models.usage import ExtractionUsage
    from app.services.extraction import run_extraction

    db = _make_db()
    with _patch_extraction_internals(_successful_extraction_result()):
        await run_extraction(db, _make_invoice())

    (row,) = [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], ExtractionUsage)]
    assert row.input_tokens is None and row.output_tokens is None and row.model is None


@pytest.mark.asyncio
async def test_token_columns_round_trip_through_the_tenant_session(realdb):
    """The 0108 columns exist on the tenant table and persist what is written."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.usage import ExtractionUsage

    invoice_id = uuid.uuid4()
    async with realdb.sessionmaker("a")() as s:
        s.add(
            ExtractionUsage(
                invoice_id=invoice_id,
                provider="claude_vision",
                program_type="platform",
                period=datetime.now(UTC).strftime("%Y-%m"),
                success=True,
                organization_id=realdb.info("a").org_id,
                **_usage().as_columns(),
            )
        )
        await s.commit()

    async with realdb.sessionmaker("a")() as s:
        row = await s.scalar(
            select(ExtractionUsage).where(ExtractionUsage.invoice_id == invoice_id)
        )
    assert (row.input_tokens, row.output_tokens, row.model) == (2400, 850, "claude-sonnet-5-5")
