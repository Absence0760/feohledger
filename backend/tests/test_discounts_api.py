"""Real-DB coverage for the dynamic-discounting router (``app/api/discounts.py``).

Exercises the offer lifecycle (create / accept / decline), the per-invoice ROI
calculator, the cash-constrained optimizer, bulk vendor negotiation, and the
captured/missed/projected-savings dashboard end-to-end against the live test
tenants — plus RBAC, tenant isolation, audit rows, and exact ``Numeric`` money.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models.discount import (
    OFFER_STATUS_CAPTURED,
    OFFER_STATUS_DECLINED,
    OFFER_STATUS_EXPIRED,
    OFFER_STATUS_OFFERED,
    DiscountOffer,
)
from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor
from app.models.workflow import AuditLog
from app.utils.dates import utc_today

# `utc_today()`, never `date.today()`. `effective_status_sql` is evaluated against
# the endpoint's own `utc_today()`, so a fixture anchored on the LOCAL date makes
# the boundary cases below ("valid_until is today, so the offer is still open")
# wrong for every hour of the day where the two calendars disagree — several
# hours daily west of UTC, the whole working day in Asia-Pacific. CI runs UTC and
# never sees it. See backend/CLAUDE.md § Date-sensitive tests.
_FUTURE = (utc_today() + timedelta(days=40)).isoformat()


async def _default_entity_id(s):
    """The tenant's default entity — what the API assigns to new writes."""
    return (
        await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


async def _add_vendor(mk, org_id, name="Globex Industrial") -> str:
    async with mk() as s:
        v = Vendor(organization_id=org_id, name=name, entity_id=await _default_entity_id(s))
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return str(v.id)


async def _add_invoice(
    mk, org_id, *, amount="1000.00", status=InvoiceStatus.approved, vendor_id=None
) -> str:
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=await _default_entity_id(s),
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            vendor_name="Globex Industrial",
            vendor_id=uuid.UUID(vendor_id) if vendor_id else None,
            amount=Decimal(amount),
            currency="USD",
            due_date=utc_today() + timedelta(days=30),
            status=status,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return str(inv.id)


def _tiers():
    return [{"days": 5, "percent": "3.00"}, {"days": 10, "percent": "2.00"}]


async def _add_offer_row(mk, org_id, *, status, valid_until=None, base="1000.00", captured=None):
    """Insert one `DiscountOffer` straight into the tenant DB.

    The dashboard/list expiry cases need offer rows in states the HTTP surface
    cannot produce (a `captured` offer, or an `offered` one whose window closed
    yesterday), so they are written directly — exactly as
    `test_discount_auto_trigger` does.
    """
    async with mk() as s:
        offer = DiscountOffer(
            organization_id=org_id,
            entity_id=await _default_entity_id(s),
            scope="vendor",
            source="supplier",
            status=status,
            tiers=_tiers(),
            base_amount=Decimal(base),
            currency="USD",
            valid_from=utc_today() - timedelta(days=60),
            valid_until=valid_until,
            captured_amount=Decimal(captured) if captured is not None else None,
        )
        s.add(offer)
        await s.commit()
        await s.refresh(offer)
        return str(offer.id)


# ---------------------------------------------------------------------------
# create + lifecycle
# ---------------------------------------------------------------------------


async def test_create_invoice_offer_defaults_base_amount_and_audits(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id, amount="2500.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/discounts/offers",
            json={"scope": "invoice", "invoice_id": invoice_id, "tiers": _tiers()},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["base_amount"] == 2500.0  # defaulted from the invoice
    assert body["status"] == "offered"
    assert [t["days"] for t in body["tiers"]] == [5, 10]  # normalized + sorted

    async with mk() as s:
        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "discount_offer.created",
                    AuditLog.entity_id == uuid.UUID(body["id"]),
                )
            )
        ).scalar_one()
        assert audit.entity_type == "discount_offer"


async def test_create_offer_422s_a_malformed_id(realdb):
    """A bad id is a 422, not the 500 an unguarded `uuid.UUID(str)` raised.

    `DiscountOfferCreate` types `invoice_id` / `vendor_id` as `UUID`, so the
    refusal happens in the schema before any query — the same fix
    `BulkNegotiationRequest.vendor_id` carries. A valid uuid string still
    coerces, so no existing caller changes.
    """
    async with realdb.client(key="a", role="ap_manager") as c:
        by_invoice = await c.post(
            "/api/discounts/offers",
            json={"scope": "invoice", "invoice_id": "not-a-uuid", "tiers": _tiers()},
        )
        by_vendor = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "vendor",
                "vendor_id": "not-a-uuid",
                "base_amount": "1000.00",
                "tiers": _tiers(),
            },
        )
    assert by_invoice.status_code == 422, by_invoice.text
    assert by_vendor.status_code == 422, by_vendor.text


