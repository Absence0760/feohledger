"""The screening queue's adjudication verdicts say exactly what the endpoints
answer (issue #575 § One rule, one place; decisions §477).

`GET /api/vendors/screening/review-queue` ships `adjudicate_refusal` and
`revoke_adjudication_refusal` per row. The parity property, per role × row
state: the verdict is `None` exactly when the POST is 2xx; otherwise the POST
answers the verdict's refusal (`require_permission`'s own 403 for
`role_not_permitted`, else the verdict's English `message` as the plain-string
detail these endpoints have always answered with).

Row states: no adjudication, an active one, a revoked one, the caller is the
vendor's onboarder, the caller approved its current bank details, and a stale
`sanctions_check_id` (a rescreen landed between the read and the click).

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest

TENANT = "a"
ROLES = ("admin", "ap_manager", "ap_clerk", "cfo")
STATES = ("none", "active", "revoked", "onboarder", "bank_change", "stale")
REASON = "Different entity: registration number does not match the list entry."
pytestmark = pytest.mark.asyncio


def _assert_parity(verdict, resp, label):
    if verdict is None:
        assert resp.status_code in (200, 201), (label, resp.text)
        return
    assert resp.status_code in (403, 409), (label, verdict, resp.text)
    if verdict["code"] == "role_not_permitted":
        assert resp.status_code == 403, (label, resp.text)
        return
    assert resp.json()["detail"] == verdict["message"], (label, verdict, resp.text)


def _other(role: str) -> str:
    # A permitted reviewer who is never the caller.
    return "admin" if role == "cfo" else "cfo"


async def _vendor(realdb, role: str, state: str) -> uuid.UUID:
    """A `review`-screened vendor in `state` relative to `role`."""
    info = realdb.info(TENANT)
    caller = info.users[role]
    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        ent = (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()
        v = Vendor(
            organization_id=info.org_id,
            entity_id=ent,
            name="Adverse Media Test Co",
            code=uuid.uuid4().hex[:8],
            status="active",
            onboarded_by_user_id=caller if state == "onboarder" else None,
        )
        s.add(v)
        await s.flush()
        if state == "bank_change":
            s.add(
                VendorChangeRequest(
                    vendor_id=v.id,
                    organization_id=info.org_id,
                    requested_by_user_id=info.users["ap_clerk" if role != "ap_clerk" else "admin"],
                    change_type="bank_details",
                    status="approved",
                    proposed_value={"bank_details": {"account_last4": "6789"}},
                    reviewed_by_user_id=caller,
                    reviewed_at=datetime.now(UTC),
                )
            )
        await s.commit()
        vendor_id = v.id
    await _rescreen(realdb, vendor_id)
    if state in ("active", "revoked"):
        row = await _row(realdb, _other(role), vendor_id)
        res = await _post_adjudicate(realdb, _other(role), vendor_id, row["latest_check_id"])
        assert res.status_code == 201, res.text
        if state == "revoked":
            assert (await _post_revoke(realdb, _other(role), vendor_id)).status_code == 200
    return vendor_id


async def _rescreen(realdb, vendor_id) -> None:
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        res = await c.post(f"/api/vendors/{vendor_id}/screen")
    assert res.status_code == 200, res.text


async def _row(realdb, role: str, vendor_id) -> dict:
    async with realdb.client(key=TENANT, role=role) as c:
        res = await c.get("/api/vendors/screening/review-queue", params={"page_size": 100})
    assert res.status_code == 200, res.text
    return next(i for i in res.json()["items"] if i["vendor_id"] == str(vendor_id))


async def _post_adjudicate(realdb, role: str, vendor_id, check_id: str):
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(
            f"/api/vendors/{vendor_id}/screening/adjudicate",
            json={"reason": REASON, "sanctions_check_id": check_id},
        )


async def _post_revoke(realdb, role: str, vendor_id):
    async with realdb.client(key=TENANT, role=role) as c:
        return await c.post(
            f"/api/vendors/{vendor_id}/screening/adjudication/revoke",
            json={"reason": "New evidence from the provider."},
        )


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("state", STATES)
async def test_adjudicate_verdict_matches_the_endpoint(realdb, role, state):
    vendor_id = await _vendor(realdb, role, state)
    row = await _row(realdb, role, vendor_id)
    verdict = row["adjudicate_refusal"]
    label = f"adjudicate/{role}/{state}"
    if state == "stale":
        seen = row["latest_check_id"]
        await _rescreen(realdb, vendor_id)
        resp = await _post_adjudicate(realdb, role, vendor_id, seen)
        if verdict is not None and verdict["code"] == "role_not_permitted":
            assert resp.status_code == 403, (label, resp.text)
            return
        # The row the reader saw said "allowed"; the screen it named is gone.
        assert verdict is None, (label, verdict)
        assert resp.status_code == 409, (label, resp.text)
        assert "screened again" in resp.json()["detail"], (label, resp.text)
        # A fresh read names the new screen, and parity holds again.
        row = await _row(realdb, role, vendor_id)
        verdict = row["adjudicate_refusal"]
    resp = await _post_adjudicate(realdb, role, vendor_id, row["latest_check_id"])
    _assert_parity(verdict, resp, label)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("state", STATES)
async def test_revoke_verdict_matches_the_endpoint(realdb, role, state):
    vendor_id = await _vendor(realdb, role, state)
    row = await _row(realdb, role, vendor_id)
    resp = await _post_revoke(realdb, role, vendor_id)
    _assert_parity(row["revoke_adjudication_refusal"], resp, f"revoke/{role}/{state}")
