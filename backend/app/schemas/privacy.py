"""Pydantic schemas for the GDPR / CCPA privacy endpoints (`/api/privacy`).

Request/response models for DSAR export, erasure / anonymization, and the
request-history list. The DSAR bundle is intentionally loosely typed (``dict``)
— it aggregates heterogeneous PII + related records across the control and
tenant DBs, and the privacy officer wants the raw portable JSON, not a flattened
view. The request-tracking responses are strict and PII-free.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.data_subject_request import (
    REQUEST_TYPES,
    SUBJECT_TYPES,
)

# Surfaced for the OpenAPI docs + validation error messages.
SUBJECT_TYPE_VALUES = list(SUBJECT_TYPES)
REQUEST_TYPE_VALUES = list(REQUEST_TYPES)


class DSARRequest(BaseModel):
    """Identify the data subject to export.

    ``identifier`` is an email for ``user`` / ``vendor_user`` subjects, or a
    Vendor UUID (string) for ``vendor_contact``. The resolver in
    ``services.privacy_export`` interprets it per ``subject_type``.

    ``include_banking`` opts a ``vendor_contact`` bundle out of the default
    masking of ``bank_details`` / ``beneficial_owner_data``. It is not a
    convenience flag: it needs the ``vendor.bank_change.approve`` permission, a
    written ``banking_justification``, and it writes its own audit row. See
    ``backend/docs/privacy.md`` § Banking data in a DSAR bundle.
    """

    subject_type: str = Field(..., description=f"One of {SUBJECT_TYPE_VALUES}")
    identifier: str = Field(..., min_length=1, max_length=320)
    include_banking: bool = Field(
        default=False,
        description=(
            "Return bank_details / beneficial_owner_data unmasked. "
            "vendor_contact only; requires the vendor.bank_change.approve "
            "permission and a banking_justification."
        ),
    )
    banking_justification: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Why an unmasked bundle is needed (legal basis / ticket reference). "
            "Required when include_banking is true. PII-free — it is recorded "
            "in the audit trail."
        ),
    )


class DSARResponse(BaseModel):
    """The portable DSAR bundle + the request-tracking metadata."""

    request_id: str
    subject_type: str
    subject_id: str
    generated_at: str
    # Whether this bundle's banking fields are masked. Surfaced so a consumer
    # never has to infer it from the shape of the values.
    banking_disclosure: str = "masked"
    # The portable bundle: every PII field + related-record summary held about
    # the subject, grouped by source. Loosely typed by design.
    data: dict


class ErasureRequest(BaseModel):
    """Identify the subject to erase + an explicit confirmation flag.

    ``confirm`` must be ``true`` — erasure is destructive (irreversible PII
    redaction), so the caller acknowledges intent. ``note`` is an optional,
    PII-free operator note (e.g. the legal basis / ticket reference).
    """

    subject_type: str = Field(..., description=f"One of {SUBJECT_TYPE_VALUES}")
    identifier: str = Field(..., min_length=1, max_length=320)
    confirm: bool = Field(..., description="Must be true to proceed")
    note: str | None = Field(default=None, max_length=500)


class ErasureResponse(BaseModel):
    request_id: str
    subject_type: str
    subject_id: str
    status: str  # completed | noop
    already_erased: bool
    fields_redacted: int
    # Non-identifying breakdown: which record kinds were touched + counts.
    record_counts: dict
    completed_at: str
    # Storage leg. `documents_failed > 0` means the erasure is INCOMPLETE and the
    # request should be re-run — surfaced rather than folded into the status, so
    # an operator sees it without reading the audit trail.
    documents_deleted: int = 0
    documents_retained: int = 0
    documents_failed: int = 0
    # Auth material (the `user` subject type only).
    passkeys_deleted: int = 0
    sessions_revoked: int = 0


class DataSubjectRequestSummary(BaseModel):
    """One row of the request-history list — strictly PII-free."""

    id: str
    request_type: str
    subject_type: str
    subject_id: str | None
    status: str
    requested_by: str | None
    fields_redacted: int
    record_counts: dict | None
    note: str | None
    created_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_row(cls, row) -> DataSubjectRequestSummary:
        return cls(
            id=str(row.id),
            request_type=row.request_type,
            subject_type=row.subject_type,
            subject_id=str(row.subject_id) if row.subject_id else None,
            status=row.status,
            requested_by=str(row.requested_by) if row.requested_by else None,
            fields_redacted=row.fields_redacted,
            record_counts=row.record_counts,
            note=row.note,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )


class DataSubjectRequestList(BaseModel):
    total: int
    requests: list[DataSubjectRequestSummary]
