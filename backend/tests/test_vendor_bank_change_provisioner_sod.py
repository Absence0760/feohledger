"""The BEC bank-redirect dual control, against the actor who can defeat it.

The attack this file exists to keep closed, verified link by link before it was
fixed:

1. ``POST /api/vendors/{id}/portal-users`` is ``require_roles(ADMIN, AP_MANAGER)``
   and used to return the plaintext ``temp_password`` for a **caller-supplied**
   address — so one AP user could mint a supplier login they controlled. (It
   now mails a set-password link to that address, which the same AP user can
   still redeem when the address is theirs — hence the stamp below.)
2. Signing in as that identity and staging a bank change fills
   ``requested_by_vendor_user_id`` and leaves ``requested_by_user_id`` NULL.
3. ``approve_change_request`` compared only ``requested_by_user_id``, so a NULL
   short-circuited the segregation check entirely.
4. ``ROLE_AP_MANAGER``'s default permission set holds ``vendor.manage``,
   ``vendor.bank_change.approve`` **and** ``payment.execute``.

One person, four requests, money redirected — with a "dual control" that never
fired. The fix records the AP actor on ``VendorUser.provisioned_by_user_id``,
freezes it onto the change request at staging, and refuses approval by that
actor; the credential now leaves only by email.

Real HTTP + real Postgres via ``realdb``: the whole point is that the NULL
short-circuit was a data-layer fact, and only a round-trip proves it is gone.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.api.deps import create_vendor_access_token
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest
from app.models.vendor_user import VendorUser
from tests.set_password_mail import link_token
from tests.test_session_management import FakeRedis

TENANT = "a"

NEW_BANK = {"account_number": "12345678", "bank_name": "Attacker Bank"}


@pytest.fixture
def mk(realdb):
    return realdb.sessionmaker(TENANT)


@pytest.fixture
def fake_redis(monkeypatch):
    """A real portal login / password change goes through
    `session_management.register_session` + `revoke_other_sessions`, which need
    the zset+hash Redis stand-in — the autouse key/value stub has no `zadd`.
    Same fixture the reset-password suite uses."""
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.redis.get_redis", _get_redis)
    return fake


@pytest.fixture
def sent_mail(monkeypatch):
    """Capture what the (dev-default `console`) email adapter would send.

    Doubles as the attacker's mailbox: the set-password link is never in the
    HTTP response, so a test that wants to *use* the credential has to read it
    where a real caller-supplied-address attacker would.
    """
    outbox: list = []

    async def _fake_send(self, message):  # noqa: ANN001
        outbox.append(message)

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    monkeypatch.setattr(ConsoleAdapter, "send", _fake_send, raising=True)
    return outbox


ATTACKER_PASSWORD = "Attacker-Chosen-Pass9"


async def _redeem(realdb, message, password: str = ATTACKER_PASSWORD) -> None:
    """Spend the mailed set-password link as its recipient would."""
    async with realdb.client(key=TENANT, role=None) as anon:
        resp = await anon.post(
            "/api/portal/auth/reset-password",
            json={"token": link_token(message), "new_password": password},
        )
    assert resp.status_code == 200, resp.text


async def _create_vendor(mk, org_id, *, name: str) -> uuid.UUID:
    vendor_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=org_id,
                name=name,
                status="active",
                source="manual",
            )
        )
        await s.commit()
    return vendor_id


async def _invite(realdb, vendor_id: uuid.UUID, *, role: str, email: str) -> dict:
    async with realdb.client(key=TENANT, role=role) as client:
        resp = await client.post(
            f"/api/vendors/{vendor_id}/portal-users",
            json={"email": email, "full_name": "Portal Contact"},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _portal_client(realdb, vendor_user_id: uuid.UUID, vendor_id: uuid.UUID):
    client = realdb.client(key=TENANT, role=None)
    token = create_vendor_access_token(vendor_user_id, vendor_id)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _stage_bank_change(realdb, vendor_user_id: uuid.UUID, vendor_id: uuid.UUID) -> None:
    async with _portal_client(realdb, vendor_user_id, vendor_id) as portal:
        resp = await portal.post("/api/portal/company/bank-change", json={"bank_details": NEW_BANK})
    assert resp.status_code == 202, resp.text


async def _pending_request(mk, vendor_id: uuid.UUID) -> VendorChangeRequest:
    async with mk() as s:
        return (
            await s.execute(
                select(VendorChangeRequest).where(VendorChangeRequest.vendor_id == vendor_id)
            )
        ).scalar_one()


# ---------------------------------------------------------------------------
# The attack chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provisioner_cannot_approve_the_bank_change_they_engineered(
    realdb, mk, sent_mail, fake_redis
):
    """End to end, as the attacker would run it: one `ap_manager` provisions a
    portal identity, uses the emailed credential, stages a bank redirect, and
    is refused at approval. The vendor's bank details must not move."""
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _create_vendor(mk, org_id, name="BEC Target Co")
    email = f"attacker-{uuid.uuid4().hex[:8]}@mailbox.test"

    invited = await _invite(realdb, vendor_id, role="ap_manager", email=email)
    vendor_user_id = uuid.UUID(invited["user"]["id"])

    # The attacker really can take the identity over — the set-password link
    # lands in the mailbox they chose, though the response never carries it.
    assert len(sent_mail) == 1
    await _redeem(realdb, sent_mail[0])
    async with realdb.client(key=TENANT, role=None) as anon:
        login = await anon.post(
            "/api/portal/auth/login",
            json={"email": email, "password": ATTACKER_PASSWORD},
        )
    assert login.status_code == 200, login.text

    await _stage_bank_change(realdb, vendor_user_id, vendor_id)
    req = await _pending_request(mk, vendor_id)
    # The NULL that used to short-circuit the check is still NULL — the fix
    # does not work by pretending a portal request is AP-initiated.
    assert req.requested_by_user_id is None
    assert req.requester_provisioned_by_user_id == realdb.info(TENANT).users["ap_manager"]

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        approve = await client.post(f"/api/vendors/change-requests/{req.id}/approve")
    assert approve.status_code == 403, approve.text
    # PII guard: the refusal names the control, never the supplier or the bank.
    assert NEW_BANK["account_number"] not in approve.text
    assert email not in approve.text

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert not (vendor.bank_details or {}).get("account_number")
        still_pending = (
            await s.execute(select(VendorChangeRequest).where(VendorChangeRequest.id == req.id))
        ).scalar_one()
        assert still_pending.status == "pending"


