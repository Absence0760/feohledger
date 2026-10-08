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
3. The callback exchanges the code, stores ``settings.erp.oauth`` and 302s the
   browser back to the tenant's own origin (``tenant_urls.tenant_base_url`` —
   the same resolver every outbound tenant link uses) at
   ``/organization?section=erp&erp_connected=<provider>`` or
   ``…&erp_error=<stable_code>``. Never a token, code or provider message.

``POST /api/organization/erp/oauth/disconnect`` revokes best-effort and clears
the block; ``GET /api/organization/erp/oauth/status`` reports it without tokens.
Connect and disconnect are audited (``organization.erp_connected`` /
``organization.erp_disconnected``). Token handling: ``services/erp_oauth``.
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
from app.services import erp_oauth
from app.services.audit_dispatch import dispatch_auth_audit
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


@router.get("/status", response_model=OAuthStatusResponse)
async def oauth_status(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),  # noqa: ARG001 - RBAC gate
):
    erp = (org.settings or {}).get("erp") or {}
    oauth = erp.get("oauth") if isinstance(erp, dict) else None
    providers = []
    for spec in sorted(erp_oauth.load_providers().values(), key=lambda s: s.key):
        creds = erp_oauth.resolve_client_credentials(spec, erp)
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
    creds = erp_oauth.resolve_client_credentials(spec, (org.settings or {}).get("erp"))
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
    """Revoke at the provider (best-effort) and remove ``settings.erp.oauth``.

    Not plan-gated: a downgraded tenant must always be able to cut the link.
    Idempotent — with nothing connected it answers ``disconnected: false``.
    """
    org = await lock_organization(db, org)
    current = dict(org.settings or {})
    erp = dict(current.get("erp") or {})
    oauth = erp.get("oauth")
    if not isinstance(oauth, dict):
        await db.rollback()
        return DisconnectResponse(disconnected=False, revoked=False)

    provider = str(oauth.get("provider") or "")
    erp.pop("oauth", None)
    current["erp"] = erp
    org.settings = current
    flag_modified(org, "settings")
    await db.commit()

    # Revoke AFTER the commit: the provider's latency is not charged to a
    # transaction holding the org row lock, and a failed revoke must not leave
    # the tenant believing it is still connected.
    revoked = False
    spec = erp_oauth.load_providers().get(provider)
    if spec is not None:
        revoked = await erp_oauth.revoke(spec, erp, oauth)

    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="organization.erp_disconnected",
        entity_id=org.id,
        entity_type="organization",
        details={"provider": provider, "revoked": revoked},
    )
    return DisconnectResponse(disconnected=True, revoked=revoked)


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
    creds = erp_oauth.resolve_client_credentials(
        spec, (org.settings or {}).get("erp"), source=bound["client_source"] or None
    )
    if creds is None:
        return fail("provider_unavailable")

    try:
        token_response = await erp_oauth.exchange_code(
            spec, creds, code, erp_settings=(org.settings or {}).get("erp")
        )
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
        prior_erp = _link(org, spec, token_response, external_tenant_id, creds.source)
        await db.commit()

    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="organization.erp_connected",
        entity_id=org.id,
        entity_type="organization",
        details={
            "provider": spec.key,
            "client_source": creds.source,
            "replaced_type": prior_erp.get("type") if prior_erp.get("type") != spec.key else None,
        },
    )
    return _home(slug, org.settings, erp_connected=spec.key)


def _link(org: Organization, spec, token_response: dict, external_tenant_id: str, source: str):
    """Write the new connection into ``org.settings`` (uncommitted). Returns the
    prior ``settings.erp`` for the audit row."""
    current = dict(org.settings or {})
    prior_erp = current.get("erp")
    prior_erp = dict(prior_erp) if isinstance(prior_erp, dict) else {}
    # Keep this provider's own saved fields (environment, BYO app); drop a
    # different ERP's credentials rather than leave them under the new type.
    erp = (
        {k: v for k, v in prior_erp.items() if k != "oauth"}
        if prior_erp.get("type") == spec.key
        else {}
    )
    erp["type"] = spec.key
    erp["integration_method"] = "direct"
    erp["oauth"] = erp_oauth.new_connection_block(
        spec=spec,
        token_response=token_response,
        external_tenant_id=external_tenant_id,
        org_id=org.id,
        client_source=source,
    )
    current["erp"] = erp
    org.settings = current
    flag_modified(org, "settings")
    return prior_erp
