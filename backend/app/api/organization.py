"""Organization settings endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import (
    ROLE_ADMIN,
    ensure_live_erp_entitled,
    get_current_user,
    require_entitlement,
    require_roles,
)
from app.config import settings
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.organization import (
    BrandConfig,
    CompanyProfile,
    CustomDomainsConfig,
    InvoiceDefaults,
    OrganizationResponse,
    UpdateOrganizationRequest,
)
from app.services import erp_credentials
from app.services.audit_dispatch import dispatch_auth_audit, record_auth_audit_or_raise
from app.services.billing.plan_catalog import FEATURE_SCIM
from app.services.currency_conversion import resolve_reporting_currency
from app.services.data_residency import (
    DEFAULT_REGION,
    SUPPORTED_REGIONS,
    check_residency_alignment,
    get_region_placement,
    resolve_region,
)
from app.services.erp_adapters import catalog as erp_catalog
from app.services.org_settings_view import settings_for_response
from app.services.sso import generate_scim_token
from app.tenant import get_tenant, lock_organization, normalize_custom_domain
from app.utils.credential_crypto import CredentialCryptoError, CredentialKeyMissingError
from app.utils.tenant_urls import is_under_platform_domain

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/organization", tags=["organization"])


def _encrypt_erp_or_refuse(erp: dict) -> dict:
    """``erp`` with its credentials encrypted, or an HTTP refusal.

    No keyring → 503: the server cannot hold the credential, and storing it in
    plaintext is not an option. A value posing as a ciphertext that does not
    decrypt for its field → 422. Neither message carries a value.
    """
    try:
        return erp_credentials.encrypt_erp_config(erp)
    except CredentialKeyMissingError:
        logger.error("ERP credentials not saved: FEOH_CREDENTIAL_ENCRYPTION_KEYS is not set")
        raise HTTPException(
            status_code=503,
            detail=(
                "ERP credentials cannot be saved: credential encryption is not configured "
                "on this server. Ask the operator to set FEOH_CREDENTIAL_ENCRYPTION_KEYS."
            ),
        ) from None
    except CredentialCryptoError:
        raise HTTPException(
            status_code=422, detail="An ERP secret field holds an invalid encrypted value."
        ) from None


class ResidencyAlignmentResponse(BaseModel):
    """Advisory: is the pinned region the one this stack actually runs in?

    Never blocks anything — it reports whether a residency commitment is
    physically honoured today. `aligned` is tri-state: `null` (with
    `status: "unknown"`) when the operator has not declared a usable
    `FEOH_DEPLOYED_REGION`, so "we can't attest" can never be read as "yes".
    """

    status: str  # "aligned" | "misaligned" | "unknown"
    aligned: bool | None  # None ⇔ status == "unknown"
    deployed_region: str | None  # the declared region, once it is a known token
    reason: str | None  # why it's unknown: deployed_region_unset|_unrecognised


class DataResidencyResponse(BaseModel):
    """Where this tenant's data is pinned to live (GDPR/CCPA residency)."""

    region: str  # the tenant's effective residency region (override → default)
    default_region: str  # platform default, so the UI can show "(default)"
    supported_regions: list[str]
    placement: dict[str, str]  # documented DB/object-storage target for `region`
    alignment: ResidencyAlignmentResponse  # advisory configured-vs-deployed check


class UpdateDataResidencyRequest(BaseModel):
    region: str  # must be one of SUPPORTED_REGIONS; validated server-side


class SCIMTokenResponse(BaseModel):
    """Returned ONCE on token generation. The plaintext `token` is never
    re-served — only the sha256 of it is persisted server-side."""

    token: str
    bearer_hash_prefix: str  # first 8 hex chars, useful as a UI identifier


def _is_admin(user: User) -> bool:
    """Whether the caller holds the admin role (roles are eager-loaded by
    `get_current_user`)."""
    return ROLE_ADMIN in {r.name for r in (user.roles or [])}


def _org_response(org: Organization, *, is_admin: bool) -> OrganizationResponse:
    """Serialize the org, projecting `settings` to what this caller may see.

    `is_admin` is REQUIRED, not defaulted: this response carries the tenant's
    third-party credentials, and a default would decide the security question
    silently at every future call site. See `services/org_settings_view`.
    """
    raw = settings_for_response(org.settings, is_admin=is_admin)
    # Ensure company and invoice_defaults have defaults. Mutating in place is
    # safe on the non-admin path (a fresh projection) and on the admin path
    # whenever a redaction copied the dict; when neither applies this touches
    # the live ORM dict exactly as it always has.
    if "company" not in raw:
        raw["company"] = CompanyProfile().model_dump()
    if "invoice_defaults" not in raw:
        raw["invoice_defaults"] = InvoiceDefaults().model_dump()
    # Advisory: is "require MFA for all users" actually enforced right now?
    # `settings.mfa.required=true` is a per-org config toggle, but MFA itself
    # is gated behind the platform master switch (`FEOH_MFA_ENABLED`) — off by
    # default in local dev. Saving `required: true` while the switch is off
    # used to be a silent no-op with no signal anywhere that the toggle was
    # inert. Computed fresh on every read (never persisted) — same shape as
    # the data-residency `alignment` block above.
    mfa_cfg = raw.get("mfa")
    if isinstance(mfa_cfg, dict):
        raw["mfa"] = {
            **mfa_cfg,
            "enforcement_active": bool(mfa_cfg.get("required")) and settings.mfa_enabled,
        }
    return OrganizationResponse(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        plan=org.plan,
        settings=raw,
        created_at=org.created_at.isoformat() if org.created_at else "",
        # Computed from the RAW settings, not `raw` above: the three rungs it
        # can read (`reporting_currency`, `payments.home_currency`,
        # `invoice_defaults.currency`) are already admitted to every role by
        # `NON_ADMIN_SETTINGS`, and the fourth rung is operator config, not
        # tenant data — there is nothing here a non-admin projection would
        # need to strip.
        resolved_reporting_currency=resolve_reporting_currency(org.settings),
    )


