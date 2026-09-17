"""Request/response shapes for the supplier portal."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.api.pagination import PageMeta
from app.schemas.money import MoneyAmount, OptionalMoneyAmount
from app.schemas.percent import PercentNumber


class PortalLoginRequest(BaseModel):
    email: str
    password: str


class PortalTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class PortalChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


# ---------- MFA (TOTP) — mirrors the employee MFA flow for vendor users ----------


class PortalMFAChallengeResponse(BaseModel):
    """Returned by /portal/auth/login when the password checks out but the
    vendor still has to clear MFA. The browser swaps `mfa_challenge_token` for
    an access token by calling /portal/auth/mfa/challenge. The challenge token
    carries `typ=vendor_mfa_challenge` — distinct from both the employee
    challenge (`mfa_challenge`) and the full vendor access token (`vendor`) — so
    a challenge token can never be used as an access token, and vice versa.

    `methods` lists the factors the vendor can clear the challenge with: TOTP
    (the enrolled authenticator) plus `email` — the on-demand email-OTP backup
    delivered to the vendor's account address, mirroring the employee flow."""

    mfa_required: bool = True
    mfa_challenge_token: str
    methods: list[str] = ["totp", "email"]


class PortalMFAEnrollStartResponse(BaseModel):
    """First step of TOTP enrollment — server mints a secret + QR. The secret is
    also returned in plaintext so vendors with no QR scanner can paste it into
    their authenticator app manually. Only returned during enrollment (before
    MFA is confirmed active); never echoed back afterwards."""

    secret: str
    provisioning_uri: str
    qr_code_data_url: str


class PortalMFAStepUpRequest(BaseModel):
    """Optional re-authentication sent when *changing* a vendor user's second
    factor (starting a fresh TOTP enrollment). Mirrors the employee
    `MFAStepUpRequest` exactly so the two surfaces can't drift.

    Both fields are optional — a vendor with no factor enrolled yet has
    nothing to protect, so first-time enrollment stays frictionless. Once a
    factor IS in force, one of the two must check out: the portal password or
    a code from the currently enrolled authenticator.
    """

    password: str | None = Field(default=None, min_length=1)
    code: str | None = Field(default=None, min_length=6, max_length=8)


class PortalMFAVerifyRequest(BaseModel):
    """Activate enrollment by proving the vendor can produce a valid code."""

    code: str = Field(..., min_length=6, max_length=8)


class PortalMFADisableRequest(BaseModel):
    """Disabling MFA re-verifies a current TOTP code — a stolen session
    shouldn't be able to silently strip MFA off."""

    code: str = Field(..., min_length=6, max_length=8)


class PortalMFAChallengeVerifyRequest(BaseModel):
    """Trade the login-issued challenge token + a valid code for a real vendor
    access token. `method` selects the factor: `totp` (the enrolled
    authenticator, default) or `email` (the on-demand email-OTP backup).

    `method` is pinned to the same `^(totp|email)$` pattern as its employee
    twin (`schemas/auth.MFAVerifyRequest`). Unconstrained, an unrecognised
    factor name — `sms`, or a client typo — fell through the route's
    `"email" if ... else "totp"` branch and silently verified TOTP, so the two
    surfaces disagreed about what a factor name even is and a caller asking for
    a factor this app has never had got a success instead of a 422.

    The one deliberate divergence from the twin is that `method` stays
    OPTIONAL here (the twin requires it): the default predates the email-OTP
    backup, so making it required would break any client still sending only
    `{challenge_token, code}`. The pattern is a pure tightening — the typed
    frontend client (`stores/portalAuth.svelte.ts::completeMfa`) only ever
    sends `'totp' | 'email'`.
    """

    challenge_token: str = Field(..., min_length=1)
    code: str = Field(..., min_length=6, max_length=8)
    method: str = Field(default="totp", pattern="^(totp|email)$")


class PortalMFAEmailChallengeRequest(BaseModel):
    """Ask for the email-OTP backup code during a login challenge. The
    login-issued `challenge_token` proves the password was already accepted, so
    we don't email codes to random people."""

    challenge_token: str = Field(..., min_length=1)


