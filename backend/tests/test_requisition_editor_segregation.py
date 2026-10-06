"""Requisition approval refuses the requester AND every material editor.

``approve_requisition`` used to pass only ``requester_user_id`` to
``check_segregation`` (``segregation_actor_ids=None``). ``PATCH
/api/requisitions/{id}`` lets any admin / ap_manager / ap_clerk rewrite another
user's ``draft`` — lines, vendor, currency, budget — so the editor could then
submit and approve the spend they had just written. That includes the
requisition an ap_manager creates through ``POST /intake/{id}/convert-to-
requisition``, whose requester is the intake's requester. Migration 0102 added
``purchase_requisitions.material_editor_ids``; ``update_requisition`` records
the actor when a field in ``REQUISITION_MATERIAL_EDIT_FIELDS`` actually changes
value, and the approve shim passes the set. Same shape as the recurring
template's editor set (decisions §141, §152).

Two halves:

* **Static guards** (no DB) — the material / cosmetic classification covers
  every ``RequisitionUpdate`` field exactly once; the approve shim passes a real
  value, never a literal ``None``; the set has exactly one writer; and the
  intake conversion body carries no material field (if it ever does, the
  converter shapes the spend and must be stamped).
* **Behaviour** (``realdb``) — an editor cannot approve; a non-material edit, or
  a re-sent identical form, implicates nobody; the intake-converter is refused
  once they edit.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text

from app.models.procurement import (
    REQUISITION_COSMETIC_EDIT_FIELDS,
    REQUISITION_MATERIAL_EDIT_FIELDS,
    PurchaseRequisition,
    RequisitionLineItem,
    RequisitionStatus,
)
from app.models.workflow import AuditLog
from app.schemas.intake import IntakeConvertRequest
from app.schemas.requisition import RequisitionUpdate
from app.services.requisition_service import (
    implicated_editor_ids,
    line_items_differ,
    record_material_editor,
)

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


# ---------------------------------------------------------------------------
# Static guards
# ---------------------------------------------------------------------------


def test_material_and_cosmetic_sets_cover_every_patchable_field_exactly_once():
    """A new ``RequisitionUpdate`` field nobody classified would default to
    "cosmetic" and silently widen the exemption, so it has to land in exactly
    one of the two sets."""
    fields = set(RequisitionUpdate.model_fields)
    assert not (REQUISITION_MATERIAL_EDIT_FIELDS & REQUISITION_COSMETIC_EDIT_FIELDS)
    assert REQUISITION_MATERIAL_EDIT_FIELDS | REQUISITION_COSMETIC_EDIT_FIELDS == fields, (
        f"unclassified: {sorted(fields - REQUISITION_MATERIAL_EDIT_FIELDS - REQUISITION_COSMETIC_EDIT_FIELDS)}; "  # noqa: E501
        f"stale: {sorted((REQUISITION_MATERIAL_EDIT_FIELDS | REQUISITION_COSMETIC_EDIT_FIELDS) - fields)}"  # noqa: E501
    )


def test_intake_conversion_body_carries_no_material_field():
    """The converter is not stamped as an editor because the conversion copies
    the APPROVED intake's terms verbatim and lets them override only cosmetic
    fields. If the body ever grows a material override (a vendor, an amount, a
    budget), the converter shapes the spend and the conversion must record them
    — this fails first, so that cannot happen silently."""
    overlap = set(IntakeConvertRequest.model_fields) & REQUISITION_MATERIAL_EDIT_FIELDS
    assert not overlap, f"convert-to-requisition now accepts material fields {sorted(overlap)}"
    assert set(IntakeConvertRequest.model_fields) <= REQUISITION_COSMETIC_EDIT_FIELDS


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_approve_shim_passes_the_editor_set():
    """Drop the kwarg or hardcode ``None`` and nothing fails loudly: the
    predicate reads "nobody beyond the requester" and the editor can approve
    again — exactly the regression this column exists to close."""
    tree = ast.parse((APP_ROOT / "api" / "requisitions.py").read_text())
    approve = _function(tree, "approve_requisition")
    shims = [
        call.args[0]
        for call in ast.walk(approve)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "check_segregation"
    ]
    assert len(shims) == 1, "approve_requisition must call check_segregation exactly once"
    shim = shims[0]
    assert isinstance(shim, ast.Call)
    kw = {k.arg: k.value for k in shim.keywords}
    assert "segregation_actor_ids" in kw, "the shim must pass segregation_actor_ids"
    value = kw["segregation_actor_ids"]
    assert not (isinstance(value, ast.Constant) and value.value is None), (
        "segregation_actor_ids=None discards the requisition's material editors"
    )


def test_editor_set_has_one_writer():
    """Every assignment to ``.material_editor_ids`` under ``app/`` lives in one of
    the two helpers that own a set — the recurring template's and the
    requisition's — so the "only on a material change, never the requester,
    always a new list" rules cannot be bypassed by a second writer. No
    ``PurchaseRequisition(...)`` construction passes it either: creation
    implicates only the requester, recorded in ``requester_user_id``."""
    allowed = {
        ("services/recurring_invoices.py", "record_material_editor"),
        ("services/requisition_service.py", "record_material_editor"),
    }
    found: set[tuple[str, str]] = set()
    for path in APP_ROOT.rglob("*.py"):
        rel = path.relative_to(APP_ROOT).as_posix()
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                    targets = [node.target]
                for t in targets:
                    if isinstance(t, ast.Attribute) and t.attr == "material_editor_ids":
                        found.add((rel, fn.name))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "PurchaseRequisition"
            ):
                assert "material_editor_ids" not in {k.arg for k in node.keywords}, (
                    f"{rel}:{node.lineno} stamps material_editor_ids at construction"
                )
    assert found == allowed, f"undeclared writers: {sorted(found - allowed)}"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _line(**kw) -> RequisitionLineItem:
    base = {
        "line_number": 1,
        "catalog_item_id": None,
        "item_code": None,
        "description": "Laptop",
        "quantity": Decimal("2"),
        "unit_price": Decimal("1000.00"),
        "gl_account_id": None,
        "uom": None,
    }
    base.update(kw)
    return RequisitionLineItem(**base)


def test_line_items_differ_compares_values_not_presence():
    # The Numeric(12, 4) quantity a loaded row holds equals the payload's "2".
    assert not line_items_differ([_line(quantity=Decimal("2.0000"))], [_line()])
    assert line_items_differ([_line()], [_line(unit_price=Decimal("1000.01"))])
    assert line_items_differ([_line()], [_line(description="Gaming PC")])
    assert line_items_differ([_line()], [_line(gl_account_id=uuid.uuid4())])
    assert line_items_differ([_line()], [_line(), _line(line_number=2)])
    assert line_items_differ([_line()], [])


def test_record_material_editor_is_a_set_without_the_requester():
    requester, editor = uuid.uuid4(), uuid.uuid4()
    req = PurchaseRequisition(requester_user_id=requester)
    record_material_editor(req, requester)
    assert req.material_editor_ids is None
    assert implicated_editor_ids(req) is None
    record_material_editor(req, editor)
    first = req.material_editor_ids
    record_material_editor(req, editor)
    assert req.material_editor_ids == [str(editor)]
    # A new list each time it grows, so the plain-JSONB dirty check sees it.
    other = uuid.uuid4()
    record_material_editor(req, other)
    assert req.material_editor_ids is not first
    assert req.material_editor_ids == sorted([str(editor), str(other)])
    assert implicated_editor_ids(req) == sorted([str(editor), str(other)])


# ---------------------------------------------------------------------------
# Behaviour (realdb)
# ---------------------------------------------------------------------------

_LINES = [
    {"description": "Laptop", "quantity": "2", "unit_price": "1000.00"},
    {"description": "Dock", "quantity": "2", "unit_price": "150.00"},
]


def _form(number: str, **over) -> dict:
    """The full body the edit modal sends on every save."""
    body = {
        "requisition_number": number,
        "title": "Laptops for eng",
        "department": "Engineering",
        "needed_by": None,
        "justification": None,
        "currency": "USD",
        "notes": None,
        "line_items": _LINES,
    }
    body.update(over)
    return body


async def _create_as_clerk(realdb) -> str:
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post("/api/requisitions", json=_form(f"REQ-{uuid.uuid4().hex[:8]}"))
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]


async def _editors(realdb, rid: str):
    async with realdb.sessionmaker("a")() as s:
        return (
            await s.execute(
                select(PurchaseRequisition.material_editor_ids).where(
                    PurchaseRequisition.id == uuid.UUID(rid)
                )
            )
        ).scalar_one()


async def _last_update_details(realdb, rid: str) -> dict:
    async with realdb.sessionmaker("a")() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.action == "requisition.updated",
                        AuditLog.entity_id == uuid.UUID(rid),
                    )
                    .order_by(AuditLog.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    assert rows, "no requisition.updated audit row"
    return rows[0].details


async def test_a_material_editor_cannot_approve_what_they_edited(realdb):
    rid = await _create_as_clerk(realdb)
    manager = realdb.info("a").users["ap_manager"]

    async with realdb.client(key="a", role="ap_manager") as c:
        get = (await c.get(f"/api/requisitions/{rid}")).json()
        edited = await c.patch(
            f"/api/requisitions/{rid}",
            json=_form(
                get["requisition_number"],
                line_items=[{"description": "Laptop", "quantity": "20", "unit_price": "1000.00"}],
            ),
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["total"] == 20000.0
        assert (await c.post(f"/api/requisitions/{rid}/submit")).status_code == 200
        refused = await c.post(f"/api/requisitions/{rid}/approve")
    assert refused.status_code == 403, refused.text
    assert "segregation" in refused.json()["detail"]["message"].lower()
    assert refused.json()["detail"]["code"] == "approval_segregation"
    assert await _editors(realdb, rid) == [str(manager)]
    details = await _last_update_details(realdb, rid)
    assert details["material"] == ["line_items"]

    # Someone who neither requested nor edited it still can.
    async with realdb.client(key="a", role="cfo") as c:
        ok = await c.post(f"/api/requisitions/{rid}/approve")
    assert ok.status_code == 200, ok.text
    assert ok.json()["approved_by"] == str(realdb.info("a").users["cfo"])


async def test_a_currency_change_implicates_the_editor(realdb):
    rid = await _create_as_clerk(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.patch(f"/api/requisitions/{rid}", json={"currency": "EUR"})
        assert resp.status_code == 200, resp.text
        await c.post(f"/api/requisitions/{rid}/submit")
        assert (await c.post(f"/api/requisitions/{rid}/approve")).status_code == 403
    assert (await _last_update_details(realdb, rid))["material"] == ["currency"]


async def test_a_cosmetic_edit_with_the_full_form_resent_implicates_nobody(realdb):
    """The modal re-sends every field and every line. Fixing the title must not
    put the editor in the refused set — nor claim in the audit trail that the
    lines changed."""
    rid = await _create_as_clerk(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        number = (await c.get(f"/api/requisitions/{rid}")).json()["requisition_number"]
        resp = await c.patch(
            f"/api/requisitions/{rid}",
            json=_form(number, title="Laptops for engineering", notes="typo fix"),
        )
        assert resp.status_code == 200, resp.text
        await c.post(f"/api/requisitions/{rid}/submit")
        approved = await c.post(f"/api/requisitions/{rid}/approve")
    assert approved.status_code == 200, approved.text
    assert await _editors(realdb, rid) is None
    details = await _last_update_details(realdb, rid)
    assert sorted(details["fields"]) == ["notes", "title"]
    assert details["material"] == []


async def test_the_requester_editing_their_own_draft_is_not_recorded_twice(realdb):
    rid = await _create_as_clerk(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.patch(f"/api/requisitions/{rid}", json={"currency": "GBP"})
        assert resp.status_code == 200, resp.text
    assert await _editors(realdb, rid) is None


async def test_intake_converter_who_edits_the_requisition_cannot_approve_it(realdb):
    """The converted requisition's requester is the intake's requester (the
    clerk), so before the editor set the converting ap_manager could rewrite its
    lines and approve them. Converting alone copies the approved intake's terms
    and implicates nobody; the edit is what does."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        iid = (
            await c.post(
                "/api/intake",
                json={
                    "request_number": f"INTK-{uuid.uuid4().hex[:8]}",
                    "title": "Figma Enterprise seats",
                    "request_type": "software",
                    "estimated_amount": "1200.00",
                    "currency": "USD",
                    "justification": "Design team scaling",
                },
            )
        ).json()["id"]
        assert (await c.post(f"/api/intake/{iid}/submit")).status_code == 200

    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.post(f"/api/intake/{iid}/approve")).status_code == 200
        conv = await c.post(f"/api/intake/{iid}/convert-to-requisition")
        assert conv.status_code == 200, conv.text
        rid = conv.json()["requisition_id"]
        assert await _editors(realdb, rid) is None

        edited = await c.patch(
            f"/api/requisitions/{rid}",
            json={"line_items": [{"description": "Seats", "quantity": "1", "unit_price": "9000"}]},
        )
        assert edited.status_code == 200, edited.text
        await c.post(f"/api/requisitions/{rid}/submit")
        refused = await c.post(f"/api/requisitions/{rid}/approve")
    assert refused.status_code == 403, refused.text
    assert await _editors(realdb, rid) == [str(realdb.info("a").users["ap_manager"])]


