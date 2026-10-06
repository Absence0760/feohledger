"""The tenant's SSO configuration — its one sanctioned, audited writer.

Mounted at `/api/organization/sso`, beside the SCIM-token mint that already
lives at `/api/organization/sso/scim-token` (`api/organization.py`).

`Organization.settings.sso` used to be writable only through the generic
`PATCH /api/organization`, which wrote no audit row and merged per top-level
key. A `{"sso": {"client_secret": …}}` body — the rotation `docs/secrets-rotation.md`
once told admins to send — therefore replaced the whole block: it dropped
`enabled`, `sso_only`, the rest of the IdP configuration and the SCIM group
state, silently. The PATCH now refuses the key and names this endpoint, the way
it does for `chat_notifications` and `brand.custom_domains`.

The shape rules (write-only secret, carried SCIM keys, PUT-not-merge) live in
the pure `services/sso_settings`; this module does persistence, RBAC, the
`sso_only` refusal and the audit write.

No module logger on purpose: the block holds the OIDC client secret, and what a
save changed already reaches the audit trail as key names.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import ROLE_ADMIN, require_roles
from app.api.refusals import coded_refusal
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import Role, User
from app.schemas.organization import SSOSettingsStatus, UpdateSSOSettingsRequest
from app.services.audit_dispatch import record_auth_audit_or_raise
from app.services.sso import SSOConfigError, check_sso_idp_config, sso_only_requested
from app.services.sso_settings import (
    SCIM_GROUP_ROLE_MAP_KEY,
    TEXT_KEYS,
    SSOSettingsError,
    build_sso_block,
    changed_keys,
    sso_status,
    stored_sso_block,
)
from app.tenant import get_tenant, lock_organization

router = APIRouter(prefix="/organization/sso", tags=["organization"])

# The coded refusal for `sso_only` over an IdP block that does not resolve
# (docs/decisions.md §204). `params.fields` names the keys, never their values.
SSO_ONLY_UNRESOLVED = "sso_only_idp_unresolved"


def refuse_unresolvable_sso_only(merged_settings: dict) -> None:
    """Refuse to save an `sso` block that asks for SSO-only but cannot deliver it.

    `sso.enabled` + `sso.sso_only` is a request to close password sign-in. It is
    honoured only when the selected protocol's IdP config resolves
    (`services/sso.is_sso_only`); otherwise the password stays open as the
    escape hatch, because that tenant's login page has no SSO button. Saving
    such a block would leave the admin believing SSO is enforced when it is
    not, so it is refused where the admin can still fix it (§204). `sso_only`
    with SSO switched off is accepted: an admin may stage the flag before the
    IdP is ready, and that closes nothing.

    The 422 names the offending keys only, never their values: the block holds
    the OIDC client secret.
    """
    if not sso_only_requested(merged_settings):
        return
    try:
        check_sso_idp_config(merged_settings)
    except SSOConfigError as exc:
        names = [f"sso.{name}" for name in exc.fields]
        listed = ", ".join(names) or "the identity-provider settings"
        raise HTTPException(
            status_code=422,
            detail=coded_refusal(
                SSO_ONLY_UNRESOLVED,
                (
                    "sso.sso_only closes password sign-in, so it needs an identity-provider "
                    f"configuration that resolves. Missing or invalid: {listed}. Complete "
                    "them, or save sso_only as false."
                ),
                fields=list(exc.fields),
            ),
        ) from None


async def _refuse_unknown_roles(db: AsyncSession, org_id, role_map: dict | None) -> None:
    """A group → role entry must name a role this org has.

    SCIM reconciliation looks the role up by name and skips one it cannot find,
    so a typo here would read as configured and grant nothing. Role names are
    not secret, so the refusal names them.
    """
    if not role_map:
        return
    wanted = set(role_map.values())
    known = set(
        (
            await db.execute(
                select(Role.name).where(
                    Role.name.in_(wanted),
                    or_(Role.organization_id == org_id, Role.organization_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
    )
    unknown = sorted(wanted - known)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                "sso.scim_group_role_map names roles this organization does not have: "
                f"{', '.join(unknown)}."
            ),
        )


def _status(org: Organization) -> SSOSettingsStatus:
    return SSOSettingsStatus(**sso_status(org.settings, tenant_slug=org.slug))


@router.get("", response_model=SSOSettingsStatus)
async def get_sso_settings(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Read the tenant's SSO configuration. Admin only.

    The client secret is never returned — by this endpoint or any other;
    `GET /api/organization` drops it for every role too
    (`services/org_settings_view.ALWAYS_REDACTED`).
    """
    return _status(org)


@router.put("", response_model=SSOSettingsStatus)
async def update_sso_settings(
    body: UpdateSSOSettingsRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Replace the tenant's SSO configuration. Admin only; audited.

    * `enabled` and `sso_only` must be stated; an omitted flag is refused, not
      read as `false`.
    * The stored client secret is kept when `client_secret` is omitted or blank,
      and removed only by `clear_client_secret: true`.
    * The SCIM token digest and group state are carried across, and so is the
      group → role map unless the request names one.
    * `sso_only` over an IdP block that does not resolve is a coded 422 naming
      the missing keys (§204).

    Audited as `organization.sso_updated` with the changed key NAMES and the
    resulting `enabled` / `sso_only` / `protocol` posture — never a value of the
    IdP configuration, and never the secret.
    """
    # Every settings writer (this one, the PATCH, the SCIM token mint and the
    # SCIM group writes) takes the same row lock before reading, so none of
    # them can write a stale snapshot back over another's change.
    locked = await lock_organization(db, org)

    before = stored_sso_block(locked.settings)
    try:
        block = build_sso_block(
            before,
            enabled=body.enabled,
            sso_only=body.sso_only,
            protocol=body.protocol,
            allowed_email_domains=body.allowed_email_domains,
            idp_x509_cert_multi=body.idp_x509_cert_multi,
            text_values={key: getattr(body, key) for key in TEXT_KEYS},
            client_secret=body.client_secret,
            clear_client_secret=body.clear_client_secret,
            scim_group_role_map=body.scim_group_role_map,
        )
    except SSOSettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    if body.scim_group_role_map is not None:
        await _refuse_unknown_roles(db, locked.id, block.get(SCIM_GROUP_ROLE_MAP_KEY))

    merged = {**(locked.settings or {}), "sso": block}
    refuse_unresolvable_sso_only(merged)

    changed = changed_keys(before, block)

    # The row FIRST, and no change without it: this is a sign-in policy (and,
    # with `client_secret`, a credential) change, and an unrecorded one is worse
    # than a refused one — the same call `services/sso_break_glass` makes. Names
    # and posture only: `client_secret` appears in `changed` as a NAME when it
    # was replaced or cleared; its value never enters the trail, which is
    # shipped to CloudWatch and a WORM bucket. Written while the row lock is
    # held; a commit failing after it leaves a row for a save that did not land,
    # which is the safe direction to be wrong in.
    try:
        await record_auth_audit_or_raise(
            organization_id=locked.id,
            actor_id=user.id,
            action="organization.sso_updated",
            entity_type="organization",
            entity_id=locked.id,
            details={
                "changed": changed,
                "enabled": block["enabled"],
                "sso_only": block["sso_only"],
                "protocol": block["protocol"],
            },
        )
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail="The change could not be recorded in the audit trail, so it was not saved.",
        ) from None

    locked.settings = merged
    flag_modified(locked, "settings")
    await db.commit()
    return _status(locked)
