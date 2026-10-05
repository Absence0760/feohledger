"""1099 reporting + vendor tax bookkeeping.

US AP table stakes: track which vendors crossed the $600/year threshold,
whether a W-9 is on file, and what tax classification they reported. The
report endpoint is pure-query so it can be run ad-hoc during January
close without side effects.

This module is the aggregation layer. Form *generation* lives in
``tax_1099_forms`` (PDF), and *e-filing* lives in the
``tax_filing_adapters`` package + the ``POST /api/tax/1099/file``
endpoint — both reuse the rows this module computes. See
``backend/docs/tax-1099.md``.

Public API:
    - ``build_1099_report(db, organization_id, year)`` — compute the list
    - ``build_1099_dashboard(db, organization_id, year)`` — readiness view
    - ``THRESHOLD_USD`` — $600, the IRS filing threshold
    - ``BOX_CATALOG`` / ``resolve_box_mapping`` / ``allocate_boxes`` — the
      per-box allocation of a vendor's reportable total (see the section
      comment above ``BOX_CATALOG``)
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, case, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.models.payment import Payment
from app.models.vendor import Vendor
from app.services.currency_conversion import payment_reporting_amount_sql
from app.services.payment_methods import card_payment_method_clause
from app.services.storage import tax_form_type_from_key
from app.utils.dates import utc_today

logger = logging.getLogger(__name__)

# The general information-reporting threshold (IRC §6041 / §6041A — 1099-NEC
# box 1, and 1099-MISC rents / other income / medical) for payments made
# BEFORE 2026. Kept under its historical name for callers that build rows by
# hand; the report itself resolves the figure per box and per year through
# ``reporting_threshold`` — there is no single 1099 threshold.
THRESHOLD_USD = Decimal("600")

# OBBBA §70433 raised the §6041 / §6041A threshold from $600 to $2,000 for
# payments made after 2025-12-31, indexed for inflation after 2026. Keyed by
# the first payment year each figure applies to. When the IRS publishes an
# indexed figure for 2027+, append it here. Until then the $2,000 floor is
# used for every later year: indexing only RAISES the threshold, so the floor
# can over-include a vendor (voluntary reporting below the threshold is
# permitted) but can never drop a return that is required.
_GENERAL_THRESHOLDS: tuple[tuple[int, Decimal], ...] = ((2026, Decimal("2000")),)

# Boxes whose threshold is NOT the general one:
# * MISC-2 royalties — §6050N, $10. Untouched by OBBBA.
# * MISC-10 gross proceeds paid to an attorney — §6045(f), $600. OBBBA amended
#   §6041 / §6041A only, and IRS Publication 1099 (2026) still lists $600.
_FIXED_BOX_THRESHOLDS: dict[str, Decimal] = {
    "MISC-2": Decimal("10"),
    "MISC-10": Decimal("600"),
}


def general_reporting_threshold(year: int) -> Decimal:
    """The §6041 / §6041A threshold for payments made in ``year``."""
    threshold = THRESHOLD_USD
    for first_year, amount in _GENERAL_THRESHOLDS:
        if year >= first_year:
            threshold = amount
    return threshold


def reporting_threshold(box_code: str, year: int) -> Decimal:
    """The amount at which a payee's ``box_code`` total requires a return."""
    fixed = _FIXED_BOX_THRESHOLDS.get(box_code)
    return fixed if fixed is not None else general_reporting_threshold(year)


# ---------------------------------------------------------------------------
# 1099 box allocation
# ---------------------------------------------------------------------------
#
# A 1099 is not one number in one box. 1099-NEC has a single relevant box
# (1, nonemployee compensation) but 1099-MISC splits across several, and a
# vendor whose spend spans categories — rent AND medical AND legal — is
# mis-filed if the whole reportable total is dumped into whichever box the
# caller happened to ask for. That was the shipped simplification this
# section removes.
#
# The driver is the money's own coding: each completed payment settles an
# invoice, and the invoice carries the GL account AP coded it to
# (``Invoice.gl_account``). A per-org GL-account → box mapping on
# ``Organization.settings.tax.boxes`` (JSON, no migration — the same place
# every other per-org tax setting lives) turns that coding into a box.
#
# **The allocation reconciles by construction, not by rounding.** Nothing is
# prorated or split by percentage: every payment lands WHOLE in exactly one
# box, so the per-box Decimals sum to the vendor's reportable total to the
# cent with no residual to lose. Spend no rule matches is not dropped and not
# quietly folded — it lands in an explicit, named fallback box (default
# ``NEC-1``, matching the prior NEC behaviour exactly) and is separately
# surfaced as ``unmapped_paid`` / ``unmapped_payment_count`` so a preparer can
# see precisely how much money is sitting in the fallback for want of a rule.

FORM_NEC = "1099-NEC"
FORM_MISC = "1099-MISC"


