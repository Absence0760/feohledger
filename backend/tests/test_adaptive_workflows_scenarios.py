"""Scenario coverage for the Adaptive AI Workflows slice.

Complements ``test_adaptive_workflows.py`` (which pins the pure-function edges
and the basic endpoint wiring) with end-to-end scenarios driven through the
HTTP surface against a live tenant DB:

  * Approval-pattern stats over a *mixed* multi-approver / multi-vendor history —
    per-approver counts + rejection rate, per-vendor approval rate, and the
    Decimal amount baselines (avg / median / min / max).
  * An out-of-range invoice is flagged as an anomaly AND the response carries the
    per-vendor baseline it deviated from (the explainability contract).
  * A vendor approved consistently yields an auto-approve suggestion; a noisy
    vendor (rejections / modified approvals) does NOT.
  * Auth (401 unauthenticated) + RBAC (clerk 403, manager/CFO/admin allowed).

All statistics are deterministic — no LLM, no cloud key — so every assertion is
an exact value, never a range.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _add_decided_invoice(
    s,
    org_id,
    *,
    vendor_id,
    vendor_name,
    amount,
    number,
    decision,  # "approved" | "rejected"
    actor_id=None,
    unmodified=True,
    status=None,
):
    """Insert one decided invoice + its ready_for_review clock-start and its
    decision audit row, matching the shape ``_decision_rows`` reads."""
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.workflow import AuditLog

    if status is None:
        status = InvoiceStatus.approved if decision == "approved" else InvoiceStatus.rejected
    inv = Invoice(
        organization_id=org_id,
        invoice_number=number,
        vendor_name=vendor_name,
        vendor_id=vendor_id,
        amount=Decimal(str(amount)),
        currency="USD",
        status=status,
    )
    s.add(inv)
    await s.commit()
    await s.refresh(inv)

    ready_at = datetime.now(UTC) - timedelta(days=10)
    s.add(
        AuditLog(
            organization_id=org_id,
            actor_id=None,
            action="invoice.status_changed",
            entity_type="invoice",
            entity_id=inv.id,
            details={"new_status": "ready_for_review"},
            created_at=ready_at,
        )
    )
    if decision == "approved":
        details = None if unmodified else {"changes": {"amount": {"old": "1", "new": "2"}}}
        s.add(
            AuditLog(
                organization_id=org_id,
                actor_id=actor_id,
                action="invoice.approved",
                entity_type="invoice",
                entity_id=inv.id,
                details=details,
                created_at=ready_at + timedelta(days=2),
            )
        )
    else:
        s.add(
            AuditLog(
                organization_id=org_id,
                actor_id=actor_id,
                action="invoice.rejected",
                entity_type="invoice",
                entity_id=inv.id,
                details={"reason": "no"},
                created_at=ready_at + timedelta(days=2),
            )
        )
    await s.commit()
    return inv.id


async def _mk_vendor(s, org_id, name):
    from app.models.vendor import Vendor

    v = Vendor(organization_id=org_id, name=name, status="active")
    s.add(v)
    await s.commit()
    await s.refresh(v)
    return v.id


# ---------------------------------------------------------------------------
# 1. Approval-pattern stats over a mixed multi-approval history
# ---------------------------------------------------------------------------


async def test_approval_patterns_counts_rates_and_amount_baselines(realdb):
    """Two approvers, two vendors, mixed approve/reject. Assert per-approver
    counts + rejection rate, per-vendor approval rate, and the Decimal amount
    baselines (avg/median/min/max) are all exact."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    mgr = realdb.info("a").users["ap_manager"]
    cfo = realdb.info("a").users["cfo"]

    async with mk() as s:
        v_acme = await _mk_vendor(s, org_id, "Acme Cleaning")
        v_globex = await _mk_vendor(s, org_id, "Globex Supply")

        # Acme: 3 approved by mgr (amounts 1000 / 2000 / 6000), 1 rejected by mgr.
        for i, amt in enumerate(("1000", "2000", "6000")):
            await _add_decided_invoice(
                s,
                org_id,
                vendor_id=v_acme,
                vendor_name="Acme Cleaning",
                amount=amt,
                number=f"ACME-A{i}",
                decision="approved",
                actor_id=mgr,
            )
        await _add_decided_invoice(
            s,
            org_id,
            vendor_id=v_acme,
            vendor_name="Acme Cleaning",
            amount="500",
            number="ACME-R0",
            decision="rejected",
            actor_id=mgr,
        )
        # Globex: 1 approved by cfo, 1 rejected by cfo.
        await _add_decided_invoice(
            s,
            org_id,
            vendor_id=v_globex,
            vendor_name="Globex Supply",
            amount="9000",
            number="GLOBEX-A0",
            decision="approved",
            actor_id=cfo,
        )
        await _add_decided_invoice(
            s,
            org_id,
            vendor_id=v_globex,
            vendor_name="Globex Supply",
            amount="100",
            number="GLOBEX-R0",
            decision="rejected",
            actor_id=cfo,
        )

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get("/api/adaptive/approval-patterns")
    assert r.status_code == 200
    data = r.json()

    # --- per-approver: counts + rejection (via approval) rate ---
    approvers = {a["approver_id"]: a for a in data["approvers"]}
    mgr_row = approvers[str(mgr)]
    assert mgr_row["approved_count"] == 3
    assert mgr_row["rejected_count"] == 1
    assert mgr_row["sample_size"] == 4
    # rejection rate = 1/4 = 25% → approval rate 75.0
    assert mgr_row["approval_rate_pct"] == "75.0"
    assert mgr_row["approver_name"] == "ap_manager"

    cfo_row = approvers[str(cfo)]
    assert cfo_row["approved_count"] == 1
    assert cfo_row["rejected_count"] == 1
    assert cfo_row["approval_rate_pct"] == "50.0"

    # --- per-vendor: approval rate + Decimal amount baselines ---
    vendors = {v["vendor_name"]: v for v in data["vendors"]}
    acme = vendors["Acme Cleaning"]
    assert acme["approved_count"] == 3
    assert acme["rejected_count"] == 1
    assert acme["approval_rate_pct"] == "75.0"  # 3/4
    # amount baselines over the 3 APPROVED Acme invoices (1000/2000/6000):
    assert acme["avg_approved_amount"] == "3000.00"
    assert acme["median_approved_amount"] == "2000.00"
    assert acme["min_approved_amount"] == "1000.00"
    assert acme["max_approved_amount"] == "6000.00"
    # serialised as string-Decimal, never float.
    assert all(
        isinstance(acme[k], str)
        for k in (
            "avg_approved_amount",
            "median_approved_amount",
            "min_approved_amount",
            "max_approved_amount",
        )
    )

    globex = vendors["Globex Supply"]
    assert globex["approval_rate_pct"] == "50.0"
    assert globex["avg_approved_amount"] == "9000.00"  # only the approved one


