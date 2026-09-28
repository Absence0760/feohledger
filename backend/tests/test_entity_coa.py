"""Multi-entity — entity-level chart of accounts (COA) in the two consumers
that previously loaded the chart by ``organization_id`` only.

The GL chart rule (``docs/multi-entity.md`` § Chart of accounts): a
``GLAccount`` with ``entity_id IS NULL`` is SHARED across every entity; one
with a set ``entity_id`` is entity-specific. An entity's *effective* chart is
``shared (NULL) ∪ its own``. This suite locks that semantics into the two
consumers wired in this change:

  1. ``services.gl_recode.bulk_recode_gl`` — a recode candidate (vendor prior)
     is valid for an invoice iff its code is in the invoice's effective chart.
     Because one bulk run spans invoices from different entities, validity is
     resolved *per-invoice-entity*: an entity-B-only code applies to a
     entity-B invoice but is rejected for an entity-A invoice; a shared code
     applies to both.

  2. ``services.extraction.run_extraction`` — the GL-catalog hint passed to the
     AI extractor is scoped to ``shared ∪ the invoice's entity``, never another
     entity's accounts; and when that effective ACTIVE chart is empty, a code
     that belongs only to ANOTHER entity's chart is still dropped from the
     header, the lines and a vendor-prior overlay (``docs/decisions.md`` §199).

Single-entity baseline: with one entity every account is either shared (NULL)
or under that one entity, so the scoping is a no-op — covered explicitly.

The ``bulk_recode_gl`` cases mock the DB session (hermetic, mirroring
``test_gl_recode.py``); the extraction cases run the real ``run_extraction``
against a real Postgres tenant via the ``realdb`` harness with only the adapter
swapped, so they assert the catalog the shipped query produced — not a copy of
that query kept here, which could never notice the shipped one changing.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.gl_account import GLAccount
from app.models.invoice import InvoiceStatus
from app.services.gl_recode import RecodeFilter, bulk_recode_gl

# ---------------------------------------------------------------------------
# Helpers — bulk_recode_gl DB-mock harness (mirrors test_gl_recode.py)
# ---------------------------------------------------------------------------


def _make_invoice(*, vendor_id, gl_account=None, entity_id=None, invoice_number="INV-1"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vendor_id=vendor_id,
        gl_account=gl_account,
        status=InvoiceStatus.ready_for_review,
        invoice_number=invoice_number,
        vendor_name="Acme Corp",
        invoice_date=None,
        entity_id=entity_id,
        warnings=None,
    )


class _Stub:
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
    obj = MagicMock()
    obj.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    return obj


def _all_rows(rows: list):
    obj = MagicMock()
    obj.all = MagicMock(return_value=rows)
    return obj


def _scalar(value):
    obj = MagicMock()
    obj.scalar = MagicMock(return_value=value)
    return obj


def _make_db_for(*, chart_rows, eligible_invoices, priors):
    """Sequence the SELECTs ``bulk_recode_gl`` issues.

    ``chart_rows`` is a list of ``(code, entity_id)`` tuples — exactly what the
    entity-aware ``_load_active_chart`` reads (``entity_id`` None = shared).
    """
    db = MagicMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.execute = _Stub(
        [
            _all_rows(list(chart_rows)),
            _scalars_all(eligible_invoices),
            _scalar(0),  # immutable count
            _scalar(0),  # no-vendor count
            _all_rows([(vid, val) for vid, val in priors.items()]),
        ]
    )
    return db


# ---------------------------------------------------------------------------
# bulk_recode_gl — per-invoice-entity validation of recode candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_specific_code_applies_only_to_its_own_entity():
    """A prior pointing at entity-B's own code applies to a entity-B invoice
    but is rejected (skipped_invalid_code) for a entity-A invoice — even though
    the code is live elsewhere in the org."""
    entity_a, entity_b = uuid.uuid4(), uuid.uuid4()
    vendor = uuid.uuid4()  # one vendor → one prior, two invoices in two entities

    inv_a = _make_invoice(
        vendor_id=vendor, gl_account="1000", entity_id=entity_a, invoice_number="A"
    )
    inv_b = _make_invoice(
        vendor_id=vendor, gl_account="1000", entity_id=entity_b, invoice_number="B"
    )

    # Chart: shared 1000; entity-B-only 6000.
    db = _make_db_for(
        chart_rows=[("1000", None), ("6000", entity_b)],
        eligible_invoices=[inv_a, inv_b],
        priors={vendor: "6000"},  # prior wants the entity-B-only code
    )

    report = await bulk_recode_gl(
        db, organization_id=uuid.uuid4(), filt=RecodeFilter(), dry_run=True
    )

    # Only the entity-B invoice gets the entity-B code; the entity-A invoice is
    # rejected as an invalid code.
    changed = {c.invoice_number for c in report.changes}
    assert changed == {"B"}
    assert report.changes[0].new_gl == "6000"
    assert report.skipped_invalid_code == 1  # the entity-A invoice
    # It HAS a learned code (just not one live in entity A's chart), so it is
    # not also counted as having none — one invoice, one bucket.
    assert report.skipped_no_prior_no_ai == 0


@pytest.mark.asyncio
async def test_shared_code_applies_across_entities():
    """A shared (entity_id NULL) code is valid for every entity's invoices."""
    entity_a, entity_b = uuid.uuid4(), uuid.uuid4()
    vendor = uuid.uuid4()

    inv_a = _make_invoice(vendor_id=vendor, gl_account="x", entity_id=entity_a, invoice_number="A")
    inv_b = _make_invoice(vendor_id=vendor, gl_account="x", entity_id=entity_b, invoice_number="B")

    db = _make_db_for(
        chart_rows=[("1000", None), ("6000", entity_b)],
        eligible_invoices=[inv_a, inv_b],
        priors={vendor: "1000"},  # prior wants the SHARED code
    )

    report = await bulk_recode_gl(
        db, organization_id=uuid.uuid4(), filt=RecodeFilter(), dry_run=True
    )

    assert {c.invoice_number for c in report.changes} == {"A", "B"}
    assert all(c.new_gl == "1000" for c in report.changes)
    assert report.skipped_invalid_code == 0


