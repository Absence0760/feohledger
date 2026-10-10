from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.api.pagination import PageMeta
from app.schemas.sanctions import ScreeningReviewItem
from app.services.vendor_consolidation import mask_tax_id
from app.utils.banking import (
    validate_aba_routing,
    validate_uk_account_number,
    validate_uk_sort_code,
)

# Bulk-status targets a human may legitimately drive over a hand-picked set of
# vendors — the same two the single-row `POST /{vendor_id}/verify` /
# `/reject` endpoints already expose. Typed as a Literal (not the free-form
# `Vendor.status` string column) so an out-of-scope target is a 422 from
# Pydantic itself rather than reaching the endpoint and failing per-row.
VendorBulkStatusTarget = Literal["active", "rejected"]


# The two ABA-shaped routing fields a `bank_details` payload may carry, and the
# rail family each one is for. `routing_number` is the ORIGINAL, generic field
# and stays the ACH/domestic number — every stored row already means that by it,
# so reading it as ACH keeps history correct with no backfill and no
# reinterpretation. `wire_routing_number` is the separate Fedwire ABA larger US
# banks publish alongside it; when a vendor has only one number, the wire rail
# falls back to the ACH one (which is what a single-number bank means).
ACH_ROUTING_FIELD = "routing_number"
WIRE_ROUTING_FIELD = "wire_routing_number"
ROUTING_NUMBER_FIELDS: tuple[str, ...] = (ACH_ROUTING_FIELD, WIRE_ROUTING_FIELD)


def validate_bank_routing_fields(details: dict) -> dict:
    """Structurally validate every routing-shaped key in a `bank_details` dict.

    Checked ONLY when a key is present and non-empty — an international
    vendor's details carry `iban` / `swift_bic` and legitimately have no ABA at
    all, so requiring one would refuse a whole class of real payees. A key that
    IS supplied and is malformed raises `ValueError`, because the alternative
    is storing a fat-fingered digit that only surfaces days later as a returned
    or misdirected payment.

    Raises with the FIELD NAME only, never the value — the message reaches an
    HTTP 4xx body and a routing number is banking data (invariant: PII/banking
    data stays out of logs and error bodies).

    Shared by `VendorBankChangeRequest` (the AP staging path) and
    `api/vendors.approve_change_request` (the single chokepoint where any
    staged change — AP- or portal-submitted — is applied to the vendor row),
    so a payload that reached staging through a route this module doesn't own
    still cannot be applied unvalidated.
    """
    for field in ROUTING_NUMBER_FIELDS:
        value = details.get(field)
        if value and not validate_aba_routing(value):
            raise ValueError(f"{field} is not a valid 9-digit ABA routing number")
    # Same posture for the UK equivalent — a sort code, only checked when
    # present (a US vendor's details never carry one). Accepted either grouped
    # (NN-NN-NN) or bare (NNNNNN); see validate_uk_sort_code.
    sort_code = details.get("sort_code")
    if sort_code and not validate_uk_sort_code(sort_code):
        raise ValueError("sort_code is not a valid 6-digit UK sort code")
    # A UK payee is identified by the sort code + account number PAIR, so the
    # account number is checked exactly when a sort code is present. It is
    # deliberately NOT checked otherwise: `account_number` is the generic key
    # every rail uses, and a US or IBAN payee's is not 8 digits — validating it
    # unconditionally would refuse most real payees. Checking only the sort-code
    # half left the pair half-validated, which is the worse failure: a valid
    # sort code alongside a 5-digit account number cleared staging AND the
    # second-approver BEC sign-off, and surfaced days later as a returned or
    # misdirected payment — the exact outcome this module exists to prevent.
    account_number = details.get("account_number")
    if sort_code and account_number and not validate_uk_account_number(account_number):
        raise ValueError("account_number is not a valid 8-digit UK account number")
    return details


def _is_masked_tax_id(value) -> bool:
    """True when a caller echoed back the ``***<last4>`` masked value we return
    in responses. Real tax ids are digits/separators and never start ``***``."""
    return isinstance(value, str) and value.startswith("***")


class VendorMailingAddress(BaseModel):
    """Physical address a printed check gets mailed to (the `checkeeper`
    adapter's `create_payment` reads this exact shape off
    `Vendor.bank_details["mailing_address"]` — see
    `services/payment_adapters/checkeeper.py`). Unlike an account/routing
    number this is not a secret — it's the same class of data as the
    vendor's own `address` field, which is already recorded verbatim in the
    audit trail — so it needs no last4-masking treatment."""

    street: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    postal: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, max_length=2)


