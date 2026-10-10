"""Connect an ERP through an OAuth 2.0 authorization-code grant.

QuickBooks Online (and Xero / Sage Accounting once their adapters land) need
the customer's admin to consent in the provider's UI. The flow:

1. The settings page calls ``GET /api/organization/erp/oauth/{provider}/authorize``
   (admin, plan-gated) and navigates the browser to the ``authorize_url`` it
   returns.
2. The provider redirects to the ONE fixed redirect URI every app registers,
   ``FEOH_API_PUBLIC_URL`` + ``/api/erp/oauth/callback``. It carries no JWT and
   no tenant header, so the tenant comes from the signed, single-use ``state``
   minted in step 1 (the SAML ACS pattern: tenant from server-minted state,
   never from the URL).
3. The callback exchanges the code, seals the tokens in ``provider_credentials``
   beside the consent metadata in ``settings.erp.oauth``, and 302s the browser
   back to the tenant's own origin (``tenant_urls.tenant_base_url`` —
   the same resolver every outbound tenant link uses) at
   ``/organization?section=erp&erp_connected=<provider>`` or
   ``…&erp_error=<stable_code>``. Never a token, code or provider message.

``POST /api/organization/erp/oauth/disconnect`` clears the block and its sealed
tokens, then revokes best-effort; ``GET /api/organization/erp/oauth/status``
reports it without tokens. Connect and disconnect write the sealed store through
``provider_credentials.update_secrets`` with their audit row FIRST
(``organization.erp_connected`` / ``organization.erp_disconnected``, decisions
§239's rule): a row that cannot be written leaves nothing changed. Token
handling: ``services/erp_oauth``.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import ROLE_ADMIN, ensure_live_erp_entitled, require_roles
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.services import erp_oauth, provider_credentials
from app.services.audit_dispatch import dispatch_auth_audit, record_auth_audit_or_raise
from app.services.credential_crypto import CredentialCryptoError
from app.tenant import get_tenant, lock_organization
from app.utils.tenant_urls import tenant_base_url

logger = logging.getLogger(__name__)

#: Admin routes, under the tenant's own ``/organization`` surface.
router = APIRouter(prefix="/organization/erp/oauth", tags=["organization"])
#: The provider-facing callback. Public by design: it is authenticated by the
#: signed, single-use ``state``, not a JWT.
public_router = APIRouter(prefix="/erp/oauth", tags=["erp"])


class AuthorizeResponse(BaseModel):
    authorize_url: str


class ProviderStatus(BaseModel):
    key: str
    display_name: str
    #: An app is configured (the tenant's own, or the platform's).
    available: bool
    #: Which app a connect would use now: "tenant" | "platform" | None.
    client_source: str | None


class OAuthStatusResponse(BaseModel):
    provider: str | None
    connected: bool
    needs_reconnect: bool = False
    external_tenant_id: str | None = None
    expires_at: str | None = None
    refresh_token_expires_at: str | None = None
    connected_at: str | None = None
    #: The redirect URI to register on a bring-your-own provider app.
    redirect_uri: str
    providers: list[ProviderStatus]


class DisconnectResponse(BaseModel):
    disconnected: bool
    revoked: bool


def _direct(provider: str) -> dict:
    """The ``settings.erp`` shape a connect selects — what the plan gate keys on."""
    return {"type": provider, "integration_method": "direct"}


def _spec_or_404(provider: str):
    spec = erp_oauth.load_providers().get(provider)
    if spec is None:
        raise HTTPException(status_code=404, detail="Unknown ERP provider.")
    return spec


async def _resolved_erp_or_none(org: Organization, db: AsyncSession) -> dict | None:
    """The org's ERP block with its sealed secrets, or None when the store is
    unavailable — which every caller here treats as "no usable app"."""
    try:
        return await provider_credentials.provider_config(org, "erp", db=db) or {}
    except CredentialCryptoError:
        return None


def _has_own_app(org: Organization, provider: str) -> bool:
    """Does the stored configuration name a bring-your-own app for ``provider``?"""
    erp = (org.settings or {}).get("erp")
    return (
        isinstance(erp, dict)
        and erp.get("type") == provider
        and bool(str(erp.get("client_id") or "").strip())
    )


@router.get("/status", response_model=OAuthStatusResponse)
async def oauth_status(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),  # noqa: ARG001 - RBAC gate
    db: AsyncSession = Depends(get_control_db),
):
    erp = (org.settings or {}).get("erp") or {}
    oauth = erp.get("oauth") if isinstance(erp, dict) else None
    resolved = await _resolved_erp_or_none(org, db)
    providers = []
    for spec in sorted(erp_oauth.load_providers().values(), key=lambda s: s.key):
        # An unopenable store reads as "no tenant app"; the platform app still
        # counts, since its credentials are not in the store.
        creds = erp_oauth.resolve_client_credentials(
            spec, resolved, source=None if resolved is not None else "platform"
        )
        providers.append(
            ProviderStatus(
                key=spec.key,
                display_name=spec.display_name,
                available=creds is not None,
                client_source=creds.source if creds else None,
            )
        )
    common = {"redirect_uri": erp_oauth.callback_url(), "providers": providers}
    if not isinstance(oauth, dict):
        provider = erp.get("type") if isinstance(erp, dict) else None
        known = provider in erp_oauth.OAUTH_PROVIDERS
        return OAuthStatusResponse(provider=provider if known else None, connected=False, **common)
    needs_reconnect = bool(oauth.get("needs_reconnect"))
    return OAuthStatusResponse(
        provider=oauth.get("provider"),
        connected=not needs_reconnect,
        needs_reconnect=needs_reconnect,
        external_tenant_id=oauth.get("external_tenant_id"),
        expires_at=oauth.get("expires_at"),
        refresh_token_expires_at=oauth.get("refresh_token_expires_at"),
        connected_at=oauth.get("connected_at"),
        **common,
    )


@router.get("/{provider}/authorize", response_model=AuthorizeResponse)
async def oauth_authorize(
    provider: str,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """The provider's consent URL, carrying a fresh signed ``state``.

    JSON rather than a 302 because the SPA authenticates with a bearer header a
    top-level navigation can't carry; the page navigates to the URL itself.
    """
    spec = _spec_or_404(provider)
    await ensure_live_erp_entitled(db, org.id, _direct(spec.key))
    resolved = await _resolved_erp_or_none(org, db)
    if resolved is None and _has_own_app(org, spec.key):
        # The tenant saved its own app, whose secret is sealed and unreadable
        # right now. Falling back to the platform app would record a consent
        # under an app the tenant did not choose; refuse instead.
        raise HTTPException(
            status_code=503, detail=provider_credentials.CREDENTIALS_UNAVAILABLE_DETAIL
        )
    creds = erp_oauth.resolve_client_credentials(
        spec, resolved, source=None if resolved is not None else "platform"
    )
    if creds is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{spec.display_name} is not available: no app is configured. Save your own "
                "client id and secret, or ask the operator to configure the platform app."
            ),
        )
    state = await erp_oauth.create_state(
        org_id=org.id,
        org_slug=org.slug,
        user_id=user.id,
        provider=spec.key,
        client_source=creds.source,
    )
    return AuthorizeResponse(authorize_url=erp_oauth.build_authorize_url(spec, creds, state))


@router.post("/disconnect", response_model=DisconnectResponse)
async def oauth_disconnect(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Remove ``settings.erp.oauth`` and its sealed tokens, then revoke at the
    provider (best-effort).

    Not plan-gated: a downgraded tenant must always be able to cut the link.
    Idempotent — with nothing connected it answers ``disconnected: false``.
    Audited first (``organization.erp_disconnected``); the revoke outcome
    follows as ``organization.erp_token_revoked``, since it is only known after
    the commit.
    """
    org = await lock_organization(db, org)
    current = dict(org.settings or {})
    erp = dict(current.get("erp") or {})
    oauth = erp.get("oauth")
    if not isinstance(oauth, dict):
        # No connection metadata. Sealed tokens without it should not exist,
        # but if any do they are still cleared (audited) rather than orphaned.
        try:
            stored = await provider_credentials.load_secrets(org.id, "erp", db=db)
        except CredentialCryptoError:
            stored = {}
        if not any(stored.get(p) for p in erp_oauth.TOKEN_PATHS):
            await db.rollback()
            return DisconnectResponse(disconnected=False, revoked=False)
        oauth = {}

    provider = str(oauth.get("provider") or "")
    # Read the tokens (and the app credentials revoking needs) before they are
    # cleared. A store that cannot be opened is a 503 with nothing changed:
    # clearing would need to open it too, and a half-disconnect is worse.
    try:
        resolved, oauth_with_tokens = await erp_oauth.resolved_erp(org.id, current, db)
    except CredentialCryptoError:
        await db.rollback()
        raise HTTPException(
            status_code=503, detail=provider_credentials.CREDENTIALS_UNAVAILABLE_DETAIL
        ) from None

    async def _audit_first(changed: list[str]) -> None:
        await _record_or_503(
            org.id,
            user.id,
            "organization.erp_disconnected",
            {"provider": provider, "cleared": changed},
        )

    erp.pop("oauth", None)
    current["erp"] = erp
    org.settings = current
    flag_modified(org, "settings")
    try:
        changed = await provider_credentials.update_secrets(
            db, org.id, "erp", {}, list(erp_oauth.TOKEN_PATHS), before_write=_audit_first
        )
        if not changed:
            # No sealed token to clear, so the hook never ran: the metadata
            # removal still needs its record.
            await _audit_first([])
    except CredentialCryptoError:
        await db.rollback()
        raise HTTPException(
            status_code=503, detail=provider_credentials.CREDENTIALS_UNAVAILABLE_DETAIL
        ) from None
    except HTTPException:
        await db.rollback()
        raise
    await db.commit()

    # Revoke AFTER the commit: the provider's latency is not charged to a
    # transaction holding the org row lock, and a failed revoke must not leave
    # the tenant believing it is still connected.
    revoked = False
    spec = erp_oauth.load_providers().get(provider)
    if spec is not None:
        revoked = await erp_oauth.revoke(spec, resolved, oauth_with_tokens)

    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="organization.erp_token_revoked",
        entity_id=org.id,
        entity_type="organization",
        details={"provider": provider, "revoked": revoked},
    )
    return DisconnectResponse(disconnected=True, revoked=revoked)