@dataclass(frozen=True)
class Box1099:
    """One reportable box on a 1099 form.

    ``code`` is the stable wire/config identifier (``NEC-1``, ``MISC-6``);
    ``number`` + ``label`` are what the IRS form prints."""

    code: str
    form_type: str
    number: str
    label: str

    @property
    def display_label(self) -> str:
        return f"Box {self.number} — {self.label}"


# The AP-payable boxes. Deliberately not the whole IRS form: boxes an AP
# system cannot originate (federal income tax withheld, fishing-boat
# proceeds, 409A deferrals, FATCA) have no payment that could land in them,
# and offering a box nothing can populate invites a mapping rule that
# silently routes real money into a box the filing partner will reject.
# Insertion order is display + serialization order.
BOX_CATALOG: dict[str, Box1099] = {
    b.code: b
    for b in (
        Box1099("NEC-1", FORM_NEC, "1", "Nonemployee compensation"),
        Box1099("MISC-1", FORM_MISC, "1", "Rents"),
        Box1099("MISC-2", FORM_MISC, "2", "Royalties"),
        Box1099("MISC-3", FORM_MISC, "3", "Other income"),
        Box1099("MISC-6", FORM_MISC, "6", "Medical and health care payments"),
        Box1099("MISC-10", FORM_MISC, "10", "Gross proceeds paid to an attorney"),
    )
}

# Where unmapped spend lands. NEC box 1 is the correct default for an AP
# book — contractor payments are the overwhelming case — and it makes the
# default configuration behave exactly as the pre-allocation code did.
DEFAULT_FALLBACK_BOX = "NEC-1"


