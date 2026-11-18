"""Operator-run backfill of `purchase_orders.currency` (`scripts/backfill_po_currency.py`).

The tool `docs/followups.md` named for POs that record no currency: migration
0099 left them NULL by design (`docs/decisions.md` §197), and with no PO edit
route nothing short of an ERP re-sync could ever give them one. The OPERATOR
asserts the currency for a named set; the tool infers nothing.

Coverage:

  * Pure `parse_currency` / `parse_po_prefix` / the parser — a malformed,
    unknown or no-money code is refused; a blank prefix is refused rather than
    read as "every PO"; dry run is the default.
  * Real Postgres — dry run writes nothing (no stamp, no audit row); `--apply`
    stamps only NULL rows inside the named set (entity, prefix, created-before);
    a PO that records a currency is never overwritten and is reported; a re-run
    is a no-op; the audit manifest is PII-free; and the matcher re-verifies in
    the same transaction — an invoice in another currency gets its
    `po_currency_mismatch` hold, an unpaid one only.
  * Multi-PO splits — a leg stamped in another currency re-matches the split
    invoice (found by `split_po_ids`, not its combined reference) and raises
    the hold in the same run; the dry run counts it (decisions §433, §437).
  * Matching only — the re-match never re-runs the org's LLM anomaly check
    (or any other non-PO rule); the PO match is the only thing a stamp moves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.entity import Entity
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.procurement import PurchaseOrder
from app.models.workflow import AuditLog
from app.services.po_matching import match_invoice_to_po
from scripts.backfill_po_currency import (
    AUDIT_ACTION,
    AssertionRefused,
    BackfillScope,
    backfill_tenant,
    build_parser,
    parse_currency,
    parse_po_prefix,
    resolve_entity,
    resolve_tenant,
)

TENANT = "a"
_CUTOVER = datetime(2026, 1, 15, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The operator's assertion
# ---------------------------------------------------------------------------


def test_currency_is_normalised():
    assert parse_currency(" usd ") == "USD"
    assert parse_currency("eur") == "EUR"


@pytest.mark.parametrize("raw", ["US", "USDD", "", "€UR", "U$D"])
def test_malformed_currency_is_refused(raw):
    with pytest.raises(AssertionRefused):
        parse_currency(raw)


def test_well_shaped_but_unknown_currency_is_refused():
    """Three letters no currency carries would be a label nobody can price."""
    with pytest.raises(AssertionRefused):
        parse_currency("ABC")


@pytest.mark.parametrize("raw", ["xxx", "xts"])  # lower case: normalised first
def test_codes_that_name_no_money_are_refused(raw):
    with pytest.raises(AssertionRefused):
        parse_currency(raw)


def test_blank_prefix_is_refused_not_read_as_every_po():
    assert parse_po_prefix(None) is None
    assert parse_po_prefix("DE-") == "DE-"
    with pytest.raises(AssertionRefused):
        parse_po_prefix("  ")


def test_currency_is_required_and_apply_defaults_off():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--tenant", "acme"])
    args = build_parser().parse_args(["--tenant", "acme", "--currency", "USD"])
    assert args.apply is False
    opted_in = build_parser().parse_args(["--tenant", "acme", "--currency", "USD", "--apply"])
    assert opted_in.apply is True


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------


async def _default_entity(s) -> uuid.UUID:
    return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _second_entity(s, org_id) -> uuid.UUID:
    ent = Entity(
        name="Second Co",
        slug=f"second-{uuid.uuid4().hex[:6]}",
        organization_id=org_id,
        is_default=False,
    )
    s.add(ent)
    await s.flush()
    return ent.id


async def _po(s, org_id, entity_id, number, *, currency=None, created_at=None) -> uuid.UUID:
    po = PurchaseOrder(
        po_number=number,
        total=Decimal("1000.00"),
        currency=currency,
        status="open",
        organization_id=org_id,
        entity_id=entity_id,
    )
    s.add(po)
    await s.flush()
    if created_at is not None:
        po.created_at = created_at  # server-defaulted; forced for the date bound
        await s.flush()
    return po.id


async def _currencies(mk) -> dict[uuid.UUID, str | None]:
    async with mk() as s:
        rows = (await s.execute(select(PurchaseOrder.id, PurchaseOrder.currency))).all()
    return dict(rows)


async def _run(mk, org_id, *, currency="USD", scope=None, apply=True, settings=None):
    async with mk() as db:
        result = await backfill_tenant(
            db,
            organization_id=org_id,
            tenant_slug="a",
            currency=currency,
            scope=scope or BackfillScope(),
            apply=apply,
            org_settings=settings if settings is not None else {},
        )
        await db.commit()
    return result


async def _audit_rows(mk) -> list[AuditLog]:
    async with mk() as s:
        return list(
            (await s.execute(select(AuditLog).where(AuditLog.action == AUDIT_ACTION)))
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_dry_run_is_the_default_and_writes_nothing(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity(s)
        po = await _po(s, org_id, ent, "PO-DRY")
        await s.commit()

    result = await _run(mk, org_id, apply=False)

    assert result.applied is False
    assert result.candidates == 1
    assert result.stamped == 0
    assert (await _currencies(mk))[po] is None
    assert await _audit_rows(mk) == []


@pytest.mark.asyncio
async def test_apply_stamps_only_null_rows_inside_the_named_set(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    before = _CUTOVER - timedelta(days=30)
    after = _CUTOVER + timedelta(days=1)
    async with mk() as s:
        ent = await _default_entity(s)
        other_ent = await _second_entity(s, org_id)
        inside = await _po(s, org_id, ent, "US-1", created_at=before)
        also_inside = await _po(s, org_id, ent, "US-2", created_at=before)
        wrong_prefix = await _po(s, org_id, ent, "DE-1", created_at=before)
        too_new = await _po(s, org_id, ent, "US-3", created_at=after)
        on_cutover = await _po(s, org_id, ent, "US-4", created_at=_CUTOVER)
        wrong_entity = await _po(s, org_id, other_ent, "US-5", created_at=before)
        await s.commit()

    scope = BackfillScope(entity_id=ent, po_prefix="US-", created_before=_CUTOVER)
    result = await _run(mk, org_id, scope=scope)

    assert result.stamped == 2
    assert sorted(result.stamped_po_ids) == sorted([str(inside), str(also_inside)])
    currencies = await _currencies(mk)
    assert currencies[inside] == currencies[also_inside] == "USD"
    for untouched in (wrong_prefix, too_new, on_cutover, wrong_entity):
        assert currencies[untouched] is None


@pytest.mark.asyncio
async def test_prefix_is_literal_not_a_like_pattern(realdb):
    """`%` / `_` in a prefix are characters, not wildcards."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity(s)
        literal = await _po(s, org_id, ent, "A_1")
        lookalike = await _po(s, org_id, ent, "AB1")
        await s.commit()

    await _run(mk, org_id, scope=BackfillScope(po_prefix="A_"))

    currencies = await _currencies(mk)
    assert currencies[literal] == "USD"
    assert currencies[lookalike] is None


