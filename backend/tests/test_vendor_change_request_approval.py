"""AP-admin approval of supplier-portal change requests.

The approval gate is the fraud control: a staged bank/tax change applies to
the vendor ONLY when an AP admin approves it, and rejection never touches the
vendor. RBAC: a clerk/cfo cannot approve. All DB-backed via `realdb`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models.sanctions_check import SanctionsCheck
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest

TENANT = "a"


@pytest.fixture
def mk(realdb):
    """Tenant sessionmaker on the realdb-managed engine — the shared pattern
    (see test_portal_self_service.py / test_credit_memos.py). Avoids a second
    engine against the same DB, which thrashes the pool and leaks past the
    harness's per-test cleanup."""
    return realdb.sessionmaker(TENANT)


async def _seed_vendor(mk, org_id, **kw) -> uuid.UUID:
    vendor_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                name=kw.get("name", "Acme Supply"),
                organization_id=org_id,
                status="active",
                source="manual",
                bank_details=kw.get("bank_details"),
                tax_id=kw.get("tax_id"),
                tin_verified_at=kw.get("tin_verified_at"),
            )
        )
        await s.commit()
    return vendor_id


async def _stage(mk, org_id, vendor_id, change_type, proposed_value) -> uuid.UUID:
    req_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            VendorChangeRequest(
                id=req_id,
                vendor_id=vendor_id,
                organization_id=org_id,
                requested_by_vendor_user_id=uuid.uuid4(),
                change_type=change_type,
                status="pending",
                proposed_value=proposed_value,
            )
        )
        await s.commit()
    return req_id


@pytest.mark.asyncio
async def test_approve_bank_change_applies_to_vendor(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, org_id, bank_details={"bank_name": "Old Bank"})
    req_id = await _stage(
        mk,
        org_id,
        vendor_id,
        "bank_details",
        {"bank_details": {"account_number": "12345678", "bank_name": "New Bank"}},
    )
    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        # _merge_bank_details merged the incoming keys onto the existing dict.
        assert v.bank_details["account_number"] == "12345678"
        assert v.bank_details["bank_name"] == "New Bank"
        req = (
            await s.execute(select(VendorChangeRequest).where(VendorChangeRequest.id == req_id))
        ).scalar_one()
        assert req.status == "approved"
        assert req.reviewed_by_user_id is not None


@pytest.mark.asyncio
async def test_approve_tax_id_change_clears_tin_verification(realdb):
    from datetime import UTC, datetime

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, org_id, tax_id="old", tin_verified_at=datetime.now(UTC))
    req_id = await _stage(mk, org_id, vendor_id, "tax_id", {"tax_id": "99-9999999"})
    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
    assert resp.status_code == 200

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert v.tax_id == "99-9999999"
        assert v.tin_verified_at is None  # re-verification required


@pytest.mark.asyncio
async def test_reject_leaves_vendor_untouched(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, org_id, bank_details={"bank_name": "Old Bank"})
    req_id = await _stage(
        mk, org_id, vendor_id, "bank_details", {"bank_details": {"account_number": "55554444"}}
    )
    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(
            f"/api/vendors/change-requests/{req_id}/reject",
            json={"review_note": "Could not verify by phone"},
        )
    assert resp.status_code == 200

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert v.bank_details == {"bank_name": "Old Bank"}  # unchanged
        req = (
            await s.execute(select(VendorChangeRequest).where(VendorChangeRequest.id == req_id))
        ).scalar_one()
        assert req.status == "rejected"
        assert req.review_note == "Could not verify by phone"


