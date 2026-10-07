"""Tests for `services.gl_recode.bulk_recode_gl`.

Mocks the DB session so the test suite stays hermetic. Two SELECTs hit
the DB on every run — `_load_active_chart` and `_select_eligible` —
plus an optional `_load_priors`. We stub them via a dispatch table so
each test only declares the rows that matter.

The AI fallback path is exercised via the `ai_runner` injection point.
We don't go anywhere near boto3 / Anthropic in these tests; that's
covered by the regular extraction test suite.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.invoice import InvoiceStatus
from app.services.gl_recode import (
    RecodeFilter,
    RecodeReport,
    bulk_recode_gl,
)

# ---------- Fixtures ------------------------------------------------------


def _make_invoice(
    *,
    vendor_id=None,
    gl_account=None,
    status=InvoiceStatus.ready_for_review,
    invoice_number="INV-1",
    invoice_date=None,
    entity_id=None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vendor_id=vendor_id,
        gl_account=gl_account,
        status=status,
        invoice_number=invoice_number,
        vendor_name="Acme Corp",
        invoice_date=invoice_date,
        entity_id=entity_id,
        warnings=None,
    )


class _Stub:
    """Sequence-driven AsyncMock for db.execute().

    Each call returns the next pre-built result.  Result objects expose
    `.scalars().all()` and a callable `.all()` for the priors join."""

    def __init__(self, results: list):
        self._results = list(results)
        self._idx = 0

    async def __call__(self, *_args, **_kwargs):
        if self._idx >= len(self._results):
            raise AssertionError("execute() called more times than the test set up")
        out = self._results[self._idx]
        self._idx += 1
        return out


def _scalars_all(rows: list):
    """Fake .scalars().all() chain — returns `rows`."""
    obj = MagicMock()
    obj.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    return obj


def _all_rows(rows: list):
    """Fake plain `.all()` for tuple-returning queries (priors join)."""
    obj = MagicMock()
    obj.all = MagicMock(return_value=rows)
    return obj


def _scalar(value):
    """Fake `.scalar()` for COUNT-returning queries."""
    obj = MagicMock()
    obj.scalar = MagicMock(return_value=value)
    return obj


def _make_db_for(
    *,
    active_codes,
    eligible_invoices,
    priors,
    skipped_immutable: int = 0,
    skipped_no_vendor: int = 0,
):
    """Sequence the SELECTs `bulk_recode_gl` issues:
    1. _load_active_chart           → list of (code, entity_id) tuples
                                      (a bare string in `active_codes` is
                                      treated as a SHARED account, entity_id
                                      NULL — preserves single-entity tests)
    2. _select_scope eligible       → list of Invoice rows
                                      (already pre-filtered for
                                      status NOT IN immutable AND
                                      vendor_id IS NOT NULL — that's
                                      the SQL's job now)
    3. _select_scope immutable count
    4. _select_scope no-vendor count
    5. _load_priors                 → list of (vendor_id, value) tuples
    """
    chart_rows = [c if isinstance(c, tuple) else (c, None) for c in active_codes]
    db = MagicMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.execute = _Stub(
        [
            _all_rows(chart_rows),
            _scalars_all(eligible_invoices),
            _scalar(skipped_immutable),
            _scalar(skipped_no_vendor),
            _all_rows([(vid, val) for vid, val in priors.items()]),
        ]
    )
    return db


# ---------- Eligibility ---------------------------------------------------


@pytest.mark.asyncio
async def test_immutable_status_count_surfaces_in_report():
    """Re-coding a posted/paid invoice would drift from the ERP.
    Eligibility is now SQL-side, so we mock the count query and assert
    it lands in `skipped_immutable` for the operator's report panel."""
    org_id = uuid.uuid4()
    inv_ready = _make_invoice(
        vendor_id=uuid.uuid4(), status=InvoiceStatus.ready_for_review, gl_account="6100"
    )

    db = _make_db_for(
        active_codes=["6100", "6200"],
        eligible_invoices=[inv_ready],  # SQL pre-filters paid/posted out
        priors={inv_ready.vendor_id: "6200"},
        skipped_immutable=2,  # the COUNT(*) query saw 2 immutable rows
    )

    report = await bulk_recode_gl(db, organization_id=org_id, filt=RecodeFilter(), dry_run=True)

    assert report.skipped_immutable == 2
    assert report.matched == 1
    assert len(report.changes) == 1


@pytest.mark.asyncio
async def test_no_vendor_count_surfaces_in_report():
    """Same shape as immutable — vendor-less invoices are filtered out
    in SQL, but the count is reported so the operator knows how many
    fell through that bucket."""
    org_id = uuid.uuid4()
    inv_with_vendor = _make_invoice(vendor_id=uuid.uuid4(), gl_account=None)

    db = _make_db_for(
        active_codes=["6100"],
        eligible_invoices=[inv_with_vendor],
        priors={inv_with_vendor.vendor_id: "6100"},
        skipped_no_vendor=1,
    )
    report = await bulk_recode_gl(db, organization_id=org_id, filt=RecodeFilter(), dry_run=True)

    assert report.skipped_no_vendor == 1
    assert report.matched == 1
    assert len(report.changes) == 1
    assert report.changes[0].source == "vendor_prior"


# ---------- Priors path --------------------------------------------------


@pytest.mark.asyncio
async def test_prior_applied_when_in_active_chart_and_changes_value():
    """Happy priors path: cached '6200', invoice has '6100', chart has
    both — record the change, increment by_source['vendor_prior']."""
    vendor_id = uuid.uuid4()
    org_id = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor_id, gl_account="6100")

    db = _make_db_for(
        active_codes=["6100", "6200"],
        eligible_invoices=[inv],
        priors={vendor_id: "6200"},
    )

    report = await bulk_recode_gl(db, organization_id=org_id, filt=RecodeFilter(), dry_run=False)

    assert len(report.changes) == 1
    change = report.changes[0]
    assert change.old_gl == "6100"
    assert change.new_gl == "6200"
    assert change.source == "vendor_prior"
    assert report.by_source["vendor_prior"] == 1
    assert inv.gl_account == "6200"  # persisted in non-dry-run mode
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_dry_run_does_not_mutate_invoice():
    """Belt-and-braces: dry_run=True must leave inv.gl_account
    untouched even though the change is reported."""
    vendor_id = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor_id, gl_account=None)

    db = _make_db_for(
        active_codes=["6100"],
        eligible_invoices=[inv],
        priors={vendor_id: "6100"},
    )
    report = await bulk_recode_gl(
        db, organization_id=uuid.uuid4(), filt=RecodeFilter(), dry_run=True
    )

    assert len(report.changes) == 1
    assert inv.gl_account is None  # not persisted
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_prior_skipped_when_already_matches_invoice():
    """No-op invoices shouldn't pollute the change list; they go in the
    skipped_no_change bucket so the operator can see total churn vs.
    actual movement."""
    vendor_id = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor_id, gl_account="6100")

    db = _make_db_for(
        active_codes=["6100"],
        eligible_invoices=[inv],
        priors={vendor_id: "6100"},
    )
    report = await bulk_recode_gl(
        db, organization_id=uuid.uuid4(), filt=RecodeFilter(), dry_run=True
    )

    assert report.skipped_no_change == 1
    assert report.changes == []


