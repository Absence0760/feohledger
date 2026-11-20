"""Adaptive AI workflows — pure computation over approval history.

This is the **read-model + anomaly-baseline + suggestion-derivation** math for
the "Adaptive AI Workflows" slice. Every function here is sync + pure (no async,
no IO): the caller (``app/api/adaptive_workflows.py``) pulls the rows from the
tenant DB and hands them in already shaped, so the statistics are unit-testable
without a database and — critically — **deterministic** (no LLM, no cloud key;
mirrors the local-first invariant). All amount math is ``Decimal``.

Three concerns, one module:

  1. **Approval-pattern learning** — per-approver and per-vendor aggregates over
     the tenant's approval/rejection history (``compute_approver_patterns`` /
     ``compute_vendor_patterns``).
  2. **Anomaly detection** — build a per-vendor *baseline* from that vendor's
     historically-approved invoices (``compute_vendor_baseline``) and flag a
     single invoice against it on three axes — amount / approver / timing
     (``detect_invoice_anomaly``). Read-only and **explainable**: the baseline
     it compared against is returned alongside the flags.
  3. **Suggestion derivation** — turn the vendor patterns into advisory
     "consider auto-approve under X <reporting currency>" suggestions
     (``derive_suggestions``). The suggestions are inert advisory data —
     nothing here applies anything.

Relationship to existing code (see ``backend/docs/adaptive-workflows.md``):
``invoice_warnings.fraud_stat_anomaly`` already does a *per-invoice,
single-vendor* σ check that **writes** warnings + Exception rows. This module's
anomaly surface is **read-only**, on-demand, covers three axes, and returns the
baseline — it does NOT write warnings or Exceptions. The two are complementary.

The ``_avg`` / ``_quantile`` / ``_decimal_days`` helpers are imported from
``app.services.analytics`` rather than re-implemented; ``_stdev`` is added here
because analytics doesn't expose one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from app.services.analytics import _avg, _decimal_days, _quantile  # noqa: F401
from app.utils.currency_code import iso_currency_or_none

__all__ = [
    "ApproverPattern",
    "VendorApprovalPattern",
    "ApprovalPatterns",
    "VendorBaseline",
    "AnomalyFlag",
    "InvoiceAnomaly",
    "DerivedSuggestion",
    "EligibleApprover",
    "RoutingCandidate",
    "RoutingSuggestion",
    "ThresholdRecommendation",
    "OutcomeStats",
    "ApproverOutcome",
    "EffectivenessMetric",
    "FeedbackSignal",
    "compute_approver_patterns",
    "compute_vendor_patterns",
    "compute_vendor_baseline",
    "detect_invoice_anomaly",
    "derive_suggestions",
    "recommend_auto_approve_threshold",
    "recommend_approvers",
    "is_overturned",
    "compute_outcome_stats",
    "compute_approver_outcomes",
    "outcome_adjusted_threshold",
    "compute_effectiveness",
    "_decimal_days",
]

_CENTS = Decimal("0.01")
_TENTH = Decimal("0.1")


def _q2(x: Decimal) -> Decimal:
    return x.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _q1(x: Decimal) -> Decimal:
    return x.quantize(_TENTH, rounding=ROUND_HALF_UP)


def _stdev(values: list[Decimal]) -> Decimal:
    """Population standard deviation, Decimal. <2 samples → 0."""
    n = len(values)
    if n < 2:
        return Decimal("0")
    mean = sum(values, Decimal("0")) / Decimal(n)
    var = sum(((v - mean) ** 2 for v in values), Decimal("0")) / Decimal(n)
    return var.sqrt()  # Decimal.sqrt — exact-context square root


def _get(row, attr, default=None):
    """Duck-typed access: dict key or attribute."""
    if isinstance(row, dict):
        return row.get(attr, default)
    return getattr(row, attr, default)


# ---------------------------------------------------------------------------
# Approval-pattern dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApproverPattern:
    approver_id: str  # UUID str, or "unknown" when actor_id is NULL
    approver_name: str | None  # joined from control-plane User; may be None
    approved_count: int
    rejected_count: int
    approval_rate_pct: Decimal  # approved / (approved+rejected) * 100, 0.1
    median_time_to_approve_days: Decimal  # over approved-leg samples
    avg_time_to_approve_days: Decimal
    sample_size: int  # approved+rejected (decisions)


@dataclass(frozen=True)
class VendorApprovalPattern:
    vendor_id: str | None  # UUID str, None when invoice had no vendor link
    vendor_name: str
    approved_count: int
    rejected_count: int
    approval_rate_pct: Decimal
    unmodified_count: int  # approved with NO corrections (details.changes absent)
    consistency_pct: Decimal  # unmodified_count / approved_count * 100, 0.1
    avg_approved_amount: Decimal  # Numeric mean, 0.01
    median_approved_amount: Decimal
    min_approved_amount: Decimal
    max_approved_amount: Decimal
    sample_size: int  # approved+rejected
    # Approvals whose amount could NOT be expressed in the org's reporting
    # currency (no locked FX rate on the invoice row, or a lock that no longer
    # describes its currency pair). Their amounts are EXCLUDED from the four
    # money fields above -- mixing currencies into one mean/max is what made a
    # JPY 100,000 vendor read as a 100,000-reporting-currency one -- and a
    # vendor carrying any of them cannot support a threshold raise (fail-closed).
    unconverted_count: int = 0


@dataclass(frozen=True)
class ApprovalPatterns:
    approvers: list[ApproverPattern]
    vendors: list[VendorApprovalPattern]
    generated_at: datetime  # UTC, set by API layer (pass now in)


def _rate_pct(approved: int, rejected: int) -> Decimal:
    total = approved + rejected
    if total == 0:
        return Decimal("0")
    return _q1(Decimal(approved) / Decimal(total) * Decimal("100"))


def compute_approver_patterns(
    decisions: list, *, names: dict[str, str] | None = None
) -> list[ApproverPattern]:
    """Aggregate per-approver approval behaviour from decision rows.

    A decision row is duck-typed (dict or object) with: ``approver_id`` (str |
    None), ``decision`` ("approved" | "rejected"), and ``time_to_approve_days``
    (Decimal | None — None for rejections). ``names`` maps approver id →
    display name.
    """
    names = names or {}
    buckets: dict[str, dict] = {}
    for row in decisions:
        approver = _get(row, "approver_id")
        key = str(approver) if approver else "unknown"
        b = buckets.setdefault(key, {"approved": 0, "rejected": 0, "times": []})
        decision = _get(row, "decision")
        if decision == "approved":
            b["approved"] += 1
            ttd = _get(row, "time_to_approve_days")
            if ttd is not None:
                b["times"].append(ttd)
        elif decision == "rejected":
            b["rejected"] += 1

    out: list[ApproverPattern] = []
    for key, b in buckets.items():
        approved, rejected, times = b["approved"], b["rejected"], b["times"]
        out.append(
            ApproverPattern(
                approver_id=key,
                approver_name=names.get(key),
                approved_count=approved,
                rejected_count=rejected,
                approval_rate_pct=_rate_pct(approved, rejected),
                median_time_to_approve_days=_quantile(times, 0.5) if times else Decimal("0"),
                avg_time_to_approve_days=_avg(times) if times else Decimal("0"),
                sample_size=approved + rejected,
            )
        )
    out.sort(key=lambda p: -p.sample_size)
    return out


def compute_vendor_patterns(decisions: list) -> list[VendorApprovalPattern]:
    """Aggregate per-vendor approval behaviour from decision rows.

    A decision row adds ``vendor_id`` (str | None), ``vendor_name`` (str),
    ``amount`` (Decimal), ``unmodified`` (bool) to the approver-row shape.

    ``amount`` must already be expressed in ONE currency -- the org's reporting
    currency (``api/adaptive_workflows._decision_rows`` converts it at each
    invoice's locked rate). A row the caller could not express there sets
    ``amount_unconverted=True``; its amount is left out of the aggregates
    entirely rather than added at face value, and is counted on
    ``unconverted_count`` so a downstream control can fail closed on it. Rows
    from a caller that predates the flag read as converted -- the correct
    reading for a single-currency tenant.
    """
    buckets: dict[str, dict] = {}
    for row in decisions:
        vendor_id = _get(row, "vendor_id")
        vendor_name = _get(row, "vendor_name") or ""
        key = str(vendor_id) if vendor_id else f"__name__:{vendor_name}"
        b = buckets.setdefault(
            key,
            {
                "vendor_id": str(vendor_id) if vendor_id else None,
                "vendor_name": vendor_name,
                "approved": 0,
                "rejected": 0,
                "unmodified": 0,
                "amounts": [],
                "unconverted": 0,
            },
        )
        decision = _get(row, "decision")
        if decision == "approved":
            b["approved"] += 1
            if _get(row, "unmodified"):
                b["unmodified"] += 1
            amount = _get(row, "amount")
            if _get(row, "amount_unconverted"):
                # Approved, but in a currency we cannot bridge -- count it, never
                # fold it into figures denominated in a different one.
                b["unconverted"] += 1
            elif amount is not None:
                b["amounts"].append(Decimal(str(amount)))
        elif decision == "rejected":
            b["rejected"] += 1

    out: list[VendorApprovalPattern] = []
    for b in buckets.values():
        approved, rejected = b["approved"], b["rejected"]
        unmodified = b["unmodified"]
        amounts = b["amounts"]
        consistency = (
            _q1(Decimal(unmodified) / Decimal(approved) * Decimal("100"))
            if approved
            else Decimal("0")
        )
        if amounts:
            avg_amt = _q2(sum(amounts, Decimal("0")) / Decimal(len(amounts)))
            median_amt = _q2(_quantile(amounts, 0.5))
            min_amt = _q2(min(amounts))
            max_amt = _q2(max(amounts))
        else:
            avg_amt = median_amt = min_amt = max_amt = Decimal("0.00")
        out.append(
            VendorApprovalPattern(
                vendor_id=b["vendor_id"],
                vendor_name=b["vendor_name"],
                approved_count=approved,
                rejected_count=rejected,
                approval_rate_pct=_rate_pct(approved, rejected),
                unmodified_count=unmodified,
                consistency_pct=consistency,
                avg_approved_amount=avg_amt,
                median_approved_amount=median_amt,
                min_approved_amount=min_amt,
                max_approved_amount=max_amt,
                sample_size=approved + rejected,
                unconverted_count=b["unconverted"],
            )
        )
    out.sort(key=lambda p: (-p.sample_size, p.vendor_name))
    return out


# ---------------------------------------------------------------------------
# Anomaly detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VendorBaseline:
    vendor_id: str | None
    vendor_name: str
    sample_size: int  # # approved invoices the AMOUNT statistics were computed from
    mean_amount: Decimal
    median_amount: Decimal
    stdev_amount: Decimal
    min_amount: Decimal
    max_amount: Decimal
    typical_approver_ids: list[str]  # approvers who approved this vendor before
    median_time_to_approve_days: Decimal
    # Approvals excluded from the four money fields because their amount could
    # not be expressed in `currency` (no locked FX rate on the invoice row).
    # They still contribute their approver and their timing — those are
    # currency-independent, and dropping them would make a legitimate approver
    # look unusual.
    unconverted_count: int = 0
    # What the money fields are denominated in: the org's reporting currency.
    currency: str = ""


@dataclass(frozen=True)
class AnomalyFlag:
    code: str  # "amount_high" | "amount_low" | "unusual_approver" | "off_pattern_timing"
    severity: str  # "info" | "warning"
    message: str  # templated, deterministic
    observed: str  # string-Decimal or value observed
    expected: str  # the baseline bound it breached (string)


@dataclass(frozen=True)
class InvoiceAnomaly:
    invoice_id: str
    vendor_id: str | None
    vendor_name: str
    amount: Decimal
    baseline: VendorBaseline | None  # None when insufficient history
    flags: list[AnomalyFlag]
    insufficient_history: bool


def compute_vendor_baseline(
    approved_rows: list,
    *,
    vendor_id: str | None = None,
    vendor_name: str = "",
    min_history: int = 5,
    currency: str = "",
) -> VendorBaseline | None:
    """Build a per-vendor baseline from that vendor's approved invoices.

    ``approved_rows`` are duck-typed rows with ``amount`` (Decimal),
    ``approver_id`` (str | None), ``time_to_approve_days`` (Decimal | None), and
    ``amount_unconverted`` (bool). Returns ``None`` when fewer than
    ``min_history`` rows carry a usable amount — too thin a base to call
    anything anomalous.

    ``amount`` must already be expressed in ONE currency, ``currency`` — the
    org's reporting currency (``api/adaptive_workflows._vendor_approved_rows``
    converts each row at the rate locked on its own invoice). A mean and a
    standard deviation over mixed currencies describe nothing: a handful of JPY
    invoices either make an ordinary USD one read as a wild outlier, or inflate
    the stdev enough to hide a genuine one. This is the same defect already
    fixed for the ``stat_anomaly`` fraud rule.

    A row whose amount could not be expressed there is left out of the money
    statistics and counted on ``unconverted_count``; its approver and timing
    still count, because neither depends on the currency.
    """
    usable = [r for r in approved_rows if not _get(r, "amount_unconverted")]
    if len(usable) < min_history:
        return None
    amounts = [Decimal(str(_get(r, "amount") or 0)) for r in usable]
    times = [t for r in approved_rows if (t := _get(r, "time_to_approve_days")) is not None]
    approvers: list[str] = []
    seen: set[str] = set()
    for r in approved_rows:
        a = _get(r, "approver_id")
        if a and str(a) not in seen:
            seen.add(str(a))
            approvers.append(str(a))
    mean = _q2(sum(amounts, Decimal("0")) / Decimal(len(amounts)))
    return VendorBaseline(
        vendor_id=str(vendor_id) if vendor_id else None,
        vendor_name=vendor_name,
        sample_size=len(usable),
        mean_amount=mean,
        median_amount=_q2(_quantile(amounts, 0.5)),
        stdev_amount=_q2(_stdev(amounts)),
        min_amount=_q2(min(amounts)),
        max_amount=_q2(max(amounts)),
        typical_approver_ids=approvers,
        median_time_to_approve_days=_quantile(times, 0.5) if times else Decimal("0"),
        unconverted_count=len(approved_rows) - len(usable),
        currency=iso_currency_or_none(currency) or "",
    )


def detect_invoice_anomaly(
    invoice,
    baseline: VendorBaseline | None,
    *,
    amount: Decimal | None,
    sigma: Decimal = Decimal("2.0"),
    median_multiple: Decimal = Decimal("3.0"),
    timing_multiple: Decimal = Decimal("3.0"),
    proposed_approver_id: str | None = None,
    time_in_review_days: Decimal | None = None,
    min_history: int = 5,
) -> InvoiceAnomaly:
    """Flag a single invoice against the per-vendor baseline.

    Read-only and explainable: the ``baseline`` it compared against is returned
    on the result. ``invoice`` is duck-typed with ``id``, ``vendor_id``,
    ``vendor_name``. ``min_history`` is accepted for signature symmetry — a
    ``None`` baseline already means "insufficient history".

    ``amount`` is **required and is not read off the invoice**. It is the
    invoice expressed in the SAME currency as ``baseline`` — the org's reporting
    currency — and ``None`` means it could not be expressed there. Reading
    ``invoice.amount`` was the bug: the baseline is built from converted
    history, so comparing a billed figure against it is comparing two different
    currencies, and a caller could fix one side without the other. Making it an
    explicit required argument is what stops that: there is no default to forget.

    When ``amount`` is ``None`` the two AMOUNT rules are skipped and an
    ``amount_comparison_unavailable`` info flag says so — this surface is
    advisory and writes nothing, so the honest answer is "no verdict", never a
    fabricated one. The approver and timing rules still run; neither depends on
    the currency.
    """
    inv_id = str(_get(invoice, "id"))
    vendor_id = _get(invoice, "vendor_id")
    vendor_id = str(vendor_id) if vendor_id else None
    vendor_name = _get(invoice, "vendor_name") or ""
    amount_expressible = amount is not None
    # Fall back to the billed figure for DISPLAY only — never for a comparison.
    amount = (
        Decimal(str(amount)) if amount_expressible else Decimal(str(_get(invoice, "amount") or 0))
    )

    if baseline is None:
        return InvoiceAnomaly(
            invoice_id=inv_id,
            vendor_id=vendor_id,
            vendor_name=vendor_name,
            amount=amount,
            baseline=None,
            flags=[],
            insufficient_history=True,
        )

    flags: list[AnomalyFlag] = []
    mean = baseline.mean_amount
    stdev = baseline.stdev_amount
    median = baseline.median_amount
    high_bound = mean + sigma * stdev
    median_bound = median * median_multiple

    if not amount_expressible:
        # No amount verdict is possible — say so rather than compare a billed
        # figure against a baseline denominated in another currency.
        base_cur = baseline.currency or "the reporting currency"
        flags.append(
            AnomalyFlag(
                code="amount_comparison_unavailable",
                severity="info",
                message=(
                    f"Amount could not be expressed in {base_cur} (no locked FX rate on this "
                    f"invoice), so it was not compared against this vendor's baseline"
                ),
                observed=str(amount),
                expected=str(baseline.mean_amount),
            )
        )

    # 1. amount_high — BOTH guards (σ AND median-multiple) so a tight-variance
    #    vendor doesn't trip on a small absolute jump.
    if amount_expressible and amount > high_bound and amount > median_bound:
        if stdev > 0:
            sigmas = (amount - mean) / stdev
            message = f"Amount {amount} is {sigmas:.1f}σ above this vendor's mean of {mean}"
        else:
            message = (
                f"Amount {amount} is {(amount / median if median else Decimal('0')):.1f}× "
                f"this vendor's median of {median}"
            )
        flags.append(
            AnomalyFlag(
                code="amount_high",
                severity="warning",
                message=message,
                observed=str(amount),
                expected=str(high_bound),
            )
        )

    # 2. amount_low — unusually tiny (possible test txn / split invoice).
    if amount_expressible and stdev > 0 and amount > 0 and amount < mean - sigma * stdev:
        low_bound = mean - sigma * stdev
        flags.append(
            AnomalyFlag(
                code="amount_low",
                severity="info",
                message=(
                    f"Amount {amount} is unusually low for this vendor "
                    f"(mean {mean}, below {low_bound})"
                ),
                observed=str(amount),
                expected=str(low_bound),
            )
        )

    # 3. unusual_approver — only when an approver is supplied.
    if (
        proposed_approver_id is not None
        and baseline.typical_approver_ids
        and proposed_approver_id not in baseline.typical_approver_ids
    ):
        flags.append(
            AnomalyFlag(
                code="unusual_approver",
                severity="info",
                message=("Proposed approver has not previously approved invoices for this vendor"),
                observed=str(proposed_approver_id),
                expected=",".join(baseline.typical_approver_ids),
            )
        )

    # 4. off_pattern_timing — in-flight invoice sitting far longer than usual.
    if (
        time_in_review_days is not None
        and baseline.median_time_to_approve_days > 0
        and time_in_review_days > baseline.median_time_to_approve_days * timing_multiple
    ):
        flags.append(
            AnomalyFlag(
                code="off_pattern_timing",
                severity="info",
                message=(
                    f"In review for {time_in_review_days} days — this vendor "
                    f"typically approves in {baseline.median_time_to_approve_days} days"
                ),
                observed=str(time_in_review_days),
                expected=str(baseline.median_time_to_approve_days * timing_multiple),
            )
        )

    return InvoiceAnomaly(
        invoice_id=inv_id,
        vendor_id=vendor_id,
        vendor_name=vendor_name,
        amount=amount,
        baseline=baseline,
        flags=flags,
        insufficient_history=False,
    )


# ---------------------------------------------------------------------------
# Suggestion derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedSuggestion:
    kind: str  # "auto_approve_threshold" (only kind this slice)
    vendor_id: str | None
    vendor_name: str
    title: str
    rationale: str
    payload: dict  # {"vendor_id", "suggested_threshold": str-Decimal, "based_on_n": int}
    confidence_pct: Decimal  # deterministic: consistency_pct gated by sample size
    dedupe_key: str  # stable identity for upsert: f"auto_approve_threshold:{vendor_id}"


def derive_suggestions(
    vendor_patterns: list[VendorApprovalPattern],
    *,
    min_history: int = 12,
    min_consistency_pct: Decimal = Decimal("95"),
    threshold_round_to: Decimal = Decimal("500"),
    currency: str = "USD",
) -> list[DerivedSuggestion]:
    """Derive advisory auto-approve-threshold suggestions from vendor patterns.

    A vendor qualifies when it has ``>= min_history`` approvals, **zero**
    rejections, and **zero** modifications — every approval was unmodified
    (``unmodified_count == approved_count``). The suggested threshold is the max
    approved amount rounded **up** to ``threshold_round_to``. Advisory only —
    nothing here is applied.

    The "zero modifications" gate is **absolute**, mirroring the absolute
    ``rejected_count == 0`` gate: a percentage tolerance would let a vendor with
    real field corrections still be recommended, while the title/rationale below
    assert a spotless ``{n}/{n} approved unmodified`` record. The copy and the
    gate must agree — an admin reading "spotless" must be reading the truth.
    ``min_consistency_pct`` is retained for forward-compat/org-config symmetry;
    the absolute gate is strictly stronger, so it is applied as a floor only.

    A vendor with ANY approval that could not be expressed in the reporting
    currency (``unconverted_count > 0``) is disqualified: the suggested
    threshold is the max approved amount, and a max computed off a partial
    history would understate the vendor's real ceiling while the copy claims a
    complete record. ``currency`` names what every figure here is denominated
    in — the org's REPORTING currency, which is also what
    ``auto_approve_below`` is measured in.
    """
    cur = iso_currency_or_none(currency) or "USD"
    out: list[DerivedSuggestion] = []
    for vp in vendor_patterns:
        if (
            vp.approved_count >= min_history
            and vp.rejected_count == 0
            and vp.unmodified_count == vp.approved_count
            and vp.consistency_pct >= min_consistency_pct
            and vp.unconverted_count == 0
        ):
            threshold = _q2(
                (vp.max_approved_amount / threshold_round_to).quantize(
                    Decimal("1"), rounding=ROUND_CEILING
                )
                * threshold_round_to
            )
            confidence = min(vp.consistency_pct, Decimal("99"))
            vendor_key = vp.vendor_id if vp.vendor_id else f"name:{vp.vendor_name}"
            title = (
                f"Vendor {vp.vendor_name}: {vp.unmodified_count}/{vp.approved_count} "
                f"invoices approved unmodified (median {vp.median_approved_amount} "
                f"{cur}) — consider auto-approve under {threshold:,.0f} {cur}"
            )
            rationale = (
                f"{vp.approved_count} invoices approved with no corrections and "
                f"0 rejections, amounts {vp.min_approved_amount}–{vp.max_approved_amount} "
                f"(median {vp.median_approved_amount}), all expressed in {cur} — the "
                f"org's reporting currency, which is what auto_approve_below is "
                f"denominated in. Advisory only — review before enabling."
            )
            out.append(
                DerivedSuggestion(
                    kind="auto_approve_threshold",
                    vendor_id=vp.vendor_id,
                    vendor_name=vp.vendor_name,
                    title=title,
                    rationale=rationale,
                    payload={
                        "vendor_id": vp.vendor_id,
                        "suggested_threshold": str(threshold),
                        "threshold_currency": cur,
                        "based_on_n": vp.approved_count,
                    },
                    confidence_pct=confidence,
                    dedupe_key=f"auto_approve_threshold:{vendor_key}",
                )
            )
    return out


# ---------------------------------------------------------------------------
# Org-wide auto-approve threshold recommendation (the "act" surface)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdRecommendation:
    """A conservative, explainable recommendation to RAISE the org's workflow
    ``auto_approve_below`` dollar threshold, derived from the same clean-history
    vendor patterns that back ``derive_suggestions``.

    ``should_raise`` is True only when there is genuine, qualifying evidence AND
    the recommended threshold is strictly higher than the current one. The
    recommendation is **advisory data** — applying it is a separate, explicit,
    audited admin action (it never auto-applies). ``recommended_threshold`` is a
    ``Decimal`` dollar amount; the apply path writes it onto the workflow
    definition's approval step through the audited PATCH path.
    """

    should_raise: bool
    current_threshold: Decimal  # the org's current auto_approve_below (0 = none set)
    recommended_threshold: Decimal  # the proposed new threshold (>= current)
    cap_threshold: Decimal  # the hard ceiling the raise was clamped to
    qualifying_vendor_count: int  # vendors clearing the clean-history gate
    total_clean_invoices: int  # # approvals across qualifying vendors
    evidence: list[dict]  # per-vendor evidence (vendor_name, n, max_amount)
    rationale: str  # deterministic, human-readable explanation
    reason_code: str  # "ok" | "insufficient_evidence" | "no_increase" | "at_cap"
    # What every money figure on this recommendation — and the
    # ``auto_approve_below`` it targets — is denominated in: the org's REPORTING
    # currency. It is not decoration: the evidence is built from invoice amounts
    # converted into it, and the gate that enforces the applied threshold
    # (``extraction.decide_auto_approve``) compares against the same currency.
    currency: str = "USD"


def recommend_auto_approve_threshold(
    vendor_patterns: list[VendorApprovalPattern],
    *,
    current_threshold: Decimal,
    min_history: int = 12,
    min_consistency_pct: Decimal = Decimal("95"),
    min_qualifying_vendors: int = 3,
    max_raise_multiple: Decimal = Decimal("2.0"),
    absolute_cap: Decimal = Decimal("25000"),
    threshold_round_to: Decimal = Decimal("500"),
    currency: str = "USD",
) -> ThresholdRecommendation:
    """Recommend a new org-wide ``auto_approve_below`` threshold — conservatively.

    The evidence base is the **clean-history** vendors (the exact gate
    ``derive_suggestions`` uses: ``>= min_history`` approvals, **zero**
    rejections, **zero** modifications). The recommendation only fires when
    enough independent vendors clear that gate (``>= min_qualifying_vendors``),
    so a single chatty vendor can't move the org-wide limit.

    The candidate threshold is the **maximum clean-approved amount** seen across
    qualifying vendors, rounded **up** to ``threshold_round_to`` — every dollar
    below it would have sailed through with a spotless record. It is then clamped
    by two ceilings (whichever is lower), so accuracy buys a *bounded* raise per
    apply, never a leap:

      * a **relative** cap of ``current_threshold * max_raise_multiple`` (when a
        threshold is already set — the first raise off ``0`` skips the relative
        cap, since 0×anything is 0, and uses the absolute cap only);
      * an **absolute** ceiling ``absolute_cap``.

    Invariants (mirroring the roadmap's "SAFE, auditable, explicit" ask):

      * **Never lowers.** ``recommended_threshold`` is ``max(current, …)``; if the
        evidence supports nothing above the current limit, ``should_raise`` is
        False (``reason_code="no_increase"``).
      * **Capped.** The raise can't exceed the relative/absolute ceiling in one
        step (``reason_code="at_cap"`` when the cap is what's binding the result,
        but a capped raise still ``should_raise``).
      * **Refuses on thin evidence.** Fewer than ``min_qualifying_vendors`` clean
        vendors → ``should_raise=False`` (``reason_code="insufficient_evidence"``).

      * **Refuses to price across currencies.** Every figure here — the
        evidence, the candidate, the cap, and ``auto_approve_below`` itself — is
        denominated in ``currency``, the org's REPORTING currency. A vendor
        carrying any approval that could not be expressed there
        (``unconverted_count > 0``) is excluded from the evidence base outright,
        rather than contributing a max computed off a partial history. Before
        this, the caller fed raw ``Invoice.amount`` in whatever currency each
        invoice was billed in, so three clean JPY 100,000 vendors could push a
        USD threshold toward the ``absolute_cap`` on evidence that supported
        roughly USD 650.

    Pure / deterministic — no LLM, no IO. The caller persists/applies.
    """
    cur = iso_currency_or_none(currency) or "USD"
    current = current_threshold if current_threshold > 0 else Decimal("0")

    qualifying: list[VendorApprovalPattern] = [
        vp
        for vp in vendor_patterns
        if vp.approved_count >= min_history
        and vp.rejected_count == 0
        and vp.unmodified_count == vp.approved_count
        and vp.consistency_pct >= min_consistency_pct
        and vp.unconverted_count == 0
    ]
    qualifying.sort(key=lambda vp: (-vp.max_approved_amount, vp.vendor_name))

    evidence = [
        {
            "vendor_id": vp.vendor_id,
            "vendor_name": vp.vendor_name,
            "based_on_n": vp.approved_count,
            "max_approved_amount": str(vp.max_approved_amount),
            "median_approved_amount": str(vp.median_approved_amount),
        }
        for vp in qualifying
    ]
    total_clean = sum(vp.approved_count for vp in qualifying)

    # The relative ceiling. A 0 current threshold means "no auto-approve yet" —
    # the relative cap (0×mult = 0) would forbid every raise, so for the first
    # raise we fall back to the absolute cap alone.
    relative_cap = current * max_raise_multiple
    cap = absolute_cap if current == 0 else min(relative_cap, absolute_cap)

    if len(qualifying) < min_qualifying_vendors:
        return ThresholdRecommendation(
            should_raise=False,
            current_threshold=_q2(current),
            recommended_threshold=_q2(current),
            cap_threshold=_q2(cap),
            qualifying_vendor_count=len(qualifying),
            total_clean_invoices=total_clean,
            evidence=evidence,
            rationale=(
                f"Only {len(qualifying)} vendor(s) have a clean auto-approvable "
                f"history priced in {cur} (need {min_qualifying_vendors}). Not enough "
                f"independent evidence to raise the org-wide auto-approve threshold."
            ),
            reason_code="insufficient_evidence",
            currency=cur,
        )

    # Highest clean-approved amount across qualifying vendors, rounded UP.
    observed_max = max(vp.max_approved_amount for vp in qualifying)
    candidate = _q2(
        (observed_max / threshold_round_to).quantize(Decimal("1"), rounding=ROUND_CEILING)
        * threshold_round_to
    )

    # Clamp to the cap, then never below current.
    capped = min(candidate, cap)
    recommended = max(current, capped)

    if recommended <= current:
        return ThresholdRecommendation(
            should_raise=False,
            current_threshold=_q2(current),
            recommended_threshold=_q2(current),
            cap_threshold=_q2(cap),
            qualifying_vendor_count=len(qualifying),
            total_clean_invoices=total_clean,
            evidence=evidence,
            rationale=(
                f"{len(qualifying)} vendor(s) with {total_clean} spotless approvals, "
                f"but the supportable threshold ({candidate:,.0f} {cur}, capped at "
                f"{cap:,.0f} {cur}) is not above the current {current:,.0f} {cur}. "
                f"No change."
            ),
            reason_code="no_increase",
            currency=cur,
        )

    at_cap = capped < candidate  # the cap, not the evidence, bound the result
    rationale = (
        f"{len(qualifying)} vendor(s) cleared the clean-history gate "
        f"({total_clean} approvals, 0 rejections, 0 corrections). The highest "
        f"clean-approved amount is {observed_max:,.2f} {cur}; raising auto-approve "
        f"from {current:,.0f} {cur} to {recommended:,.0f} {cur}"
        + (f" (capped at {cap:,.0f} {cur})." if at_cap else ".")
        + f" Every figure is denominated in {cur}, the org's reporting currency —"
        " which is what auto_approve_below is measured in. Affects only NEW"
        " invoices; in-flight invoices keep their frozen workflow snapshot."
    )
    return ThresholdRecommendation(
        should_raise=True,
        current_threshold=_q2(current),
        recommended_threshold=_q2(recommended),
        cap_threshold=_q2(cap),
        qualifying_vendor_count=len(qualifying),
        total_clean_invoices=total_clean,
        evidence=evidence,
        rationale=rationale,
        reason_code="at_cap" if at_cap else "ok",
        currency=cur,
    )


# ---------------------------------------------------------------------------
# Smart routing — advisory approver recommendation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EligibleApprover:
    """An approver eligible to act on the invoice — a control-plane User in the
    org holding an approval-capable role. ``vendor_approved_count`` is how many
    of *this vendor's* invoices this approver has historically approved (vendor
    familiarity); supplied by the caller from the per-vendor decision rows."""

    approver_id: str
    approver_name: str | None
    vendor_approved_count: int = 0


@dataclass(frozen=True)
class RoutingCandidate:
    approver_id: str
    approver_name: str | None
    score: Decimal  # 0-100, deterministic; higher = better routing fit (net of penalty)
    base_score: Decimal  # the forward score BEFORE the outcome down-weight (explainability)
    outcome_penalty: Decimal  # points subtracted for overturned decisions (>= 0)
    rank: int  # 1-based, after sort
    median_time_to_approve_days: Decimal  # over this approver's whole history
    approval_rate_pct: Decimal
    sample_size: int  # decisions this approver has made (any vendor)
    vendor_approved_count: int  # how many of this vendor's invoices they approved
    overturn_rate_pct: Decimal  # share of THIS approver's decisions later overturned, 0.1
    overturned_count: int  # # of this approver's decisions later voided/corrected/rejected
    outcome_sample_size: int  # # of this approver's decisions the overturn rate is over
    reasons: list[str]  # deterministic, human-readable explanation of the score


@dataclass(frozen=True)
class RoutingSuggestion:
    """Advisory routing read model. ``insufficient_history`` is True when NONE of
    the eligible approvers has any decision history to rank on — the caller
    should fall back to its normal assignment policy. Nothing here assigns an
    approver; it is a ranked recommendation only."""

    invoice_id: str | None
    vendor_id: str | None
    vendor_name: str
    amount: Decimal
    candidates: list[RoutingCandidate]
    insufficient_history: bool


# Score weights (sum to 100). Deterministic, documented in
# backend/docs/adaptive-workflows.md § Smart routing.
_W_SPEED = Decimal("45")  # faster median time-to-approve
_W_CONSISTENCY = Decimal("25")  # higher approval rate (fewer rejections/rework)
_W_VENDOR_FAMILIARITY = Decimal("20")  # has approved this vendor before
_W_EXPERIENCE = Decimal("10")  # larger decision sample (more signal)

# Outcome down-weight (the feedback-loop routing penalty). A SUBTRACTIVE term on
# top of the 0..100 forward score, NOT one of the four positive weights — it
# bites only when an approver's decisions are being walked back (see
# backend/docs/adaptive-workflows.md § Smart routing — outcome down-weighting).
#
#   penalty = min(overturn_rate, _OUTCOME_PENALTY_RATE_CAP) / _OUTCOME_PENALTY_RATE_CAP
#             * _W_OUTCOME_PENALTY
#
# so an approver overturned at/above the cap rate loses the FULL penalty, one
# below the cap loses it linearly, and a clean approver loses nothing. Bounded
# (≤ _W_OUTCOME_PENALTY points) and never hard-zeros a candidate — a high
# overturn rate de-prioritises an approver without making them unroutable, and
# below the min-sample the API passes no ApproverOutcome → zero penalty (thin
# evidence never penalises).
_W_OUTCOME_PENALTY = Decimal("30")  # max points an overturned approver can lose
_OUTCOME_PENALTY_RATE_CAP = Decimal("25")  # overturn % at which the full penalty applies


def recommend_approvers(
    eligible: list[EligibleApprover],
    approver_patterns: list[ApproverPattern],
    *,
    outcomes: dict[str, ApproverOutcome] | None = None,
    invoice_id: str | None = None,
    vendor_id: str | None = None,
    vendor_name: str = "",
    amount: Decimal = Decimal("0"),
    speed_horizon_days: Decimal = Decimal("14"),
    experience_full_at: int = 20,
    familiarity_full_at: int = 5,
    top_n: int = 5,
) -> RoutingSuggestion:
    """Rank eligible approvers by routing fit — fastest + most-consistent +
    most-familiar with this vendor, **down-weighted by how often their decisions
    were later overturned** — purely from their existing approval history + the
    realised human outcomes of those decisions.

    Deterministic, no LLM, no IO. The caller resolves ``eligible`` (org approvers
    with an approval-capable role), ``approver_patterns`` (from
    ``compute_approver_patterns`` over the tenant history), and ``outcomes`` (from
    ``compute_approver_outcomes`` over the same window — the per-approver overturn
    signal) and hands them in.

    Scoring (each sub-score normalised to 0..1, weighted, summed → a 0..100 *base*
    score, then a subtractive outcome penalty → the final score):
      * **speed** (``_W_SPEED``) — ``1 - median_time_to_approve / horizon``,
        clamped to [0,1]; an approver with no timing samples scores 0 here.
      * **consistency** (``_W_CONSISTENCY``) — ``approval_rate_pct / 100``.
      * **vendor familiarity** (``_W_VENDOR_FAMILIARITY``) —
        ``min(vendor_approved_count, familiarity_full_at) / familiarity_full_at``.
      * **experience** (``_W_EXPERIENCE``) —
        ``min(sample_size, experience_full_at) / experience_full_at``.
      * **outcome penalty** (``_W_OUTCOME_PENALTY``, SUBTRACTED) — the feedback
        down-weight: ``min(overturn_rate, cap)/cap * _W_OUTCOME_PENALTY`` points
        removed, so an approver whose decisions are frequently voided / corrected
        / rejected ranks below an otherwise-equal clean peer. Banded + bounded
        (≤ ``_W_OUTCOME_PENALTY``, never hard-zeros) and gated on the API side by
        a min-sample (below it, no ``ApproverOutcome`` is passed → **no penalty**;
        thin evidence never penalises). ``base_score`` and ``outcome_penalty`` are
        returned on each candidate for explainability.

    An eligible approver with no decision history at all still appears (so a new
    but valid approver is routable), scoring only on familiarity (also 0 when
    unfamiliar). Ties break on more vendor familiarity, then larger sample, then
    approver_id — stable + deterministic. ``insufficient_history`` is True when
    no eligible approver has any history to rank on.
    """
    pat_by_id = {p.approver_id: p for p in approver_patterns}
    outcomes = outcomes or {}

    horizon = speed_horizon_days if speed_horizon_days > 0 else Decimal("14")
    exp_full = Decimal(experience_full_at) if experience_full_at > 0 else Decimal("1")
    fam_full = Decimal(familiarity_full_at) if familiarity_full_at > 0 else Decimal("1")
    rate_cap = _OUTCOME_PENALTY_RATE_CAP if _OUTCOME_PENALTY_RATE_CAP > 0 else Decimal("25")

    scored: list[RoutingCandidate] = []
    any_history = False
    for e in eligible:
        pat = pat_by_id.get(e.approver_id)
        sample = pat.sample_size if pat else 0
        rate = pat.approval_rate_pct if pat else Decimal("0")
        median_ttd = pat.median_time_to_approve_days if pat else Decimal("0")
        fam = e.vendor_approved_count
        if sample > 0 or fam > 0:
            any_history = True

        # speed: only when the approver actually has approval timing (a 0 median
        # from "no approvals" must NOT read as instant). A genuine same-day
        # approver (has approvals, 0-day median) is the fastest (speed=1).
        has_timing = pat is not None and pat.approved_count > 0
        if has_timing and median_ttd > 0:
            speed = max(Decimal("0"), min(Decimal("1"), Decimal("1") - median_ttd / horizon))
        elif has_timing:
            speed = Decimal("1")
        else:
            speed = Decimal("0")

        consistency = max(Decimal("0"), min(Decimal("1"), rate / Decimal("100")))
        familiarity = min(Decimal(fam), fam_full) / fam_full
        experience = min(Decimal(sample), exp_full) / exp_full

        base_score = _q1(
            _W_SPEED * speed
            + _W_CONSISTENCY * consistency
            + _W_VENDOR_FAMILIARITY * familiarity
            + _W_EXPERIENCE * experience
        )

        # Outcome down-weight. Only when the API passed an ApproverOutcome with
        # enough evidence (insufficient_data → no penalty). Banded by the overturn
        # rate, capped at _W_OUTCOME_PENALTY points; the score floors at 0.
        oc = outcomes.get(e.approver_id)
        if oc is not None and not oc.insufficient_data:
            overturn_rate = oc.overturn_rate_pct
            overturned_n = oc.overturned_count
            outcome_n = oc.decided_count
            capped_rate = min(overturn_rate, rate_cap)
            penalty = _q1(capped_rate / rate_cap * _W_OUTCOME_PENALTY)
        else:
            overturn_rate = oc.overturn_rate_pct if oc is not None else Decimal("0")
            overturned_n = oc.overturned_count if oc is not None else 0
            outcome_n = oc.decided_count if oc is not None else 0
            penalty = Decimal("0")

        score = max(Decimal("0"), _q1(base_score - penalty))

        reasons: list[str] = []
        if pat and pat.approved_count > 0:
            reasons.append(f"median time-to-approve {median_ttd} days")
            reasons.append(f"{rate}% approval rate over {sample} decisions")
        else:
            reasons.append("no approval history yet")
        if fam > 0:
            reasons.append(f"approved {fam} invoice(s) from this vendor")
        if penalty > 0:
            reasons.append(
                f"down-weighted {penalty} pts — {overturned_n}/{outcome_n} decisions "
                f"later overturned ({overturn_rate}%)"
            )

        scored.append(
            RoutingCandidate(
                approver_id=e.approver_id,
                approver_name=e.approver_name,
                score=score,
                base_score=base_score,
                outcome_penalty=penalty,
                rank=0,  # filled after sort
                median_time_to_approve_days=median_ttd,
                approval_rate_pct=rate,
                sample_size=sample,
                vendor_approved_count=fam,
                overturn_rate_pct=overturn_rate,
                overturned_count=overturned_n,
                outcome_sample_size=outcome_n,
                reasons=reasons,
            )
        )

    scored.sort(key=lambda c: (-c.score, -c.vendor_approved_count, -c.sample_size, c.approver_id))
    ranked = [
        RoutingCandidate(
            approver_id=c.approver_id,
            approver_name=c.approver_name,
            score=c.score,
            base_score=c.base_score,
            outcome_penalty=c.outcome_penalty,
            rank=i + 1,
            median_time_to_approve_days=c.median_time_to_approve_days,
            approval_rate_pct=c.approval_rate_pct,
            sample_size=c.sample_size,
            vendor_approved_count=c.vendor_approved_count,
            overturn_rate_pct=c.overturn_rate_pct,
            overturned_count=c.overturned_count,
            outcome_sample_size=c.outcome_sample_size,
            reasons=c.reasons,
        )
        for i, c in enumerate(scored[: max(0, top_n)])
    ]

    return RoutingSuggestion(
        invoice_id=str(invoice_id) if invoice_id else None,
        vendor_id=str(vendor_id) if vendor_id else None,
        vendor_name=vendor_name,
        amount=amount,
        candidates=ranked,
        insufficient_history=not any_history,
    )


# ---------------------------------------------------------------------------
# Feedback loop — fold human OUTCOMES back into the recommendations
# ---------------------------------------------------------------------------
#
# The recommendations above are forward-looking: they read the *approval*
# history and project a routing/threshold suggestion. The feedback loop closes
# the circle by reading what HAPPENED to the invoices that already sailed
# through — the human overturns the system never sees on the way in:
#
#   * a payment that was VOIDED (`invoice.voided_return_to_approved`) — the
#     strongest "this should not have been paid as-is" signal;
#   * an auto-approval that was later CORRECTED or REJECTED.
#
# These outcomes already live in ``audit_log`` (no new instrumentation). The two
# pure functions here turn them into (1) an *outcome-adjusted* threshold that
# pulls BACK when the auto-approved population is being overturned, and (2) an
# honest *effectiveness* metric (post-apply overturn rate + recommendation
# acceptance rate) with an explicit "insufficient data" state — never a
# fabricated figure. Both are deterministic, no-LLM, no-IO; the API layer pulls
# the rows and hands them in already shaped.


def is_overturned(*, voided: bool, corrected: bool, rejected: bool) -> bool:
    """The single source of truth for "was this approval/auto-approval later
    overturned?" — a void, a correction, or a rejection of the same invoice
    counts (each a human walking the decision back). Shared by the *threshold*
    feedback (``compute_outcome_stats``, over auto-approved invoices) and the
    *routing* feedback (``compute_approver_outcomes``, over each approver's own
    decisions) so the two surfaces agree on what "overturned" means and the
    audit-row parsing isn't re-derived twice. Pure / deterministic."""
    return bool(voided) or bool(corrected) or bool(rejected)