# ---------------------------------------------------------------------------
# Concurrency — PATCH / submit / approve serialise on the requisition row
#
# `material_editor_ids` is read-modify-write, and approval reads the set the
# PATCH writes, so every writing route takes `SELECT … FOR UPDATE` on the row.
# Each test holds that row lock in its own session (standing in for a
# concurrent request), starts the competing request as a task, waits for a REAL
# signal — a backend on the tenant DB waiting on a lock in `pg_stat_activity` —
# then commits the holder's write. Without the lock the request reads the row
# before the holder commits, and its later write either overwrites the
# holder's or acts on stale state; each assertion below fails in that case.
# ---------------------------------------------------------------------------


async def _wait_for_lock_waiter(realdb, key="a", timeout_s: float = 15.0) -> None:
    db_name = realdb.info(key).db_name
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    async with realdb.sessionmaker(key)() as probe:
        while True:
            waiting = (
                await probe.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = :d AND wait_event_type = 'Lock'"
                    ),
                    {"d": db_name},
                )
            ).scalar()
            await probe.rollback()
            if waiting:
                return
            assert loop.time() < deadline, "the request never blocked on the requisition row"
            await asyncio.sleep(0.02)


async def _lock_requisition(session, rid: str) -> PurchaseRequisition:
    return (
        await session.execute(
            select(PurchaseRequisition)
            .where(PurchaseRequisition.id == uuid.UUID(rid))
            .with_for_update()
        )
    ).scalar_one()


