"""Positive Pay — return classifier (pure) + file-item builders (async DB).

Positive Pay is a treasury fraud control. We give the bank the cheques we
*issued*; when an item is later presented for payment, the bank matches it
against that file and flags anything that doesn't line up. This module has two
halves:

  * **The pure return classifier** — :func:`classify_presented_items` takes the
    items the bank reports as *presented* and the items we *issued*, and labels
    each presented item ``matched_ok`` / ``amount_mismatch`` (an ALTERED cheque —
    fraud) / ``not_on_file`` (a cheque we never wrote — fraud). No DB, no I/O, no
    account numbers — just check numbers + amounts.

  * **The async file-item builders** — :func:`build_check_issue_items` and
    :func:`build_ach_authorization_items` project tenant rows (Payments /
    Vendors) into the formatter dataclasses the
    ``positive_pay_adapters`` layer renders. These touch the DB but never log a
    full account / routing number (PII invariant).

Money is :class:`~decimal.Decimal` (never float). See
``backend/docs/positive-pay.md``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.models.payment import Payment, PaymentRun
from app.models.vendor import Vendor
from app.services.payment_runs import one_currency
from app.services.positive_pay_adapters import AchAuthorizationItem, CheckIssueItem
from app.tenant import apply_entity_scope
from app.utils.dates import utc_today

# Default amount tolerance for the matched/altered decision (one cent).
DEFAULT_AMOUNT_TOLERANCE = Decimal("0.01")

# Payment statuses that DON'T represent a cheque actually issued to the bank —
# excluded from a check-issue file (we never handed these to anyone).
_EXCLUDED_PAYMENT_STATUSES = frozenset({"failed", "cancelled", "voided"})

# Return classifications.
CLASS_MATCHED_OK = "matched_ok"
CLASS_AMOUNT_MISMATCH = "amount_mismatch"
CLASS_NOT_ON_FILE = "not_on_file"


# ---------------------------------------------------------------------------
# Pure return-classifier dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PresentedItem:
    """One item the bank reports as presented for payment. Carries only a
    check number + amount — never an account number."""

    check_number: str | None
    amount: Decimal | None


@dataclass
class IssuedItem:
    """One cheque we issued (the file we gave the bank), projected to the two
    fields the classifier matches on."""

    check_number: str
    amount: Decimal


@dataclass
class ReturnItemResult:
    """Per-presented-item verdict from :func:`classify_presented_items`.

    ``matched_check_number`` is the normalised number of the issued cheque this
    presented item mapped to (set for ``matched_ok`` and ``amount_mismatch``;
    ``None`` for ``not_on_file``). PII-free."""

    check_number: str | None
    classification: str
    matched_check_number: str | None = None
    presented_amount: Decimal | None = None
    issued_amount: Decimal | None = None


@dataclass
class ReturnClassification:
    """The classifier's whole-file outcome — per-item results + roll-up counts."""

    results: list[ReturnItemResult] = field(default_factory=list)
    presented_count: int = 0
    matched_ok: int = 0
    amount_mismatch: int = 0
    not_on_file: int = 0


def normalize_check_number(value: str | None) -> str:
    """Canonicalise a check number for matching: strip, upper-case, drop every
    non-alphanumeric character. ``None`` / blank → ``""``.

    So ``"1001"``, ``"#1001"`` and ``"chk-1001"`` collapse to a comparable form
    (``"1001"`` / ``"CHK1001"``), mirroring ``vendor_statement_recon`` invoice
    normalisation.
    """
    if not value:
        return ""
    return "".join(c for c in value.upper() if c.isalnum())


def classify_presented_items(
    presented: list[PresentedItem],
    issued: list[IssuedItem],
    *,
    amount_tolerance: Decimal = DEFAULT_AMOUNT_TOLERANCE,
) -> ReturnClassification:
    """Classify each presented item against the issued file.

    For every presented item, look it up by normalised check number among the
    issued cheques:

      * ``matched_ok``       — found, and ``|presented - issued| <= tolerance``.
      * ``amount_mismatch``  — found by number, amount differs beyond tolerance
                               (an ALTERED cheque — fraud signal).
      * ``not_on_file``      — no issued cheque with that number (a cheque we
                               never wrote — fraud signal).

    Pure: no DB, no I/O, no account numbers. An issued cheque may map more than
    one presented item (we don't consume them) — the bank's return is the
    authority on what was presented; double-presentation of one number is itself
    worth surfacing, and both land as results.
    """
    by_number: dict[str, IssuedItem] = {}
    for it in issued:
        key = normalize_check_number(it.check_number)
        if key:
            # First-wins on a duplicated issued number (shouldn't happen, but be
            # deterministic).
            by_number.setdefault(key, it)

    results: list[ReturnItemResult] = []
    matched_ok = amount_mismatch = not_on_file = 0

    for p in presented:
        key = normalize_check_number(p.check_number)
        issued_item = by_number.get(key) if key else None

        if issued_item is None:
            not_on_file += 1
            results.append(
                ReturnItemResult(
                    check_number=p.check_number,
                    classification=CLASS_NOT_ON_FILE,
                    matched_check_number=None,
                    presented_amount=p.amount,
                    issued_amount=None,
                )
            )
            continue

        presented_amount = p.amount if p.amount is not None else Decimal("0")
        if abs(presented_amount - issued_item.amount) <= amount_tolerance:
            matched_ok += 1
            classification = CLASS_MATCHED_OK
        else:
            amount_mismatch += 1
            classification = CLASS_AMOUNT_MISMATCH

        results.append(
            ReturnItemResult(
                check_number=p.check_number,
                classification=classification,
                matched_check_number=key,
                presented_amount=p.amount,
                issued_amount=issued_item.amount,
            )
        )

    return ReturnClassification(
        results=results,
        presented_count=len(presented),
        matched_ok=matched_ok,
        amount_mismatch=amount_mismatch,
        not_on_file=not_on_file,
    )


