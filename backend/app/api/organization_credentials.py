"""Tenant provider credentials — their one sanctioned, audited writer.

Mounted at `/api/organization/credentials`. The secrets of the `erp`,
`payments` and `cards` settings blocks (API keys, OAuth client secrets, account
tokens, webhook signing secrets — the catalogue is
`services/provider_credentials.SECRET_FIELDS`) are stored sealed in
`provider_credentials`, not in `Organization.settings`. This router is the only
way to set or remove one, and no endpoint returns one:

* `GET  /api/organization/credentials` — which secret paths are set, per block.
* `PUT  /api/organization/credentials/{block}` — `{"set": {path: value},
  "clear": [path]}`. A blank value keeps what is stored.

`PATCH /api/organization` refuses a non-blank secret in these blocks and names
this endpoint, the arrangement `PUT /api/organization/sso` has for the OIDC
client secret (decisions §239).

No module logger on purpose: the request body is credential material, and what
a save changed reaches the audit trail as path names.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, require_roles
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.services.audit_dispatch import record_auth_audit_or_raise
from app.services.credential_crypto import CredentialCryptoError
from app.services.provider_credentials import (
    CREDENTIAL_BLOCKS,
    CredentialPathError,
    credential_status,
    update_secrets,
    validate_update,
)
from app.tenant import get_tenant, lock_organization

router = APIRouter(prefix="/organization/credentials", tags=["organization"])


class ProviderCredentialStatus(BaseModel):
    """Which secret paths are stored, per block. Names only — never a value."""

    erp: list[str]
    payments: list[str]
    cards: list[str]


class UpdateProviderCredentialsRequest(BaseModel):
    """Documents the PUT body for the OpenAPI contract.

    The endpoint parses the body itself (:func:`_parse_body`) so a malformed
    one is refused with paths only: FastAPI's default 422 echoes each failing
    `input`, which here is the credential.
    """

    set: dict[str, str] = {}
    clear: list[str] = []


def _parse_body(raw: object) -> tuple[dict[str, object], list[str]]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    unknown = sorted(set(raw) - {"set", "clear"})
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown fields: {', '.join(unknown)}")
    to_set = raw.get("set", {})
    to_clear = raw.get("clear", [])
    if not isinstance(to_set, dict) or not all(isinstance(k, str) for k in to_set):
        raise HTTPException(status_code=422, detail="`set` must be an object of path → text")
    if not isinstance(to_clear, list) or not all(isinstance(p, str) for p in to_clear):
        raise HTTPException(status_code=422, detail="`clear` must be a list of paths")
    return to_set, to_clear


@router.get("", response_model=ProviderCredentialStatus)
async def get_provider_credentials(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Which ERP / payment / card secrets are stored. Admin only; names only."""
    return ProviderCredentialStatus(**await credential_status(db, org.id))


@router.put(
    "/{block}",
    response_model=ProviderCredentialStatus,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": UpdateProviderCredentialsRequest.model_json_schema()}
            },
        }
    },
)
async def update_provider_credentials(
    block: str,
    request: Request,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Set or remove secrets of one block. Admin only; audited first.

    * `set` — path → new value. A blank value is ignored (keeps what is
      stored), which is what a "leave blank to keep" form sends.
    * `clear` — paths to remove. A path may not be both set and cleared.
    * Paths are the block's secret fields (`api_key`, `client_secret`, …) or,
      for `payments`, `providers.<provider>.<field>` for a multi-route entry.

    Audited as `organization.credentials_updated` with the block and the changed
    path NAMES — written before the save, and a row that cannot be written is a
    503 with nothing saved (the `PUT /organization/sso` rule, decisions §239).
    Nothing is recorded, and nothing is written, when the request changes
    nothing.
    """
    if block not in CREDENTIAL_BLOCKS:
        raise HTTPException(status_code=404, detail="Unknown credential block")
    try:
        raw = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="Request body must be a JSON object") from None
    set_values, clear = _parse_body(raw)
    try:
        to_set, to_clear = validate_update(block, set_values, clear)
    except CredentialPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    # Same row lock every settings writer takes: it serialises two credential
    # saves for this org, and a credential save against a concurrent PATCH of
    # the block's configuration.
    locked = await lock_organization(db, org)

    async def _audit_first(changed: list[str]) -> None:
        try:
            await record_auth_audit_or_raise(
                organization_id=locked.id,
                actor_id=user.id,
                action="organization.credentials_updated",
                entity_type="organization",
                entity_id=locked.id,
                details={
                    "block": block,
                    "changed": changed,
                    "cleared": sorted(p for p in changed if p in to_clear),
                },
            )
        except Exception:
            raise HTTPException(
                status_code=503,
                detail="The change could not be recorded in the audit trail, so it was not saved.",
            ) from None

    try:
        await update_secrets(db, locked.id, block, to_set, to_clear, before_write=_audit_first)
    except CredentialCryptoError:
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail="The credential store is unavailable, so nothing was saved.",
        ) from None
    except HTTPException:
        await db.rollback()
        raise
    await db.commit()
    return ProviderCredentialStatus(**await credential_status(db, locked.id))
