"""The unmasked-banking DSAR export demands a second-factor proof.

`POST /api/privacy/dsar` with `include_banking` already required the
`vendor.bank_change.approve` permission and a written justification
(`docs/decisions.md` §182). `ROLE_ADMIN` resolves to the whole permission
catalogue, so on the stock roles that admitted every admin: a stolen admin
session, or an admin's password alone, could produce a supplier's full account
number. The route now also calls `api/auth.require_sensitive_step_up`, which
accepts ONLY a current authenticator code or a passkey assertion — never the
password — and refuses, rather than exempts, an account with no second factor.

Two layers:

* the helper's decision table, driven directly (no DB), so each refusal code and
  the proof-kind it reports are pinned exactly;
* the route against a real tenant (`realdb`), with the MFA master switch on and
  a real TOTP secret on the admin, proving the gate runs BEFORE any subject data
  is read and that the audit row names the proof.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pyotp
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api import auth as auth_api
from app.models.data_subject_request import DataSubjectRequest
from app.models.user import User
from app.models.workflow import AuditLog
from app.schemas.auth import (
    STEP_UP_OPERATION_DSAR_UNMASKED,
    STEP_UP_OPERATIONS,
    MFAStepUpRequest,
    WebAuthnStepUpStartRequest,
)
from tests.test_privacy import _seed_vendor_with_records

OP = STEP_UP_OPERATION_DSAR_UNMASKED


def _user(*, totp: bool = True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        hashed_password="$bcrypt-sha256$irrelevant",
        mfa_enabled=totp,
        mfa_secret="JBSWY3DPEHPK3PXP" if totp else None,
    )


@pytest.fixture
def mfa_on(monkeypatch):
    monkeypatch.setattr(auth_api.settings, "mfa_enabled", True)
    monkeypatch.setattr(auth_api.settings, "environment", "development")


@pytest.fixture
def plumbing():
    """Patch the helper's collaborators; `satisfied` decides each proof."""
    with (
        patch.object(auth_api, "_user_passkeys", AsyncMock(return_value=[])) as passkeys,
        patch.object(auth_api, "_throttle_step_up", AsyncMock()) as throttle,
        patch.object(auth_api, "_relying_party", AsyncMock(return_value=object())),
        patch.object(auth_api, "_step_up_satisfied", AsyncMock(return_value=False)) as satisfied,
        patch.object(auth_api, "_audit_step_up_failure", AsyncMock()) as audit,
    ):
        yield SimpleNamespace(
            passkeys=passkeys, throttle=throttle, satisfied=satisfied, audit=audit
        )


def _code(exc: HTTPException) -> str:
    return exc.detail["code"]


async def _call(user, body):
    return await auth_api.require_sensitive_step_up(
        user, body, db=AsyncMock(), operation=OP, host=None
    )


# ---------------------------------------------------------------------------
# The decision table
# ---------------------------------------------------------------------------


async def test_mfa_off_locally_skips_the_gate_and_says_so(monkeypatch):
    monkeypatch.setattr(auth_api.settings, "mfa_enabled", False)
    monkeypatch.setattr(auth_api.settings, "environment", "development")
    assert await _call(_user(), None) == auth_api.SENSITIVE_PROOF_MFA_OFF_LOCAL


async def test_mfa_off_in_a_deployed_environment_fails_closed(monkeypatch):
    monkeypatch.setattr(auth_api.settings, "mfa_enabled", False)
    monkeypatch.setattr(auth_api.settings, "environment", "production")
    with pytest.raises(HTTPException) as exc:
        await _call(_user(), MFAStepUpRequest(code="123456"))
    assert exc.value.status_code == 403
    assert _code(exc.value) == "sensitive_step_up_unavailable"


async def test_an_account_with_no_second_factor_is_refused_not_exempted(mfa_on, plumbing):
    with pytest.raises(HTTPException) as exc:
        await _call(_user(totp=False), MFAStepUpRequest(password="hunter2-correct"))
    assert exc.value.status_code == 403
    assert _code(exc.value) == "sensitive_step_up_no_factor"
    plumbing.satisfied.assert_not_awaited()


async def test_a_passkey_alone_counts_as_a_second_factor(mfa_on, plumbing):
    plumbing.passkeys.return_value = [object()]
    with pytest.raises(HTTPException) as exc:
        await _call(_user(totp=False), None)
    assert _code(exc.value) == "sensitive_step_up_required"