async def test_concurrent_material_edits_both_land_in_the_editor_set(realdb):
    """Editor B (the holder) changes the vendor-side terms and records itself
    while editor A's PATCH replaces the lines. Unlocked, A read the NULL set
    before B committed and its write dropped B — who could then approve."""
    rid = await _create_as_clerk(realdb)
    admin = realdb.info("a").users["admin"]
    manager = realdb.info("a").users["ap_manager"]
    async with realdb.sessionmaker("a")() as holder:
        req = await _lock_requisition(holder, rid)
        async with realdb.client(key="a", role="ap_manager") as c:
            task = asyncio.create_task(
                c.patch(
                    f"/api/requisitions/{rid}",
                    json={
                        "line_items": [
                            {"description": "Laptop", "quantity": "9", "unit_price": "1000.00"}
                        ]
                    },
                )
            )
            await _wait_for_lock_waiter(realdb)
            req.currency = "EUR"
            record_material_editor(req, admin)
            await holder.commit()
            resp = await task
    assert resp.status_code == 200, resp.text
    assert await _editors(realdb, rid) == sorted([str(admin), str(manager)])


async def test_a_patch_waiting_behind_a_submit_is_refused(realdb):
    """A PATCH that had read `draft` used to commit after a concurrent submit,
    changing the spend the approver was about to see. Locked, it re-reads the
    row after the submit commits and gets the non-draft 422."""
    rid = await _create_as_clerk(realdb)
    async with realdb.sessionmaker("a")() as holder:
        req = await _lock_requisition(holder, rid)
        async with realdb.client(key="a", role="ap_manager") as c:
            task = asyncio.create_task(
                c.patch(
                    f"/api/requisitions/{rid}",
                    json={
                        "line_items": [
                            {"description": "Laptop", "quantity": "50", "unit_price": "1000.00"}
                        ]
                    },
                )
            )
            await _wait_for_lock_waiter(realdb)
            req.status = RequisitionStatus.pending_approval
            await holder.commit()
            resp = await task
    assert resp.status_code == 422, resp.text
    async with realdb.sessionmaker("a")() as s:
        total = (
            await s.execute(
                select(PurchaseRequisition.total).where(PurchaseRequisition.id == uuid.UUID(rid))
            )
        ).scalar_one()
    assert total == Decimal("2300.00")
    assert await _editors(realdb, rid) is None


async def test_an_approve_waiting_behind_a_material_edit_sees_the_editor(realdb):
    """The approve reads the editor set it checks. Unlocked, it read the set
    before a concurrent material edit by the same person committed, passed the
    check, and approved spend that person had just shaped."""
    rid = await _create_as_clerk(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.post(f"/api/requisitions/{rid}/submit")).status_code == 200
    manager = realdb.info("a").users["ap_manager"]
    async with realdb.sessionmaker("a")() as holder:
        req = await _lock_requisition(holder, rid)
        async with realdb.client(key="a", role="ap_manager") as c:
            task = asyncio.create_task(c.post(f"/api/requisitions/{rid}/approve"))
            await _wait_for_lock_waiter(realdb)
            record_material_editor(req, manager)
            await holder.commit()
            resp = await task
    assert resp.status_code == 403, resp.text