class VendorBankDetails(BaseModel):
    """Subset of `Vendor.bank_details` JSONB the UI is allowed to set.

    The processor's counterparty / external ID is the bridge to live
    payment rails — Modern Treasury and friends key payment-orders off
    that. Last4s of routing + account number are stored alongside for
    display, never the full account numbers (those live with the
    processor).

    International fields:
      - `iban_last4`: the last 4 chars of the IBAN, displayed in the UI.
        The FULL IBAN lives in `Vendor.bank_details` JSONB and is read
        only by the payment orchestrator — it is never returned to the
        browser. A full IBAN is effectively an account identifier in
        SEPA-zone countries.
      - `swift_bic`: the bank's SWIFT/BIC code. Public bank routing
        information (same trust level as a US ABA routing number),
        safe to surface in the UI.
      - `country`: ISO 3166-1 alpha-2 country code for the destination
        bank; used by the corridor selector.

    Routing numbers (US):
      - `routing_last4` displays the ACH/domestic routing number
        (`bank_details["routing_number"]`, the original generic key —
        every stored row already means ACH by it).
      - `wire_routing_last4` displays the SEPARATE Fedwire ABA
        (`bank_details["wire_routing_number"]`) larger US banks publish for
        incoming wires. Optional: when it is absent the wire rail falls back
        to the ACH number, which is what a single-number bank means. See
        `services/payment_adapters/base.resolve_routing_number`.
      - `mailing_address`: where a `check` payment via the `checkeeper` rail
        gets physically mailed (street/city/state/postal/country). Without
        it that rail refuses every payment with `checkeeper_missing_
        mailing_address` — this is the only writer of that key."""

    counterparty_id: str | None = Field(default=None, max_length=255)
    account_last4: str | None = Field(default=None, max_length=4)
    routing_last4: str | None = Field(default=None, max_length=4)
    wire_routing_last4: str | None = Field(default=None, max_length=4)
    bank_name: str | None = Field(default=None, max_length=255)
    iban_last4: str | None = Field(default=None, max_length=4)
    swift_bic: str | None = Field(default=None, max_length=11)
    country: str | None = Field(default=None, max_length=2)
    mailing_address: VendorMailingAddress | None = None


class VendorBase(BaseModel):
    name: str = Field(..., max_length=255)
    code: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    tax_id: str | None = Field(default=None, max_length=50)
    payment_terms: str | None = Field(default=None, max_length=100)
    accepts_virtual_cards: bool = False
    bank_details: VendorBankDetails | None = None

    @model_validator(mode="after")
    def _guard_masked_tax_id(self):
        # `tax_id` is returned masked (`***<last4>`) in VendorResponse. A UI that
        # round-trips the vendor on save echoes that masked value back — never
        # persist the mask over the stored raw tax id. Null it and drop it from
        # the write set so `model_dump(exclude_unset=True)` skips it on update
        # (leaving the stored value unchanged) and a create stores nothing.
        if _is_masked_tax_id(self.tax_id):
            self.tax_id = None
            self.__pydantic_fields_set__.discard("tax_id")
        return self


class VendorCreate(VendorBase):
    pass


class VendorUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    code: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    tax_id: str | None = None
    payment_terms: str | None = None
    accepts_virtual_cards: bool | None = None
    status: str | None = None
    bank_details: VendorBankDetails | None = None

    @model_validator(mode="after")
    def _guard_masked_tax_id(self):
        # See VendorBase._guard_masked_tax_id — same round-trip protection on edit.
        if _is_masked_tax_id(self.tax_id):
            self.tax_id = None
            self.__pydantic_fields_set__.discard("tax_id")
        return self


class CardRevocationItem(BaseModel):
    """One card a vendor-ineligibility write could not close. PII-free: the
    card id, its last four, the outcome tag and (for a card behind booked
    money) the payment that must be voided to close it."""

    card_id: str
    last_four: str | None = None
    outcome: str
    payment_id: str | None = None


