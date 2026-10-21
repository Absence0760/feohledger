"""The vendor, change-request, exception and credit-memo list rows carry the
caller's verdict on each row action, and it says exactly what the endpoint
answers (issue #575 § One rule, one place).

* change requests — `approve_refusal`: the queue compared only
  `requested_by_user_id` with the reader, so an approver whose provisioned
  portal login staged the change was offered an Approve that 403s;
* vendors — `status_actions.{verify,reject}`: the list hid Reject on an
  `active` vendor that `/reject` and bulk status both accept;
* exceptions — `decision_refusals.{resolve,dismiss,escalate}`: the queue
  checked only the status, so the uploader of a duplicate invoice was offered
  a Resolve that 403s (`segregation_implicated`);
* credit memos — `actions.{apply,edit,void}`: a restated copy of three gates
  (no drift today; now read, not copied).

The parity property, per role and row state: verdict `None` exactly when the
mutation succeeds, otherwise the code (coded detail) or the sentence (plain
detail) of the refusal it answers with. Both sides are asked.

Requires the dev Postgres (`pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.models.credit_memo import CreditMemo
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor
from app.models.vendor_change_request import VendorChangeRequest

TENANT = "a"
ROLES = ("admin", "ap_manager", "ap_clerk", "cfo")
pytestmark = pytest.mark.asyncio


def _assert_parity(verdict, resp, label):
    if verdict is None:
        assert resp.status_code in (200, 201), (label, resp.text)
        return
    assert resp.status_code in (403, 409), (label, verdict, resp.text)
    detail = resp.json()["detail"]
    if isinstance(detail, dict):
        assert detail["code"] == verdict["code"], (label, verdict, detail)
    else:
        assert detail == verdict["message"], (label, verdict, detail)


async def _vendor(realdb, *, status="active") -> uuid.UUID:
    vendor_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=realdb.info(TENANT).org_id,
                name=f"Verdict Vendor {vendor_id.hex[:8]}",
                status=status,
                source="manual",
            )
        )
        await s.commit()
    return vendor_id


async def _rows(realdb, role, path, params=None):
    async with realdb.client(key=TENANT, role=role) as c:
        resp = await c.get(path, params={"page_size": 100, **(params or {})})
    if resp.status_code == 403:
        return None
    assert resp.status_code == 200, resp.text
    return {item["id"]: item for item in resp.json()["items"]}


# ── Bank-change approve ────────────────────────────────────────────────────


async def _change_request(realdb, *, requested_by=None, provisioned_by=None, status="pending"):
    vendor_id = await _vendor(realdb)
    req_id = uuid.uuid4()
    async with realdb.sessionmaker(TENANT)() as s:
        s.add(
            VendorChangeRequest(
                id=req_id,
                vendor_id=vendor_id,
                organization_id=realdb.info(TENANT).org_id,
                change_type="bank_details",
                status=status,
                proposed_value={
                    "bank_details": {"account_number": "12345678", "bank_name": "Parity Bank"}
                },
                requested_by_user_id=requested_by,
                requested_by_vendor_user_id=None if requested_by else uuid.uuid4(),
                requester_provisioned_by_user_id=provisioned_by,
            )
        )
        await s.commit()
    return req_id


@pytest.mark.parametrize("role", ROLES)
async def test_the_change_request_verdict_matches_approve(realdb, role):
    me = realdb.info(TENANT).users[role]
    scenarios = {
        "third_party": {},
        "requested_by_caller": {"requested_by": me},
        "provisioned_by_caller": {"provisioned_by": me},
        "provisioned_by_someone_else": {"provisioned_by": uuid.uuid4()},
    }
    seen = set()
    for name, kwargs in scenarios.items():
        req_id = await _change_request(realdb, **kwargs)
        rows = await _rows(realdb, role, "/api/vendors/change-requests")
        async with realdb.client(key=TENANT, role=role) as c:
            resp = await c.post(f"/api/vendors/change-requests/{req_id}/approve", json={})
        if rows is None:
            assert resp.status_code == 403, (role, name, resp.text)
            continue
        verdict = rows[str(req_id)]["approve_refusal"]
        _assert_parity(verdict, resp, (role, name))
        seen.add(verdict and verdict["code"])
    if role in ("admin", "ap_manager"):
        assert seen == {
            None,
            "vendor_change_self_requested",
            "vendor_change_self_provisioned",
        }, seen


async def test_a_resolved_change_request_is_not_offered(realdb):
    req_id = await _change_request(realdb, status="approved")
    rows = await _rows(realdb, "admin", "/api/vendors/change-requests", {"status": "all"})
    verdict = rows[str(req_id)]["approve_refusal"]
    assert verdict["code"] == "vendor_change_not_pending"
    async with realdb.client(key=TENANT, role="admin") as c:
        resp = await c.post(f"/api/vendors/change-requests/{req_id}/approve", json={})
    _assert_parity(verdict, resp, "resolved")


# ── Vendor verify / reject ─────────────────────────────────────────────────


@pytest.mark.parametrize("role", ROLES)
async def test_the_vendor_status_verdicts_match_verify_and_reject(realdb, role):
    for status in ("unverified", "active", "rejected", "inactive"):
        for action in ("verify", "reject"):
            vendor_id = await _vendor(realdb, status=status)
            rows = await _rows(realdb, role, "/api/vendors")
            async with realdb.client(key=TENANT, role=role) as c:
                resp = await c.post(f"/api/vendors/{vendor_id}/{action}")
            if rows is None:
                assert resp.status_code == 403, (role, status, action, resp.text)
                continue
            row = rows.get(str(vendor_id))
            assert row is not None, "vendor not on the first page"
            _assert_parity(row["status_actions"][action], resp, (role, status, action))


async def test_an_active_vendor_may_be_rejected_from_its_row(realdb):
    """The drift this closes: the page offered Reject only on `unverified`."""
    vendor_id = await _vendor(realdb, status="active")
    rows = await _rows(realdb, "ap_manager", "/api/vendors")
    assert rows[str(vendor_id)]["status_actions"]["reject"] is None


# ── Exception resolve / dismiss / escalate ────────────────────────────────


async def _exception(realdb, *, exception_type, uploaded_by=None, raised_by=None, status="open"):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    inv_id = uuid.uuid4()
    exc_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number=f"EXV-{inv_id.hex[:8]}",
                vendor_name="Exception Vendor",
                amount=Decimal("100.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                organization_id=org_id,
                uploaded_by_id=uploaded_by,
                correlation_id=uuid.uuid4(),
            )
        )
        await s.flush()
        s.add(
            APException(
                id=exc_id,
                invoice_id=inv_id,
                exception_type=exception_type,
                severity="error",
                status=status,
                organization_id=org_id,
                raised_by_user_id=raised_by,
            )
        )
        await s.commit()
    return exc_id


@pytest.mark.parametrize("role", ROLES)
async def test_the_exception_verdicts_match_resolve(realdb, role):
    me = realdb.info(TENANT).users[role]
    scenarios = {
        "blocking_clean": {"exception_type": "duplicate"},
        "blocking_uploaded_by_caller": {"exception_type": "duplicate", "uploaded_by": me},
        "blocking_raised_by_caller": {"exception_type": "fraud_flag", "raised_by": me},
        "non_blocking_uploaded_by_caller": {"exception_type": "missing_data", "uploaded_by": me},
        "resolved": {"exception_type": "duplicate", "status": "resolved"},
    }
    seen = set()
    for name, kwargs in scenarios.items():
        for action in ("resolve", "dismiss", "escalate"):
            exc_id = await _exception(realdb, **kwargs)
            rows = await _rows(realdb, role, "/api/exceptions", {"status": kwargs.get("status")})
            async with realdb.client(key=TENANT, role=role) as c:
                resp = await c.post(
                    f"/api/exceptions/{exc_id}/resolve",
                    json={"action": action, "resolution": "parity"},
                )
            if rows is None:
                assert resp.status_code == 403, (role, name, action, resp.text)
                continue
            verdict = rows[str(exc_id)]["decision_refusals"][action]
            _assert_parity(verdict, resp, (role, name, action))
            seen.add(verdict and verdict["code"])
    if role in ("admin", "ap_manager"):
        assert seen == {
            None,
            "segregation_implicated",
            "segregation_raiser",
            "exception_not_actionable",
        }, seen


# ── Credit memo apply / edit / void ───────────────────────────────────────


async def _memo(realdb, *, status="open"):
    mk = realdb.sessionmaker(TENANT)
    org_id = realdb.info(TENANT).org_id
    vendor_id = await _vendor(realdb)
    inv_id = uuid.uuid4()
    memo_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                invoice_number=f"CMV-{inv_id.hex[:8]}",
                vendor_name="Memo Vendor",
                vendor_id=vendor_id,
                amount=Decimal("500.00"),
                currency="USD",
                status=InvoiceStatus.approved,
                organization_id=org_id,
                correlation_id=uuid.uuid4(),
            )
        )
        await s.flush()
        s.add(
            CreditMemo(
                id=memo_id,
                memo_number=f"CM-{memo_id.hex[:8]}",
                vendor_id=vendor_id,
                amount=Decimal("50.00"),
                currency="USD",
                status=status,
                organization_id=org_id,
            )
        )
        await s.commit()
    return memo_id, inv_id


@pytest.mark.parametrize("role", ROLES)
async def test_the_credit_memo_verdicts_match_apply_edit_and_void(realdb, role):
    for status in ("open", "applied", "void"):
        for action in ("apply", "edit", "void"):
            memo_id, inv_id = await _memo(realdb, status=status)
            rows = await _rows(realdb, role, "/api/credit-memos")
            async with realdb.client(key=TENANT, role=role) as c:
                if action == "apply":
                    resp = await c.post(
                        f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": str(inv_id)}
                    )
                elif action == "edit":
                    resp = await c.patch(f"/api/credit-memos/{memo_id}", json={"reason": "x"})
                else:
                    resp = await c.post(f"/api/credit-memos/{memo_id}/void")
            assert rows is not None
            verdict = rows[str(memo_id)]["actions"][action]
            _assert_parity(verdict, resp, (role, status, action))