async def test_create_offer_refuses_an_unknown_key(realdb):
    """`extra="forbid"` on `DiscountOfferCreate` — the sibling of the
    `bulk-negotiate` guard, applied to the surface that has live callers.

    Dropped in silence, a misspelled `valid_until` yields an offer with no end
    date: it has no net due date, so the optimizer cannot rank it and it stands
    against the invoice indefinitely. A misspelled `currency` is worse in a
    quieter way — the offer is stamped from the invoice or the org instead of
    what the caller asserted, which is exactly the divergence `discount_capture`
    refuses to capture against, so the savings are never realised and nothing
    says why.

    Forbidding extras here was held back a round because this endpoint, unlike
    `bulk-negotiate`, has live callers. The audit that unblocked it is written up
    on `DiscountOfferCreate`'s docstring; this test is what keeps the guard from
    being quietly reverted the next time a caller sends something new.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id, amount="1000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        typo = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "invoice",
                "invoice_id": invoice_id,
                "tiers": _tiers(),
                "validUntil": _FUTURE,  # camelCase typo
            },
        )
    assert typo.status_code == 422, typo.text

    # The refusal is total, not partial — no half-built offer left behind.
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(DiscountOffer).where(DiscountOffer.invoice_id == uuid.UUID(invoice_id))
                )
            )
            .scalars()
            .all()
        )
    assert rows == []

    # And the correctly-spelled key still works, so the guard didn't widen into
    # the declared surface.
    async with realdb.client(key="a", role="ap_manager") as c:
        ok = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "invoice",
                "invoice_id": invoice_id,
                "tiers": _tiers(),
                "valid_until": _FUTURE,
            },
        )
    assert ok.status_code == 201, ok.text
    assert ok.json()["valid_until"] == _FUTURE


async def test_create_offer_accepts_every_field_its_live_callers_send(realdb):
    """The caller audit, as an executable assertion.

    `extra="forbid"` is only safe because every caller sends a subset of the
    declared fields. That was established by grepping the tree once; a grep
    result rots. The union of keys the 42 audited call sites send is
    `{scope, invoice_id, vendor_id, tiers, base_amount, currency, valid_from}`,
    and both shapes are exercised here so a future field rename or removal fails
    HERE — loudly, in the backend suite — rather than as a 422 in an e2e spec or,
    worse, in a deployed caller.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id, amount="1000.00")
    vendor_id = await _add_vendor(mk, org_id, name="Audited Caller Supply")

    async with realdb.client(key="a", role="ap_manager") as c:
        # The invoice-scoped shape (the pytest + Playwright majority).
        invoice_scoped = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "invoice",
                "invoice_id": invoice_id,
                "tiers": _tiers(),
                "currency": "USD",
                "valid_from": utc_today().isoformat(),
            },
        )
        # The vendor-scoped shape (`test_discount_currency_resolution`).
        vendor_scoped = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "vendor",
                "vendor_id": vendor_id,
                "base_amount": "1000.00",
                "currency": "USD",
                "tiers": _tiers(),
            },
        )
    assert invoice_scoped.status_code == 201, invoice_scoped.text
    assert vendor_scoped.status_code == 201, vendor_scoped.text