def normalize_box_code(value: object) -> str | None:
    """Canonicalise a configured box code (``"misc-6"`` → ``"MISC-6"``).

    Returns ``None`` for anything that isn't a box in ``BOX_CATALOG`` — a
    typo in admin-entered config must not resolve to *some* box."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper().replace("_", "-")
    return code if code in BOX_CATALOG else None


def _normalize_gl_key(value: object) -> str:
    """GL codes are compared case-insensitively with surrounding space
    stripped — the same code arrives from ERP sync, AI extraction and manual
    entry with inconsistent casing."""
    return str(value or "").strip().upper()


@dataclass(frozen=True)
class BoxMapping:
    """Resolved per-org GL-account → 1099-box mapping.

    Pure data + a pure resolver: built once per report from
    ``Organization.settings.tax.boxes`` and consulted per GL bucket."""

    fallback_box: str = DEFAULT_FALLBACK_BOX
    # Exact GL-code rules, normalized key → box code.
    gl_exact: Mapping[str, str] = field(default_factory=dict)
    # Prefix rules (config key ended in ``*``), longest prefix first so the
    # most specific rule wins deterministically.
    gl_prefix: tuple[tuple[str, str], ...] = ()
    # Per-vendor overrides, ``str(vendor_id)`` → box code. Beats every GL
    # rule: an attorney's payments are gross proceeds however they're coded.
    vendors: Mapping[str, str] = field(default_factory=dict)
    # Configured box codes we could not resolve, kept so the report can say so
    # instead of the money silently taking the fallback path. PII-free — these
    # are admin-entered config strings.
    invalid_box_codes: tuple[str, ...] = ()

    def resolve(self, *, vendor_id: object, gl_account: str | None) -> tuple[str, bool]:
        """Return ``(box_code, matched)`` for one GL bucket.

        ``matched`` is False when no rule applied and the amount is taking the
        fallback — that is what ``unmapped_paid`` counts."""
        vendor_key = str(vendor_id)
        override = self.vendors.get(vendor_key)
        if override:
            return override, True
        key = _normalize_gl_key(gl_account)
        if key:
            exact = self.gl_exact.get(key)
            if exact:
                return exact, True
            for prefix, box in self.gl_prefix:
                if key.startswith(prefix):
                    return box, True
        return self.fallback_box, False


def resolve_box_mapping(org_settings: Mapping | None) -> BoxMapping:
    """Build the mapping from ``Organization.settings.tax.boxes``.

    Shape (every key optional)::

        {"tax": {"boxes": {
            "fallback_box": "NEC-1",
            "gl_accounts": {"6010": "MISC-1", "64*": "MISC-6"},
            "vendors": {"<vendor uuid>": "MISC-10"}
        }}}

    A GL key ending in ``*`` is a prefix rule over the chart's hierarchy;
    longest prefix wins and an exact code beats any prefix. An unrecognised
    box code anywhere is DROPPED rather than guessed at — the affected spend
    then takes the fallback and shows up in ``unmapped_paid``, which is the
    visible symptom a preparer can act on. Never raises: a malformed settings
    blob degrades to the platform default, it does not take the 1099 report
    down in January."""
    invalid: list[str] = []

    raw: object = None
    if isinstance(org_settings, Mapping):
        tax_cfg = org_settings.get("tax")
        if isinstance(tax_cfg, Mapping):
            raw = tax_cfg.get("boxes")
    cfg: Mapping = raw if isinstance(raw, Mapping) else {}

    fallback = normalize_box_code(cfg.get("fallback_box"))
    if fallback is None and cfg.get("fallback_box") is not None:
        invalid.append(str(cfg.get("fallback_box")))
    fallback = fallback or DEFAULT_FALLBACK_BOX

    gl_exact: dict[str, str] = {}
    gl_prefix: list[tuple[str, str]] = []
    gl_cfg = cfg.get("gl_accounts")
    if isinstance(gl_cfg, Mapping):
        for key, value in gl_cfg.items():
            box = normalize_box_code(value)
            if box is None:
                invalid.append(str(value))
                continue
            norm = _normalize_gl_key(key)
            if not norm:
                continue
            if norm.endswith("*"):
                stem = norm[:-1]
                if stem:
                    gl_prefix.append((stem, box))
            else:
                gl_exact[norm] = box

    vendors: dict[str, str] = {}
    vendor_cfg = cfg.get("vendors")
    if isinstance(vendor_cfg, Mapping):
        for key, value in vendor_cfg.items():
            box = normalize_box_code(value)
            if box is None:
                invalid.append(str(value))
                continue
            vendors[str(key).strip()] = box

    if invalid:
        # Config, never PII — the box code an admin typed. Logged once per
        # report build so a broken mapping is diagnosable without reading JSON.
        logger.warning(
            "[tax] 1099 box mapping contains %d unrecognised box code(s): %s",
            len(invalid),
            ", ".join(sorted(set(invalid))[:10]),
        )

    return BoxMapping(
        fallback_box=fallback,
        gl_exact=gl_exact,
        # Longest prefix first — deterministic "most specific rule wins".
        gl_prefix=tuple(sorted(gl_prefix, key=lambda p: (-len(p[0]), p[0]))),
        vendors=vendors,
        invalid_box_codes=tuple(sorted(set(invalid))),
    )


@dataclass(frozen=True)
class BoxAllocation:
    """One box's share of a vendor's reportable total. Money is Decimal."""

    box: str
    form_type: str
    box_number: str
    label: str
    amount: Decimal
    payment_count: int
    # True when this box received spend no mapping rule matched (i.e. it is
    # the fallback and absorbed unmapped money). Surfaced so "everything is in
    # box 1" is distinguishable from "nothing was ever mapped".
    fallback: bool = False

    def to_dict(self) -> dict:
        return {
            "box": self.box,
            "form_type": self.form_type,
            "box_number": self.box_number,
            "label": self.label,
            "amount": str(self.amount),
            "payment_count": self.payment_count,
            "fallback": self.fallback,
        }


@dataclass(frozen=True)
class GLSpendBucket:
    """Reportable spend for one vendor under one GL account, straight out of
    the aggregation. ``gl_account`` is ``None`` for uncoded invoices."""

    gl_account: str | None
    amount: Decimal
    payment_count: int


@dataclass(frozen=True)
class BoxAllocationResult:
    allocations: tuple[BoxAllocation, ...]
    unmapped_amount: Decimal
    unmapped_payment_count: int


def allocate_boxes(
    buckets: Iterable[GLSpendBucket],
    mapping: BoxMapping,
    *,
    vendor_id: object,
) -> BoxAllocationResult:
    """Attribute a vendor's per-GL reportable spend to 1099 boxes.

    Whole-payment attribution — a bucket's Decimal moves intact into exactly
    one box — so ``sum(a.amount for a in allocations) == sum(b.amount for b in
    buckets)`` exactly, with no rounding step that could shed a cent. Buckets
    with no money and no payments are dropped (an invoice that was never
    paid)."""
    totals: dict[str, list] = {}
    unmapped = Decimal("0")
    unmapped_count = 0
    for bucket in buckets:
        if not bucket.amount and not bucket.payment_count:
            continue
        box_code, matched = mapping.resolve(vendor_id=vendor_id, gl_account=bucket.gl_account)
        if box_code not in BOX_CATALOG:  # pragma: no cover — mapping normalises
            box_code = DEFAULT_FALLBACK_BOX
        slot = totals.setdefault(box_code, [Decimal("0"), 0, False])
        slot[0] += bucket.amount
        slot[1] += bucket.payment_count
        if not matched:
            slot[2] = True
            unmapped += bucket.amount
            unmapped_count += bucket.payment_count

    order = list(BOX_CATALOG)
    allocations = tuple(
        BoxAllocation(
            box=code,
            form_type=BOX_CATALOG[code].form_type,
            box_number=BOX_CATALOG[code].number,
            label=BOX_CATALOG[code].label,
            amount=amount,
            payment_count=count,
            fallback=is_fallback,
        )
        for code, (amount, count, is_fallback) in sorted(
            totals.items(), key=lambda kv: order.index(kv[0])
        )
    )
    return BoxAllocationResult(
        allocations=allocations,
        unmapped_amount=unmapped,
        unmapped_payment_count=unmapped_count,
    )


def box_total_for_form(row: VendorReportRow, form_type: str) -> Decimal:
    """The part of a vendor's reportable total that belongs on ONE form.

    A vendor with rent (MISC-1) and contractor spend (NEC-1) gets two forms
    carrying two different figures; filing either one for the whole total is
    the mis-report this exists to prevent. Falls back to the vendor's whole
    reportable total when no allocation is present (a hand-built row)."""
    if not row.box_allocations:
        return row.ytd_paid
    return sum((a.amount for a in row.box_allocations if a.form_type == form_type), Decimal("0"))


def forms_requiring_filing(allocations: Iterable[BoxAllocation], year: int) -> tuple[str, ...]:
    """The forms a payee must receive for ``year``, in catalog order.

    A form is required when ANY of its boxes reaches that box's own threshold
    (``reporting_threshold``) — not when the payee's combined total across
    every form does. $400 of rent plus $300 of contract work is $700, yet
    neither form is required; $50 of royalties is far below $600, yet a MISC
    is. Once a form is required, its other populated boxes ride on it."""
    required: set[str] = set()
    for alloc in allocations:
        if alloc.amount >= reporting_threshold(alloc.box, year):
            required.add(alloc.form_type)
    return tuple(f for f in (FORM_NEC, FORM_MISC) if f in required)


def row_requires_form(row: VendorReportRow, form_type: str) -> bool:
    """Whether ``row`` must be filed on ``form_type``.

    Rows the aggregation produced carry ``required_forms``. A hand-built row
    with no allocation falls back to its own ``over_threshold`` flag, matching
    ``box_total_for_form``'s whole-total fallback for the same rows."""
    if not row.box_allocations:
        return row.over_threshold
    return form_type in row.required_forms