@pytest.mark.asyncio
async def test_single_entity_baseline_unchanged():
    """With one entity, every account is shared or under that entity, so the
    scoping is a no-op: an in-chart prior applies exactly as before."""
    only_entity = uuid.uuid4()
    vendor = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor, gl_account="6100", entity_id=only_entity)

    db = _make_db_for(
        chart_rows=[("6100", only_entity), ("6200", only_entity)],
        eligible_invoices=[inv],
        priors={vendor: "6200"},
    )

    report = await bulk_recode_gl(
        db, organization_id=uuid.uuid4(), filt=RecodeFilter(), dry_run=True
    )

    assert len(report.changes) == 1
    assert report.changes[0].new_gl == "6200"
    assert report.skipped_invalid_code == 0


@pytest.mark.asyncio
async def test_empty_chart_accepts_any_code_regardless_of_entity():
    """No active accounts at all → nothing to validate against, so a prior is
    accepted (pre-multi-entity behaviour preserved)."""
    vendor = uuid.uuid4()
    inv = _make_invoice(vendor_id=vendor, gl_account=None, entity_id=uuid.uuid4())

    db = _make_db_for(chart_rows=[], eligible_invoices=[inv], priors={vendor: "9999"})

    report = await bulk_recode_gl(
        db, organization_id=uuid.uuid4(), filt=RecodeFilter(), dry_run=True
    )

    assert len(report.changes) == 1
    assert report.changes[0].new_gl == "9999"
    assert report.skipped_invalid_code == 0


