"""`GET /api/invoices/chat/mentionable-users` — the @mention picker's source.

The picker shipped with no source at all. `SupplierChatThread` derives its
candidates from a `members` prop that `InvoiceModal` filled from the admin
store's `users` — a list only `/admin` and `/workflows/[id]` ever load. Landing
on `/invoices` directly left the dropdown permanently empty; arriving via
`/admin` first made it work. Same feature, different behaviour by navigation
path.

Neither existing endpoint fits, which is why this one exists:

* `GET /api/admin/users` is `require_permission(user.manage)` and returns
  emails, roles and last-login. Every non-admin 403s (the identical bug already
  fixed once for the approver picker) and none of that payload belongs in a
  chat composer.
* `GET /api/invoices/assignable-reviewers` is PII-free but gated
  admin/ap_manager and scoped to holders of `invoice.approve`. Mentioning is
  broader in both directions: an ap_clerk or CFO can post to the thread yet
  could not read that list, and a clerk is a perfectly reasonable mention.

So the gate is `get_current_user` — exactly what posting a mention requires —
and the projection is id / full_name / is_active, nothing else.

The second half pinned here is the POST's own check: `mention_user_ids` was
validated as a well-formed UUID and nothing more, so any id at all could be
persisted onto `SupplierChatMessage.mentions` and read back on every GET of the
thread. Nothing leaked (`notify_event`'s recipient load is org-scoped), but the
record asserted a mention of someone who was never notified. Both halves now
read the same roster.

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.supplier_chat import SupplierChatMessage, SupplierChatThread
from app.models.user import Role, User, UserRole

pytestmark = pytest.mark.asyncio

TENANT = "a"
PATH = "/api/invoices/chat/mentionable-users"


@pytest_asyncio.fixture(autouse=True)
async def _fresh_control_factory(realdb, monkeypatch):
    """Bind `control_session_factory` to this test's loop + slot.

    Same reason as `test_supplier_chat.py`: the mention POST path reaches
    control-plane users through the module-global factory (via `notify_event`),
    which otherwise points at another slot's engine on another loop.
    """
    monkeypatch.setattr("app.database.control_session_factory", realdb.control_sessionmaker())


async def _add_invoice(mk, org_id) -> uuid.UUID:
    iid = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=iid,
                organization_id=org_id,
                invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                vendor_name="Acme Supplies",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.new,
            )
        )
        await s.commit()
    return iid


async def _add_user(realdb, *, full_name: str, role_name: str, is_active: bool = True) -> uuid.UUID:
    """Seed one control-plane user in tenant A holding `role_name`."""
    from app.utils.passwords import pwd_context

    info = realdb.info(TENANT)
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        role = (
            (
                await s.execute(
                    select(Role).where(
                        Role.name == role_name,
                        (Role.organization_id.is_(None)) | (Role.organization_id == info.org_id),
                    )
                )
            )
            .scalars()
            .first()
        )
        assert role is not None, role_name
        uid = uuid.uuid4()
        s.add(
            User(
                id=uid,
                email=f"{uuid.uuid4().hex[:10]}@mentionable.test",
                full_name=full_name,
                hashed_password=pwd_context.hash("Passw0rd!xyz"),
                is_active=is_active,
                organization_id=info.org_id,
                must_change_password=False,
            )
        )
        await s.flush()
        s.add(UserRole(user_id=uid, role_id=role.id))
        await s.commit()
    return uid


# --------------------------------------------------------------------------
# The list
# --------------------------------------------------------------------------


async def test_every_employee_role_can_read_the_roster(realdb):
    """The gate mirrors `POST /api/invoices/{id}/chat`, which is
    `get_current_user` — every authenticated employee can post a mention, so
    every one of them must be able to see who. A narrower read gate is the bug
    this endpoint replaces, not a hardening of it."""
    seeded = realdb.info(TENANT).users
    for role in ("admin", "ap_manager", "ap_clerk", "cfo"):
        async with realdb.client(key=TENANT, role=role) as c:
            resp = await c.get(PATH)
        assert resp.status_code == 200, f"{role}: {resp.text}"
        # Keyed on ids, never `full_name`: the harness's role users are
        # control-plane rows shared by every test in this slot, and the realdb
        # fixture truncates tenant data between tests but not those. Any earlier
        # test that renames one (test_admin_user_management does exactly that)
        # would otherwise make this fail on test ORDER rather than on behaviour.
        offered_ids = {u["id"] for u in resp.json()}
        expected = {str(seeded[r]) for r in ("admin", "ap_manager", "ap_clerk", "cfo")}
        assert offered_ids >= expected, f"{role}: missing {expected - offered_ids}"


async def test_a_clerk_is_offered(realdb):
    """The one thing that makes this endpoint different from
    `assignable-reviewers`: a clerk holds no `invoice.approve` and so is never
    an approver candidate, but is an ordinary person to mention."""
    clerk_id = str(realdb.info(TENANT).users["ap_clerk"])
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.get(PATH)
    assert resp.status_code == 200
    # By id — `full_name` is an ordinary mutable field an admin can PATCH.
    assert clerk_id in {u["id"] for u in resp.json()}


async def test_response_carries_no_email_or_other_pii(realdb):
    """The narrow shape IS the fix. The picker previously rendered each
    candidate's EMAIL under their name — a directory of every colleague's
    address rebuilt inside a chat composer."""
    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.get(PATH)
    assert resp.status_code == 200
    body = resp.json()
    assert body, "expected at least the seeded role users"
    for entry in body:
        assert set(entry) == {"id", "full_name", "is_active"}
    payload = json.dumps(body)
    assert "@" not in payload
    for leaked in ("email", "roles", "last_login", "created_at", "permissions"):
        assert leaked not in payload


async def test_inactive_users_are_excluded(realdb):
    await _add_user(realdb, full_name="Retired Colleague", role_name="ap_clerk", is_active=False)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.get(PATH)
    assert resp.status_code == 200
    assert "Retired Colleague" not in {u["full_name"] for u in resp.json()}


async def test_requires_auth(realdb):
    async with realdb.client(key=TENANT, role=None) as c:
        resp = await c.get(PATH)
    assert resp.status_code == 401


async def test_scoped_to_the_callers_org(realdb):
    """Tenant isolation: `users` is control-plane, so an unscoped roster would
    hand this tenant every other tenant's employees."""
    other = realdb.info("b")
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.get(PATH)
    assert resp.status_code == 200
    ids = {u["id"] for u in resp.json()}
    assert not ids & {str(uid) for uid in other.users.values()}


