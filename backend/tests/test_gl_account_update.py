"""``PATCH /api/gl-accounts/{id}`` — correct or retire a GL account.

Until this route existed, an account created with the wrong name, type or parent
was permanent, and nothing under ``app/`` ever wrote ``GLAccount.is_active``: the
list endpoint's ``active_only`` filter (and the page's *Include inactive*
toggle) revealed a state only direct SQL or an imported chart could produce.

**Retire, never delete**, and these tests pin why rather than only that:

* ``Invoice.gl_account`` / ``InvoiceLineItem.gl_account`` are ``String(100)``
  *codes*, not foreign keys — a hard delete would leave every posted line
  pointing at a code the chart no longer contains, silently, with no constraint
  to raise.
* ``Expense``, ``RequisitionLine`` and ``CatalogItem`` DO hold a real
  ``ForeignKey("gl_accounts.id")`` declared with no ``ON DELETE``, so Postgres
  defaults to ``NO ACTION`` and a delete against a referenced account raises a
  ``ForeignKeyViolation`` — a 500, not a clean refusal.

Both halves are asserted below, so a future PR that adds a DELETE has to
confront the reason there isn't one.

Two fields stay immutable and are absent from ``GLAccountUpdate`` entirely:
``code`` (an invoice records it as a string) and ``entity_id`` (NULL means
SHARED, so moving a row between charts either steals the account from every
other entity or hands it to all of them). A genuine move is create + deactivate.

Real-Postgres harness (`realdb`).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.gl_account import GLAccount

TENANT = "a"


async def _add_account(realdb, key: str, **kwargs) -> uuid.UUID:
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        acct = GLAccount(organization_id=realdb.info(key).org_id, **kwargs)
        s.add(acct)
        await s.commit()
        return acct.id


async def _row(realdb, key: str, account_id: uuid.UUID) -> GLAccount:
    mk = realdb.sessionmaker(key)
    async with mk() as s:
        return (await s.execute(select(GLAccount).where(GLAccount.id == account_id))).scalar_one()


@pytest_asyncio.fixture
async def entities(realdb):
    """(default_entity_id, subsidiary_id) as strings. Entity CRUD is admin-only."""
    async with realdb.client(key=TENANT, role="admin") as admin:
        r = await admin.post(
            "/api/entities",
            json={"name": "GL Patch Sub", "slug": f"gl-patch-{uuid.uuid4().hex[:8]}"},
        )
        assert r.status_code == 201, r.text
        sub_id = r.json()["id"]
        rows = (await admin.get("/api/entities")).json()
    return next(e["id"] for e in rows if e["is_default"]), sub_id


# ---------------------------------------------------------------------------
# correction
# ---------------------------------------------------------------------------


async def test_patch_corrects_name_type_and_parent(realdb):
    account_id = await _add_account(
        realdb, TENANT, code="5000", name="Ofice Supplies", account_type="expence"
    )

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(
            f"/api/gl-accounts/{account_id}",
            json={"name": "Office Supplies", "account_type": "expense", "parent_code": "5"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Office Supplies"
    assert body["account_type"] == "expense"
    assert body["parent_code"] == "5"
    # The code is untouched — it is what posted invoice lines carry.
    assert body["code"] == "5000"

    row = await _row(realdb, TENANT, account_id)
    assert (row.name, row.account_type, row.parent_code) == ("Office Supplies", "expense", "5")


async def test_patch_leaves_unset_fields_alone(realdb):
    """`exclude_unset` — a PATCH of one field must not blank the others. Sending
    only `name` used to be the kind of request that quietly nulls `account_type`
    if the handler read `model_dump()` instead."""
    account_id = await _add_account(
        realdb, TENANT, code="5100", name="Old", account_type="expense", parent_code="5"
    )

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"name": "New"})

    assert resp.status_code == 200
    row = await _row(realdb, TENANT, account_id)
    assert row.name == "New"
    assert row.account_type == "expense"
    assert row.parent_code == "5"


async def test_patch_can_clear_an_optional_field_explicitly(realdb):
    """An explicit `null` is distinguishable from "not sent" and DOES clear."""
    account_id = await _add_account(realdb, TENANT, code="5200", name="X", parent_code="5")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"parent_code": None})

    assert resp.status_code == 200
    assert resp.json()["parent_code"] is None
    assert (await _row(realdb, TENANT, account_id)).parent_code is None


async def test_patch_rejects_an_empty_body(realdb):
    """A PATCH with nothing set is a client bug, not a no-op worth a 200."""
    account_id = await _add_account(realdb, TENANT, code="5300", name="X")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={})

    assert resp.status_code == 422


async def test_patch_rejects_a_blank_name(realdb):
    """A whitespace-only name passes `min_length=1` but is not a name."""
    account_id = await _add_account(realdb, TENANT, code="5400", name="Real Name")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"name": "   "})

    assert resp.status_code == 422
    assert (await _row(realdb, TENANT, account_id)).name == "Real Name"


async def test_code_and_entity_id_are_not_patchable(realdb, entities):
    """The two immutable fields. Pydantic ignores unknown keys, so the assertion
    that matters is that the values did NOT move — a silently-accepted `code`
    would orphan every invoice line already coded to it."""
    _default_id, sub_id = entities
    account_id = await _add_account(realdb, TENANT, code="6000", name="Travel")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(
            f"/api/gl-accounts/{account_id}",
            json={"code": "9999", "entity_id": sub_id, "name": "Travel & Expense"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "6000"
    assert body["entity_id"] is None
    assert body["name"] == "Travel & Expense"

    row = await _row(realdb, TENANT, account_id)
    assert row.code == "6000"
    assert row.entity_id is None


# ---------------------------------------------------------------------------
# retirement
# ---------------------------------------------------------------------------


async def test_deactivate_retires_the_account_from_the_chart(realdb):
    """The whole point: a retired account leaves the default list — and so every
    GL picker — while the row (and every historical reference to its code)
    survives."""
    account_id = await _add_account(realdb, TENANT, code="7000", name="Obsolete")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"is_active": False})
        default_list = (await c.get("/api/gl-accounts")).json()
        with_inactive = (await c.get("/api/gl-accounts", params={"active_only": "false"})).json()

    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    assert "7000" not in {r["code"] for r in default_list}
    assert "7000" in {r["code"] for r in with_inactive}
    assert (await _row(realdb, TENANT, account_id)).is_active is False


async def test_a_retired_account_can_be_brought_back(realdb):
    account_id = await _add_account(realdb, TENANT, code="7100", name="Back", is_active=False)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"is_active": True})
        codes = {r["code"] for r in (await c.get("/api/gl-accounts")).json()}

    assert resp.status_code == 200
    assert resp.json()["is_active"] is True
    assert "7100" in codes


async def test_a_retired_account_is_no_longer_valid_for_coding(realdb):
    """Retirement has to reach the validator, not just the list. `_ActiveChart`
    is what invoice GL coding and bulk-recode check against, and it filters on
    `is_active` — so deactivating is what actually stops new coding."""
    from app.services.gl_recode import _load_active_chart

    account_id = await _add_account(realdb, TENANT, code="7200", name="Soon Retired")
    org_id = realdb.info(TENANT).org_id

    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        before = await _load_active_chart(s, org_id)
    assert before.is_valid_for("7200", None)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        assert (
            await c.patch(f"/api/gl-accounts/{account_id}", json={"is_active": False})
        ).status_code == 200

    async with mk() as s:
        after = await _load_active_chart(s, org_id)
    assert not after.is_valid_for("7200", None)


# ---------------------------------------------------------------------------
# why there is no DELETE
# ---------------------------------------------------------------------------


async def test_there_is_no_delete_route(realdb):
    account_id = await _add_account(realdb, TENANT, code="7300", name="Undeletable")

    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.delete(f"/api/gl-accounts/{account_id}")

    assert resp.status_code == 405


async def test_a_hard_delete_would_orphan_an_invoices_gl_string(realdb):
    """Half one of the no-DELETE case. The invoice side has NO foreign key — it
    records the GL as a string — so deleting the account is silent data loss
    rather than a refused statement."""
    from app.models.invoice import Invoice

    assert isinstance(Invoice.__table__.columns["gl_account"].type.length, int)
    assert not Invoice.__table__.columns["gl_account"].foreign_keys, (
        "Invoice.gl_account gained a foreign key — re-evaluate the no-DELETE rule"
    )


async def test_every_gl_foreign_key_is_no_action_in_the_real_database(realdb):
    """Half two, asserted against live Postgres rather than the ORM.

    Three tables hold a real `ForeignKey("gl_accounts.id")` and all three declare
    it with no `ON DELETE` clause, so Postgres stores `confdeltype = 'a'`
    (NO ACTION): deleting a referenced account raises a ForeignKeyViolation —
    a 500 from a DELETE route, not a clean refusal. Together with the invoice
    side (which has no FK at all and so fails *silently*), that is the whole
    case for retirement being a flag.

    Read from `pg_constraint` so the assertion survives an ORM refactor and
    catches an `ON DELETE CASCADE` added in a migration — which would be far
    worse than either failure mode, since it would delete the referencing
    expense and requisition lines along with the account.
    """
    from sqlalchemy import text

    mk = realdb.sessionmaker(TENANT)
    async with mk() as s:
        rows = (
            await s.execute(
                text(
                    """
                    SELECT c.conrelid::regclass::text AS table_name, c.confdeltype
                    FROM pg_constraint c
                    WHERE c.contype = 'f'
                      AND c.confrelid = 'gl_accounts'::regclass
                    """
                )
            )
        ).all()

    assert rows, "no FKs point at gl_accounts — re-evaluate the no-DELETE rule"
    # `confdeltype` is Postgres's internal `"char"`, which asyncpg hands back as
    # a one-byte `bytes`. 'a' = NO ACTION.
    offenders = {
        name: deltype
        for name, deltype in rows
        if (deltype.decode() if isinstance(deltype, bytes) else deltype) != "a"
    }
    assert not offenders, (
        "an ON DELETE rule appeared on a gl_accounts FK. CASCADE would delete "
        "referencing expense / requisition / catalog rows with the account, and "
        "SET NULL would erase their coding. Both break the money trail the "
        f"no-DELETE rule protects: {offenders}"
    )


# ---------------------------------------------------------------------------
# parent-chain cycles
# ---------------------------------------------------------------------------


async def test_an_account_cannot_become_its_own_parent(realdb):
    account_id = await _add_account(realdb, TENANT, code="8000", name="Self")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"parent_code": "8000"})

    assert resp.status_code == 422
    assert "own parent" in resp.json()["detail"]


async def test_a_longer_parent_cycle_is_refused(realdb):
    """`A → B → C`, then pointing A's parent at C would close the loop. Cycles
    were unreachable before PATCH existed (parent was fixed at create), which is
    why the guard lives on this route."""
    a_id = await _add_account(realdb, TENANT, code="8100", name="A")
    await _add_account(realdb, TENANT, code="8200", name="B", parent_code="8100")
    await _add_account(realdb, TENANT, code="8300", name="C", parent_code="8200")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{a_id}", json={"parent_code": "8300"})

    assert resp.status_code == 422
    assert "loop" in resp.json()["detail"]
    assert (await _row(realdb, TENANT, a_id)).parent_code is None


async def test_a_legitimate_reparent_still_works(realdb):
    """The cycle guard must not refuse an ordinary move up the tree."""
    await _add_account(realdb, TENANT, code="8400", name="Parent")
    child_id = await _add_account(realdb, TENANT, code="8500", name="Child", parent_code="8400")
    await _add_account(realdb, TENANT, code="8600", name="New Parent")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{child_id}", json={"parent_code": "8600"})

    assert resp.status_code == 200
    assert resp.json()["parent_code"] == "8600"


async def test_an_unknown_parent_code_is_allowed(realdb):
    """Parity with create, which does not resolve `parent_code` either. The
    cycle guard is not a referential-integrity guard, and tightening PATCH alone
    would make a chart created via POST un-PATCHable."""
    account_id = await _add_account(realdb, TENANT, code="8700", name="Orphan")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"parent_code": "NOPE"})

    assert resp.status_code == 200
    assert resp.json()["parent_code"] == "NOPE"


# ---------------------------------------------------------------------------
# entity scope — an editor follows the CREATE rule, not the READ rule
# ---------------------------------------------------------------------------


async def test_an_entity_context_cannot_edit_a_shared_account(realdb, entities):
    """A shared row (`entity_id IS NULL`) is visible in every entity's chart, so
    the read rule would let subsidiary B retire an account subsidiary A depends
    on. Editing one is therefore consolidated-only — the same view that creates
    a shared row."""
    _default_id, sub_id = entities
    account_id = await _add_account(realdb, TENANT, code="9000", name="Shared", entity_id=None)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        # It IS visible from the subsidiary's chart...
        listed = (await c.get("/api/gl-accounts", headers={"X-Entity-ID": sub_id})).json()
        assert "9000" in {r["code"] for r in listed}
        # ...and still not theirs to change.
        resp = await c.patch(
            f"/api/gl-accounts/{account_id}",
            json={"is_active": False},
            headers={"X-Entity-ID": sub_id},
        )

    assert resp.status_code == 403
    assert "consolidated" in resp.json()["detail"]
    assert (await _row(realdb, TENANT, account_id)).is_active is True


async def test_an_entity_context_can_edit_its_own_account(realdb, entities):
    _default_id, sub_id = entities
    account_id = await _add_account(
        realdb, TENANT, code="9100", name="Sub Own", entity_id=uuid.UUID(sub_id)
    )

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(
            f"/api/gl-accounts/{account_id}",
            json={"name": "Sub Own Fixed"},
            headers={"X-Entity-ID": sub_id},
        )

    assert resp.status_code == 200
    assert resp.json()["name"] == "Sub Own Fixed"


async def test_an_entity_context_cannot_edit_another_entitys_account(realdb, entities):
    default_id, sub_id = entities
    account_id = await _add_account(
        realdb, TENANT, code="9200", name="Default Own", entity_id=uuid.UUID(default_id)
    )

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(
            f"/api/gl-accounts/{account_id}",
            json={"name": "Stolen"},
            headers={"X-Entity-ID": sub_id},
        )

    assert resp.status_code == 403


async def test_the_consolidated_view_can_edit_any_row(realdb, entities):
    """So a typo in a subsidiary's chart is fixable without switching entity."""
    _default_id, sub_id = entities
    account_id = await _add_account(
        realdb, TENANT, code="9300", name="Typo", entity_id=uuid.UUID(sub_id)
    )

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"name": "Fixed"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "Fixed"
    assert resp.json()["entity_id"] == sub_id


