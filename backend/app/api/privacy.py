"""GDPR / CCPA privacy endpoints (`/api/privacy`) — DSAR export + erasure.

Two coupled data-subject rights, both **admin-only** (the privacy-officer
privilege) and both audited into the tenant's append-only trail:

- ``POST /privacy/dsar`` — assemble everything held about a data subject into a
  portable JSON bundle (GDPR Art. 15 / CCPA right-to-know). Audited
  ``privacy.dsar_export``. **Banking fields are masked by default**; an
  unmasked bundle needs ``include_banking`` + the
  ``vendor.bank_change.approve`` permission + a written justification, and
  writes its OWN audit row (``privacy.dsar_export.unmasked``).
- ``POST /privacy/erasure`` — irreversibly redact the subject's PII while
  PRESERVING the immutable financial + audit record (GDPR Art. 17 / CCPA
  right-to-delete). Legally-required retention wins for transactional rows: we
  redact PII text fields and keep the money trail. Audited ``privacy.erasure``.
  Idempotent — re-running on an already-erased subject is a safe no-op. It also
  deletes the subject's sole-subject documents from object storage, their
  passkey rows, and their live sessions; what is deleted vs retained is decided
  in ``services/privacy_documents`` and published in ``backend/docs/privacy.md``.
- ``GET /privacy/requests`` — the privacy officer's request history (PII-free).

Subjects span the control plane (``User``) and the tenant DB (``VendorUser``,
``Vendor`` contacts). Tenant isolation is enforced by the injected ``get_tenant``
/ ``get_tenant_db`` chokepoint (which cross-checks the JWT ``org`` claim) plus
filtering every query by ``organization_id`` — a DSAR / erasure for one subject
can never reach another subject's or another tenant's data.

**PII-out-of-logs:** the audit row and the persisted ``DataSubjectRequest`` row
record only the resolved subject UUID + type + non-identifying counts — never the
raw email / tax-id / bank details. The DSAR bundle itself is returned in the HTTP
response and never written to a log or to the request-tracking table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, require_roles
from app.api.permissions import PERM_VENDOR_BANK_CHANGE_APPROVE
from app.database import get_control_db
from app.models.data_subject_request import (
    REQUEST_DSAR_EXPORT,
    REQUEST_ERASURE,
    STATUS_COMPLETED,
    STATUS_NOOP,
    SUBJECT_TYPES,
    SUBJECT_VENDOR_CONTACT,
    DataSubjectRequest,
)
from app.models.organization import Organization
from app.models.user import User
from app.schemas.privacy import (
    DataSubjectRequestList,
    DataSubjectRequestSummary,
    DSARRequest,
    DSARResponse,
    ErasureRequest,
    ErasureResponse,
)
from app.services.audit_dispatch import dispatch_audit
from app.services.privacy_erasure import erase_subject
from app.services.privacy_export import (
    BankingDisclosureNotPermitted,
    SubjectNotFound,
    build_dsar_bundle,
    resolve_subject_id,
)
from app.tenant import get_tenant, get_tenant_db

router = APIRouter(prefix="/privacy", tags=["privacy"])


def _validate_subject_type(subject_type: str) -> None:
    if subject_type not in SUBJECT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown subject_type '{subject_type}'; valid: {list(SUBJECT_TYPES)}",
        )


def _authorize_banking_disclosure(body: DSARRequest, user: User) -> None:
    """Gate the unmasked-banking variant of a DSAR bundle.

    Three conditions, all checked before any data is read:

    * the caller holds ``vendor.bank_change.approve`` — the granular permission
      that already gates APPROVING a bank-detail change. Reading every payee
      coordinate out into a file is the same asset as redirecting where the money
      goes, so it answers to the same duty. ``require_permission`` is not used as
      a route dependency here because the flag, not the route, is what needs
      gating: a routine masked DSAR must stay available to any admin;
    * a written ``banking_justification``, because an unmasked bundle is meant to
      be a deliberate act with a reason attached, not a default;
    * the subject is a ``vendor_contact`` — nothing else has bank details, and a
      silently-ignored flag would leave the operator unable to tell a masked
      bundle from an unmasked one.

    NOTE ON REACH: ``ROLE_ADMIN`` resolves to every permission in the catalogue,
    so on the four stock system roles this gate admits exactly the callers the
    route already admits. That is not a no-op — it is what makes the control
    configurable: an org that splits duties with a custom admin-equivalent role
    can now deny this without denying DSARs. The stronger gate (a step-up MFA
    proof on the request) needs the SPA to collect that proof and is tracked in
    ``docs/followups.md``.
    """
    if not body.include_banking:
        return
    if body.subject_type != SUBJECT_VENDOR_CONTACT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="include_banking applies to vendor_contact subjects only",
        )
    if not (body.banking_justification or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="banking_justification is required when include_banking is true",
        )
    held = getattr(user, "effective_permissions", frozenset())
    if PERM_VENDOR_BANK_CHANGE_APPROVE not in held:
        # PII-free, and it names the permission so the caller can ask for it.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "An unmasked banking disclosure requires the "
                f"'{PERM_VENDOR_BANK_CHANGE_APPROVE}' permission"
            ),
        )


@router.post("/dsar", response_model=DSARResponse)
async def dsar_export(
    body: DSARRequest,
    org: Organization = Depends(get_tenant),
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Assemble a portable bundle of everything held about a data subject.

    Admin only. The request itself is audited (``privacy.dsar_export``) and
    recorded in ``data_subject_requests`` — both PII-free (subject UUID + type +
    counts only). The bundle is returned in the body, never logged or stored.

    Banking fields are MASKED unless ``include_banking`` is set and the caller
    clears ``_authorize_banking_disclosure`` — in which case a second,
    separately-actioned audit row records the disclosure and its justification.
    """
    _validate_subject_type(body.subject_type)
    _authorize_banking_disclosure(body, user)
    now = datetime.now(UTC)

    try:
        subject_id = await resolve_subject_id(
            subject_type=body.subject_type,
            identifier=body.identifier,
            organization_id=org.id,
            control_db=control_db,
            tenant_db=db,
        )
        bundle = await build_dsar_bundle(
            subject_type=body.subject_type,
            subject_id=subject_id,
            organization_id=org.id,
            control_db=control_db,
            tenant_db=db,
            include_banking=body.include_banking,
        )
    except BankingDisclosureNotPermitted as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except SubjectNotFound as exc:
        # Same shape regardless of WHY (wrong tenant vs. truly absent) so the
        # response can't be used to probe which subjects exist in other tenants.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found"
        ) from exc

    request_row = DataSubjectRequest(
        id=uuid.uuid4(),
        organization_id=org.id,
        request_type=REQUEST_DSAR_EXPORT,
        subject_type=body.subject_type,
        subject_id=subject_id,
        status=STATUS_COMPLETED,
        requested_by=user.id,
        completed_at=now,
        record_counts=bundle.get("counts"),
        # The justification rides the request row too, so the privacy officer's
        # own history (`GET /privacy/requests`) shows which exports were
        # unmasked and why without cross-referencing the audit trail.
        note=(
            f"unmasked banking disclosure: {(body.banking_justification or '').strip()[:400]}"
            if body.include_banking
            else None
        ),
    )
    db.add(request_row)

    # Append-only audit row — PII-free: subject UUID + type, never the email /
    # tax-id / bank details that the bundle itself carries.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="privacy.dsar_export",
        entity_type="data_subject_request",
        entity_id=request_row.id,
        details={
            "subject_type": body.subject_type,
            "subject_id": str(subject_id),
            "banking_disclosure": "unmasked" if body.include_banking else "masked",
        },
    )

    # A SEPARATE, separately-named audit row for the unmasked variant. Folding it
    # into a field of the row above would make "who pulled a supplier's full
    # account number" a JSONB filter rather than an action anyone can grep for,
    # and this is the row an incident review goes looking for. PII-free: the
    # justification is an operator note, never subject data.
    if body.include_banking:
        await dispatch_audit(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org.id,
            actor_id=user.id,
            action="privacy.dsar_export.unmasked",
            entity_type="data_subject_request",
            entity_id=request_row.id,
            details={
                "subject_type": body.subject_type,
                "subject_id": str(subject_id),
                "disclosed": ["bank_details", "beneficial_owner_data"],
                "justification": (body.banking_justification or "").strip()[:500],
            },
        )
    await db.commit()

    return DSARResponse(
        request_id=str(request_row.id),
        subject_type=body.subject_type,
        subject_id=str(subject_id),
        generated_at=now.isoformat(),
        banking_disclosure="unmasked" if body.include_banking else "masked",
        data=bundle,
    )