@dataclass(frozen=True)
class OutcomeStats:
    """Outcome tallies over a population of *auto-approved* invoices — the cohort
    a raised auto-approve threshold actually creates. ``overturned`` = the union
    of voided / corrected / rejected auto-approvals (each invoice counted once).
    ``overturn_rate_pct`` is the share of the auto-approved population that a
    human later had to walk back. ``insufficient_data`` is True when there are
    too few auto-approvals to read a meaningful rate (the caller then leaves the
    forward recommendation untouched rather than pulling back on noise)."""

    auto_approved_count: int
    voided_count: int
    corrected_count: int
    rejected_count: int
    overturned_count: int  # distinct invoices in {voided ∪ corrected ∪ rejected}
    overturn_rate_pct: Decimal  # overturned / auto_approved * 100, 0.1
    insufficient_data: bool


def compute_outcome_stats(
    outcome_rows: list,
    *,
    min_sample: int = 5,
) -> OutcomeStats:
    """Tally overturns over the auto-approved population.

    Each ``outcome_row`` is one auto-approved invoice, duck-typed with:
      * ``invoice_id`` — identity (de-dupes multiple outcome signals per invoice);
      * ``voided`` (bool) — a payment on it was later voided;
      * ``corrected`` (bool) — an approval carried field corrections
        (``details.changes`` present) — the extraction was overturned;
      * ``rejected`` (bool) — the invoice was later rejected.

    An invoice is **overturned** if any of those is true; it's counted once
    regardless of how many signals fired. Pure / deterministic.
    """
    auto_n = 0
    voided = corrected = rejected = overturned = 0
    seen: set[str] = set()
    for r in outcome_rows:
        inv_id = str(_get(r, "invoice_id"))
        if inv_id in seen:
            continue
        seen.add(inv_id)
        auto_n += 1
        v = bool(_get(r, "voided"))
        c = bool(_get(r, "corrected"))
        j = bool(_get(r, "rejected"))
        voided += 1 if v else 0
        corrected += 1 if c else 0
        rejected += 1 if j else 0
        if is_overturned(voided=v, corrected=c, rejected=j):
            overturned += 1

    rate = _q1(Decimal(overturned) / Decimal(auto_n) * Decimal("100")) if auto_n else Decimal("0")
    return OutcomeStats(
        auto_approved_count=auto_n,
        voided_count=voided,
        corrected_count=corrected,
        rejected_count=rejected,
        overturned_count=overturned,
        overturn_rate_pct=rate,
        insufficient_data=auto_n < min_sample,
    )