# ---------------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------------


async def _audit_rows(realdb, key: str) -> list:
    from app.models.workflow import AuditLog

    mk = realdb.sessionmaker(key)
    async with mk() as s:
        return (await s.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars().all()


async def test_a_correction_writes_an_audit_row(realdb):
    account_id = await _add_account(realdb, TENANT, code="9400", name="Before")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        assert (
            await c.patch(f"/api/gl-accounts/{account_id}", json={"name": "After"})
        ).status_code == 200

    rows = [r for r in await _audit_rows(realdb, TENANT) if r.entity_type == "gl_account"]
    assert [r.action for r in rows] == ["gl_account.updated"]
    assert rows[0].details["code"] == "9400"
    assert rows[0].details["changed"] == {"name": "After"}


async def test_a_retirement_gets_its_own_action_name(realdb):
    """So an auditor can find every retirement without parsing `changed`."""
    account_id = await _add_account(realdb, TENANT, code="9500", name="Retiring")

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        assert (
            await c.patch(f"/api/gl-accounts/{account_id}", json={"is_active": False})
        ).status_code == 200
        assert (
            await c.patch(f"/api/gl-accounts/{account_id}", json={"is_active": True})
        ).status_code == 200

    rows = [r for r in await _audit_rows(realdb, TENANT) if r.entity_type == "gl_account"]
    assert [r.action for r in rows] == ["gl_account.deactivated", "gl_account.reactivated"]


async def test_a_no_op_patch_writes_no_audit_row(realdb):
    """An audit row asserting a change that did not happen is noise, and this
    trail is append-only — so it never gets cleaned up. Mirrors `sync-erp`,
    which only counts an account as updated when a value actually moved."""
    account_id = await _add_account(
        realdb, TENANT, code="9600", name="Same", account_type="expense"
    )

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(
            f"/api/gl-accounts/{account_id}",
            json={"name": "Same", "account_type": "expense"},
        )

    assert resp.status_code == 200
    assert [r for r in await _audit_rows(realdb, TENANT) if r.entity_type == "gl_account"] == []


# ---------------------------------------------------------------------------
# RBAC + isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "ap_manager"])
async def test_managers_may_patch(realdb, role):
    account_id = await _add_account(realdb, TENANT, code=f"111{role[0]}", name="X")
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"name": "Y"})
    assert resp.status_code == 200


