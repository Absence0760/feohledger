"""Compute warnings and fraud flags for invoices — persisted on write.

Also creates exception records for issues that need human resolution.
"""

import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.services.invoice_warning_catalog import warning
from app.services.matching_rules import resolve_match_rule
from app.services.po_matching import format_quantity, match_invoice_to_po
from app.utils.dates import utc_today

logger = logging.getLogger(__name__)

# ---------- Fraud rules: per-org tunable thresholds -----------------------
#
# Defaults are conservative — they should never fire on a healthy AP flow.
# Org admins override via PATCH /api/organization with `settings.fraud_rules`.
# Every rule key listed here is honored by `_fraud_config()`; unknown keys
# are ignored so we can ship new rules without a settings migration.

DEFAULT_FRAUD_RULES: dict = {
    # Master switch per rule. False suppresses both the warning and the
    # exception so an org can opt out of noisy rules without our help.
    "round_amount_enabled": True,
    "future_date_enabled": True,
    "bank_change_enabled": True,
    "stat_anomaly_enabled": True,
    "rush_payment_enabled": True,
    "new_vendor_large_enabled": True,
    "personal_email_enabled": True,
    "price_variance_enabled": True,  # per-vendor line-item price deviation
    "line_total_mismatch_enabled": True,  # summed line totals vs the header amount
    "llm_anomaly_enabled": False,  # opt-in: costs an LLM call per invoice
    # Structuring guard: aggregates a vendor's OTHER recent invoices so the
    # approval max/CFO gate can escalate on the SUM even when no single
    # invoice crosses it alone (splitting one payable into several small
    # ones). Enforced in services.structuring / services.review, not this
    # module — listed here so every fraud-rule knob lives in one place.
    "structuring_enabled": True,
    "structuring_window_days": 7,
    # Threshold knobs. Whatever the org sets here drives the warning.
    "round_amount_min": "1000",  # amounts >= this AND an even multiple of 100 flag
    "rush_payment_max_days": 3,  # due_date within N days of invoice_date
    "new_vendor_max_age_days": 30,  # vendor created within N days
    "new_vendor_large_amount": "10000",
    "stat_anomaly_sigma": 2.0,  # amount > mean + N*stdev
    "stat_anomaly_min_history": 3,  # need at least N prior approved invoices
    # Generic personal-email domains — case-insensitive match on the
    # vendor's email host. Anything outside this list is treated as a
    # business address (not perfect, but good enough as a flag).
    "personal_email_domains": [
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "aol.com",
        "icloud.com",
        "protonmail.com",
        "proton.me",
        "live.com",
        "msn.com",
        "yandex.com",
        "mail.com",
        "gmx.com",
    ],
}


# Warning categories appended directly onto `invoice.warnings` by callers
# OTHER than this module — currently all in `services/extraction.py`, before
# it calls `refresh_warnings` as the last step of `run_extraction`.
# `refresh_warnings` rebuilds every category IT owns from scratch on every
# call (that's the point — a fully fresh re-derivation), but it must not
# blind-overwrite `invoice.warnings` and silently drop these, since it never
# computes them itself and has no other way to reconstruct them.
UPSTREAM_WARNING_TYPES = frozenset(
    {
        "extraction_self_correction",
        "gl_account_invalid",
        "duplicate_similar",
    }
)


def _fraud_config(org_settings: dict | None) -> dict:
    """Merge org overrides over the defaults. Org settings.fraud_rules
    can omit keys to inherit; unknown keys are dropped silently."""
    overrides = (org_settings or {}).get("fraud_rules") or {}
    merged = dict(DEFAULT_FRAUD_RULES)
    for k, v in overrides.items():
        if k in merged:
            merged[k] = v
    return merged


def _status_str(status) -> str:
    """Normalize an invoice status into a plain string.

    Handles three shapes that reach us in practice:
      - `InvoiceStatus` (StrEnum on the SQLA model) — `.value` works
        but `str(...)` is also correct since StrEnum inherits from str.
      - Plain string, e.g. after `setattr(invoice, "status", "x")` in
        `update_invoice` — has no `.value` attribute. This was a real
        500 in production until this helper landed.
      - Test-fixture mocks like `SimpleNamespace(value=...)` — no
        `__str__` override; `getattr(..., 'value', ...)` covers them.
    """
    return getattr(status, "value", status) if not isinstance(status, str) else status


# ---------- Invoice-number normalization (rule-based duplicate check) ------
#
# One supplier's `INV-001` and `INV-1` are the same payable, and before this
# the always-on rule-based gate compared `lower(trim(...))` — byte-equal after
# case/whitespace — so the two sailed through as distinct invoices and could
# both be paid. The semantic (pgvector) pass was the only backstop, and it is
# off whenever RAG is off, which is the common configuration.
#
# The rule is stated ONCE, here, as a pure function so it can be tested in
# isolation instead of being buried in a query expression.

#: Characters treated as interchangeable separator noise. Each *run* of them
#: collapses to a single "-" — runs are collapsed, never deleted, because
#: deleting them makes "INV-1-2" and "INV-12" collide.
_SEPARATOR_RUN = re.compile(r"[\s\-_./#\\]+")

#: Leading zeros inside a run of digits, keeping at least one digit.
_LEADING_ZEROS = re.compile(r"(?<![0-9])0+(?=[0-9])")

#: Everything that is NOT an ASCII letter — the cheap SQL-computable
#: *superset* invariant used to narrow candidates before normalizing in
#: Python. Two numbers that normalize equal always share this skeleton.
_NON_ASCII_LETTER = re.compile(r"[^a-z]")

#: Hard bound on rows the widened (normalized) pass will load for one
#: invoice. The pass only runs when the exact match already missed, and the
#: candidate query is scoped to the same vendor AND the same letter skeleton,
#: so this cap is a safety valve rather than the usual path — it keeps the
#: check from degrading into an unbounded scan for a very high-volume vendor.
MAX_DUPLICATE_CANDIDATES = 500