def aggregate_box_allocations(rows: Sequence[VendorReportRow]) -> list[BoxAllocation]:
    """Roll per-vendor allocations up into one per-box total for a population.

    Exact-Decimal addition of figures already denominated in the reporting
    currency — no conversion, no rounding."""
    totals: dict[str, list] = {}
    for row in rows:
        for alloc in row.box_allocations:
            slot = totals.setdefault(alloc.box, [Decimal("0"), 0, False])
            slot[0] += alloc.amount
            slot[1] += alloc.payment_count
            slot[2] = slot[2] or alloc.fallback
    order = list(BOX_CATALOG)
    return [
        BoxAllocation(
            box=code,
            form_type=BOX_CATALOG[code].form_type,
            box_number=BOX_CATALOG[code].number,
            label=BOX_CATALOG[code].label,
            amount=amount,
            payment_count=count,
            fallback=is_fallback,
        )
        for code, (amount, count, is_fallback) in sorted(
            totals.items(), key=lambda kv: order.index(kv[0])
        )
    ]


def _box_summary(rows: Sequence[VendorReportRow], total_reportable: Decimal) -> dict:
    """The summary-level box breakdown + its reconciliation proof.

    ``box_unallocated`` is the residual between the filed total and the sum of
    the boxes. It is zero by construction (whole-payment attribution), and it
    is reported anyway — a reconciliation guarantee nobody can read is a
    guarantee nobody can check."""
    allocations = aggregate_box_allocations(rows)
    allocated = sum((a.amount for a in allocations), Decimal("0"))
    residual = total_reportable - allocated
    return {
        "box_allocations": [a.to_dict() for a in allocations],
        "total_unmapped": str(sum((r.unmapped_paid for r in rows), Decimal("0"))),
        "unmapped_payment_count": sum(r.unmapped_payment_count for r in rows),
        "box_unallocated": str(residual),
        "box_allocation_reconciled": residual == 0,
    }