async def test_accept_offer_picks_best_tier_and_audits(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        offer_id = (
            await c.post(
                "/api/discounts/offers",
                json={"scope": "invoice", "invoice_id": invoice_id, "tiers": _tiers()},
            )
        ).json()["id"]
        resp = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["accepted_tier"]["days"] == 5  # highest-percent tier
    assert body["accepted_tier"]["percent"] == 3.0


async def test_accept_specific_tier(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        offer_id = (
            await c.post(
                "/api/discounts/offers",
                json={"scope": "invoice", "invoice_id": invoice_id, "tiers": _tiers()},
            )
        ).json()["id"]
    # A CFO (who cannot create) is allowed to accept.
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={"tier_days": 10})
    assert resp.status_code == 200
    assert resp.json()["accepted_tier"]["days"] == 10


async def test_accept_refuses_a_tier_whose_window_has_closed(realdb):
    """Issue #124's exact repro on the AP side: an offer opened 20 days ago,
    still within its own valid_until (+10 days out), but both sliding-scale
    tiers' real deadlines (measured from when the offer was extended) are
    long past. Before the fix, best_tier_for_date defaulted its reference to
    "today", so every tier looked perpetually achievable."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id)

    offer_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            DiscountOffer(
                id=offer_id,
                organization_id=org_id,
                scope="invoice",
                invoice_id=uuid.UUID(invoice_id),
                source="ap",
                status="offered",
                tiers=_tiers(),
                base_amount=Decimal("1000.00"),
                currency="USD",
                valid_from=utc_today() - timedelta(days=20),
                valid_until=utc_today() + timedelta(days=10),
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        best_resp = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
        assert best_resp.status_code == 409, best_resp.text

        # Naming the (long-closed) 5-day tier explicitly must not bypass the
        # window check either.
        named_resp = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={"tier_days": 5})
        assert named_resp.status_code == 422, named_resp.text

    async with mk() as s:
        offer = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == offer_id))
        ).scalar_one()
        assert offer.status == "offered"  # untouched — never accepted


async def test_decline_then_double_accept_conflicts(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        offer_id = (
            await c.post(
                "/api/discounts/offers",
                json={"scope": "invoice", "invoice_id": invoice_id, "tiers": _tiers()},
            )
        ).json()["id"]
        assert (await c.post(f"/api/discounts/offers/{offer_id}/decline")).status_code == 200
        # Accepting a declined offer is a 409 (state guard).
        conflict = await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
        assert conflict.status_code == 409

    async with mk() as s:
        row = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(offer_id)))
        ).scalar_one()
        assert row.status == OFFER_STATUS_DECLINED


# ---------------------------------------------------------------------------
# list + filter
# ---------------------------------------------------------------------------


async def test_list_filters_by_status(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    inv1 = await _add_invoice(mk, org_id)
    inv2 = await _add_invoice(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        o1 = (
            await c.post(
                "/api/discounts/offers",
                json={"scope": "invoice", "invoice_id": inv1, "tiers": _tiers()},
            )
        ).json()["id"]
        await c.post(
            "/api/discounts/offers",
            json={"scope": "invoice", "invoice_id": inv2, "tiers": _tiers()},
        )
        await c.post(f"/api/discounts/offers/{o1}/decline")

        offered = (await c.get("/api/discounts/offers?status=offered")).json()
        missed = (await c.get("/api/discounts/offers?status=missed")).json()

    assert all(i["status"] == "offered" for i in offered["items"])
    assert any(i["id"] == o1 and i["status"] == "declined" for i in missed["items"])


# ---------------------------------------------------------------------------
# ROI
# ---------------------------------------------------------------------------


async def test_invoice_roi_uses_open_offer(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id, amount="1000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        await c.post(
            "/api/discounts/offers",
            json={"scope": "invoice", "invoice_id": invoice_id, "tiers": _tiers()},
        )
    # ap_clerk can read the ROI.
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get(f"/api/discounts/invoices/{invoice_id}/roi")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 3% best tier, deadline today+5, due today+30 → ~25 days accelerated.
    assert body["savings"] == 30.0  # 1000 * 3%
    assert body["worthwhile"] is True
    assert body["annualized_return_pct"] > 12


# ---------------------------------------------------------------------------
# optimizer
# ---------------------------------------------------------------------------


async def test_optimize_ranks_and_respects_cash_budget(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    big = await _add_invoice(mk, org_id, amount="10000.00")
    small = await _add_invoice(mk, org_id, amount="500.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        for inv in (big, small):
            await c.post(
                "/api/discounts/offers",
                json={"scope": "invoice", "invoice_id": inv, "tiers": _tiers()},
            )
    # Budget only covers the small invoice's discounted outlay.
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/discounts/optimize", json={"cash_budget": 1000})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["recommendations"]) == 2
    selected = [r for r in body["recommendations"] if r["selected"]]
    assert len(selected) == 1
    assert selected[0]["invoice_id"] == small
    assert body["total_savings_available"] > body["total_savings_selected"]


async def _single_tier_offer(realdb, *, amount: str, percent: str) -> str:
    """One open offer on a fresh invoice, with exactly one tier.

    A single tier keeps the outlay arithmetic deterministic: savings =
    ``amount * percent / 100`` quantised to 2dp, so the discounted outlay the
    budget is measured against is an exact figure the test can name.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id, amount=amount)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "invoice",
                "invoice_id": invoice_id,
                "tiers": [{"days": 10, "percent": percent}],
            },
        )
    assert resp.status_code == 201, resp.text
    return invoice_id


def _selected_invoice_ids(body: dict) -> set[str]:
    return {r["invoice_id"] for r in body["recommendations"] if r["selected"]}


async def test_optimize_cash_budget_is_exact_not_a_rounded_float(realdb):
    """The budget decides which invoices get paid early — so it must be exact.

    ``POST /optimize`` took a bare ``dict`` and did
    ``Decimal(str(body["cash_budget"]))``. That *looks* exact, but ``json.loads``
    had already turned a JSON number into a ``float``, so the value the
    optimizer compared against was whatever double the literal rounded to.
    Typing the field ``Decimal`` does not fix a JSON body either — pydantic
    yields ``Decimal('100')`` from ``100.00000000000000001`` for the same
    reason. Only the string form round-trips.

    This is not a display value: the rounding changes WHICH INVOICES ARE
    SELECTED, and the two parses of one wire value disagree below.
    """
    invoice_id = await _single_tier_offer(realdb, amount="10000.00", percent="2.00")
    # savings = 10000.00 * 2% = 200.00 → discounted outlay = exactly 9800.00.
    outlay = Decimal("9800.00")

    # The wire value under test, and what the old float path made of it.
    lossy_wire = "9799.999999999999999"
    assert Decimal(str(float(lossy_wire))) == Decimal("9800.0")  # the rounding, stated
    assert Decimal(lossy_wire) < outlay  # …and what it rounded away from

    async with realdb.client(key="a", role="cfo") as c:
        # (a) The budget the OLD float path produced: it covers the outlay, so
        #     the invoice is selected and the cash goes out.
        rounded = await c.post("/api/discounts/optimize", json={"cash_budget": "9800.0"})
        # (b) The budget the caller actually sent: a hair short, so it does not.
        exact = await c.post("/api/discounts/optimize", json={"cash_budget": lossy_wire})
        # (c) The wire shape that caused (a) to stand in for (b). It is refused
        #     now; before, it was accepted and silently became (a).
        lossy = await c.post("/api/discounts/optimize", json={"cash_budget": 9799.999999999999999})

    assert rounded.status_code == 200, rounded.text
    assert exact.status_code == 200, exact.text
    # Different SELECTIONS from the same wire value — the rounding mattered.
    assert invoice_id in _selected_invoice_ids(rounded.json())
    assert invoice_id not in _selected_invoice_ids(exact.json())
    # …so the shape that collapses (b) into (a) cannot be accepted at all.
    assert lossy.status_code == 422, lossy.text


