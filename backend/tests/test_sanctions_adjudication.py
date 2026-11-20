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
    latest_stored_screen,
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


async def _adjudicate(
    realdb,
    vendor_id,
    *,
    role: str = "cfo",
    reason: str = REASON,
    check_id: str | None = None,
):
    if check_id is None:
        async with realdb.sessionmaker(TENANT)() as s:
            check = await latest_stored_screen(s, vendor_id)
        # A vendor with no stored screen still needs a well-formed id.
        check_id = str(check.id) if check is not None else str(uuid.uuid4())
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(
            f"/api/vendors/{vendor_id}/screening/adjudicate",
            json={"reason": reason, "sanctions_check_id": check_id},
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
    assert [r["revoked_by_user_id"] for r in revoked.json()] == [str(info.users["cfo"])]
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
    assert cfo["active_adjudications"] == []
    assert cfo["revoke_adjudication_refusal"]["code"] == "sanctions_adjudication_not_active"
    async with realdb.sessionmaker(TENANT)() as s:
        assert cfo["latest_check_id"] == str((await latest_stored_screen(s, vendor_id)).id)

    assert (await _adjudicate(realdb, vendor_id)).status_code == 201
    cfo = await _row("cfo")
    assert cfo["adjudication"]["reason"] == REASON
    assert [a["id"] for a in cfo["active_adjudications"]] == [cfo["adjudication"]["id"]]
    assert cfo["adjudicate_refusal"]["code"] == SANCTIONS_ADJUDICATION_ALREADY_CLEARED
    assert cfo["revoke_adjudication_refusal"] is None


# ---------------------------------------------------------------------------
# The stamp covers exactly the screen the reviewer saw
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_check_id_is_refused_and_stamps_nothing(realdb):
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    async with mk() as s:
        seen = str((await latest_stored_screen(s, vendor_id)).id)
    # A rescreen lands between the reviewer opening the row and clicking.
    await _rescreen(realdb, vendor_id)
    res = await _adjudicate(realdb, vendor_id, check_id=seen)
    assert res.status_code == 409, res.text
    assert "screened again" in res.json()["detail"]
    async with mk() as s:
        rows = (
            await s.execute(
                select(SanctionsAdjudication).where(SanctionsAdjudication.vendor_id == vendor_id)
            )
        ).all()
    assert rows == []
    # The latest screen is accepted, and the stamp names it.
    res = await _adjudicate(realdb, vendor_id)
    assert res.status_code == 201, res.text
    async with mk() as s:
        assert res.json()["sanctions_check_id"] == str(
            (await latest_stored_screen(s, vendor_id)).id
        )


@pytest.mark.asyncio
async def test_a_pre_payment_screen_is_not_the_row_the_queue_shows(realdb):
    """The dispatch gate writes `pre_payment` rows without updating the vendor;
    the queue's `latest_check_id` stays on the stored screen the stamp covers."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    async with mk() as s:
        stored = str((await latest_stored_screen(s, vendor_id)).id)
    async with mk() as s:
        vendor = await s.get(Vendor, vendor_id)
        await check_payment_compliance(
            s,
            vendor=vendor,
            payment_amount=Decimal("100.00"),
            payment_currency="USD",
            payment_method="ach",
            org_settings=_ORG_SETTINGS,
            organization_id=info.org_id,
            sanctions_adapter=MockSanctionsAdapter(),
        )
        await s.commit()
    async with realdb.client(key=TENANT, role="cfo") as c:
        res = await c.get("/api/vendors/screening/review-queue", params={"page_size": 100})
    row = next(i for i in res.json()["items"] if i["vendor_id"] == str(vendor_id))
    assert row["latest_check_id"] == stored
    res = await _adjudicate(realdb, vendor_id, check_id=row["latest_check_id"])
    assert res.status_code == 201, res.text
    assert res.json()["sanctions_check_id"] == stored


# ---------------------------------------------------------------------------
# Revoking a match re-applies what the match means
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoking_a_match_re_blocks_and_closes_cards_issued_while_it_stood(realdb):
    from app.models.organization import Organization
    from app.models.virtual_card import VirtualCard

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, info.org_id)
        settings = dict(org.settings or {})
        settings["cards"] = {"enabled": True, "program_type": "byok", "provider": "mock"}
        org.settings = settings
        await s.commit()

    vendor_id = await _create_vendor(realdb, "Blocked Party LLC")
    assert (await _adjudicate(realdb, vendor_id)).status_code == 201
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        assert (await c.post(f"/api/vendors/{vendor_id}/unblock")).status_code == 200

    # A card issued in the cleared window.
    inv_id = await _invoice_for(mk, info.org_id, vendor_id)
    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        card = VirtualCard(
            invoice_id=inv_id,
            vendor_id=vendor_id,
            organization_id=info.org_id,
            entity_id=inv.entity_id,
            correlation_id=inv.correlation_id or uuid.uuid4(),
            card_provider="mock",
            provider_card_id=f"mock_{uuid.uuid4().hex[:10]}",
            last_four="4242",
            amount_limit=Decimal("100.00"),
            currency="USD",
            status="created",
        )
        s.add(card)
        await s.commit()
        card_id = card.id

    revoked = await _revoke(realdb, vendor_id)
    assert revoked.status_code == 200, revoked.text
    async with mk() as s:
        vendor = await s.get(Vendor, vendor_id)
        assert vendor.payments_blocked is True
        assert vendor.payments_blocked_reason.startswith("sanctions match")
        assert vendor.screening_adjudication_id is None
        assert (await s.get(VirtualCard, card_id)).status == "cancelled"
    assert await _erp_hold(mk, inv_id) == "payments_blocked"


@pytest.mark.asyncio
async def test_revoking_a_review_adjudication_sets_no_block(realdb):
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    assert (await _adjudicate(realdb, vendor_id)).status_code == 201
    assert (await _revoke(realdb, vendor_id)).status_code == 200
    async with mk() as s:
        assert (await s.get(Vendor, vendor_id)).payments_blocked is False


# ---------------------------------------------------------------------------
# Two people clear a match: the adjudicator may not also unblock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_adjudicator_may_not_lift_the_block_it_cleared(realdb):
    vendor_id = await _create_vendor(realdb, "Blocked Party LLC", role="ap_manager")
    # admin holds both vendor.sanctions.adjudicate and vendor.block.
    assert (await _adjudicate(realdb, vendor_id, role="admin")).status_code == 201
    async with realdb.client(key=TENANT, role="admin") as c:
        res = await c.post(f"/api/vendors/{vendor_id}/unblock")
    assert res.status_code == 403, res.text
    assert "another person" in res.json()["detail"]
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        assert (await s.get(Vendor, vendor_id)).payments_blocked is True
    # A second person may.
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        assert (await c.post(f"/api/vendors/{vendor_id}/unblock")).status_code == 200


@pytest.mark.asyncio
async def test_a_manual_block_on_an_unflagged_vendor_is_not_refused(realdb):
    vendor_id = await _create_vendor(realdb, "Totally Normal Supplies Co")
    async with realdb.client(key=TENANT, role="admin") as c:
        assert (await c.post(f"/api/vendors/{vendor_id}/block", json={})).status_code == 200
        assert (await c.post(f"/api/vendors/{vendor_id}/unblock")).status_code == 200


# ---------------------------------------------------------------------------
# Segregation holds in both orders: changed inputs owe a fresh judgement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bank_change_after_the_stamp_is_not_covered(realdb):
    """The adjudicator later approves a bank change (the reverse of the order
    the segregation refusal covers). The re-screen of the same hit on the new
    bank details is uncovered: the clearance does not ride onto the account."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    inv_id = await _invoice_for(mk, info.org_id, vendor_id)
    assert (await _adjudicate(realdb, vendor_id, role="cfo")).status_code == 201
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "allow"

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

    # The live screen on the new inputs holds at once, before any re-screen.
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "hold"
    screened = await _rescreen(realdb, vendor_id)
    assert screened["screening_status"] == "review"
    async with mk() as s:
        assert (await s.get(Vendor, vendor_id)).screening_adjudication_id is None
    assert await _erp_hold(mk, inv_id) == "sanctions_review"
    # And the cfo, who approved that change, may not re-adjudicate it.
    res = await _adjudicate(realdb, vendor_id, role="cfo")
    assert res.status_code == 403, res.text


@pytest.mark.asyncio
async def test_a_tax_id_edit_after_the_stamp_is_not_covered(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    assert (await _adjudicate(realdb, vendor_id)).status_code == 201
    async with mk() as s:
        (await s.get(Vendor, vendor_id)).tax_id = "98-7654321"
        await s.commit()
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "hold"


# ---------------------------------------------------------------------------
# No hidden adjudication: every active row is listed, revoked, and ignored by
# the live screen unless it is the one the stored screen is shown under
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_older_adjudication_is_listed_revoked_and_not_honoured_live(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    first = (await _adjudicate(realdb, vendor_id)).json()

    # A different hit lands and is adjudicated too: two active rows.
    async with mk() as s:
        (await s.get(Vendor, vendor_id)).name = "Negative News Fixture"
        await s.commit()
    await _rescreen(realdb, vendor_id)
    second = await _adjudicate(realdb, vendor_id, role="admin")
    assert second.status_code == 201, second.text

    async with realdb.client(key=TENANT, role="cfo") as c:
        res = await c.get("/api/vendors/screening/review-queue", params={"page_size": 100})
    row = next(i for i in res.json()["items"] if i["vendor_id"] == str(vendor_id))
    assert row["adjudication"]["id"] == second.json()["id"]
    assert {a["id"] for a in row["active_adjudications"]} == {first["id"], second.json()["id"]}

    # Back on the first name, the live screen returns the first hit: its
    # adjudication is active but is not the vendor's pointer, so it holds.
    async with mk() as s:
        (await s.get(Vendor, vendor_id)).name = "Adverse Media Test Co"
        await s.commit()
    assert await _dispatch_verdict(mk, info.org_id, vendor_id) == "hold"

    # Revoke ends both.
    revoked = await _revoke(realdb, vendor_id)
    assert revoked.status_code == 200, revoked.text
    assert {r["id"] for r in revoked.json()} == {first["id"], second.json()["id"]}
    async with mk() as s:
        active = (
            await s.execute(
                select(SanctionsAdjudication).where(
                    SanctionsAdjudication.vendor_id == vendor_id,
                    SanctionsAdjudication.revoked_at.is_(None),
                )
            )
        ).all()
    assert active == []


# ---------------------------------------------------------------------------
# The run gate's read (`payment_runs.vendor_payability().sanctions_hold`),
# shared by the run builder's vendor checks and the ERP push
# ---------------------------------------------------------------------------


async def _run_gate_hold(mk, inv_id) -> str | None:
    from app.services.payment_runs import vendor_payability

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        return (await vendor_payability(s, [inv]))[inv_id].sanctions_hold


def test_the_adjudicated_lift_never_lifts_the_sticky_block():
    from app.services.payment_runs import (
        HOLD_PAYMENTS_BLOCKED,
        HOLD_SANCTIONS_MATCH,
        HOLD_SANCTIONS_REVIEW,
        VendorPayability,
    )

    def hold(**kw):
        base = {"status": "active", "payments_blocked": False, "screening_status": "review"}
        return VendorPayability(**{**base, **kw}).sanctions_hold

    assert hold() == HOLD_SANCTIONS_REVIEW
    assert hold(screening_adjudicated=True) is None
    assert hold(screening_status="match") == HOLD_SANCTIONS_MATCH
    assert hold(screening_status="match", screening_adjudicated=True) is None
    assert hold(payments_blocked=True, screening_adjudicated=True) == HOLD_PAYMENTS_BLOCKED


@pytest.mark.asyncio
async def test_the_run_gate_follows_the_adjudication(realdb):
    """An adjudicated `review` vendor's invoice passes; revoked, it holds again;
    a different fingerprint holds again."""
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    inv_id = await _invoice_for(mk, info.org_id, vendor_id)
    assert await _run_gate_hold(mk, inv_id) == "sanctions_review"

    assert (await _adjudicate(realdb, vendor_id)).status_code == 201
    assert await _run_gate_hold(mk, inv_id) is None

    assert (await _revoke(realdb, vendor_id)).status_code == 200
    assert await _run_gate_hold(mk, inv_id) == "sanctions_review"

    assert (await _adjudicate(realdb, vendor_id, role="admin")).status_code == 201
    assert await _run_gate_hold(mk, inv_id) is None
    async with mk() as s:
        (await s.get(Vendor, vendor_id)).name = "Negative News Fixture"
        await s.commit()
    await _rescreen(realdb, vendor_id)
    assert await _run_gate_hold(mk, inv_id) == "sanctions_review"


@pytest.mark.asyncio
async def test_the_run_gate_keeps_a_sticky_block_on_an_adjudicated_match(realdb):
    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Blocked Party LLC")
    inv_id = await _invoice_for(mk, info.org_id, vendor_id)
    assert (await _adjudicate(realdb, vendor_id)).status_code == 201
    assert await _run_gate_hold(mk, inv_id) == "payments_blocked"


# ---------------------------------------------------------------------------
# A material edit uncovers the vendor before the re-screen, so a failed
# re-screen leaves the hold standing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_edit_whose_rescreen_fails_leaves_the_hold_standing(realdb):
    from unittest.mock import AsyncMock, patch

    from app.services import vendor_screening as vendor_screening_module

    info = realdb.info(TENANT)
    mk = realdb.sessionmaker(TENANT)
    vendor_id = await _create_vendor(realdb, "Adverse Media Test Co")
    inv_id = await _invoice_for(mk, info.org_id, vendor_id)
    assert (await _adjudicate(realdb, vendor_id)).status_code == 201
    assert await _erp_hold(mk, inv_id) is None

    with patch.object(
        vendor_screening_module,
        "screen_vendor_record",
        AsyncMock(side_effect=RuntimeError("provider down")),
    ):
        async with realdb.client(key=TENANT, role="ap_manager") as c:
            res = await c.patch(f"/api/vendors/{vendor_id}", json={"tax_id": "98-7654321"})
    assert res.status_code == 200, res.text

    async with mk() as s:
        vendor = await s.get(Vendor, vendor_id)
        assert vendor.screening_adjudication_id is None
        assert vendor.screening_status == "review"
    assert await _erp_hold(mk, inv_id) == "sanctions_review"
    changes = await _audit(mk, vendor_id, CLEARANCE_CHANGED_ACTION)
    assert any(c.details.get("trigger") == "material_edit" for c in changes)
