"""Adjudicated sanctions false positives (decisions §477).

A recorded, audited human finding that ONE screen result is not this vendor.
Pinned here:

* the fingerprint identifies a result (provider, verdict, list, entry) and
  ignores per-call noise;
* the stamp clears the ERP-push gate (`erp_export_vendor_hold`, the function
  every send door and `_call_erp` run) and the dispatch gate
  (`check_payment_compliance`, which dispatch and `/compliance/release` run)
  for the SAME result, including after a fresh screen;
* a new or different hit is not covered and holds again;
* the permission: a preparer (ap_clerk) and, by default, ap_manager are
  refused; cfo holds it;
* segregation: the onboarder and the people behind the current bank details
  are refused;
* audit rows for the stamp and its revocation, and revocation re-arms both gates.

All DB-backed via `realdb`; the local `mock` sanctions adapter needs no key
("Blocked Party LLC" matches, "Adverse Media Test Co" / "Negative News Fixture"
need review).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.sanctions_adjudication import SanctionsAdjudication
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.models.workflow import AuditLog
from app.services.compliance import check_payment_compliance
from app.services.erp_export_gate import erp_export_vendor_hold
from app.services.sanctions_adapters.mock_adapter import MockSanctionsAdapter
from app.services.sanctions_adjudication import (
    ADJUDICATED_ACTION,
    CLEARANCE_ADJUDICATED,
    CLEARANCE_CHANGED_ACTION,
    REVOKED_ACTION,
    SANCTIONS_ADJUDICATION_ALREADY_CLEARED,
    screen_fingerprint,
)

TENANT = "a"
REASON = "Different entity: registration number and country do not match the list entry."
# No AML signal: the dispatch verdict below is the screen's alone.
_ORG_SETTINGS = {"compliance": {"aml_spend_alert_threshold": "0"}}


# ---------------------------------------------------------------------------
# The fingerprint
# ---------------------------------------------------------------------------


def _fp(**over) -> str:
    base = {
        "provider": "mock",
        "result": "match",
        "matched_list": "MOCK_TEST_SDN",
        "raw_response": {"hit": "blocked party llc", "list": "MOCK_TEST_SDN"},
    }
    base.update(over)
    return screen_fingerprint(**base)


def test_the_fingerprint_is_stable_and_ignores_per_call_noise():
    assert _fp() == _fp()
    noisy = {"hit": "blocked party llc", "list": "MOCK_TEST_SDN", "request_id": "r-1"}
    assert _fp(raw_response=noisy) == _fp()
    assert len(_fp()) == 64


@pytest.mark.parametrize(
    "over",
    [
        {"provider": "complyadvantage"},
        {"result": "review_required"},
        {"matched_list": "EU_CONSOLIDATED"},
        {"raw_response": {"hit": "another party", "list": "MOCK_TEST_SDN"}},
    ],
)
def test_a_different_result_has_a_different_fingerprint(over):
    assert _fp(**over) != _fp()


def test_an_adapter_declared_entry_identity_is_used_verbatim():
    a = _fp(raw_response={"match_fingerprint": "SDN-123@2026-11-01", "score": 91})
    b = _fp(raw_response={"match_fingerprint": "SDN-123@2026-11-01", "score": 88})
    c = _fp(raw_response={"match_fingerprint": "SDN-123@2026-11-15", "score": 91})
    assert a == b  # same entry, same list version
    assert a != c  # the list was republished: a new judgement is owed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_vendor(realdb, name: str, *, role: str = "ap_manager") -> uuid.UUID:
    async with realdb.client(key=TENANT, role=role) as c:
        res = await c.post("/api/vendors", json={"name": name, "code": uuid.uuid4().hex[:8]})
    assert res.status_code == 201, res.text
    return uuid.UUID(res.json()["id"])


async def _invoice_for(mk, org_id, vendor_id) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=f"ADJ-{inv_id.hex[:6]}",
                vendor_name="Screened Vendor",
                vendor_id=vendor_id,
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                uploaded_by_id=None,
            )
        )
        await s.commit()
    return inv_id


async def _erp_hold(mk, inv_id) -> str | None:
    async with mk() as s:
        return await erp_export_vendor_hold(s, await s.get(Invoice, inv_id))


async def _dispatch_verdict(mk, org_id, vendor_id) -> str:
    async with mk() as s:
        vendor = await s.get(Vendor, vendor_id)
        decision = await check_payment_compliance(
            s,
            vendor=vendor,
            payment_amount=Decimal("100.00"),
            payment_currency="USD",
            payment_method="ach",
            org_settings=_ORG_SETTINGS,
            organization_id=org_id,
            sanctions_adapter=MockSanctionsAdapter(),
        )
        await s.rollback()
        return decision.verdict


async def _adjudicate(realdb, vendor_id, *, role: str = "cfo", reason: str = REASON):
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(
            f"/api/vendors/{vendor_id}/screening/adjudicate", json={"reason": reason}
        )


async def _revoke(realdb, vendor_id, *, role: str = "cfo"):
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(
            f"/api/vendors/{vendor_id}/screening/adjudication/revoke",
            json={"reason": "New evidence from the provider."},
        )


async def _rescreen(realdb, vendor_id) -> dict:
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/vendors/{vendor_id}/screen")
    assert res.status_code == 200, res.text
    return res.json()


async def _audit(mk, vendor_id, action: str) -> list[AuditLog]:
    async with mk() as s:
        return list(
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == vendor_id, AuditLog.action == action
                    )
                )
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# The stamp clears both gates for the same result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_review_adjudication_clears_the_erp_push_and_dispatch(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    inv_id = await _invoice_for(mk, info.org_id, vendor_id)

    assert await _erp_hold(mk, inv_id) == "sanctions_review"
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "hold"

    res = await _adjudicate(realdb, vendor_id)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["result"] == "review_required"
    assert body["matched_list"] == "ADVERSE_MEDIA"
    assert body["adjudicated_by_user_id"] == str(info.users["cfo"])
    assert body["reason"] == REASON

    assert await _erp_hold(mk, inv_id) is None
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "allow"

    # A fresh stored screen returning the SAME result stays cleared.
    screened = await _rescreen(realdb, vendor_id)
    assert screened["screening_status"] == "review"
    async with mk() as s:
        assert (await s.get(Vendor, vendor_id)).screening_adjudication_id == uuid.UUID(body["id"])
    assert await _erp_hold(mk, inv_id) is None
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "allow"


@pytest.mark.asyncio
async def test_a_match_adjudication_leaves_the_sticky_block_to_its_owner(realdb):
    """A `match` set the sticky block; adjudicating does not lift it (that is
    `vendor.block`'s unblock), and a later screen of the same match neither
    re-blocks nor holds once it is lifted."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Blocked Party LLC")
    inv_id = await _invoice_for(mk, info.org_id, vendor_id)
    assert await _erp_hold(mk, inv_id) == "payments_blocked"

    assert (await _adjudicate(realdb, vendor_id)).status_code == 201
    assert await _erp_hold(mk, inv_id) == "payments_blocked"
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "refuse"

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        assert (await c.post(f"/api/vendors/{vendor_id}/unblock")).status_code == 200
    assert await _erp_hold(mk, inv_id) is None
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "allow"

    screened = await _rescreen(realdb, vendor_id)
    assert screened["screening_status"] == "match"
    assert screened["payments_blocked"] is False
    assert await _erp_hold(mk, inv_id) is None