class PortalMeResponse(BaseModel):
    id: str
    email: str
    full_name: str
    must_change_password: bool
    mfa_enabled: bool = False
    vendor_id: str
    vendor_name: str
    vendor_status: str
    # Account-level email-language preference (NULL = English fallback). Drives
    # outbound supplier email copy only — never portal UI. See
    # docs/notifications.md § Localized email.
    locale: str | None = None


class PortalUpdateProfileRequest(BaseModel):
    # The supplier user's email-language preference. Validated against the
    # supported locale set at the route (422 on unknown); empty string clears it
    # (→ English). The vendor sets their OWN locale (RBAC = the authed vendor).
    locale: str | None = Field(default=None, max_length=16)


class PortalInviteRequest(BaseModel):
    email: str
    full_name: str


class PortalUserResponse(BaseModel):
    id: str
    vendor_id: str
    email: str
    full_name: str
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None = None
    created_at: datetime


class PortalInviteResponse(BaseModel):
    """Result of provisioning (or resetting) a supplier-portal credential.

    There is deliberately **no** `temp_password` field. It used to carry the
    plaintext password back to whoever called the route, for an address that
    same caller chose — which made "mint a supplier login I control" a single
    frictionless request with no delivery record, and was the first link in a
    BEC chain one `ap_manager` could complete alone (invite a portal user, sign
    in as it, stage a bank-detail change, approve their own request through the
    NULL `requested_by_user_id` short-circuit, then pay it).

    The credential now leaves only through the email adapter, to the portal
    user's own address. Local-first is unaffected: the dev default `console`
    adapter renders the message, exactly as the tenant-signup welcome email
    already relies on. See `backend/docs/supplier-portal.md` § Credential
    delivery.
    """

    user: PortalUserResponse
    # The real tenant portal URL (built from `FEOH_TENANT_URL_TEMPLATE`, same
    # construction as the signup welcome email / supplier-chat portal-link
    # email) — echoed back so the admin can point the supplier at the right
    # host. `None` when no template is configured. Not a secret.
    portal_url: str | None = None


class PortalInvoiceListItem(BaseModel):
    id: str
    invoice_number: str
    amount: Decimal
    currency: str
    status: str
    invoice_date: date | None = None
    due_date: date | None = None
    submitted_at: datetime
    file_url: str | None = None
    # The AP-authored reason from the latest `review_rejected` exception, shown
    # only when `status == "rejected"` so a supplier knows what to fix before
    # resubmitting. Same text the rejection email already carries; never the
    # rejecting employee's name.
    rejection_reason: str | None = None
    # A vendor-facing "what is this waiting on" category — set ONLY while the
    # invoice is in a processing phase (review / processing / erp), NULL
    # otherwise. PII-free: a bucket, not an internal status or a user name. The
    # portal renders it (localized) beside the phase chip, with `waiting_on_days`.
    waiting_on: str | None = None
    waiting_on_days: int | None = None


class PortalInvoiceListResponse(PageMeta):
    items: list[PortalInvoiceListItem]
    total: int


class PortalPaymentListItem(BaseModel):
    id: str
    invoice_id: str
    invoice_number: str
    amount: Decimal
    # The payment's currency, carried so the portal renders the supplier's own
    # currency instead of falling back to USD. Sourced from the paid invoice.
    currency: str = "USD"
    method: str | None = None
    status: str
    reference: str | None = None
    submitted_at: datetime | None = None
    completed_at: datetime | None = None


class PortalPaymentListResponse(PageMeta):
    items: list[PortalPaymentListItem]
    total: int


# ---------- Purchase orders + PO flip ----------


class PortalPOListItem(BaseModel):
    id: str
    po_number: str
    status: str
    total: Decimal
    currency: str = "USD"
    line_item_count: int = 0
    created_at: datetime


class PortalPOListResponse(PageMeta):
    items: list[PortalPOListItem]
    total: int


class PortalPOLineItem(BaseModel):
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    total: Decimal | None = None


class PortalPODetail(BaseModel):
    id: str
    po_number: str
    status: str
    total: Decimal
    currency: str = "USD"
    created_at: datetime
    line_items: list[PortalPOLineItem] = []


class PortalFlipResponse(BaseModel):
    id: str
    correlation_id: str
    status: str
    message: str


# ---------- Company self-service ----------


class PortalPendingChange(BaseModel):
    id: str
    change_type: str
    status: str
    created_at: datetime