@pytest.mark.asyncio
async def test_a_different_approver_can_still_approve_it(realdb, mk, sent_mail):
    """Dual control, not a dead end: the request the provisioner may not
    approve is approved normally by a second person."""
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _create_vendor(mk, org_id, name="Second Approver Co")
    invited = await _invite(
        realdb,
        vendor_id,
        role="ap_manager",
        email=f"contact-{uuid.uuid4().hex[:8]}@supplier.test",
    )
    await _stage_bank_change(realdb, uuid.UUID(invited["user"]["id"]), vendor_id)
    req = await _pending_request(mk, vendor_id)

    async with realdb.client(key=TENANT, role="admin") as client:
        approve = await client.post(f"/api/vendors/change-requests/{req.id}/approve")
    assert approve.status_code == 200, approve.text

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert vendor.bank_details["account_number"] == NEW_BANK["account_number"]


@pytest.mark.asyncio
async def test_a_supplier_holding_their_own_credential_is_untouched(realdb, mk):
    """The common, genuine case: a portal identity nobody in AP provisioned
    (NULL provisioner) stages a change and any approver may approve it. The fix
    must not turn every supplier-initiated bank change into a refusal."""
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _create_vendor(mk, org_id, name="Self Managed Supplier")
    vendor_user_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            VendorUser(
                id=vendor_user_id,
                vendor_id=vendor_id,
                organization_id=org_id,
                email=f"self-{uuid.uuid4().hex[:8]}@supplier.test",
                full_name="Self Managed",
                hashed_password="x",
                is_active=True,
            )
        )
        await s.commit()

    await _stage_bank_change(realdb, vendor_user_id, vendor_id)
    req = await _pending_request(mk, vendor_id)
    assert req.requester_provisioned_by_user_id is None

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        approve = await client.post(f"/api/vendors/change-requests/{req.id}/approve")
    assert approve.status_code == 200, approve.text

    async with mk() as s:
        vendor = (await s.execute(select(Vendor).where(Vendor.id == vendor_id))).scalar_one()
        assert vendor.bank_details["account_number"] == NEW_BANK["account_number"]