@pytest.mark.parametrize(
    "body",
    [None, MFAStepUpRequest(), MFAStepUpRequest(password="the-real-password")],
    ids=["no-body", "empty", "password-only"],
)
async def test_no_factor_proof_is_a_prompt_not_a_failure(mfa_on, plumbing, body):
    """A password is not a second factor, so offering only one is the same as
    offering nothing — and neither burns the throttle or writes a failure."""
    with pytest.raises(HTTPException) as exc:
        await _call(_user(), body)
    assert exc.value.status_code == 403
    assert _code(exc.value) == "sensitive_step_up_required"
    plumbing.throttle.assert_not_awaited()
    plumbing.audit.assert_not_awaited()
    plumbing.satisfied.assert_not_awaited()


async def test_a_verified_code_reports_totp_and_never_forwards_the_password(mfa_on, plumbing):
    plumbing.satisfied.return_value = True
    proof = await _call(_user(), MFAStepUpRequest(code="123456", password="pw-not-a-factor"))
    assert proof == auth_api.SENSITIVE_PROOF_TOTP
    plumbing.throttle.assert_awaited_once()
    forwarded = plumbing.satisfied.await_args.args[2]
    assert forwarded.code == "123456"
    assert forwarded.password is None
    assert plumbing.satisfied.await_args.kwargs["operation"] == OP


async def test_a_verified_assertion_reports_passkey(mfa_on, plumbing):
    plumbing.satisfied.side_effect = lambda db, u, b, **k: b.assertion is not None
    proof = await _call(_user(), MFAStepUpRequest(code="000000", assertion={"id": "x"}))
    assert proof == auth_api.SENSITIVE_PROOF_PASSKEY
    # The code was tried first and failed; the assertion went on its own.
    first, second = (c.args[2] for c in plumbing.satisfied.await_args_list)
    assert (first.code, first.assertion) == ("000000", None)
    assert (second.code, second.assertion, second.password) == (None, {"id": "x"}, None)
    plumbing.audit.assert_not_awaited()


async def test_a_proof_that_does_not_verify_is_throttled_audited_and_refused(mfa_on, plumbing):
    with pytest.raises(HTTPException) as exc:
        await _call(_user(), MFAStepUpRequest(code="999999"))
    assert exc.value.status_code == 400
    assert _code(exc.value) == "sensitive_step_up_failed"
    plumbing.throttle.assert_awaited_once()
    plumbing.audit.assert_awaited_once()
    assert plumbing.audit.await_args.kwargs["operation"] == OP


def test_the_operation_can_mint_its_own_passkey_challenge():
    """The assertion is operation-bound, so the passkey step-up endpoint must
    accept this operation — and the tuple and the request pattern must agree."""
    assert OP in STEP_UP_OPERATIONS
    for op in STEP_UP_OPERATIONS:
        WebAuthnStepUpStartRequest(operation=op)
    with pytest.raises(ValueError):
        WebAuthnStepUpStartRequest(operation="dsar_unmasked_export_typo")


def test_every_refusal_carries_a_code_the_spa_localizes():
    codes = {
        auth_api.SENSITIVE_STEP_UP_REQUIRED_DETAIL["code"],
        auth_api.SENSITIVE_STEP_UP_FAILED_DETAIL["code"],
        auth_api.SENSITIVE_STEP_UP_NO_FACTOR_DETAIL["code"],
        auth_api.SENSITIVE_STEP_UP_UNAVAILABLE_DETAIL["code"],
    }
    # Pinned against `frontend/src/lib/api/authRefusals.ts` (its test pins the
    # same literals from the other side).
    assert codes == {
        "sensitive_step_up_required",
        "sensitive_step_up_failed",
        "sensitive_step_up_no_factor",
        "sensitive_step_up_unavailable",
    }


# ---------------------------------------------------------------------------
# The route, against a real tenant
# ---------------------------------------------------------------------------

SECRET = pyotp.random_base32()


@pytest.fixture
async def admin_with_totp(realdb, monkeypatch):
    monkeypatch.setattr(auth_api.settings, "mfa_enabled", True)
    monkeypatch.setattr(auth_api.settings, "environment", "development")
    ctrl_mk = realdb.control_sessionmaker()
    admin_id = realdb.info("a").users["admin"]
    async with ctrl_mk() as s:
        u = await s.get(User, admin_id)
        saved = (u.mfa_enabled, u.mfa_secret)
        u.mfa_enabled, u.mfa_secret = True, SECRET
        await s.commit()
    try:
        yield admin_id
    finally:
        # The control-plane database outlives the test; put the admin back.
        async with ctrl_mk() as s:
            u = await s.get(User, admin_id)
            u.mfa_enabled, u.mfa_secret = saved
            await s.commit()


def _unmasked(vendor_id, step_up=None) -> dict:
    body = {
        "subject_type": "vendor_contact",
        "identifier": str(vendor_id),
        "include_banking": True,
        "banking_justification": "Supplier asked for their own record, ticket AP-7",
    }
    if step_up is not None:
        body["step_up"] = step_up
    return body


