"""Composite vendor risk scoring + the risk endpoints.

Two tiers:

  * Real-Postgres (``realdb``) end-to-end over the scoring service and
    the three ``/api/vendors/*risk*`` routes — the sanctions ⋈ fraud ⋈
    payments blend, `recompute_and_persist` column writes, and the
    org-wide summary distribution all run against live tenant DBs.

Pins:
  * A vendor whose latest sanctions check is a `match` → `critical`.
  * A clean vendor with no history → `low`/`unknown` (genuinely
    untouched → `unknown`; a clear screen → `low`).
  * Open `fraud_flag` exceptions raise the composite.
  * `recompute_and_persist` writes risk_score / risk_level /
    risk_factors / risk_scored_at and never commits.
  * `GET /risk/summary` returns the per-bucket distribution.
  * Factors are PII-free — list NAMES / counts / scores only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.models.vendor import Vendor
from app.services.vendor_risk_scoring import compute_vendor_risk, recompute_and_persist

# ---------------------------------------------------------------------------
# Seed helpers (realdb)
# ---------------------------------------------------------------------------


async def _add_vendor(mk, org_id, *, name="Acme Supply", payments_blocked=False):
    from app.models.vendor import Vendor

    async with mk() as s:
        v = Vendor(
            organization_id=org_id,
            name=name,
            status="active",
            payments_blocked=payments_blocked,
        )
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return v.id


async def _add_sanctions_check(mk, org_id, vendor_id, *, result, matched_list, risk_score):
    from app.models.sanctions_check import SanctionsCheck

    async with mk() as s:
        s.add(
            SanctionsCheck(
                vendor_id=vendor_id,
                organization_id=org_id,
                provider="mock",
                check_type="manual",
                result=result,
                matched_list=matched_list,
                risk_score=Decimal(str(risk_score)) if risk_score is not None else None,
            )
        )
        await s.commit()


async def _add_invoice_with_fraud_flag(mk, org_id, vendor_id, *, number, status="open"):
    from app.models.exception import Exception as ExceptionModel
    from app.models.invoice import Invoice, InvoiceStatus

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Acme Supply",
            vendor_id=vendor_id,
            amount=Decimal("1000"),
            currency="USD",
            status=InvoiceStatus.new,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        s.add(
            ExceptionModel(
                organization_id=org_id,
                invoice_id=inv.id,
                exception_type="fraud_flag",
                severity="error",
                status=status,
            )
        )
        await s.commit()
        return inv.id


async def _add_completed_payment(
    mk,
    org_id,
    vendor_id,
    *,
    number,
    amount,
    status="completed",
    currency="USD",
    source_amount=None,
    source_currency=None,
):
    """Seed an invoice + its payment.

    `currency` is the INVOICE's currency — which is also what `Payment.amount`
    is denominated in. `source_amount` / `source_currency` are the rate-locked
    home-currency leg an international payment carries."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.payment import Payment

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Acme Supply",
            vendor_id=vendor_id,
            amount=Decimal(str(amount)),
            status=InvoiceStatus.approved,
            currency=currency,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        pay = Payment(
            invoice_id=inv.id,
            amount=Decimal(str(amount)),
            status=status,
            completed_at=datetime.now(UTC) if status == "completed" else None,
            source_amount=Decimal(str(source_amount)) if source_amount is not None else None,
            source_currency=source_currency,
        )
        s.add(pay)
        await s.commit()


# ---------------------------------------------------------------------------
# Scoring service (realdb)
# ---------------------------------------------------------------------------


