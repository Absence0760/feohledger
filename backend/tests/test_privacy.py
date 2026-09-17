"""GDPR / CCPA privacy endpoints — /api/privacy DSAR export + erasure.

Covers (against real Postgres via the ``realdb`` fixture):
  * DSAR bundle assembles the subject's PII + related rows (user / vendor_user /
    vendor_contact).
  * Banking fields are MASKED by default (#423); the unmasked variant needs
    `include_banking` + a justification + the `vendor.bank_change.approve`
    permission, and writes its own `privacy.dsar_export.unmasked` audit row.
  * The export returns DATA, not counts, for audit / notification activity, and
    reaches passkey metadata and the stored-document manifest (#424).
  * Erasure deletes the subject's passkey rows and revokes their live sessions
    through the existing `revoke_user_sessions` (#424).

The storage leg's retain/delete split has its own file,
``tests/test_privacy_documents.py``.
  * Erasure redacts every PII field, leaves Invoice/Payment money fields and the
    append-only audit_log untouched, and is idempotent.
  * Tenant isolation — a subject in tenant A is neither exported nor erased when
    acting as tenant B.
  * The request itself is audited + recorded PII-free in data_subject_requests.

RBAC (admin-only) is also enforced by test_rbac.py's coverage gate; here we add a
direct non-admin 403 check on the DSAR route.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.data_subject_request import DataSubjectRequest
from app.models.invoice import Invoice, InvoiceStatus
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.models.vendor_user import VendorUser
from app.models.workflow import AuditLog
from app.utils.passwords import pwd_context

# ---------------------------------------------------------------------------
# Helpers — seed a vendor (+ portal user, invoice, payment) in a tenant.
# ---------------------------------------------------------------------------


async def _seed_vendor_with_records(tenant_mk, org_id, *, with_portal=True):
    """Create a vendor with contact PII, one invoice, one payment, and
    (optionally) a portal user. Returns (vendor_id, vendor_user_id, invoice_id)."""
    vendor_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    vendor_user_id = uuid.uuid4()
    async with tenant_mk() as s:
        s.add(
            Vendor(
                id=vendor_id,
                organization_id=org_id,
                name="Acme Supplies Ltd",
                code="V-001",
                email="contact@acmesupplies.test",
                phone="+1-555-0100",
                address="123 Market St, Springfield",
                tax_id="12-3456789",
                bank_details={"account": "000111222", "routing": "021000021"},
                beneficial_owner_data={"owner": "Jane Doe"},
                status="active",
            )
        )
        s.add(
            Invoice(
                id=invoice_id,
                correlation_id=uuid.uuid4(),
                organization_id=org_id,
                invoice_number="INV-9001",
                vendor_name="Acme Supplies Ltd",
                vendor_id=vendor_id,
                amount=Decimal("4200.50"),
                currency="USD",
                status=InvoiceStatus.approved,
            )
        )
        await s.flush()  # ensure the invoice exists before its FK-dependent rows
        s.add(
            Payment(
                id=uuid.uuid4(),
                invoice_id=invoice_id,
                amount=Decimal("4200.50"),
                method="ach",
                status="completed",
            )
        )
        if with_portal:
            s.add(
                VendorUser(
                    id=vendor_user_id,
                    vendor_id=vendor_id,
                    email="portal@acmesupplies.test",
                    full_name="Portal Person",
                    hashed_password=pwd_context.hash("Passw0rd!xyz"),
                    is_active=True,
                )
            )
        await s.commit()
    return vendor_id, vendor_user_id, invoice_id


# ---------------------------------------------------------------------------
# DSAR export
# ---------------------------------------------------------------------------


async def test_dsar_user_bundle(realdb):
    """DSAR for a control-plane User returns their PII + roles + activity."""
    users = realdb.info("a").users
    # Seed an audit row authored by the admin so activity count > 0.
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with tenant_mk() as s:
        s.add(
            AuditLog(
                correlation_id=uuid.uuid4(),
                organization_id=org_id,
                actor_id=users["admin"],
                action="invoice.approved",
                entity_type="invoice",
                entity_id=uuid.uuid4(),
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "user", "identifier": realdb.email("a")},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject_type"] == "user"
    assert body["data"]["profile"]["email"] == realdb.email("a")
    assert "admin" in body["data"]["roles"]
    assert body["data"]["activity"]["audit_actions_authored"] >= 1


async def test_dsar_vendor_contact_bundle(realdb):
    """DSAR for a vendor_contact returns vendor PII + related invoices/payments."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, invoice_id = await _seed_vendor_with_records(tenant_mk, org_id)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "vendor_contact", "identifier": str(vendor_id)},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["vendor"]["tax_id"] == "12-3456789"
    # Issue #423: banking is MASKED unless the caller deliberately opts in.
    # `account` / `routing` are not in the display allowlist, so the allowlist
    # masks them — which is exactly the fail-closed direction the fix wanted.
    assert data["vendor"]["banking_disclosure"] == "masked"
    assert data["vendor"]["bank_details"]["account"] == "****1222"
    assert data["vendor"]["bank_details"]["_masked"] is True
    assert "000111222" not in resp.text
    assert len(data["related_invoices"]) == 1
    assert data["related_invoices"][0]["amount"] == "4200.50"  # Decimal-as-string
    assert len(data["related_payments"]) == 1
    assert data["counts"]["portal_users"] == 1