@dataclass(frozen=True)
class ApproverOutcome:
    """Per-approver overturn signal — the routing-side mirror of ``OutcomeStats``.

    Over the approver's OWN ``invoice.approved`` decisions in the window:
    ``overturned_count`` is the share whose invoice a human later voided,
    corrected, or rejected (an approval that didn't hold up). ``decided_count``
    is the denominator (distinct decided invoices). ``insufficient_data`` is True
    below the min-sample — too thin to penalise on (no penalty then; an approver
    is never down-weighted on one bad call). Pure data; the penalty itself is
    applied in ``recommend_approvers``."""

    approver_id: str
    decided_count: int
    overturned_count: int
    overturn_rate_pct: Decimal  # overturned / decided * 100, 0.1
    insufficient_data: bool


def compute_approver_outcomes(
    decision_outcome_rows: list,
    *,
    min_sample: int = 5,
) -> dict[str, ApproverOutcome]:
    """Per-approver overturn rate from their decided-invoice outcomes.

    Each row is one of an approver's decided invoices, duck-typed with:
      * ``approver_id`` — the approver who made the decision (rows with no
        approver are skipped — there is no approver to penalise);
      * ``invoice_id`` — identity (de-dupes multiple outcome signals / rows per
        ``(approver, invoice)``);
      * ``voided`` / ``corrected`` / ``rejected`` (bool) — the same overturn
        signals ``compute_outcome_stats`` reads, classified by the shared
        ``is_overturned`` primitive (a correction/rejection here means a *later*
        human walked this approver's decision back — the API layer is
        responsible for only feeding back signals attributable to someone else).

    Returns ``{approver_id: ApproverOutcome}``. Pure / deterministic; mirrors
    ``compute_outcome_stats`` but bucketed per approver. ``insufficient_data`` is
    True when an approver has fewer than ``min_sample`` decided invoices — the
    caller then applies **no** down-weight (thin evidence never penalises)."""
    # Per approver: distinct invoices decided + which of those were overturned.
    decided: dict[str, set[str]] = {}
    overturned: dict[str, set[str]] = {}
    for r in decision_outcome_rows:
        approver = _get(r, "approver_id")
        if not approver:
            continue
        aid = str(approver)
        inv_id = str(_get(r, "invoice_id"))
        decided.setdefault(aid, set()).add(inv_id)
        if is_overturned(
            voided=bool(_get(r, "voided")),
            corrected=bool(_get(r, "corrected")),
            rejected=bool(_get(r, "rejected")),
        ):
            overturned.setdefault(aid, set()).add(inv_id)

    out: dict[str, ApproverOutcome] = {}
    for aid, inv_ids in decided.items():
        n = len(inv_ids)
        ot = len(overturned.get(aid, set()))
        rate = _q1(Decimal(ot) / Decimal(n) * Decimal("100")) if n else Decimal("0")
        out[aid] = ApproverOutcome(
            approver_id=aid,
            decided_count=n,
            overturned_count=ot,
            overturn_rate_pct=rate,
            insufficient_data=n < min_sample,
        )
    return out


