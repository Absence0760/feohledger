"""Critical-path PO matching — the seam where the *resolved* match rule
(`matching_rules.resolve_match_rule`) actually flows into the matcher
(`po_matching.match_invoice_to_po`) inside
`invoice_warnings._refresh_po_match`, plus the 2-way / 3-way match against
*real* Postgres rows.

Existing coverage:
  * `test_matching_rules.py` proves the resolver returns the right rule.
  * `test_po_matching_algorithm.py` proves the matcher math, with the DB mocked.
  * `test_po_matching_wiring.py` proves `_refresh_po_match` routes a *given*
    MatchResult to warnings/exceptions — but it patches `match_invoice_to_po`
    out entirely, so it never proves the org's per-vendor `tolerance_pct`
    setting reaches the matcher and changes the pay/no-pay verdict.

That last seam is this file's reason to exist: a per-vendor tolerance is the
control an org uses to say "trust this vendor to ±2%, flag anything above" — a
regression that dropped `rule.tolerance_pct` (or passed the org default
regardless of vendor) would silently let out-of-policy variances clear the
matched gate. The integration is verified two ways:

  1. unit — real resolver + real matcher (DB mocked at the row level), driving
     `_refresh_po_match` so an invoice that is *matched* under the org default
     5% becomes *mismatch* under a vendor-specific 2% rule (and vice-versa);
  2. realdb — a real PO + invoice + GR exercise the matcher's actual SQL
     (po_number + vendor scoping, partial-receipt downgrade) end-to-end.

Money is `Decimal` throughout.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import invoice_warnings
from app.services.matching_rules import resolve_match_rule
from app.services.po_matching import match_invoice_to_po

# ---------------------------------------------------------------------------
# Helpers — a row-level mock DB for the matcher (PO lookup, GR lookup, then
# the 4-way inspection lookup). Mirrors test_po_matching_algorithm._mk_db.
# ---------------------------------------------------------------------------


def _mk_db(*, po=None, gr=None, inspection=None):
    # PO lookup, GR lookup, then the inspection leg — which can issue TWO
    # queries (GR-scoped, then the PO-level fallback). Serve the inspection
    # result for every call past the first two; see the fuller note on the
    # matching helper in `test_po_matching_algorithm._mk_db`.
    po_res = MagicMock()
    po_res.scalar_one_or_none = MagicMock(return_value=po)
    # The GR leg fetches ALL receipts via `.scalars().all()` now.
    gr_res = MagicMock()
    gr_scalars = MagicMock()
    gr_scalars.all = MagicMock(return_value=[gr] if gr is not None else [])
    gr_res.scalars = MagicMock(return_value=gr_scalars)
    insp_res = MagicMock()
    insp_res.scalar_one_or_none = MagicMock(return_value=inspection)

    fixed = [po_res, gr_res]
    calls = {"n": 0}

    async def _execute(*_args, **_kwargs):
        i = calls["n"]
        calls["n"] += 1
        return fixed[i] if i < len(fixed) else insp_res

    db = AsyncMock()
    db.execute = _execute
    return db


def _po(*, total: Decimal, vendor_id=None, line_items=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        po_number="PO-100",
        total=total,
        vendor_id=vendor_id,
        line_items=line_items or [],
    )


def _invoice(*, amount: Decimal, vendor_id=None, gl_account=None, currency="USD"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        amount=amount,
        # Read by the money-bearing warning sentences, which name the invoice's
        # own currency rather than a hardcoded `$` (decisions.md §157).
        currency=currency,
        po_number="PO-100",
        vendor_id=vendor_id,
        gl_account=gl_account,
        po_match=None,
        contract_id=None,
        status=SimpleNamespace(value="ready_for_review"),
    )


async def _refresh(db, inv, warnings, org_settings):
    """Drive `_refresh_po_match` with `_ensure_exception` patched out.

    The exception-row creation is covered by `test_po_matching_wiring.py`; here
    we only need the resolver→matcher tolerance flow reflected on
    `invoice.po_match` + `warnings`, and patching the exception writer keeps the
    mock DB's `execute` side-effect queue (PO / GR / inspection) from being
    consumed by `_ensure_exception`'s own duplicate-check query.
    """
    with patch.object(invoice_warnings, "_ensure_exception", AsyncMock()):
        await invoice_warnings._refresh_po_match(db, inv, warnings, org_settings=org_settings)


# ===========================================================================
# Resolver → matcher integration (the gap): the org's per-vendor / per-commodity
# tolerance must actually change the matched/mismatch verdict.
# ===========================================================================


@pytest.mark.asyncio
async def test_vendor_tolerance_tightens_gate_via_refresh_po_match():
    """An invoice +4% over its PO is *matched* under the org default 5%, but a
    vendor-specific 2% rule must flip it to *mismatch* — proving
    `_refresh_po_match` resolves the vendor rule AND hands its tolerance to the
    matcher. If the wiring dropped `rule.tolerance_pct`, the verdict would stay
    `matched` and an out-of-policy variance would clear the gate."""
    vendor_id = uuid.uuid4()
    inv = _invoice(amount=Decimal("1040.00"), vendor_id=vendor_id)
    db = _mk_db(po=_po(total=Decimal("1000.00"), vendor_id=vendor_id))

    org_settings = {
        "matching": {
            "tolerance_pct": 5.0,  # org default would pass +4%
            "vendor_rules": {str(vendor_id): {"tolerance_pct": 2.0}},  # but this vendor: 2%
        }
    }
    # Sanity: the resolver alone picks the 2% vendor rule.
    rule = resolve_match_rule(org_settings, vendor_id=vendor_id, gl_account=None)
    assert rule.tolerance_pct == 2.0

    warnings: list[dict] = []
    await _refresh(db, inv, warnings, org_settings)

    # +4% > 2% → mismatch persisted + a po_mismatch warning raised.
    assert inv.po_match["status"] == "mismatch"
    assert inv.po_match["within_tolerance"] is False
    assert inv.po_match["details"]["tolerance_pct"] == 2.0
    assert any(w["type"] == "po_mismatch" and w["severity"] == "warning" for w in warnings)


@pytest.mark.asyncio
async def test_vendor_tolerance_widens_gate_via_refresh_po_match():
    """The mirror: an invoice +9% over its PO is a *mismatch* under the org
    default 5%, but a vendor-specific 10% rule must clear it to *matched*. This
    pins that the resolved tolerance is used in BOTH directions, not just to
    tighten."""
    vendor_id = uuid.uuid4()
    inv = _invoice(amount=Decimal("1090.00"), vendor_id=vendor_id)
    db = _mk_db(po=_po(total=Decimal("1000.00"), vendor_id=vendor_id))

    org_settings = {
        "matching": {
            "tolerance_pct": 5.0,
            "vendor_rules": {str(vendor_id): {"tolerance_pct": 10.0}},
        }
    }
    warnings: list[dict] = []
    await _refresh(db, inv, warnings, org_settings)

    assert inv.po_match["status"] == "matched"
    assert inv.po_match["within_tolerance"] is True
    assert inv.po_match["details"]["tolerance_pct"] == 10.0
    assert warnings == []


@pytest.mark.asyncio
async def test_commodity_tolerance_applies_when_no_vendor_rule():
    """No vendor rule, but the invoice's GL account (commodity) carries a tight
    1% rule. `_refresh_po_match` passes `invoice.gl_account` to the resolver, so
    a +4% invoice that the org default 5% would pass must mismatch under the
    commodity rule. A regression that forgot to forward `gl_account` would let
    it slip through."""
    inv = _invoice(amount=Decimal("1040.00"), vendor_id=None, gl_account="6000")
    db = _mk_db(po=_po(total=Decimal("1000.00")))

    org_settings = {
        "matching": {
            "tolerance_pct": 5.0,
            "commodity_rules": {"6000": {"tolerance_pct": 1.0}},
        }
    }
    warnings: list[dict] = []
    await _refresh(db, inv, warnings, org_settings)

    assert inv.po_match["status"] == "mismatch"
    assert inv.po_match["details"]["tolerance_pct"] == 1.0


@pytest.mark.asyncio
async def test_org_default_tolerance_used_when_no_per_entity_rule():
    """With no vendor/commodity overrides, the org-level tolerance (here a tight
    3%) reaches the matcher. +4% must mismatch at 3%."""
    inv = _invoice(amount=Decimal("1040.00"))
    db = _mk_db(po=_po(total=Decimal("1000.00")))

    org_settings = {"matching": {"tolerance_pct": 3.0}}
    warnings: list[dict] = []
    await _refresh(db, inv, warnings, org_settings)

    assert inv.po_match["status"] == "mismatch"
    assert inv.po_match["details"]["tolerance_pct"] == 3.0


@pytest.mark.asyncio
async def test_no_settings_falls_back_to_hardcoded_5pct_in_refresh():
    """No `matching` settings at all → the hardcoded 5% default reaches the
    matcher. +4% clears (matched), +6% does not (mismatch) — pinning the
    fallback value end-to-end through the resolver+matcher path."""
    # +4% under default 5% → matched.
    inv_ok = _invoice(amount=Decimal("1040.00"))
    db_ok = _mk_db(po=_po(total=Decimal("1000.00")))
    warnings_ok: list[dict] = []
    await _refresh(db_ok, inv_ok, warnings_ok, None)
    assert inv_ok.po_match["status"] == "matched"
    assert inv_ok.po_match["details"]["tolerance_pct"] == 5.0
    assert warnings_ok == []

    # +6% over the same PO → mismatch under the same default.
    inv_bad = _invoice(amount=Decimal("1060.00"))
    db_bad = _mk_db(po=_po(total=Decimal("1000.00")))
    warnings_bad: list[dict] = []
    await _refresh(db_bad, inv_bad, warnings_bad, None)
    assert inv_bad.po_match["status"] == "mismatch"


@pytest.mark.asyncio
async def test_po_match_is_jsonb_serialisable_after_refresh():
    """`invoice.po_match` is a JSONB column — `_refresh_po_match` stores
    `asdict(MatchResult)`. Confirm the persisted dict is plain JSON-friendly
    types (no Decimal / enum / datetime leaked), since a Decimal here would 500
    the asyncpg JSONB encode on save."""
    inv = _invoice(amount=Decimal("1000.00"))
    db = _mk_db(po=_po(total=Decimal("1000.00")))
    await _refresh(db, inv, [], {})

    stored = inv.po_match
    assert isinstance(stored, dict)
    # Every numeric is a float/int, every string is a str, no Decimal anywhere.
    for key in ("po_total", "amount_variance", "amount_variance_pct"):
        assert isinstance(stored[key], (int, float))
    assert not any(isinstance(v, Decimal) for v in stored.values())
    # asdict on a fresh result yields the same key set the stored dict has.
    from app.services.po_matching import MatchResult

    assert set(stored.keys()) == set(asdict(MatchResult()).keys())


# ===========================================================================
# 2-way / 3-way against REAL Postgres rows — exercises the matcher's actual
# SQL filters (po_number + vendor scoping) and the partial-receipt downgrade.
# The algorithm tests mock the DB, so the live WHERE clauses are unverified
# there. realdb skips cleanly when Postgres isn't up.
# ===========================================================================


async def _default_entity_id(session) -> uuid.UUID:
    from sqlalchemy import select

    from app.models.entity import Entity

    return (await session.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


async def _add_po(session, org_id, entity_id, *, po_number, total, vendor_id=None, lines=None):
    from app.models.procurement import POLineItem, PurchaseOrder

    po = PurchaseOrder(
        po_number=po_number,
        total=Decimal(total),
        status="open",
        organization_id=org_id,
        entity_id=entity_id,
        vendor_id=vendor_id,
    )
    session.add(po)
    await session.flush()
    for qty in lines or []:
        session.add(POLineItem(po_id=po.id, description="Item", quantity=Decimal(qty)))
    await session.flush()
    return po


async def _add_gr(session, org_id, entity_id, po_id, *, received=None):
    from app.models.procurement import GoodsReceipt, GRLineItem

    gr = GoodsReceipt(
        gr_number=f"GR-{uuid.uuid4().hex[:8]}",
        po_id=po_id,
        status="received",
        organization_id=org_id,
        entity_id=entity_id,
    )
    session.add(gr)
    await session.flush()
    for qty in received or []:
        session.add(GRLineItem(gr_id=gr.id, description="Item", quantity_received=Decimal(qty)))
    await session.flush()
    return gr


async def _add_invoice(session, org_id, entity_id, *, po_number, amount, vendor_id=None):
    from app.models.invoice import Invoice

    inv = Invoice(
        invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
        vendor_name="Acme",
        amount=Decimal(amount),
        po_number=po_number,
        organization_id=org_id,
        entity_id=entity_id,
        vendor_id=vendor_id,
    )
    session.add(inv)
    await session.flush()
    return inv


@pytest.mark.asyncio
async def test_realdb_two_way_match_within_and_outside_tolerance(realdb):
    """Two real invoices against the same real PO: +4% clears the default 5%
    gate (matched), +10% does not (mismatch). Proves the matcher's live SQL PO
    lookup + variance math against actual rows."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        await _add_po(s, org_id, ent, po_number="PO-2WAY", total="1000.00")
        inv_ok = await _add_invoice(s, org_id, ent, po_number="PO-2WAY", amount="1040.00")
        inv_bad = await _add_invoice(s, org_id, ent, po_number="PO-2WAY", amount="1100.00")
        await s.commit()

        ok = await match_invoice_to_po(s, inv_ok)
        bad = await match_invoice_to_po(s, inv_bad)

    assert ok.match_type == "2-way"
    assert ok.status == "matched"
    assert ok.amount_variance_pct == pytest.approx(4.0)
    assert bad.status == "mismatch"
    assert bad.amount_variance_pct == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_realdb_po_lookup_is_vendor_scoped(realdb):
    """Two real POs share po_number `PO-DUP` but belong to different vendors.
    An invoice carrying vendor_id V1 must match V1's PO (total 1000), NOT V2's
    (total 9999) — the vendor-scoping WHERE clause is the real isolation here.
    Matching the wrong PO would compare against the wrong amount and either pass
    a fraudulent invoice or flag a valid one."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        ent = await _default_entity_id(s)
        # Real vendor rows so the FK is satisfiable.
        from app.models.vendor import Vendor

        s.add(
            Vendor(
                id=v1,
                name="Vendor One",
                organization_id=org_id,
                entity_id=ent,
                status="active",
                source="manual",
            )
        )
        s.add(
            Vendor(
                id=v2,
                name="Vendor Two",
                organization_id=org_id,
                entity_id=ent,
                status="active",
                source="manual",
            )
        )
        await s.flush()
        await _add_po(s, org_id, ent, po_number="PO-DUP", total="1000.00", vendor_id=v1)
        await _add_po(s, org_id, ent, po_number="PO-DUP", total="9999.00", vendor_id=v2)
        inv = await _add_invoice(s, org_id, ent, po_number="PO-DUP", amount="1000.00", vendor_id=v1)
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    # Must have matched V1's $1000 PO (variance 0), never V2's $9999.
    assert match.po_total == 1000.0
    assert match.status == "matched"
    assert match.amount_variance == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_realdb_partial_receipt_downgrades_matched_to_partial(realdb):
    """Real PO ordered 10, real GR received 6 → the 3-way leg downgrades a
    within-tolerance match from `matched` to `partial`, with the percentage in
    the issue text. Pins the live GR quantity comparison."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(
            s, org_id, ent, po_number="PO-PARTIAL", total="1000.00", lines=["10.0000"]
        )
        await _add_gr(s, org_id, ent, po.id, received=["6.0000"])
        inv = await _add_invoice(s, org_id, ent, po_number="PO-PARTIAL", amount="1000.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.match_type == "3-way"
    assert match.status == "partial"
    assert match.gr_id is not None
    assert any("60%" in i and "Partial" in i for i in match.issues)


@pytest.mark.asyncio
async def test_realdb_missing_po_reports_no_po(realdb):
    """An invoice citing a PO that doesn't exist in real Postgres → status
    `no_po` with the missing PO number in the issue. Confirms the live empty
    lookup path, not just the mocked one."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        inv = await _add_invoice(s, org_id, ent, po_number="PO-DOES-NOT-EXIST", amount="500.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.status == "no_po"
    assert any("PO-DOES-NOT-EXIST" in i for i in match.issues)


@pytest.mark.asyncio
async def test_realdb_multiple_goods_receipts_aggregate_received_qty(realdb):
    """A PO with TWO goods receipts (the normal partial-delivery case: a PO is
    filled by several shipments, each a separate GR) must not crash the matcher
    AND must aggregate received quantity across every GR. The GR lookup selected
    a single row via scalar_one_or_none() with no LIMIT, so a second GR raised
    MultipleResultsFound — taking down PO matching and the whole refresh_warnings
    pipeline on every mutation of such an invoice. With the fix, 6 + 4 of 10
    ordered = fully received → `matched`, not falsely `partial`."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(
            s, org_id, ent, po_number="PO-MULTIGR", total="1000.00", lines=["10.0000"]
        )
        # Two separate receipts against the same PO (6 then 4 of the 10 ordered).
        await _add_gr(s, org_id, ent, po.id, received=["6.0000"])
        await _add_gr(s, org_id, ent, po.id, received=["4.0000"])
        inv = await _add_invoice(s, org_id, ent, po_number="PO-MULTIGR", amount="1000.00")
        await s.commit()

        # Must not raise MultipleResultsFound.
        match = await match_invoice_to_po(s, inv)

    assert match.match_type == "3-way"
    assert match.gr_id is not None
    # 6 + 4 == 10 ordered → fully received across both GRs → matched.
    assert match.status == "matched"
    assert not any("Partial receipt" in i for i in match.issues)


