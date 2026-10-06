"""Schemas for organization settings."""

import re

from pydantic import BaseModel, Field, field_validator

# 3- or 6-digit hex color, with the leading '#'. White-label accent tokens are
# injected verbatim into CSS custom properties on document.documentElement, so
# we constrain them to a strict color literal — no `url(...)`, no `expression()`,
# nothing that could smuggle a value into the cascade.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Accept only http(s) URLs for the logo / support / legal links. A logo_url is
# rendered as an <img src> and the links as <a href>, so we reject javascript:,
# data:, and other schemes outright.
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Whitespace (incl. the C0/C1 control range) inside a URL is never legitimate
# here and is how a value smuggles a second token past a naive consumer — a
# newline in a URL that later lands in an email header being the classic. The
# shape rule below is the ONE URL check for `settings.brand`; both validators
# read it, so a field can't drift onto a looser spelling of "is this a URL".
_URL_FORBIDDEN_RE = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")


def looks_like_http_url(value: str) -> bool:
    """True when `value` is an http(s) URL safe to store and later emit.

    Exported because `app/utils/tenant_urls.py` re-checks the persisted
    `brand.tenant_url_template` on read — a row written straight into the
    database has never passed through the branding endpoint, and that value
    ends up in outbound email bodies.
    """
    v = (value or "").strip()
    return bool(v) and bool(_URL_RE.match(v)) and not _URL_FORBIDDEN_RE.search(v)


# Defensive caps so a giant string can't bloat the settings JSONB.
_MAX_NAME = 120
_MAX_URL = 2048


class CompanyProfile(BaseModel):
    address: str = ""
    phone: str = ""
    website: str = ""
    tax_id: str = ""
    logo_url: str = ""
    # `tax_id` above is generically labelled "Tax ID / EIN" with a US-shaped
    # (`XX-XXXXXXX`) placeholder in the UI — it doesn't fit a UK company's own
    # identifiers. These two are separate, optional, plain strings (no format
    # validation — VAT number and Companies House registration number formats
    # vary by jurisdiction) rather than overloading `tax_id`.
    vat_registration_number: str = ""
    companies_house_number: str = ""


class InvoiceDefaults(BaseModel):
    currency: str = "USD"
    payment_terms: str = "Net 30"
    number_prefix: str = "INV-"
    default_gl_account: str = ""
    default_cost_center: str = ""


class OrganizationSettings(BaseModel):
    company: CompanyProfile = Field(default_factory=CompanyProfile)
    invoice_defaults: InvoiceDefaults = Field(default_factory=InvoiceDefaults)