@pytest.mark.asyncio
async def test_deleting_the_portal_user_cannot_erase_the_evidence(realdb, mk, sent_mail):
    """`DELETE /portal-users/{id}` carries no FK into `vendor_change_requests`,
    so resolving the provisioner by join at approval time would let the
    approver delete the identity first and walk their own request through.
    The value is frozen onto the request at staging, so it survives."""
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _create_vendor(mk, org_id, name="Evidence Erasure Co")
    invited = await _invite(
        realdb,
        vendor_id,
        role="ap_manager",
        email=f"doomed-{uuid.uuid4().hex[:8]}@mailbox.test",
    )
    vendor_user_id = uuid.UUID(invited["user"]["id"])
    await _stage_bank_change(realdb, vendor_user_id, vendor_id)
    req = await _pending_request(mk, vendor_id)

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        deleted = await client.delete(f"/api/vendors/{vendor_id}/portal-users/{vendor_user_id}")
        assert deleted.status_code in (200, 204), deleted.text
        approve = await client.post(f"/api/vendors/change-requests/{req.id}/approve")
    assert approve.status_code == 403, approve.text


@pytest.mark.asyncio
async def test_admin_password_reset_stamps_the_resetting_actor(realdb, mk, sent_mail):
    """Reset is the OTHER way an AP actor can steer a supplier credential —
    it is how the attack runs against an identity that already exists — so it
    has to leave the same record invite does. A legacy row's NULL provisioner
    is only safe because taking it over goes through here."""
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _create_vendor(mk, org_id, name="Legacy Credential Co")
    vendor_user_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            VendorUser(
                id=vendor_user_id,
                vendor_id=vendor_id,
                organization_id=org_id,
                email=f"legacy-{uuid.uuid4().hex[:8]}@supplier.test",
                full_name="Legacy Supplier",
                hashed_password="x",
                is_active=True,
            )
        )
        await s.commit()

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        reset = await client.post(
            f"/api/vendors/{vendor_id}/portal-users/{vendor_user_id}/reset-password"
        )
    assert reset.status_code == 200, reset.text

    async with mk() as s:
        vu = (
            await s.execute(select(VendorUser).where(VendorUser.id == vendor_user_id))
        ).scalar_one()
        assert vu.provisioned_by_user_id == realdb.info(TENANT).users["ap_manager"]

    await _stage_bank_change(realdb, vendor_user_id, vendor_id)
    req = await _pending_request(mk, vendor_id)

    async with realdb.client(key=TENANT, role="ap_manager") as client:
        approve = await client.post(f"/api/vendors/change-requests/{req.id}/approve")
    assert approve.status_code == 403, approve.text


@pytest.mark.asyncio
async def test_supplier_changing_their_own_password_does_not_clear_the_stamp(
    realdb, mk, sent_mail, fake_redis
):
    """`POST /api/portal/auth/change-password` requires the CURRENT password —
    which the provisioner has, once they have spent the link they mailed to
    themselves. Clearing the stamp there would therefore be a
    one-request bypass of this whole control, so it is deliberately durable
    provenance rather than a "who knows the password right now" flag."""
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _create_vendor(mk, org_id, name="Self Change Co")
    email = f"selfchange-{uuid.uuid4().hex[:8]}@mailbox.test"
    invited = await _invite(realdb, vendor_id, role="ap_manager", email=email)
    vendor_user_id = uuid.UUID(invited["user"]["id"])
    await _redeem(realdb, sent_mail[0])

    async with _portal_client(realdb, vendor_user_id, vendor_id) as portal:
        changed = await portal.post(
            "/api/portal/auth/change-password",
            json={
                "current_password": ATTACKER_PASSWORD,
                "new_password": "Brand-New-Passphrase-1234",
            },
        )
    assert changed.status_code in (200, 204), changed.text

    async with mk() as s:
        vu = (
            await s.execute(select(VendorUser).where(VendorUser.id == vendor_user_id))
        ).scalar_one()
        assert vu.provisioned_by_user_id == realdb.info(TENANT).users["ap_manager"]

    await _stage_bank_change(realdb, vendor_user_id, vendor_id)
    req = await _pending_request(mk, vendor_id)
    async with realdb.client(key=TENANT, role="ap_manager") as client:
        approve = await client.post(f"/api/vendors/change-requests/{req.id}/approve")
    assert approve.status_code == 403, approve.text