class VendorCardRevocationResponse(BaseModel):
    """What happened to the vendor's live virtual cards when it stopped being
    payable (`services/vendor_card_revocation.py`).

    - `cancelled` — closed at the provider and in our DB.
    - `not_closed` — the provider did not confirm the close; the card is
      STILL LIVE. Retry with `POST /api/vendors/{id}/cancel-cards`.
    - `requires_payment_void` — the card is behind a live payment; closing it
      is the payment void's job (`POST /api/payments/{id}/void`), which also
      reopens the invoice. Still live until then.
    """

    vendor_id: str
    cancelled: int = 0
    not_closed: list[CardRevocationItem] = Field(default_factory=list)
    requires_payment_void: list[CardRevocationItem] = Field(default_factory=list)

    @classmethod
    def from_result(cls, r) -> "VendorCardRevocationResponse":
        def _item(c) -> CardRevocationItem:
            return CardRevocationItem(
                card_id=str(c.card_id),
                last_four=c.last_four,
                outcome=c.outcome,
                payment_id=str(c.payment_id) if c.payment_id else None,
            )

        return cls(
            vendor_id=str(r.vendor_id),
            cancelled=len(r.cancelled),
            not_closed=[_item(c) for c in r.not_closed],
            requires_payment_void=[_item(c) for c in r.requires_payment_void],
        )

    @classmethod
    def from_optional(cls, r) -> "VendorCardRevocationResponse | None":
        return cls.from_result(r) if r is not None else None


class VendorResponse(BaseModel):
    id: str
    name: str
    code: str | None
    email: str | None
    phone: str | None
    address: str | None
    website: str | None = None
    tax_id: str | None
    payment_terms: str | None
    accepts_virtual_cards: bool
    status: str
    source: str
    verified_by: str | None
    verified_at: str | None
    erp_vendor_id: str | None
    erp_synced_at: str | None
    invoice_count: int = 0
    created_at: str
    bank_details: VendorBankDetails | None = None

    # Sanctions & risk screening (migration 0042). Denormalised current
    # state; the full trail is GET /api/vendors/{id}/screening-history.
    screening_status: str = "unscreened"
    last_screened_at: str | None = None
    payments_blocked: bool = False
    payments_blocked_reason: str | None = None
    risk_score: str | None = None
    risk_level: str = "unknown"

    # Set only by a write that left the vendor ineligible for card spend
    # (status off `active`, payment block, sanctions match): what happened to
    # its live virtual cards. `None` on every other read and write.
    card_revocation: VendorCardRevocationResponse | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_db(cls, v, invoice_count: int = 0) -> "VendorResponse":
        bank_details = None
        if v.bank_details:
            # Pydantic strips unknown keys via VendorBankDetails — keeps
            # the legacy JSONB shape (which historically held arbitrary
            # processor metadata) from leaking out to the UI.
            full_iban = (v.bank_details.get("iban") or "").strip()
            iban_last4 = full_iban[-4:] if len(full_iban) >= 4 else None
            bank_details = VendorBankDetails(
                counterparty_id=v.bank_details.get("counterparty_id"),
                account_last4=v.bank_details.get("account_last4"),
                routing_last4=v.bank_details.get("routing_last4"),
                bank_name=v.bank_details.get("bank_name"),
                iban_last4=iban_last4,
                swift_bic=v.bank_details.get("swift_bic"),
                country=v.bank_details.get("country"),
                mailing_address=v.bank_details.get("mailing_address"),
            )
        return cls(
            id=str(v.id),
            name=v.name,
            code=v.code,
            email=v.email,
            phone=v.phone,
            address=v.address,
            website=getattr(v, "website", None),
            # PII: never return the raw tax id — mask to `***<last4>`.
            tax_id=mask_tax_id(v.tax_id),
            payment_terms=v.payment_terms,
            accepts_virtual_cards=v.accepts_virtual_cards,
            status=v.status,
            source=v.source,
            verified_by=v.verified_by,
            verified_at=v.verified_at.isoformat() if v.verified_at else None,
            erp_vendor_id=v.erp_vendor_id,
            erp_synced_at=v.erp_synced_at.isoformat() if v.erp_synced_at else None,
            invoice_count=invoice_count,
            created_at=v.created_at.isoformat() if v.created_at else "",
            bank_details=bank_details,
            screening_status=getattr(v, "screening_status", "unscreened"),
            last_screened_at=(
                v.last_screened_at.isoformat() if getattr(v, "last_screened_at", None) else None
            ),
            payments_blocked=bool(getattr(v, "payments_blocked", False)),
            payments_blocked_reason=getattr(v, "payments_blocked_reason", None),
            risk_score=(str(v.risk_score) if getattr(v, "risk_score", None) is not None else None),
            risk_level=getattr(v, "risk_level", "unknown"),
        )