# ---------------------------------------------------------------------------
# 2. Out-of-range invoice → anomaly, response carries the deviated-from baseline
# ---------------------------------------------------------------------------


async def test_out_of_range_invoice_flagged_with_deviated_baseline(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    mgr = realdb.info("a").users["ap_manager"]

    async with mk() as s:
        vid = await _mk_vendor(s, org_id, "Steady Vendor")
        # 12 tightly-clustered approvals around ~5000 (history for the baseline).
        amounts = [4800 + (i % 5 - 2) * 150 for i in range(12)]  # 4500..5100
        for i, amt in enumerate(amounts):
            await _add_decided_invoice(
                s,
                org_id,
                vendor_id=vid,
                vendor_name="Steady Vendor",
                amount=str(amt),
                number=f"STEADY-{i}",
                decision="approved",
                actor_id=mgr,
            )
        # The invoice under test — far outside the band.
        from app.models.invoice import Invoice, InvoiceStatus

        outlier = Invoice(
            organization_id=org_id,
            invoice_number="STEADY-OUTLIER",
            vendor_name="Steady Vendor",
            vendor_id=vid,
            amount=Decimal("48000"),
            currency="USD",
            status=InvoiceStatus.ready_for_review,
        )
        s.add(outlier)
        await s.commit()
        await s.refresh(outlier)
        outlier_id = outlier.id

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/adaptive/anomalies?invoice_id={outlier_id}")
    assert r.status_code == 200
    data = r.json()

    assert data["insufficient_history"] is False
    assert "amount_high" in [f["code"] for f in data["flags"]]

    # Explainability contract: the baseline it deviated from is returned.
    baseline = data["baseline"]
    assert baseline is not None
    assert baseline["sample_size"] == 12
    assert baseline["vendor_name"] == "Steady Vendor"
    # the breached bound is reported on the flag, and the observed amount exceeds it.
    high_flag = next(f for f in data["flags"] if f["code"] == "amount_high")
    assert Decimal(high_flag["observed"]) == Decimal("48000")
    assert Decimal(high_flag["observed"]) > Decimal(baseline["max_amount"])
    # baseline amounts are string-Decimal, not float.
    assert isinstance(baseline["mean_amount"], str)
    assert isinstance(baseline["stdev_amount"], str)


async def test_in_band_invoice_not_flagged(realdb):
    """Control: an invoice squarely inside the vendor's historical band is not
    flagged, proving the anomaly flag isn't firing indiscriminately."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    mgr = realdb.info("a").users["ap_manager"]

    async with mk() as s:
        vid = await _mk_vendor(s, org_id, "Steady Vendor")
        for i, amt in enumerate([4800 + (i % 5 - 2) * 150 for i in range(12)]):
            await _add_decided_invoice(
                s,
                org_id,
                vendor_id=vid,
                vendor_name="Steady Vendor",
                amount=str(amt),
                number=f"STEADY-{i}",
                decision="approved",
                actor_id=mgr,
            )
        from app.models.invoice import Invoice, InvoiceStatus

        normal = Invoice(
            organization_id=org_id,
            invoice_number="STEADY-NORMAL",
            vendor_name="Steady Vendor",
            vendor_id=vid,
            amount=Decimal("4900"),
            currency="USD",
            status=InvoiceStatus.ready_for_review,
        )
        s.add(normal)
        await s.commit()
        await s.refresh(normal)
        normal_id = normal.id

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get(f"/api/adaptive/anomalies?invoice_id={normal_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["insufficient_history"] is False
    assert "amount_high" not in [f["code"] for f in data["flags"]]


# ---------------------------------------------------------------------------
# 3. Consistent vendor → suggestion; noisy vendor → none
# ---------------------------------------------------------------------------


async def test_consistent_vendor_suggested_noisy_vendor_not(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    mgr = realdb.info("a").users["ap_manager"]

    async with mk() as s:
        v_good = await _mk_vendor(s, org_id, "Reliable Co")
        v_noisy = await _mk_vendor(s, org_id, "Flaky LLC")

        # Reliable Co: 18 unmodified approvals, 0 rejections → qualifies.
        for i in range(18):
            await _add_decided_invoice(
                s,
                org_id,
                vendor_id=v_good,
                vendor_name="Reliable Co",
                amount=str(3000 + i * 100),
                number=f"REL-{i}",
                decision="approved",
                actor_id=mgr,
                unmodified=True,
            )

        # Flaky LLC: 18 approvals BUT several modified + 2 rejections → noisy.
        for i in range(18):
            await _add_decided_invoice(
                s,
                org_id,
                vendor_id=v_noisy,
                vendor_name="Flaky LLC",
                amount=str(3000 + i * 100),
                number=f"FLK-A{i}",
                decision="approved",
                actor_id=mgr,
                unmodified=(i % 2 == 0),
            )
        for i in range(2):
            await _add_decided_invoice(
                s,
                org_id,
                vendor_id=v_noisy,
                vendor_name="Flaky LLC",
                amount="999",
                number=f"FLK-R{i}",
                decision="rejected",
                actor_id=mgr,
            )

    async with realdb.client(key="a", role="ap_manager") as client:
        r = await client.get("/api/adaptive/suggestions")
    assert r.status_code == 200
    suggestions = r.json()["suggestions"]

    names = {sg["vendor_name"] for sg in suggestions}
    assert "Reliable Co" in names
    assert "Flaky LLC" not in names

    good = next(sg for sg in suggestions if sg["vendor_name"] == "Reliable Co")
    assert good["kind"] == "auto_approve_threshold"
    assert good["payload"]["based_on_n"] == 18
    # threshold = max approved (3000+17*100 = 4700) rounded up to 500 → 5000.
    assert good["payload"]["suggested_threshold"] == "5000.00"
    # confidence is a string-Decimal, capped at 99.
    assert Decimal(good["confidence_pct"]) <= Decimal("99")


# ---------------------------------------------------------------------------
# 4. Auth + RBAC
# ---------------------------------------------------------------------------


async def test_all_endpoints_require_auth(realdb):
    async with realdb.client(key="a", role=None) as client:
        for path, method in (
            ("/api/adaptive/approval-patterns", "get"),
            ("/api/adaptive/anomalies", "get"),
            ("/api/adaptive/suggestions", "get"),
            (f"/api/adaptive/suggestions/{uuid.uuid4()}/dismiss", "post"),
        ):
            resp = await getattr(client, method)(path)
            assert resp.status_code == 401, path


async def test_rbac_read_roles_and_write_roles(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    mgr = realdb.info("a").users["ap_manager"]

    async with mk() as s:
        vid = await _mk_vendor(s, org_id, "Reliable Co")
        for i in range(18):
            await _add_decided_invoice(
                s,
                org_id,
                vendor_id=vid,
                vendor_name="Reliable Co",
                amount=str(3000 + i * 100),
                number=f"REL-{i}",
                decision="approved",
                actor_id=mgr,
                unmodified=True,
            )

    # Manager / CFO / admin can read; create the suggestion row to dismiss.
    async with realdb.client(key="a", role="ap_manager") as client:
        sid = (await client.get("/api/adaptive/suggestions")).json()["suggestions"][0]["id"]
    async with realdb.client(key="a", role="cfo") as cfo:
        assert (await cfo.get("/api/adaptive/approval-patterns")).status_code == 200
        assert (await cfo.get("/api/adaptive/anomalies")).status_code == 200

    # Clerk is not a read role → 403 on every surface.
    async with realdb.client(key="a", role="ap_clerk") as clerk:
        assert (await clerk.get("/api/adaptive/approval-patterns")).status_code == 403
        assert (await clerk.get("/api/adaptive/anomalies")).status_code == 403
        assert (await clerk.get("/api/adaptive/suggestions")).status_code == 403
        # CFO is a read role but NOT a write role → cannot dismiss.
        assert (await clerk.post(f"/api/adaptive/suggestions/{sid}/dismiss")).status_code == 403

    # CFO is read-only here: dismiss (a write) is forbidden.
    async with realdb.client(key="a", role="cfo") as cfo:
        assert (await cfo.post(f"/api/adaptive/suggestions/{sid}/dismiss")).status_code == 403

    # Manager + admin are write roles → dismiss allowed (idempotent).
    async with realdb.client(key="a", role="admin") as admin:
        assert (await admin.post(f"/api/adaptive/suggestions/{sid}/dismiss")).status_code == 200