@pytest.mark.asyncio
async def test_double_approve_409(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, org_id)
    req_id = await _stage(
        mk, org_id, vendor_id, "bank_details", {"bank_details": {"account_number": "1"}}
    )
    async with realdb.client(key=TENANT, role="admin") as client:
        first = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
        second = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
    assert first.status_code == 200
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_clerk_cannot_approve(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, org_id)
    req_id = await _stage(
        mk, org_id, vendor_id, "bank_details", {"bank_details": {"account_number": "1"}}
    )
    async with realdb.client(key=TENANT, role="ap_clerk") as client:
        resp = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_change_requests_masks_value(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, org_id)
    await _stage(
        mk,
        org_id,
        vendor_id,
        "bank_details",
        {"bank_details": {"account_number": "12349876", "bank_name": "Bank"}},
    )
    async with realdb.client(key=TENANT, role="ap_manager") as client:
        resp = await client.get("/api/vendors/change-requests")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The pending queue masks the value — full account number must be absent.
    assert "12349876" not in resp.text
    mine = [it for it in body["items"] if it["vendor_id"] == str(vendor_id)]
    assert mine, "the staged request should appear in the pending queue"
    assert mine[0]["proposed_value"].get("account_last4") == "9876"


@pytest.mark.asyncio
async def test_change_requests_literal_route_not_shadowed(realdb):
    """`GET /vendors/change-requests` must hit the queue handler, not the
    `/{vendor_id}` route (which would 422 on the non-UUID segment)."""
    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.get("/api/vendors/change-requests")
    assert resp.status_code == 200
    assert "items" in resp.json()


@pytest.mark.asyncio
async def test_change_request_counts_are_whole_set_and_pii_free(realdb):
    """The nav badge for the dual-control queue needs a whole-set tally, not a
    page of results — same reason `GET /api/vendors/counts` exists. A staged
    bank redirect nobody notices is the failure mode the gate exists to
    prevent."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, org_id)
    await _stage(
        mk,
        org_id,
        vendor_id,
        "bank_details",
        {"bank_details": {"account_number": "12349876", "bank_name": "Bank"}},
    )
    await _stage(mk, org_id, vendor_id, "tax_id", {"tax_id": "99-1234567"})

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        resp = await client.get("/api/vendors/change-requests/counts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pending"] == 2
    assert body["total"] == 2
    assert body["by_status"]["pending"] == 2
    # Counts only — never a proposed value.
    assert "12349876" not in resp.text
    assert "99-1234567" not in resp.text


@pytest.mark.asyncio
async def test_change_request_counts_drop_once_resolved(realdb):
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, org_id)
    req_id = await _stage(
        mk, org_id, vendor_id, "bank_details", {"bank_details": {"account_number": "11112222"}}
    )

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        assert (await client.get("/api/vendors/change-requests/counts")).json()["pending"] == 1
        approved = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
        assert approved.status_code == 200, approved.text
        after = (await client.get("/api/vendors/change-requests/counts")).json()
    assert after["pending"] == 0
    assert after["by_status"]["approved"] == 1


@pytest.mark.asyncio
async def test_change_request_counts_forbidden_for_clerk(realdb):
    async with realdb.client(key=TENANT, role="ap_clerk") as client:
        resp = await client.get("/api/vendors/change-requests/counts")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_change_request_counts_are_gated_exactly_like_the_queue(realdb):
    """A CFO must not learn how many bank redirects are staged for review.

    The tally used to admit ROLE_CFO while `GET /api/vendors/change-requests`
    does not, so a CFO could read the size of a fraud-review queue they can
    neither see nor act on (they hold no `vendor.bank_change.approve`, and both
    the nav entry and the page exclude them). decisions §48 requires a counts
    endpoint's RBAC to match its list's exactly; this asserts the pairing
    behaviourally, per role, rather than only structurally.
    """
    for role in ("admin", "ap_manager"):
        async with realdb.client(key=TENANT, role=role) as client:
            listed = await client.get("/api/vendors/change-requests")
            counted = await client.get("/api/vendors/change-requests/counts")
        assert listed.status_code == 200, f"{role} should read the queue"
        assert counted.status_code == 200, f"{role} should read its tally"

    for role in ("cfo", "ap_clerk"):
        async with realdb.client(key=TENANT, role=role) as client:
            listed = await client.get("/api/vendors/change-requests")
            counted = await client.get("/api/vendors/change-requests/counts")
        assert listed.status_code == 403, f"{role} should not read the queue"
        assert counted.status_code == 403, (
            f"{role} is refused the queue but was served its tally — the size of "
            "the staged-bank-change queue is exactly what must not leak"
        )


# ===========================================================================
# AP-initiated bank-detail changes are dual-control (BEC / bank-redirect gate).
# An AP user can PROPOSE a bank change but it stages a pending request instead
# of applying; a SECOND user (not the proposer) must approve it.
# ===========================================================================


@pytest.mark.asyncio
async def test_ap_patch_bank_details_stages_instead_of_applying(realdb):
    """PATCH /vendors/{id} with bank_details must NOT apply it inline — it
    stages a pending change request stamped with the AP requester. The vendor's
    bank_details stay put until a second approver signs off."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, info.org_id, bank_details={"counterparty_id": "cp_old"})

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.patch(
            f"/api/vendors/{vendor_id}",
            json={"bank_details": {"counterparty_id": "cp_attacker"}},
        )
    assert resp.status_code == 200, resp.text
    # Response shows the UNCHANGED bank details (the change is only staged).
    assert (resp.json().get("bank_details") or {}).get("counterparty_id") == "cp_old"

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert (v.bank_details or {}).get("counterparty_id") == "cp_old", "bank must NOT change"
        req = (
            await s.execute(
                select(VendorChangeRequest).where(VendorChangeRequest.vendor_id == vendor_id)
            )
        ).scalar_one()
        assert req.status == "pending"
        assert req.change_type == "bank_details"
        assert req.requested_by_user_id == info.users["admin"]
        assert req.requested_by_vendor_user_id is None  # AP-initiated, not portal


