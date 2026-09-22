"""Coverage for the Intelligent Data Enrichment first slice.

Two tiers (mirrors the repo's adaptive/analytics test style):

  * Pure-Python edges — the auto-fill dominance/confidence math, the price-
    variance median baseline + tolerance, and the vendor-score composite with
    N/A handling. These pin the deterministic (no-LLM, no-cloud-key) statistics
    without a DB and assert every numeric is Decimal/str, never float.

  * Real-Postgres end-to-end (``realdb``) — drives the two
    ``/api/enrichment/*`` routes against a live tenant DB so the SQL, entity
    scoping, RBAC, PII-absence, and tenant isolation are all exercised.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.vendor_enrichment import (
    FieldSuggestion,
    PriceVarianceFlag,
    compute_vendor_score,
    detect_price_variance,
    suggest_fields,
)

# ---------------------------------------------------------------------------
# Pure-function unit tests (no DB)
# ---------------------------------------------------------------------------


def _hist(gl=None, cost_center=None, payment_terms=None):
    return {"gl_account": gl, "cost_center": cost_center, "payment_terms": payment_terms}


_EMPTY_CURRENT = {"gl_account": None, "cost_center": None, "payment_terms": None}


def test_autofill_suggests_dominant_gl():
    rows = [_hist(gl="6000")] * 8 + [_hist(gl="6100")] * 2
    out = suggest_fields(rows, _EMPTY_CURRENT)
    gl = next(s for s in out if s.field == "gl_account")
    assert gl.value == "6000"
    assert gl.confidence == Decimal("80.0")
    assert gl.occurrences == 8
    assert gl.sample_size == 10
    assert gl.runner_up == "6100"
    assert "8 of 10" in gl.evidence


def test_autofill_below_threshold_no_suggestion():
    rows = [_hist(gl="A")] * 4 + [_hist(gl="B")] * 3 + [_hist(gl="C")] * 3
    out = suggest_fields(rows, _EMPTY_CURRENT)
    assert [s for s in out if s.field == "gl_account"] == []


def test_autofill_populated_field_suppressed():
    rows = [_hist(gl="6000")] * 10
    current = {"gl_account": "9999", "cost_center": None, "payment_terms": None}
    out = suggest_fields(rows, current)
    assert [s for s in out if s.field == "gl_account"] == []


def test_autofill_sample_below_minimum_no_suggestion():
    rows = [_hist(gl="6000")] * 2
    out = suggest_fields(rows, _EMPTY_CURRENT)
    assert out == []


def test_autofill_tie_broken_by_recency():
    # Equal counts; values list is newest-first. "NEW" appears first so it wins.
    # Drop the threshold to 50% so the tied (50%-dominant) value surfaces and we
    # can assert the tie-break, rather than being suppressed by MIN_CONFIDENCE.
    rows = [_hist(gl="NEW"), _hist(gl="OLD"), _hist(gl="NEW"), _hist(gl="OLD")]
    out1 = suggest_fields(rows, _EMPTY_CURRENT, min_confidence=Decimal("50.0"))
    out2 = suggest_fields(rows, _EMPTY_CURRENT, min_confidence=Decimal("50.0"))
    gl = next(s for s in out1 if s.field == "gl_account")
    assert gl.value == "NEW"  # most-recent occurrence wins
    assert gl.confidence == Decimal("50.0")
    # Deterministic across runs.
    assert [s.value for s in out1] == [s.value for s in out2]


def _line(item_code=None, description=None, unit_price=None):
    return {
        "item_code": item_code,
        "description": description,
        "unit_price": None if unit_price is None else Decimal(unit_price),
    }


def test_price_variance_flagged_with_baseline():
    history = [_line(item_code="W-A", unit_price=p) for p in ("10", "10", "10", "9", "11")]
    draft = [
        _line(item_code="W-A", description="Widget A", unit_price="12.50"),
        _line(item_code="W-A", description="Widget A", unit_price="15.00"),
    ]
    flags = detect_price_variance(draft, history)
    assert len(flags) == 2
    f0 = flags[0]
    assert f0.baseline_unit_price == Decimal("10.00")
    assert f0.delta == Decimal("2.50")
    assert f0.delta_pct == Decimal("25.0")
    assert f0.direction == "over"
    assert f0.severity == "info"
    f1 = flags[1]
    assert f1.delta_pct == Decimal("50.0")
    assert f1.severity == "warning"


def test_price_variance_within_tolerance_not_flagged():
    history = [_line(item_code="W-A", unit_price=p) for p in ("10", "10", "10")]
    draft = [_line(item_code="W-A", unit_price="10.50")]  # 5% < 15%
    assert detect_price_variance(draft, history) == []


def test_price_variance_insufficient_history_no_flag():
    history = [_line(item_code="W-A", unit_price="10")]  # seen once < min_history(2)
    draft = [_line(item_code="W-A", unit_price="50")]
    assert detect_price_variance(draft, history) == []


def test_price_variance_unkeyable_line_skipped():
    history = [_line(item_code="W-A", unit_price="10")] * 3
    draft = [_line(item_code=None, description="   ", unit_price="99")]  # no key
    # No crash, no flag.
    assert detect_price_variance(draft, history) == []


def test_price_variance_description_normalization_key_match():
    # "Widget  A " and "widget a" normalize to the same key; baseline built across both.
    history = [
        _line(description="Widget  A ", unit_price="10"),
        _line(description="widget a", unit_price="10"),
        _line(description="WIDGET A", unit_price="10"),
    ]
    draft = [_line(description="Widget A", unit_price="13.00")]  # +30%
    flags = detect_price_variance(draft, history)
    assert len(flags) == 1
    assert flags[0].sample_size == 3
    assert flags[0].delta_pct == Decimal("30.0")
    assert flags[0].severity == "warning"


def test_price_variance_keyed_per_currency():
    # Same item, two currencies. A USD draft line must be compared only against
    # the USD history median — not a median pooled across USD + EUR. EUR history
    # at 100 would, if pooled, drag the USD baseline up and produce a bogus flag.
    history = [
        _line(item_code="W-A", unit_price="10"),  # USD (default)
        _line(item_code="W-A", unit_price="10"),
        _line(item_code="W-A", unit_price="10"),
        {**_line(item_code="W-A", unit_price="100"), "currency": "EUR"},
        {**_line(item_code="W-A", unit_price="100"), "currency": "EUR"},
        {**_line(item_code="W-A", unit_price="100"), "currency": "EUR"},
    ]
    # USD draft at 11 → vs USD median 10 → +10% < 15% tolerance → NOT flagged.
    # (If currencies were pooled, the median would be ~55 and this would either
    # mis-flag or compute a nonsense delta.)
    usd_draft = [_line(item_code="W-A", unit_price="11")]
    assert detect_price_variance(usd_draft, history) == []

    # EUR draft at 130 → vs EUR median 100 → +30% → flagged warning, sample_size
    # is the 3 EUR rows only (the USD rows never enter this baseline).
    eur_draft = [{**_line(item_code="W-A", unit_price="130"), "currency": "EUR"}]
    flags = detect_price_variance(eur_draft, history)
    assert len(flags) == 1
    assert flags[0].baseline_unit_price == Decimal("100.00")
    assert flags[0].delta_pct == Decimal("30.0")
    assert flags[0].sample_size == 3
    assert flags[0].severity == "warning"


def test_price_variance_no_same_currency_history_skipped():
    # History is all EUR; the draft line is USD. There is no same-currency
    # baseline, so the line is N/A (skipped) rather than compared cross-currency.
    history = [{**_line(item_code="W-A", unit_price="10"), "currency": "EUR"}] * 3
    usd_draft = [_line(item_code="W-A", unit_price="50")]  # would be +400% if pooled
    assert detect_price_variance(usd_draft, history) == []


def test_vendor_score_composite_with_na_ontime():
    score = compute_vendor_score(
        vendor_id="v1",
        vendor_name="Acme",
        accuracy_input={"approved_count": 25, "corrected_count": 3},  # 88.0
        dispute_input={"total_invoices": 40, "exception_invoices": 3},  # 92.5
        ontime_input=None,  # N/A
    )
    acc = next(s for s in score.sub_scores if s.name == "accuracy")
    disp = next(s for s in score.sub_scores if s.name == "dispute")
    ontime = next(s for s in score.sub_scores if s.name == "on_time")
    assert acc.score == Decimal("88.0")
    assert disp.score == Decimal("92.5")
    assert ontime.score is None  # excluded from composite
    # (0.4*88.0 + 0.3*92.5) / 0.7 = (35.2 + 27.75) / 0.7 = 62.95 / 0.7 = 89.928... → 89.9
    assert score.composite == Decimal("89.9")


def test_vendor_score_ontime_all_on_time():
    score = compute_vendor_score(
        vendor_id="v1",
        vendor_name="Punctual Co",
        accuracy_input={"approved_count": 0, "corrected_count": 0},  # N/A
        dispute_input={"total_invoices": 0, "exception_invoices": 0},  # N/A
        ontime_input={"gr_count": 5, "on_time_count": 5, "source": "expected_date"},
    )
    ontime = next(s for s in score.sub_scores if s.name == "on_time")
    assert ontime.score == Decimal("100.0")
    assert ontime.sample_size == 5
    assert "expected" in ontime.detail.lower()
    # Only on_time available → composite equals it.
    assert score.composite == Decimal("100.0")


def test_vendor_score_ontime_some_late():
    score = compute_vendor_score(
        vendor_id="v1",
        vendor_name="Mixed Co",
        accuracy_input={"approved_count": 0, "corrected_count": 0},
        dispute_input={"total_invoices": 0, "exception_invoices": 0},
        ontime_input={"gr_count": 4, "on_time_count": 3, "source": "expected_date"},
    )
    ontime = next(s for s in score.sub_scores if s.name == "on_time")
    assert ontime.score == Decimal("75.0")  # 3 of 4
    assert ontime.sample_size == 4


def test_vendor_score_ontime_no_comparable_data_na():
    """Zero comparable POs (none carry both an expected date and a receipt) →
    honest N/A, never a misleading 0, never a divide-by-zero."""
    score = compute_vendor_score(
        vendor_id="v1",
        vendor_name="No Data Co",
        accuracy_input={"approved_count": 10, "corrected_count": 1},  # 90.0
        dispute_input={"total_invoices": 0, "exception_invoices": 0},
        ontime_input={"gr_count": 0, "on_time_count": 0, "source": "expected_date"},
    )
    ontime = next(s for s in score.sub_scores if s.name == "on_time")
    assert ontime.score is None
    assert ontime.sample_size == 0
    assert "expected delivery date" in ontime.detail
    # on_time drops out; composite is accuracy alone (the only available score).
    assert score.composite == Decimal("90.0")


def test_vendor_score_composite_includes_ontime():
    """The composite folds in on-time at weight 0.3, renormalized over the three
    available sub-scores: (0.4*90 + 0.3*80 + 0.3*100) / (0.4+0.3+0.3)."""
    score = compute_vendor_score(
        vendor_id="v1",
        vendor_name="Full Co",
        accuracy_input={"approved_count": 10, "corrected_count": 1},  # 90.0
        dispute_input={"total_invoices": 10, "exception_invoices": 2},  # 80.0
        ontime_input={"gr_count": 4, "on_time_count": 4, "source": "expected_date"},  # 100.0
    )
    # (36 + 24 + 30) / 1.0 = 90.0
    assert score.composite == Decimal("90.0")
    ontime = next(s for s in score.sub_scores if s.name == "on_time")
    assert ontime.score == Decimal("100.0")


def test_vendor_score_ontime_proxy_detail_distinct():
    """When the source is the weak due-date proxy, the detail must say so (it
    must not claim a PO expected delivery date it didn't use)."""
    score = compute_vendor_score(
        vendor_id="v1",
        vendor_name="Proxy Co",
        accuracy_input={"approved_count": 0, "corrected_count": 0},
        dispute_input={"total_invoices": 0, "exception_invoices": 0},
        ontime_input={"gr_count": 2, "on_time_count": 1, "source": "due_date_proxy"},
    )
    ontime = next(s for s in score.sub_scores if s.name == "on_time")
    assert ontime.score == Decimal("50.0")
    assert "proxy" in ontime.detail.lower()
    assert "expected" not in ontime.detail.lower()


def test_vendor_score_no_history_degrades_gracefully():
    score = compute_vendor_score(
        vendor_id="v1",
        vendor_name="Empty",
        accuracy_input={"approved_count": 0, "corrected_count": 0},
        dispute_input={"total_invoices": 0, "exception_invoices": 0},
        ontime_input=None,
    )
    assert all(s.score is None for s in score.sub_scores)
    assert score.composite is None


def test_vendor_score_dispute_rate_math():
    score = compute_vendor_score(
        vendor_id="v1",
        vendor_name="Acme",
        accuracy_input={"approved_count": 0, "corrected_count": 0},  # N/A
        dispute_input={"total_invoices": 40, "exception_invoices": 3},
        ontime_input=None,
    )
    disp = next(s for s in score.sub_scores if s.name == "dispute")
    assert disp.score == Decimal("92.5")
    # Only dispute available → composite equals it.
    assert score.composite == Decimal("92.5")


def test_decimal_only_no_floats():
    rows = [_hist(gl="6000")] * 8 + [_hist(gl="6100")] * 2
    fs = suggest_fields(rows, _EMPTY_CURRENT)[0]
    assert isinstance(fs, FieldSuggestion)
    assert isinstance(fs.confidence, Decimal) and not isinstance(fs.confidence, float)

    history = [_line(item_code="W-A", unit_price=p) for p in ("10", "10", "10")]
    pf = detect_price_variance([_line(item_code="W-A", unit_price="13")], history)[0]
    assert isinstance(pf, PriceVarianceFlag)
    for attr in ("current_unit_price", "baseline_unit_price", "delta", "delta_pct"):
        v = getattr(pf, attr)
        assert isinstance(v, Decimal) and not isinstance(v, float)

    score = compute_vendor_score(
        vendor_id="v1",
        vendor_name="Acme",
        accuracy_input={"approved_count": 25, "corrected_count": 3},
        dispute_input={"total_invoices": 40, "exception_invoices": 3},
        ontime_input=None,
    )
    assert isinstance(score.composite, Decimal) and not isinstance(score.composite, float)
    for s in score.sub_scores:
        assert s.score is None or (isinstance(s.score, Decimal) and not isinstance(s.score, float))


# ---------------------------------------------------------------------------
# Real-DB / API tests (realdb fixture)
# ---------------------------------------------------------------------------


async def _seed_vendor_with_history(
    mk,
    org_id,
    actor_id,
    *,
    vendor_name="Acme Supplies",
    n_approved=6,
    gl="6000",
    item_code="W-A",
    hist_unit_price="10.00",
):
    """Create a vendor + N approved invoices (each with the dominant GL + one
    line item) + matching approval audit rows. Returns the vendor id."""
    from app.models.invoice import Invoice, InvoiceLineItem, InvoiceStatus
    from app.models.vendor import Vendor
    from app.models.workflow import AuditLog

    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name=vendor_name, status="active")
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)
        base = datetime.now(UTC) - timedelta(days=30)
        for i in range(n_approved):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"H-{i}",
                vendor_name=vendor_name,
                vendor_id=vendor.id,
                amount=Decimal("1000.00"),
                status=InvoiceStatus.approved,
                gl_account=gl,
                payment_terms="NET30",
            )
            s.add(inv)
            await s.commit()
            await s.refresh(inv)
            s.add(
                InvoiceLineItem(
                    invoice_id=inv.id,
                    item_code=item_code,
                    description="Widget A",
                    unit_price=Decimal(hist_unit_price),
                )
            )
            s.add(
                AuditLog(
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv.id,
                    details=None,
                    created_at=base + timedelta(days=i),
                )
            )
        await s.commit()
        return vendor.id