@router.get("", response_model=OrganizationResponse)
async def get_organization(
    org: Organization = Depends(get_tenant),
    user: User = Depends(get_current_user),
):
    """Return the org + the settings this caller's role may read.

    Open to any authenticated org user because the whole app reads
    `invoice_defaults.currency` from here — but a non-admin now gets an
    allow-listed projection, not the raw JSONB. Before that, every role could
    read the tenant's ERP / payment / card / extraction / SSO credentials and
    the Slack-Teams webhook URL straight out of this response.

    `resolved_reporting_currency` carries the server's own answer to "what
    currency is this org's reporting denominated in" — the same function every
    cross-currency rollup calls, all four resolution rungs included. Every
    role gets it; a client that previously had to guess (the web `orgCurrency`
    store, mobile's `OrgCurrencyStore`) reads it first now, and its own
    three-rung resolution stays only as a fallback for an older cached
    response.
    """
    return _org_response(org, is_admin=_is_admin(user))


@router.get("/fraud-rules/defaults")
async def get_fraud_rule_defaults(
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Return the canonical fraud-rule defaults baked into the warning
    engine. The Org Settings UI uses this as a starting point so a stale
    UI can't drift from what the engine actually evaluates."""
    from app.services.invoice_warnings import DEFAULT_FRAUD_RULES

    return DEFAULT_FRAUD_RULES


def _validate_settings_patch(incoming: dict) -> None:
    """Reject the specific type-confusion holes a careless PATCH can sneak
    through for the well-known settings sub-keys this endpoint understands.

    This is deliberately NOT a schema for the whole freeform `settings` bag —
    that JSONB blob is intentionally open-ended (see `org_settings_view.py`'s
    module docstring on why an allow-list, not a full schema, is the right
    shape here too). It only closes the specific type holes that would
    otherwise persist silently and corrupt a value every other reader assumes
    is well-typed:

    * `invoice_defaults.currency` — must be a 3-letter alpha string (ISO-4217
      shape). Every currency formatter downstream (`<Money>`, the reporting
      rollups) assumes this; a stray int or a 2-char typo would corrupt every
      invoice created under the default from then on.
    * `payments.cfo_approval_above` — must be numeric (int/float, not `bool`
      — `bool` is a `int` subclass in Python and would otherwise sneak past a
      bare `isinstance(x, (int, float))` check) when present. It gates the
      CFO-approval threshold on every payment run; a non-numeric value would
      make that comparison raise or silently misbehave at the worst possible
      moment.

    Raises `HTTPException(422)` naming the offending field; returns `None`
    when everything present is well-typed.
    """
    invoice_defaults = incoming.get("invoice_defaults")
    if isinstance(invoice_defaults, dict) and "currency" in invoice_defaults:
        currency = invoice_defaults["currency"]
        if (
            not isinstance(currency, str)
            or len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
        ):
            raise HTTPException(
                status_code=422,
                detail="invoice_defaults.currency must be a 3-letter currency code (e.g. 'USD').",
            )

    payments_cfg = incoming.get("payments")
    if isinstance(payments_cfg, dict) and "mode" in payments_cfg:
        # An unknown mode resolves record-only anyway (fail closed —
        # `services/payment_execution_mode`), but a typo the admin can't see
        # take effect is worse than a 422 that names the two values.
        from app.services.payment_execution_mode import PAYMENT_MODES

        if payments_cfg["mode"] is not None and payments_cfg["mode"] not in PAYMENT_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"payments.mode must be one of: {', '.join(PAYMENT_MODES)}.",
            )
    if isinstance(payments_cfg, dict) and payments_cfg.get("nacha") is not None:
        # The NACHA originator block (`services/nacha`). Validated at save so a
        # bad company ID or a mistyped routing number surfaces here, not as a
        # file the customer's bank rejects. Field names only in the message.
        from app.services.nacha import originator_problems

        problems = originator_problems(payments_cfg["nacha"])
        if problems:
            raise HTTPException(
                status_code=422,
                detail=f"payments.nacha has invalid fields: {', '.join(problems)}.",
            )
    if isinstance(payments_cfg, dict) and "cfo_approval_above" in payments_cfg:
        threshold = payments_cfg["cfo_approval_above"]
        if threshold is not None and (
            isinstance(threshold, bool) or not isinstance(threshold, (int, float))
        ):
            raise HTTPException(
                status_code=422,
                detail="payments.cfo_approval_above must be a number.",
            )