class PortalCompanyInfoResponse(BaseModel):
    """Current company info as seen by the supplier.

    Never returns full bank account numbers or an unmasked tax ID — only
    a masked `tax_id_last4` and a `has_bank_details` boolean. The pending
    change (if any) lets the UI show a "pending AP approval" banner.
    """

    name: str
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    tax_id_last4: str | None = None
    has_bank_details: bool = False
    pending_change: PortalPendingChange | None = None


class PortalCompanyInfoUpdateRequest(BaseModel):
    """The live-apply contact fields only. Bank details and tax ID are
    intentionally absent here — they route through the staging endpoints."""

    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)


class PortalBankChangeRequest(BaseModel):
    # Structurally validated in `api/portal.request_bank_change`, NOT here.
    # FastAPI renders a Pydantic ValidationError as a 422 whose body echoes the
    # rejected `input` — and the input is this whole dict, account number
    # included, so "your routing number has a typo" would answer with banking
    # data. The route raises an HTTPException naming only the field; the
    # approve chokepoint re-validates, so a payload that reached staging some
    # other way still cannot be applied unvalidated.
    bank_details: dict


class PortalTaxIdChangeRequest(BaseModel):
    tax_id: str = Field(..., min_length=1, max_length=50)


class PortalChangeRequestResponse(BaseModel):
    id: str
    change_type: str
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Tax forms (W-9 / W-8) — vendor self-service. The vendor uploads their own
# signed form; AP uses it for 1099 / withholding compliance. The response is
# PII-free: it never echoes a tax ID, only whether a form is on file + the
# form type + received date. See backend/docs/supplier-portal.md.
# ---------------------------------------------------------------------------


# US vendors file a W-9; foreign vendors file a W-8 (BEN / BEN-E etc.). We keep
# the stored marker to the two coarse buckets; the AP-side tax tooling already
# tracks the finer `tax_classification`.
TAX_FORM_TYPES = ("w9", "w8")


class PortalTaxFormResponse(BaseModel):
    """Whether a tax form is on file for the caller's own vendor.

    Never carries the tax ID or any document bytes — only the boolean
    on-file flag, the coarse form type, and the received date. ``form_type``
    is ``None`` when nothing is on file.
    """

    on_file: bool = False
    form_type: str | None = None
    received_date: date | None = None
    # The vendor's country, when known, drives the default form type the UI
    # pre-selects (US → W-9, otherwise W-8). Never required for upload.
    suggested_form_type: str = "w9"


# ---------------------------------------------------------------------------
# Supplier chat (portal side) — datetimes are raw `datetime`, matching the rest
# of portal.py. AP author ids are masked (never exposed to the supplier).
# See backend/docs/supplier-chat.md.
# ---------------------------------------------------------------------------


class PortalChatMessageCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=10_000)


class PortalChatAttachmentOut(BaseModel):
    file_url: str
    filename: str
    content_type: str
    size: int


class PortalChatMessageResponse(BaseModel):
    id: str
    author_role: str  # "ap_team" | "supplier" | "system"
    author_name: str | None  # NO author_user_id exposed to supplier
    body: str
    attachments: list[PortalChatAttachmentOut] = []
    created_at: datetime


class PortalChatThreadResponse(BaseModel):
    invoice_id: str
    status: str  # "open" | "resolved"
    messages: list[PortalChatMessageResponse] = []


# ---------------------------------------------------------------------------
# Early-payment discount offers (portal side)
#
# A vendor sees early-payment discount offers the AP team has extended to them
# (scoped to their own vendor_id and/or their own invoices) and can accept the
# offered early-pay discount. Accepting only flips the offer status — it never
# moves money (the CFO-gated payment run still funds it). Tier percents stay
# Decimal-exact; the JSON contract serialises them as numbers.
# See backend/docs/dynamic-discounting.md § Supplier portal.
# ---------------------------------------------------------------------------


class PortalDiscountTier(BaseModel):
    """One rung of a sliding-scale offer — pay within `days` for `percent` off."""

    days: int
    percent: PercentNumber
    # Dollar discount this rung yields against the offer's base amount.
    savings: MoneyAmount