@dataclass
class VendorReportRow:
    vendor_id: uuid.UUID
    vendor_name: str
    tax_id: str | None
    tax_classification: str | None
    is_1099_eligible: bool
    w9_received_date: date | None
    w9_on_file: bool
    # REPORTABLE year-to-date paid — card-rail payments are excluded (they are
    # the card settlement entity's 1099-K, not our 1099). This is the figure
    # that lands in the 1099 box amount.
    ytd_paid: Decimal
    over_threshold: bool
    payment_count: int
    # True once a TIN match has stamped ``Vendor.tin_verified_at``. Defaulted
    # so older call sites that build rows by hand keep working.
    tin_verified: bool = False
    # The card-rail total deliberately EXCLUDED from ``ytd_paid``, surfaced so
    # an operator can reconcile against the processor's 1099-K instead of the
    # money silently vanishing from the report. Defaulted for the same reason
    # as ``tin_verified``.
    card_paid: Decimal = Decimal("0")
    card_payment_count: int = 0
    # Completed payments whose outflow could not be expressed in the reporting
    # currency at all (see ``currency_conversion.payment_reporting_amount_sql``).
    # A COUNT and not a total, deliberately: summing figures across unknown
    # currencies is exactly the mixture being refused. Non-zero means this
    # vendor's box amount is UNDERSTATED and a human has to establish the
    # home-currency figure before filing.
    unconverted_payment_count: int = 0
    # Per-box split of ``ytd_paid``. Whole-payment attribution, so these sum to
    # ``ytd_paid`` exactly — see ``allocate_boxes``. Empty on a hand-built row
    # (no aggregation ran), which ``box_unallocated`` then reports honestly
    # rather than pretending the total is allocated.
    box_allocations: tuple[BoxAllocation, ...] = ()
    # The slice of ``ytd_paid`` that reached the fallback box because no
    # mapping rule matched it. NOT money that went missing — it is filed, in
    # the fallback box — but it is money nobody has told us the box for, which
    # is the preparer's worklist before filing.
    unmapped_paid: Decimal = Decimal("0")
    unmapped_payment_count: int = 0
    # The forms this payee must receive (``forms_requiring_filing``) — each
    # box measured against its own per-year threshold. ``over_threshold`` is
    # exactly "this tuple is non-empty" for every aggregated row.
    required_forms: tuple[str, ...] = ()

    @property
    def box_unallocated(self) -> Decimal:
        """Reportable total minus the sum of the boxes.

        Zero for any row the aggregation produced (whole-payment attribution
        cannot lose a cent); equal to ``ytd_paid`` for a hand-built row that
        carries no allocation. Reported either way — a reconciliation
        guarantee that isn't published can't be checked."""
        return self.ytd_paid - sum((a.amount for a in self.box_allocations), Decimal("0"))

    def to_dict(self) -> dict:
        return {
            "vendor_id": str(self.vendor_id),
            "vendor_name": self.vendor_name,
            "tax_id": self.tax_id,
            "tax_classification": self.tax_classification,
            "is_1099_eligible": self.is_1099_eligible,
            "w9_received_date": self.w9_received_date.isoformat()
            if self.w9_received_date
            else None,
            "w9_on_file": self.w9_on_file,
            "ytd_paid": str(self.ytd_paid),
            "over_threshold": self.over_threshold,
            "payment_count": self.payment_count,
            "tin_verified": self.tin_verified,
            "card_paid": str(self.card_paid),
            "card_payment_count": self.card_payment_count,
            "unconverted_payment_count": self.unconverted_payment_count,
            "box_allocations": [a.to_dict() for a in self.box_allocations],
            "unmapped_paid": str(self.unmapped_paid),
            "unmapped_payment_count": self.unmapped_payment_count,
            "box_unallocated": str(self.box_unallocated),
            "required_forms": list(self.required_forms),
        }


def _total_unconverted_payments(rows: list[VendorReportRow]) -> int:
    """Completed payments across every vendor row that could not be expressed
    in the reporting currency, and so are missing from ``total_reportable``.

    Surfaced alongside the totals for the same reason ``total_card_excluded``
    is: money that has been deliberately left out of a filed figure must be
    visible and reconcilable, never silently absent. Non-zero means the report
    is not yet filable as it stands."""
    return sum(r.unconverted_payment_count for r in rows)


def _total_card_excluded(rows: list[VendorReportRow]) -> str:
    """Card-rail spend for the year across EVERY vendor row — the money the
    1099 deliberately leaves out because the card settlement entity reports it
    on a 1099-K. Spans all vendors (not just the eligible-over-threshold ones
    ``total_reportable`` covers) because it exists to be reconciled against the
    processor's own filing, which knows nothing about our eligibility flags."""
    return str(sum((r.card_paid for r in rows), Decimal("0")))