@router.patch("", response_model=OrganizationResponse)
async def update_organization(
    body: UpdateOrganizationRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    # The merge below writes the whole settings dict back, so read it under the
    # row lock (`tenant.lock_organization`) or a concurrent audited writer —
    # `PUT /organization/sso`, the SCIM group writes — is silently reverted.
    org = await lock_organization(db, org)
    if body.name is not None:
        org.name = body.name

    if body.settings is not None:
        _validate_settings_patch(body.settings)

        # Saving a LIVE ERP adapter is a Growth feature (decisions §258). Only
        # the `erp` key this PATCH carries is checked: re-saving the company
        # profile on a downgraded tenant whose stored ERP is live must still
        # work, and clearing the key or choosing `mock` is never refused.
        # `settings.erp` secrets are write-only (`erp_adapters/catalog`): a
        # blank or masked secret keeps the stored one, and the OAuth token block
        # is never taken from a PATCH — `services/erp_oauth` is its only writer.
        # Resolve the block to what will actually be stored BEFORE the plan
        # gate, so the gate judges the real config.
        erp_before = (org.settings or {}).get("erp")
        erp_changed: list[str] | None = None
        if "erp" in body.settings:
            incoming_erp = body.settings.get("erp")
            if incoming_erp is not None and not isinstance(incoming_erp, dict):
                raise HTTPException(status_code=422, detail="erp must be an object.")
            if isinstance(incoming_erp, dict):
                merged_erp = erp_catalog.merge_erp_update(erp_before, incoming_erp)
            else:
                # Clearing the ERP still keeps the OAuth block: only the OAuth
                # disconnect removes it (it revokes at the provider first).
                merged_erp = {}
                if isinstance(erp_before, dict) and erp_catalog.OAUTH_KEY in erp_before:
                    merged_erp[erp_catalog.OAUTH_KEY] = erp_before[erp_catalog.OAUTH_KEY]
            # The one encrypt on the settings way in: every secret is stored as
            # ciphertext (`services/erp_credentials`), kept ones unchanged.
            merged_erp = _encrypt_erp_or_refuse(merged_erp)
            if isinstance(incoming_erp, dict):
                body.settings["erp"] = merged_erp
            else:
                body.settings["erp"] = merged_erp or None
            await ensure_live_erp_entitled(db, org.id, body.settings.get("erp"))
            # Compared by VALUE: encrypting a legacy plaintext secret a save
            # keeps is not a change the audit row should name.
            try:
                erp_changed = erp_catalog.changed_keys(
                    erp_credentials.plain_view(erp_before), erp_credentials.plain_view(merged_erp)
                )
            except CredentialCryptoError:
                erp_changed = erp_catalog.changed_keys(erp_before, merged_erp)

        # The chat webhook URL has one sanctioned writer — the audited
        # `PUT /api/organization/chat-notifications/webhook`. This generic merge
        # would otherwise be a second, unaudited way to set the credential, and
        # a shallow `update()` here also silently replaces the WHOLE
        # chat_notifications block (dropping the webhook while "saving the
        # provider"). Refuse the key and name the endpoint that owns it.
        if "chat_notifications" in body.settings:
            raise HTTPException(
                status_code=422,
                detail=(
                    "chat_notifications is managed by "
                    "/api/organization/chat-notifications (and its /webhook "
                    "sub-resource for the incoming-webhook URL), so that every "
                    "change to it is audited."
                ),
            )
        # SSO has one sanctioned writer too — the audited
        # `PUT /api/organization/sso`. The shallow `update()` below replaces the
        # WHOLE `sso` key, so a `{"sso": {"client_secret": ...}}` PATCH used to
        # drop `enabled`, `sso_only`, the rest of the IdP config and the SCIM
        # group state, with no audit row. Refuse the key and name the endpoint.
        if "sso" in body.settings:
            raise HTTPException(
                status_code=422,
                detail=(
                    "sso is managed by /api/organization/sso, which keeps the stored "
                    "client secret and the SCIM settings across a save and audits every "
                    "change."
                ),
            )
        # `billing` is the platform's own billing of THIS tenant, not a tenant
        # preference, and every value in it now drives a charge: the provider
        # overage is reported to, the spending cap, and the per-period
        # reported-overage markers that stop a unit being billed twice (decisions
        # §255). A tenant admin must not set any of it — the shallow `update()`
        # below would also replace the whole block and drop the markers. The cap
        # has its own audited writer; the rest is server- or operator-owned.
        if "billing" in body.settings:
            raise HTTPException(
                status_code=422,
                detail=(
                    "billing is not a tenant setting. The spending cap is managed by "
                    "PUT /api/billing/spending-cap; the rest is set by the platform."
                ),
            )
        # Custom domains have one sanctioned writer too — the audited
        # `PUT /api/organization/branding/custom-domains`, which normalizes each
        # host through the tenant resolver's own `normalize_custom_domain`, takes
        # an advisory lock, and refuses a host already claimed by another org.
        # The shallow `update()` below replaces the WHOLE `brand` key, so a
        # `{"brand": {"custom_domains": [...]}}` PATCH would write the list with
        # none of those checks — an unaudited way to claim another tenant's
        # vanity hostname and make resolution ambiguous. Refuse the key.
        incoming_brand = body.settings.get("brand")
        if isinstance(incoming_brand, dict) and "custom_domains" in incoming_brand:
            raise HTTPException(
                status_code=422,
                detail=(
                    "brand.custom_domains is managed by "
                    "/api/organization/branding/custom-domains, so that every "
                    "change to it is normalized, checked for cross-tenant "
                    "conflicts, and audited."
                ),
            )

        # "Require MFA for all users" is a per-org toggle, but MFA itself only
        # runs when the platform master switch (`FEOH_MFA_ENABLED`) is on —
        # off by default in local dev. The setting is still ACCEPTED (an admin
        # may legitimately be pre-configuring it ahead of the switch flipping
        # in a deployed env), but a save that would currently be a silent
        # no-op is logged loudly, and the response's `settings.mfa.
        # enforcement_active` (see `_org_response`) tells the caller so too.
        incoming_mfa = body.settings.get("mfa")
        if (
            isinstance(incoming_mfa, dict)
            and incoming_mfa.get("required")
            and not settings.mfa_enabled
        ):
            logger.warning(
                "[organization] org %s saved settings.mfa.required=true, but "
                "FEOH_MFA_ENABLED is off — MFA enforcement is NOT currently active "
                "for this org's users.",
                org.id,
            )

        # Merge incoming keys into existing settings (don't replace the whole dict)
        existing = dict(org.settings or {})
        prior_brand = existing.get("brand")
        existing.update(body.settings)
        # ...and carry the stored domain list across a `brand` replacement, the
        # same way `PUT /branding` does. Refusing the key above is not enough on
        # its own: a brand PATCH that simply omits `custom_domains` would still
        # drop every registered hostname, silently un-routing the tenant.
        if "brand" in body.settings and isinstance(prior_brand, dict):
            preserved = prior_brand.get("custom_domains")
            if preserved is not None:
                merged_brand = existing.get("brand")
                if not isinstance(merged_brand, dict):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "brand must be an object; replacing it with a "
                            "non-object would drop the registered custom domains."
                        ),
                    )
                merged_brand = dict(merged_brand)
                merged_brand["custom_domains"] = preserved
                existing["brand"] = merged_brand
        org.settings = existing

        # Audit an ERP change with key NAMES only, written before the commit: a
        # credential change with no audit row is refused, not made (the same
        # shape as `PUT /organization/sso`).
        if erp_changed:
            stored_erp = existing.get("erp") if isinstance(existing.get("erp"), dict) else {}
            try:
                await record_auth_audit_or_raise(
                    organization_id=org.id,
                    actor_id=user.id,
                    action="organization.erp_updated",
                    entity_type="organization",
                    entity_id=org.id,
                    details={
                        "changed": erp_changed,
                        "type": str(stored_erp.get("type") or "")[:50] or None,
                        "integration_method": str(stored_erp.get("integration_method") or "")[:50]
                        or None,
                    },
                )
            except Exception:
                await db.rollback()
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "The change could not be recorded in the audit trail, so it was not saved."
                    ),
                ) from None

    await db.commit()
    # Admin-only endpoint, so the response is the admin projection.
    return _org_response(org, is_admin=True)