@pytest.mark.asyncio
async def test_empty_chart_is_resolved_per_invoice_entity_not_org_wide():
    """Mixed multi-entity tenant: entity A has neither its own accounts nor a
    shared one (its effective chart is empty), while entity B has its own
    chart. A's invoice accepts any code — exactly what `gl_chart` and
    extraction would do for the same invoice — while B's invoice is still
    validated against B's own chart in the SAME pass.

    Before the fix, `_ActiveChart.is_empty()` asked org-wide: because B's
    account makes the org non-empty, A's candidate would be validated against
    the org-wide chart and rejected as `skipped_invalid_code`, even though no
    account of A's own or shared exists to reject it against."""
    entity_a, entity_b = uuid.uuid4(), uuid.uuid4()
    vendor_a, vendor_b = uuid.uuid4(), uuid.uuid4()

    inv_a = _make_invoice(
        vendor_id=vendor_a, gl_account=None, entity_id=entity_a, invoice_number="A"
    )
    inv_b = _make_invoice(
        vendor_id=vendor_b, gl_account=None, entity_id=entity_b, invoice_number="B"
    )

    # Chart: entity_b owns 6000. No shared accounts, nothing for entity_a.
    db = _make_db_for(
        chart_rows=[("6000", entity_b)],
        eligible_invoices=[inv_a, inv_b],
        priors={vendor_a: "9999", vendor_b: "6000"},
    )

    report = await bulk_recode_gl(
        db, organization_id=uuid.uuid4(), filt=RecodeFilter(), dry_run=True
    )

    changed = {c.invoice_number: c.new_gl for c in report.changes}
    assert changed == {"A": "9999", "B": "6000"}
    assert report.skipped_invalid_code == 0


@pytest.mark.asyncio
async def test_empty_chart_for_one_entity_does_not_exempt_a_populated_one():
    """The other direction of the same mixed tenant: entity B (populated)
    still refuses an out-of-chart code for its own invoice, even though
    entity A — in the same bulk pass — has no chart at all and is accepting
    any code. Emptiness for A must not leak into B's validation."""
    entity_a, entity_b = uuid.uuid4(), uuid.uuid4()
    vendor_a, vendor_b = uuid.uuid4(), uuid.uuid4()

    inv_a = _make_invoice(
        vendor_id=vendor_a, gl_account=None, entity_id=entity_a, invoice_number="A"
    )
    inv_b = _make_invoice(
        vendor_id=vendor_b, gl_account=None, entity_id=entity_b, invoice_number="B"
    )

    # Chart: entity_b owns 6000. entity_a has nothing. Vendor B's prior points
    # at a code that isn't live in entity_b's chart at all.
    db = _make_db_for(
        chart_rows=[("6000", entity_b)],
        eligible_invoices=[inv_a, inv_b],
        priors={vendor_a: "9999", vendor_b: "7777"},
    )

    report = await bulk_recode_gl(
        db, organization_id=uuid.uuid4(), filt=RecodeFilter(), dry_run=True
    )

    changed = {c.invoice_number: c.new_gl for c in report.changes}
    assert changed == {"A": "9999"}
    assert report.skipped_invalid_code == 1  # entity B's invoice only


# ---------------------------------------------------------------------------
# extraction — the invoice's own chart, driven through `run_extraction` (realdb)
#
# These run the real `run_extraction` against a real multi-entity tenant with
# only the adapter swapped for one that returns chosen GL codes and records the
# config it was built from — so they assert the catalog the shipped query
# actually produced, not a copy of that query kept in this file.
# ---------------------------------------------------------------------------

_BYOK_MOCK = {"extraction": {"program_type": "byok", "provider": "mock"}}


class _GLAdapter:
    """An extraction adapter returning fixed GL codes. ``configs`` collects the
    config every ``get_extraction_adapter`` call was given."""

    provider_name = "mock"

    def __init__(self, *, vendor: str, suggested: str | None, lines: list[str | None]):
        self.vendor = vendor
        self.suggested = suggested
        self.lines = lines
        self.configs: list[dict] = []

    def factory(self, config: dict):
        self.configs.append(dict(config))
        return self

    async def extract(self, **_kwargs):
        from app.services.extraction_adapters.base import (
            ExtractedField,
            ExtractedLineItem,
            ExtractionResult,
        )

        return ExtractionResult(
            success=True,
            overall_confidence=0.9,
            vendor_name=ExtractedField(self.vendor, 0.95),
            invoice_number=ExtractedField("COA-1", 0.95),
            amount=ExtractedField("100.00", 0.95),
            suggested_gl_account=ExtractedField(self.suggested, 0.9 if self.suggested else 0.0),
            line_items=[
                ExtractedLineItem(
                    line_number=i + 1,
                    description=ExtractedField(f"Line {i + 1}", 0.9),
                    total=ExtractedField("50.00", 0.9),
                    gl_account=ExtractedField(gl, 0.9),
                )
                for i, gl in enumerate(self.lines)
            ],
            provider="mock",
        )

    def catalog_codes(self) -> set[str]:
        """Codes in the GL catalog handed to the adapter (empty: none sent)."""
        catalog = next(
            (c["gl_account_catalog"] for c in self.configs if "gl_account_catalog" in c), ""
        )
        return {line.split(" ", 1)[0] for line in catalog.splitlines()}