def outcome_adjusted_threshold(
    base: ThresholdRecommendation,
    outcomes: OutcomeStats,
    *,
    pullback_overturn_pct: Decimal = Decimal("5.0"),
    freeze_overturn_pct: Decimal = Decimal("15.0"),
) -> ThresholdRecommendation:
    """Fold the auto-approval overturn signal back into the forward threshold
    recommendation, so accuracy *measured from outcomes* — not just clean
    approval history — governs whether the limit rises.

    Three bands, by the measured overturn rate of the auto-approved population:

      * **below ``pullback_overturn_pct``** (default 5%) — the auto-approved
        cohort is holding up; the forward recommendation passes through unchanged.
      * **between the two thresholds** — elevated overturns. The raise is
        *capped at the current threshold's clean candidate* but never withdrawn
        below current: concretely we refuse to RAISE — the recommendation is
        pulled back to ``no_increase`` (the system stops widening auto-approve
        while overturns are climbing), with a rationale that cites the rate.
      * **at/above ``freeze_overturn_pct``** (default 15%) — the auto-approved
        population is being walked back too often to trust *any* raise; identical
        no-raise outcome, stronger rationale (a freeze recommendation).

    When ``outcomes.insufficient_data`` is True (too few auto-approvals to read a
    rate) the base recommendation is returned untouched — we never pull back on
    noise. **Never lowers** the existing threshold (consistent with the forward
    rule); it only declines to raise. Pure / deterministic.
    """
    # Not enough outcome evidence, or the base already isn't raising → nothing to
    # adjust. (A base that's already `no_increase`/`insufficient_evidence` stays
    # as-is; pulling it back further would be meaningless.)
    if outcomes.insufficient_data or not base.should_raise:
        return base

    rate = outcomes.overturn_rate_pct
    if rate < pullback_overturn_pct:
        return base  # outcomes confirm the raise is safe — pass through

    frozen = rate >= freeze_overturn_pct
    reason = "outcome_freeze" if frozen else "outcome_pullback"
    verb = (
        "is being overturned too often to widen auto-approve at all"
        if frozen
        else "has started to climb"
    )
    rationale = (
        f"Outcome feedback: {outcomes.overturned_count} of "
        f"{outcomes.auto_approved_count} auto-approved invoices were later "
        f"voided, corrected, or rejected ({rate}% overturn rate) — the "
        f"auto-approved population {verb}. Holding the auto-approve threshold at "
        f"the current {base.current_threshold:,.0f} {base.currency} instead of "
        f"raising to {base.recommended_threshold:,.0f} {base.currency}; the "
        f"approval history alone supported "
        f"the raise, but the realised outcomes do not. Re-evaluate once the "
        f"overturn rate falls below {pullback_overturn_pct}%."
    )
    return ThresholdRecommendation(
        should_raise=False,
        current_threshold=base.current_threshold,
        recommended_threshold=base.current_threshold,  # never lowers; declines to raise
        cap_threshold=base.cap_threshold,
        qualifying_vendor_count=base.qualifying_vendor_count,
        total_clean_invoices=base.total_clean_invoices,
        evidence=base.evidence,
        rationale=rationale,
        reason_code=reason,
        currency=base.currency,
    )