async def _record_or_503(org_id: uuid.UUID, actor_id: uuid.UUID, action: str, details: dict):
    """Write an audit row or refuse with 503 — the audit-first rule (§239)."""
    try:
        await record_auth_audit_or_raise(
            organization_id=org_id,
            actor_id=actor_id,
            action=action,
            entity_type="organization",
            entity_id=org_id,
            details=details,
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="The change could not be recorded in the audit trail, so it was not saved.",
        ) from None


# ---------------------------------------------------------------------------
# Callback (public)
# ---------------------------------------------------------------------------


def _home(slug: str, org_settings: dict | None, **params: str):
    """302 to the tenant's settings page, or a plain page when no URL is known."""
    base = tenant_base_url(slug, org_settings)
    if not base:
        outcome = "connected" if "erp_connected" in params else "not connected"
        return PlainTextResponse(
            f"ERP {outcome}. Return to FeohLedger → Organization → ERP.", status_code=200
        )
    query = urlencode({"section": "erp", **params})
    return RedirectResponse(f"{base}/organization?{query}", status_code=302)


async def _realm_claimed_elsewhere(
    db: AsyncSession, provider: str, external_tenant_id: str, org_id: uuid.UUID
) -> bool:
    """Is this provider company already connected to a different tenant?

    Two tenants on one QuickBooks company would each post into the other's
    books, and the Phase 3 webhook (keyed by realmId alone) could not route.
    """
    row = (
        await db.execute(
            text(
                "SELECT 1 FROM organizations WHERE id <> :org "
                "AND settings->'erp'->'oauth'->>'provider' = :provider "
                "AND settings->'erp'->'oauth'->>'external_tenant_id' = :ext LIMIT 1"
            ),
            {"org": org_id, "provider": provider, "ext": external_tenant_id},
        )
    ).first()
    return row is not None