async def _seed_draft(mk, org_id, *, vendor_id, vendor_name, item_code="W-A", unit_price="13.00"):
    from app.models.invoice import Invoice, InvoiceLineItem, InvoiceStatus

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number="DRAFT-1",
            vendor_name=vendor_name,
            vendor_id=vendor_id,
            amount=Decimal("1000.00"),
            status=InvoiceStatus.ready_for_review,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        s.add(
            InvoiceLineItem(
                invoice_id=inv.id,
                item_code=item_code,
                description="Widget A",
                unit_price=Decimal(unit_price),
            )
        )
        await s.commit()
        return inv.id


async def test_suggestions_endpoint_happy_path(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_vendor_with_history(mk, org_id, actor_id)
    draft_id = await _seed_draft(mk, org_id, vendor_id=vid, vendor_name="Acme Supplies")

    async with realdb.client(key="a", role="ap_clerk") as client:
        r = await client.get(f"/api/enrichment/invoices/{draft_id}/suggestions")
    assert r.status_code == 200
    data = r.json()
    gl = next(s for s in data["field_suggestions"] if s["field"] == "gl_account")
    assert gl["value"] == "6000"
    assert gl["confidence"] == "100.0"
    # Draft line is 13.00 vs baseline 10.00 → +30% → warning.
    assert len(data["price_variances"]) == 1
    pv = data["price_variances"][0]
    assert pv["baseline_unit_price"] == "10.00"
    assert pv["delta_pct"] == "30.0"
    assert pv["severity"] == "warning"


@pytest.mark.parametrize(
    ("dominant", "chart", "suggested"),
    [
        # An active account of the invoice's chart: offered.
        ("6000", [("6000", True)], True),
        # Retired since the vendor was coded to it: a save would refuse it.
        ("6000", [("6000", False), ("7000", True)], False),
        # In no chart while the chart has active accounts: refused on save.
        ("6000", [("7000", True)], False),
    ],
)
async def test_gl_suggestion_is_only_offered_when_a_save_would_accept_it(
    realdb, dominant, chart, suggested
):
    """The suggestion is applied into the form and then SAVED, and the save
    holds the code to the invoice's chart (`services/gl_chart`, decisions
    §199). A suggestion the save would refuse is advice the reviewer cannot
    take — so it is not offered. The other fields' suggestions are unaffected."""
    from app.models.gl_account import GLAccount

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    async with mk() as s:
        s.add_all(
            [
                GLAccount(organization_id=org_id, code=code, name=code, is_active=active)
                for code, active in chart
            ]
        )
        await s.commit()
    vid = await _seed_vendor_with_history(mk, org_id, actor_id, gl=dominant)
    draft_id = await _seed_draft(mk, org_id, vendor_id=vid, vendor_name="Acme Supplies")

    async with realdb.client(key="a", role="ap_clerk") as client:
        r = await client.get(f"/api/enrichment/invoices/{draft_id}/suggestions")
    assert r.status_code == 200, r.text
    fields = {f["field"]: f["value"] for f in r.json()["field_suggestions"]}
    assert (fields.get("gl_account") == dominant) is suggested, fields
    assert fields.get("payment_terms") == "NET30"


async def test_suggestions_vendorless_draft_empty(realdb):
    from app.models.invoice import Invoice, InvoiceStatus

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number="NOVEN-1",
            vendor_name="Mystery",
            amount=Decimal("500.00"),
            status=InvoiceStatus.ready_for_review,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        draft_id = inv.id

    async with realdb.client(key="a", role="ap_clerk") as client:
        r = await client.get(f"/api/enrichment/invoices/{draft_id}/suggestions")
    assert r.status_code == 200
    data = r.json()
    assert data["vendor_id"] is None
    assert data["field_suggestions"] == []
    assert data["price_variances"] == []


async def test_score_endpoint_happy_path_no_pii(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_vendor_with_history(mk, org_id, actor_id, n_approved=6)
    # Give the vendor a tax_id + bank_details to make sure they never leak.
    from sqlalchemy import update

    from app.models.vendor import Vendor

    async with mk() as s:
        await s.execute(
            update(Vendor)
            .where(Vendor.id == vid)
            .values(tax_id="12-3456789", bank_details={"account": "999000111"})
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/enrichment/vendors/{vid}/score")
    assert r.status_code == 200
    body_text = r.text
    data = r.json()
    names = {s["name"] for s in data["sub_scores"]}
    assert names == {"accuracy", "dispute", "on_time"}
    ontime = next(s for s in data["sub_scores"] if s["name"] == "on_time")
    assert ontime["score"] is None  # N/A by default
    # PII never present in the response body.
    assert "tax_id" not in body_text
    assert "bank_details" not in body_text
    assert "12-3456789" not in body_text
    assert "999000111" not in body_text


async def test_unknown_ids_404(realdb):
    async with realdb.client(key="a", role="ap_manager") as client:
        r1 = await client.get(f"/api/enrichment/invoices/{uuid.uuid4()}/suggestions")
        r2 = await client.get(f"/api/enrichment/vendors/{uuid.uuid4()}/score")
    assert r1.status_code == 404
    assert r2.status_code == 404


async def test_auth_required(realdb):
    async with realdb.client(key="a", role=None) as client:
        r1 = await client.get(f"/api/enrichment/invoices/{uuid.uuid4()}/suggestions")
        r2 = await client.get(f"/api/enrichment/vendors/{uuid.uuid4()}/score")
    assert r1.status_code == 401
    assert r2.status_code == 401


async def test_rbac_clerk_allowed_on_suggestions_forbidden_on_score(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_vendor_with_history(mk, org_id, actor_id)
    draft_id = await _seed_draft(mk, org_id, vendor_id=vid, vendor_name="Acme Supplies")

    async with realdb.client(key="a", role="ap_clerk") as clerk:
        # Clerk reviews drafts → suggestions OK.
        assert (
            await clerk.get(f"/api/enrichment/invoices/{draft_id}/suggestions")
        ).status_code == 200
        # Score is managerial → 403 for a clerk.
        assert (await clerk.get(f"/api/enrichment/vendors/{vid}/score")).status_code == 403


async def test_tenant_isolation_vendor_score(realdb):
    """A vendor that exists only in tenant A is 404 when queried with tenant B's
    header + token (tenant-DB scoping makes it not-found, not 403)."""
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    actor_a = realdb.info("a").users["ap_manager"]
    vid = await _seed_vendor_with_history(mk_a, org_a, actor_a)

    async with realdb.client(key="b", role="ap_manager") as client_b:
        r = await client_b.get(f"/api/enrichment/vendors/{vid}/score")
    assert r.status_code == 404


async def test_suggestions_endpoint_autofills_payment_terms(realdb):
    """Auto-fill surfaces the dominant historical *payment_terms* (not just GL)
    with the right confidence + evidence string, and only on a draft that has no
    terms of its own (suggestion-only, never overwrites)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    # _seed_vendor_with_history sets payment_terms="NET30" on every history row.
    vid = await _seed_vendor_with_history(mk, org_id, actor_id, n_approved=5)
    # Draft has no gl/terms of its own so both can be suggested.
    draft_id = await _seed_draft(mk, org_id, vendor_id=vid, vendor_name="Acme Supplies")

    async with realdb.client(key="a", role="ap_clerk") as client:
        r = await client.get(f"/api/enrichment/invoices/{draft_id}/suggestions")
    assert r.status_code == 200
    data = r.json()
    terms = next(s for s in data["field_suggestions"] if s["field"] == "payment_terms")
    assert terms["value"] == "NET30"
    assert terms["confidence"] == "100.0"
    assert terms["sample_size"] == 5
    assert terms["occurrences"] == 5
    assert "5 of 5" in terms["evidence"]
    # Decimals ride the wire as strings, never floats.
    assert isinstance(terms["confidence"], str)


async def test_suggestions_endpoint_does_not_overwrite_populated_terms(realdb):
    """If the draft already carries payment_terms, it must NOT be suggested —
    the endpoint surfaces hints, it never proposes overwriting a set value."""
    from app.models.invoice import Invoice, InvoiceStatus

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_vendor_with_history(mk, org_id, actor_id, n_approved=5)

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number="DRAFT-TERMS",
            vendor_name="Acme Supplies",
            vendor_id=vid,
            amount=Decimal("1000.00"),
            status=InvoiceStatus.ready_for_review,
            payment_terms="NET60",  # already set → must be suppressed
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        draft_id = inv.id

    async with realdb.client(key="a", role="ap_clerk") as client:
        r = await client.get(f"/api/enrichment/invoices/{draft_id}/suggestions")
    assert r.status_code == 200
    data = r.json()
    assert [s for s in data["field_suggestions"] if s["field"] == "payment_terms"] == []


async def test_score_endpoint_no_history_vendor_graceful(realdb):
    """A vendor with no invoices at all returns a clean all-N/A score and a
    null composite — no 500, no division-by-zero."""
    from app.models.vendor import Vendor

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="Brand New Co", status="active")
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)
        vid = vendor.id

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/enrichment/vendors/{vid}/score")
    assert r.status_code == 200
    data = r.json()
    assert data["composite"] is None
    assert {s["name"] for s in data["sub_scores"]} == {"accuracy", "dispute", "on_time"}
    assert all(s["score"] is None for s in data["sub_scores"])


async def _enable_ontime_proxy(ctrl_mk, org_id):
    """Flip the org's ``settings.enrichment.ontime_use_due_date_proxy`` flag on
    (it ships off by default — the due-date proxy is opt-in)."""
    from sqlalchemy import select

    from app.models.organization import Organization

    async with ctrl_mk() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        settings = dict(org.settings or {})
        settings["enrichment"] = {"ontime_use_due_date_proxy": True}
        org.settings = settings
        await s.commit()


async def test_score_ontime_na_for_no_gr_vendor_even_with_proxy_on(realdb):
    """The 'no-GR vendor' case the task names: with the due-date proxy *enabled*,
    a vendor that has approved invoices (so accuracy/dispute score) but no goods
    receipts to compare must still report on_time = N/A — there's nothing to
    measure on-time against — while the composite renormalizes over the two
    available sub-scores."""
    mk = realdb.sessionmaker("a")
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    vid = await _seed_vendor_with_history(mk, org_id, actor_id, n_approved=6)
    await _enable_ontime_proxy(ctrl_mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/enrichment/vendors/{vid}/score")
    assert r.status_code == 200
    data = r.json()
    ontime = next(s for s in data["sub_scores"] if s["name"] == "on_time")
    assert ontime["score"] is None  # no goods receipts → N/A
    assert ontime["sample_size"] == 0
    # accuracy + dispute are present and feed a non-null composite.
    acc = next(s for s in data["sub_scores"] if s["name"] == "accuracy")
    disp = next(s for s in data["sub_scores"] if s["name"] == "dispute")
    assert acc["score"] is not None
    assert disp["score"] is not None
    assert data["composite"] is not None


async def test_score_ontime_proxy_computes_when_gr_present(realdb):
    """With the proxy on AND goods receipts whose received_date precedes the
    linked invoice's due_date, on_time becomes a real score (not N/A) — proving
    the GR→PO→Invoice(po_number) join + received_date<=due_date math the endpoint
    runs. One on-time GR + one late GR over two POs → 50.0."""
    from datetime import date

    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.procurement import GoodsReceipt, PurchaseOrder
    from app.models.vendor import Vendor

    mk = realdb.sessionmaker("a")
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id

    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="GR Co", status="active")
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)
        vid = vendor.id

        # Two POs, each with a matching invoice (by po_number) carrying a due_date,
        # and one goods receipt each: PO-1 received on time, PO-2 received late.
        specs = [
            ("PO-1", date(2026, 1, 10), date(2026, 1, 5)),  # received <= due → on time
            ("PO-2", date(2026, 2, 1), date(2026, 2, 20)),  # received >  due → late
        ]
        for po_number, due, received in specs:
            po = PurchaseOrder(
                organization_id=org_id,
                po_number=po_number,
                vendor_id=vid,
                total=Decimal("100.00"),
                status="open",
            )
            s.add(po)
            await s.commit()
            await s.refresh(po)
            s.add(
                Invoice(
                    organization_id=org_id,
                    invoice_number=f"INV-{po_number}",
                    vendor_name="GR Co",
                    vendor_id=vid,
                    amount=Decimal("100.00"),
                    status=InvoiceStatus.approved,
                    po_number=po_number,
                    due_date=due,
                )
            )
            s.add(
                GoodsReceipt(
                    organization_id=org_id,
                    gr_number=f"GR-{po_number}",
                    po_id=po.id,
                    received_date=received,
                    status="received",
                )
            )
            await s.commit()

    await _enable_ontime_proxy(ctrl_mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/enrichment/vendors/{vid}/score")
    assert r.status_code == 200
    data = r.json()
    ontime = next(s for s in data["sub_scores"] if s["name"] == "on_time")
    assert ontime["sample_size"] == 2
    assert ontime["score"] == "50.0"  # 1 of 2 receipts on or before due date


async def test_score_ontime_expected_date_computes_without_proxy(realdb):
    """The real on-time signal: POs carrying an ``expected_delivery_date`` plus a
    goods receipt, scored ``received_date <= expected_delivery_date``. NO org
    flag needed — this is authoritative, not the opt-in proxy. Three POs: two on
    time, one late → 66.7. A PO with an expected date but no receipt, and one
    with a receipt but no expected date, contribute nothing."""
    from datetime import date

    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.procurement import GoodsReceipt, PurchaseOrder
    from app.models.vendor import Vendor

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="Delivery Co", status="active")
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)
        vid = vendor.id

        # (po_number, expected_delivery_date | None, received_date | None)
        specs = [
            ("EP-1", date(2026, 1, 10), date(2026, 1, 5)),  # on time
            ("EP-2", date(2026, 2, 10), date(2026, 2, 10)),  # on time (== boundary)
            ("EP-3", date(2026, 3, 1), date(2026, 3, 20)),  # late
            ("EP-4", date(2026, 4, 1), None),  # expected date, no GR → excluded
            ("EP-5", None, date(2026, 5, 1)),  # GR, no expected date → excluded
        ]
        for po_number, expected, received in specs:
            po = PurchaseOrder(
                organization_id=org_id,
                po_number=po_number,
                vendor_id=vid,
                total=Decimal("100.00"),
                status="open",
                expected_delivery_date=expected,
            )
            s.add(po)
            await s.commit()
            await s.refresh(po)
            if received is not None:
                s.add(
                    GoodsReceipt(
                        organization_id=org_id,
                        gr_number=f"GR-{po_number}",
                        po_id=po.id,
                        received_date=received,
                        status="received",
                    )
                )
        # Give the vendor an approved invoice so accuracy/dispute also score and
        # we can assert on-time folds into a composite alongside them.
        inv = Invoice(
            organization_id=org_id,
            invoice_number="DELIV-1",
            vendor_name="Delivery Co",
            vendor_id=vid,
            amount=Decimal("100.00"),
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()

    # NO proxy flag flipped — the real expected-date signal must compute on its own.
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/enrichment/vendors/{vid}/score")
    assert r.status_code == 200
    data = r.json()
    ontime = next(s for s in data["sub_scores"] if s["name"] == "on_time")
    assert ontime["sample_size"] == 3  # EP-4 / EP-5 excluded
    assert ontime["score"] == "66.7"  # 2 of 3 on or before expected date
    assert "expected delivery date" in ontime["detail"]
    # On-time folds into the composite.
    assert data["composite"] is not None


async def test_score_accuracy_dedupes_multi_approval_invoice(realdb):
    """An invoice can accumulate MORE THAN ONE ``invoice.approved`` audit row
    (rejected → re-approved, or a voided payment returning it to ``approved``
    and being re-approved). The accuracy sub-score must count *distinct
    invoices*, not raw audit rows — otherwise a re-approved invoice inflates
    both the sample size and (if any approval carried changes) the correction
    count. Two distinct invoices, one approved twice → sample_size must be 2,
    not 3."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.vendor import Vendor
    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    actor_id = realdb.info("a").users["ap_manager"]
    base = datetime.now(UTC) - timedelta(days=10)

    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="Reapprove Co", status="active")
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)
        vid = vendor.id

        # Invoice A: approved twice (a clean re-approval cycle, no changes).
        inv_a = Invoice(
            organization_id=org_id,
            invoice_number="RA-A",
            vendor_name="Reapprove Co",
            vendor_id=vid,
            amount=Decimal("100.00"),
            status=InvoiceStatus.approved,
        )
        # Invoice B: approved once, with field corrections.
        inv_b = Invoice(
            organization_id=org_id,
            invoice_number="RA-B",
            vendor_name="Reapprove Co",
            vendor_id=vid,
            amount=Decimal("200.00"),
            status=InvoiceStatus.approved,
        )
        s.add_all([inv_a, inv_b])
        await s.commit()
        await s.refresh(inv_a)
        await s.refresh(inv_b)

        s.add_all(
            [
                AuditLog(
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv_a.id,
                    details=None,
                    created_at=base,
                ),
                AuditLog(
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv_a.id,  # SAME invoice, second approval
                    details=None,
                    created_at=base + timedelta(days=1),
                ),
                AuditLog(
                    organization_id=org_id,
                    actor_id=actor_id,
                    action="invoice.approved",
                    entity_type="invoice",
                    entity_id=inv_b.id,
                    details={"changes": {"gl_account": {"old": "6000", "new": "6100"}}},
                    created_at=base + timedelta(days=2),
                ),
            ]
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/enrichment/vendors/{vid}/score")
    assert r.status_code == 200
    data = r.json()
    acc = next(s for s in data["sub_scores"] if s["name"] == "accuracy")
    # Two DISTINCT invoices (not 3 audit rows); one carried corrections → 50.0.
    assert acc["sample_size"] == 2
    assert acc["score"] == "50.0"
    assert "1 of 2" in acc["detail"]


# ---------------------------------------------------------------------------
# Vendor consolidation — pure clustering (no DB)
# ---------------------------------------------------------------------------


def _vrec(vid, name, *, code=None, tax_id=None, status="active", invoice_count=0, age_rank=0):
    from app.services.vendor_consolidation import VendorRecord

    return VendorRecord(
        id=vid,
        name=name,
        code=code,
        tax_id=tax_id,
        status=status,
        invoice_count=invoice_count,
        age_rank=age_rank,
    )


def test_consolidation_same_tax_id_clusters():
    from app.services.vendor_consolidation import find_consolidation_clusters

    vendors = [
        _vrec("v1", "Acme Industrial Inc", tax_id="12-3456789", invoice_count=10),
        _vrec("v2", "Totally Different Name LLC", tax_id="123456789", invoice_count=2),
        _vrec("v3", "Unrelated Co", tax_id="99-9999999", invoice_count=1),
    ]
    clusters, truncated = find_consolidation_clusters(vendors)
    assert truncated is False
    assert len(clusters) == 1
    c = clusters[0]
    assert {m.vendor_id for m in c.members} == {"v1", "v2"}
    assert "same tax id" in c.reasons
    assert c.score == Decimal("1.00")


def test_consolidation_near_identical_names_cluster():
    from app.services.vendor_consolidation import find_consolidation_clusters

    vendors = [
        _vrec("v1", "Acme Supplies Inc.", invoice_count=5),
        _vrec("v2", "Acme Supplies, LLC", invoice_count=3),
        _vrec("v3", "Globex Corporation", invoice_count=1),
    ]
    clusters, _ = find_consolidation_clusters(vendors)
    assert len(clusters) == 1
    c = clusters[0]
    assert {m.vendor_id for m in c.members} == {"v1", "v2"}
    assert any(r.startswith("names ") for r in c.reasons)


def test_consolidation_distinct_vendors_no_cluster():
    from app.services.vendor_consolidation import find_consolidation_clusters

    vendors = [
        _vrec("v1", "Acme Supplies", tax_id="11-1111111", code="ACME"),
        _vrec("v2", "Globex Corporation", tax_id="22-2222222", code="GLBX"),
        _vrec("v3", "Initech Systems", tax_id="33-3333333", code="INIT"),
    ]
    clusters, truncated = find_consolidation_clusters(vendors)
    assert clusters == []
    assert truncated is False


def test_consolidation_canonical_is_highest_invoice_volume_deterministic():
    from app.services.vendor_consolidation import find_consolidation_clusters

    # v2 has the most invoices → canonical. Run twice → identical result.
    vendors = [
        _vrec("v1", "Acme Inc", tax_id="55-5555555", invoice_count=3, age_rank=0),
        _vrec("v2", "Acme LLC", tax_id="555555555", invoice_count=20, age_rank=1),
        _vrec("v3", "Acme Co", tax_id="5555-55555", invoice_count=1, age_rank=2),
    ]
    c1, _ = find_consolidation_clusters(vendors)
    c2, _ = find_consolidation_clusters(vendors)
    assert c1[0].canonical_vendor_id == "v2"
    assert c1[0].members[0].is_canonical is True
    assert c1[0].members[0].vendor_id == "v2"  # canonical sorts first
    assert [m.vendor_id for m in c1[0].members] == [m.vendor_id for m in c2[0].members]


def test_consolidation_canonical_tie_breaks_to_oldest():
    from app.services.vendor_consolidation import find_consolidation_clusters

    # Equal invoice volume → oldest (lowest age_rank) wins.
    vendors = [
        _vrec("vYoung", "Acme Inc", tax_id="77-7777777", invoice_count=4, age_rank=5),
        _vrec("vOld", "Acme LLC", tax_id="777777777", invoice_count=4, age_rank=0),
    ]
    clusters, _ = find_consolidation_clusters(vendors)
    assert clusters[0].canonical_vendor_id == "vOld"


def test_consolidation_tax_id_masked_never_full():
    from app.services.vendor_consolidation import (
        find_consolidation_clusters,
        mask_tax_id,
    )

    assert mask_tax_id("12-3456789") == "***6789"
    assert mask_tax_id("ab") == "***"
    assert mask_tax_id(None) is None

    vendors = [
        _vrec("v1", "Acme Inc", tax_id="12-3456789", invoice_count=2),
        _vrec("v2", "Acme LLC", tax_id="123456789", invoice_count=1),
    ]
    clusters, _ = find_consolidation_clusters(vendors)
    for m in clusters[0].members:
        assert m.tax_id_masked == "***6789"
    # The raw / normalized tax id never appears anywhere in the cluster repr.
    assert "123456789" not in repr(clusters[0])
    assert "12-3456789" not in repr(clusters[0])


def test_consolidation_transitive_clustering():
    from app.services.vendor_consolidation import find_consolidation_clusters

    # A~B by tax id, B~C by code → all three in one cluster even though A and C
    # share no direct evidence.
    vendors = [
        _vrec("A", "Acme Supplies", tax_id="88-8888888", invoice_count=1),
        _vrec("B", "Acme Supplies", tax_id="888888888", code="SHARED", invoice_count=2),
        _vrec("C", "Wildly Different Name", code="SHARED", invoice_count=1),
    ]
    clusters, _ = find_consolidation_clusters(vendors)
    assert len(clusters) == 1
    assert {m.vendor_id for m in clusters[0].members} == {"A", "B", "C"}


# ---------------------------------------------------------------------------
# Vendor consolidation — real-DB / API
# ---------------------------------------------------------------------------


async def test_consolidation_endpoint_clusters_duplicates(realdb):
    from app.models.vendor import Vendor

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        s.add_all(
            [
                Vendor(
                    organization_id=org_id,
                    name="Acme Supplies Inc.",
                    tax_id="12-3456789",
                    status="active",
                ),
                Vendor(
                    organization_id=org_id,
                    name="Acme Supplies LLC",
                    tax_id="123456789",
                    status="active",
                ),
                Vendor(organization_id=org_id, name="Globex Corporation", status="active"),
            ]
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get("/api/enrichment/vendors/consolidation-suggestions")
    assert r.status_code == 200
    data = r.json()
    assert data["cluster_count"] == 1
    assert data["truncated"] is False
    cluster = data["clusters"][0]
    assert len(cluster["members"]) == 2
    assert "same tax id" in cluster["reasons"]
    # Tax id is masked in the response, never full.
    for m in cluster["members"]:
        assert m["tax_id_masked"] == "***6789"
    assert "123456789" not in r.text
    assert "3456789" not in r.text.replace("***6789", "")


async def test_consolidation_endpoint_canonical_pick(realdb):
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.vendor import Vendor

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        v_big = Vendor(
            organization_id=org_id, name="Beta Co Inc", tax_id="44-4444444", status="active"
        )
        v_small = Vendor(
            organization_id=org_id, name="Beta Co LLC", tax_id="444444444", status="active"
        )
        s.add_all([v_big, v_small])
        await s.commit()
        await s.refresh(v_big)
        await s.refresh(v_small)
        # Give v_big more invoices → it should be canonical.
        for i in range(3):
            s.add(
                Invoice(
                    organization_id=org_id,
                    invoice_number=f"BIG-{i}",
                    vendor_name="Beta Co Inc",
                    vendor_id=v_big.id,
                    amount=Decimal("100.00"),
                    status=InvoiceStatus.approved,
                )
            )
        s.add(
            Invoice(
                organization_id=org_id,
                invoice_number="SMALL-0",
                vendor_name="Beta Co LLC",
                vendor_id=v_small.id,
                amount=Decimal("100.00"),
                status=InvoiceStatus.approved,
            )
        )
        await s.commit()
        big_id = str(v_big.id)

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get("/api/enrichment/vendors/consolidation-suggestions")
    assert r.status_code == 200
    cluster = r.json()["clusters"][0]
    assert cluster["canonical_vendor_id"] == big_id
    canonical = next(m for m in cluster["members"] if m["is_canonical"])
    assert canonical["vendor_id"] == big_id
    assert canonical["invoice_count"] == 3


async def test_consolidation_endpoint_rbac_clerk_forbidden(realdb):
    async with realdb.client(key="a", role="ap_clerk") as client:
        r = await client.get("/api/enrichment/vendors/consolidation-suggestions")
    assert r.status_code == 403


async def test_consolidation_endpoint_auth_required(realdb):
    async with realdb.client(key="a", role=None) as client:
        r = await client.get("/api/enrichment/vendors/consolidation-suggestions")
    assert r.status_code == 401


async def test_consolidation_endpoint_tenant_isolation(realdb):
    """Tenant A's duplicate vendors must not appear when tenant B asks for its
    own consolidation suggestions."""
    from app.models.vendor import Vendor

    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    async with mk_a() as s:
        s.add_all(
            [
                Vendor(organization_id=org_a, name="Dupe One Inc", tax_id="66-6666666"),
                Vendor(organization_id=org_a, name="Dupe One LLC", tax_id="666666666"),
            ]
        )
        await s.commit()

    async with realdb.client(key="b", role="ap_manager") as client_b:
        r = await client_b.get("/api/enrichment/vendors/consolidation-suggestions")
    assert r.status_code == 200
    assert "Dupe One" not in r.text


async def test_score_dispute_subscore_counts_exceptions(realdb):
    """The dispute sub-score reflects real exception rows: a vendor with N
    invoices, M of which raised a vendor-facing exception, scores
    (1 - M/N)*100. Two invoices, one with a po_mismatch exception → 50.0."""
    from app.models.exception import Exception as APException
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.vendor import Vendor

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        vendor = Vendor(organization_id=org_id, name="Disputey Co", status="active")
        s.add(vendor)
        await s.commit()
        await s.refresh(vendor)
        vid = vendor.id
        inv_ids = []
        for i in range(2):
            inv = Invoice(
                organization_id=org_id,
                invoice_number=f"D-{i}",
                vendor_name="Disputey Co",
                vendor_id=vid,
                amount=Decimal("100.00"),
                status=InvoiceStatus.approved,
            )
            s.add(inv)
            await s.commit()
            await s.refresh(inv)
            inv_ids.append(inv.id)
        # One of the two raises a vendor-facing exception.
        s.add(
            APException(
                organization_id=org_id,
                invoice_id=inv_ids[0],
                exception_type="po_mismatch",
                severity="warning",
                status="open",
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/enrichment/vendors/{vid}/score")
    assert r.status_code == 200
    data = r.json()
    disp = next(s for s in data["sub_scores"] if s["name"] == "dispute")
    assert disp["sample_size"] == 2
    assert disp["score"] == "50.0"  # 1 of 2 invoices raised an exception