@pytest.mark.parametrize("role", ["ap_clerk", "cfo"])
async def test_non_managers_may_not_patch(realdb, role):
    """Same gate as `POST ""` and `POST /sync-erp`. The READ is role-open — a
    clerk has to look codes up — but correcting the chart is not theirs."""
    account_id = await _add_account(realdb, TENANT, code=f"222{role[0]}", name="X")
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"name": "Y"})
    assert resp.status_code == 403
    assert (await _row(realdb, TENANT, account_id)).name == "X"


async def test_patch_requires_auth(realdb):
    account_id = await _add_account(realdb, TENANT, code="3333", name="X")
    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"name": "Y"})
    assert resp.status_code == 401


async def test_another_tenants_account_is_not_found(realdb):
    """Tenant isolation at the data layer: tenant `b` resolves its own DB, so
    `a`'s id simply does not exist there."""
    account_id = await _add_account(realdb, "a", code="4444", name="Tenant A")

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{account_id}", json={"name": "Hijacked"})

    assert resp.status_code == 404
    assert (await _row(realdb, "a", account_id)).name == "Tenant A"


async def test_an_unknown_id_is_404(realdb):
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.patch(f"/api/gl-accounts/{uuid.uuid4()}", json={"name": "Y"})
    assert resp.status_code == 404