async def test_sanctions_match_forces_critical(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Sanctioned Co")
    await _add_sanctions_check(
        mk, org_id, vid, result="match", matched_list="OFAC_SDN", risk_score="90"
    )

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        assessment = await compute_vendor_risk(s, vendor=vendor, organization_id=org_id)
    assert assessment.risk_level == "critical"
    # Factor breakdown is PII-free: names + scores only.
    assert assessment.factors["sanctions"]["latest_result"] == "match"
    assert assessment.factors["sanctions"]["matched_list"] == "OFAC_SDN"


async def test_blocked_vendor_is_critical_even_without_sanctions_check(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Manually Blocked", payments_blocked=True)

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        assessment = await compute_vendor_risk(s, vendor=vendor, organization_id=org_id)
    assert assessment.risk_level == "critical"


async def test_clean_vendor_no_history_is_unknown(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Brand New Vendor")

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        assessment = await compute_vendor_risk(s, vendor=vendor, organization_id=org_id)
    # Nothing screened, no fraud, no payments → genuinely unknown.
    assert assessment.risk_level == "unknown"
    assert assessment.risk_score == Decimal("0.00")


async def test_clear_screen_is_low_not_unknown(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Screened Clean")
    await _add_sanctions_check(mk, org_id, vid, result="clear", matched_list=None, risk_score="0")

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        assessment = await compute_vendor_risk(s, vendor=vendor, organization_id=org_id)
    # A clear screen is a *known* signal → low, not unknown.
    assert assessment.risk_level == "low"


async def test_fraud_flags_raise_the_score(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Flagged Vendor")

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        before = await compute_vendor_risk(s, vendor=vendor, organization_id=org_id)

    await _add_invoice_with_fraud_flag(mk, org_id, vid, number="F-1")
    await _add_invoice_with_fraud_flag(mk, org_id, vid, number="F-2")
    # A resolved flag must not count.
    await _add_invoice_with_fraud_flag(mk, org_id, vid, number="F-3", status="resolved")

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        after = await compute_vendor_risk(s, vendor=vendor, organization_id=org_id)

    assert after.risk_score > before.risk_score
    assert after.factors["fraud"]["open_fraud_flags"] == 2


async def test_payment_history_contributes_exposure(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="High Volume Vendor")
    await _add_completed_payment(mk, org_id, vid, number="P-1", amount="60000")
    await _add_completed_payment(mk, org_id, vid, number="P-2", amount="40000")
    await _add_completed_payment(mk, org_id, vid, number="P-3", amount="5000", status="failed")

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        assessment = await compute_vendor_risk(s, vendor=vendor, organization_id=org_id)

    ph = assessment.factors["payment_history"]
    assert ph["payment_count"] == 2
    assert ph["failed_payments"] == 1
    assert Decimal(ph["trailing_12m_amount"]) == Decimal("100000.00")
    # The figure is denominated, not a bare number.
    assert ph["currency"] == "USD"
    assert ph["unconverted_payments"] == 0
    # Some signal → not unknown.
    assert assessment.risk_level != "unknown"


# ---------------------------------------------------------------------------
# Payment history is a REPORTING-currency figure, never a raw SUM
# ---------------------------------------------------------------------------


async def test_foreign_currency_payment_is_not_summed_at_face_value(realdb):
    """`Payment.amount` is in the INVOICE's currency, so summing it raw mixes
    currencies. ¥10,000,000 used to land in a USD-denominated exposure ramp
    whose full-exposure point is 100,000 — pinning the sub-score at 100 off one
    ordinary Japanese invoice. It must be excluded and COUNTED, not converted at
    read time and not folded in at face value."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Tokyo Parts KK")
    await _add_completed_payment(mk, org_id, vid, number="JPY-1", amount="10000000", currency="JPY")

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        assessment = await compute_vendor_risk(s, vendor=vendor, organization_id=org_id)

    ph = assessment.factors["payment_history"]
    assert Decimal(ph["trailing_12m_amount"]) == Decimal("0.00")
    assert ph["unconverted_payments"] == 1
    # The payment still counts as a signal — the vendor isn't "untouched".
    assert ph["payment_count"] == 1
    assert assessment.risk_level != "unknown"


async def test_foreign_payment_with_a_locked_home_leg_is_counted(realdb):
    """An international payment carries the rate-locked home-currency debit on
    `source_amount`/`source_currency`. That IS the cash that left the bank, so
    it counts — at the locked figure, never re-fetched."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Tokyo Parts KK 2")
    await _add_completed_payment(
        mk,
        org_id,
        vid,
        number="JPY-2",
        amount="10000000",
        currency="JPY",
        source_amount="65000",
        source_currency="USD",
    )

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        assessment = await compute_vendor_risk(s, vendor=vendor, organization_id=org_id)

    ph = assessment.factors["payment_history"]
    assert Decimal(ph["trailing_12m_amount"]) == Decimal("65000.00")
    assert ph["unconverted_payments"] == 0


async def test_unconvertible_volume_does_not_inflate_the_bucket(realdb):
    """The user-visible consequence: with the raw sum, one JPY invoice on top of
    a `review_required` screen scored 60*0.55 + 100*0.15 = 48 → `medium`. The
    unconvertible volume must contribute nothing, leaving 33 → `low`."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Reviewed Tokyo Co")
    await _add_sanctions_check(
        mk, org_id, vid, result="review_required", matched_list=None, risk_score=None
    )
    await _add_completed_payment(mk, org_id, vid, number="JPY-3", amount="10000000", currency="JPY")

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        assessment = await compute_vendor_risk(s, vendor=vendor, organization_id=org_id)

    assert assessment.risk_score == Decimal("33.00")
    assert assessment.risk_level == "low"


async def test_recompute_and_persist_writes_columns(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Persist Me")
    await _add_sanctions_check(
        mk, org_id, vid, result="match", matched_list="OFAC_SDN", risk_score="90"
    )

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
        assessment = await recompute_and_persist(s, vendor=vendor, organization_id=org_id)
        # Service does NOT commit — the test owns the txn.
        await s.commit()

    async with mk() as s:
        fresh = (await s.execute(select(Vendor).where(Vendor.id == vid))).scalar_one()
    assert fresh.risk_level == "critical"
    assert fresh.risk_score == assessment.risk_score
    assert fresh.risk_factors is not None
    assert fresh.risk_factors["sanctions"]["latest_result"] == "match"
    assert fresh.risk_scored_at is not None


# ---------------------------------------------------------------------------
# Risk endpoints (realdb)
# ---------------------------------------------------------------------------


async def test_get_vendor_risk_returns_persisted(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Read Risk")

    async with realdb.client(key="a", role="ap_clerk") as client:
        r = await client.get(f"/api/vendors/{vid}/risk")
    assert r.status_code == 200
    body = r.json()
    assert body["vendor_id"] == str(vid)
    assert body["risk_level"] == "unknown"


async def test_recompute_endpoint_updates_persisted(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Recompute Me")
    await _add_sanctions_check(
        mk, org_id, vid, result="match", matched_list="OFAC_SDN", risk_score="90"
    )

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(f"/api/vendors/{vid}/risk/recompute")
    assert r.status_code == 200
    assert r.json()["risk_level"] == "critical"

    # Persisted: a subsequent GET reflects the recompute.
    async with realdb.client(key="a", role="ap_clerk") as client:
        r2 = await client.get(f"/api/vendors/{vid}/risk")
    assert r2.json()["risk_level"] == "critical"


async def test_recompute_404_for_missing_vendor(realdb):
    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.post(f"/api/vendors/{uuid.uuid4()}/risk/recompute")
    assert r.status_code == 404


async def test_recompute_forbidden_for_clerk(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="RBAC Vendor")

    async with realdb.client(key="a", role="ap_clerk") as client:
        r = await client.post(f"/api/vendors/{vid}/risk/recompute")
    assert r.status_code == 403


async def test_risk_summary_distribution(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    # Two critical (sanctions match), one untouched (unknown).
    v1 = await _add_vendor(mk, org_id, name="Crit One")
    v2 = await _add_vendor(mk, org_id, name="Crit Two")
    await _add_vendor(mk, org_id, name="Unknown One")
    for vid in (v1, v2):
        await _add_sanctions_check(
            mk, org_id, vid, result="match", matched_list="OFAC_SDN", risk_score="90"
        )
        async with realdb.client(key="a", role="ap_manager") as client:
            await client.post(f"/api/vendors/{vid}/risk/recompute")

    async with realdb.client(key="a", role="cfo") as client:
        r = await client.get("/api/vendors/risk/summary")
    assert r.status_code == 200
    buckets = {item["risk_level"]: item["count"] for item in r.json()}
    assert buckets.get("critical") == 2
    assert buckets.get("unknown") == 1


async def test_summary_route_not_shadowed_by_vendor_id(realdb):
    """`/risk/summary` must resolve to the summary handler, not be
    captured by the `/{vendor_id}/risk` param route."""
    async with realdb.client(key="a", role="admin") as client:
        r = await client.get("/api/vendors/risk/summary")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_risk_endpoints_require_auth(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vid = await _add_vendor(mk, org_id, name="Auth Vendor")
    async with realdb.client(key="a", role=None) as client:
        assert (await client.get(f"/api/vendors/{vid}/risk")).status_code == 401
        assert (await client.get("/api/vendors/risk/summary")).status_code == 401
        assert (await client.post(f"/api/vendors/{vid}/risk/recompute")).status_code == 401