@pytest.mark.asyncio
async def test_recorded_currency_is_never_overwritten_and_rerun_is_a_no_op(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity(s)
        recorded = await _po(s, org_id, ent, "PO-EUR", currency="EUR")
        unrecorded = await _po(s, org_id, ent, "PO-NONE")
        await s.commit()

    first = await _run(mk, org_id, currency="USD")
    second = await _run(mk, org_id, currency="USD")

    assert first.stamped == 1
    # The set's own codes are reported, so the operator sees a scope drawn wide.
    assert first.already_recorded == {"EUR": 1}
    assert second.candidates == 0
    assert second.stamped == 0
    assert second.already_recorded == {"EUR": 1, "USD": 1}
    currencies = await _currencies(mk)
    assert currencies[recorded] == "EUR"
    assert currencies[unrecorded] == "USD"
    assert len(await _audit_rows(mk)) == 1  # one stamping run, one manifest


@pytest.mark.asyncio
async def test_apply_writes_a_pii_free_audit_manifest(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity(s)
        po = await _po(s, org_id, ent, "PO-SECRET-NUMBER")
        await s.commit()

    await _run(mk, org_id, currency="GBP", scope=BackfillScope(po_prefix="PO-"))

    (row,) = await _audit_rows(mk)
    details = row.details
    assert row.actor_id is None
    assert details["asserted_currency"] == "GBP"
    assert "not inferred" in details["assertion"]
    assert details["scope"]["po_prefix"] == "PO-"
    assert details["pos_stamped"] == 1
    assert details["stamped_po_ids"] == [str(po)]
    # PO ids and counts only — never the PO number, a vendor or an amount.
    blob = str(details)
    assert "SECRET" not in blob
    assert "1000" not in blob


@pytest.mark.asyncio
async def test_stamp_re_verifies_matching_and_raises_a_currency_hold(realdb):
    """The point of the tool: the matcher stops reporting `unknown`, and an
    invoice in another currency gets its payment-blocking hold in the same
    transaction — not on some later edit. A paid invoice is left alone."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity(s)
        await _po(s, org_id, ent, "PO-LEGACY")

        def _inv(currency, status):
            return Invoice(
                invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
                vendor_name="Acme",
                amount=Decimal("1000.00"),
                currency=currency,
                po_number="PO-LEGACY",
                status=status,
                organization_id=org_id,
                entity_id=ent,
            )

        same = _inv("USD", InvoiceStatus.ready_for_review)
        other = _inv("EUR", InvoiceStatus.ready_for_review)
        paid = _inv("EUR", InvoiceStatus.paid)
        s.add_all([same, other, paid])
        await s.commit()
        ids = {"same": same.id, "other": other.id, "paid": paid.id}

        before = await match_invoice_to_po(s, other)
        assert before.currency_check == "unknown"

    dry = await _run(mk, org_id, apply=False)
    assert dry.invoices_rematched == 2  # the paid one is not counted

    result = await _run(mk, org_id)
    assert result.invoices_rematched == 2

    async with mk() as s:
        same_inv = await s.get(Invoice, ids["same"])
        other_inv = await s.get(Invoice, ids["other"])
        assert (await match_invoice_to_po(s, same_inv)).currency_check == "same"
        assert (await match_invoice_to_po(s, other_inv)).currency_check == "different"

        def _codes(inv):
            return [w.get("code") for w in (inv.warnings or [])]

        assert "po_currency_mismatch" in _codes(other_inv)
        assert "po_currency_mismatch" not in _codes(same_inv)
        assert other_inv.po_match["currency_check"] == "different"

        holds = (
            await s.execute(
                select(APException.invoice_id, func.count())
                .where(
                    APException.exception_type == "po_mismatch",
                    APException.status.in_(["open", "escalated"]),
                )
                .group_by(APException.invoice_id)
            )
        ).all()
    assert dict(holds) == {ids["other"]: 1}


@pytest.mark.asyncio
async def test_stamping_a_split_leg_re_matches_the_split_invoice(realdb):
    """A multi-PO split cites "PO-A,PO-B", which equals neither number; it is
    found by the legs it recorded. Stamping ONE leg EUR on a USD split raises
    the currency hold in the same run, and the dry run counts the invoice."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity(s)
        leg_a = await _po(s, org_id, ent, "SPLIT-A", currency="USD")
        leg_b = await _po(s, org_id, ent, "SPLIT-B")  # records no currency
        split = Invoice(
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Acme",
            amount=Decimal("2000.00"),
            currency="USD",
            po_number="SPLIT-A,SPLIT-B",
            split_po_ids=[str(leg_a), str(leg_b)],
            status=InvoiceStatus.ready_for_review,
            organization_id=org_id,
            entity_id=ent,
        )
        s.add(split)
        await s.commit()
        split_id = split.id
        before = await match_invoice_to_po(s, split)
        assert before.match_type == "multi-po-split"
        assert before.currency_check == "unknown"

    scope = BackfillScope(po_prefix="SPLIT-B")
    dry = await _run(mk, org_id, currency="EUR", scope=scope, apply=False)
    assert dry.candidates == 1
    assert dry.invoices_rematched == 1

    result = await _run(mk, org_id, currency="EUR", scope=scope)
    assert result.stamped_po_ids == [str(leg_b)]
    assert result.invoices_rematched == 1

    async with mk() as s:
        inv = await s.get(Invoice, split_id)
        assert inv.po_match["currency_check"] == "different"
        assert "po_currency_mismatch" in [w.get("code") for w in (inv.warnings or [])]
        holds = (
            await s.execute(
                select(func.count()).where(
                    APException.invoice_id == split_id,
                    APException.exception_type == "po_mismatch",
                    APException.status.in_(["open", "escalated"]),
                )
            )
        ).scalar()
    assert holds == 1


@pytest.mark.asyncio
async def test_rematch_is_matching_only_and_never_calls_the_llm(realdb, monkeypatch):
    """The stamp moves only the PO side of the match, so the re-match runs only
    the PO match: an org that opted into the LLM anomaly check pays no LLM call
    per re-matched invoice, and a warning the PO match does not own survives."""
    from app.models.vendor import Vendor
    from app.services import invoice_warnings
    from app.services.invoice_warning_catalog import warning

    calls: list[uuid.UUID] = []

    async def _recording_llm_check(db, invoice, *args, **kwargs):
        calls.append(invoice.id)

    monkeypatch.setattr(invoice_warnings, "_llm_anomaly_check", _recording_llm_check)
    settings = {"fraud_rules": {"llm_anomaly_enabled": True}}

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity(s)
        vendor = Vendor(name="Acme", organization_id=org_id, entity_id=ent)
        s.add(vendor)
        await s.flush()
        await _po(s, org_id, ent, "PO-LLM")
        inv = Invoice(
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Acme",
            vendor_id=vendor.id,
            amount=Decimal("1000.00"),
            currency="EUR",
            po_number="PO-LLM",
            status=InvoiceStatus.ready_for_review,
            organization_id=org_id,
            entity_id=ent,
            # A finding no PO match writes: a matching-only refresh keeps it.
            warnings=[warning("past_due", "warning")],
        )
        s.add(inv)
        await s.commit()
        inv_id = inv.id

        # The premise: this invoice is one the full refresh WOULD ask the LLM about.
        await invoice_warnings.refresh_warnings(s, inv, org_settings=settings)
        assert calls == [inv_id]
        await s.rollback()
    calls.clear()

    result = await _run(mk, org_id, currency="USD", settings=settings)

    assert result.invoices_rematched == 1
    assert calls == []
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        codes = [w.get("code") for w in (inv.warnings or [])]
    assert "po_currency_mismatch" in codes
    assert "past_due" in codes


def test_matching_only_refresh_rebuilds_exactly_the_po_match_warnings():
    """`refresh_po_match_only` drops and rebuilds the warnings typed
    `po_mismatch` / `quality_hold`; that must be exactly the codes
    `_refresh_po_match` writes, or it would erase another rule's finding (a
    code typed so elsewhere) or keep a stale PO one (a PO code typed otherwise)."""
    import ast
    import inspect
    import textwrap

    from app.services import invoice_warnings
    from app.services.invoice_warning_catalog import WARNING_SPECS

    tree = ast.parse(textwrap.dedent(inspect.getsource(invoice_warnings._refresh_po_match)))
    written = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "warning"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    typed = {
        spec.code
        for spec in WARNING_SPECS
        if spec.type in invoice_warnings.PO_MATCH_EXCEPTION_TYPES
    }
    assert written == typed


@pytest.mark.asyncio
async def test_resolvers_refuse_unknown_tenant_and_entity(realdb):
    org_id, db_name, settings = await resolve_tenant(realdb.info(TENANT).slug)
    assert org_id == realdb.info(TENANT).org_id
    assert db_name == realdb.info(TENANT).db_name
    assert isinstance(settings, dict)
    with pytest.raises(LookupError):
        await resolve_tenant(f"no-such-tenant-{uuid.uuid4().hex[:8]}")

    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = await _default_entity(s)
        slug = (await s.execute(select(Entity.slug).where(Entity.id == ent))).scalar_one()
        assert await resolve_entity(s, slug) == ent
        assert await resolve_entity(s, str(ent)) == ent
        with pytest.raises(AssertionRefused):
            await resolve_entity(s, "no-such-entity")
        with pytest.raises(AssertionRefused):
            await resolve_entity(s, str(uuid.uuid4()))