@public_router.get("/callback")
async def oauth_callback(request: Request, db: AsyncSession = Depends(get_control_db)):
    params = {k: v for k, v in request.query_params.items()}
    try:
        bound = await erp_oauth.consume_state(params.get("state", ""))
    except erp_oauth.OAuthStateError as exc:
        if exc.org_slug is None:
            # Forged / malformed: we don't know whose tenant to send it to,
            # and must not let the URL choose.
            return PlainTextResponse(
                "This ERP connection link is invalid. Start again from FeohLedger.",
                status_code=400,
            )
        org = (
            await db.execute(select(Organization).where(Organization.slug == exc.org_slug))
        ).scalar_one_or_none()
        return _home(exc.org_slug, org.settings if org else None, erp_error=exc.code)

    slug = bound["org_slug"]
    org = (
        await db.execute(select(Organization).where(Organization.id == uuid.UUID(bound["org_id"])))
    ).scalar_one_or_none()
    if org is None or org.slug != slug:
        return _home(slug, None, erp_error="invalid_state")

    # Snapshot now: a later rollback expires `org`, and a lazy reload from a
    # sync helper would fail. Only the brand block (redirect base) is read.
    home_settings = dict(org.settings or {})

    def fail(code: str):
        return _home(slug, home_settings, erp_error=code)

    if params.get("error"):
        # The admin declined consent (or the provider refused the request).
        return fail("access_denied")

    spec = erp_oauth.load_providers().get(bound["provider"])
    if spec is None:
        return fail("unknown_provider")

    # The admin who started this must still be an active admin of this org.
    user = (
        await db.execute(
            select(User)
            .options(selectinload(User.roles))
            .where(User.id == uuid.UUID(bound["user_id"]))
        )
    ).scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or user.organization_id != org.id
        or ROLE_ADMIN not in {r.name for r in (user.roles or [])}
    ):
        return fail("not_authorized")

    try:
        await ensure_live_erp_entitled(db, org.id, _direct(spec.key))
    except HTTPException:
        return fail("plan_required")

    code = params.get("code") or ""
    if not code:
        return fail("missing_code")
    resolved = await _resolved_erp_or_none(org, db)
    if resolved is None and bound["client_source"] != "platform":
        # The tenant's own app secret is sealed and the store is unavailable.
        return fail("credentials_unavailable")
    creds = erp_oauth.resolve_client_credentials(
        spec, resolved or {}, source=bound["client_source"] or None
    )
    if creds is None:
        return fail("provider_unavailable")

    try:
        token_response = await erp_oauth.exchange_code(spec, creds, code, erp_settings=resolved)
    except erp_oauth.ErpNotConnectedError:
        logger.warning("erp_oauth: %s code exchange failed for org %s", spec.key, org.id)
        return fail("token_exchange_failed")

    from app.services.erp_adapters.dispatcher import _ADAPTER_REGISTRY
    from app.services.erp_adapters.oauth_base import OAuthErpAdapter

    adapter_cls = _ADAPTER_REGISTRY.get(spec.key)
    external_tenant_id = None
    if adapter_cls is not None and issubclass(adapter_cls, OAuthErpAdapter):
        try:
            external_tenant_id = await adapter_cls.resolve_external_tenant_id(
                access_token=str(token_response["access_token"]),
                token_response=token_response,
                callback_params=params,
            )
        except Exception:  # noqa: BLE001 — any lookup failure refuses the connect
            logger.warning("erp_oauth: %s company lookup failed for org %s", spec.key, org.id)
            external_tenant_id = None
    if not external_tenant_id:
        return fail("no_external_tenant")

    # The check-and-write must be atomic ACROSS tenants: the org row lock below
    # serialises this tenant only, so two tenants' callbacks for one company
    # would both pass the check. `realm_claim` holds a Redis lock on
    # (provider, company) from the check through the commit.
    async with erp_oauth.realm_claim(spec.key, external_tenant_id) as claimed:
        if not claimed:
            return fail("already_linked")
        org = await lock_organization(db, org)
        if await _realm_claimed_elsewhere(db, spec.key, external_tenant_id, org.id):
            await db.rollback()
            return fail("already_linked")
        try:
            await _link(db, org, user, spec, token_response, external_tenant_id, creds.source)
        except CredentialCryptoError:
            await db.rollback()
            return fail("credentials_unavailable")
        except HTTPException:
            # The audit row could not be written, so nothing was linked.
            await db.rollback()
            return fail("audit_unavailable")
        await db.commit()

    return _home(slug, org.settings, erp_connected=spec.key)