# ---------------------------------------------------------------------------
# Async file-item builders (DB → formatter dataclasses)
# ---------------------------------------------------------------------------


async def build_check_issue_items(
    db: AsyncSession,
    *,
    run: PaymentRun,
    entity_id: uuid.UUID | None,
    account_number: str = "",
) -> tuple[list[CheckIssueItem], Decimal, list[tuple[str, uuid.UUID, Decimal]], str | None]:
    """Build the check-issue items for a payment run.

    Selects the run's cheque payments (``method == "check"``, excluding
    ``failed`` / ``cancelled`` / ``voided`` AT THE TIME THIS RUNS), joins
    ``Invoice`` for the payee name, and projects each into a
    :class:`CheckIssueItem`. ``check_number`` is the Payment's ``reference``;
    ``issue_date`` is the run's ``executed_at`` date (or today if it hasn't
    executed). ``account_number`` (the org's originating cheque account) is
    stamped onto every item by the caller.

    Returns ``(items, total_amount, mapping, currency)`` where ``mapping`` is a list of
    ``(normalized_check_number, invoice_id, amount)`` triples so the CALLER can
    persist a point-in-time snapshot (see the check-issue file's
    ``meta["issued_map"]``). Because this is a LIVE query, calling it again
    later (e.g. at return-processing time) reflects payments' CURRENT status,
    not what was actually on the file already sent to the bank — a cheque
    voided after the file was sent would silently drop out. Callers that need
    "what did we tell the bank" must persist this call's result once, at
    generation time, and read that snapshot back rather than re-calling this
    function (see ``docs/positive-pay.md`` § Return processing). No account
    number is ever logged here.

    ``currency`` is what ``total_amount`` is denominated in: the one currency
    the included cheques' invoices agree on, or ``None`` when they carry none
    or disagree (:func:`~app.services.payment_runs.one_currency`). A
    ``Payment.amount`` is in its INVOICE's currency, not the org's reporting
    currency, so stamping the reporting code on this total — as the file did
    until 2026-09 — labelled a EUR cheque run in dollars for a USD-reporting
    org.
    """
    query = (
        select(Payment, Invoice.vendor_name, func.upper(Invoice.currency))
        .join(Invoice, Payment.invoice_id == Invoice.id)
        .where(
            Payment.payment_run_id == run.id,
            Payment.method == "check",
            Payment.status.notin_(_EXCLUDED_PAYMENT_STATUSES),
        )
    )
    query = apply_entity_scope(query, Payment, entity_id)

    rows = (await db.execute(query)).all()

    issue_date = run.executed_at.date() if run.executed_at else utc_today()

    items: list[CheckIssueItem] = []
    total = Decimal("0")
    mapping: list[tuple[str, uuid.UUID, Decimal]] = []
    currencies: list[str | None] = []

    for payment, vendor_name, currency in rows:
        currencies.append(currency)
        check_number = payment.reference or ""
        amount = payment.amount if payment.amount is not None else Decimal("0")
        items.append(
            CheckIssueItem(
                check_number=check_number,
                payee=vendor_name or "",
                amount=amount,
                issue_date=issue_date,
                account_number=account_number,
            )
        )
        total += amount
        key = normalize_check_number(check_number)
        if key:
            mapping.append((key, payment.invoice_id, amount))

    return items, total, mapping, one_currency(currencies)


async def build_ach_authorization_items(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
) -> list[AchAuthorizationItem]:
    """Build the ACH debit-authorization items for an org.

    Selects ``active`` vendors that are not payments-blocked (sanctions/compliance
    hold) whose ``bank_details`` carry both a routing and an account number, and
    projects each into an :class:`AchAuthorizationItem`. Vendors without ACH bank
    details are skipped (there's nothing to authorize). A vendor with
    ``payments_blocked=True`` is excluded even if its ``status`` is still
    ``"active"`` — the two flags are independent, and the bank must never be told
    to honor a debit the compliance layer intended to stop. Full routing /
    account numbers go only into the returned items (and thence the rendered
    file) — never a log line.
    """
    query = select(Vendor).where(
        Vendor.organization_id == org_id,
        Vendor.status == "active",
        Vendor.payments_blocked.is_(False),
    )
    query = apply_entity_scope(query, Vendor, entity_id)

    vendors = (await db.execute(query)).scalars().all()

    items: list[AchAuthorizationItem] = []
    for v in vendors:
        bank = v.bank_details or {}
        routing = (bank.get("routing_number") or "").strip()
        account = (bank.get("account_number") or "").strip()
        if not routing or not account:
            continue
        items.append(
            AchAuthorizationItem(
                vendor_name=v.name or "",
                routing_number=routing,
                account_number=account,
                status=v.status or "active",
            )
        )

    return items
