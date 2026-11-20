"""Intake and requisition rows carry the caller's verdict on each lifecycle
action, and it says exactly what the endpoint answers (issue #575 § One rule,
one place).

* intake — `actions.{submit,approve,reject,convert,cancel,reopen}`. The page
  restated them as a status list plus `auth.isManager`;
* requisitions — the same six. The page restated a status list, three role
  sets and `requester_user_id !== me`: that hid a Reject the endpoint accepts
  from the requester, and offered an Approve that 403s to a material editor
  of someone else's draft.

The parity property, per role × row state × whose row: verdict `None` exactly
when the mutation succeeds, otherwise exactly the coded detail it answers with
(only `require_roles`' own 403 stays a plain string). Both sides are asked, and
the list, the detail and the mutation response carry the same verdict shape.

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.procurement import IntakeRequest, PurchaseRequisition

TENANT = "a"
ROLES = ("admin", "ap_manager", "ap_clerk", "cfo")
ACTIONS = ("submit", "approve", "reject", "convert", "cancel", "reopen")
pytestmark = pytest.mark.asyncio


def _assert_parity(verdict, resp, label):
    if verdict is None:
        assert resp.status_code in (200, 201), (label, resp.text)
        return
    assert resp.status_code in (403, 409, 422), (label, verdict, resp.text)
    detail = resp.json()["detail"]
    if verdict["code"] == "role_not_permitted":
        assert resp.status_code == 403, (label, resp.text)
        return
    assert detail == verdict, (label, verdict, detail)


async def _default_entity(realdb) -> uuid.UUID:
    async with realdb.sessionmaker(TENANT)() as s:
        return (await s.execute(select(Entity.id).where(Entity.is_default))).scalar_one()


def _other_user(realdb, role: str) -> uuid.UUID:
    users = realdb.info(TENANT).users
    return users["ap_clerk" if role != "ap_clerk" else "admin"]


# ── Intake ──────────────────────────────────────────────────────────────────

INTAKE_STATES = ("open", "in_review", "approved", "rejected", "converted", "cancelled")


async def _intake(realdb, *, requester: uuid.UUID, status: str) -> uuid.UUID:
    intake_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            IntakeRequest(
                id=intake_id,
                request_number=f"INTK-V-{intake_id.hex[:8]}",
                title="Parity intake",
                requester_user_id=requester,
                estimated_amount=Decimal("120.00"),
                currency="USD",
                status=status,
                organization_id=realdb.info(TENANT).org_id,
                entity_id=await _default_entity(realdb),
            )
        )
        await s.commit()
    return intake_id


async def _post_intake(c, intake_id, action):
    base = f"/api/intake/{intake_id}"
    if action == "convert":
        return await c.post(f"{base}/convert-to-requisition")
    if action in ("approve", "reject", "cancel"):
        return await c.post(f"{base}/{action}", json={"reason": "parity"})
    return await c.post(f"{base}/{action}")


@pytest.mark.parametrize("role", ROLES)
async def test_the_intake_verdicts_match_every_lifecycle_endpoint(realdb, role):
    me = realdb.info(TENANT).users[role]
    seen = set()
    for state in INTAKE_STATES:
        for whose in ("mine", "theirs"):
            requester = me if whose == "mine" else _other_user(realdb, role)
            for action in ACTIONS:
                intake_id = await _intake(realdb, requester=requester, status=state)
                async with realdb.client(key=TENANT, role=role) as c:
                    listed = await c.get("/api/intake", params={"page_size": 100})
                    detail = await c.get(f"/api/intake/{intake_id}")
                    assert listed.status_code == 200, listed.text
                    assert detail.status_code == 200, detail.text
                    row = {i["id"]: i for i in listed.json()["items"]}[str(intake_id)]
                    assert row["actions"] == detail.json()["actions"]
                    verdict = row["actions"][action]
                    resp = await _post_intake(c, intake_id, action)
                _assert_parity(verdict, resp, (role, state, whose, action))
                if verdict is not None:
                    seen.add(verdict["code"])
                elif action != "convert":
                    # A mutation response carries the verdicts too, so a row
                    # the page upserts from it keeps its controls.
                    assert set(resp.json()["actions"]) == set(ACTIONS)
    if role in ("admin", "ap_manager"):
        assert "intake_status_refused" in seen
    else:
        assert "role_not_permitted" in seen


async def test_a_converted_intake_replays_its_requisition_and_says_so(realdb):
    """`converted` is the one state convert accepts that is not `approved`:
    the endpoint replays the requisition it already built (`created` false),
    so the verdict is `None` there too."""
    intake_id = await _intake(
        realdb, requester=_other_user(realdb, "ap_manager"), status="approved"
    )
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post(f"/api/intake/{intake_id}/convert-to-requisition")
        assert first.status_code == 200, first.text
        assert first.json()["intake"]["actions"]["convert"] is None
        again = await c.post(f"/api/intake/{intake_id}/convert-to-requisition")
        assert again.status_code == 200 and again.json()["created"] is False


# ── Requisitions ────────────────────────────────────────────────────────────

REQUISITION_STATES = (
    "draft",
    "submitted",
    "pending_approval",
    "approved",
    "rejected",
    "converted",
    "cancelled",
)


async def _requisition(
    realdb, *, requester: uuid.UUID, status: str, editors: list[uuid.UUID] | None = None
) -> uuid.UUID:
    req_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            PurchaseRequisition(
                id=req_id,
                requisition_number=f"REQ-V-{req_id.hex[:8]}",
                title="Parity requisition",
                requester_user_id=requester,
                status=status,
                total=Decimal("250.00"),
                currency="USD",
                material_editor_ids=sorted(str(e) for e in editors) if editors else None,
                organization_id=realdb.info(TENANT).org_id,
                entity_id=await _default_entity(realdb),
            )
        )
        await s.commit()
    return req_id


async def _post_requisition(c, req_id, action):
    base = f"/api/requisitions/{req_id}"
    if action == "convert":
        return await c.post(f"{base}/convert-to-po")
    if action in ("reject", "cancel"):
        return await c.post(f"{base}/{action}", json={"reason": "parity"})
    return await c.post(f"{base}/{action}")


#: Whose row it is, relative to the caller: the requester, a material editor of
#: someone else's draft, or uninvolved.
WHOSE = ("mine", "edited_by_me", "theirs")


@pytest.mark.parametrize("role", ROLES)
async def test_the_requisition_verdicts_match_every_lifecycle_endpoint(realdb, role):
    me = realdb.info(TENANT).users[role]
    other = _other_user(realdb, role)
    seen = set()
    for state in REQUISITION_STATES:
        for whose in WHOSE:
            requester = me if whose == "mine" else other
            editors = [me] if whose == "edited_by_me" else None
            for action in ACTIONS:
                req_id = await _requisition(
                    realdb, requester=requester, status=state, editors=editors
                )
                async with realdb.client(key=TENANT, role=role) as c:
                    listed = await c.get("/api/requisitions", params={"page_size": 100})
                    detail = await c.get(f"/api/requisitions/{req_id}")
                    assert listed.status_code == 200, listed.text
                    assert detail.status_code == 200, detail.text
                    row = {i["id"]: i for i in listed.json()["items"]}[str(req_id)]
                    assert row["actions"] == detail.json()["actions"]
                    verdict = row["actions"][action]
                    resp = await _post_requisition(c, req_id, action)
                _assert_parity(verdict, resp, (role, state, whose, action))
                if verdict is not None:
                    seen.add(verdict["code"])
                elif action != "convert":
                    assert set(resp.json()["actions"]) == set(ACTIONS)
    if role in ("admin", "ap_manager", "cfo"):
        # The requester and a material editor are both refused approve.
        assert "approval_segregation" in seen
        assert "requisition_status_refused" in seen
    else:
        assert "role_not_permitted" in seen


async def test_the_requester_may_reject_their_own_requisition(realdb):
    """The page hid Reject from the requester; the endpoint never refused it
    (segregation guards approval, not withdrawal). The verdict says what the
    endpoint does."""
    me = realdb.info(TENANT).users["ap_manager"]
    req_id = await _requisition(realdb, requester=me, status="pending_approval")
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        row = (await c.get(f"/api/requisitions/{req_id}")).json()
        assert row["actions"]["approve"]["code"] == "approval_segregation"
        assert row["actions"]["reject"] is None
        resp = await c.post(f"/api/requisitions/{req_id}/reject", json={"reason": "withdrawn"})
        assert resp.status_code == 200, resp.text


async def test_a_converted_requisition_replays_its_po(realdb):
    """Convert on a requisition holding its PO replays it (`created` false), so
    the verdict is `None`; a converted row with no PO is refused."""
    other = _other_user(realdb, "ap_manager")
    req_id = await _requisition(realdb, requester=other, status="approved")
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        first = await c.post(f"/api/requisitions/{req_id}/convert-to-po")
        assert first.status_code == 200 and first.json()["created"] is True, first.text
        row = (await c.get(f"/api/requisitions/{req_id}")).json()
        assert row["status"] == "converted"
        assert row["actions"]["convert"] is None
        again = await c.post(f"/api/requisitions/{req_id}/convert-to-po")
        assert again.status_code == 200 and again.json()["created"] is False