@pytest.mark.asyncio
async def test_prior_with_stale_code_skipped_or_routed_to_ai():
    """A cached value pointing at a now-deactivated GL must not be
    applied. With AI fallback off it's just skipped; with AI on it
    becomes an AI candidate."""
    vendor_id = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor_id, gl_account=None)

    db = _make_db_for(
        active_codes=["6100"],  # 5500 no longer active
        eligible_invoices=[inv],
        priors={vendor_id: "5500"},
    )
    report = await bulk_recode_gl(
        db,
        organization_id=uuid.uuid4(),
        filt=RecodeFilter(),
        dry_run=True,
        include_ai_fallback=False,
    )

    assert report.skipped_invalid_code == 1
    # The vendor HAS a learned code — it's just not live in the chart — so it
    # must not also be filed under "no learned code". One invoice, one bucket:
    # the skip counters have to reconcile against `matched`.
    assert report.skipped_no_prior_no_ai == 0
    assert report.changes == []
    assert (
        report.skipped_invalid_code + report.skipped_no_prior_no_ai + len(report.changes)
        == report.matched
    )


@pytest.mark.asyncio
async def test_no_prior_skipped_when_ai_fallback_disabled():
    vendor_id = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor_id, gl_account=None)

    db = _make_db_for(
        active_codes=["6100"],
        eligible_invoices=[inv],
        priors={},  # no prior for this vendor
    )
    report = await bulk_recode_gl(
        db,
        organization_id=uuid.uuid4(),
        filt=RecodeFilter(),
        dry_run=True,
        include_ai_fallback=False,
    )

    assert report.skipped_no_prior_no_ai == 1
    assert report.changes == []