@router.post("/erasure", response_model=ErasureResponse)
async def erasure(
    body: ErasureRequest,
    org: Organization = Depends(get_tenant),
    db: AsyncSession = Depends(get_tenant_db),
    control_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Irreversibly redact a subject's PII; preserve the financial + audit trail.

    Admin only. ``confirm`` must be true. Redacts PII text fields in place and
    NEVER touches a money field or an ``audit_log`` row (it writes a NEW audit
    row instead). Idempotent — re-running on an already-erased subject returns a
    ``noop`` status with no further change.
    """
    _validate_subject_type(body.subject_type)
    if not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="confirm must be true to perform an erasure",
        )
    now = datetime.now(UTC)

    try:
        subject_id = await resolve_subject_id(
            subject_type=body.subject_type,
            identifier=body.identifier,
            organization_id=org.id,
            control_db=control_db,
            tenant_db=db,
        )
        result = await erase_subject(
            subject_type=body.subject_type,
            subject_id=subject_id,
            organization_id=org.id,
            control_db=control_db,
            tenant_db=db,
            now=now,
        )
    except SubjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found"
        ) from exc

    req_status = STATUS_NOOP if result.already_erased else STATUS_COMPLETED

    request_row = DataSubjectRequest(
        id=uuid.uuid4(),
        organization_id=org.id,
        request_type=REQUEST_ERASURE,
        subject_type=body.subject_type,
        subject_id=subject_id,
        status=req_status,
        requested_by=user.id,
        completed_at=now,
        record_counts=result.record_counts,
        fields_redacted=result.fields_redacted,
        note=body.note,
    )
    db.add(request_row)

    # Append-only audit row. PII-free: subject UUID + type + counts only. The
    # storage + auth legs are recorded here too — "we deleted N documents and
    # revoked M sessions" is the evidence that an Art. 17 request was honoured
    # beyond the database, and `documents_failed` is what says it was not.
    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="privacy.erasure",
        entity_type="data_subject_request",
        entity_id=request_row.id,
        details={
            "subject_type": body.subject_type,
            "subject_id": str(subject_id),
            "status": req_status,
            "fields_redacted": result.fields_redacted,
            "documents_deleted": result.documents_deleted,
            "documents_retained": result.documents_retained,
            "documents_failed": result.documents_failed,
            "passkeys_deleted": result.passkeys_deleted,
            "sessions_revoked": result.sessions_revoked,
        },
    )
    # Cross-DB write without 2PC (control plane + tenant DB). Commit the
    # tenant-side audit + request rows FIRST, then the control-plane PII
    # mutation. If the control commit then fails, the subject is NOT yet erased
    # (control rolls back) and a re-run re-attempts cleanly. Committing control
    # first would, on a tenant-commit failure, leave the PII erased with no audit
    # evidence — and the idempotency tombstone would suppress the audit on every
    # retry. An append-only audit row that slightly precedes its
    # (retried-to-success) mutation is the safe trade; the reverse loses the
    # regulated record permanently. For vendor_* subjects control_db has no
    # pending change, so its commit is a harmless no-op.
    await db.commit()
    await control_db.commit()

    return ErasureResponse(
        request_id=str(request_row.id),
        subject_type=body.subject_type,
        subject_id=str(subject_id),
        status=req_status,
        already_erased=result.already_erased,
        fields_redacted=result.fields_redacted,
        record_counts=result.record_counts,
        completed_at=now.isoformat(),
        documents_deleted=result.documents_deleted,
        documents_retained=result.documents_retained,
        documents_failed=result.documents_failed,
        passkeys_deleted=result.passkeys_deleted,
        sessions_revoked=result.sessions_revoked,
    )


@router.get("/requests", response_model=DataSubjectRequestList)
async def list_requests(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """The privacy officer's request history for this tenant (PII-free)."""
    rows = (
        (
            await db.execute(
                select(DataSubjectRequest)
                .where(DataSubjectRequest.organization_id == org.id)
                .order_by(DataSubjectRequest.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return DataSubjectRequestList(
        total=len(rows),
        requests=[DataSubjectRequestSummary.from_row(r) for r in rows],
    )