@pytest.mark.asyncio
async def test_ap_bank_change_endpoint_stages_and_returns_202(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, info.org_id, bank_details={"counterparty_id": "cp_old"})

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(
            f"/api/vendors/{vendor_id}/bank-change",
            json={"bank_details": {"counterparty_id": "cp_new"}},
        )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "pending"

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert (v.bank_details or {}).get("counterparty_id") == "cp_old", "still unapplied"


@pytest.mark.asyncio
async def test_ap_bank_change_rejects_a_malformed_routing_number(realdb):
    """A routing number that fails the ABA checksum must 422 before it's even
    staged — nothing validated it before, so a typo silently reached the
    change-request queue and only surfaced as a returned ACH days later."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, info.org_id, bank_details={})

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(
            f"/api/vendors/{vendor_id}/bank-change",
            json={"bank_details": {"routing_number": "021000020", "account_number": "12345"}},
        )
    assert resp.status_code == 422, resp.text

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(VendorChangeRequest).where(VendorChangeRequest.vendor_id == vendor_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == [], "malformed routing number must never reach the staging queue"


@pytest.mark.asyncio
async def test_ap_bank_change_rejects_a_malformed_sort_code(realdb):
    """The UK equivalent of the routing-number check — a sort code that
    isn't 6 digits (grouped or bare) must 422 before staging, same as a bad
    ABA routing number."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, info.org_id, bank_details={})

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(
            f"/api/vendors/{vendor_id}/bank-change",
            json={"bank_details": {"sort_code": "12-34", "account_number": "12345678"}},
        )
    assert resp.status_code == 422, resp.text

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(VendorChangeRequest).where(VendorChangeRequest.vendor_id == vendor_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows == [], "malformed sort code must never reach the staging queue"


@pytest.mark.asyncio
async def test_ap_bank_change_accepts_a_valid_sort_code(realdb):
    """A well-formed sort code (either grouped or bare) stages cleanly,
    alongside the ABA routing_number field it's mutually exclusive with in
    practice — only the field that's actually present is validated."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, info.org_id, bank_details={})

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(
            f"/api/vendors/{vendor_id}/bank-change",
            json={"bank_details": {"sort_code": "20-00-00", "account_number": "12345678"}},
        )
    assert resp.status_code == 202, resp.text


@pytest.mark.asyncio
async def test_requester_cannot_approve_their_own_bank_change(realdb):
    """Segregation of duties: the admin who proposed the change can't approve
    it — that would collapse dual control back to a one-person bank redirect."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, info.org_id, bank_details={"counterparty_id": "cp_old"})

    async with realdb.client(key=TENANT, role="admin") as client:
        staged = await client.post(
            f"/api/vendors/{vendor_id}/bank-change",
            json={"bank_details": {"counterparty_id": "cp_new"}},
        )
        req_id = staged.json()["id"]
        # Same admin tries to approve their own request.
        resp = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
    assert resp.status_code == 403, resp.text

    async with mk() as s:
        req = (
            await s.execute(select(VendorChangeRequest).where(VendorChangeRequest.id == req_id))
        ).scalar_one()
        assert req.status == "pending", "a self-approval must not resolve the request"
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert (v.bank_details or {}).get("counterparty_id") == "cp_old", "still unapplied"


@pytest.mark.asyncio
async def test_a_different_approver_applies_the_ap_bank_change(realdb):
    """The happy path: admin proposes, a DIFFERENT approver (ap_manager) signs
    off, and only then does the change apply."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, info.org_id, bank_details={"counterparty_id": "cp_old"})

    async with realdb.client(key=TENANT, role="admin") as client:
        staged = await client.post(
            f"/api/vendors/{vendor_id}/bank-change",
            json={"bank_details": {"counterparty_id": "cp_new"}},
        )
        req_id = staged.json()["id"]

    async with realdb.client(key=TENANT, role="ap_manager") as approver:
        resp = await approver.post(f"/api/vendors/change-requests/{req_id}/approve")
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert (v.bank_details or {}).get("counterparty_id") == "cp_new", "now applied"


@pytest.mark.asyncio
async def test_bank_change_approval_flags_payable_invoices(realdb):
    """BEC gate: approving a bank-detail change must raise a fraud_flag on every
    invoice already in the payment queue for that vendor, so the next run gets a
    human second look before paying into the new account."""
    from decimal import Decimal

    from app.models.exception import Exception as APException
    from app.models.invoice import Invoice, InvoiceStatus

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _seed_vendor(mk, org_id, bank_details={"bank_name": "Old Bank"})

    # One invoice in the payment queue (approved) and one still in review.
    payable_id, review_id = uuid.uuid4(), uuid.uuid4()
    async with mk() as s:
        s.add_all(
            [
                Invoice(
                    id=payable_id,
                    organization_id=org_id,
                    vendor_id=vendor_id,
                    invoice_number="BEC-PAYABLE",
                    vendor_name="Acme Supply",
                    amount=Decimal("75000.00"),
                    currency="USD",
                    status=InvoiceStatus.approved,
                ),
                Invoice(
                    id=review_id,
                    organization_id=org_id,
                    vendor_id=vendor_id,
                    invoice_number="BEC-REVIEW",
                    vendor_name="Acme Supply",
                    amount=Decimal("10.00"),
                    currency="USD",
                    status=InvoiceStatus.ready_for_review,
                ),
            ]
        )
        await s.commit()

    req_id = await _stage(
        mk,
        org_id,
        vendor_id,
        "bank_details",
        {"bank_details": {"account_number": "99999999", "bank_name": "Attacker Bank"}},
    )
    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        # The payable invoice got a fraud_flag payment hold.
        payable_flags = (
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == payable_id,
                        APException.exception_type == "fraud_flag",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(payable_flags) == 1
        assert "bank details" in (payable_flags[0].description or "").lower()
        # The in-review invoice (not in the payment queue) is NOT flagged.
        review_flags = (
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == review_id,
                        APException.exception_type == "fraud_flag",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert review_flags == []


@pytest.mark.asyncio
async def test_bank_change_approval_rescreens_new_coordinates(realdb):
    """BEC gate: approving a bank-detail change must RE-SCREEN the vendor against
    the newly-applied coordinates (not just flag payable invoices). Here the
    swap redirects payment to a bank in a high-risk jurisdiction — the classic
    BEC redirect — so the re-screen must fire on the new country and surface it,
    proving the screen reads the applied details, not the stale ones."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    # Clean start: US bank, screened clear, not blocked.
    vendor_id = await _seed_vendor(
        mk, org_id, name="Clean Vendor Inc", bank_details={"country": "US"}
    )
    req_id = await _stage(
        mk,
        org_id,
        vendor_id,
        "bank_details",
        # Redirect to a Russian account (FATF high-risk → review_required).
        {"bank_details": {"account_number": "99999999", "country": "RU"}},
    )
    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        # The re-screen saw the NEW country and flagged the vendor for review.
        assert v.screening_status == "review"
        assert v.last_screened_at is not None
        # A `bank_change` sanctions-check row records the re-screen on the trail.
        rows = (
            (
                await s.execute(
                    select(SanctionsCheck).where(
                        SanctionsCheck.vendor_id == vendor_id,
                        SanctionsCheck.check_type == "bank_change",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].result == "review_required"
        assert "RU" in (rows[0].matched_list or "")


@pytest.mark.asyncio
async def test_bank_change_approval_rescreen_blocks_sanctioned_vendor(realdb):
    """BEC gate: if the post-change re-screen returns a sanctions MATCH, the
    vendor must be payment-blocked. A vendor sitting on the SDN blocklist that
    was never blocked (e.g. added to the list after onboarding) gets caught the
    next time its record is touched — approving a bank change re-screens it,
    the name matches, and the hard payment block lands."""
    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    # Name is on the mock SDN blocklist, but the row starts un-blocked (simulating
    # a list addition after onboarding, or a never-screened import).
    vendor_id = await _seed_vendor(
        mk, org_id, name="Blocked Party LLC", bank_details={"bank_name": "Old Bank"}
    )
    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert not v.payments_blocked, "precondition: starts un-blocked"

    req_id = await _stage(
        mk,
        org_id,
        vendor_id,
        "bank_details",
        {"bank_details": {"account_number": "12345678", "bank_name": "New Bank"}},
    )
    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
    assert resp.status_code == 200, resp.text

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        # The re-screen matched the SDN name and hard-blocked payments.
        assert v.screening_status == "match"
        assert v.payments_blocked is True
        assert v.payments_blocked_reason is not None
        rows = (
            (
                await s.execute(
                    select(SanctionsCheck).where(
                        SanctionsCheck.vendor_id == vendor_id,
                        SanctionsCheck.check_type == "bank_change",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].result == "match"


@pytest.mark.asyncio
async def test_tax_id_change_approval_rescreens_against_the_new_tax_id(realdb, monkeypatch):
    """`tax_id` is an identity field: the sanctions adapters take it
    (`vendor_tax_id`), and `PATCH /vendors/{id}` re-screens when AP edits it
    directly. Approving a STAGED tax-ID change applied the new value with no
    screen at all, so the supplier-portal route — the one a supplier drives —
    was the one way to re-key a vendor's identity without a sanctions check.

    The probe records what the adapter was asked, so this proves the screen ran
    against the APPLIED tax id, not the stale one."""
    from app.services.sanctions_adapters.mock_adapter import MockSanctionsAdapter

    seen_tax_ids: list[str | None] = []
    real_screen = MockSanctionsAdapter.screen_vendor

    async def recording_screen(self, **kwargs):
        seen_tax_ids.append(kwargs.get("vendor_tax_id"))
        return await real_screen(self, **kwargs)

    monkeypatch.setattr(MockSanctionsAdapter, "screen_vendor", recording_screen)

    org_id = realdb.info(TENANT).org_id
    mk = realdb.sessionmaker(TENANT)
    # On the mock SDN list but un-blocked, as in the bank-change sibling above:
    # the re-screen is what has to catch it.
    vendor_id = await _seed_vendor(mk, org_id, name="Blocked Party LLC", tax_id="11-1111111")
    req_id = await _stage(mk, org_id, vendor_id, "tax_id", {"tax_id": "22-2222222"})

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(f"/api/vendors/change-requests/{req_id}/approve")
    assert resp.status_code == 200, resp.text

    assert seen_tax_ids == ["22-2222222"]
    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert v.tax_id == "22-2222222"
        assert v.screening_status == "match"
        assert v.payments_blocked is True
        rows = (
            (
                await s.execute(
                    select(SanctionsCheck).where(
                        SanctionsCheck.vendor_id == vendor_id,
                        SanctionsCheck.check_type == "tax_id_change",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].result == "match"


# ---------------------------------------------------------------------------
# Issue #121 — new-vendor bank details bypassed the BEC dual-control gate.
#
# Only bank-detail CHANGES to existing vendors were dual-controlled; creating
# a brand-new vendor with attacker-controlled bank details was a single-person
# action (the row landed active + self-verified in one call, no second
# approver). Fake-new-payee is the more common real-world BEC pattern.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_vendor_with_bank_details_stages_instead_of_applying(realdb):
    """POST /vendors with bank_details must NOT land them on the row — the
    vendor is created payable-eligible metadata-wise but with NO bank details
    until a second approver signs off on the staged VendorChangeRequest,
    exactly like the existing-vendor PATCH path."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(
            "/api/vendors",
            json={
                "name": "Fresh Fraud LLC",
                "bank_details": {"account_last4": "8888", "bank_name": "Attacker Bank"},
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    vendor_id = uuid.UUID(body["id"])
    # The response reflects the row as actually persisted — no bank details.
    assert not body.get("bank_details")

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert not v.bank_details, "bank details must NOT land on create"

        req = (
            await s.execute(
                select(VendorChangeRequest).where(VendorChangeRequest.vendor_id == vendor_id)
            )
        ).scalar_one()
        assert req.status == "pending"
        assert req.change_type == "bank_details"
        assert req.requested_by_user_id == info.users["admin"]
        assert req.proposed_value["bank_details"]["account_last4"] == "8888"


@pytest.mark.asyncio
async def test_create_vendor_without_bank_details_is_unaffected(realdb):
    """A vendor created with no bank_details at all must NOT stage a spurious
    change request — the dual-control gate only engages when bank details are
    actually submitted."""
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post("/api/vendors", json={"name": "No Bank Info Inc"})
    assert resp.status_code == 201, resp.text
    vendor_id = uuid.UUID(resp.json()["id"])

    async with mk() as s:
        count = (
            (
                await s.execute(
                    select(VendorChangeRequest).where(VendorChangeRequest.vendor_id == vendor_id)
                )
            )
            .scalars()
            .all()
        )
        assert count == []


@pytest.mark.asyncio
async def test_approving_new_vendor_bank_change_applies_it(realdb):
    """The full loop: create with bank details (staged, not applied), a
    SECOND user approves, and only then does the vendor's bank_details get
    set."""
    mk = realdb.sessionmaker(TENANT)

    async with realdb.client(key=TENANT, role="admin") as client:
        create_resp = await client.post(
            "/api/vendors",
            json={
                "name": "Pending Bank Co",
                "bank_details": {"account_last4": "2222", "bank_name": "Real Bank"},
            },
        )
        vendor_id = create_resp.json()["id"]

    async with mk() as s:
        req = (
            await s.execute(
                select(VendorChangeRequest).where(
                    VendorChangeRequest.vendor_id == uuid.UUID(vendor_id)
                )
            )
        ).scalar_one()

    # A DIFFERENT user (ap_manager, distinct from the admin who created the
    # vendor) approves — segregation of duties applies here too.
    async with realdb.client(key=TENANT, role="ap_manager") as client:
        approve_resp = await client.post(f"/api/vendors/change-requests/{req.id}/approve")
    assert approve_resp.status_code == 200, approve_resp.text

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == uuid.UUID(vendor_id)))).scalar_one()
        assert v.bank_details["account_last4"] == "2222"
        assert v.bank_details["bank_name"] == "Real Bank"


@pytest.mark.asyncio
async def test_mailing_address_round_trips_and_unblocks_checkeeper(realdb):
    """`bank_details.mailing_address` is what the `checkeeper` check-printing
    rail reads (`services/payment_adapters/checkeeper.py`) — before
    `VendorBankDetails` declared the field, it was silently stripped by
    Pydantic on every typed write path (`POST /vendors`, `PATCH /vendors/{id}`)
    and never surfaced back out of `VendorResponse.from_db`, so a check
    payment to ANY vendor failed `checkeeper_missing_mailing_address` forever
    with no way to fix it from the app.

    Proves the full loop: create with a mailing address (staged, not
    applied) -> a second user approves -> it lands on `Vendor.bank_details`
    exactly as-is -> `GET` surfaces it back -> and, with that address on the
    row, the `checkeeper` adapter accepts a check payment to this vendor
    instead of refusing it.
    """
    mk = realdb.sessionmaker(TENANT)
    mailing_address = {
        "street": "1 Acme Plaza",
        "city": "Springfield",
        "state": "IL",
        "postal": "62701",
        "country": "US",
    }

    async with realdb.client(key=TENANT, role="admin") as client:
        create_resp = await client.post(
            "/api/vendors",
            json={
                "name": "Check-Paid Supply Co",
                "bank_details": {"bank_name": "Barclays", "mailing_address": mailing_address},
            },
        )
        assert create_resp.status_code in (200, 201), create_resp.text
        vendor_id = create_resp.json()["id"]

    async with mk() as s:
        req = (
            await s.execute(
                select(VendorChangeRequest).where(
                    VendorChangeRequest.vendor_id == uuid.UUID(vendor_id)
                )
            )
        ).scalar_one()
        # Not silently dropped by the (formerly undeclared) schema field.
        assert req.proposed_value["bank_details"]["mailing_address"]["street"] == ("1 Acme Plaza")

    # A second user approves — same segregation-of-duties shape as the
    # sibling test above.
    async with realdb.client(key=TENANT, role="ap_manager") as client:
        approve_resp = await client.post(f"/api/vendors/change-requests/{req.id}/approve")
    assert approve_resp.status_code == 200, approve_resp.text

    async with mk() as s:
        v = (await s.execute(select(Vendor).where(Vendor.id == uuid.UUID(vendor_id)))).scalar_one()
        assert v.bank_details["mailing_address"]["street"] == "1 Acme Plaza"
        assert v.bank_details["mailing_address"]["postal"] == "62701"
        stored_bank_details = dict(v.bank_details)

    # GET /vendors/{id} — VendorResponse.from_db must surface it back, not
    # just persist it. A UI that can never read the value back can't confirm
    # it saved, and this is exactly what `from_db` dropped before the fix.
    async with realdb.client(key=TENANT, role="admin") as client:
        get_resp = await client.get(f"/api/vendors/{vendor_id}")
    assert get_resp.status_code == 200, get_resp.text
    returned_mailing = get_resp.json()["bank_details"]["mailing_address"]
    assert returned_mailing["street"] == "1 Acme Plaza"
    assert returned_mailing["postal"] == "62701"
    assert returned_mailing["country"] == "US"

    # And the payoff: the exact dict `api/payments.py` reads off
    # `Vendor.bank_details` and hands to the adapter as `payload.vendor_bank`
    # now satisfies the checkeeper rail instead of refusing the payment.
    # httpx + the Redis idempotency gate are stubbed exactly like the
    # adapter's own tests (`test_payment_adapters_new.py`) — this test's
    # subject is the mailing-address plumbing, not a live network call.
    from decimal import Decimal
    from unittest.mock import AsyncMock, patch

    from app.services.payment_adapters import PaymentPayload
    from app.services.payment_adapters.checkeeper import CheckeeperAdapter
    from tests.test_payment_adapters_new import _fake_async_client, _FakeResponse

    fake_client = _fake_async_client(
        [_FakeResponse({"id": "chk_mail_1", "status": "queued", "check_number": "9001"})]
    )
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)  # idempotency slot claimed

    adapter = CheckeeperAdapter({"api_key": "k", "bank_account_id": "bnk"})
    payload = PaymentPayload(
        correlation_id="cor-mailing-test",
        invoice_id="inv-mailing-test",
        invoice_number="INV-MAIL-1",
        vendor_name="Check-Paid Supply Co",
        amount=Decimal("42.00"),
        currency="USD",
        method="check",
        vendor_bank=stored_bank_details,
    )
    with (
        patch("app.services.payment_adapters.checkeeper.httpx.AsyncClient", fake_client),
        patch("app.redis.get_redis", AsyncMock(return_value=fake_redis)),
    ):
        result = await adapter.create_payment(payload)
    assert result.failure_reason != "checkeeper_missing_mailing_address"
    assert result.success is True