async def test_optimize_refuses_a_json_number_budget(realdb):
    """The lossy shape is refused outright, not silently accepted rounded.

    A fractional JSON number has already lost exactness before the server sees
    it, so there is no honest value to proceed with. The 422 names the fix.
    """
    await _single_tier_offer(realdb, amount="10000.00", percent="2.00")

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/discounts/optimize", json={"cash_budget": 9799.999999999999999})
    assert resp.status_code == 422, resp.text
    assert "string" in resp.text.lower()

    async with realdb.client(key="a", role="cfo") as c:
        # A JSON integer is exact (`json.loads` yields `int`), so it still works —
        # that is the shape existing callers send.
        ok = await c.post("/api/discounts/optimize", json={"cash_budget": 100000})
    assert ok.status_code == 200, ok.text


async def test_optimize_rejects_a_malformed_or_misspelled_budget(realdb):
    """A bare dict on a money path made both of these silent or fatal.

    ``Decimal(str(...))`` on a non-numeric value raised ``InvalidOperation`` and
    surfaced as a 500; a misspelled key ran the optimizer UNCONSTRAINED and
    returned a plan committing more cash than the caller asked for. Both are
    422s now.
    """
    await _single_tier_offer(realdb, amount="10000.00", percent="2.00")

    async with realdb.client(key="a", role="cfo") as c:
        malformed = await c.post("/api/discounts/optimize", json={"cash_budget": "lots"})
        misspelled = await c.post("/api/discounts/optimize", json={"cashBudget": "100.00"})
        negative = await c.post("/api/discounts/optimize", json={"cash_budget": "-5.00"})

    assert malformed.status_code == 422, malformed.text
    assert misspelled.status_code == 422, misspelled.text
    assert negative.status_code == 422, negative.text


async def test_optimize_recommendation_states_its_own_currency(realdb):
    """Each row names the currency ITS money is in.

    ``roi.savings`` is computed from the offer's own ``base_amount``, so it is
    the OFFER's currency — which equals the response-level ``currency`` only
    when ``unconvertible`` is False. Without a per-row code a client cannot
    label a foreign row's figure at all: it either renders the amount bare or
    stamps the totals' currency on it, which is how "Save $412.00" came to
    describe €412.
    """
    invoice_id = await _single_tier_offer(realdb, amount="10000.00", percent="2.00")

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/discounts/optimize", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rec = next(r for r in body["recommendations"] if r["invoice_id"] == invoice_id)
    assert rec["currency"] == "USD"
    # This row IS in the totals' currency, so the two agree — and the flag says so.
    assert rec["unconvertible"] is False
    assert rec["currency"] == body["currency"]


async def test_optimize_foreign_recommendation_carries_the_offers_currency(realdb):
    """A row flagged `unconvertible` names the currency the totals are NOT in.

    This is the row the per-row field exists for: its money is real, it is
    simply denominated in something the totals could not absorb.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id, amount="10000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post(
            "/api/discounts/offers",
            json={
                "scope": "invoice",
                "invoice_id": invoice_id,
                "tiers": [{"days": 10, "percent": "2.00"}],
            },
        )
    assert created.status_code == 201, created.text
    offer_id = created.json()["id"]
    # Denominate the offer in a currency the tenant does not report in. Set on
    # the row directly: the create route inherits the invoice's currency.
    async with mk() as s:
        offer = await s.get(DiscountOffer, uuid.UUID(offer_id))
        offer.currency = "JPY"
        await s.commit()

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/discounts/optimize", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["currency"] != "JPY"  # the premise: the totals are in something else
    rec = next(r for r in body["recommendations"] if r["invoice_id"] == invoice_id)
    assert rec["unconvertible"] is True
    assert rec["currency"] == "JPY"


# ---------------------------------------------------------------------------
# bulk negotiation
# ---------------------------------------------------------------------------


async def test_bulk_negotiate_sums_open_invoices(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Acme Bulk Co")
    # Two open invoices for this vendor.
    await _add_invoice(mk, org_id, amount="1000.00", vendor_id=vendor_id)
    await _add_invoice(mk, org_id, amount="2000.00", vendor_id=vendor_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/discounts/bulk-negotiate",
            json={
                "vendor_id": vendor_id,
                "tiers": [{"days": 7, "percent": "2.00"}],
                "valid_until": _FUTURE,
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["scope"] == "vendor"
    assert body["base_amount"] == 3000.0  # summed open balances


async def test_bulk_negotiate_refuses_a_vendor_with_no_open_invoices(realdb):
    """409 — the refusal the `/discounts` panel surfaces verbatim.

    A vendor with nothing open has no balance to discount, and
    `build_bulk_offer` would raise on an empty sum. The endpoint is the only
    thing that knows this: `base_amount` is computed server-side from the
    vendor's open invoices, so the caller cannot tell in advance and the
    refusal has to be legible rather than a 500.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Nothing Open Ltd")
    # Not `_OPEN_FOR_DISCOUNT`: an invoice still in review is not a balance we
    # can offer to pay early.
    await _add_invoice(mk, org_id, amount="900.00", vendor_id=vendor_id, status=InvoiceStatus.new)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/discounts/bulk-negotiate",
            json={"vendor_id": vendor_id, "tiers": [{"days": 7, "percent": "2.00"}]},
        )
    assert resp.status_code == 409, resp.text
    assert "no open invoices" in resp.json()["detail"]