@dataclass(frozen=True)
class EffectivenessMetric:
    """One measured effectiveness figure with an honest insufficient-data guard.

    ``value_pct`` is meaningful only when ``insufficient_data`` is False;
    otherwise it is ``None`` and ``label`` carries the deterministic "not yet
    measurable" explanation. ``sample_size`` is the denominator the figure (or
    the data shortfall) is computed over."""

    name: str  # machine key, e.g. "auto_approval_overturn_rate"
    value_pct: Decimal | None  # None ⇔ insufficient_data
    sample_size: int
    insufficient_data: bool
    label: str  # deterministic human-readable description / shortfall reason


@dataclass(frozen=True)
class FeedbackSignal:
    """The full feedback-loop read model: the outcome tallies, the two
    effectiveness metrics, and the outcome-adjusted threshold recommendation —
    plus the base (history-only) recommendation it adjusted, so the UI can show
    *why* the loop pulled back (explainability, mirroring how the anomaly surface
    returns the baseline it compared against)."""

    outcomes: OutcomeStats
    metrics: list[EffectivenessMetric]
    base_recommendation: ThresholdRecommendation
    adjusted_recommendation: ThresholdRecommendation


def compute_effectiveness(
    outcomes: OutcomeStats,
    *,
    applied_suggestion_count: int,
    total_suggestion_count: int,
    min_sample: int = 5,
) -> list[EffectivenessMetric]:
    """Compute the effectiveness metrics that replace the old "Not yet measured"
    placeholder — each with an explicit insufficient-data state rather than a
    fabricated number.

      1. **auto_approval_overturn_rate** — of the invoices the system
         auto-approved, the share a human later voided/corrected/rejected. The
         honest accuracy signal for the auto-approve automation; lower is better.
         Insufficient when the auto-approved sample is below ``min_sample``.
      2. **recommendation_acceptance_rate** — of all advisory suggestions
         surfaced, the share an admin actually applied (``status='applied'``).
         Measures whether the recommendations are trusted; insufficient when no
         suggestions have ever been surfaced.

    Pure / deterministic.
    """
    # 1. Auto-approval overturn rate (inverse-accuracy of the automation).
    if outcomes.insufficient_data:
        overturn = EffectivenessMetric(
            name="auto_approval_overturn_rate",
            value_pct=None,
            sample_size=outcomes.auto_approved_count,
            insufficient_data=True,
            label=(
                f"Not yet measurable — only {outcomes.auto_approved_count} "
                f"auto-approved invoice(s) so far (need {min_sample}). The "
                f"overturn rate appears once enough invoices have been "
                f"auto-approved to read a stable figure."
            ),
        )
    else:
        overturn = EffectivenessMetric(
            name="auto_approval_overturn_rate",
            value_pct=outcomes.overturn_rate_pct,
            sample_size=outcomes.auto_approved_count,
            insufficient_data=False,
            label=(
                f"{outcomes.overturned_count} of {outcomes.auto_approved_count} "
                f"auto-approved invoices were later voided, corrected, or "
                f"rejected ({outcomes.overturn_rate_pct}%). Lower is better."
            ),
        )

    # 2. Recommendation acceptance rate (are the suggestions trusted?).
    if total_suggestion_count <= 0:
        acceptance = EffectivenessMetric(
            name="recommendation_acceptance_rate",
            value_pct=None,
            sample_size=0,
            insufficient_data=True,
            label=(
                "Not yet measurable — no workflow suggestions have been surfaced "
                "yet, so there is nothing to have accepted."
            ),
        )
    else:
        applied = max(0, min(applied_suggestion_count, total_suggestion_count))
        pct = _q1(Decimal(applied) / Decimal(total_suggestion_count) * Decimal("100"))
        acceptance = EffectivenessMetric(
            name="recommendation_acceptance_rate",
            value_pct=pct,
            sample_size=total_suggestion_count,
            insufficient_data=False,
            label=(
                f"{applied} of {total_suggestion_count} workflow suggestion(s) "
                f"applied ({pct}%). Higher means the recommendations are trusted."
            ),
        )

    return [overturn, acceptance]