def _mask_change_value(change_type: str, proposed_value: dict) -> dict:
    """Mask the sensitive parts of a staged change for list/summary views.

    Bank account numbers and the full tax ID never appear in a list payload
    — only enough to identify the change (a last-4). The full value is
    revealed only on the dedicated detail/approve path so AP can verify it.
    """
    if change_type == "tax_id":
        tax_id = str(proposed_value.get("tax_id") or "")
        return {"tax_id_last4": tax_id[-4:] if len(tax_id) >= 4 else None}
    if change_type == "bank_details":
        bank = proposed_value.get("bank_details") or {}
        account = str(bank.get("account_number") or "")
        wire = str(bank.get(WIRE_ROUTING_FIELD) or "")
        return {
            "bank_name": bank.get("bank_name"),
            "account_last4": account[-4:] if len(account) >= 4 else bank.get("account_last4"),
            # Whether the proposal changes the WIRE ABA is material to the
            # approver (it re-points every wire to this vendor), so the masked
            # list view says so — a last-4, never the number.
            "wire_routing_last4": (wire[-4:] if len(wire) >= 4 else bank.get("wire_routing_last4")),
        }
    return {}


class VendorChangeRequestResponse(BaseModel):
    """Admin-side view of a staged vendor change request.

    `proposed_value` is masked by default (`reveal=False`); the full
    banking / tax value is included only when an admin explicitly opens
    the detail or is about to apply the change. The route is RBAC-gated.
    """

    id: str
    vendor_id: str
    vendor_name: str | None = None
    change_type: str
    status: str
    proposed_value: dict
    # Exactly one requester is set: the portal VendorUser, or the AP User.
    requested_by_vendor_user_id: str | None = None
    requested_by_user_id: str | None = None
    # The AP actor (if any) who provisioned the portal identity that submitted
    # this request — frozen at staging. Surfaced so the review UI can explain,
    # BEFORE the approve click 403s, that this reviewer is not an independent
    # second party to it. An actor id, never an address: PII-free.
    requester_provisioned_by_user_id: str | None = None
    reviewed_by_user_id: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None
    created_at: str

    @classmethod
    def from_db(
        cls, r, *, vendor_name: str | None = None, reveal: bool = False
    ) -> "VendorChangeRequestResponse":
        proposed = (
            r.proposed_value if reveal else _mask_change_value(r.change_type, r.proposed_value)
        )
        return cls(
            id=str(r.id),
            vendor_id=str(r.vendor_id),
            vendor_name=vendor_name,
            change_type=r.change_type,
            status=r.status,
            proposed_value=proposed,
            requested_by_vendor_user_id=(
                str(r.requested_by_vendor_user_id) if r.requested_by_vendor_user_id else None
            ),
            requested_by_user_id=(
                str(r.requested_by_user_id) if getattr(r, "requested_by_user_id", None) else None
            ),
            requester_provisioned_by_user_id=(
                str(r.requester_provisioned_by_user_id)
                if getattr(r, "requester_provisioned_by_user_id", None)
                else None
            ),
            reviewed_by_user_id=str(r.reviewed_by_user_id) if r.reviewed_by_user_id else None,
            reviewed_at=r.reviewed_at.isoformat() if r.reviewed_at else None,
            review_note=r.review_note,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )


class VendorChangeReviewRequest(BaseModel):
    review_note: str | None = Field(default=None, max_length=1000)


class VendorBankChangeRequest(BaseModel):
    """AP-initiated bank-details change. Staged for dual-control approval rather
    than applied (the BEC / bank-redirect gate). `bank_details` mirrors the
    partial accepted by the vendor PATCH (counterparty_id, *_last4, bank_name,
    and optionally a full account/routing/iban)."""

    bank_details: dict

    # NOTE the routing/sort-code check is deliberately NOT a `field_validator`
    # here. FastAPI renders a Pydantic `ValidationError` as a 422 whose body
    # echoes the rejected `input` back to the caller — and the input is the
    # whole `bank_details` dict, account number included. A validator on this
    # field therefore turned "your routing number has a typo" into a response
    # body carrying banking data, which the PII invariant forbids. The check
    # runs instead inside `api/vendors._stage_ap_bank_change` — the single
    # staging chokepoint all three AP paths (create / PATCH / bank-change) go
    # through — and raises an `HTTPException` naming only the FIELD.