async def test_bulk_negotiate_refuses_a_vendor_from_another_entity(realdb):
    """A vendor outside the caller's write entity is an opaque 404.

    The open-invoice sum has always been entity-scoped; the vendor lookup was
    not, so the two halves could disagree — an offer stamped entity A pointing
    at entity B's supplier. And the refusals differed in a way that leaked:
    an out-of-entity vendor answered 409 ("no open invoices"), which confirms
    the id exists somewhere in the tenant, while a missing one answered 404.

    The vendor here has an open invoice **in its own entity**, so under the old
    behaviour this request reached the 409 rather than being refused outright.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with mk() as s:
        sub = Entity(
            organization_id=org_id,
            name="EU Subsidiary",
            slug=f"eu-{uuid.uuid4().hex[:6]}",
            is_default=False,
        )
        s.add(sub)
        await s.flush()
        sub_id = sub.id
        vendor = Vendor(organization_id=org_id, name="Subsidiary Supply Co", entity_id=sub_id)
        s.add(vendor)
        await s.flush()
        vendor_id = vendor.id
        s.add(
            Invoice(
                organization_id=org_id,
                entity_id=sub_id,
                invoice_number=f"SUB-{uuid.uuid4().hex[:8]}",
                vendor_name="Subsidiary Supply Co",
                vendor_id=vendor_id,
                amount=Decimal("4000.00"),
                currency="USD",
                due_date=utc_today() + timedelta(days=30),
                status=InvoiceStatus.approved,
            )
        )
        await s.commit()

    payload = {"vendor_id": str(vendor_id), "tiers": [{"days": 7, "percent": "2.00"}]}

    async with realdb.client(key="a", role="ap_manager") as c:
        # No `X-Entity-ID` → the write scope is the tenant's DEFAULT entity.
        cross = await c.post("/api/discounts/bulk-negotiate", json=payload)
        # With the subsidiary selected, the very same request succeeds — the
        # refusal is about scope, not about the vendor being unusable.
        own = await c.post(
            "/api/discounts/bulk-negotiate",
            json=payload,
            headers={"X-Entity-ID": str(sub_id)},
        )

    assert cross.status_code == 404, cross.text
    assert own.status_code == 201, own.text
    assert own.json()["base_amount"] == 4000.0


async def test_bulk_negotiate_404s_an_unknown_vendor(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/discounts/bulk-negotiate",
            json={
                "vendor_id": str(uuid.uuid4()),
                "tiers": [{"days": 7, "percent": "2.00"}],
            },
        )
    assert resp.status_code == 404, resp.text
    assert mk is not None  # sessionmaker resolved; no rows needed for this one


async def test_bulk_negotiate_422s_a_malformed_vendor_id(realdb):
    """A bad id is a 422, not the 500 an unguarded `uuid.UUID(str)` raised.

    `BulkNegotiationRequest.vendor_id` is typed `UUID`, so the refusal happens
    in the schema before any query runs.
    """
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/discounts/bulk-negotiate",
            json={"vendor_id": "not-a-uuid", "tiers": [{"days": 7, "percent": "2.00"}]},
        )
    assert resp.status_code == 422, resp.text


async def test_bulk_negotiate_refuses_an_unknown_key(realdb):
    """`extra="forbid"` — a misspelled `valid_until` must not create a
    never-ending offer.

    Silently dropped, the typo yields an offer with no end date: it has no net
    due date, so the optimizer cannot rank it and it stands against the
    vendor's whole open balance indefinitely. 422 is the honest answer to a
    field we did not understand, exactly as on `POST /optimize`.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Typo Supply Co")
    await _add_invoice(mk, org_id, amount="1000.00", vendor_id=vendor_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/discounts/bulk-negotiate",
            json={
                "vendor_id": vendor_id,
                "tiers": [{"days": 7, "percent": "2.00"}],
                "validUntil": _FUTURE,  # camelCase typo
            },
        )
    assert resp.status_code == 422, resp.text

    # And nothing was created — the refusal is total, not partial.
    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(DiscountOffer).where(DiscountOffer.vendor_id == uuid.UUID(vendor_id))
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_bulk_negotiate_is_gated_to_admin_and_ap_manager(realdb):
    """`_WRITE_ROLES` — the gate the `/discounts` trigger button mirrors.

    A CFO may ACCEPT an offer (`_ACCEPT_ROLES`) but may not propose one, and a
    clerk may do neither. The page hides the control for both, so this is what
    keeps the two layers honest.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Gated Bulk Co")
    await _add_invoice(mk, org_id, amount="1000.00", vendor_id=vendor_id)
    payload = {"vendor_id": vendor_id, "tiers": [{"days": 7, "percent": "2.00"}]}

    for role in ("ap_clerk", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            resp = await c.post("/api/discounts/bulk-negotiate", json=payload)
        assert resp.status_code == 403, f"{role}: {resp.text}"


async def test_optimize_reports_a_horizonless_bulk_offer_as_unrankable(realdb):
    """A bulk offer with no `valid_until` has no net due date, so it has no APR.

    Regression: `_build_opportunity` ended its due-date chain at `or pay_by`,
    which is zero days accelerated — so the offer came back with
    `annualized_return_pct: 0.00` and `worthwhile: false` sitting beside a
    POSITIVE `net_benefit`, and was permanently unrecommendable. The unknown is
    now expressed (`horizon_known: false`, nulls) and carried on `unrankable`,
    where a client can tell it apart from a genuine 0 % return.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Horizonless Supply Co")
    await _add_invoice(mk, org_id, amount="500000.00", vendor_id=vendor_id)
    # A second, invoice-scoped offer that DOES have a horizon, so the two
    # populations are visibly separated in one response.
    ranked_invoice = await _add_invoice(mk, org_id, amount="1000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        bulk_id = (
            await c.post(
                "/api/discounts/bulk-negotiate",
                json={
                    "vendor_id": vendor_id,
                    "tiers": [{"days": 5, "percent": "3.00"}],
                    # No `valid_until` — exactly what `build_bulk_offer` allows
                    # and what the UI sends when the buyer names no end date.
                },
            )
        ).json()["id"]
        ranked_id = (
            await c.post(
                "/api/discounts/offers",
                json={"scope": "invoice", "invoice_id": ranked_invoice, "tiers": _tiers()},
            )
        ).json()["id"]

        body = (await c.post("/api/discounts/optimize", json={})).json()

    assert [r["offer_id"] for r in body["recommendations"]] == [ranked_id]
    assert [r["offer_id"] for r in body["unrankable"]] == [bulk_id]

    unranked = body["unrankable"][0]["roi"]
    assert unranked["horizon_known"] is False
    assert unranked["days_accelerated"] is None
    assert unranked["annualized_return_pct"] is None
    assert unranked["opportunity_cost"] is None
    assert unranked["net_benefit"] is None
    assert unranked["worthwhile"] is None
    # The discount itself is a real figure: 3 % of 500,000.
    assert Decimal(str(unranked["savings"])) == Decimal("15000.00")
    assert body["unrankable"][0]["selected"] is False

    # The ranked offer still carries real numbers — the nulls are specific to
    # the missing horizon, not a blanket weakening of the contract.
    ranked_roi = body["recommendations"][0]["roi"]
    assert ranked_roi["horizon_known"] is True
    assert ranked_roi["annualized_return_pct"] > 0
    assert ranked_roi["worthwhile"] is True


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