async def _dsar_rows(tenant_mk, org_id):
    async with tenant_mk() as s:
        audits = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.organization_id == org_id,
                        AuditLog.action.like("privacy.dsar_export%"),
                    )
                )
            )
            .scalars()
            .all()
        )
        requests = (
            (
                await s.execute(
                    select(DataSubjectRequest).where(DataSubjectRequest.organization_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    return audits, requests


async def test_route_refuses_before_reading_anything_until_a_factor_is_proved(
    realdb, admin_with_totp
):
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor_with_records(tenant_mk, org_id, with_portal=False)

    async with realdb.client(key="a", role="admin") as c:
        missing = await c.post("/api/privacy/dsar", json=_unmasked(vendor_id))
        password = await c.post(
            "/api/privacy/dsar", json=_unmasked(vendor_id, {"password": "Demo-password-1"})
        )
        wrong = await c.post("/api/privacy/dsar", json=_unmasked(vendor_id, {"code": "000000"}))
        # A MASKED export is untouched by the gate.
        masked = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "vendor_contact", "identifier": str(vendor_id)},
        )

    assert missing.status_code == 403, missing.text
    assert missing.json()["detail"]["code"] == "sensitive_step_up_required"
    assert password.status_code == 403
    assert password.json()["detail"]["code"] == "sensitive_step_up_required"
    assert wrong.status_code == 400
    assert wrong.json()["detail"]["code"] == "sensitive_step_up_failed"
    # No refusal leaked a byte of the subject.
    for resp in (missing, password, wrong):
        assert "000111222" not in resp.text
    assert masked.status_code == 200, masked.text
    assert masked.json()["banking_disclosure"] == "masked"

    audits, requests = await _dsar_rows(tenant_mk, org_id)
    # No refused attempt produced an export or a request row. The proof that
    # failed is on the tenant trail as a refusal; the two that sent no factor
    # proof were the routine prompt and are not.
    assert sorted(a.action for a in audits) == [
        "privacy.dsar_export",
        "privacy.dsar_export.unmasked_refused",
    ]
    refused = next(a for a in audits if a.action.endswith("unmasked_refused"))
    assert refused.details == {
        "subject_type": "vendor_contact",
        "subject_id": str(vendor_id),
        "refusal": "sensitive_step_up_failed",
    }
    assert refused.entity_id == vendor_id
    assert len(requests) == 1


async def test_route_unmasks_on_a_current_code_and_records_the_proof(realdb, admin_with_totp):
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor_with_records(tenant_mk, org_id, with_portal=False)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json=_unmasked(vendor_id, {"code": pyotp.TOTP(SECRET).now()}),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["banking_disclosure"] == "unmasked"
    assert resp.json()["data"]["vendor"]["bank_details"]["account"] == "000111222"

    audits, _ = await _dsar_rows(tenant_mk, org_id)
    unmasked = [a for a in audits if a.action == "privacy.dsar_export.unmasked"]
    assert len(unmasked) == 1
    assert unmasked[0].details["step_up"] == "totp"


async def test_route_refuses_the_same_code_twice(realdb, admin_with_totp):
    """A code is single-use (`mfa.verify_totp` claims it in Redis), so one
    shoulder-surfed inside its 30-second window cannot authorize a second
    unmasked export."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor_with_records(tenant_mk, org_id, with_portal=False)
    code = pyotp.TOTP(SECRET).now()

    async with realdb.client(key="a", role="admin") as c:
        first = await c.post("/api/privacy/dsar", json=_unmasked(vendor_id, {"code": code}))
        again = await c.post("/api/privacy/dsar", json=_unmasked(vendor_id, {"code": code}))
    assert first.status_code == 200, first.text
    assert again.status_code == 400, again.text
    assert again.json()["detail"]["code"] == "sensitive_step_up_failed"
    assert "000111222" not in again.text


async def test_route_refuses_an_admin_with_no_second_factor(realdb, monkeypatch):
    monkeypatch.setattr(auth_api.settings, "mfa_enabled", True)
    monkeypatch.setattr(auth_api.settings, "environment", "development")
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor_with_records(tenant_mk, org_id, with_portal=False)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/privacy/dsar", json=_unmasked(vendor_id, {"code": "123456"}))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "sensitive_step_up_no_factor"
    audits, requests = await _dsar_rows(tenant_mk, org_id)
    assert [(a.action, a.details["refusal"]) for a in audits] == [
        ("privacy.dsar_export.unmasked_refused", "sensitive_step_up_no_factor")
    ]
    assert requests == []