async def test_dsar_records_request_and_audits(realdb):
    """The DSAR request is recorded PII-free + writes a privacy.dsar_export audit
    row carrying only the subject UUID + type."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor_with_records(tenant_mk, org_id)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "vendor_contact", "identifier": str(vendor_id)},
        )
    assert resp.status_code == 200

    async with tenant_mk() as s:
        reqs = (
            (
                await s.execute(
                    select(DataSubjectRequest).where(DataSubjectRequest.organization_id == org_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(reqs) == 1
        assert reqs[0].request_type == "dsar_export"
        assert reqs[0].subject_id == vendor_id
        # PII-free: the email / tax_id never landed in the request row.
        assert "acmesupplies" not in (reqs[0].note or "")

        audit = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "privacy.dsar_export")))
            .scalars()
            .all()
        )
        assert len(audit) == 1
        details = audit[0].details
        assert details["subject_id"] == str(vendor_id)
        # No raw PII in the audit details.
        assert "tax_id" not in str(details)
        assert "acmesupplies" not in str(details)


async def test_dsar_vendor_user_bundle(realdb):
    """DSAR for a vendor_user (supplier-portal login) returns their profile PII,
    resolved by email and scoped to the tenant."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    _, vendor_user_id, _ = await _seed_vendor_with_records(tenant_mk, org_id)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "vendor_user", "identifier": "portal@acmesupplies.test"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["subject_type"] == "vendor_user"
    assert body["subject_id"] == str(vendor_user_id)
    assert body["data"]["profile"]["email"] == "portal@acmesupplies.test"
    assert body["data"]["profile"]["full_name"] == "Portal Person"


async def test_dsar_unknown_subject_404(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "user", "identifier": "nobody@nowhere.test"},
        )
    assert resp.status_code == 404