async def test_dashboard_rolls_up_captured_missed_open(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    inv_open = await _add_invoice(mk, org_id, amount="1000.00")
    inv_missed = await _add_invoice(mk, org_id, amount="4000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        # one open offer
        await c.post(
            "/api/discounts/offers",
            json={"scope": "invoice", "invoice_id": inv_open, "tiers": _tiers()},
        )
        # one declined (missed) offer
        missed_id = (
            await c.post(
                "/api/discounts/offers",
                json={"scope": "invoice", "invoice_id": inv_missed, "tiers": _tiers()},
            )
        ).json()["id"]
        await c.post(f"/api/discounts/offers/{missed_id}/decline")

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/discounts/dashboard")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["open_offer_count"] >= 1
    assert body["missed_count"] >= 1
    # missed savings counts the best tier (3%) of the 4000 invoice = 120.
    assert body["missed_amount"] >= 120.0
    assert body["projected_savings"] > 0


async def test_dashboard_capture_rate_counts_lapsed_offers_as_missed(realdb):
    """1 captured of 10 must not read as a 100 % capture rate.

    Regression: `expire_if_past` has exactly one caller — the auto-capture
    sweep — and that sweep is behind `FEOH_DISCOUNT_OPTIMIZATION_ENABLED`,
    which is `false` by default (and is off here). So nine offers whose
    `valid_until` passed months ago stayed at `offered`: they never entered the
    `missed` bucket, `capture_rate_pct` is captured / (captured + missed), and
    the card reported **100.00 %** while the `open` chip counted nine dead
    offers. Expiry is now derived on read
    (`discount_offers.effective_status_sql`), so it no longer depends on any
    sweep having run.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    yesterday = utc_today() - timedelta(days=1)

    await _add_offer_row(mk, org_id, status=OFFER_STATUS_CAPTURED, captured="30.00")
    for _ in range(9):
        await _add_offer_row(mk, org_id, status=OFFER_STATUS_OFFERED, valid_until=yesterday)

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/discounts/dashboard")).json()

    assert body["captured_count"] == 1
    assert body["missed_count"] == 9
    # Each lapsed offer's best tier is 3 % of 1000.00 = 30.00.
    assert Decimal(str(body["missed_amount"])) == Decimal("270.00")
    # A lapsed offer is NOT still on the table, so it is not counted twice.
    assert body["open_offer_count"] == 0
    assert Decimal(str(body["capture_rate_pct"])) == Decimal("10.00")


async def test_dashboard_capture_rate_ignores_offers_still_in_window(realdb):
    """The mirror case — an offer whose window is still open stays `open` and
    never inflates `missed`, so the derived rule can't overcorrect."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    await _add_offer_row(mk, org_id, status=OFFER_STATUS_CAPTURED, captured="30.00")
    # Today is the last day of the window — `valid_until < today` is false.
    await _add_offer_row(mk, org_id, status=OFFER_STATUS_OFFERED, valid_until=utc_today())
    # No `valid_until` at all: nothing has elapsed.
    await _add_offer_row(mk, org_id, status=OFFER_STATUS_OFFERED, valid_until=None)

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/discounts/dashboard")).json()

    assert body["captured_count"] == 1
    assert body["missed_count"] == 0
    assert body["open_offer_count"] == 2
    assert Decimal(str(body["capture_rate_pct"])) == Decimal("100.00")
    # Something WAS decided, so the rate is a real measurement.
    assert body["insufficient_data"] is False


async def test_dashboard_reports_no_capture_rate_before_anything_is_decided(realdb):
    """`null` + `insufficient_data`, never `0.00`.

    A capture rate is a ratio over the DECIDED population — captured plus
    missed. A tenant whose offers are all still inside their window has decided
    nothing, and the endpoint used to report `0.00` for that: "we captured none
    of the ones we could have", which is the opposite fact and the one that
    reads as a failing programme. The sibling rollup
    (`analytics.DiscountCaptureMetrics`, on `GET /api/dashboard`) has always
    answered `None` here; the two agree now. See `docs/decisions.md` §34.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    await _add_offer_row(mk, org_id, status=OFFER_STATUS_OFFERED, valid_until=utc_today())
    await _add_offer_row(mk, org_id, status=OFFER_STATUS_OFFERED, valid_until=None)

    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/discounts/dashboard")).json()

    assert body["captured_count"] == 0
    assert body["missed_count"] == 0
    assert body["open_offer_count"] == 2
    # The key must be PRESENT and null — a dropped key reads as "the server is
    # older than my client", not "there is nothing to report".
    assert "capture_rate_pct" in body
    assert body["capture_rate_pct"] is None
    assert body["insufficient_data"] is True


async def test_dashboard_reports_no_capture_rate_with_no_offers_at_all(realdb):
    """The emptiest case — a tenant that has never been offered a discount. The
    old `0.00` was at its most misleading exactly here."""
    async with realdb.client(key="a", role="cfo") as c:
        body = (await c.get("/api/discounts/dashboard")).json()

    assert body["captured_count"] == 0
    assert body["missed_count"] == 0
    assert body["capture_rate_pct"] is None
    assert body["insufficient_data"] is True


async def test_offer_list_reports_and_filters_on_the_effective_status(realdb):
    """A lapsed offer renders as `expired`, is filtered out of `offered`, and
    is found by the `missed` chip — without the sweep ever running."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    lapsed_id = await _add_offer_row(
        mk, org_id, status=OFFER_STATUS_OFFERED, valid_until=utc_today() - timedelta(days=1)
    )
    live_id = await _add_offer_row(
        mk, org_id, status=OFFER_STATUS_OFFERED, valid_until=utc_today() + timedelta(days=30)
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        rows = {o["id"]: o for o in (await c.get("/api/discounts/offers")).json()["items"]}
        assert rows[lapsed_id]["status"] == OFFER_STATUS_EXPIRED
        assert rows[live_id]["status"] == OFFER_STATUS_OFFERED

        detail = (await c.get(f"/api/discounts/offers/{lapsed_id}")).json()
        assert detail["status"] == OFFER_STATUS_EXPIRED

        open_ids = [
            o["id"] for o in (await c.get("/api/discounts/offers?status=offered")).json()["items"]
        ]
        assert open_ids == [live_id]

        missed_ids = [
            o["id"] for o in (await c.get("/api/discounts/offers?status=missed")).json()["items"]
        ]
        assert missed_ids == [lapsed_id]

    # Derived, never written: the stored column is untouched by a read.
    async with mk() as s:
        row = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(lapsed_id)))
        ).scalar_one()
        assert row.status == OFFER_STATUS_OFFERED