def normalize_invoice_number(value: str | None) -> str | None:
    r"""Canonical comparison form of an invoice number. Pure.

    Three steps, deliberately in this order:

    1. **case + trim** — `INV-001 ` == `inv-001` (already the old behaviour).
    2. **separator runs** — every run of whitespace / ``-`` / ``_`` / ``.`` /
       ``/`` / ``#`` / ``\`` collapses to a single ``-``, and leading/trailing
       separators are dropped. So ``INV 001``, ``INV--001`` and ``INV_001``
       all agree. Runs are *collapsed*, not deleted: deleting them would make
       ``INV-1-2`` and ``INV-12`` the same string, which they are not.
    3. **leading zeros inside each digit run** — ``INV-001`` → ``inv-1``,
       ``2026-0007`` → ``2026-7``, ``000`` → ``0``. Only *leading* zeros go;
       ``INV-100`` stays ``inv-100`` and never collides with ``INV-1``.

    Non-digit characters are never stripped, so ``INV-1`` and ``PO-1`` stay
    distinct — which is the point at which "normalize" would turn into
    "guess". Returns ``None`` for a blank/absent number so callers can skip
    the comparison entirely rather than matching empty against empty.
    """
    if not value:
        return None
    collapsed = _SEPARATOR_RUN.sub("-", value.strip().lower()).strip("-")
    if not collapsed:
        return None
    return _LEADING_ZEROS.sub("", collapsed)


def invoice_number_letter_skeleton(value: str | None) -> str:
    """ASCII letters of an invoice number, lowered. Pure.

    A cheap invariant of :func:`normalize_invoice_number`: normalization only
    lowers case, rewrites separators and drops leading zeros, so two numbers
    that normalize equal ALWAYS have the same skeleton. That makes it a safe
    SQL-side prefilter — it can only ever cost us a match (never invent one),
    and the Postgres expression it mirrors is
    ``regexp_replace(lower(invoice_number), '[^a-z]', '', 'g')``.
    """
    return _NON_ASCII_LETTER.sub("", (value or "").lower())