async def test_dsar_non_admin_forbidden(realdb):
    """A non-admin (ap_clerk) is denied — the privacy surface is admin-only."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "user", "identifier": realdb.email("a")},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Erasure
# ---------------------------------------------------------------------------


async def test_erasure_redacts_vendor_contact_preserves_money(realdb):
    """Erasing a vendor_contact redacts every contact PII field but leaves the
    invoice/payment amounts + statuses and the audit_log untouched."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, vendor_user_id, invoice_id = await _seed_vendor_with_records(tenant_mk, org_id)

    # Snapshot the audit_log count + the invoice amount before erasure.
    async with tenant_mk() as s:
        audit_before = (
            (await s.execute(select(AuditLog).where(AuditLog.organization_id == org_id)))
            .scalars()
            .all()
        )
        audit_count_before = len(audit_before)
        inv_amount_before = (
            await s.execute(select(Invoice.amount).where(Invoice.id == invoice_id))
        ).scalar_one()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/erasure",
            json={
                "subject_type": "vendor_contact",
                "identifier": str(vendor_id),
                "confirm": True,
                "note": "GDPR request #42",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["fields_redacted"] == 6

    async with tenant_mk() as s:
        vendor = await s.get(Vendor, vendor_id)
        # Every contact PII field redacted.
        assert vendor.email is None
        assert vendor.phone is None
        assert vendor.address is None
        assert vendor.tax_id is None
        assert vendor.bank_details is None
        assert vendor.beneficial_owner_data is None
        # Legal payee preserved (load-bearing on the invoice money trail).
        assert vendor.name == "Acme Supplies Ltd"

        # Portal user redacted too.
        vu = await s.get(VendorUser, vendor_user_id)
        assert vu.full_name == "[redacted]"
        assert vu.email.endswith("@redacted.invalid")
        assert vu.is_active is False

        # MONEY TRAIL UNTOUCHED.
        inv = await s.get(Invoice, invoice_id)
        assert inv.amount == inv_amount_before
        assert inv.vendor_name == "Acme Supplies Ltd"
        assert str(inv.status) in ("approved", "InvoiceStatus.approved")
        pay_amount = (
            await s.execute(select(Payment.amount).where(Payment.invoice_id == invoice_id))
        ).scalar_one()
        assert pay_amount == Decimal("4200.50")

        # AUDIT LOG: append-only — the prior rows are intact, and a NEW
        # privacy.erasure row was added (count strictly increased).
        audit_after = (
            (await s.execute(select(AuditLog).where(AuditLog.organization_id == org_id)))
            .scalars()
            .all()
        )
        assert len(audit_after) == audit_count_before + 1
        assert any(a.action == "privacy.erasure" for a in audit_after)


async def test_erasure_user_redacts_pii(realdb):
    """Erasing a control-plane user redacts email/full_name/sso + deactivates."""
    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id

    # Add a fresh disposable user to erase (don't nuke the seeded admin).
    from app.models.user import User

    target_id = uuid.uuid4()
    # Slug-derived so two concurrent pytest processes (each on its own realdb
    # slot) can't collide on the control-plane unique email.
    target_email = f"erase-me-{target_id}@{realdb.info('a').slug}.test"
    async with ctrl_mk() as s:
        s.add(
            User(
                id=target_id,
                email=target_email,
                full_name="Erase Me",
                hashed_password="x",
                sso_provider="okta",
                sso_provider_id="okta|123",
                is_active=True,
                organization_id=org_id,
                must_change_password=False,
            )
        )
        await s.commit()

    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(
                "/api/privacy/erasure",
                json={
                    "subject_type": "user",
                    "identifier": target_email,
                    "confirm": True,
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "completed"

        async with ctrl_mk() as s:
            u = await s.get(User, target_id)
            assert u.full_name == "[redacted]"
            assert u.email.endswith("@redacted.invalid")
            assert u.sso_provider is None
            assert u.sso_provider_id is None
            assert u.is_active is False
            assert u.hashed_password is None
            # Identity preserved for the audit/financial link.
            assert u.organization_id == org_id
    finally:
        from app.models.user import User as U

        async with ctrl_mk() as s:
            await s.execute(U.__table__.delete().where(U.id == target_id))
            await s.commit()


async def test_erasure_is_idempotent(realdb):
    """Re-running erasure on an already-erased subject is a safe noop."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor_with_records(tenant_mk, org_id, with_portal=False)

    async with realdb.client(key="a", role="admin") as c:
        first = await c.post(
            "/api/privacy/erasure",
            json={"subject_type": "vendor_contact", "identifier": str(vendor_id), "confirm": True},
        )
        second = await c.post(
            "/api/privacy/erasure",
            json={"subject_type": "vendor_contact", "identifier": str(vendor_id), "confirm": True},
        )
    assert first.status_code == 200
    assert first.json()["status"] == "completed"
    assert second.status_code == 200
    assert second.json()["status"] == "noop"
    assert second.json()["already_erased"] is True


async def test_erasure_vendor_user_redacts_pii_and_idempotent(realdb):
    """Erasing a vendor_user redacts email/full_name, nulls the credential +
    MFA secret, deactivates — and a re-run is a safe noop."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    _, vendor_user_id, _ = await _seed_vendor_with_records(tenant_mk, org_id)

    async with realdb.client(key="a", role="admin") as c:
        first = await c.post(
            "/api/privacy/erasure",
            json={
                "subject_type": "vendor_user",
                "identifier": "portal@acmesupplies.test",
                "confirm": True,
            },
        )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "completed"

    async with tenant_mk() as s:
        vu = await s.get(VendorUser, vendor_user_id)
        assert vu.full_name == "[redacted]"
        assert vu.email.endswith("@redacted.invalid")
        assert vu.hashed_password is None
        assert vu.mfa_secret is None
        assert vu.is_active is False

    # Re-running by the (now tombstoned) email no longer resolves — the subject
    # has been erased — so a second attempt is a clean 404, not a re-redaction.
    async with realdb.client(key="a", role="admin") as c:
        second = await c.post(
            "/api/privacy/erasure",
            json={
                "subject_type": "vendor_user",
                "identifier": "portal@acmesupplies.test",
                "confirm": True,
            },
        )
    assert second.status_code == 404


async def test_erasure_vendor_user_tenant_isolation(realdb):
    """A vendor_user in tenant A is NOT erasable when acting as tenant B — the
    tenant-A portal login stays intact."""
    tenant_mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    _, vendor_user_id, _ = await _seed_vendor_with_records(tenant_mk_a, org_a)

    async with realdb.client(key="b", role="admin") as c:
        resp = await c.post(
            "/api/privacy/erasure",
            json={
                "subject_type": "vendor_user",
                "identifier": "portal@acmesupplies.test",
                "confirm": True,
            },
        )
    assert resp.status_code == 404

    async with tenant_mk_a() as s:
        vu = await s.get(VendorUser, vendor_user_id)
        assert vu.email == "portal@acmesupplies.test"
        assert vu.full_name == "Portal Person"
        assert vu.is_active is True


async def test_erasure_requires_confirm(realdb):
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor_with_records(tenant_mk, org_id, with_portal=False)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/erasure",
            json={"subject_type": "vendor_contact", "identifier": str(vendor_id), "confirm": False},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_dsar_tenant_isolation(realdb):
    """A vendor in tenant A is NOT exportable when acting as tenant B."""
    tenant_mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor_with_records(tenant_mk_a, org_a, with_portal=False)

    # Acting as tenant B, the tenant-A vendor id must resolve to nothing.
    async with realdb.client(key="b", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "vendor_contact", "identifier": str(vendor_id)},
        )
    assert resp.status_code == 404


async def test_erasure_tenant_isolation(realdb):
    """A vendor in tenant A is NOT erasable when acting as tenant B — the
    tenant-A row stays fully intact."""
    tenant_mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor_with_records(tenant_mk_a, org_a, with_portal=False)

    async with realdb.client(key="b", role="admin") as c:
        resp = await c.post(
            "/api/privacy/erasure",
            json={"subject_type": "vendor_contact", "identifier": str(vendor_id), "confirm": True},
        )
    assert resp.status_code == 404

    # Tenant A's vendor PII is untouched.
    async with tenant_mk_a() as s:
        vendor = await s.get(Vendor, vendor_id)
        assert vendor.email == "contact@acmesupplies.test"
        assert vendor.tax_id == "12-3456789"


async def test_dsar_user_cross_org_not_resolved(realdb):
    """A user belonging to org A is not exportable when acting as tenant B —
    resolve_subject_id filters by organization_id."""
    # Tenant B admin asks for tenant A's admin email.
    async with realdb.client(key="b", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "user", "identifier": realdb.email("a")},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# #423 — banking disclosure is masked by default and gated when it isn't
# ---------------------------------------------------------------------------


def _dsar_request(**overrides):
    from app.schemas.privacy import DSARRequest

    body = {
        "subject_type": "vendor_contact",
        "identifier": str(uuid.uuid4()),
        "include_banking": True,
        "banking_justification": "GDPR request #42, legal hold LH-7",
    }
    body.update(overrides)
    return DSARRequest(**body)


class _StubUser:
    def __init__(self, perms):
        self.effective_permissions = frozenset(perms)


def test_banking_gate_is_inert_without_the_flag():
    """A routine masked DSAR must stay available to any admin — the gate only
    fires on the opt-in, so nothing about the default path changes."""
    from app.api.privacy import _authorize_banking_disclosure

    _authorize_banking_disclosure(_dsar_request(include_banking=False), _StubUser(set()))


def test_banking_gate_requires_the_bank_change_permission():
    from fastapi import HTTPException

    from app.api.permissions import PERM_VENDOR_BANK_CHANGE_APPROVE, PERM_VENDOR_MANAGE
    from app.api.privacy import _authorize_banking_disclosure

    # An admin-equivalent custom role that can manage vendors but was
    # deliberately NOT given the bank-change duty is refused.
    with pytest.raises(HTTPException) as exc:
        _authorize_banking_disclosure(_dsar_request(), _StubUser({PERM_VENDOR_MANAGE}))
    assert exc.value.status_code == 403
    assert PERM_VENDOR_BANK_CHANGE_APPROVE in exc.value.detail

    # Holding it passes.
    _authorize_banking_disclosure(_dsar_request(), _StubUser({PERM_VENDOR_BANK_CHANGE_APPROVE}))


def test_banking_gate_requires_a_written_justification():
    from fastapi import HTTPException

    from app.api.permissions import PERM_VENDOR_BANK_CHANGE_APPROVE
    from app.api.privacy import _authorize_banking_disclosure

    for blank in (None, "", "   "):
        with pytest.raises(HTTPException) as exc:
            _authorize_banking_disclosure(
                _dsar_request(banking_justification=blank),
                _StubUser({PERM_VENDOR_BANK_CHANGE_APPROVE}),
            )
        assert exc.value.status_code == 422


def test_banking_gate_rejects_a_subject_type_that_has_no_bank_details():
    """Silently ignoring the flag would leave the operator unable to tell a
    masked bundle from an unmasked one."""
    from fastapi import HTTPException

    from app.api.permissions import PERM_VENDOR_BANK_CHANGE_APPROVE
    from app.api.privacy import _authorize_banking_disclosure

    for subject_type in ("user", "vendor_user"):
        with pytest.raises(HTTPException) as exc:
            _authorize_banking_disclosure(
                _dsar_request(subject_type=subject_type, identifier="who@x.test"),
                _StubUser({PERM_VENDOR_BANK_CHANGE_APPROVE}),
            )
        assert exc.value.status_code == 422


async def test_dsar_include_banking_unmasks_and_writes_its_own_audit_row(realdb):
    """The unmasked bundle is a separately-audited, attributable act."""
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, _ = await _seed_vendor_with_records(tenant_mk, org_id, with_portal=False)

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={
                "subject_type": "vendor_contact",
                "identifier": str(vendor_id),
                "include_banking": True,
                "banking_justification": "Supplier asked for their own record, ticket AP-99",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["banking_disclosure"] == "unmasked"
    assert body["data"]["vendor"]["bank_details"]["account"] == "000111222"
    assert body["data"]["vendor"]["beneficial_owner_data"] == {"owner": "Jane Doe"}

    async with tenant_mk() as s:
        actions = [
            a.action
            for a in (
                (await s.execute(select(AuditLog).where(AuditLog.organization_id == org_id)))
                .scalars()
                .all()
            )
        ]
        assert "privacy.dsar_export" in actions
        # A separate ACTION, not a field on the routine row — this is the row an
        # incident review greps for.
        assert "privacy.dsar_export.unmasked" in actions

        unmasked = (
            (
                await s.execute(
                    select(AuditLog).where(AuditLog.action == "privacy.dsar_export.unmasked")
                )
            )
            .scalars()
            .all()
        )
        assert len(unmasked) == 1
        details = unmasked[0].details
        assert details["disclosed"] == ["bank_details", "beneficial_owner_data"]
        assert "AP-99" in details["justification"]
        # PII-out-of-logs still holds: the audit row carries the justification,
        # never the value it justified.
        assert "000111222" not in str(details)

        req = (
            (
                await s.execute(
                    select(DataSubjectRequest).where(
                        DataSubjectRequest.organization_id == org_id,
                        DataSubjectRequest.request_type == "dsar_export",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(req) == 1
        assert req[0].note.startswith("unmasked banking disclosure:")


async def test_dsar_include_banking_rejected_for_a_user_subject(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={
                "subject_type": "user",
                "identifier": realdb.email("a"),
                "include_banking": True,
                "banking_justification": "because",
            },
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# #424 — the export returns data, not a tally, and reaches auth material
# ---------------------------------------------------------------------------


async def test_dsar_user_bundle_returns_activity_content_not_only_counts(realdb):
    """Art 15 is a right to the data, not to a count of it."""
    from app.models.notification import Notification

    users = realdb.info("a").users
    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with tenant_mk() as s:
        s.add(
            AuditLog(
                correlation_id=uuid.uuid4(),
                organization_id=org_id,
                actor_id=users["admin"],
                action="invoice.approved",
                entity_type="invoice",
                entity_id=uuid.uuid4(),
                details={"other_subject_id": str(uuid.uuid4())},
            )
        )
        s.add(
            Notification(
                id=uuid.uuid4(),
                organization_id=org_id,
                recipient_user_id=users["admin"],
                event_type="invoice_approved",
                entity_type="invoice",
                title="Invoice INV-1 approved",
                body="You approved INV-1",
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "user", "identifier": realdb.email("a")},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert data["audit_events"]["total"] >= 1
    assert any(e["action"] == "invoice.approved" for e in data["audit_events"]["items"])
    # `details` is withheld — an audit row an admin authored ABOUT someone else
    # carries that person's identifiers (Art 15(4)).
    assert all("details" not in e for e in data["audit_events"]["items"])

    assert data["notifications"]["total"] >= 1
    assert any(n["title"] == "Invoice INV-1 approved" for n in data["notifications"]["items"])

    # The legacy counts are still there — something reads them.
    assert data["activity"]["audit_actions_authored"] >= 1
    # And the storage manifest is present even when it is empty.
    assert data["documents"]["total"] == 0


async def test_dsar_user_bundle_lists_passkeys_without_the_credential_material(realdb):
    from app.models.webauthn_credential import WebAuthnCredential

    ctrl_mk = realdb.control_sessionmaker()
    users = realdb.info("a").users
    cred_id = uuid.uuid4()
    async with ctrl_mk() as s:
        s.add(
            WebAuthnCredential(
                id=cred_id,
                user_id=users["admin"],
                credential_id=f"cred-{cred_id}",
                public_key="PUBLIC-KEY-MATERIAL",
                rp_id="acme.localhost",
                name="MacBook Touch ID",
                transports="internal",
            )
        )
        await s.commit()

    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(
                "/api/privacy/dsar",
                json={"subject_type": "user", "identifier": realdb.email("a")},
            )
        assert resp.status_code == 200, resp.text
        passkeys = resp.json()["data"]["passkeys"]
        assert len(passkeys) == 1
        assert passkeys[0]["name"] == "MacBook Touch ID"
        assert passkeys[0]["rp_id"] == "acme.localhost"
        # Authenticator material is withheld — it identifies the subject's
        # physical device and adds nothing they cannot see in the security UI.
        assert "credential_id" not in passkeys[0]
        assert "public_key" not in passkeys[0]
        assert "PUBLIC-KEY-MATERIAL" not in resp.text
    finally:
        async with ctrl_mk() as s:
            await s.execute(
                WebAuthnCredential.__table__.delete().where(WebAuthnCredential.id == cred_id)
            )
            await s.commit()


async def test_dsar_vendor_contact_bundle_reaches_contracts_cards_and_documents(realdb):
    from datetime import date

    from app.models.contract import Contract
    from app.models.virtual_card import VirtualCard

    tenant_mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id, _, invoice_id = await _seed_vendor_with_records(tenant_mk, org_id)
    async with tenant_mk() as s:
        vendor = await s.get(Vendor, vendor_id)
        vendor.w9_file_key = f"{org_id}/tax-forms/{vendor_id}/w9/w9.pdf"
        s.add(
            Contract(
                id=uuid.uuid4(),
                organization_id=org_id,
                contract_number="C-100",
                title="Master services agreement",
                vendor_id=vendor_id,
                currency="USD",
                total_value=Decimal("50000.00"),
                start_date=date(2026, 1, 1),
            )
        )
        s.add(
            VirtualCard(
                id=uuid.uuid4(),
                organization_id=org_id,
                invoice_id=invoice_id,
                vendor_id=vendor_id,
                card_provider="mock",
                provider_card_id="mock_card_1",
                last_four="4242",
                amount_limit=Decimal("4200.50"),
                currency="USD",
                status="created",
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/privacy/dsar",
            json={"subject_type": "vendor_contact", "identifier": str(vendor_id)},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert [x["contract_number"] for x in data["contracts"]] == ["C-100"]
    assert [x["last_four"] for x in data["virtual_cards"]] == ["4242"]
    # The W-9 is ENUMERATED with the disposition an erasure would apply.
    docs = data["documents"]["documents"]
    assert len(docs) == 1
    assert docs[0]["kind"] == "tax_form"
    assert docs[0]["erasure_disposition"] == "delete"


# ---------------------------------------------------------------------------
# #424 — erasure reaches passkeys and live sessions
# ---------------------------------------------------------------------------


async def test_erasure_user_deletes_passkeys_and_revokes_sessions(realdb, monkeypatch):
    from app.models.user import User
    from app.models.webauthn_credential import WebAuthnCredential
    from app.services import privacy_erasure as erasure_mod

    ctrl_mk = realdb.control_sessionmaker()
    org_id = realdb.info("a").org_id
    target_id = uuid.uuid4()
    target_email = f"erase-auth-{target_id}@{realdb.info('a').slug}.test"
    cred_id = uuid.uuid4()
    async with ctrl_mk() as s:
        s.add(
            User(
                id=target_id,
                email=target_email,
                full_name="Passkey Person",
                hashed_password="x",
                is_active=True,
                organization_id=org_id,
                must_change_password=False,
            )
        )
        s.add(
            WebAuthnCredential(
                id=cred_id,
                user_id=target_id,
                credential_id=f"cred-{cred_id}",
                public_key="PUBLIC-KEY-MATERIAL",
                rp_id="acme.localhost",
                name="Yubikey",
            )
        )
        await s.commit()

    revoked_for: list = []

    async def _fake_revoke(user_id):
        revoked_for.append(user_id)
        return ["jti-1", "jti-2"]

    # Patched on the erasure module — the point is that it calls the EXISTING
    # revocation path rather than growing a second one.
    monkeypatch.setattr(erasure_mod, "revoke_user_sessions", _fake_revoke)

    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.post(
                "/api/privacy/erasure",
                json={"subject_type": "user", "identifier": target_email, "confirm": True},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["passkeys_deleted"] == 1
        assert body["sessions_revoked"] == 2
        assert revoked_for == [target_id]

        async with ctrl_mk() as s:
            creds = (
                (
                    await s.execute(
                        select(WebAuthnCredential).where(WebAuthnCredential.user_id == target_id)
                    )
                )
                .scalars()
                .all()
            )
            # DELETED, not redacted — a credential row is authenticator material,
            # not a financial record.
            assert creds == []
    finally:
        async with ctrl_mk() as s:
            await s.execute(
                WebAuthnCredential.__table__.delete().where(WebAuthnCredential.user_id == target_id)
            )
            await s.execute(User.__table__.delete().where(User.id == target_id))
            await s.commit()
