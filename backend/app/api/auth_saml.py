"""SAML 2.0 SSO — Service-Provider endpoints (SP-initiated).

Additive, separate code path from OIDC (`auth_sso.py`); the two share the
identity-provisioning + session-mint tail (`services/identity_provisioning.py`)
and differ only in how they VERIFY the IdP response. SAML verification is
delegated to python3-saml (in-process XML-DSig via python-xmlsec), pinned to
the tenant's pre-registered signing cert.

Flow (SP-initiated, HTTP-POST ACS binding):
    1. GET /api/auth/saml/login[?slug=<tenant>]  (public)
       Resolve the tenant — by `?slug=`, or from the request `Host` against
       `settings.brand.custom_domains` when the tenant is served on its own
       white-label vanity hostname — then its IdP config, build the AuthnRequest, mint a
       RelayState bound to {tenant, AuthnRequest-ID} in Redis (single-use),
       302 the browser to the IdP.
    2. IdP authenticates the user (IdP owns MFA — we skip our own challenge).
    3. POST /api/auth/saml/acs  (public, form-encoded)
       Verify the SAMLResponse signature against the configured cert + all
       SAML conditions; recover the tenant from RelayState (NEVER the IdP);
       dedupe the assertion to block replay; JIT-provision; mint our JWT;
       303-redirect to the SPA bridge with a one-time handoff code.
    4. POST /api/auth/saml/exchange  (public)
       The SPA bridge swaps the one-time code for the JWT in the response body.

The ACS / exchange split keeps the JWT out of every URL — it is only ever
returned in a POST response body, like the OIDC callback.

The trust anchor is the per-tenant `idp_x509_cert` from settings (the SAML
analog of OIDC JWKS pinning + webhook HMAC). Every rejection path fails closed
to ONE generic error + a PII-safe audit row — no raw assertion / NameID /
attribute value ever enters a log or the audit trail.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.errors import OneLogin_Saml2_Error
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from onelogin.saml2.xml_utils import OneLogin_Saml2_XML
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import create_access_token_with_jti
from app.config import settings
from app.database import get_control_db
from app.models.organization import Organization
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.identity_provisioning import (
    DeactivatedAccount,
    EmailDomainNotAllowed,
    UnsafeEmailAddress,
    extract_and_check_email,
    jit_provision,
)
from app.services.rate_limit import resolve_client_ip
from app.services.session_management import register_session
from app.services.sso import (
    ResolvedSAMLConfig,
    SSOConfigError,
    SSOValidationError,
    consume_saml_handoff,
    consume_saml_relay_state,
    create_saml_handoff,
    is_sso_only,
    resolve_saml_config,
    resolve_sso_tenant_slug,
    saml_acs_url,
    saml_bridge_url,
    store_saml_relay_state,
)
from app.services.webhook_security import is_event_already_processed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/saml", tags=["auth-saml"])

# SAML binding / format / algorithm URIs
_REDIRECT_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
_POST_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
_NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
_RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
_SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"

# Attribute names email / display name commonly arrive under (friendly names +
# the SAML/LDAP OIDs + the WS-Fed claim URIs Keycloak/ADFS/Entra emit).
_EMAIL_ATTRS = (
    "email",
    "Email",
    "emailAddress",
    "mail",
    "urn:oid:0.9.2342.19200300.100.1.3",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
)
_NAME_ATTRS = (
    "displayName",
    "name",
    "cn",
    "urn:oid:2.16.840.1.113730.3.1.241",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
)
_GIVEN_ATTRS = ("givenName", "firstName", "urn:oid:2.5.4.42")
_FAMILY_ATTRS = ("sn", "surname", "lastName", "urn:oid:2.5.4.4")


class SAMLConfigPublic(BaseModel):
    """Unauthenticated config surface for the login page. Pinned to
    {enabled, provider, sso_only} — NEVER leaks idp_x509_cert / sp_entity_id /
    URLs. A field-allowlist test guards this contract. `sso_only` lets the page
    hide the password form; only ever true alongside `enabled=True`."""

    enabled: bool = False
    provider: str | None = None
    sso_only: bool = False


class SAMLExchangeRequest(BaseModel):
    code: str


class SAMLExchangeResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False
    tenant_slug: str


# ---------------------------------------------------------------------------


async def _fetch_org_by_slug(slug: str, db: AsyncSession) -> Organization:
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="Unknown tenant.")
    return org


async def _resolve_org(
    slug: str | None, host: str | None, db: AsyncSession
) -> tuple[Organization, str]:
    """Resolve the tenant for a PUBLIC SAML entry point, by `?slug=` or `Host`.

    The OIDC twin of this lives in `auth_sso.py` and both call the one shared
    `services/sso.resolve_sso_tenant_slug`, which is itself a thin wrapper over
    the `app/tenant.py` custom-domain resolver — so a vanity hostname resolves
    identically for a page load, an OIDC login and a SAML login.

    **The failure posture is deliberately unchanged.** An unresolvable `Host`
    raises the *identical* 404 `_fetch_org_by_slug` already raises for an
    unknown `?slug=`, so nothing new distinguishes "no such tenant" from "SAML
    is not configured".
    """
    resolved = await resolve_sso_tenant_slug(slug, host, db)
    if not resolved:
        raise HTTPException(status_code=404, detail="Unknown tenant.")
    return await _fetch_org_by_slug(resolved, db), resolved


def _resolve_saml_or_none(org_settings: dict | None, slug: str) -> ResolvedSAMLConfig | None:
    """`resolve_saml_config`, but a block with `enabled` set that fails to
    resolve (`SSOConfigError`) reads exactly like a genuinely absent one — the
    same posture `saml_config` below already takes. Keeps `saml_login`,
    `saml_acs` and `saml_metadata` on their documented 400/404 instead of an
    uncaught 500, and never echoes `SSOConfigError.fields` (the offending
    keys) to the caller."""
    try:
        return resolve_saml_config(org_settings, slug)
    except SSOConfigError:
        return None


def _build_saml_settings(config: ResolvedSAMLConfig) -> dict:
    """Assemble the python3-saml settings dict.

    The `security` block is explicit on purpose — python3-saml ships most of
    these checks OFF by default. `wantAssertionsSigned` makes the assertion
    signature mandatory; `rejectDeprecatedAlgorithm` rejects SHA-1 / weak
    digests; `rejectUnsolicited` requires an InResponseTo (we are SP-initiated
    only). The IdP signing cert is pinned from settings (x509certMulti when the
    tenant lists rotation certs) — never a fingerprint, never a cert embedded
    in the document.
    """
    idp: dict = {
        "entityId": config.idp_entity_id,
        "singleSignOnService": {"url": config.idp_sso_url, "binding": _REDIRECT_BINDING},
    }
    if config.idp_x509_cert_multi:
        idp["x509certMulti"] = {"signing": [config.idp_x509_cert, *config.idp_x509_cert_multi]}
    else:
        idp["x509cert"] = config.idp_x509_cert

    sp_signs_authn = bool(settings.saml_sp_private_key and settings.saml_sp_cert)

    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": config.sp_entity_id,
            "assertionConsumerService": {"url": saml_acs_url(), "binding": _POST_BINDING},
            "NameIDFormat": _NAMEID_EMAIL,
            "x509cert": settings.saml_sp_cert or "",
            "privateKey": settings.saml_sp_private_key or "",
        },
        "idp": idp,
        "security": {
            "wantAssertionsSigned": True,
            "wantMessagesSigned": False,
            "wantNameId": True,
            "wantNameIdEncrypted": False,
            "wantAssertionsEncrypted": False,
            "rejectDeprecatedAlgorithm": True,
            "requestedAuthnContext": False,
            "signatureAlgorithm": _RSA_SHA256,
            "digestAlgorithm": _SHA256,
            "authnRequestsSigned": sp_signs_authn,
            "wantAttributeStatement": False,
            # NB: python3-saml validates InResponseTo only when the response
            # CARRIES one (an unsolicited response slips through), so we enforce
            # its presence + match explicitly in the ACS handler below — we are
            # SP-initiated only.
        },
    }


def _acs_request_data(post_data: dict) -> dict:
    """Build the python3-saml request dict from the TRUSTED, configured ACS URL
    — never from spoofable Host / X-Forwarded-* headers. This is what python3-
    saml validates the SAMLResponse `Destination` against, so it must reflect
    server config, not the inbound request line."""
    acs = urlparse(saml_acs_url())
    return {
        "https": "on" if acs.scheme == "https" else "off",
        "http_host": acs.netloc,
        "script_name": acs.path,
        "get_data": {},
        "post_data": post_data,
    }


def _extract_email_and_name(nameid: str | None, attributes: dict) -> tuple[str | None, str | None]:
    """Pull email + display name out of the NameID / attribute statement.
    Email comes from an emailAddress NameID, else a known email attribute."""
    email = nameid if (nameid and "@" in nameid) else None
    if not email:
        for key in _EMAIL_ATTRS:
            vals = attributes.get(key)
            if vals:
                email = vals[0]
                break

    name = None
    for key in _NAME_ATTRS:
        vals = attributes.get(key)
        if vals:
            name = vals[0]
            break
    if not name:
        first = next((attributes[k][0] for k in _GIVEN_ATTRS if attributes.get(k)), "")
        last = next((attributes[k][0] for k in _FAMILY_ATTRS if attributes.get(k)), "")
        name = f"{first} {last}".strip() or None
    return email, name


def _assertion_issuer(auth: OneLogin_Saml2_Auth) -> str | None:
    """Return the <Assertion><Issuer> value from the just-validated response.

    Uses python3-saml's own XML helper (forbid_dtd / forbid_entities — XXE/DTD
    hardened), never raw lxml, on the already-signature-verified response XML.
    Returns None if it can't be extracted, which the caller treats as a
    mismatch (fail closed)."""
    try:
        doc = OneLogin_Saml2_XML.to_etree(auth.get_last_response_xml())
        nodes = OneLogin_Saml2_XML.query(doc, "//saml:Assertion/saml:Issuer")
        for node in nodes:
            if node.text:
                return node.text.strip()
    except Exception:  # noqa: BLE001 — extraction failure => treated as mismatch
        return None
    return None


def _response_in_response_to(auth: OneLogin_Saml2_Auth) -> str | None:
    """Return the <Response> @InResponseTo, or None if absent. Lets the handler
    REQUIRE its presence (SP-initiated only) — python3-saml validates the value
    only when present, so an unsolicited response would otherwise slip through.
    Parsed with the DTD/entity-hardened helper, never raw lxml."""
    try:
        doc = OneLogin_Saml2_XML.to_etree(auth.get_last_response_xml())
        return doc.get("InResponseTo")
    except Exception:  # noqa: BLE001 — absent/unparseable => treated as missing
        return None


# ---------------------------------------------------------------------------


@router.get("/config", response_model=SAMLConfigPublic)
async def saml_config(
    slug: str | None = None,
    host: str | None = Header(default=None),
    db: AsyncSession = Depends(get_control_db),
):
    """Public — lets the login page decide whether to render the SAML button.
    Returns only the non-secret {enabled, provider}.

    `slug` is optional: on a tenant's vanity host the SPA has no slug, so the
    tenant is resolved from the request `Host` instead."""
    org, slug = await _resolve_org(slug, host, db)
    try:
        config = resolve_saml_config(org.settings, slug)
    except SSOConfigError:
        return SAMLConfigPublic(enabled=False)
    if config is None:
        return SAMLConfigPublic(enabled=False)
    return SAMLConfigPublic(
        enabled=True, provider=config.provider, sso_only=is_sso_only(org.settings)
    )


@router.get("/login")
async def saml_login(
    slug: str | None = None,
    host: str | None = Header(default=None),
    db: AsyncSession = Depends(get_control_db),
):
    """SP-initiated entry: build the AuthnRequest, bind RelayState to
    {tenant, request_id}, 302 to the IdP.

    `slug` is optional. When it is absent the tenant is resolved from the
    request `Host` against its registered custom domains — that is what lets
    the SAML button work at all on a white-label vanity hostname, which has no
    slug anywhere in the URL. An unresolvable tenant gets the same 404 an
    unknown slug has always produced (see `_resolve_org`).

    The SP EntityID and ACS URL are unaffected: both are derived from
    `FEOH_API_PUBLIC_URL` and registered at the IdP, so a vanity `Host` never
    re-points them."""
    org, slug = await _resolve_org(slug, host, db)
    config = _resolve_saml_or_none(org.settings, slug)
    if config is None:
        raise HTTPException(status_code=400, detail="SAML SSO is not configured for this tenant.")

    # Defensive: the IdP SSO URL is admin-configured, but validate its shape so
    # a malformed value can't produce a junk 302 target.
    parsed = urlparse(config.idp_sso_url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="SAML SSO is not configured correctly.")

    # Mint the RelayState first so python3-saml bakes it into the redirect as
    # the RelayState param the IdP echoes back to the ACS.
    state = secrets.token_urlsafe(24)
    try:
        auth = OneLogin_Saml2_Auth(_acs_request_data({}), old_settings=_build_saml_settings(config))
        redirect_url = auth.login(return_to=state)
    except OneLogin_Saml2_Error:
        logger.warning("SAML login build failed for slug %s", slug)
        raise HTTPException(status_code=400, detail="SAML SSO is not configured correctly.")

    # The AuthnRequest ID is generated inside login(); bind it to the RelayState
    # so the ACS can enforce InResponseTo against THIS request (single record).
    await store_saml_relay_state(state, slug, auth.get_last_request_id())
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/acs")
async def saml_acs(request: Request, db: AsyncSession = Depends(get_control_db)):
    """Assertion Consumer Service. Verifies the signed SAMLResponse, recovers
    the tenant from server-minted RelayState, blocks replay, JIT-provisions,
    mints our JWT, and 303-redirects to the SPA bridge with a one-time code."""
    ip = resolve_client_ip(request)
    form = await request.form()
    saml_response = form.get("SAMLResponse")
    relay_state = form.get("RelayState")
    if not saml_response or not relay_state:
        raise HTTPException(status_code=400, detail="Malformed SAML response.")

    # Tenant comes ONLY from the server-minted, single-use RelayState — never
    # from the IdP-supplied Audience/slug. Invalid RelayState => no tenant to
    # scope an audit row to; fail generically (mirrors the OIDC bad-state path).
    try:
        bound = await consume_saml_relay_state(str(relay_state))
    except SSOValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tenant_slug = bound["tenant"]
    request_id = bound.get("request_id")
    # A missing request_id would let python3-saml skip the InResponseTo check —
    # hard-reject rather than ever calling process_response(request_id=None).
    if not request_id:
        raise HTTPException(status_code=400, detail="Login session was incomplete. Try again.")

    org = await _fetch_org_by_slug(tenant_slug, db)
    config = _resolve_saml_or_none(org.settings, tenant_slug)
    if config is None:
        raise HTTPException(status_code=400, detail="SAML SSO is not configured for this tenant.")

    async def _fail(reason: str) -> HTTPException:
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=None,
            action="auth.saml.login.failure",
            details={"tenant": tenant_slug, "ip": ip, "reason": reason},
        )
        return HTTPException(status_code=400, detail="SAML login could not be verified.")

    # --- Signature + conditions + audience + destination + InResponseTo ------
    try:
        auth = OneLogin_Saml2_Auth(
            _acs_request_data({"SAMLResponse": saml_response, "RelayState": relay_state}),
            old_settings=_build_saml_settings(config),
        )
        auth.process_response(request_id=request_id)
    except Exception:  # noqa: BLE001 — any parse/verify error fails closed to one generic error
        logger.warning("SAML ACS: response processing raised for tenant %s", tenant_slug)
        raise await _fail("assertion_invalid")

    if auth.get_errors() or not auth.is_authenticated():
        # get_last_error_reason() is python3-saml's own reason string (e.g.
        # "Signature validation failed") — not PII; safe at WARN.
        logger.warning(
            "SAML ACS rejected for tenant %s: %s", tenant_slug, auth.get_last_error_reason()
        )
        raise await _fail("assertion_invalid")

    # --- Defence-in-depth: pin the Issuer to the configured IdP --------------
    # python3-saml validates Audience (== sp_entity_id), Destination, Conditions
    # and InResponseTo in strict mode; we ALSO require the Assertion Issuer to
    # equal the tenant's configured IdP so an assertion minted by a DIFFERENT
    # tenant's IdP can't be cross-replayed. Extracted with python3-saml's own
    # DTD/entity-hardened parser (forbid_dtd/forbid_entities) — never raw lxml.
    if _assertion_issuer(auth) != config.idp_entity_id:
        raise await _fail("issuer_mismatch")

    # Require an InResponseTo that matches the AuthnRequest we issued — rejects
    # unsolicited / IdP-initiated responses (python3-saml only checks it when
    # present, so presence is enforced here).
    if _response_in_response_to(auth) != request_id:
        raise await _fail("unsolicited")

    # --- Replay protection: dedupe both Assertion + Response IDs --------------
    # Scoped per-tenant (assertion IDs are only unique within an issuer, so a
    # global keyspace would let one tenant's replay block another's first login).
    assertion_id = auth.get_last_assertion_id()
    message_id = auth.get_last_message_id()
    dedup_scope = f"saml:{tenant_slug}"
    for event_id in (assertion_id, message_id):
        if event_id and await is_event_already_processed(dedup_scope, event_id):
            raise await _fail("replay")

    # --- Identity ------------------------------------------------------------
    email_raw, name = _extract_email_and_name(auth.get_nameid(), auth.get_attributes())
    sub = auth.get_nameid()
    if not email_raw or not sub:
        raise await _fail("missing_email")

    try:
        # NOTE: unlike the OIDC path, the SAML failure audit deliberately omits
        # the email (PII-out-of-logs — tighter than the OIDC precedent).
        email = extract_and_check_email(email_raw, config.allowed_email_domains)
    except UnsafeEmailAddress as exc:
        # A control character in an asserted address — never legitimate, and
        # the value would become a login and a mail destination.
        raise await _fail("unsafe_email") from exc
    except EmailDomainNotAllowed as exc:
        raise await _fail("domain_blocked") from exc

    try:
        user = await jit_provision(db, org, email, sub, config.provider, {"name": name})
    except DeactivatedAccount as exc:
        # The IdP vouched for them, but the app account is offboarded. Refuse
        # rather than minting a token `get_current_user` would reject on every
        # subsequent call. `_fail` keeps the PII-free audit + the one generic
        # error this endpoint returns for every rejection.
        raise await _fail("inactive") from exc

    token, jti = create_access_token_with_jti(user.id, user.organization_id)
    await register_session(
        user.id,
        jti,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
        method=f"saml:{config.provider}",
    )
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="auth.saml.login.success",
        entity_id=user.id,
        details={"tenant": tenant_slug, "ip": ip, "provider": config.provider},
    )

    # Hand the JWT off via a one-time code so it never transits the URL.
    code = await create_saml_handoff(token, user.must_change_password, tenant_slug)
    bridge = saml_bridge_url(tenant_slug, org.settings)
    return RedirectResponse(f"{bridge}?{urlencode({'code': code})}", status_code=303)


@router.post("/exchange", response_model=SAMLExchangeResponse)
async def saml_exchange(body: SAMLExchangeRequest):
    """The SPA bridge swaps the one-time handoff code for the JWT (in the
    response body — never a URL)."""
    try:
        data = await consume_saml_handoff(body.code)
    except SSOValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SAMLExchangeResponse(
        access_token=data["access_token"],
        must_change_password=data["must_change_password"],
        tenant_slug=data["tenant"],
    )


@router.get("/metadata")
async def saml_metadata(slug: str, db: AsyncSession = Depends(get_control_db)):
    """SP EntityDescriptor metadata XML so an admin can register our SP at the
    IdP. No secrets (only the public SP cert if AuthnRequest signing is on)."""
    org = await _fetch_org_by_slug(slug, db)
    config = _resolve_saml_or_none(org.settings, slug)
    if config is None:
        raise HTTPException(status_code=404, detail="SAML SSO is not configured for this tenant.")
    saml_settings = OneLogin_Saml2_Settings(_build_saml_settings(config), sp_validation_only=True)
    metadata = saml_settings.get_sp_metadata()
    errors = saml_settings.validate_metadata(metadata)
    if errors:
        raise HTTPException(status_code=500, detail="Could not generate SP metadata.")
    return Response(content=metadata, media_type="application/xml")