async def refresh_warnings(
    db: AsyncSession, invoice: Invoice, *, org_settings: dict | None = None
) -> list[dict]:
    """Recompute warnings for a single invoice and persist them on the row.

    Also creates exception records for actionable issues.

    `org_settings` drives the configurable fraud rules. When omitted (the
    common case from existing call sites that haven't threaded it
    through), the defaults in `DEFAULT_FRAUD_RULES` are used.

    Every category this function computes is rebuilt from scratch below —
    but `invoice.warnings` can already carry entries this function doesn't
    own (`UPSTREAM_WARNING_TYPES`, appended by `extraction.run_extraction`
    before it calls this as its last step). Seed the list with those instead
    of starting from `[]`, or the final `invoice.warnings = warnings or None`
    assignment silently erases them — self-correction / hallucinated-GL /
    semantic-duplicate flags would never reach the reviewer or an exception
    row.
    """
    warnings: list[dict] = [
        w for w in (invoice.warnings or []) if w.get("type") in UPSTREAM_WARNING_TYPES
    ]
    cfg = _fraud_config(org_settings)

    # Missing required fields
    if not invoice.vendor_name or not invoice.vendor_name.strip():
        warnings.append(warning("missing_vendor_name", "error"))
    if not invoice.invoice_number or not invoice.invoice_number.strip():
        warnings.append(warning("missing_invoice_number", "error"))
    if invoice.amount is None or invoice.amount <= 0:
        warnings.append(warning("missing_amount", "error"))

    # Duplicate detection — another invoice with the same invoice # AND the same
    # vendor. "Same vendor" matches on the STABLE vendor_id (when set) OR the
    # case-insensitive / whitespace-trimmed vendor_name. Keying on vendor_name
    # alone missed a resent invoice whenever the vendor carries two name
    # spellings in the DB (a manual row + an ERP-synced row → different vendor_id
    # but same real supplier); the vendor_id leg closes that. Trimmed + lowered
    # comparison also stops "ACME Corp"/"acme corp" and "INV-001"/"INV-001 " (a
    # trailing space) evading this always-on first gate.
    #
    # The invoice number is compared twice: exactly (unbounded, unchanged), then
    # — only if that missed — through `normalize_invoice_number`, which is what
    # makes "INV-001" and "INV-1" the same payable. See
    # `_has_normalized_duplicate` for the scope and the bound.
    if invoice.vendor_name and invoice.vendor_name.strip() and invoice.invoice_number:
        vendor_match = func.lower(func.trim(Invoice.vendor_name)) == (
            invoice.vendor_name.strip().lower()
        )
        if invoice.vendor_id is not None:
            vendor_match = or_(vendor_match, Invoice.vendor_id == invoice.vendor_id)
        dup_count = await db.execute(
            select(func.count())
            .select_from(Invoice)
            .where(
                func.lower(func.trim(Invoice.invoice_number))
                == invoice.invoice_number.strip().lower(),
                vendor_match,
                Invoice.id != invoice.id,
            )
        )
        is_duplicate = (dup_count.scalar() or 0) > 0
        if not is_duplicate:
            # Widened pass: `INV-001` vs `INV-1`. Only reached when the exact
            # match already missed, so the existing behaviour is untouched and
            # the extra work is off the common path.
            is_duplicate = await _has_normalized_duplicate(db, invoice, vendor_match)
        if is_duplicate:
            warnings.append(warning("duplicate_invoice_number", "warning"))
            await _ensure_exception(
                db,
                invoice,
                "duplicate",
                "warning",
                "Duplicate invoice number for this vendor",
                org_settings=org_settings,
            )

    # Fraud: round amounts (configurable threshold). "Round" = no fractional
    # cents and an even multiple of 100 (…00) — catches the classic fabricated
    # figures (1500, 2500, 7500), not only exact thousands. Anchored at
    # `round_amount_min` so small even amounts don't flood the queue.
    if cfg["round_amount_enabled"] and invoice.amount and invoice.amount > 0:
        threshold = Decimal(str(cfg["round_amount_min"]))
        round_step = Decimal("100")
        if invoice.amount >= threshold and invoice.amount % round_step == 0:
            warnings.append(
                warning(
                    "round_amount",
                    "info",
                    amount=invoice.amount,
                    currency=invoice.currency,
                )
            )
            await _ensure_exception(
                db,
                invoice,
                "fraud_flag",
                "info",
                f"Suspicious round amount: ${invoice.amount}",
                org_settings=org_settings,
            )

    # Fraud: future invoice date
    if cfg["future_date_enabled"] and invoice.invoice_date and invoice.invoice_date > utc_today():
        warnings.append(warning("future_invoice_date", "warning"))
        await _ensure_exception(
            db,
            invoice,
            "fraud_flag",
            "warning",
            "Invoice date is in the future",
            org_settings=org_settings,
        )

    # Fraud: rush payment pattern. Very short window between invoice_date
    # and due_date is a classic social-engineering signal — fraudsters
    # push for "pay today" to skip controls.
    if (
        cfg["rush_payment_enabled"]
        and invoice.invoice_date
        and invoice.due_date
        and (invoice.due_date - invoice.invoice_date).days <= int(cfg["rush_payment_max_days"])
        and (invoice.due_date - invoice.invoice_date).days >= 0
    ):
        days = (invoice.due_date - invoice.invoice_date).days
        flag = warning("rush_payment", "warning", days=days)
        msg = flag["message"]
        warnings.append(flag)
        await _ensure_exception(
            db, invoice, "fraud_flag", "warning", msg, org_settings=org_settings
        )

    # Past-due flag (informational, not fraud — but lives in the same block).
    if (
        invoice.due_date
        and invoice.due_date < utc_today()
        and _status_str(invoice.status) in ("new", "pending", "ready_for_review")
    ):
        warnings.append(warning("past_due", "warning"))

    # Vendor-scoped fraud rules. All require `vendor_id` so we can pull
    # historical context — no point comparing the new invoice to itself.
    if invoice.vendor_id:
        from app.models.vendor import Vendor

        v_result = await db.execute(select(Vendor).where(Vendor.id == invoice.vendor_id))
        vendor = v_result.scalar_one_or_none()

        if vendor is not None:
            # Unverified vendor (existing rule)
            if vendor.status == "unverified":
                warnings.append(warning("unverified_vendor", "warning"))
                await _ensure_exception(
                    db,
                    invoice,
                    "unverified_vendor",
                    "warning",
                    "Invoice linked to an unverified vendor",
                    org_settings=org_settings,
                )

            # Personal-email-domain flag. The vendor's email is set during
            # onboarding/sync; if it falls in a generic domain list, the
            # invoice rides through with extra scrutiny.
            if cfg["personal_email_enabled"] and vendor.email:
                _, _, host = vendor.email.partition("@")
                host = host.lower().strip()
                personal = {d.lower() for d in cfg["personal_email_domains"]}
                if host and host in personal:
                    flag = warning("personal_email_domain", "warning", domain=host)
                    msg = flag["message"]
                    warnings.append(flag)
                    await _ensure_exception(
                        db, invoice, "fraud_flag", "warning", msg, org_settings=org_settings
                    )

            # New-vendor + large-amount. A brand-new vendor making a huge
            # first ask is the canonical phishing pattern.
            if cfg["new_vendor_large_enabled"] and vendor.created_at and invoice.amount:
                vendor_age = datetime.now(vendor.created_at.tzinfo) - vendor.created_at
                large = Decimal(str(cfg["new_vendor_large_amount"]))
                if (
                    vendor_age <= timedelta(days=int(cfg["new_vendor_max_age_days"]))
                    and invoice.amount >= large
                ):
                    flag = warning(
                        "new_vendor_large_amount",
                        "warning",
                        days=vendor_age.days,
                        amount=invoice.amount,
                        currency=invoice.currency,
                    )
                    msg = flag["message"]
                    warnings.append(flag)
                    await _ensure_exception(
                        db, invoice, "fraud_flag", "warning", msg, org_settings=org_settings
                    )

            # Bank-account / remit-to change. We compare the incoming
            # invoice's `remit_to_address` to the most recent approved
            # invoice's value for this vendor — a mid-relationship change
            # is the bank-redirect attack signature.
            if cfg["bank_change_enabled"] and invoice.remit_to_address:
                last_remit_q = await db.execute(
                    select(Invoice.remit_to_address)
                    .where(
                        Invoice.vendor_id == invoice.vendor_id,
                        Invoice.id != invoice.id,
                        Invoice.status.in_(
                            [
                                InvoiceStatus.approved.value,
                                InvoiceStatus.posted_in_erp.value,
                                InvoiceStatus.payment_scheduled.value,
                                InvoiceStatus.paid.value,
                            ]
                        ),
                        Invoice.remit_to_address.isnot(None),
                    )
                    .order_by(Invoice.created_at.desc())
                    .limit(1)
                )
                prior_remit = last_remit_q.scalar_one_or_none()
                if prior_remit and prior_remit.strip() != (invoice.remit_to_address or "").strip():
                    flag = warning("remit_to_changed", "error")
                    msg = flag["message"]
                    warnings.append(flag)
                    await _ensure_exception(
                        db, invoice, "fraud_flag", "error", msg, org_settings=org_settings
                    )

            # Statistical amount anomaly. Pull last N approved invoice
            # amounts; if the new one is more than `sigma` above the
            # mean, flag. Only kicks in once we have enough history —
            # otherwise every second invoice is "anomalous." The history is
            # scoped to the evaluated invoice's own currency — mixing
            # currencies into one mean/stdev would both misfire (a normal
            # foreign-currency invoice reads as a huge outlier) and mask
            # real anomalies (a mixed-currency history inflates stdev).
            if cfg["stat_anomaly_enabled"] and invoice.amount:
                hist_q = await db.execute(
                    select(Invoice.amount)
                    .where(
                        Invoice.vendor_id == invoice.vendor_id,
                        Invoice.id != invoice.id,
                        Invoice.status.in_(
                            [
                                InvoiceStatus.approved.value,
                                InvoiceStatus.posted_in_erp.value,
                                InvoiceStatus.payment_scheduled.value,
                                InvoiceStatus.paid.value,
                            ]
                        ),
                        Invoice.amount.isnot(None),
                        Invoice.currency == (invoice.currency or "USD"),
                    )
                    .order_by(Invoice.created_at.desc())
                    .limit(20)
                )
                amounts = [Decimal(str(a)) for a in hist_q.scalars().all() if a is not None]
                if len(amounts) >= int(cfg["stat_anomaly_min_history"]):
                    mean = sum(amounts) / Decimal(len(amounts))
                    variance = sum((a - mean) ** 2 for a in amounts) / Decimal(len(amounts))
                    stdev = variance.sqrt() if variance > 0 else Decimal(0)
                    threshold_amount = mean + Decimal(str(cfg["stat_anomaly_sigma"])) * stdev
                    if invoice.amount > threshold_amount and stdev > 0:
                        sigma_count = (Decimal(str(invoice.amount)) - mean) / stdev
                        flag = warning(
                            "amount_above_vendor_mean",
                            "warning",
                            amount=invoice.amount,
                            sigma=f"{sigma_count:.1f}",
                            mean=mean.quantize(Decimal("0.01")),
                            currency=invoice.currency,
                        )
                        msg = flag["message"]
                        warnings.append(flag)
                        await _ensure_exception(
                            db, invoice, "fraud_flag", "warning", msg, org_settings=org_settings
                        )

            # LLM anomaly detection (opt-in). Costs an LLM call; gated
            # behind a per-org flag. When the rule fires, the LLM's
            # explanation rides the warning message so the reviewer
            # sees *why* — vital for AI-derived flags to be actionable.
            if cfg["llm_anomaly_enabled"]:
                await _llm_anomaly_check(db, invoice, vendor, warnings, org_settings)

    # Missing data (no amount after extraction)
    has_missing = any(w["type"] == "missing_field" for w in warnings)
    if has_missing and _status_str(invoice.status) not in ("new",):
        await _ensure_exception(
            db,
            invoice,
            "missing_data",
            "error",
            "Required fields missing after extraction",
            org_settings=org_settings,
        )

    # PO matching — runs whenever the invoice has a po_number. The match
    # service handles 2-way (invoice vs PO) and 3-way (with goods receipt).
    # Result is persisted on `invoice.po_match` so the modal can render it
    # without re-running. Mismatches and missing POs raise exceptions for
    # the queue. Skip on draft `new` invoices that haven't been extracted yet.
    if invoice.po_number and _status_str(invoice.status) != "new":
        await _refresh_po_match(db, invoice, warnings, org_settings)
    else:
        invoice.po_match = None

    # Contract compliance — flag spend that falls outside the terms of the
    # invoice's linked contract (over spend-limit, expired/terminated, wrong
    # vendor, off-contract GL). One `contract_noncompliant` exception covers
    # all findings (de-duped by _ensure_exception). Skip un-extracted drafts.
    if invoice.contract_id and _status_str(invoice.status) != "new":
        await _refresh_contract_compliance(db, invoice, warnings, org_settings)

    # Recurring-template variance — for a normally-ingested invoice (NOT one
    # generated by a template) whose vendor has an active recurring template,
    # flag an amount that drifts beyond the template's tolerance. Best-effort:
    # never raise into the warnings pipeline. Skip un-extracted drafts.
    if (
        getattr(invoice, "recurring_template_id", None) is None
        and invoice.vendor_id
        and _status_str(invoice.status) != "new"
    ):
        await _refresh_recurring_variance(db, invoice, warnings)

    # Line-item price variance — flag draft line items whose unit price deviates
    # from this vendor's per-item historical median beyond tolerance, reusing the
    # pure `vendor_enrichment.detect_price_variance` computation. Raises a single
    # de-duped `price_variance` exception covering all flagged lines. Gated by the
    # `price_variance_enabled` fraud rule; skip un-extracted drafts (no lines yet).
    if cfg["price_variance_enabled"] and invoice.vendor_id and _status_str(invoice.status) != "new":
        await _refresh_price_variance(db, invoice, warnings, org_settings)

    # Line-total reconciliation — the summed line items must reconcile with the
    # header money fields under one of the two standard conventions. Runs in
    # EVERY status (unlike the checks above): a manually-entered draft can carry
    # lines from its first save, and the header amount is what a payment run
    # pays, so a header that disagrees with its own lines must never be silent.
    # No-ops when the invoice has no line totals at all.
    if cfg["line_total_mismatch_enabled"]:
        await _refresh_line_total_reconciliation(db, invoice, warnings, org_settings)

    # Reporting-currency conversion — lock the invoice's amount into the org's
    # reporting (base) currency so multi-currency analytics roll up correctly.
    # The rate is snapshotted on the row (see currency_conversion) and never
    # recomputed for an already-locked row, so historical totals stay stable.
    # Best-effort: an FX failure must never block saving an invoice.
    await _refresh_reporting_amount(invoice, org_settings)

    # Persist
    invoice.warnings = warnings or None
    return warnings