@dataclass
class Report1099:
    year: int
    generated_at: date
    rows: list[VendorReportRow]
    # The headline (§6041 / §6041A) threshold for ``year`` — $600 before 2026,
    # $2,000 from 2026. Derived from the year unless given; per-box exceptions
    # (royalties $10, attorney gross proceeds $600) are applied per row.
    threshold_usd: Decimal | None = None
    # The currency the reportable totals + per-vendor ``ytd_paid`` are actually
    # denominated in — the org's reporting (home) currency. Not a label applied
    # after the fact: the aggregation only counts a payment it can PROVE is
    # denominated in this currency (``currency_conversion.payment_reporting_amount_sql``),
    # and counts the rest on ``unconverted_payment_count`` instead. 1099 is a
    # US/IRS concept (dollars), but a non-USD tenant's home currency is
    # surfaced honestly here instead of being silently called "USD".
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.threshold_usd is None:
            self.threshold_usd = general_reporting_threshold(self.year)

    def summary(self) -> dict:
        eligible_over = [r for r in self.rows if r.is_1099_eligible and r.over_threshold]
        total_reportable_dec = sum((r.ytd_paid for r in eligible_over), Decimal("0"))
        total_reportable = str(total_reportable_dec)
        return {
            "year": self.year,
            "threshold_usd": str(self.threshold_usd),
            "currency": self.currency,
            "vendor_count_total": len(self.rows),
            "vendor_count_eligible_over_threshold": len(eligible_over),
            "vendor_count_over_threshold_without_w9": sum(
                1 for r in eligible_over if not r.w9_on_file
            ),
            "total_reportable": total_reportable,
            # Back-compat alias of ``total_reportable`` — historically named
            # ``_usd`` before the currency became explicit. Same value; kept so
            # existing API consumers don't break. Prefer ``total_reportable`` +
            # ``currency``.
            "total_reportable_usd": total_reportable,
            "total_card_excluded": _total_card_excluded(self.rows),
            "unconverted_payment_count": _total_unconverted_payments(self.rows),
            # Per-box split of exactly the population `total_reportable` covers.
            **_box_summary(eligible_over, total_reportable_dec),
        }

    def to_dict(self) -> dict:
        return {
            **self.summary(),
            "generated_at": self.generated_at.isoformat(),
            "rows": [r.to_dict() for r in self.rows],
        }