@pytest.mark.asyncio
async def test_a_different_hit_is_not_covered_and_holds_again(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    inv_id = await _invoice_for(mk, info.org_id, vendor_id)
    assert (await _adjudicate(realdb, vendor_id)).status_code == 201
    assert await _erp_hold(mk, inv_id) is None

    # The identity changes so the provider reports a different entry.
    async with mk() as s:
        (await s.get(Vendor, vendor_id)).name = "Negative News Fixture"
        await s.commit()
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "hold"
    screened = await _rescreen(realdb, vendor_id)
    assert screened["screening_status"] == "review"
    async with mk() as s:
        assert (await s.get(Vendor, vendor_id)).screening_adjudication_id is None
    assert await _erp_hold(mk, inv_id) == "sanctions_review"

    # And escalating to a list match on the old name is a new result too.
    async with mk() as s:
        (await s.get(Vendor, vendor_id)).name = "Blocked Party LLC"
        await s.commit()
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "refuse"


# ---------------------------------------------------------------------------
# Who may adjudicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap_clerk", "ap_manager"])
async def test_a_role_without_the_permission_is_refused(realdb, role):
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co", role="admin")
    res = await _adjudicate(realdb, vendor_id, role=role)
    assert res.status_code == 403, res.text
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        rows = (
            await s.execute(
                select(SanctionsAdjudication).where(SanctionsAdjudication.vendor_id == vendor_id)
            )
        ).all()
    assert rows == []


@pytest.mark.asyncio
async def test_the_onboarder_may_not_adjudicate(realdb):
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co", role="admin")
    res = await _adjudicate(realdb, vendor_id, role="admin")
    assert res.status_code == 403, res.text
    assert "another reviewer" in res.json()["detail"]
    assert (await _adjudicate(realdb, vendor_id, role="cfo")).status_code == 201


@pytest.mark.asyncio
async def test_whoever_put_the_bank_details_in_place_may_not_adjudicate(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    async with mk() as s:
        s.add(
            VendorChangeRequest(
                vendor_id=vendor_id,
                organization_id=info.org_id,
                requested_by_user_id=info.users["ap_manager"],
                change_type="bank_details",
                status="approved",
                proposed_value={"bank_details": {"account_last4": "6789"}},
                reviewed_by_user_id=info.users["cfo"],
                reviewed_at=datetime.now(UTC),
            )
        )
        await s.commit()
    res = await _adjudicate(realdb, vendor_id, role="cfo")
    assert res.status_code == 403, res.text
    # An admin with no part in the vendor is a second pair of eyes.
    assert (await _adjudicate(realdb, vendor_id, role="admin")).status_code == 201


@pytest.mark.asyncio
async def test_a_blank_reason_is_refused(realdb):
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    assert (await _adjudicate(realdb, vendor_id, reason="   ")).status_code == 422


@pytest.mark.asyncio
async def test_a_clear_vendor_has_nothing_to_adjudicate(realdb):
    vendor_id = await _create_vendor(realdb, "Totally Normal Supplies Co")
    assert (await _adjudicate(realdb, vendor_id)).status_code == 409


# ---------------------------------------------------------------------------
# Audit + revocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stamp_and_revocation_are_audited_and_revocation_re_arms_the_gates(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    inv_id = await _invoice_for(mk, info.org_id, vendor_id)

    adj = (await _adjudicate(realdb, vendor_id)).json()
    stamped = await _audit(mk, vendor_id, ADJUDICATED_ACTION)
    assert len(stamped) == 1
    assert stamped[0].actor_id == info.users["cfo"]
    assert stamped[0].details["adjudication_id"] == adj["id"]
    assert stamped[0].details["fingerprint"] == adj["fingerprint"]
    assert stamped[0].details["reason"] == REASON
    changes = await _audit(mk, vendor_id, CLEARANCE_CHANGED_ACTION)
    assert [(c.details["from"], c.details["to"]) for c in changes] == [
        ("review", CLEARANCE_ADJUDICATED)
    ]

    # Adjudicating twice is refused while the first stands.
    again = await _adjudicate(realdb, vendor_id, role="admin")
    assert again.status_code == 409, again.text

    revoked = await _revoke(realdb, vendor_id)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_by_user_id"] == str(info.users["cfo"])
    assert len(await _audit(mk, vendor_id, REVOKED_ACTION)) == 1
    changes = await _audit(mk, vendor_id, CLEARANCE_CHANGED_ACTION)
    assert sorted((c.details["from"], c.details["to"]) for c in changes) == sorted(
        [("review", CLEARANCE_ADJUDICATED), (CLEARANCE_ADJUDICATED, "review")]
    )

    assert await _erp_hold(mk, inv_id) == "sanctions_review"
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "hold"
    assert (await _revoke(realdb, vendor_id)).status_code == 409

    # A revoked row no longer blocks a new adjudication of the same result.
    assert (await _adjudicate(realdb, vendor_id, role="admin")).status_code == 201


@pytest.mark.asyncio
async def test_the_review_queue_ships_the_server_verdicts(realdb):
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")

    async def _row(role: str) -> dict:
        async with realdb.client(key=TENANT, role=role) as c:
            res = await c.get("/api/vendors/screening/review-queue", params={"page_size": 100})
        assert res.status_code == 200, res.text
        return next(i for i in res.json()["items"] if i["vendor_id"] == str(vendor_id))

    assert (await _row("ap_clerk"))["adjudicate_refusal"]["code"] == "role_not_permitted"
    cfo = await _row("cfo")
    assert cfo["adjudicate_refusal"] is None
    assert cfo["adjudication"] is None
    assert cfo["revoke_adjudication_refusal"]["code"] == "sanctions_adjudication_not_active"

    assert (await _adjudicate(realdb, vendor_id)).status_code == 201
    cfo = await _row("cfo")
    assert cfo["adjudication"]["reason"] == REASON
    assert cfo["adjudicate_refusal"]["code"] == SANCTIONS_ADJUDICATION_ALREADY_CLEARED
    assert cfo["revoke_adjudication_refusal"] is None