@pytest.mark.asyncio
async def test_skip_buckets_partition_the_matched_set():
    """Every matched invoice lands in exactly ONE terminal bucket, so an
    operator can reconcile the report against `matched`. Previously an invoice
    whose prior was rejected by the chart was counted twice — once as
    `invalid_code` and again as `no_prior_no_ai`, the bucket the UI labels
    "no learned code" — so the panel added up to more invoices than it
    matched."""
    stale_vendor, absent_vendor, good_vendor, coded_vendor = (uuid.uuid4() for _ in range(4))
    inv_stale = _make_invoice(vendor_id=stale_vendor, gl_account=None, invoice_number="STALE")
    inv_absent = _make_invoice(vendor_id=absent_vendor, gl_account=None, invoice_number="ABSENT")
    inv_good = _make_invoice(vendor_id=good_vendor, gl_account=None, invoice_number="GOOD")
    inv_same = _make_invoice(vendor_id=coded_vendor, gl_account="6100", invoice_number="SAME")

    db = _make_db_for(
        active_codes=["6100", "6200"],
        eligible_invoices=[inv_stale, inv_absent, inv_good, inv_same],
        priors={
            stale_vendor: "5500",  # not in the chart
            good_vendor: "6200",  # applies
            coded_vendor: "6100",  # already coded → no change
        },
    )
    report = await bulk_recode_gl(
        db,
        organization_id=uuid.uuid4(),
        filt=RecodeFilter(),
        dry_run=True,
        include_ai_fallback=False,
    )

    assert report.matched == 4
    assert len(report.changes) == 1
    assert report.skipped_no_change == 1
    assert report.skipped_invalid_code == 1
    assert report.skipped_no_prior_no_ai == 1
    assert (
        len(report.changes)
        + report.skipped_no_change
        + report.skipped_invalid_code
        + report.skipped_no_prior_no_ai
        + report.skipped_ai_failed
        == report.matched
    )


@pytest.mark.asyncio
async def test_rejected_prior_is_an_ai_candidate_not_a_skip():
    """With AI fallback on, an out-of-chart prior is just "no usable prior" —
    the AI pass owns the invoice's disposition, so it is a candidate, not a
    terminal skip. Counting it as both inflated the panel."""
    stale_vendor = uuid.uuid4()
    inv = _make_invoice(vendor_id=stale_vendor, gl_account=None)

    db = _make_db_for(
        active_codes=["6100"],
        eligible_invoices=[inv],
        priors={stale_vendor: "5500"},
    )
    report = await bulk_recode_gl(
        db,
        organization_id=uuid.uuid4(),
        filt=RecodeFilter(),
        dry_run=True,
        include_ai_fallback=True,
        ai_runner=AsyncMock(),
    )

    assert report.matched == 1
    assert report.ai_candidates == 1
    assert report.skipped_invalid_code == 0
    assert report.skipped_no_prior_no_ai == 0