async def build_1099_report(
    db: AsyncSession,
    organization_id: uuid.UUID,
    year: int,
    reporting_currency: str = "USD",
    org_settings: Mapping | None = None,
) -> Report1099:
    """Aggregate completed payments per vendor for the given calendar year.

    Only payments with ``status='completed'`` and a ``completed_at`` in
    the target year are counted — pending/failed payments don't show up
    on a 1099. ``completed_at`` is preferred over the invoice date
    because the IRS reports payments in the year they were actually made.

    **Card-rail payments are excluded from ``ytd_paid``** and totalled
    separately on ``card_paid``: the card settlement entity reports those on a
    Form 1099-K, so putting them in our 1099 box amount over-reports the
    vendor and double-counts the same dollar. The classification lives in
    ``services/payment_methods`` — see that module for the rail-by-rail
    treatment and the drift guard.

    ``reporting_currency`` is the org's reporting (home) currency, resolved by
    the caller via ``currency_conversion.resolve_reporting_currency``. It is
    **not** a label applied to whatever the SUM produced: ``Payment.amount`` is
    denominated in the INVOICE's currency (see
    ``international_payments.prepare_international_payment``), so a book with
    one EUR invoice in it used to add 1 000 EUR into a USD box amount at face
    value. ``currency_conversion.payment_reporting_amount_sql`` resolves each
    payment's outflow into ``reporting_currency`` — the rate-locked
    ``source_amount`` when the payment carries a home-currency leg, otherwise
    ``amount`` when the invoice is already in that currency — and a payment
    neither rung can establish is left OUT of ``ytd_paid`` / ``card_paid`` and
    counted on ``unconverted_payment_count`` instead. Nothing is converted at
    read time; a rate fetched on a read would make a filed historical total
    move under the reader.

    ``org_settings`` carries the per-org GL-account → 1099-box mapping
    (``settings.tax.boxes``). The aggregation groups by the paying invoice's
    ``gl_account`` as well as by vendor, and every GL bucket is attributed
    WHOLE to one box, so the per-box figures sum to ``ytd_paid`` to the cent.
    Omit it and every vendor's spend lands in the default fallback box
    (``NEC-1``) — exactly the pre-allocation behaviour — with the amount
    surfaced on ``unmapped_paid`` rather than presented as a mapped figure.
    """
    # Conditional aggregation splits the joined payments in one pass. ``case``
    # with no ``else_`` yields NULL on the other branch, which ``sum``/``count``
    # skip — so the Decimal("0") coalesce fallback (never int 0, which can
    # promote the aggregate off Numeric and mis-classify a vendor sitting
    # exactly at the $600 threshold) still governs the empty case.
    is_card = card_payment_method_clause(Payment.method)
    reported = payment_reporting_amount_sql(
        reporting_currency=reporting_currency,
        payment_amount=Payment.amount,
        payment_source_amount=Payment.source_amount,
        payment_source_currency=Payment.source_currency,
        invoice_currency=Invoice.currency,
    )
    countable = reported.is_expressible
    # The paying invoice's GL coding is the box driver. Grouping by it as well
    # as by vendor keeps this ONE query: the per-vendor figures are recovered
    # by adding the buckets back up, which is exact (same Decimals), while the
    # buckets themselves are what the box mapping is applied to.
    gl_key = Invoice.gl_account
    q = (
        select(
            Vendor.id.label("vendor_id"),
            Vendor.name.label("vendor_name"),
            Vendor.tax_id.label("tax_id"),
            Vendor.tax_classification.label("tax_classification"),
            Vendor.is_1099_eligible.label("is_1099_eligible"),
            Vendor.w9_received_date.label("w9_received_date"),
            Vendor.w9_file_key.label("w9_file_key"),
            Vendor.tin_verified_at.label("tin_verified_at"),
            gl_key.label("gl_account"),
            func.coalesce(
                func.sum(case((and_(~is_card, countable), reported.amount))), Decimal("0")
            ).label("ytd_paid"),
            func.count(case((and_(~is_card, countable), Payment.id))).label("payment_count"),
            func.coalesce(
                func.sum(case((and_(is_card, countable), reported.amount))), Decimal("0")
            ).label("card_paid"),
            func.count(case((and_(is_card, countable), Payment.id))).label("card_payment_count"),
            func.count(case((~countable, Payment.id))).label("unconverted_payment_count"),
        )
        .select_from(Vendor)
        .outerjoin(Invoice, Invoice.vendor_id == Vendor.id)
        .outerjoin(
            Payment,
            (Payment.invoice_id == Invoice.id)
            & (Payment.status == "completed")
            # `Payment.completed_at` is `TIMESTAMPTZ` — a bare `EXTRACT(YEAR
            # FROM …)` resolves against the Postgres session `timezone` GUC,
            # not UTC. `func.timezone("UTC", …)` normalises to the UTC
            # calendar year first (the same pattern
            # `bank_reconciliation.py`'s `sent_on_expr` uses), so a payment
            # completed late on Dec 31 UTC doesn't drift into next year's
            # filing — or the reverse — on a non-UTC server session.
            & (extract("year", func.timezone("UTC", Payment.completed_at)) == year),
        )
        .where(Vendor.organization_id == organization_id)
        .group_by(
            Vendor.id,
            Vendor.name,
            Vendor.tax_id,
            Vendor.tax_classification,
            Vendor.is_1099_eligible,
            Vendor.w9_received_date,
            Vendor.w9_file_key,
            Vendor.tin_verified_at,
            gl_key,
        )
        .order_by(Vendor.name, gl_key)
    )
    result = await db.execute(q)
    mapping = resolve_box_mapping(org_settings)

    # One vendor now spans several GL buckets; fold them back together in
    # issue order (the query is ordered by vendor name) so the row list stays
    # exactly what it was before allocation existed.
    ordered_vendor_ids: list[uuid.UUID] = []
    per_vendor: dict[uuid.UUID, dict] = {}
    for row in result.all():
        acc = per_vendor.get(row.vendor_id)
        if acc is None:
            ordered_vendor_ids.append(row.vendor_id)
            acc = per_vendor[row.vendor_id] = {"first": row, "buckets": []}
        acc["buckets"].append(
            GLSpendBucket(
                gl_account=row.gl_account,
                amount=Decimal(row.ytd_paid or Decimal("0")),
                payment_count=int(row.payment_count or 0),
            )
        )
        acc["card_paid"] = acc.get("card_paid", Decimal("0")) + Decimal(
            row.card_paid or Decimal("0")
        )
        acc["card_payment_count"] = acc.get("card_payment_count", 0) + int(
            row.card_payment_count or 0
        )
        acc["unconverted_payment_count"] = acc.get("unconverted_payment_count", 0) + int(
            row.unconverted_payment_count or 0
        )

    rows = []
    for vendor_id in ordered_vendor_ids:
        acc = per_vendor[vendor_id]
        first = acc["first"]
        buckets: list[GLSpendBucket] = acc["buckets"]
        # Exact-Decimal re-sum of the buckets — never a float, never a rounded
        # figure. This IS `ytd_paid`, and the box allocation partitions the
        # same buckets, which is why the two can never disagree.
        ytd = sum((b.amount for b in buckets), Decimal("0"))
        # Card money is excluded from every box by construction: the buckets
        # the allocation sees carry only the non-card population
        # (`~is_card` in the aggregation), and `card_paid` is summed apart.
        allocation = allocate_boxes(buckets, mapping, vendor_id=vendor_id)
        # Each box against its OWN per-year threshold, never the combined
        # total against one $600 figure — see `forms_requiring_filing`.
        required_forms = forms_requiring_filing(allocation.allocations, year)
        rows.append(
            VendorReportRow(
                vendor_id=first.vendor_id,
                vendor_name=first.vendor_name,
                tax_id=first.tax_id,
                tax_classification=first.tax_classification,
                is_1099_eligible=bool(first.is_1099_eligible),
                w9_received_date=first.w9_received_date,
                # `w9_file_key` is shared with W-8 uploads (no separate vendor
                # column — see `storage.upload_tax_form_file`), so a bare
                # not-None check reported a foreign vendor who filed a W-8 as
                # "1099-ready", rolling them into the Tax1099 export as if
                # they'd filed the domestic form. The form type is encoded in
                # the key's path segment — only an actual W-9 counts here.
                w9_on_file=tax_form_type_from_key(first.w9_file_key) == "w9",
                ytd_paid=ytd,
                over_threshold=bool(required_forms),
                payment_count=sum(b.payment_count for b in buckets),
                tin_verified=first.tin_verified_at is not None,
                card_paid=acc["card_paid"],
                card_payment_count=acc["card_payment_count"],
                unconverted_payment_count=acc["unconverted_payment_count"],
                box_allocations=allocation.allocations,
                unmapped_paid=allocation.unmapped_amount,
                unmapped_payment_count=allocation.unmapped_payment_count,
                required_forms=required_forms,
            )
        )

    return Report1099(
        year=year,
        generated_at=utc_today(),
        rows=rows,
        currency=(reporting_currency or "USD").upper(),
    )


