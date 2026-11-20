"""The gates for the procurement lifecycle actions — intake submit / approve /
reject / convert / cancel / reopen and requisition submit / approve / reject /
convert / cancel / reopen. Each is the ONE definition its endpoint raises from
and the row's ``actions`` verdict is read from (issue #575 § One rule, one
place), so the /intake and /requisitions pages stop restating a status list, a
role list and ``requester_user_id !== me``.

Every gate returns an :class:`~app.api.refusals.ActionRefusal` or ``None``. The
endpoint raises ``refusal.http()``; every gate here is ``coded=True``, so the
422 / 403 ``detail`` is the coded ``{code, message, params}`` the verdict
carries (the English ``message`` stays a full sentence for a client that reads
it as text). ``api/intake.intake_action_refusals`` and
``api/requisitions.requisition_action_refusals`` ship ``refusal.verdict()`` on
the row after the endpoint's own role predicate.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi import HTTPException, status

from app.api.refusals import ActionRefusal
from app.models.procurement import (
    IntakeRequest,
    IntakeStatus,
    PurchaseRequisition,
    RequisitionStatus,
)
from app.services import intake_service, requisition_service
from app.services.approval_chain import check_segregation

#: An intake action its current status does not allow (an edge
#: ``intake_service.VALID_TRANSITIONS`` lacks, or convert from anything but
#: ``approved`` / ``converted``). ``params``: ``status`` (the row's), ``target``.
INTAKE_STATUS_REFUSED = "intake_status_refused"
#: The requisition sibling (``requisition_service.VALID_TRANSITIONS``).
REQUISITION_STATUS_REFUSED = "requisition_status_refused"


def _status_refusal(code: str, noun: str, current: str, target: str) -> ActionRefusal:
    return ActionRefusal(
        code,
        f"Cannot move {noun} from '{current}' to '{target}'.",
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        params={"status": str(current), "target": str(target)},
        coded=True,
    )


# ── Intake ──────────────────────────────────────────────────────────────────


def intake_transition_refusal(intake: IntakeRequest, target: IntakeStatus) -> ActionRefusal | None:
    """Submit / approve / reject / cancel / reopen: the move must be an edge of
    ``intake_service.VALID_TRANSITIONS``."""
    if target in intake_service.VALID_TRANSITIONS.get(intake.status, set()):
        return None
    return _status_refusal(INTAKE_STATUS_REFUSED, "an intake request", intake.status, target)


def intake_convert_refusal(intake: IntakeRequest) -> ActionRefusal | None:
    """Convert: only an ``approved`` intake, or an already ``converted`` one
    (the endpoint replays its requisition, idempotently)."""
    if intake.status in (IntakeStatus.approved, IntakeStatus.converted):
        return None
    return _status_refusal(
        INTAKE_STATUS_REFUSED, "an intake request", intake.status, IntakeStatus.converted
    )


# ── Requisitions ────────────────────────────────────────────────────────────


def requisition_transition_refusal(
    req: PurchaseRequisition, target: RequisitionStatus
) -> ActionRefusal | None:
    """Submit / reject / cancel / reopen: an edge of
    ``requisition_service.VALID_TRANSITIONS``."""
    if target in requisition_service.VALID_TRANSITIONS.get(req.status, set()):
        return None
    return _status_refusal(REQUISITION_STATUS_REFUSED, "a requisition", req.status, target)


def requisition_approve_refusal(
    req: PurchaseRequisition, actor_id: uuid.UUID
) -> ActionRefusal | None:
    """Approve: the status edge, then segregation of duties — the approver is
    neither the requester nor a material editor of the draft
    (``approval_chain.check_segregation``, 403 ``approval_segregation``). The
    order is the endpoint's, so a row failing both shows the 422."""
    refusal = requisition_transition_refusal(req, RequisitionStatus.approved)
    if refusal is not None:
        return refusal
    try:
        check_segregation(
            SimpleNamespace(
                uploaded_by_id=req.requester_user_id,
                segregation_actor_ids=requisition_service.implicated_editor_ids(req),
            ),
            actor_id,
            {"require_segregation": True},
        )
    except HTTPException as exc:
        detail = exc.detail
        return ActionRefusal(
            detail["code"],
            detail["message"],
            exc.status_code,
            params=detail.get("params") or None,
            coded=True,
        )
    return None


def requisition_convert_refusal(
    req: PurchaseRequisition, *, po_exists: bool
) -> ActionRefusal | None:
    """Convert: a requisition whose PO exists is replayed (200, ``created``
    false); otherwise the ``→ converted`` edge, i.e. only ``approved``.

    ``po_exists`` is the endpoint's lookup of ``converted_po_id``; the verdict
    passes ``converted_po_id is not None``, which the column's FK makes the same
    fact (a referenced PO cannot be deleted)."""
    if req.converted_po_id is not None and po_exists:
        return None
    return requisition_transition_refusal(req, RequisitionStatus.converted)
