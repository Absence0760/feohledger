"""A second factor changing hands is a security event, so it goes on the trail.

`test_mfa_enrollment_security.py` pins the *enrollment contract* (what a caller
must prove before a factor moves). This file pins the *evidence*: that a factor
which actually took effect leaves an audit row behind it, on both surfaces.

Until now only step-up **failures** audited. That was exactly the wrong way
round. A wrong password against a factor change is a signal; a second factor
successfully added, replaced or removed on an account that can approve invoices
or stage a bank-detail change is the thing an incident responder needs to be
able to see at all — and neither `api/auth.enroll_mfa_verify` nor
`portal_auth.portal_mfa_verify` recorded it, so a stolen session that completed
a step-up left no trace of the factor it swapped in.

The counterweight is the PII rule: an MFA row must never carry the material it
is about. Every assertion below checks the row's `details` against the
credential that produced it — the TOTP secret, the submitted code, the passkey
public key — because an append-only trail that is also shipped to a WORM store
is the worst possible place to leak one.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pyotp
import pytest

from app.services import mfa as mfa_service

# ---------------------------------------------------------------------------
# Employee surface (`app/api/auth.py`) — control-plane account
# ---------------------------------------------------------------------------


def _fake_user(*, mfa_secret: str | None = None, mfa_enabled: bool = False):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="user@acme.test",
        full_name="Test User",
        organization_id=uuid.uuid4(),
        is_active=True,
        hashed_password=(
            "$bcrypt-sha256$v=2,t=2b,r=12$Jl.B.u9pD6kCuDNdO0nFfu$cK3Jg2DYkqzysEPJ0Q1opQpBVRQtyka"
        ),
        mfa_secret=mfa_secret,
        mfa_enabled=mfa_enabled,
        mfa_enrolled_at=None,
        must_change_password=False,
        delegate_to_id=None,
        delegate_until=None,
        locale=None,
        roles=[],
    )


def _db_returning(*rows):
    """Control session whose successive `execute(...)` calls answer `rows`.

    Each element is consumed by whichever accessor the endpoint reaches for —
    `scalar_one_or_none()` (the org lookup) or `scalars().all()` (the passkey
    lookup) — so one helper serves both shapes.
    """
    seq = iter(rows)

    def _execute(*_a, **_k):
        try:
            value = next(seq)
        except StopIteration:
            value = None
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=value)
        result.scalars.return_value.all.return_value = value if isinstance(value, list) else []
        return result

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    return db


@pytest.fixture
def audit(monkeypatch):
    """Capture what `api/auth.py` hands the auth-audit dispatcher.

    The dispatcher itself is exercised end to end by
    `test_totp_enrollment_row_lands_in_the_tenant_audit_log` below; here the
    interest is which action + details each endpoint asks it to record.
    """
    spy = AsyncMock()
    monkeypatch.setattr("app.api.auth.dispatch_auth_audit", spy)
    return spy


@pytest.fixture
def portal_audit(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr("app.api.portal_auth.dispatch_auth_audit", spy)
    return spy


def _rows(spy) -> list[dict]:
    return [call.kwargs for call in spy.await_args_list]


def _actions(spy) -> list[str]:
    return [row["action"] for row in _rows(spy)]


@pytest.mark.asyncio
async def test_first_totp_enrollment_is_audited(audit):
    """The row lands, names the factor, and says this was a first enrollment."""
    from app.api.auth import enroll_mfa_verify
    from app.schemas.auth import MFAEnrollVerifyRequest

    secret = pyotp.random_base32()
    user = _fake_user(mfa_secret=None, mfa_enabled=False)
    await mfa_service.stash_pending_totp_secret(user.id, secret)
    code = pyotp.TOTP(secret).now()

    with patch("app.api.auth.settings.mfa_enabled", True):
        await enroll_mfa_verify(
            body=MFAEnrollVerifyRequest(code=code), user=user, db=_db_returning(None)
        )

    assert _actions(audit) == ["auth.mfa.enrolled"]
    row = _rows(audit)[0]
    assert row["organization_id"] == user.organization_id
    assert row["actor_id"] == user.id
    assert row["entity_id"] == user.id
    assert row["details"] == {"factor": "totp", "replaced": False}
    # The credential the row is about must never be in the row.
    assert secret not in repr(row)
    assert code not in repr(row)


@pytest.mark.asyncio
async def test_re_enrollment_over_a_live_factor_records_that_it_replaced_one(audit):
    """`replaced` is the distinction that matters to an incident responder: a
    fresh account enrolling for the first time is routine, a live authenticator
    being swapped out from under one is not. It is read BEFORE the write —
    afterwards every account looks freshly enrolled."""
    from app.api.auth import enroll_mfa_verify
    from app.schemas.auth import MFAEnrollVerifyRequest

    old_secret = pyotp.random_base32()
    new_secret = pyotp.random_base32()
    user = _fake_user(mfa_secret=old_secret, mfa_enabled=True)
    await mfa_service.stash_pending_totp_secret(user.id, new_secret)

    with patch("app.api.auth.settings.mfa_enabled", True):
        await enroll_mfa_verify(
            body=MFAEnrollVerifyRequest(code=pyotp.TOTP(new_secret).now()),
            user=user,
            db=_db_returning(None),
        )

    assert _rows(audit)[0]["details"] == {"factor": "totp", "replaced": True}
    assert old_secret not in repr(_rows(audit))
    assert new_secret not in repr(_rows(audit))


@pytest.mark.asyncio
async def test_a_failed_enrollment_verify_writes_no_enrolled_row(audit):
    """The trail must record factors that took effect, not attempts. A wrong
    code leaves `mfa_secret` untouched, so an `auth.mfa.enrolled` row here
    would assert a factor change that never happened."""
    from fastapi import HTTPException

    from app.api.auth import enroll_mfa_verify
    from app.schemas.auth import MFAEnrollVerifyRequest

    user = _fake_user(mfa_secret=None, mfa_enabled=False)
    await mfa_service.stash_pending_totp_secret(user.id, pyotp.random_base32())

    with patch("app.api.auth.settings.mfa_enabled", True):
        with pytest.raises(HTTPException) as exc:
            await enroll_mfa_verify(
                body=MFAEnrollVerifyRequest(code="000000"), user=user, db=_db_returning(None)
            )

    assert exc.value.status_code == 400
    assert "auth.mfa.enrolled" not in _actions(audit)


@pytest.mark.asyncio
async def test_disabling_totp_is_audited(audit):
    """Stripping a factor is at least as audit-worthy as adding one — it is
    the half of the lifecycle an attacker with a stolen session wants."""
    from app.api.auth import disable_mfa
    from app.schemas.auth import MFADisableRequest

    secret = pyotp.random_base32()
    user = _fake_user(mfa_secret=secret, mfa_enabled=True)
    org = SimpleNamespace(id=user.organization_id, settings={}, name="Acme")
    code = pyotp.TOTP(secret).now()

    with patch("app.api.auth.settings.mfa_enabled", True):
        await disable_mfa(
            body=MFADisableRequest(code=code), user=user, db=_db_returning([], org, org)
        )

    assert user.mfa_enabled is False
    assert _actions(audit) == ["auth.mfa.disabled"]
    row = _rows(audit)[0]
    assert row["actor_id"] == user.id
    assert row["details"] == {"factor": "totp"}
    assert secret not in repr(row)
    assert code not in repr(row)


@pytest.mark.asyncio
async def test_passkey_registration_audit_names_the_credential(audit):
    """Passkey registration already audited; the row now carries the same
    `factor` vocabulary as TOTP and the API-visible credential id, so the
    registration row and the removal row that later retires it name the same
    object. The COSE public key never appears."""
    from app.api.auth import passkey_register_finish
    from app.models.webauthn_credential import WebAuthnCredential
    from app.schemas.auth import WebAuthnRegisterFinishRequest

    user = _fake_user()
    public_key = "COSE-PUBLIC-KEY-MATERIAL"
    fields = {
        "credential_id": "authenticator-handle",
        "public_key": public_key,
        "sign_count": 0,
        "transports": "internal",
        "rp_id": "localhost",
    }
    added: list[WebAuthnCredential] = []
    db = _db_returning(None)
    db.add = MagicMock(side_effect=added.append)

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = None
        obj.last_used_at = None

    db.refresh = AsyncMock(side_effect=_refresh)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth.webauthn.finish_registration", AsyncMock(return_value=fields)),
    ):
        await passkey_register_finish(
            body=WebAuthnRegisterFinishRequest(credential={}, name="Work laptop"),
            user=user,
            db=db,
        )

    assert _actions(audit) == ["auth.mfa.passkey.registered"]
    row = _rows(audit)[0]
    assert row["details"]["factor"] == "passkey"
    assert row["details"]["credential"] == str(added[0].id)
    assert row["details"]["rp_id"] == "localhost"
    assert public_key not in repr(row)
    assert fields["credential_id"] not in repr(row), (
        "the authenticator's own credential handle is a login identifier — the "
        "trail names the API-visible passkey id instead"
    )


@pytest.mark.asyncio
async def test_passkey_removal_audit_names_the_same_credential(audit):
    """The removal row is joinable to the registration row by `credential`,
    which is what makes "who took this factor off, and when" answerable."""
    from app.api.auth import passkey_delete

    user = _fake_user()
    cred = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Work laptop",
        rp_id="localhost",
        credential_id="authenticator-handle",
    )
    org = SimpleNamespace(id=user.organization_id, settings={}, name="Acme")
    db = _db_returning(cred, org, org)

    with (
        patch("app.api.auth.settings.mfa_enabled", True),
        patch("app.api.auth._require_mfa_step_up", AsyncMock()),
    ):
        await passkey_delete(credential_id=str(cred.id), user=user, db=db)

    assert _actions(audit) == ["auth.mfa.passkey.removed"]
    assert _rows(audit)[0]["details"] == {
        "factor": "passkey",
        "credential": str(cred.id),
        "name": "Work laptop",
    }


# ---------------------------------------------------------------------------
# Supplier portal (`app/api/portal_auth.py`) — tenant-scoped VendorUser
# ---------------------------------------------------------------------------


def _vendor_user(**overrides):
    base = dict(
        id=uuid.uuid4(),
        vendor_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="supplier@vendor.test",
        full_name="Supplier Contact",
        hashed_password="hash",
        must_change_password=False,
        is_active=True,
        mfa_secret=None,
        mfa_enabled=False,
        mfa_enrolled_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _portal_db(vendor=None):
    result = MagicMock()
    result.scalar_one_or_none.return_value = vendor
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_portal_totp_enrollment_is_audited(portal_audit, monkeypatch):
    """The supplier surface audits on exactly the same terms as the employee
    one — a portal account can stage a bank-detail change, so its second factor
    is not a preference."""
    from app.api.portal_auth import portal_mfa_verify
    from app.config import settings
    from app.schemas.portal import PortalMFAVerifyRequest

    monkeypatch.setattr(settings, "mfa_enabled", True)
    secret = pyotp.random_base32()
    vu = _vendor_user()
    await mfa_service.stash_pending_vendor_totp_secret(vu.id, secret)
    code = pyotp.TOTP(secret).now()

    await portal_mfa_verify(
        body=PortalMFAVerifyRequest(code=code),
        vu=vu,
        db=_portal_db(SimpleNamespace(name="V", status="verified")),
    )

    assert _actions(portal_audit) == ["portal.mfa.enrolled"]
    row = _rows(portal_audit)[0]
    assert row["organization_id"] == vu.organization_id
    assert row["actor_id"] == vu.id
    assert row["entity_id"] == vu.id
    assert row["details"] == {"factor": "totp", "replaced": False}
    assert secret not in repr(row)
    assert code not in repr(row)
    assert vu.email not in repr(row), "the supplier contact's address is third-party PII"


@pytest.mark.asyncio
async def test_portal_re_enrollment_records_that_it_replaced_a_live_factor(
    portal_audit, monkeypatch
):
    from app.api.portal_auth import portal_mfa_verify
    from app.config import settings
    from app.schemas.portal import PortalMFAVerifyRequest

    monkeypatch.setattr(settings, "mfa_enabled", True)
    new_secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=pyotp.random_base32(), mfa_enabled=True)
    await mfa_service.stash_pending_vendor_totp_secret(vu.id, new_secret)

    await portal_mfa_verify(
        body=PortalMFAVerifyRequest(code=pyotp.TOTP(new_secret).now()),
        vu=vu,
        db=_portal_db(SimpleNamespace(name="V", status="verified")),
    )

    assert _rows(portal_audit)[0]["details"] == {"factor": "totp", "replaced": True}


@pytest.mark.asyncio
async def test_portal_mfa_disable_is_audited(portal_audit, monkeypatch):
    from app.api.portal_auth import portal_mfa_disable
    from app.config import settings
    from app.schemas.portal import PortalMFADisableRequest

    monkeypatch.setattr(settings, "mfa_enabled", True)
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True)
    code = pyotp.TOTP(secret).now()

    await portal_mfa_disable(
        body=PortalMFADisableRequest(code=code),
        vu=vu,
        db=_portal_db(SimpleNamespace(name="V", status="verified")),
    )

    assert vu.mfa_enabled is False
    assert _actions(portal_audit) == ["portal.mfa.disabled"]
    assert _rows(portal_audit)[0]["details"] == {"factor": "totp"}
    assert secret not in repr(_rows(portal_audit))
    assert code not in repr(_rows(portal_audit))


@pytest.mark.asyncio
async def test_a_legacy_vendor_user_without_an_organization_is_skipped(portal_audit, monkeypatch):
    """`dispatch_auth_audit` resolves the tenant DB from `organization_id`, so
    a legacy row without one has nowhere to write. The endpoint must still
    succeed rather than 500 on an audit gap."""
    from app.api.portal_auth import portal_mfa_disable
    from app.config import settings
    from app.schemas.portal import PortalMFADisableRequest

    monkeypatch.setattr(settings, "mfa_enabled", True)
    secret = pyotp.random_base32()
    vu = _vendor_user(mfa_secret=secret, mfa_enabled=True, organization_id=None)

    await portal_mfa_disable(
        body=PortalMFADisableRequest(code=pyotp.TOTP(secret).now()),
        vu=vu,
        db=_portal_db(SimpleNamespace(name="V", status="verified")),
    )

    assert vu.mfa_enabled is False
    portal_audit.assert_not_awaited()


# ---------------------------------------------------------------------------
# End to end — the row reaches the tenant `audit_log`, not just the dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_totp_enrollment_row_lands_in_the_tenant_audit_log(realdb, monkeypatch):
    """The unit tests above stop at the dispatcher. This one runs the real
    enrollment over HTTP against a real tenant and reads the row back out of
    `audit_log`, because "we called the dispatcher" is not the same claim as
    "an auditor can find this". Auth endpoints run on the CONTROL-plane session
    while `audit_log` is tenant-scoped, so the row only exists if
    `dispatch_auth_audit` resolved the tenant DB and committed there.
    """
    from sqlalchemy import select

    from app.config import settings
    from app.models.workflow import AuditLog

    monkeypatch.setattr(settings, "mfa_enabled", True)

    async with realdb.client(key="a", role="admin") as client:
        start = await client.post("/api/auth/mfa/enroll", json={})
        assert start.status_code == 200, start.text
        secret = start.json()["secret"]

        verify = await client.post(
            "/api/auth/mfa/enroll/verify", json={"code": pyotp.TOTP(secret).now()}
        )
        assert verify.status_code == 200, verify.text
        assert verify.json()["mfa_enabled"] is True

    async with realdb.sessionmaker("a")() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "auth.mfa.enrolled")))
            .scalars()
            .all()
        )

    assert len(rows) == 1, "a completed enrollment left no row on the tenant audit trail"
    row = rows[0]
    assert row.entity_type == "auth"
    assert row.actor_id == realdb.info("a").users["admin"], (
        "the row must name the account whose factor changed"
    )
    assert row.entity_id == row.actor_id
    assert row.organization_id == realdb.info("a").org_id
    assert row.details == {"factor": "totp", "replaced": False}
    assert secret not in repr(row.details), "the TOTP secret must never reach the trail"