async def _link(
    db: AsyncSession,
    org: Organization,
    user: User,
    spec,
    token_response: dict,
    external_tenant_id: str,
    source: str,
) -> None:
    """Write the new connection (uncommitted): metadata into ``settings.erp``,
    tokens into the sealed store, audit row first.

    Keeps this provider's own saved fields (environment, bring-your-own app);
    a different ERP's configuration AND its sealed secrets are dropped rather
    than left under the new type.
    """
    current = dict(org.settings or {})
    prior_erp = current.get("erp")
    prior_erp = dict(prior_erp) if isinstance(prior_erp, dict) else {}
    same_type = prior_erp.get("type") == spec.key
    erp = {k: v for k, v in prior_erp.items() if k != "oauth"} if same_type else {}
    erp["type"] = spec.key
    erp["integration_method"] = "direct"
    block, sealed = erp_oauth.new_connection(
        spec=spec,
        token_response=token_response,
        external_tenant_id=external_tenant_id,
        org_id=org.id,
        client_source=source,
    )
    erp["oauth"] = block
    current["erp"] = erp
    org.settings = current
    flag_modified(org, "settings")

    # A token the new consent did not supply must not survive from the previous
    # one: the new `connection_id` would pair with another consent's grant.
    to_clear = [p for p in erp_oauth.TOKEN_PATHS if p not in sealed]
    if not same_type:
        to_clear += sorted(provider_credentials.SECRET_FIELDS["erp"])

    async def _audit_first(changed: list[str]) -> None:
        await _record_or_503(
            org.id,
            user.id,
            "organization.erp_connected",
            {
                "provider": spec.key,
                "client_source": source,
                "replaced_type": prior_erp.get("type") if not same_type else None,
                "changed": changed,
            },
        )

    await provider_credentials.update_secrets(
        db, org.id, "erp", sealed, to_clear, before_write=_audit_first
    )