# ---------------------------------------------------------------------------
# Credential delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_returns_no_plaintext_credential(realdb, mk, sent_mail):
    """The response must carry no credential for the caller-supplied address —
    the set-password link travels only on the email adapter."""
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _create_vendor(mk, org_id, name="No Echo Co")
    email = f"noecho-{uuid.uuid4().hex[:8]}@supplier.test"
    body = await _invite(realdb, vendor_id, role="admin", email=email)

    assert "temp_password" not in body
    assert set(body) <= {"user", "portal_url"}
    # And nothing password-shaped smuggled through another field.
    assert len(sent_mail) == 1
    assert link_token(sent_mail[0]) not in str(body)
    assert sent_mail[0].to == email


@pytest.mark.asyncio
async def test_invite_rolls_back_when_the_credential_cannot_be_delivered(realdb, mk, monkeypatch):
    """Email is the link's only channel, so a delivery failure must not leave a
    portal account nobody can sign in to — it 502s and creates nothing."""
    from app.services.email_adapters.console_adapter import ConsoleAdapter

    async def _boom(self, message):  # noqa: ANN001
        raise RuntimeError("smtp down")

    monkeypatch.setattr(ConsoleAdapter, "send", _boom, raising=True)

    org_id = realdb.info(TENANT).org_id
    vendor_id = await _create_vendor(mk, org_id, name="Undeliverable Co")
    email = f"undeliverable-{uuid.uuid4().hex[:8]}@supplier.test"

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(
            f"/api/vendors/{vendor_id}/portal-users",
            json={"email": email, "full_name": "Never Created"},
        )
    assert resp.status_code == 502, resp.text

    async with mk() as s:
        rows = (
            (await s.execute(select(VendorUser).where(VendorUser.email == email))).scalars().all()
        )
    assert rows == [], "a portal account must not survive an undelivered invite"


@pytest.mark.asyncio
async def test_reset_rolls_back_when_the_new_password_cannot_be_delivered(
    realdb, mk, sent_mail, monkeypatch
):
    """Same shape for reset, where the stakes are higher: a failed delivery
    must not clear a working supplier password for a link nobody received."""
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _create_vendor(mk, org_id, name="Reset Undeliverable Co")
    invited = await _invite(
        realdb,
        vendor_id,
        role="admin",
        email=f"resetfail-{uuid.uuid4().hex[:8]}@supplier.test",
    )
    vendor_user_id = uuid.UUID(invited["user"]["id"])
    await _redeem(realdb, sent_mail[0])
    async with mk() as s:
        before = (
            await s.execute(select(VendorUser).where(VendorUser.id == vendor_user_id))
        ).scalar_one()
        original_hash = before.hashed_password
    assert original_hash is not None

    from app.services.email_adapters.console_adapter import ConsoleAdapter

    async def _boom(self, message):  # noqa: ANN001
        raise RuntimeError("smtp down")

    monkeypatch.setattr(ConsoleAdapter, "send", _boom, raising=True)

    async with realdb.client(key=TENANT, role="admin") as client:
        resp = await client.post(
            f"/api/vendors/{vendor_id}/portal-users/{vendor_user_id}/reset-password"
        )
    assert resp.status_code == 502, resp.text

    async with mk() as s:
        after = (
            await s.execute(select(VendorUser).where(VendorUser.id == vendor_user_id))
        ).scalar_one()
        assert after.hashed_password == original_hash