# ---------------------------------------------------------------------------
# 1099 vendor dashboard
# ---------------------------------------------------------------------------


def _row_needs_attention(row: VendorReportRow) -> bool:
    """A 1099-eligible vendor that can't be cleanly filed as things stand.

    Two ways in:

    * over the $600 threshold and missing a W-9 on file or a verified TIN, or
    * holding a completed payment whose outflow could not be expressed in the
      reporting currency (``unconverted_payment_count``), which means the box
      amount on record is UNDERSTATED by that payment. That one is flagged
      regardless of the threshold, precisely because the missing money is what
      could carry the vendor over it.
    """
    if row.is_1099_eligible and row.unconverted_payment_count:
        return True
    return (
        row.is_1099_eligible and row.over_threshold and (not row.w9_on_file or not row.tin_verified)
    )


@dataclass
class Dashboard1099:
    """A compliance-readiness view over the 1099 report rows.

    Same underlying aggregation as ``build_1099_report``; this adds the
    W-9 / TIN-verified / threshold readiness flags an AP team needs to know
    who still needs chasing before filing season.
    """

    year: int
    generated_at: date
    rows: list[VendorReportRow]
    # The headline (§6041 / §6041A) threshold for ``year`` — $600 before 2026,
    # $2,000 from 2026. Derived from the year unless given; per-box exceptions
    # (royalties $10, attorney gross proceeds $600) are applied per row.
    threshold_usd: Decimal | None = None
    # See ``Report1099.currency`` — the reporting (home) currency the totals are
    # denominated in, enforced by the aggregation rather than asserted.
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.threshold_usd is None:
            self.threshold_usd = general_reporting_threshold(self.year)

    def summary(self) -> dict:
        eligible = [r for r in self.rows if r.is_1099_eligible]
        eligible_over = [r for r in eligible if r.over_threshold]
        total_reportable_dec = sum((r.ytd_paid for r in eligible_over), Decimal("0"))
        total_reportable = str(total_reportable_dec)
        return {
            "year": self.year,
            "threshold_usd": str(self.threshold_usd),
            "currency": self.currency,
            "vendor_count_total": len(self.rows),
            "vendor_count_eligible": len(eligible),
            "vendor_count_eligible_over_threshold": len(eligible_over),
            "vendor_count_over_threshold_without_w9": sum(
                1 for r in eligible_over if not r.w9_on_file
            ),
            "vendor_count_over_threshold_tin_unverified": sum(
                1 for r in eligible_over if not r.tin_verified
            ),
            "vendor_count_needs_attention": sum(1 for r in self.rows if _row_needs_attention(r)),
            "total_reportable": total_reportable,
            # Back-compat alias — see ``Report1099.summary``.
            "total_reportable_usd": total_reportable,
            "total_card_excluded": _total_card_excluded(self.rows),
            "unconverted_payment_count": _total_unconverted_payments(self.rows),
            **_box_summary(eligible_over, total_reportable_dec),
        }

    def to_dict(self) -> dict:
        return {
            **self.summary(),
            "generated_at": self.generated_at.isoformat(),
            "rows": [
                {**r.to_dict(), "needs_attention": _row_needs_attention(r)} for r in self.rows
            ],
        }


async def build_1099_dashboard(
    db: AsyncSession,
    organization_id: uuid.UUID,
    year: int,
    reporting_currency: str = "USD",
    org_settings: Mapping | None = None,
) -> Dashboard1099:
    """Build the 1099-eligible vendor dashboard for a year.

    Reuses ``build_1099_report``'s aggregation and re-frames it around filing
    readiness (W-9-on-file, TIN-verified, threshold, needs-attention)."""
    report = await build_1099_report(db, organization_id, year, reporting_currency, org_settings)
    return Dashboard1099(
        year=report.year,
        generated_at=report.generated_at,
        rows=report.rows,
        currency=report.currency,
    )