# ---------- AI fallback ---------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_with_ai_fallback_does_not_call_ai():
    """Critical honesty contract: dry-run + AI must NOT call the AI
    runner. `run_extraction` writes line items, vendor priors, RAG
    rows, audit entries, and may transition the invoice's status —
    none of that is cleanly reversible. A dry-run that secretly
    mutates the DB is worse than one that doesn't try.

    Operators get an `ai_candidates` count instead so they can decide
    whether to re-issue with `dry_run=false`.
    """
    vendor_with_prior = uuid.uuid4()
    vendor_without_prior = uuid.uuid4()
    inv_priored = _make_invoice(vendor_id=vendor_with_prior, gl_account=None)
    inv_no_prior = _make_invoice(vendor_id=vendor_without_prior, gl_account=None)

    ai_mock = AsyncMock()

    db = _make_db_for(
        active_codes=["6100", "6200"],
        eligible_invoices=[inv_priored, inv_no_prior],
        priors={vendor_with_prior: "6200"},
    )

    report = await bulk_recode_gl(
        db,
        organization_id=uuid.uuid4(),
        filt=RecodeFilter(),
        dry_run=True,
        include_ai_fallback=True,
        ai_runner=ai_mock,
    )

    assert ai_mock.await_count == 0, "dry-run must not call the AI runner"
    # The prior-covered invoice still produces a vendor_prior change.
    assert report.by_source == {"vendor_prior": 1, "ai": 0}
    # The no-prior invoice is reported as an AI candidate so the
    # operator can size the cost of a non-dry pass.
    assert report.ai_candidates == 1


@pytest.mark.asyncio
async def test_non_dry_run_invokes_ai_for_no_prior_invoices_only():
    """Cheap path is preferred — the AI runner should NOT run for an
    invoice the prior covers, even in non-dry mode. Locks the priors-
    first ordering so a refactor can't accidentally double-bill."""
    vendor_with_prior = uuid.uuid4()
    vendor_without_prior = uuid.uuid4()
    inv_priored = _make_invoice(vendor_id=vendor_with_prior, gl_account=None)
    inv_no_prior = _make_invoice(vendor_id=vendor_without_prior, gl_account=None)

    async def fake_ai_runner(db_, inv, *, actor_id, org_settings):
        inv.gl_account = "6100"

    ai_mock = AsyncMock(side_effect=fake_ai_runner)

    db = _make_db_for(
        active_codes=["6100", "6200"],
        eligible_invoices=[inv_priored, inv_no_prior],
        priors={vendor_with_prior: "6200"},
    )

    report = await bulk_recode_gl(
        db,
        organization_id=uuid.uuid4(),
        filt=RecodeFilter(),
        dry_run=False,
        include_ai_fallback=True,
        ai_runner=ai_mock,
    )

    assert ai_mock.await_count == 1
    assert ai_mock.await_args.args[1] is inv_no_prior
    sources = sorted(c.source for c in report.changes)
    assert sources == ["ai", "vendor_prior"]
    assert report.by_source == {"vendor_prior": 1, "ai": 1}
    assert report.ai_candidates == 0  # only populated in dry-run


@pytest.mark.asyncio
async def test_the_default_ai_runner_is_called_the_way_run_extraction_accepts():
    """The injected fakes accepted any kwargs, so nothing noticed the default
    runner was called with a `ctrl_db=` that `run_extraction` does not take:
    every real AI re-code raised TypeError and was reported as `ai_failed`.
    An autospec of the REAL function refuses a call it could not accept."""
    from unittest.mock import create_autospec

    from app.services import extraction

    vendor_id = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor_id, gl_account=None)
    db = _make_db_for(active_codes=["6100"], eligible_invoices=[inv], priors={})
    runner = create_autospec(extraction.run_extraction)

    with patch.object(extraction, "run_extraction", runner):
        report = await bulk_recode_gl(
            db,
            organization_id=uuid.uuid4(),
            filt=RecodeFilter(),
            dry_run=False,
            include_ai_fallback=True,
            ctrl_db=AsyncMock(),
        )

    assert runner.await_count == 1
    assert report.skipped_ai_failed == 0


@pytest.mark.asyncio
async def test_ai_fallback_failure_counted_not_raised():
    """One AI call going wrong must not abort the whole bulk run.
    Now that dry-run skips AI, this scenario only applies in non-dry
    mode."""
    vendor_id = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor_id, gl_account=None)

    async def boom(*_a, **_kw):
        raise RuntimeError("simulated quota exceeded")

    db = _make_db_for(active_codes=["6100"], eligible_invoices=[inv], priors={})

    report = await bulk_recode_gl(
        db,
        organization_id=uuid.uuid4(),
        filt=RecodeFilter(),
        dry_run=False,
        include_ai_fallback=True,
        ai_runner=AsyncMock(side_effect=boom),
    )
    assert report.skipped_ai_failed == 1
    assert report.changes == []