async def _refresh_recurring_variance(
    db: AsyncSession, invoice: Invoice, warnings: list[dict]
) -> None:
    """Append a `recurring_variance` warning when an arrived invoice's amount
    drifts beyond the matching active recurring template's tolerance.

    Best-effort: any failure here (no template, query error) is swallowed —
    a recurring-variance check must never break saving an invoice. Imported
    lazily so the warning engine doesn't pull the recurring stack on import."""
    try:
        from app.models.recurring_invoice import STATUS_ACTIVE, RecurringInvoiceTemplate
        from app.services.recurring_invoices import flag_template_variance

        template = (
            await db.execute(
                select(RecurringInvoiceTemplate)
                .where(
                    RecurringInvoiceTemplate.vendor_id == invoice.vendor_id,
                    RecurringInvoiceTemplate.status == STATUS_ACTIVE,
                    RecurringInvoiceTemplate.amount.isnot(None),
                )
                .order_by(RecurringInvoiceTemplate.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if template is None:
            return
        flag = flag_template_variance(invoice, template)
        if flag is not None:
            warnings.append(flag)
    except Exception:  # noqa: BLE001 — best-effort; never break the save path
        logger.warning("recurring-variance check failed for invoice; skipped")


def _price_variance_settings(org_settings: dict | None) -> dict:
    """Resolve the price-variance tolerance knobs from ``settings.enrichment``.

    Reuses the same ``enrichment`` settings block (and the same defaults) as the
    read-only ``/api/enrichment`` suggestions endpoint so the persisted warning
    and the inline advisory agree. Unknown keys are ignored and bad values fall
    back to the constant default — never raises."""
    from app.services.vendor_enrichment import (
        PRICE_ESCALATE_PCT,
        PRICE_MIN_HISTORY,
        PRICE_TOLERANCE_PCT,
    )

    overrides = (org_settings or {}).get("enrichment") or {}
    out = {
        "tolerance_pct": PRICE_TOLERANCE_PCT,
        "escalate_pct": PRICE_ESCALATE_PCT,
        "min_history": PRICE_MIN_HISTORY,
    }
    for src, dst, coerce in (
        ("price_tolerance_pct", "tolerance_pct", lambda v: Decimal(str(v))),
        ("price_escalate_pct", "escalate_pct", lambda v: Decimal(str(v))),
        ("price_min_history", "min_history", int),
    ):
        if src in overrides:
            try:
                out[dst] = coerce(overrides[src])
            except (TypeError, ValueError, ArithmeticError):
                pass
    return out


# ---------------------------------------------------------------------------
# Line-total reconciliation
#
# `Invoice.amount` is the number a payment run pays. `InvoiceLineItem.total` is
# what the reviewer edits in the invoice modal. Nothing used to tie the two
# together, so correcting a line left the header at its old value: the payment
# paid the stale header, PO-match variance had been computed against a total the
# lines no longer supported, and no trace of the divergence existed anywhere.
#
# We deliberately do NOT recompute `amount` from the lines. Line `total`
# semantics are not uniform across the ingest paths — the vision-adapter prompt
# and the mock adapter emit a TAX-INCLUSIVE line total, while `e_invoice.mapper`
# maps the same column onto UBL `LineExtensionAmount`, which is tax-EXCLUSIVE —
# and lines are frequently partial (a reviewer keys in only the disputed line).
# Silently overwriting the header from a sum under either misreading would move
# money with no approval behind it, which is exactly what the post-approval
# financial freeze exists to prevent. Nor do we hard-reject a mismatch: header
# `tax_amount` / `shipping_amount` / `discount_amount` are separate columns, so
# `sum(lines) != amount` is the normal shape of a perfectly valid invoice.
#
# Instead we reconcile explicitly and make any genuine divergence loud: a sum is
# accepted when it matches EITHER standard convention, and anything else raises
# an `error`-severity warning plus a de-duped `line_total_mismatch` exception
# into the queue, so the invoice cannot reach approval without a human seeing
# that its header and its lines disagree.
# ---------------------------------------------------------------------------

# One cent. Line and header money columns are all Numeric(15, 2), so a sum of
# them is exact; this only absorbs rounding in the derived net-of-tax figure.
LINE_TOTAL_TOLERANCE = Decimal("0.01")


def _dec(value) -> Decimal:
    """Coerce a possibly-NULL money column to an exact Decimal. Never float."""
    return Decimal(str(value)) if value is not None else Decimal("0")


def reconcile_line_totals(invoice: Invoice, line_total: Decimal) -> dict | None:
    """Pure: does ``line_total`` reconcile with the invoice's header money?

    Returns ``None`` when it reconciles, else a PII-free payload describing the
    divergence (exact decimal strings, never float).

    A sum reconciles when it matches, within ``LINE_TOTAL_TOLERANCE``, any of:

    * ``amount``  — lines carry tax (the vision adapters' convention);
    * ``subtotal`` — lines are net of tax and the header states the subtotal;
    * ``amount - tax_amount - shipping_amount + discount_amount`` — lines are net
      of tax and the subtotal is absent, so it is derived from the header.
    """
    amount = _dec(invoice.amount)
    candidates: dict[str, Decimal] = {"amount": amount}
    if invoice.subtotal is not None:
        candidates["subtotal"] = _dec(invoice.subtotal)
    candidates["net_of_header_adjustments"] = (
        amount
        - _dec(invoice.tax_amount)
        - _dec(invoice.shipping_amount)
        + _dec(invoice.discount_amount)
    )

    if any(abs(line_total - expected) <= LINE_TOTAL_TOLERANCE for expected in candidates.values()):
        return None

    # Report the divergence against the header amount — the figure a payment run
    # actually pays, and so the one a reviewer needs to see.
    return {
        "line_items_total": str(line_total),
        "header_amount": str(amount),
        "difference": str(line_total - amount),
        "currency": invoice.currency,
    }


async def _refresh_line_total_reconciliation(
    db: AsyncSession,
    invoice: Invoice,
    warnings: list[dict],
    org_settings: dict | None,
) -> None:
    """Append a ``line_total_mismatch`` warning + exception when the summed line
    items don't reconcile with the header money fields.

    Best-effort: any failure here is swallowed (logged, PII-free) — reconciling
    must never break saving an invoice. Mutates ``warnings`` in place.
    """
    try:
        from app.models.invoice import InvoiceLineItem

        # Sum in the DB so a large invoice doesn't materialise every row. NULL
        # totals are excluded by SUM; a set of lines with no totals at all
        # yields NULL and is treated as "nothing to reconcile against".
        line_total = (
            await db.execute(
                select(func.sum(InvoiceLineItem.total)).where(
                    InvoiceLineItem.invoice_id == invoice.id,
                    InvoiceLineItem.total.isnot(None),
                )
            )
        ).scalar_one_or_none()
        if line_total is None:
            return

        mismatch = reconcile_line_totals(invoice, _dec(line_total))
        if mismatch is None:
            return

        flag = warning(
            "line_total_mismatch",
            "error",
            lineItemsTotal=mismatch["line_items_total"],
            headerAmount=mismatch["header_amount"],
            currency=mismatch["currency"],
        )
        msg = flag["message"]
        warnings.append({**flag, **mismatch})
        await _ensure_exception(
            db,
            invoice,
            "line_total_mismatch",
            "error",
            msg,
            org_settings=org_settings,
        )
    except Exception:  # noqa: BLE001 — best-effort; never break the save path
        logger.warning("line-total reconciliation failed for invoice; skipped")


async def _refresh_price_variance(
    db: AsyncSession,
    invoice: Invoice,
    warnings: list[dict],
    org_settings: dict | None,
) -> None:
    """Append ``price_variance`` warnings for draft line items whose unit price
    deviates from this vendor's per-item historical median beyond tolerance, and
    raise one de-duped ``price_variance`` exception covering them.

    Reuses the pure ``vendor_enrichment.detect_price_variance`` math (no
    re-implementation). The baseline is built from this vendor's approved-or-beyond
    invoice line items (same set the ``/api/enrichment`` advisory endpoint uses),
    keyed per ``(item, currency)`` so a multi-currency vendor is never cross-judged.

    Best-effort: any failure here is swallowed (logged, PII-free) — a price-variance
    check must never break saving an invoice. Mutates ``warnings`` in place."""
    try:
        from app.models.invoice import InvoiceLineItem
        from app.services.vendor_enrichment import (
            PRICE_HISTORY_LIMIT,
            detect_price_variance,
        )

        cfg = _price_variance_settings(org_settings)

        # Draft (this invoice's) line items, in line order. The draft's own
        # currency tags every line so it's compared only against same-currency
        # history (matches the enrichment endpoint).
        draft_q = (
            select(
                InvoiceLineItem.item_code,
                InvoiceLineItem.description,
                InvoiceLineItem.unit_price,
            )
            .where(InvoiceLineItem.invoice_id == invoice.id)
            .order_by(
                InvoiceLineItem.line_number.asc().nulls_last(),
                InvoiceLineItem.id.asc(),
            )
        )
        draft_lines = [
            {**dict(r._mapping), "currency": invoice.currency}
            for r in (await db.execute(draft_q)).all()
        ]
        if not draft_lines:
            return

        # This vendor's approved-or-beyond historical line items (+ each line's
        # invoice currency), excluding this invoice. Bounded by PRICE_HISTORY_LIMIT.
        hist_q = (
            select(
                InvoiceLineItem.item_code,
                InvoiceLineItem.description,
                InvoiceLineItem.unit_price,
                Invoice.currency,
            )
            .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
            .where(
                Invoice.vendor_id == invoice.vendor_id,
                Invoice.id != invoice.id,
                Invoice.status.in_(
                    [
                        InvoiceStatus.approved.value,
                        InvoiceStatus.sending_to_erp.value,
                        InvoiceStatus.sent_to_erp.value,
                        InvoiceStatus.posted_in_erp.value,
                        InvoiceStatus.payment_scheduled.value,
                        InvoiceStatus.paid.value,
                        InvoiceStatus.done.value,
                    ]
                ),
                InvoiceLineItem.unit_price.isnot(None),
            )
            .order_by(Invoice.created_at.desc())
            .limit(PRICE_HISTORY_LIMIT)
        )
        history_lines = [dict(r._mapping) for r in (await db.execute(hist_q)).all()]

        flags = detect_price_variance(
            draft_lines,
            history_lines,
            tolerance_pct=cfg["tolerance_pct"],
            escalate_pct=cfg["escalate_pct"],
            min_history=cfg["min_history"],
        )
        if not flags:
            return

        for f in flags:
            label = f.description or f.item_key
            warnings.append(
                warning(
                    # `direction` is a WORD in the sentence, so it selects the
                    # code rather than riding as a param — a client cannot
                    # splice an English "over" into a translated sentence.
                    "price_variance_over" if f.direction == "over" else "price_variance_under",
                    f.severity,
                    deltaPct=f"{f.delta_pct:+.1f}",
                    item=label,
                    unitPrice=f.current_unit_price,
                    baselineUnitPrice=f.baseline_unit_price,
                    currency=invoice.currency,
                )
            )

        # One exception covers all flagged lines; severity escalates if any line
        # cleared the escalate threshold ("warning"), else "info".
        worst = "warning" if any(f.severity == "warning" for f in flags) else "info"
        summary = "; ".join(
            (
                f"{(f.description or f.item_key)}: {f.delta_pct:+.1f}% "
                f"(${f.current_unit_price} vs ${f.baseline_unit_price})"
            )
            for f in flags
        )
        await _ensure_exception(
            db,
            invoice,
            "price_variance",
            worst,
            f"Line-item price variance vs vendor history — {summary}",
            org_settings=org_settings,
        )
    except Exception:  # noqa: BLE001 — best-effort; never break the save path
        logger.warning("price-variance check failed for invoice; skipped")


async def _refresh_reporting_amount(invoice: Invoice, org_settings: dict | None) -> None:
    """Materialize `invoice.reporting_*` for the org's reporting currency.

    Imported lazily so the warning engine doesn't pull the FX stack on every
    import. Swallows FX/adapter errors (logged) — a missing rate leaves the
    reporting fields NULL and the rollup falls back to face value for that one
    row rather than failing the whole save."""
    if invoice.amount is None:
        return
    try:
        from app.services.currency_conversion import (
            materialize_reporting_amount,
            resolve_reporting_currency,
        )
        from app.services.fx_adapters import get_fx_adapter

        reporting_currency = resolve_reporting_currency(org_settings)
        fx_adapter = get_fx_adapter((org_settings or {}).get("fx"))
        await materialize_reporting_amount(
            invoice,
            reporting_currency=reporting_currency,
            fx_adapter=fx_adapter,
        )
    except Exception:  # noqa: BLE001 — best-effort; never break the save path
        logger.warning("reporting-currency materialization failed for invoice; left NULL")


async def _refresh_po_match(
    db: AsyncSession,
    invoice: Invoice,
    warnings: list[dict],
    org_settings: dict | None = None,
) -> None:
    """Run PO matching and append to warnings + exceptions on issues.

    Stores the structured result on `invoice.po_match` for UI rendering.
    Mutates `warnings` in place.
    """
    rule = resolve_match_rule(
        org_settings, vendor_id=invoice.vendor_id, gl_account=invoice.gl_account
    )
    match = await match_invoice_to_po(
        db,
        invoice,
        require_inspection=rule.require_inspection,
        tolerance_pct=rule.tolerance_pct,
    )
    # to_json_dict() renders the MatchResult's exact-Decimal money fields back to
    # numbers for the JSONB column (the default JSON serialiser can't encode
    # Decimal); every variance figure was computed and compared in Decimal.
    invoice.po_match = match.to_json_dict()

    if match.status == "no_po":
        warnings.append(warning("po_not_found", "error", poNumber=invoice.po_number))
        await _ensure_exception(
            db,
            invoice,
            "po_mismatch",
            "error",
            f"Invoice references PO {invoice.po_number} but no matching PO exists",
            org_settings=org_settings,
        )
    elif match.status == "mismatch":
        # Lead with the most useful number — variance %.
        flag = warning(
            "po_amount_variance",
            "warning",
            variancePct=f"{match.amount_variance_pct:+.1f}",
            poNumber=match.po_number,
            invoiceAmount=invoice.amount,
            poTotal=match.po_total,
            currency=invoice.currency,
        )
        msg = flag["message"]
        warnings.append(flag)
        await _ensure_exception(
            db, invoice, "po_mismatch", "warning", msg, org_settings=org_settings
        )
    elif match.status == "partial":
        # Partial 3-way receipt — informational. Reviewer needs to know but
        # it's not an error; goods may be in transit.
        flag = warning(
            "po_partial_receipt",
            "info",
            matchType=match.match_type,
            poNumber=match.po_number,
        )
        msg = flag["message"]
        warnings.append(flag)
        await _ensure_exception(db, invoice, "po_mismatch", "info", msg, org_settings=org_settings)

    # 3-way: an OVER-receipt (more units booked in than were ordered).
    # Independent of the po-status handling above, and for the same reason the
    # inspection block below is: `status` is owned by the AMOUNT control, so an
    # over-receipt can ride alongside a perfectly `matched` invoice — which is
    # exactly the case that used to disappear. The matcher flagged it on
    # `po_match.over_receipt` and rendered it into the invoice modal, but
    # nothing raised it here, so it never reached the exception queue and no
    # clerk was ever asked about it.
    #
    # `warning`, not the `info` a partial receipt gets: a short delivery is
    # routinely benign (goods in transit), whereas quantities nobody ordered
    # cannot be explained by timing — and an over-delivery is how an invoice
    # for unauthorised quantities acquires its supporting receipt.
    #
    # `po_mismatch` is the type: an over-receipt IS an invoice-vs-PO
    # discrepancy, and the roster in `services/exception_lifecycle` is a fixed
    # vocabulary. When the amount leg already opened one, `_ensure_exception`
    # de-dupes per (invoice, type, open) and this is a no-op — the warning
    # still lands on the invoice, which is where a reviewer reads it.
    # The PO reference the over-receipt and quality sentences both name. The
    # matcher's `po_number` when it resolved one, else what the invoice claims.
    po_ref = match.po_number or invoice.po_number or ""

    if match.over_receipt:
        # Read the quantities off the match result rather than scraping the
        # matcher's composed `issues` sentence: a warning built by cutting a
        # substring out of English prose is the shape this catalogue removes.
        if match.received_quantity is not None and match.ordered_quantity is not None:
            flag = warning(
                "po_over_receipt",
                "warning",
                receivedQuantity=format_quantity(match.received_quantity),
                orderedQuantity=format_quantity(match.ordered_quantity),
                excessQuantity=format_quantity(match.received_quantity - match.ordered_quantity),
                poNumber=po_ref,
            )
        else:
            flag = warning("po_over_receipt_unquantified", "warning", poNumber=po_ref)
        msg = flag["message"]
        warnings.append(flag)
        await _ensure_exception(
            db, invoice, "po_mismatch", "warning", msg, org_settings=org_settings
        )

    # 4-way: quality-inspection outcomes route to a `quality_hold` exception.
    # Independent of the po-status handling above — a quality failure can ride
    # alongside an otherwise-matched amount. Maps verdict → severity:
    #   fail               -> error   (block: goods were rejected)
    #   required + missing -> warning (no inspection on record yet)
    #   partial            -> info    (some quantity accepted)
    if match.inspection_result == "fail":
        # The deviation notes come off the inspection row, not out of the
        # matcher's composed issue string — and the PO reference now survives
        # the notes branch, which the substring version dropped.
        if match.inspection_deviation_notes:
            flag = warning(
                "quality_inspection_failed_notes",
                "error",
                poNumber=po_ref,
                notes=match.inspection_deviation_notes,
            )
        else:
            flag = warning("quality_inspection_failed", "error", poNumber=po_ref)
        msg = flag["message"]
        warnings.append(flag)
        await _ensure_exception(
            db, invoice, "quality_hold", "error", msg, org_settings=org_settings
        )
    elif match.inspection_required and match.inspection_result is None:
        flag = warning("quality_inspection_missing", "warning", poNumber=po_ref)
        msg = flag["message"]
        warnings.append(flag)
        await _ensure_exception(
            db, invoice, "quality_hold", "warning", msg, org_settings=org_settings
        )
    elif match.inspection_result == "partial":
        if match.inspection_accepted_quantity is not None:
            flag = warning(
                "quality_partial_acceptance",
                "info",
                acceptedQuantity=f"{match.inspection_accepted_quantity:g}",
                poNumber=po_ref,
            )
        else:
            flag = warning("quality_partial_acceptance_unquantified", "info", poNumber=po_ref)
        msg = flag["message"]
        warnings.append(flag)
        await _ensure_exception(db, invoice, "quality_hold", "info", msg, org_settings=org_settings)


async def _refresh_contract_compliance(
    db: AsyncSession,
    invoice: Invoice,
    warnings: list[dict],
    org_settings: dict | None = None,
) -> None:
    """Append contract-compliance findings to ``warnings`` and raise one
    ``contract_noncompliant`` exception covering them. Mutates ``warnings``."""
    from app.services.contract_compliance import (
        COMPLIANCE_EXCEPTION_TYPE,
        evaluate_contract_compliance,
    )

    findings = await evaluate_contract_compliance(db, invoice)
    if not findings:
        return
    warnings.extend(findings)
    worst = "error" if any(f["severity"] == "error" for f in findings) else "warning"
    await _ensure_exception(
        db,
        invoice,
        COMPLIANCE_EXCEPTION_TYPE,
        worst,
        "; ".join(f["message"] for f in findings),
        org_settings=org_settings,
    )


async def _has_normalized_duplicate(db: AsyncSession, invoice: Invoice, vendor_match) -> bool:
    """Same vendor, same invoice number once normalized (`INV-001` == `INV-1`).

    Runs only after the exact `lower(trim(...))` match has already missed, so
    it can never change an existing verdict — it can only widen.

    **Scope is what keeps this safe.** Normalization widens the match, so the
    candidate query keeps *exactly* the scope the exact check uses: the tenant
    DB the session is already bound to, `vendor_match` (the stable `vendor_id`
    OR the case-folded vendor name) and `id != self`. A different vendor with
    the same number is never a candidate.

    **The bound.** Candidates are additionally narrowed in SQL by the ASCII
    letter skeleton — a superset invariant of the normalized form, so it can
    only cost a match, never invent one — and capped at
    `MAX_DUPLICATE_CANDIDATES`, newest first, selecting one short string per
    row. The comparison itself is the pure `normalize_invoice_number`, applied
    in Python rather than as a query expression so the rule lives in one
    testable place. No unbounded scan, and no expression index (hence no
    migration) is required.
    """
    target = normalize_invoice_number(invoice.invoice_number)
    if target is None:
        return False
    skeleton = invoice_number_letter_skeleton(invoice.invoice_number)
    rows = await db.execute(
        select(Invoice.invoice_number)
        .where(
            vendor_match,
            Invoice.id != invoice.id,
            Invoice.invoice_number.is_not(None),
            func.regexp_replace(func.lower(Invoice.invoice_number), "[^a-z]", "", "g") == skeleton,
        )
        .order_by(Invoice.created_at.desc())
        .limit(MAX_DUPLICATE_CANDIDATES)
    )
    return any(normalize_invoice_number(number) == target for (number,) in rows.all())


async def _ensure_exception(
    db: AsyncSession,
    invoice: Invoice,
    exception_type: str,
    severity: str,
    description: str,
    *,
    org_settings: dict | None = None,
) -> None:
    """Create an exception if one doesn't already exist for this invoice + type."""
    existing = await db.execute(
        select(func.count()).where(
            APException.invoice_id == invoice.id,
            APException.exception_type == exception_type,
            APException.status.in_(["open", "escalated"]),
        )
    )
    if (existing.scalar() or 0) > 0:
        return  # already exists

    # Auto-routing: org settings can map exception_type → user UUID
    # (and a default-SLA-hours per type). Both are optional — when
    # absent we fall back to a sensible default and leave the assignee
    # unset.
    exc_settings = (org_settings or {}).get("exceptions") or {}
    auto_assign = (exc_settings.get("auto_assign_by_type") or {}).get(exception_type)
    sla_map = exc_settings.get("sla_hours_by_type") or {}
    sla_hours = sla_map.get(exception_type, exc_settings.get("default_sla_hours"))

    assigned_to_user_id = None
    if auto_assign:
        try:
            import uuid as _uuid

            assigned_to_user_id = _uuid.UUID(auto_assign)
        except (TypeError, ValueError):
            assigned_to_user_id = None

    due_at = None
    if isinstance(sla_hours, (int, float)) and sla_hours > 0:
        from datetime import UTC as _UTC
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        due_at = _dt.now(_UTC) + _td(hours=float(sla_hours))

    from app.services.exception_service import create_exception

    await create_exception(
        db,
        exception_type=exception_type,
        severity=severity,
        description=description,
        status="open",
        organization_id=invoice.organization_id,
        invoice=invoice,  # exception follows its invoice (P2)
        assigned_to_user_id=assigned_to_user_id,
        due_at=due_at,
        # Every finding here is a DETECTOR's, attributable to the invoice's own
        # contents rather than to whoever most recently saved the row. There is
        # no actor to thread: `refresh_warnings` takes none, and it is reached
        # from fifteen doors including the extraction worker, the QMS sync sweep
        # and two agent resolvers. Naming the caller would bar the AP manager
        # who merely PATCHed a field from clearing a duplicate flag they had no
        # hand in, while leaving the uploader who created the duplicate free —
        # a refusal and an absolution from one wrong guess. The invoice's own
        # `uploaded_by_id` / `segregation_actor_ids` is the axis that binds here.
        raised_by_user_id=None,
    )


async def _llm_anomaly_check(
    db: AsyncSession,
    invoice: Invoice,
    vendor,
    warnings: list[dict],
    org_settings: dict | None,
) -> None:
    """Pull last-N approved invoices for this vendor, run LLM anomaly
    detection, and append a warning + exception when flagged.

    Lives in this file so it can share the SQL session and ensure-
    exception machinery; the actual prompt + LLM I/O is in
    `services.llm_fraud_detection`.
    """
    from app.config import settings as app_settings
    from app.services.llm_fraud_detection import (
        HISTORY_SIZE,
        detect_anomaly,
        invoice_to_candidate,
        invoice_to_history,
    )

    # API key resolution: org BYOK overrides the platform default.
    api_key = ((org_settings or {}).get("extraction") or {}).get(
        "api_key"
    ) or app_settings.anthropic_api_key
    if not api_key:
        return

    # Pull approved history (same set the stat-anomaly rule uses).
    hist_q = await db.execute(
        select(Invoice)
        .where(
            Invoice.vendor_id == vendor.id,
            Invoice.id != invoice.id,
            Invoice.status.in_(
                [
                    InvoiceStatus.approved.value,
                    InvoiceStatus.posted_in_erp.value,
                    InvoiceStatus.payment_scheduled.value,
                    InvoiceStatus.paid.value,
                ]
            ),
        )
        .order_by(Invoice.created_at.desc())
        .limit(HISTORY_SIZE)
    )
    history_invoices = list(hist_q.scalars().all())
    if not history_invoices:
        return

    # Order oldest → newest in the prompt for natural reading.
    history_invoices.reverse()

    candidate = invoice_to_candidate(invoice)
    history = [invoice_to_history(h) for h in history_invoices]
    candidate.vendor_name = invoice.vendor_name or vendor.name or "Unknown vendor"

    result = await detect_anomaly(candidate, history, api_key=api_key)
    if result.is_anomaly and result.reason:
        flag = warning("llm_anomaly", "warning", reason=result.reason)
        msg = flag["message"]
        warnings.append(flag)
        await _ensure_exception(
            db, invoice, "fraud_flag", "warning", msg, org_settings=org_settings
        )