class BrandConfig(BaseModel):
    """Per-tenant white-label branding (stored under `settings.brand`).

    All fields optional / empty by default — an unset field means "use the
    platform default" (product name "FeohLedger", the bundled logo, the
    app.css accent tokens). Validated so the values are safe to inject into the
    DOM: accent colors must be hex literals, URLs must be http(s).
    """

    product_name: str = Field(default="", max_length=_MAX_NAME)
    logo_url: str = Field(default="", max_length=_MAX_URL)
    accent_color: str = ""  # primary accent (borders, focus rings, accent text)
    accent_strong_color: str = ""  # darker companion for accent BACKGROUNDS (AA text)
    support_url: str = Field(default="", max_length=_MAX_URL)
    legal_url: str = Field(default="", max_length=_MAX_URL)
    # Where this tenant's app actually lives. Empty = the platform's global
    # `FEOH_TENANT_URL_TEMPLATE`. A white-label tenant reachable at its own
    # vanity hostname sets its full base URL here (`https://ap.acmecorp.com`)
    # so every outbound link — invites, password resets, portal and approval
    # deep links — points at the brand the customer paid for instead of
    # `<slug>.<platform-domain>`. `{slug}` is OPTIONAL: substituted when
    # present, used verbatim when not. Read by `app/utils/tenant_urls.py`,
    # which is the only place that substitution happens.
    tenant_url_template: str = Field(default="", max_length=_MAX_URL)
    # Where this tenant's SSO callbacks land. Empty = the platform's global
    # `FEOH_TENANT_URL_TEMPLATE`, byte-for-byte the behaviour that shipped
    # before this field existed.
    #
    # Deliberately SEPARATE from `tenant_url_template` even though a vanity
    # tenant will usually set both to the same host: the OIDC `redirect_uri`
    # and the SAML bridge URL are values *registered at the customer's IdP*
    # (`docs/decisions.md` §91). Folding them into `tenant_url_template` would
    # mean that setting a vanity base URL for invite emails silently re-points
    # the callback too, breaking every SSO login until the operator
    # re-registers the app at the IdP. So this one is opt-in on its own, and is
    # set only as the last step of the IdP re-registration runbook.
    # `{slug}` is OPTIONAL: substituted when present, used verbatim when not
    # (same rule as `tenant_url_template`). Read by `app/services/sso.py`.
    sso_callback_base_url: str = Field(default="", max_length=_MAX_URL)

    @field_validator("accent_color", "accent_strong_color")
    @classmethod
    def _validate_hex(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not _HEX_COLOR_RE.match(v):
            raise ValueError("must be a 3- or 6-digit hex color (e.g. #638cff)")
        return v

    @field_validator("tenant_url_template", "sso_callback_base_url", mode="before")
    @classmethod
    def _null_to_empty(cls, v: object) -> object:
        # The field is "nullable" on the wire — an admin clearing the override
        # in the UI sends `null`, which must mean "fall back to the platform
        # template", not a 422. Every other brand field predates that and is
        # cleared with `""`; both spellings land on `""` here.
        return "" if v is None else v

    @field_validator(
        "logo_url", "support_url", "legal_url", "tenant_url_template", "sso_callback_base_url"
    )
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not looks_like_http_url(v):
            raise ValueError("must be an http(s) URL")
        return v

    @field_validator("product_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return (v or "").strip()


class CustomDomainsConfig(BaseModel):
    """The tenant's registered white-label vanity hostnames.

    Stored under `settings.brand.custom_domains` (a JSON array of bare,
    lowercase hostnames). A request arriving on one of these hosts with no
    `X-Tenant-Slug` header resolves to this tenant — but only as a *candidate*;
    the JWT `org`-claim cross-check in `app.tenant.get_tenant` still gates it
    (see `docs/white-label.md` § Custom domains). Validation/normalization is
    done in the endpoint via the SAME `normalize_custom_domain` the resolver
    uses — not here — so the stored value can never diverge from what resolves.
    """

    custom_domains: list[str] = Field(default_factory=list)


class ChatNotificationStatus(BaseModel):
    """The **credential-free** view of `settings.chat_notifications`.

    `webhook_url` is deliberately absent and always will be: it is a bearer
    capability (see `services/chat_notifications_config`), so it is write-only.
    `webhook_host` is the bare hostname — enough for an admin to answer "where
    does our approval channel post?" during an incident, with none of the path
    or query that carries the provider's token.
    """

    enabled: bool = False
    provider: str | None = None
    events: dict[str, bool] = Field(default_factory=dict)
    webhook_configured: bool = False
    webhook_host: str | None = None
    # Registry-derived, so the picker can't offer a provider with no adapter.
    supported_providers: list[str] = Field(default_factory=list)
    supported_events: list[str] = Field(default_factory=list)


class UpdateChatNotificationsRequest(BaseModel):
    """Non-credential chat settings. The webhook URL is NOT settable here — it
    has its own endpoint, and a save through this one preserves it."""

    enabled: bool = False
    provider: str = "mock"
    events: dict[str, bool] = Field(default_factory=dict)


class SetChatWebhookRequest(BaseModel):
    """The incoming-webhook URL (the credential).

    Deliberately declared as a bare `str` with **no** Pydantic constraints:
    FastAPI's default `RequestValidationError` body echoes the offending
    `input` back, so a `min_length` / pattern failure here would put the
    credential into an HTTP error body (and any log or APM span that captures
    one). Every check runs in the endpoint instead and answers with one
    generic, value-free 422.
    """

    webhook_url: str = ""


class OrganizationResponse(BaseModel):
    id: str
    name: str
    slug: str
    plan: str
    settings: dict  # raw JSONB — preserves erp, cards, extraction keys
    created_at: str
    # The org's reporting (base) currency, ALREADY resolved server-side —
    # `currency_conversion.resolve_reporting_currency(org.settings)`, all four
    # rungs including `settings.reporting_currency_default`
    # (`FEOH_REPORTING_CURRENCY_DEFAULT`), the operator config no client can
    # read. A bare top-level field, not part of `settings`, so it is never
    # touched by the role projection above and reaches every role exactly like
    # the three settings rungs it is derived from (`services/org_settings_view`
    # already admits those to non-admins for this same purpose). Always a
    # 3-letter ISO 4217 code — the resolver never returns `None`. See
    # `docs/decisions.md` §119, §160, §200.
    resolved_reporting_currency: str


class UpdateOrganizationRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    settings: dict | None = None  # raw dict — merged into existing settings


class SSOSettingsStatus(BaseModel):
    """The **secret-free** view of `settings.sso`, for the admin SSO panel.

    `client_secret` is deliberately absent and always will be: a save that
    omits it keeps the stored one, so the panel never needs to read it back
    (`services/sso_settings`). `client_secret_configured` answers the only
    question the panel has. The certificate fields are the IdP's PUBLIC signing
    certificates, not secrets.
    """

    enabled: bool = False
    sso_only: bool = False
    protocol: str = "oidc"
    provider: str | None = None
    allowed_email_domains: list[str] = Field(default_factory=list)
    discovery_url: str | None = None
    client_id: str | None = None
    client_secret_configured: bool = False
    idp_entity_id: str | None = None
    idp_sso_url: str | None = None
    idp_x509_cert: str | None = None
    idp_x509_cert_multi: list[str] = Field(default_factory=list)
    sp_entity_id: str | None = None
    idp_slo_url: str | None = None
    scim_group_role_map: dict[str, str] = Field(default_factory=dict)
    scim_token_configured: bool = False
    # The verdict sign-in uses (`services/sso.is_sso_only`), not the request.
    password_sign_in_closed: bool = False
    # IdP key names the selected protocol still lacks, as if SSO were on.
    idp_config_missing: list[str] = Field(default_factory=list)
    # What the admin registers at the IdP — computed, never stored.
    oidc_redirect_uri: str = ""
    saml_acs_url: str = ""
    saml_sp_entity_id: str = ""


class UpdateSSOSettingsRequest(BaseModel):
    """The whole SSO configuration, for `PUT /api/organization/sso`.

    Every field is optional **at the Pydantic layer on purpose**, and
    `client_secret` is untyped. FastAPI's default `RequestValidationError` body
    echoes the offending `input`, and for a missing required field that input is
    the whole request object, client secret included. So `enabled` / `sso_only`
    being required, and every shape rule, is enforced by
    `services/sso_settings.build_sso_block`, which answers with a value-free 422.

    Omitting `client_secret` (or sending it blank) keeps the stored one; set
    `clear_client_secret` to remove it. Omitting `scim_group_role_map` keeps the
    stored map. Every other key the request leaves out is removed — this is a
    PUT of the IdP configuration, not a merge.
    """

    enabled: bool | None = None
    sso_only: bool | None = None
    protocol: str | None = None
    provider: str | None = None
    allowed_email_domains: list[str] | None = None
    discovery_url: str | None = None
    client_id: str | None = None
    client_secret: object = None
    clear_client_secret: bool = False
    idp_entity_id: str | None = None
    idp_sso_url: str | None = None
    idp_x509_cert: str | None = None
    idp_x509_cert_multi: list[str] | None = None
    sp_entity_id: str | None = None
    idp_slo_url: str | None = None
    scim_group_role_map: dict[str, str] | None = None