class VendorBulkStatusRequest(BaseModel):
    """Bulk verify / reject over a hand-picked set of vendors — the bulk
    counterpart of `POST /{vendor_id}/verify` and `/reject`. `status` is
    restricted to the two targets those single-row endpoints already
    support (see `VendorBulkStatusTarget`); anything else is a 422 before
    the endpoint even runs."""

    ids: list[str] = Field(..., min_length=1)
    status: VendorBulkStatusTarget


class VendorBulkStatusSkip(BaseModel):
    """One vendor `bulk/status` didn't move, and why. Mirrors
    `api/invoices.py::BulkStatusSkip` — a skip can be "not found", a status
    that isn't a legal starting point for the target (mirroring the
    single-row endpoints' 409), or a bad id format; `reason` carries the
    real cause rather than a single generic label."""

    id: str
    reason: str


class VendorBulkStatusResponse(BaseModel):
    updated: int
    skipped: list[VendorBulkStatusSkip] = Field(default_factory=list)
    # One entry per vendor this call made ineligible that held live cards
    # (a `rejected` target only — `active` never revokes anything).
    card_revocations: list[VendorCardRevocationResponse] = Field(default_factory=list)


class VendorBulkScreenRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)


class VendorBulkScreenSkip(BaseModel):
    id: str
    reason: str


class VendorBulkScreenResponse(BaseModel):
    """Same partial-success contract as `VendorBulkStatusResponse` /
    `api/expenses.py::ExpenseBulkGlCodeResponse`: each vendor is screened
    independently, so a sanctions-provider failure or a stale id on one
    vendor is skipped-and-reported rather than aborting the whole batch —
    a batch of 200 shouldn't lose its other 199 because one id is stale."""

    screened: int
    skipped: list[VendorBulkScreenSkip] = Field(default_factory=list)
    # One entry per vendor a sanctions match blocked that held live cards.
    card_revocations: list[VendorCardRevocationResponse] = Field(default_factory=list)


class VendorBulkExportRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)


class VendorStatusCounts(BaseModel):
    """`GET /api/vendors/counts` — whole-set tallies over the entity-scoped,
    search/source-filtered vendor population.

    `by_status` drives the vendor list's status chips; `payments_blocked` is a
    SECOND tally over the SAME population rather than a slice of `by_status`,
    because a payment block is an orthogonal axis: `POST /vendors/{id}/block`
    sets `Vendor.payments_blocked` and never touches `status` or
    `screening_status`. `total` is the sum of `by_status`, so
    `payments_blocked` may legitimately overlap any of the buckets and must
    never be added to `total`.

    PII-free — counts only, never a vendor name or a block reason.
    """

    total: int
    by_status: dict[str, int] = Field(default_factory=dict)
    payments_blocked: int = 0
    # Whole-set tally of `Vendor.screening_status` over the SAME population, on
    # the same single aggregate pass as `by_status` / `payments_blocked` — so
    # the three can never describe differently-filtered sets.
    #
    # It exists because the `/vendors/screening` page derived its "Sanctions
    # matches" / "Needs review" headline figures by filtering the LOADED review
    # queue. That was correct only while the queue endpoint returned every row
    # AND was selected on exactly those two statuses — a construction accident,
    # not a stated property, and paginating the queue would have turned both
    # KPIs into silent page-scoped undercounts. A tally has to come from a
    # query that asks the tally's own question (`docs/decisions.md` §48).
    by_screening_status: dict[str, int] = Field(default_factory=dict)


class ScreeningReviewQueueResponse(PageMeta):
    """`GET /api/vendors/screening/review-queue` — the canonical paginated
    envelope (`items` / `total` / `page` / `page_size`), same contract as every
    other list endpoint.

    The queue used to return a bare, unbounded `list[...]`: one `Vendor` query
    plus one `sanctions_checks` lookup PER ROW, growing without limit with the
    tenant's flagged-vendor population. Paginating bounds both. The headline
    counts that used to be derived from the full list now come from
    `GET /api/vendors/counts` (`by_screening_status`) instead."""

    items: list[ScreeningReviewItem]
    total: int