async def test_literal_path_is_not_swallowed_by_the_invoice_id_route(realdb):
    """Route-ordering guard: `/{invoice_id}/chat` must not parse `chat` as an
    invoice id and `mentionable-users` as the literal segment."""
    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.get(PATH)
    assert resp.status_code != 422


# --------------------------------------------------------------------------
# The POST's matching check
# --------------------------------------------------------------------------


async def test_a_mention_from_the_roster_is_accepted(realdb):
    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    invoice_id = await _add_invoice(mk, info.org_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        offered = (await c.get(PATH)).json()
        clerk_id = str(realdb.info(TENANT).users["ap_clerk"])
        target = next(u for u in offered if u["id"] == clerk_id)
        resp = await c.post(
            f"/api/invoices/{invoice_id}/chat",
            json={"body": "Can you check this?", "mention_user_ids": [target["id"]]},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["mention_user_ids"] == [target["id"]]


async def test_a_foreign_org_user_cannot_be_mentioned(realdb):
    """`users` is control-plane, so a well-formed UUID from another tenant used
    to be persisted onto `mentions` verbatim and read back on every GET of the
    thread — a mention of someone who could never be notified."""
    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    invoice_id = await _add_invoice(mk, info.org_id)
    foreign = next(iter(realdb.info("b").users.values()))

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{invoice_id}/chat",
            json={"body": "Cross-tenant mention.", "mention_user_ids": [str(foreign)]},
        )
    assert resp.status_code == 400, resp.text

    # And nothing was written: the refusal happens before the thread is created.
    async with mk() as s:
        assert (
            await s.execute(
                select(SupplierChatThread).where(SupplierChatThread.invoice_id == invoice_id)
            )
        ).scalar_one_or_none() is None


async def test_an_unknown_uuid_cannot_be_mentioned(realdb):
    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    invoice_id = await _add_invoice(mk, info.org_id)

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{invoice_id}/chat",
            json={"body": "Ghost mention.", "mention_user_ids": [str(uuid.uuid4())]},
        )
    assert resp.status_code == 400, resp.text


async def test_a_deactivated_colleague_cannot_be_mentioned(realdb):
    """The roster excludes them, so the POST must too — otherwise the picker
    and the check disagree about who exists."""
    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    invoice_id = await _add_invoice(mk, info.org_id)
    retired = await _add_user(
        realdb, full_name="Left The Company", role_name="ap_clerk", is_active=False
    )

    async with realdb.client(key=TENANT, role="ap_manager") as c:
        resp = await c.post(
            f"/api/invoices/{invoice_id}/chat",
            json={"body": "Stale mention.", "mention_user_ids": [str(retired)]},
        )
    assert resp.status_code == 400, resp.text


async def test_a_message_with_no_mentions_still_posts(realdb):
    """The overwhelmingly common post carries no mention. The new check is
    guarded on a non-empty list, so it neither runs nor can refuse here."""
    mk = realdb.sessionmaker(TENANT)
    info = realdb.info(TENANT)
    invoice_id = await _add_invoice(mk, info.org_id)

    async with realdb.client(key=TENANT, role="ap_clerk") as c:
        resp = await c.post(f"/api/invoices/{invoice_id}/chat", json={"body": "No mentions here."})
    assert resp.status_code == 201, resp.text
    assert resp.json()["mention_user_ids"] == []

    async with mk() as s:
        stored = (await s.execute(select(SupplierChatMessage))).scalars().all()
        assert [m.mentions for m in stored] == [None]