def _residency_response(org: Organization, region: str) -> DataResidencyResponse:
    """Build the residency payload for `region`, including the advisory alignment.

    Shared by GET and PUT so the two can't drift — a PUT that answered without
    the alignment block would leave an admin who just pinned `eu` with no
    indication that the stack still runs elsewhere, which is precisely the
    moment the signal is worth the most.
    """
    alignment = check_residency_alignment(org, settings.deployed_region)
    return DataResidencyResponse(
        region=region,
        default_region=DEFAULT_REGION,
        supported_regions=list(SUPPORTED_REGIONS),
        placement=get_region_placement(region),
        alignment=ResidencyAlignmentResponse(
            status=alignment.status,
            aligned=alignment.aligned,
            deployed_region=alignment.deployed_region,
            reason=alignment.reason,
        ),
    )


@router.get("/data-residency", response_model=DataResidencyResponse)
async def get_data_residency(
    org: Organization = Depends(get_tenant),
    user: User = Depends(get_current_user),
):
    """Return the tenant's effective data-residency region + its placement.

    Read-gated to any authenticated org user (same as `GET /api/organization`);
    only the mutate path is admin-only. The placement block is the documented
    DB-cluster + object-storage target the region maps to, and `alignment` is the
    advisory answer to "is that where we actually run?" — see
    `docs/data-residency.md` for the single-region reality + multi-region plan.
    """
    return _residency_response(org, resolve_region(org))