# ---------- Audit log ----------------------------------------------------


@pytest.mark.asyncio
async def test_non_dry_run_writes_audit_log_per_change(monkeypatch):
    """Every persisted change goes through `services.audit.log_action`
    so the activity is reflected on the invoice's history. SOC 2 cares
    about this — bulk operations on financially-relevant fields can't
    be invisible."""
    from app.services import audit

    log_calls: list[dict] = []

    async def fake_log(db_, **kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(audit, "log_action", fake_log)

    vendor_id = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor_id, gl_account=None)
    db = _make_db_for(
        active_codes=["6100"],
        eligible_invoices=[inv],
        priors={vendor_id: "6100"},
    )

    actor_id = uuid.uuid4()
    org_id = uuid.uuid4()
    await bulk_recode_gl(
        db,
        organization_id=org_id,
        filt=RecodeFilter(),
        dry_run=False,
        actor_id=actor_id,
    )

    assert len(log_calls) == 1
    call = log_calls[0]
    assert call["action"] == "invoice.gl_recoded"
    assert call["entity_type"] == "invoice"
    assert call["entity_id"] == inv.id
    assert call["actor_id"] == actor_id
    assert call["organization_id"] == org_id
    assert call["details"]["new_gl"] == "6100"
    assert call["details"]["source"] == "vendor_prior"


@pytest.mark.asyncio
async def test_dry_run_does_not_write_audit_log(monkeypatch):
    from app.services import audit

    log_calls: list[dict] = []

    async def fake_log(db_, **kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(audit, "log_action", fake_log)

    vendor_id = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor_id, gl_account=None)
    db = _make_db_for(
        active_codes=["6100"],
        eligible_invoices=[inv],
        priors={vendor_id: "6100"},
    )
    await bulk_recode_gl(
        db,
        organization_id=uuid.uuid4(),
        filt=RecodeFilter(),
        dry_run=True,
    )
    assert log_calls == []


# ---------- Report shape ------------------------------------------------


def test_report_dict_uses_would_change_in_dry_run_else_applied():
    """The frontend toggles its banner copy based on the key name.
    Don't change this shape without coordinating with the UI."""
    dry = RecodeReport(dry_run=True).as_dict()
    assert "would_change" in dry and "applied" not in dry
    assert "ai_candidates" in dry  # always present, even when 0

    wet = RecodeReport(dry_run=False).as_dict()
    assert "applied" in wet and "would_change" not in wet
    assert "ai_candidates" in wet


@pytest.mark.asyncio
async def test_audit_routes_through_dispatch_audit_not_raw_log_action(monkeypatch):
    """Recode audit rows must go through the mode-aware `dispatch_audit`
    chokepoint (so `lambda` audit mode ships them to SQS like every other
    mutation), not the low-level `log_action` shim directly."""
    from app.services import gl_recode as gl_recode_mod

    dispatch_calls: list[dict] = []

    async def fake_dispatch(db_, **kwargs):
        dispatch_calls.append(kwargs)

    # dispatch_audit is imported lazily inside bulk_recode_gl; patch it on its
    # own module so the lazy `from app.services.audit_dispatch import dispatch_audit`
    # resolves to the fake.
    import app.services.audit_dispatch as audit_dispatch_mod

    monkeypatch.setattr(audit_dispatch_mod, "dispatch_audit", fake_dispatch)

    vendor_id = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor_id, gl_account=None)
    db = _make_db_for(
        active_codes=["6100"],
        eligible_invoices=[inv],
        priors={vendor_id: "6100"},
    )
    await bulk_recode_gl(
        db,
        organization_id=uuid.uuid4(),
        filt=RecodeFilter(),
        dry_run=False,
        actor_id=uuid.uuid4(),
    )
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["action"] == "invoice.gl_recoded"
    assert dispatch_calls[0]["entity_id"] == inv.id
    assert "correlation_id" in dispatch_calls[0]
    _ = gl_recode_mod  # module referenced for clarity