async def _default_entity_id(realdb, key: str = "a") -> uuid.UUID:
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _add_entity_b(realdb) -> uuid.UUID:
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        entity_b = Entity(
            organization_id=realdb.info("a").org_id,
            name="B Co",
            slug=f"b-co-{uuid.uuid4().hex[:6]}",
            is_default=False,
            is_active=True,
        )
        s.add(entity_b)
        await s.commit()
        return entity_b.id


async def _invoice_with_file(realdb, *, vendor: str, entity_id=None) -> uuid.UUID:
    import io

    headers = {"X-Entity-ID": str(entity_id)} if entity_id else {}
    async with realdb.client(key="a", role="admin") as c:
        created = await c.post(
            "/api/invoices",
            json={
                "invoice_number": f"COA-{uuid.uuid4().hex[:8]}",
                "vendor": vendor,
                "amount": "100.00",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        inv_id = created.json()["id"]
        attached = await c.post(
            f"/api/invoices/{inv_id}/file",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4 fixture"), "application/pdf")},
        )
        assert attached.status_code == 201, attached.text
    return uuid.UUID(inv_id)


async def _extract(realdb, inv_id, adapter: _GLAdapter):
    from unittest.mock import patch

    from app.models.invoice import Invoice, InvoiceLineItem
    from app.services.extraction import run_extraction

    mk = realdb.sessionmaker("a")
    with patch(
        "app.services.extraction_adapters.get_extraction_adapter", side_effect=adapter.factory
    ):
        async with mk() as s:
            inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
            await run_extraction(s, inv, org_settings=_BYOK_MOCK)
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        lines = list(
            (
                await s.execute(
                    select(InvoiceLineItem.gl_account)
                    .where(InvoiceLineItem.invoice_id == inv_id)
                    .order_by(InvoiceLineItem.line_number)
                )
            )
            .scalars()
            .all()
        )
    return inv, lines


def _gl_warnings(inv) -> list[dict]:
    return [w for w in (inv.warnings or []) if w.get("type") == "gl_account_invalid"]


async def test_extraction_gl_catalog_scopes_to_shared_union_entity(realdb):
    """The catalog hint for an invoice sees shared (NULL) ∪ its own entity's
    ACTIVE accounts — never another entity's, never a retired one."""
    org_id = realdb.info("a").org_id
    default_id = await _default_entity_id(realdb)
    b_id = await _add_entity_b(realdb)

    async with realdb.sessionmaker("a")() as s:
        s.add_all(
            [
                GLAccount(organization_id=org_id, code="1000", name="Shared Cash", entity_id=None),
                GLAccount(organization_id=org_id, code="6000", name="B Marketing", entity_id=b_id),
                GLAccount(
                    organization_id=org_id,
                    code="7000",
                    name="Default Travel",
                    entity_id=default_id,
                ),
                # Inactive shared account — never in the catalog.
                GLAccount(
                    organization_id=org_id,
                    code="9999",
                    name="Retired",
                    entity_id=None,
                    is_active=False,
                ),
            ]
        )
        await s.commit()

    catalogs = {}
    for label, entity_id in (("default", None), ("b", b_id)):
        inv_id = await _invoice_with_file(realdb, vendor=f"Catalog {label}", entity_id=entity_id)
        adapter = _GLAdapter(vendor=f"Catalog {label}", suggested=None, lines=[])
        await _extract(realdb, inv_id, adapter)
        catalogs[label] = adapter.catalog_codes()

    # Default entity: shared 1000 ∪ its own 7000 — NOT B's 6000, NOT retired 9999.
    assert catalogs["default"] == {"1000", "7000"}
    # Entity B: shared 1000 ∪ its own 6000 — NOT the default entity's 7000.
    assert catalogs["b"] == {"1000", "6000"}


async def test_extraction_gl_catalog_single_entity_unchanged(realdb):
    """Single-entity baseline: all accounts under the one (default) entity or
    shared → the catalog is the full active chart, exactly as before."""
    org_id = realdb.info("a").org_id
    default_id = await _default_entity_id(realdb)

    async with realdb.sessionmaker("a")() as s:
        s.add_all(
            [
                GLAccount(organization_id=org_id, code="1000", name="Cash", entity_id=None),
                GLAccount(organization_id=org_id, code="6100", name="Office", entity_id=default_id),
            ]
        )
        await s.commit()

    inv_id = await _invoice_with_file(realdb, vendor="Single Entity")
    adapter = _GLAdapter(vendor="Single Entity", suggested="6100", lines=["1000"])
    inv, lines = await _extract(realdb, inv_id, adapter)

    assert adapter.catalog_codes() == {"1000", "6100"}
    assert inv.gl_account == "6100"
    assert lines == ["1000"]
    assert _gl_warnings(inv) == []


async def _empty_own_chart_with_b_code(realdb) -> uuid.UUID:
    """The default entity has no active account and there are no shared ones;
    subsidiary B holds a live 6000. Returns B's id."""
    b_id = await _add_entity_b(realdb)
    async with realdb.sessionmaker("a")() as s:
        s.add(
            GLAccount(
                organization_id=realdb.info("a").org_id,
                code="6000",
                name="B Marketing",
                entity_id=b_id,
            )
        )
        await s.commit()
    return b_id


async def test_extraction_with_an_empty_own_chart_still_refuses_another_entitys_code(realdb):
    """The default entity's invoice has no active chart to validate against, so
    an unknown code is accepted as before — but B's 6000 would resolve against
    the default entity's chart as a different account, or none, and is dropped
    from the header and the lines with the usual warning (§194, §199)."""
    await _empty_own_chart_with_b_code(realdb)
    inv_id = await _invoice_with_file(realdb, vendor="Empty Chart Vendor")
    adapter = _GLAdapter(vendor="Empty Chart Vendor", suggested="6000", lines=["6000", "9999"])
    inv, lines = await _extract(realdb, inv_id, adapter)

    assert adapter.catalog_codes() == set(), "no active account in the invoice's chart"
    assert inv.gl_account is None
    assert lines == [None, "9999"]
    warnings = _gl_warnings(inv)
    assert [w["code"] for w in warnings] == ["gl_codes_not_in_chart"]
    assert warnings[0]["codes"] == ["6000"]


async def test_extraction_with_an_empty_own_chart_accepts_a_code_under_its_owner(realdb):
    """The same 6000 on B's own invoice is B's account — kept."""
    b_id = await _empty_own_chart_with_b_code(realdb)
    inv_id = await _invoice_with_file(realdb, vendor="Owner Vendor", entity_id=b_id)
    adapter = _GLAdapter(vendor="Owner Vendor", suggested="6000", lines=["6000"])
    inv, lines = await _extract(realdb, inv_id, adapter)

    assert inv.gl_account == "6000"
    assert lines == ["6000"]
    assert _gl_warnings(inv) == []


async def test_extraction_with_an_empty_own_chart_clears_a_prior_from_another_entity(realdb):
    """A vendor prior carrying B's code — learned while the vendor was coded
    for B — is overlaid on a default-entity invoice and must be cleared."""
    from app.models.invoice import Invoice
    from app.models.vendor_priors import VendorExtractionPrior

    await _empty_own_chart_with_b_code(realdb)
    inv_id = await _invoice_with_file(realdb, vendor="Prior Vendor")
    async with realdb.sessionmaker("a")() as s:
        vendor_id = (
            await s.execute(select(Invoice.vendor_id).where(Invoice.id == inv_id))
        ).scalar_one()
        assert vendor_id is not None
        s.add(VendorExtractionPrior(vendor_id=vendor_id, field_name="gl_account", value="6000"))
        await s.commit()

    adapter = _GLAdapter(vendor="Prior Vendor", suggested=None, lines=[])
    inv, _ = await _extract(realdb, inv_id, adapter)

    assert inv.gl_account is None
    warnings = _gl_warnings(inv)
    assert [w["code"] for w in warnings] == ["gl_code_stale_prior"]
    assert warnings[0]["codes"] == ["6000"]