async def test_effective_status_sql_matches_the_python_rule(realdb):
    """Drift guard: the `CASE` the queries filter/group by must agree with the
    Python predicate the responses are built from, row for row — including on
    the window's own last day, where an off-by-one would mislabel every offer
    for exactly one day."""
    from app.services import discount_offers as offers_svc

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    today = utc_today()

    for status, valid_until in (
        (OFFER_STATUS_OFFERED, today - timedelta(days=1)),  # lapsed
        (OFFER_STATUS_OFFERED, today),  # last day of the window — still open
        (OFFER_STATUS_OFFERED, today + timedelta(days=1)),
        (OFFER_STATUS_OFFERED, None),
        (OFFER_STATUS_CAPTURED, today - timedelta(days=1)),
        (OFFER_STATUS_DECLINED, today - timedelta(days=1)),
        (OFFER_STATUS_EXPIRED, today - timedelta(days=1)),
    ):
        await _add_offer_row(mk, org_id, status=status, valid_until=valid_until)

    async with mk() as s:
        rows = list((await s.execute(select(DiscountOffer))).scalars().all())
        sql_by_id = dict(
            (
                await s.execute(select(DiscountOffer.id, offers_svc.effective_status_sql(today)))
            ).all()
        )

    assert len(rows) == 7
    for row in rows:
        assert sql_by_id[row.id] == offers_svc.effective_status(row, as_of=today), row.id