@pytest.mark.asyncio
async def test_realdb_multiple_goods_receipts_partial_sum_downgrades(realdb):
    """Two GRs that together still fall short of the ordered quantity (6 + 2 of
    10) downgrade to `partial` — proving the aggregation counts every GR, not
    just the newest (the newest alone would be 2 → a different percentage)."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(
            s, org_id, ent, po_number="PO-MULTIGR-SHORT", total="1000.00", lines=["10.0000"]
        )
        await _add_gr(s, org_id, ent, po.id, received=["6.0000"])
        await _add_gr(s, org_id, ent, po.id, received=["2.0000"])
        inv = await _add_invoice(s, org_id, ent, po_number="PO-MULTIGR-SHORT", amount="1000.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.match_type == "3-way"
    assert match.status == "partial"
    # 6 + 2 == 8 of 10 → 80% received (not 20% from the newest GR alone).
    assert any("80%" in i and "Partial receipt" in i for i in match.issues)


@pytest.mark.asyncio
async def test_realdb_fractional_quantities_sum_exactly_not_float(realdb):
    """A PO ordering 30.3 units, fully received via 3 shipments of 10.1 each,
    must be `matched` — not `partial` (issue #140). Summing via `float(...)`
    produces the classic binary-float artefact (10.1 * 3 == 30.299999999999997
    < 30.3), flipping a fully-received match to `partial` and emitting the
    self-contradictory 'Partial receipt: 100% of ordered quantity received'."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(
            s, org_id, ent, po_number="PO-FRACTIONAL", total="1000.00", lines=["30.3000"]
        )
        for _ in range(3):
            await _add_gr(s, org_id, ent, po.id, received=["10.1000"])
        inv = await _add_invoice(s, org_id, ent, po_number="PO-FRACTIONAL", amount="1000.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.match_type == "3-way"
    assert match.status == "matched"
    assert not any("Partial receipt" in i for i in match.issues)


@pytest.mark.asyncio
async def test_realdb_duplicate_po_number_no_vendor_does_not_crash(realdb):
    """An invoice with NO vendor_id citing a po_number shared by two POs (e.g.
    two entities / a re-used number) must not crash. The unscoped PO lookup used
    scalar_one_or_none() with no LIMIT, so two same-numbered POs raised
    MultipleResultsFound instead of degrading to a safe verdict."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        await _add_po(s, org_id, ent, po_number="PO-DUPNUM", total="1000.00")
        await _add_po(s, org_id, ent, po_number="PO-DUPNUM", total="2000.00")
        # Invoice has NO vendor_id, so the PO lookup is NOT vendor-scoped.
        inv = await _add_invoice(s, org_id, ent, po_number="PO-DUPNUM", amount="1000.00")
        await s.commit()

        # Must not raise MultipleResultsFound.
        match = await match_invoice_to_po(s, inv)

    # A deterministic single PO is chosen; matching still returns a verdict.
    assert match.po_number == "PO-DUPNUM"
    assert match.status in ("matched", "mismatch")


# ---------------------------------------------------------------------------
# Entity scoping — a PO belongs to exactly ONE subsidiary
# ---------------------------------------------------------------------------


async def _make_entity(session, org_id, slug):
    from app.models.entity import Entity

    eid = uuid.uuid4()
    session.add(
        Entity(
            id=eid,
            organization_id=org_id,
            name=slug.title(),
            slug=slug,
            is_default=False,
            is_active=True,
        )
    )
    await session.flush()
    return eid


@pytest.mark.asyncio
async def test_realdb_po_lookup_is_entity_scoped(realdb):
    """Two subsidiaries each number a PO `PO-1001` and share a supplier. The
    lookup picks `created_at DESC LIMIT 1`, so entity A's invoice used to match
    entity B's NEWER PO and read `matched` — the amount control silently passed
    against a different subsidiary's order."""
    from app.models.vendor import Vendor

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent_a = await _default_entity_id(s)
        ent_b = await _make_entity(s, org_id, f"sub-b-{uuid.uuid4().hex[:6]}")
        vendor_id = uuid.uuid4()
        s.add(Vendor(id=vendor_id, organization_id=org_id, name="Shared Supplier"))
        await s.flush()

        # A's PO first, B's second — B's is the newer row the LIMIT 1 would take.
        await _add_po(s, org_id, ent_a, po_number="PO-1001", total="1000.00", vendor_id=vendor_id)
        await _add_po(s, org_id, ent_b, po_number="PO-1001", total="9999.00", vendor_id=vendor_id)
        inv_a = await _add_invoice(
            s, org_id, ent_a, po_number="PO-1001", amount="1000.00", vendor_id=vendor_id
        )
        await s.commit()

        match = await match_invoice_to_po(s, inv_a)

    assert match.status == "matched", match.issues
    assert match.po_total == Decimal("1000.00")


@pytest.mark.asyncio
async def test_realdb_po_in_another_entity_is_not_found(realdb):
    """A PO booked under a sibling subsidiary must be invisible, not a match —
    `no_po` is the honest answer a clerk can act on."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent_a = await _default_entity_id(s)
        ent_b = await _make_entity(s, org_id, f"sub-c-{uuid.uuid4().hex[:6]}")
        await _add_po(s, org_id, ent_b, po_number="PO-OTHER-ENTITY", total="500.00")
        inv_a = await _add_invoice(s, org_id, ent_a, po_number="PO-OTHER-ENTITY", amount="500.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv_a)

    assert match.status == "no_po"
    assert match.po_id is None


@pytest.mark.asyncio
async def test_realdb_goods_receipt_lookup_is_entity_scoped(realdb):
    """A receipt booked under another subsidiary must not satisfy this
    invoice's 3-way leg."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent_a = await _default_entity_id(s)
        ent_b = await _make_entity(s, org_id, f"sub-d-{uuid.uuid4().hex[:6]}")
        po = await _add_po(s, org_id, ent_a, po_number="PO-GR-SCOPE", total="1000.00", lines=["10"])
        # Receipt booked under the WRONG entity — it must be ignored entirely.
        await _add_gr(s, org_id, ent_b, po.id, received=["10"])
        inv = await _add_invoice(s, org_id, ent_a, po_number="PO-GR-SCOPE", amount="1000.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.match_type == "2-way", "a sibling entity's GR must not open the 3-way leg"
    assert match.gr_id is None


@pytest.mark.asyncio
async def test_realdb_entityless_invoice_still_matches(realdb):
    """A NULL `entity_id` invoice (pre-multi-entity / unstamped) stays unscoped,
    so single-entity tenants and legacy rows are unchanged."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        await _add_po(s, org_id, ent, po_number="PO-LEGACY", total="750.00")
        inv = await _add_invoice(s, org_id, None, po_number="PO-LEGACY", amount="750.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.status == "matched"
    assert match.po_total == Decimal("750.00")


# ===========================================================================
# 4-way leg — the inspection lookup must not go blind once a GR exists.
#
# `qms_sync` writes a PO-level inspection (`gr_id` NULL) whenever the QMS knows
# the PO number but not the GR number, and `POST /api/inspections` accepts a
# PO-only body. The lookup used to query the GR-scoped row *instead of* the
# PO-level one whenever any receipt was booked, so those rows became invisible —
# and the control failed OPEN in the worst direction: a `fail` verdict on
# rejected goods never reached the matcher and the invoice read a clean 3-way
# `matched`.
# ===========================================================================


async def _add_inspection(session, org_id, *, po_id, gr_id, result, notes=None):
    from app.models.quality_inspection import QualityInspection

    qi = QualityInspection(
        inspection_number=f"QI-{uuid.uuid4().hex[:8]}",
        po_id=po_id,
        gr_id=gr_id,
        result=result,
        deviation_notes=notes,
        organization_id=org_id,
    )
    session.add(qi)
    await session.flush()
    return qi


@pytest.mark.asyncio
async def test_realdb_po_level_failed_inspection_is_seen_when_a_gr_exists(realdb):
    """The fail-open case: rejected goods must never read as a clean match."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number="PO-INSP-FAILOPEN", total="1000.00")
        await _add_gr(s, org_id, ent, po.id)
        await _add_inspection(
            s, org_id, po_id=po.id, gr_id=None, result="fail", notes="Goods rejected"
        )
        inv = await _add_invoice(s, org_id, ent, po_number="PO-INSP-FAILOPEN", amount="1000.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.match_type == "4-way"
    assert match.inspection_result == "fail"
    assert match.status == "mismatch"
    assert any(i.startswith("Failed quality") for i in match.issues)


@pytest.mark.asyncio
async def test_realdb_receipt_inspection_wins_over_the_po_level_one(realdb):
    """Precedence is unchanged — the receipt's own verdict is the specific one."""
    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number="PO-INSP-PREC", total="1000.00")
        gr = await _add_gr(s, org_id, ent, po.id)
        await _add_inspection(s, org_id, po_id=po.id, gr_id=None, result="fail")
        gr_qi = await _add_inspection(s, org_id, po_id=po.id, gr_id=gr.id, result="pass")
        inv = await _add_invoice(s, org_id, ent, po_number="PO-INSP-PREC", amount="1000.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.inspection_id == str(gr_qi.id)
    assert match.inspection_result == "pass"
    assert match.status == "matched"


@pytest.mark.asyncio
async def test_realdb_another_shipments_inspection_does_not_stand_in(realdb):
    """The fallback is restricted to genuinely PO-level rows (`gr_id` NULL).

    A different shipment's inspection also carries this `po_id`; letting it
    substitute would swap one masking bug for another — shipment 2 passed, so
    shipment 1 reads inspected."""
    from datetime import UTC, datetime, timedelta

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        ent = await _default_entity_id(s)
        po = await _add_po(s, org_id, ent, po_number="PO-INSP-OTHERGR", total="1000.00")
        older_gr = await _add_gr(s, org_id, ent, po.id)
        newest_gr = await _add_gr(s, org_id, ent, po.id)
        # Stamp `created_at` explicitly: both rows are inserted in one
        # transaction, so `now()` ties and `ORDER BY created_at DESC` would be
        # non-deterministic about which receipt the matcher represents.
        now = datetime.now(UTC)
        older_gr.created_at = now - timedelta(hours=1)
        newest_gr.created_at = now
        # Only the OLDER shipment was inspected.
        await _add_inspection(s, org_id, po_id=po.id, gr_id=older_gr.id, result="pass")
        inv = await _add_invoice(s, org_id, ent, po_number="PO-INSP-OTHERGR", amount="1000.00")
        await s.commit()

        match = await match_invoice_to_po(s, inv)

    assert match.gr_id == str(newest_gr.id)
    assert match.inspection_result is None
    assert match.match_type == "3-way"