@router.put("/data-residency", response_model=DataResidencyResponse)
async def update_data_residency(
    body: UpdateDataResidencyRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Pin this tenant to a residency region. Admin only; audited.

    Validates against `SUPPORTED_REGIONS` before any write (an unknown region is
    422, so a typo can't strand a tenant on a dead placement key). Writes to
    `org.settings["residency"]["region"]` via `flag_modified` (in-place nested
    JSONB mutation otherwise isn't marked dirty) and audits the change into the
    tenant trail. Changing the region is a *configuration* change — it does not
    itself migrate data; multi-region data movement is an infra operation tracked
    separately (see `docs/data-residency.md`).
    """
    if body.region not in SUPPORTED_REGIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported region '{body.region}'; valid: {list(SUPPORTED_REGIONS)}",
        )

    # Serialise with every other settings writer (`lock_organization`).
    org = await lock_organization(db, org)
    before = resolve_region(org)

    existing = dict(org.settings or {})
    residency = dict(existing.get("residency") or {})
    residency["region"] = body.region
    existing["residency"] = residency
    org.settings = existing
    # Mutating a nested dict in-place doesn't mark JSONB dirty on its own.
    flag_modified(org, "settings")

    await db.commit()

    # Audit the config change into the TENANT trail (where every other mutation
    # for this tenant lands). Settings live on the control plane, so use the
    # self-committing tenant-audit helper. PII-free: only region tokens.
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="organization.residency_updated",
        entity_id=org.id,
        details={"region": {"old": before, "new": body.region}},
    )

    return _residency_response(org, body.region)


def _resolve_brand(org: Organization) -> BrandConfig:
    """Parse `settings.brand` into a validated BrandConfig, tolerating a missing
    or malformed block by returning all-empty (= platform defaults)."""
    raw = (org.settings or {}).get("brand")
    if not isinstance(raw, dict):
        return BrandConfig()
    try:
        return BrandConfig(**raw)
    except Exception:
        # A persisted-but-now-invalid brand block must never break the read.
        return BrandConfig()


@router.get("/branding", response_model=BrandConfig)
async def get_branding(
    org: Organization = Depends(get_tenant),
    user: User = Depends(get_current_user),
):
    """Return this tenant's white-label branding config.

    Read-gated to any authenticated org user (same posture as
    `GET /api/organization` / data-residency) — the whole app needs the brand to
    theme itself, not just admins. Only the mutate path is admin-only. Empty
    fields mean "use the platform default" on the client.
    """
    return _resolve_brand(org)


@router.put("/branding", response_model=BrandConfig)
async def update_branding(
    body: BrandConfig,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Update this tenant's white-label branding. Admin only; audited.

    Pydantic has already validated the payload (hex colors, http(s) URLs), so by
    the time we get here the values are safe to persist + later inject into the
    DOM. Written to `org.settings["brand"]` via `flag_modified` (nested JSONB
    in-place mutation isn't auto-marked dirty), then audited into the tenant
    trail. PII-free: only the configured branding fields (a logo URL, links, a
    product name, colors) — never user data.

    `tenant_url_template` is the vanity base URL every outbound link for this
    tenant is built from (`app/utils/tenant_urls.py::tenant_base_url`); empty
    means "use the global `FEOH_TENANT_URL_TEMPLATE`".
    """
    # Serialise with every other settings writer (`lock_organization`).
    org = await lock_organization(db, org)
    existing = dict(org.settings or {})
    # Preserve `custom_domains` — it lives under `settings.brand` but is NOT a
    # `BrandConfig` field, so a naive `existing["brand"] = body.model_dump()`
    # would silently wipe a tenant's registered vanity hostnames on every
    # branding save. Carry the existing list forward; it is managed only by the
    # dedicated custom-domains endpoint below.
    prior_brand = existing.get("brand")
    new_brand = body.model_dump()
    if isinstance(prior_brand, dict) and "custom_domains" in prior_brand:
        new_brand["custom_domains"] = prior_brand["custom_domains"]
    # `sso_callback_base_url` IS a BrandConfig field, so `model_dump()` always
    # emits it — as `""` when the caller never mentioned it. That makes a
    # routine branding save (the `/organization` panel PUTs the whole config)
    # silently clear a value that is REGISTERED AT THE CUSTOMER'S IdP, taking
    # SSO logins with it. `custom_domains` above is protected by not being a
    # BrandConfig field at all; this one needs the omitted-vs-explicitly-cleared
    # distinction, which is what `model_fields_set` carries. Sending `null` or
    # `""` still clears it — that is the documented rollback.
    if "sso_callback_base_url" not in body.model_fields_set and isinstance(prior_brand, dict):
        preserved_callback = prior_brand.get("sso_callback_base_url")
        if preserved_callback:
            new_brand["sso_callback_base_url"] = preserved_callback
    existing["brand"] = new_brand
    org.settings = existing
    flag_modified(org, "settings")

    await db.commit()

    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="organization.branding_updated",
        entity_id=org.id,
        details={
            # Booleans only — record *which* fields are now set, never echo a
            # raw value into the audit trail.
            "product_name_set": bool(body.product_name),
            "logo_url_set": bool(body.logo_url),
            "accent_color_set": bool(body.accent_color),
            "support_url_set": bool(body.support_url),
            "legal_url_set": bool(body.legal_url),
            # The vanity base URL every outbound link is built from. Recorded
            # as a boolean like its siblings — the value itself is tenant infra
            # config, kept out of the trail for the same reason the hostnames
            # in `organization.custom_domains_updated` are.
            "tenant_url_template_set": bool(body.tenant_url_template),
            # Separate from the line above on purpose: this one is registered at
            # the customer's IdP, so a change to it is an operator-sequenced
            # migration rather than a preference. The trail records THAT it
            # changed; the value stays out for the same reason as its siblings.
            "sso_callback_base_url_set": bool(new_brand.get("sso_callback_base_url")),
        },
    )

    # Echo what was STORED, not what was sent: a caller that omitted
    # `sso_callback_base_url` had its existing value carried forward above, and
    # returning the request body verbatim would tell it the field is now empty.
    return body.model_copy(
        update={"sso_callback_base_url": new_brand.get("sso_callback_base_url") or ""}
    )


def _resolve_custom_domains(org: Organization) -> list[str]:
    """Read `settings.brand.custom_domains`, tolerating a missing / malformed
    block by returning an empty list (mirrors the resolver's own resilience)."""
    brand = (org.settings or {}).get("brand")
    if not isinstance(brand, dict):
        return []
    raw = brand.get("custom_domains")
    if not isinstance(raw, list):
        return []
    # Keep only well-formed string entries — a stray non-string can't break the
    # read or the UI list.
    return [d for d in raw if isinstance(d, str)]


@router.get("/branding/custom-domains", response_model=CustomDomainsConfig)
async def get_custom_domains(
    org: Organization = Depends(get_tenant),
    user: User = Depends(get_current_user),
):
    """Return this tenant's registered white-label vanity hostnames.

    Read-gated to any authenticated org user (same posture as
    `GET /api/organization/branding`). The list is the source the custom-domain
    tenant resolver matches an inbound `Host` against (see
    `docs/white-label.md` § Custom domains).
    """
    return CustomDomainsConfig(custom_domains=_resolve_custom_domains(org))


@router.put("/branding/custom-domains", response_model=CustomDomainsConfig)
async def update_custom_domains(
    body: CustomDomainsConfig,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Replace this tenant's registered vanity hostnames. Admin only; audited.

    Each host is normalized through the SAME `normalize_custom_domain` the tenant
    resolver uses (strip `:port`, lowercase, reject empty / IPv6-literal /
    malformed), so a stored value can never diverge from what actually resolves.
    Malformed entries are rejected (422); the normalized list is de-duplicated.
    So is a host under the platform's OWN domain (422) — that name is already
    routed by the `<slug>.<platform-domain>` subdomain path, so registering it
    would give two resolvers a conflicting claim on one hostname. The platform
    domain is derived from `FEOH_TENANT_URL_TEMPLATE` (see
    `app/utils/tenant_urls.py::platform_domain`), the one place the platform's
    hostname shape is already declared.

    **Cross-org uniqueness (anti-hijack):** a host already registered to a
    *different* org is rejected (409). A custom domain is only a *candidate*
    tenant selector — the JWT `org`-claim cross-check in `get_tenant` is what
    actually gates access — but letting two orgs claim the same host would make
    resolution ambiguous and is a footgun, so we refuse it at registration time.
    The operator still owns DNS + TLS for the host (out of scope for app code);
    see `docs/white-label.md` § Custom domains.

    Audited PII-free: only the host COUNT, never the hostnames themselves.
    """
    before = _resolve_custom_domains(org)

    # Normalize + validate every entry through the resolver's own function so the
    # stored form is exactly what `resolve_tenant_slug_by_custom_domain` matches.
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in body.custom_domains:
        host = normalize_custom_domain(raw)
        if host is None:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid custom domain: {raw!r}",
            )
        if is_under_platform_domain(host):
            # A host under the platform's own domain is ALREADY routed, by the
            # subdomain path (`<slug>.<platform-domain>` → tenant). Registering
            # it here adds no reachability and creates a second, conflicting
            # claim on a name another tenant's slug owns — or will own the next
            # time a signup takes that slug — so the custom-domain resolver and
            # the subdomain resolver would disagree about whose request it is.
            # Refused at registration, where it is still cheap to fix.
            #
            # Message names no host: the hostnames are this endpoint's PII-free
            # posture (see the 409 below), and the platform domain is derived
            # config, not something to echo back per-request.
            raise HTTPException(
                status_code=422,
                detail=(
                    "Custom domains under the platform's own domain are already "
                    "routed by tenant subdomain and can't be registered here."
                ),
            )
        if host in seen:
            # De-duplicate silently — a repeated host is not an error, just noise.
            continue
        seen.add(host)
        normalized.append(host)

    # Serialize the check-and-write so two orgs can't race past the
    # cross-org-uniqueness guard and both claim the same host (a TOCTOU hijack).
    # A transaction-level advisory lock on a constant key makes every
    # custom-domains write across the cluster mutually exclusive; it auto-releases
    # at commit/rollback. Writes are admin-initiated config (rare), so a single
    # global lock is cheap and there's no DB constraint to add (the domains live
    # in a JSONB array, not their own column). Key derived from a fixed label.
    _CUSTOM_DOMAINS_LOCK_KEY = 0x4350_4D44  # "CPMD" — arbitrary constant
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:k)").bindparams(k=_CUSTOM_DOMAINS_LOCK_KEY)
    )
    # Then the org row, like every other settings writer (`lock_organization`),
    # and re-read what we hold: a branding save may have committed since the
    # request loaded it. Order is advisory lock → org row; nothing takes them
    # the other way round.
    org = await lock_organization(db, org)
    before = _resolve_custom_domains(org)

    # Cross-org uniqueness: refuse a host already claimed by another org. Query
    # each candidate via the SAME JSONB containment the resolver uses, so the
    # check and the resolution can't disagree.
    for host in normalized:
        if host in before:
            # Already ours — no conflict possible.
            continue
        owners = (
            (
                await db.execute(
                    select(Organization.id)
                    .where(Organization.settings.contains({"brand": {"custom_domains": [host]}}))
                    .order_by(Organization.created_at.asc(), Organization.id.asc())
                )
            )
            .scalars()
            .all()
        )
        # Every claimant, not just the first row: a duplicate that predates this
        # guard could otherwise hide behind our own org's row and be re-saved.
        if any(owner != org.id for owner in owners):
            # Generic message — do NOT echo the host, which would confirm to this
            # caller that a specific hostname is claimed by another tenant
            # (cross-tenant info disclosure + the endpoint's PII-free posture).
            raise HTTPException(
                status_code=409,
                detail="One or more requested custom domains is already registered "
                "to another tenant.",
            )

    existing = dict(org.settings or {})
    brand = dict(existing.get("brand") or {})
    brand["custom_domains"] = normalized
    existing["brand"] = brand
    org.settings = existing
    # Mutating a nested dict in-place doesn't mark JSONB dirty on its own.
    flag_modified(org, "settings")

    await db.commit()

    # Audit the config change into the TENANT trail. PII-free: counts only —
    # the hostnames themselves are tenant infra config, kept out of the trail.
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="organization.custom_domains_updated",
        entity_id=org.id,
        details={"count": {"old": len(before), "new": len(normalized)}},
    )

    return CustomDomainsConfig(custom_domains=normalized)


@router.get("/erp/providers")
async def list_erp_providers(
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """The ERP setup form's catalogue (`erp_adapters/catalog`).

    `available` is whether the adapter is registered in this build; the form
    shows an unavailable entry as not yet selectable rather than letting a save
    land on `UnknownErpAdapterError`.
    """
    from app.services.erp_adapters import list_available_adapters

    registered = set(list_available_adapters())
    return {
        "providers": [
            {**entry, "available": entry["key"] in registered}
            for entry in erp_catalog.all_providers()
        ],
        "merge_dev_long_tail": erp_catalog.MERGE_DEV_LONG_TAIL,
        "secret_mask": erp_catalog.SECRET_MASK,
    }


@router.post("/test-erp")
async def test_erp_connection(
    request: dict | None = None,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Test the ERP connection. Uses request body config if provided, otherwise saved config.

    A live adapter needs ``FEATURE_ERP_INTEGRATIONS`` (decisions §258) — a test
    reaches the real ERP with the tenant's credentials. ``mock`` stays open.
    """
    stored_erp = (org.settings or {}).get("erp")
    if request and request.get("type"):
        # The form sends what it shows, and it shows secrets masked: fill each
        # blank / masked secret from the stored config (same ERP only) and the
        # stored OAuth block, exactly as a save would.
        erp_config = erp_catalog.merge_erp_update(stored_erp, request)
    else:
        erp_config = stored_erp
    if not erp_config:
        raise HTTPException(status_code=400, detail="No ERP configuration provided")
    await ensure_live_erp_entitled(db, org.id, erp_config)

    from app.services.erp_adapters import UnknownErpAdapterError, get_erp_adapter

    try:
        adapter = get_erp_adapter(erp_config)
    except CredentialCryptoError:
        # A stored credential this server cannot decrypt (a key id dropped
        # from the keyring too early, or a tampered row). Say so; name no value.
        return {
            "success": False,
            "message": (
                "The saved ERP credentials could not be decrypted on this server. "
                "Re-enter the secrets, or ask the operator to check "
                "FEOH_CREDENTIAL_ENCRYPTION_KEYS."
            ),
        }
    except UnknownErpAdapterError as exc:
        # This endpoint exists to catch exactly this misconfiguration. It used
        # to CONFIRM it instead: the unknown type fell back to `mock`, whose
        # `test_connection` returns True, so the admin was told "Connected to
        # <typo> successfully". Name the bad value; echo no credential.
        return {
            "success": False,
            "message": (
                f"'{exc.adapter_key}' is not a supported ERP adapter. "
                "Pick one from the ERP list and re-test."
            ),
        }

    try:
        success = await adapter.test_connection()
        if success:
            return {
                "success": True,
                "message": f"Connected to {erp_config.get('type', 'ERP')} successfully",
            }
        else:
            return {"success": False, "message": "Connection failed — check your credentials"}
    except Exception:
        logger.exception("ERP test_connection failed")
        return {"success": False, "message": "Connection failed — check your credentials"}


@router.post("/sso/scim-token", response_model=SCIMTokenResponse)
async def mint_scim_token(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    _entitled: User = Depends(require_entitlement(FEATURE_SCIM)),
    db: AsyncSession = Depends(get_control_db),
):
    """Mint a fresh SCIM bearer token for this tenant.

    Returns the plaintext token in the response — the admin pastes it into
    Okta/Entra and we never see it again. Only the sha256 hex digest is stored
    in `org.settings.sso.scim_bearer_hash`. Rotating is a re-call: the previous
    hash is overwritten and any IdP still using the old token will start
    getting 401s, which is the desired behaviour.

    Audited (`organization.scim_token_minted`). This token is a tenant-wide
    user-provisioning credential — whoever holds it can create, rename and
    deactivate accounts in this org, and grant roles through group mapping — so
    minting or rotating it is exactly the access event an auditor expects to
    find, and it is the one credential mint on the platform that wasn't
    recording one (`api_key.created` and `webhook_subscription.created` both
    do). PII-free and secret-free: the digest prefix is a non-secret label that
    lets an operator tell which token is live without revealing it.
    """
    raw, digest = generate_scim_token()

    # Under the row lock, like every settings writer: an unlocked read here could
    # write back a stale `sso` block over a concurrent `PUT /organization/sso`.
    org = await lock_organization(db, org)
    settings_dict = dict(org.settings or {})
    sso = dict(settings_dict.get("sso") or {})
    sso["scim_bearer_hash"] = digest
    settings_dict["sso"] = sso
    org.settings = settings_dict
    # Mutating a nested dict in-place doesn't mark JSONB dirty on its own.
    flag_modified(org, "settings")
    # Mirror onto the indexed column — this is what SCIM auth resolves on
    # since migration 0021. settings.sso.scim_bearer_hash stays populated
    # for backward compat (logs, audit history) but is no longer authoritative.
    org.scim_bearer_hash = digest

    await db.commit()

    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="organization.scim_token_minted",
        entity_type="organization",
        entity_id=org.id,
        details={"bearer_hash_prefix": digest[:8]},
    )

    return SCIMTokenResponse(token=raw, bearer_hash_prefix=digest[:8])


@router.post("/test-payments")
async def test_payment_connection(
    request: dict | None = None,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Test the payment processor connection.

    Uses request body if provided (for the "Test Connection" button before
    saving), otherwise the saved org settings.
    """
    config = (
        request if request and request.get("provider") else (org.settings or {}).get("payments")
    )
    if not config:
        raise HTTPException(status_code=400, detail="No payment processor configuration provided")

    # Trigger registration of all bundled adapters.
    from app.services.payment_adapters import (
        UnknownPaymentProviderError,
        get_payment_adapter,
    )

    try:
        adapter = get_payment_adapter(config)
    except UnknownPaymentProviderError as exc:
        # This endpoint is where an admin discovers a typo'd provider name
        # before it reaches a payment run, so name it rather than returning
        # the generic "check your configuration". No credential is echoed.
        return {
            "success": False,
            "message": (
                f"'{exc.provider}' is not a supported payment provider. "
                "Pick one from the provider list and re-test."
            ),
        }

    try:
        success = await adapter.test_connection()
        provider = config.get("provider", "unknown")
        if success:
            return {"success": True, "message": f"Connected to {provider} successfully"}
        # Surface the most likely cause without leaking key material.
        if provider == "modern_treasury":
            return {
                "success": False,
                "message": (
                    "Modern Treasury rejected the credentials — verify the "
                    "Organization ID, API key, and that the key has API access enabled."
                ),
            }
        return {"success": False, "message": "Connection failed — check your configuration"}
    except Exception:
        logger.exception("Payments test_connection failed")
        return {"success": False, "message": "Connection failed — check your configuration"}


@router.post("/test-extraction")
async def test_extraction_connection(
    request: dict | None = None,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Test the AI extraction provider connection. Uses request body config if provided."""
    config = (
        request if request and request.get("provider") else (org.settings or {}).get("extraction")
    )
    if not config:
        raise HTTPException(status_code=400, detail="No extraction configuration provided")

    from app.services.extraction_adapters import (
        UnknownExtractionProviderError,
        get_extraction_adapter,
        list_available_providers,
    )

    try:
        adapter = get_extraction_adapter(config)
    except UnknownExtractionProviderError as exc:
        # This endpoint exists to catch exactly this misconfiguration. It used
        # to CONFIRM it instead: an unknown provider fell back to `mock`, whose
        # `test_connection` returns True, so the admin was told "Connected to
        # <typo> successfully". Name the bad value; echo no credential.
        return {
            "success": False,
            "message": (
                f"'{exc.provider}' is not a supported extraction provider "
                f"(one of: {', '.join(list_available_providers())}). "
                "Pick one and re-test."
            ),
        }

    try:
        success = await adapter.test_connection()
        provider = config.get("provider", "unknown")
        if success:
            return {"success": True, "message": f"Connected to {provider} successfully"}
        else:
            if provider == "ollama":
                model = config.get("model", "llama3.2-vision:11b")
                return {
                    "success": False,
                    "message": (
                        f"Ollama is running but model '{model}' not found. Run: ollama pull {model}"
                    ),
                }
            return {"success": False, "message": "Connection failed — check your configuration"}
    except Exception:
        logger.exception("Extraction test_connection failed")
        return {"success": False, "message": "Connection failed — check your configuration"}