# ---------------------------------------------------------------------------
# RBAC + tenant isolation
# ---------------------------------------------------------------------------


async def test_clerk_cannot_create_offer(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/discounts/offers",
            json={"scope": "invoice", "invoice_id": invoice_id, "tiers": _tiers()},
        )
    assert resp.status_code == 403


async def test_cfo_can_decline_an_offer(realdb):
    """Decline carries the SAME gate as accept.

    The two are halves of one decision, and the CFO owns the early-pay-vs-cash
    trade-off. Decline used to be gated to ``_WRITE_ROLES`` (admin/ap_manager)
    while accept allowed the CFO — so a CFO could commit cash early but not
    refuse the offer, which is backwards, and the ``/discounts`` page (open to
    admin/ap_manager/cfo) rendered a Decline button the backend then 403'd.
    Declining moves no money; it only flips status.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        offer_id = (
            await c.post(
                "/api/discounts/offers",
                json={"scope": "invoice", "invoice_id": invoice_id, "tiers": _tiers()},
            )
        ).json()["id"]

    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post(f"/api/discounts/offers/{offer_id}/decline")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == OFFER_STATUS_DECLINED

    # The refusal is on the record, attributed to the CFO who made it.
    async with mk() as s:
        row = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "discount_offer.declined",
                    AuditLog.entity_id == uuid.UUID(offer_id),
                )
            )
        ).scalar_one()
        assert row.entity_type == "discount_offer"


async def test_ap_decline_refuses_a_lapsed_offer(realdb):
    """The AP side takes the same guard as the portal — one rule in
    `discount_offers.decline_offer`, so the buyer and the supplier can't diverge
    on whether a dead offer is still refusable. A lapsed offer already reads
    `expired` here; flipping it to `declined` would assert a refusal nobody made
    on an append-only audit row.

    Two days past `valid_until`, because `decline_offer` allows
    `DECLINE_GRACE_DAYS` of slack for a payee whose own last day has not ended
    yet. One day past still reads `expired` on every read surface — that
    asymmetry is pinned directly in `test_the_grace_does_not_reach_the_read_surfaces`."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    offer_id = await _add_offer_row(
        mk, org_id, status=OFFER_STATUS_OFFERED, valid_until=utc_today() - timedelta(days=2)
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/discounts/offers/{offer_id}/decline")
    assert resp.status_code == 409, resp.text

    async with mk() as s:
        row = (
            await s.execute(select(DiscountOffer).where(DiscountOffer.id == uuid.UUID(offer_id)))
        ).scalar_one()
        assert row.status == OFFER_STATUS_OFFERED
        actions = {
            a.action
            for a in (
                await s.execute(select(AuditLog).where(AuditLog.entity_id == uuid.UUID(offer_id)))
            )
            .scalars()
            .all()
        }
        assert "discount_offer.declined" not in actions


async def test_clerk_cannot_decline_an_offer(realdb):
    """Widening decline to the CFO must not widen it to everyone — a clerk is
    still refused, exactly as they are on accept."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        offer_id = (
            await c.post(
                "/api/discounts/offers",
                json={"scope": "invoice", "invoice_id": invoice_id, "tiers": _tiers()},
            )
        ).json()["id"]

    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.post(f"/api/discounts/offers/{offer_id}/decline")).status_code == 403
        assert (
            await c.post(f"/api/discounts/offers/{offer_id}/accept", json={})
        ).status_code == 403


async def test_offer_not_visible_cross_tenant(realdb):
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk_a, org_a)
    async with realdb.client(key="a", role="ap_manager") as c:
        offer_id = (
            await c.post(
                "/api/discounts/offers",
                json={"scope": "invoice", "invoice_id": invoice_id, "tiers": _tiers()},
            )
        ).json()["id"]

    # Tenant B must not see tenant A's offer.
    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get(f"/api/discounts/offers/{offer_id}")
    assert resp.status_code == 404