class PortalDiscountOfferResponse(BaseModel):
    """An early-payment discount offer as seen by the supplier.

    Only the fields a vendor cares about: the amount the discount applies to,
    the sliding-scale tiers with per-tier savings, the offer window, the chosen
    tier once accepted, and the realised savings once captured. No internal
    actor ids are exposed. Money + percent serialise as JSON numbers (the
    dynamic-discounting contract).
    """

    id: str
    # The EFFECTIVE status (`discount_offers.effective_status`), not the stored
    # column: an offer past its `valid_until` reads `expired` here whether or
    # not the auto-capture sweep — off by default — has written that down. The
    # AP side derives it the same way, so the two views of one offer agree.
    status: str  # offered | accepted | captured | declined | expired
    scope: str  # invoice | vendor
    invoice_id: str | None = None
    invoice_number: str | None = None
    base_amount: MoneyAmount
    currency: str
    tiers: list[PortalDiscountTier] = []
    # The best (highest-percent) tier still capturable today, with its savings —
    # the headline number for the vendor's "accept" decision. None once the
    # window has closed or the offer is no longer open.
    best_tier: PortalDiscountTier | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    accepted_tier: PortalDiscountTier | None = None
    accepted_at: datetime | None = None
    captured_amount: OptionalMoneyAmount = None
    captured_at: datetime | None = None
    notes: str | None = None
    created_at: datetime


class PortalDiscountOfferListResponse(PageMeta):
    items: list[PortalDiscountOfferResponse]
    total: int


class PortalAcceptOfferRequest(BaseModel):
    """Accept an offer. `tier_days` picks an explicit rung; omit it to take the
    best tier still capturable today (mirrors the AP-side accept)."""

    tier_days: int | None = None


# Notification preferences (portal side) — vendor-controlled email opt-out for
# invoice lifecycle events that touch THEIR invoices (paid / rejected).
# Vendors have no in-app notification center, so only the `email` channel is
# exposed. See backend/docs/supplier-portal.md + backend/docs/notifications.md.
# ---------------------------------------------------------------------------


class PortalNotificationPreferencesResponse(BaseModel):
    """Effective preferences, with defaults applied for any unset event."""

    email_on_payment: bool = True
    email_on_rejection: bool = True


class PortalNotificationPreferencesUpdateRequest(BaseModel):
    """Partial update — an unspecified field leaves that preference unchanged."""

    email_on_payment: bool | None = None
    email_on_rejection: bool | None = None


# ---------------------------------------------------------------------------
# Portal home summary (`GET /api/portal/summary`)
#
# The at-a-glance figures a supplier lands on: what needs THEIR action, what is
# sitting with the customer, what has been paid, and the two decisions waiting
# on them elsewhere in the portal (an open early-payment offer, a staged
# bank/tax change). PII-free by construction — counts, currency codes and money
# only; never an AP employee's name and never an internal status string (the
# buckets are vendor-facing, exactly like `PORTAL_INVOICE_STATUS_LABELS`).
# ---------------------------------------------------------------------------


class PortalCurrencyTotal(BaseModel):
    """One currency's slice of a money rollup.

    `total` is an EXACT decimal string, never a float and never added across
    currencies: a cross-currency SUM is a number denominated in nothing, and an
    FX rate fetched on a read would make the figure non-deterministic. Same
    contract as `ExpenseCurrencyTotal` (`app/schemas/expense.py`).
    """

    currency: str
    total: str
    count: int


class PortalSummaryResponse(BaseModel):
    """Whole-set, vendor-scoped counts for the supplier-portal home.

    Buckets collapse `InvoiceStatus` the same way the portal's status labels do
    — a supplier never sees `sending_to_erp`. `invoices_action_required` is the
    only bucket the supplier can move (a rejected invoice they can resubmit);
    `invoices_in_progress` is with the customer.
    """

    invoices_total: int = 0
    invoices_action_required: int = 0
    invoices_in_progress: int = 0
    invoices_paid: int = 0
    invoices_completed: int = 0
    # Value still owed on the in-progress bucket, per currency. Deliberately NOT
    # a "paid" total: what was actually settled (net of credit memos, partial
    # settlement, FX) is the payments page's figure, and restating it from
    # invoice headers here would let the home page contradict it.
    outstanding_by_currency: list[PortalCurrencyTotal] = []
    open_discount_offers: int = 0
    pending_change: PortalPendingChange | None = None
