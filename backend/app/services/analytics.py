"""Analytics — pure computation functions for CFO + operational
metrics. Caller hands in the raw row sets pulled from the DB; the
functions return Decimal / dict results that the API layer
serialises.

Splitting the SQL from the math keeps the math testable without a
DB. Every function in this module is sync + pure (no async, no IO).
The async layer lives in `app/api/analytics.py`.

Metrics shipped:

  Operational (`/api/dashboard`):
    - processing_time_metrics: avg upload→approval, upload→paid,
      median + p95 variants
    - approval_bottleneck: per-approver pending counts, oldest
      pending, average time-in-queue
    - discount_capture_rate: discount-eligible captured vs missed

  CFO (`/api/analytics/cfo`):
    - days_payable_outstanding (DPO) trend
    - cash_conversion_cycle (DSO + DIO − DPO)
    - accruals: open POs + GR − invoices not yet posted
    - working_capital_impact: "if we paid N days later"
    - supplier_concentration: top-10 / top-50 spend share
    - fraud_rate_trend: exceptions per invoice, by type, monthly
    - early_pay_discount_roi: discount $ captured vs missed
    - rebate_yield: virtual-card rebates / total spend
    - forecast_variance: actual vs forecast outflow
    - cashflow_forecast (`bucket_outflows`): projected AP outflows
      bucketed by day / week / month from committed + pending
      invoices
    - cashflow_whatif (`apply_payment_timing_scenario`): early vs
      on-time vs late payment-timing scenarios, with early-pay
      discount capture
    - cash_position (`compute_cash_position` +
      `detect_threshold_breaches`): running balance from a
      bring-your-own opening balance, with threshold-breach alerts

Drill-through: every metric has a corresponding "give me the rows
that produced this number" function; the API layer exposes those
under `/api/analytics/drill/<metric>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.utils.dates import utc_today

# Canonical "open accounts-payable" invoice-status set — an approved liability
# that is still outstanding (not yet paid, not voided/rejected). This is the ONE
# population every payable aggregate must share so they reconcile: the CFO
# `accounts_payable_balance` KPI, the per-entity outstanding rollup, AND the
# aging buckets (dashboard + CSV export + scheduled report) all filter on it, so
# the aging bands always sum to the AP balance. Kept here (pure, IO-free module)
# so the API layer, the dashboard, and the scheduled-report runner import the
# same tuple instead of maintaining drifting copies (the F-4 root cause). Values
# are the `InvoiceStatus` enum's `.value` strings — stated literally to keep this
# module free of an ORM-model import.
OPEN_AP_STATUSES: tuple[str, ...] = (
    "approved",
    "sending_to_erp",
    "sent_to_erp",
    "posted_in_erp",
    "payment_scheduled",
)


@dataclass(frozen=True)
class ProcessingTimeMetrics:
    """Time-to-X distributions in business days.

    `count_*` is the sample size for each leg; a metric with too
    small a sample (< 5) returns Decimal("0") rather than a wild
    value driven by one outlier."""

    avg_upload_to_approval_days: Decimal
    median_upload_to_approval_days: Decimal
    p95_upload_to_approval_days: Decimal
    avg_upload_to_paid_days: Decimal
    median_upload_to_paid_days: Decimal
    p95_upload_to_paid_days: Decimal
    count_approval_leg: int
    count_paid_leg: int


def _decimal_days(td: timedelta) -> Decimal:
    """Total elapsed days as Decimal with one decimal place."""
    return Decimal(str(round(td.total_seconds() / 86400, 1)))


def _quantile(values: list[Decimal], q: float) -> Decimal:
    """Return the q-th percentile (0 ≤ q ≤ 1). Pure-Python so we
    don't have to add numpy as a dependency for a single number."""
    if not values:
        return Decimal("0")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    weight = Decimal(str(pos - lo))
    return (s[lo] * (Decimal("1") - weight) + s[hi] * weight).quantize(Decimal("0.1"))


def _avg(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(Decimal("0.1"))


def compute_processing_time_metrics(
    invoices: list,
    *,
    min_sample: int = 5,
) -> ProcessingTimeMetrics:
    """Inputs are duck-typed invoice rows with `created_at`,
    `approved_at`, and `paid_at` attributes (the latter two
    nullable). Computes avg / median / p95 for both legs.

    Samples smaller than `min_sample` collapse to 0 so a tenant
    with two invoices doesn't see a wildly noisy "avg time to
    pay = 11.7 days" derived from one outlier."""
    approval_days: list[Decimal] = []
    paid_days: list[Decimal] = []
    for inv in invoices:
        created = getattr(inv, "created_at", None)
        if not created:
            continue
        approved = getattr(inv, "approved_at", None)
        if approved is not None:
            approval_days.append(_decimal_days(approved - created))
        paid = getattr(inv, "paid_at", None)
        if paid is not None:
            paid_days.append(_decimal_days(paid - created))

    def _bundle(days: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
        if len(days) < min_sample:
            return Decimal("0"), Decimal("0"), Decimal("0")
        return _avg(days), _quantile(days, 0.5), _quantile(days, 0.95)

    avg_a, med_a, p95_a = _bundle(approval_days)
    avg_p, med_p, p95_p = _bundle(paid_days)
    return ProcessingTimeMetrics(
        avg_upload_to_approval_days=avg_a,
        median_upload_to_approval_days=med_a,
        p95_upload_to_approval_days=p95_a,
        avg_upload_to_paid_days=avg_p,
        median_upload_to_paid_days=med_p,
        p95_upload_to_paid_days=p95_p,
        count_approval_leg=len(approval_days),
        count_paid_leg=len(paid_days),
    )


# ---------------------------------------------------------------------------
# Approval bottleneck
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalBottleneckRow:
    approver_id: str
    approver_name: str | None
    pending_count: int
    oldest_pending_days: Decimal
    avg_pending_days: Decimal


def compute_approval_bottleneck(
    pending_steps: list,
    *,
    now: datetime | None = None,
) -> list[ApprovalBottleneckRow]:
    """Inputs: per-WorkflowStep rows that are `pending` (no
    `completed_at`) and `step_type == "approval"`. Each carries an
    `assigned_to` UUID, an optional `assignee_name` we joined in,
    and a `created_at`.

    Returns one row per approver, sorted descending by pending_count
    so the worst bottleneck is on top. Unassigned steps roll up
    under a synthetic `"unassigned"` key — a non-zero bucket there
    is its own kind of incident (workflow routing broken)."""
    now = now or datetime.now(UTC)
    buckets: dict[str, dict] = {}
    for step in pending_steps:
        approver_id = str(getattr(step, "assigned_to", None) or "unassigned")
        approver_name = getattr(step, "assignee_name", None)
        created = getattr(step, "created_at", None)
        if created is None:
            continue
        age = _decimal_days(now - created)
        b = buckets.setdefault(
            approver_id,
            {
                "approver_name": approver_name,
                "pending_count": 0,
                "ages": [],
            },
        )
        b["pending_count"] += 1
        b["ages"].append(age)

    rows: list[ApprovalBottleneckRow] = []
    for aid, b in buckets.items():
        rows.append(
            ApprovalBottleneckRow(
                approver_id=aid,
                approver_name=b["approver_name"],
                pending_count=b["pending_count"],
                oldest_pending_days=max(b["ages"]),
                avg_pending_days=_avg(b["ages"]),
            )
        )
    return sorted(rows, key=lambda r: (-r.pending_count, -r.oldest_pending_days))


# ---------------------------------------------------------------------------
# Discount capture rate
# ---------------------------------------------------------------------------


def discount_window_open(discount_date, today) -> bool:
    """Is an early-pay discount still capturable as of ``today``?

    The single owner of the elapsed-window rule. A discount window is open
    only while its deadline has NOT passed; a row with no ``discount_date``
    has no window that can be shown to have elapsed, so it is not "missed"
    either — both cases are "undecided", which is what the callers key off.

    Restating ``discount_date >= today`` inline is how the consumers of the
    same economics (``bucket_outflows``' discount-eligible amount, the
    ``early`` what-if scenario, ``discount_optimizer.optimize``,
    ``discount_offers._tier_achievable`` and the dashboard's capture rate)
    drifted apart in the first place — the dashboard was the one that never
    applied the gate and reported every still-open window as already missed.
    """
    return discount_date is not None and discount_date >= today


@dataclass(frozen=True)
class DiscountCaptureMetrics:
    """Net of the early-pay discount opportunity for a period.

    Three buckets, not two. ``captured`` and ``missed`` are the DECIDED
    population — the discount was taken, or its window elapsed without being
    taken. ``pending`` is the rest: an invoice whose discount deadline is
    still in the future (or which carries no deadline at all) has not missed
    anything yet, and counting it as missed made the dashboard report a
    growing pile of forgone savings that were in fact still on the table.

    ``capture_rate_pct`` is ``None`` — never ``0`` — when nothing has been
    decided yet, because "we have not missed a discount yet" and "we captured
    none of the discounts we could have" are opposite facts and 0% reads as
    the bad one (``docs/decisions.md`` §34).

    ``*_reporting`` are the same three figures expressed in the org's
    reporting currency; the bare ones are per-row face values and mix
    currencies the moment one eligible invoice is foreign.
    ``unconverted_count`` is how many eligible rows could not be converted
    and so contribute face value to the reporting figures.
    """

    eligible_count: int
    captured_count: int
    missed_count: int
    pending_count: int
    captured_amount: Decimal
    missed_amount: Decimal
    pending_amount: Decimal
    captured_amount_reporting: Decimal
    missed_amount_reporting: Decimal
    pending_amount_reporting: Decimal
    unconverted_count: int
    capture_rate_pct: Decimal | None
    insufficient_data: bool


def compute_discount_capture(invoice_rows: list) -> DiscountCaptureMetrics:
    """Inputs: invoice rows that have a `discount_amount` plus flags
    `discount_eligible`, `paid_before_discount_date` and
    `discount_window_elapsed`. We don't model the dates directly here — the
    SQL layer computes the flags from `PaymentSchedule.discount_date` /
    `Payment.completed_at` against `discount_window_open`. This keeps the
    math layer pure.

    Optional per-row extras, defaulted so a caller that has only face-value
    figures still works: `discount_amount_reporting` (the same discount
    expressed in the org's reporting currency; falls back to
    `discount_amount`) and `unconverted` (that fallback was taken).
    """
    eligible = 0
    captured = 0
    missed = 0
    pending = 0
    unconverted = 0
    captured_amt = Decimal("0")
    missed_amt = Decimal("0")
    pending_amt = Decimal("0")
    captured_rep = Decimal("0")
    missed_rep = Decimal("0")
    pending_rep = Decimal("0")
    for r in invoice_rows:
        if not getattr(r, "discount_eligible", False):
            continue
        eligible += 1
        amt = Decimal(str(getattr(r, "discount_amount", "0") or "0"))
        rep_raw = getattr(r, "discount_amount_reporting", None)
        rep = Decimal(str(rep_raw)) if rep_raw is not None else amt
        if getattr(r, "unconverted", False):
            unconverted += 1
        if getattr(r, "paid_before_discount_date", False):
            captured += 1
            captured_amt += amt
            captured_rep += rep
        elif getattr(r, "discount_window_elapsed", False):
            # The window closed and we did not take it — the only shape that
            # is genuinely a miss.
            missed += 1
            missed_amt += amt
            missed_rep += rep
        else:
            pending += 1
            pending_amt += amt
            pending_rep += rep
    decided = captured + missed
    rate = (
        (Decimal(captured) / Decimal(decided) * Decimal("100")).quantize(Decimal("0.1"))
        if decided > 0
        else None
    )
    return DiscountCaptureMetrics(
        eligible_count=eligible,
        captured_count=captured,
        missed_count=missed,
        pending_count=pending,
        captured_amount=captured_amt.quantize(Decimal("0.01")),
        missed_amount=missed_amt.quantize(Decimal("0.01")),
        pending_amount=pending_amt.quantize(Decimal("0.01")),
        captured_amount_reporting=captured_rep.quantize(Decimal("0.01")),
        missed_amount_reporting=missed_rep.quantize(Decimal("0.01")),
        pending_amount_reporting=pending_rep.quantize(Decimal("0.01")),
        unconverted_count=unconverted,
        capture_rate_pct=rate,
        insufficient_data=decided == 0,
    )


#: The three buckets ``compute_discount_capture`` sorts an eligible invoice
#: into. Exhaustive and mutually exclusive: captured (paid inside the discount
#: window), missed (the window closed unused), pending (still capturable — no
#: deadline, or one that has not passed). Named here because the SQL ``CASE``
#: in ``api/dashboard`` emits these exact strings and
#: ``discount_capture_from_grouped_rows`` reads them back.
DISCOUNT_CAPTURE_BUCKETS = ("captured", "missed", "pending")


def discount_capture_from_grouped_rows(groups: list[dict]) -> DiscountCaptureMetrics:
    """Same result as `compute_discount_capture`, from rows the DB has already
    classified and aggregated — so the caller doesn't stream every
    discount-scheduled invoice into Python to bucket it a row at a time.

    Each group dict is one bucket's rollup::

        {"bucket": "captured"|"missed"|"pending", "count": int,
         "amount": Decimal, "reporting_amount": Decimal,
         "unconverted_count": int}

    The three per-row facts the row-at-a-time fold derives — the discount
    amount, the same amount in the reporting currency, and whether that
    conversion fell back to face value — are computed by the query, and the
    bucket is the SQL statement of the same two comparisons
    (`paid_at <= discount_date`; `discount_date < today`). What stays here is
    everything that is NOT per-row: the three-bucket vocabulary, the
    `captured / (captured + missed)` rate, and the `insufficient_data` state
    that keeps an undecided population from reporting 0 %.

    Money arrives already rounded to cents per row and summed by Postgres, so
    quantizing here is a no-op that keeps the return type honest — the same
    argument `currency_conversion.rollup_from_grouped_rows` makes.

    An unrecognised bucket raises rather than being dropped: the query's CASE
    is exhaustive, so a key outside the vocabulary means the caller and this
    function have drifted, and silently discarding it would understate every
    figure in the tile.
    """
    totals: dict[str, dict] = {
        b: {"count": 0, "amount": Decimal("0"), "reporting_amount": Decimal("0")}
        for b in DISCOUNT_CAPTURE_BUCKETS
    }
    unconverted = 0
    for g in groups:
        bucket = g["bucket"]
        if bucket not in totals:
            raise ValueError(
                f"unknown discount-capture bucket {bucket!r}; "
                f"expected one of {DISCOUNT_CAPTURE_BUCKETS}"
            )
        totals[bucket]["count"] += int(g["count"] or 0)
        totals[bucket]["amount"] += Decimal(str(g["amount"] or 0))
        totals[bucket]["reporting_amount"] += Decimal(str(g["reporting_amount"] or 0))
        unconverted += int(g["unconverted_count"] or 0)

    captured, missed, pending = (totals[b] for b in DISCOUNT_CAPTURE_BUCKETS)
    decided = captured["count"] + missed["count"]
    rate = (
        (Decimal(captured["count"]) / Decimal(decided) * Decimal("100")).quantize(Decimal("0.1"))
        if decided > 0
        else None
    )
    return DiscountCaptureMetrics(
        eligible_count=captured["count"] + missed["count"] + pending["count"],
        captured_count=captured["count"],
        missed_count=missed["count"],
        pending_count=pending["count"],
        captured_amount=captured["amount"].quantize(Decimal("0.01")),
        missed_amount=missed["amount"].quantize(Decimal("0.01")),
        pending_amount=pending["amount"].quantize(Decimal("0.01")),
        captured_amount_reporting=captured["reporting_amount"].quantize(Decimal("0.01")),
        missed_amount_reporting=missed["reporting_amount"].quantize(Decimal("0.01")),
        pending_amount_reporting=pending["reporting_amount"].quantize(Decimal("0.01")),
        unconverted_count=unconverted,
        capture_rate_pct=rate,
        insufficient_data=decided == 0,
    )


# ---------------------------------------------------------------------------
# Touchless (straight-through-processing) rate
# ---------------------------------------------------------------------------
#
# DEFINITION (decided, not inherited — see `docs/analytics.md`):
#   "passed review without human touch".
#
# The population is every invoice that PROVABLY reached and finished the
# review stage. The numerator is the ones that cleared it without a human
# bouncing them back; the denominator adds the ones a human rejected. An
# invoice that never entered review is in NEITHER leg — it is evidence
# neither for nor against touchless processing.
#
# The rate is a claim about how much work the machine did instead of a
# person, so the numerator requires POSITIVE EVIDENCE that review happened.
# A terminal status is not that evidence: `new -> done` is a legal
# `VALID_TRANSITIONS` edge that skips approval outright, and the Day-0 CSV
# importer (`services/csv_import`) plants historical rows straight at `done`
# (its default) or `paid` without the workflow engine running at all.
# Counting those as "cleared review" inflates exactly the figure leadership
# reads.

# Statuses an invoice can ONLY be in because review finished and it cleared:
# every `VALID_TRANSITIONS` edge into them originates at `approved`, and every
# writer of `approved` stamps `Invoice.approval_date`. Status alone is proof
# here, so no extra evidence is needed.
TOUCHLESS_CLEARED_STATUSES: tuple[str, ...] = (
    "approved",
    "sending_to_erp",
    "sent_to_erp",
    "posted_in_erp",
    "payment_scheduled",
)

# Statuses reachable EITHER through review or around it — status alone proves
# nothing, so these count as cleared only with the approval stamp:
#   `done`   — `new -> done` skips approval; CSV import's default landing state.
#   `paid`   — normally only from `payment_scheduled`, but CSV-importable too.
#   `failed` — from `pending` (extraction failed, never reviewed) OR from
#              `sending_to_erp` (approved, then the ERP export blew up).
# The caller supplies how many of these carry the durable `Invoice.approval_date`
# stamp (written on every approval path, never cleared); the rest are out of the
# population entirely.
TOUCHLESS_REVIEW_EVIDENCE_STATUSES: tuple[str, ...] = ("done", "paid", "failed")

# Review finished and the invoice did NOT clear — denominator only. There is
# no approval stamp on a rejection (nothing writes one), so this leg cannot be
# evidence-gated the way the cleared leg is; a rejected row IS the evidence
# that a human touched it.
#
# That asymmetry is why provenance, not evidence, is what removes an IMPORTED
# rejection: `services/csv_import` can plant a historical `rejected` row, and
# gating this leg on an approval stamp would zero the bounced population
# outright (no rejection ever carries one) rather than exclude the imports.
TOUCHLESS_BOUNCED_STATUSES: tuple[str, ...] = ("rejected",)

# ---------------------------------------------------------------------------
# Imported rows are outside the metric — BOTH legs
# ---------------------------------------------------------------------------
#
# A CSV-imported invoice is history migrated in from the tenant's previous
# system; the workflow engine never ran on it. It is therefore evidence
# neither for nor against this platform's automation, in either direction:
#
#   * an imported `done` / `paid` row would inflate the numerator (already
#     handled, since it carries no approval stamp), and
#   * an imported `rejected` row DEFLATES the rate — it lands in the bounced
#     leg as though a reviewer here had sent it back.
#
# Provenance is what settles this, not status: `done`, `paid` and `rejected`
# are each reachable BOTH natively and by import, so no status set can tell
# them apart. `services/csv_import` stamps every row it creates with the
# `meta["imported"]` marker (`imported_invoice_clause` is its SQL predicate);
# the caller counts marked rows per status and passes them in.
#
# An UNMARKED row is treated as native, deliberately: the marker is written
# only going forward and is never backfilled, so absence means "we do not
# know". Rows imported before it shipped stay in the population — stated in
# `docs/analytics.md` rather than papered over with an invented provenance.


def _native_count(pipeline: dict, imported_pipeline: dict, statuses: tuple[str, ...]) -> int:
    """Rows in `statuses` that carry no import marker.

    Subtracted per status and floored at zero, so a stale or mismatched
    `imported_pipeline` can neither drive a leg negative nor let a surplus in
    one status cancel real rows in another.
    """
    total = 0
    for status in statuses:
        present = int(pipeline.get(status, 0) or 0)
        imported = int(imported_pipeline.get(status, 0) or 0)
        total += max(0, present - imported)
    return total


def compute_touchless_rate(
    pipeline: dict,
    *,
    review_cleared_count: int,
    imported_pipeline: dict,
) -> float:
    """Straight-through-processing share, in [0, 100].

    `pipeline` is the dashboard's `{status: count}` map over EVERY invoice.

    `imported_pipeline` is the same `{status: count}` shape restricted to rows
    that provably came from an import (`csv_import.imported_invoice_clause`).
    Those rows are subtracted from BOTH legs: the workflow engine never ran on
    them, so they are evidence neither for nor against this platform's
    automation. It is REQUIRED for the same reason `review_cleared_count` is —
    a caller that has not been updated must fail loudly rather than quietly
    re-publish the old, wider population.

    `review_cleared_count` is the number of NATIVE invoices sitting in a
    `TOUCHLESS_REVIEW_EVIDENCE_STATUSES` status that carry a durable
    `Invoice.approval_date` stamp — the positive evidence that the invoice
    actually reached and cleared the approval step rather than arriving at a
    terminal status around it. The caller excludes imported rows from this
    count at the query, since an imported row's stamp (if a future importer
    ever wrote one) would not be evidence of review HERE.

    The numerator is a strict subset of the denominator, so the rate can
    never go negative (BUG 9).
    """
    imported_pipeline = imported_pipeline or {}
    cleared = _native_count(pipeline, imported_pipeline, TOUCHLESS_CLEARED_STATUSES)
    bounced = _native_count(pipeline, imported_pipeline, TOUCHLESS_BOUNCED_STATUSES)
    # Never claim more evidenced-cleared invoices than there are NATIVE
    # invoices in those statuses (all three figures come from the same
    # snapshot, so this is belt-and-braces against an unrelated count).
    ambiguous_total = _native_count(pipeline, imported_pipeline, TOUCHLESS_REVIEW_EVIDENCE_STATUSES)
    cleared += max(0, min(int(review_cleared_count or 0), ambiguous_total))
    reviewed_total = cleared + bounced
    if reviewed_total <= 0:
        return 0.0
    return round(cleared / reviewed_total * 100, 1)


# ---------------------------------------------------------------------------
# Days Payable Outstanding (DPO)
# ---------------------------------------------------------------------------


def compute_dpo(
    *,
    accounts_payable: Decimal,
    cogs: Decimal,
    period_days: int = 365,
) -> Decimal:
    """Classic DPO = (AP / COGS) × period_days.

    Returns Decimal with one decimal. Zero COGS → 0 (avoids
    divide-by-zero) and warrants an UI annotation that the metric
    isn't computable for this period."""
    if cogs <= 0:
        return Decimal("0")
    return (accounts_payable / cogs * Decimal(period_days)).quantize(Decimal("0.1"))


def compute_dpo_trend(
    monthly_snapshots: list[dict],
    *,
    period_days: int = 30,
) -> list[dict]:
    """`monthly_snapshots` is a list of `{"month": "YYYY-MM",
    "accounts_payable": Decimal, "cogs": Decimal}`. Returns the
    same list with `dpo` appended per row."""
    out: list[dict] = []
    for s in monthly_snapshots:
        ap = Decimal(str(s.get("accounts_payable", "0") or "0"))
        cogs = Decimal(str(s.get("cogs", "0") or "0"))
        out.append(
            {
                **s,
                "dpo": compute_dpo(accounts_payable=ap, cogs=cogs, period_days=period_days),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Cash conversion cycle (DSO + DIO − DPO)
# ---------------------------------------------------------------------------


def compute_cash_conversion_cycle(
    *,
    dso_days: Decimal | None,
    dio_days: Decimal | None,
    dpo_days: Decimal,
) -> Decimal | None:
    """Returns the cycle in days, or None when DSO/DIO aren't
    available (the cycle is only meaningful when all three legs
    are known — AP-only tenants can't compute it)."""
    if dso_days is None or dio_days is None:
        return None
    return (dso_days + dio_days - dpo_days).quantize(Decimal("0.1"))


# ---------------------------------------------------------------------------
# Accruals view
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReceivedPO:
    """One PO's goods-receipt position, for valuing received goods.

    `po_total`: the PO's monetary total.
    `po_qty_total`: sum of ordered line quantities (0 when the PO has
                    no quantified line items).
    `gr_qty_total`: sum of received quantities across every goods
                    receipt booked against the PO.
    """

    po_total: Decimal
    po_qty_total: Decimal
    gr_qty_total: Decimal


def value_received_goods(receipts: list[ReceivedPO]) -> Decimal:
    """Value goods physically received against their POs (the GR/IR
    accrual leg — goods received, not yet invoiced).

    For each PO carrying a goods receipt, value the *received fraction*
    of the PO total: ``po_total × min(1, gr_qty / po_qty)``. This reuses
    the exact received-fraction `po_matching.match_invoice_to_po`
    computes for the 3-way match, fanned out across every receipted PO.

    A PO with no quantified line items (`po_qty_total == 0`) but a booked
    receipt is treated as fully received (fraction 1.0) — the only signal
    we have is that goods arrived. The fraction is capped at 1.0 so an
    over-receipt never inflates the accrual above the PO's value.
    """
    total = Decimal("0")
    for r in receipts:
        if r.po_qty_total > 0:
            fraction = min(Decimal("1"), r.gr_qty_total / r.po_qty_total)
        else:
            fraction = Decimal("1")
        total += r.po_total * fraction
    # ROUND_HALF_UP to match the rest of this module's money rounding.
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class AccrualsSnapshot:
    """Open commitments awaiting invoice + posting.

    `open_po_amount`: total of non-closed POs (open purchase
                       commitments).
    `received_amount`: value of goods physically received against
                       their POs — the received fraction of each
                       receipted PO's total (GR/IR accrual). See
                       `value_received_goods`. Receipts on a PO we
                       can't price (no PO link) are excluded.
    `unposted_invoice_amount`: invoices in approved/sent_to_erp
                       states — accrued liability not yet on the
                       ledger.
    `total_accrual`: open_po_amount + received_amount −
                       unposted_invoice_amount. The invoiced side is
                       netted here at the aggregate level, not per-PO.
    """

    open_po_amount: Decimal
    received_amount: Decimal
    unposted_invoice_amount: Decimal
    total_accrual: Decimal


def compute_accruals(
    *,
    open_po_amount: Decimal,
    received_amount: Decimal,
    unposted_invoice_amount: Decimal,
) -> AccrualsSnapshot:
    total = (open_po_amount + received_amount - unposted_invoice_amount).quantize(Decimal("0.01"))
    return AccrualsSnapshot(
        open_po_amount=open_po_amount.quantize(Decimal("0.01")),
        received_amount=received_amount.quantize(Decimal("0.01")),
        unposted_invoice_amount=unposted_invoice_amount.quantize(Decimal("0.01")),
        total_accrual=total,
    )


@dataclass(frozen=True)
class CurrencyAccruals:
    """One currency's slice of the accruals — every leg in ``currency``.

    ``currency`` is ``None`` for figures whose currency nobody recorded: POs
    created before ``purchase_orders.currency`` existed with no requisition
    behind them, or synced from an ERP that stated none (decisions §197). That
    slice is kept apart and rendered bare, never folded into a real currency's.
    """

    currency: str | None
    open_po_amount: Decimal
    received_amount: Decimal
    unposted_invoice_amount: Decimal
    total_accrual: Decimal


def compute_accruals_by_currency(
    *,
    open_po_amounts: dict[str | None, Decimal],
    received_amounts: dict[str | None, Decimal],
    unposted_invoice_amounts: dict[str | None, Decimal],
) -> list[CurrencyAccruals]:
    """The accruals, one row per currency, each netted WITHIN its currency.

    `compute_accruals` over the whole book adds a EUR purchase order to a USD
    one and subtracts a GBP invoice from both — a figure denominated in
    nothing. The legs never convert (a PO carries no locked rate to bridge it),
    so this groups instead: every currency that appears in any leg gets a row,
    with zero for the legs it is absent from, and `total_accrual` is that
    currency's own `open + received − unposted` through `compute_accruals`.

    Ordered by code, the unknown-currency row last — a deterministic order that
    does not reshuffle as the amounts move.
    """
    codes = set(open_po_amounts) | set(received_amounts) | set(unposted_invoice_amounts)
    rows: list[CurrencyAccruals] = []
    for code in sorted(codes, key=lambda c: (c is None, c or "")):
        snapshot = compute_accruals(
            open_po_amount=open_po_amounts.get(code, Decimal("0")),
            received_amount=received_amounts.get(code, Decimal("0")),
            unposted_invoice_amount=unposted_invoice_amounts.get(code, Decimal("0")),
        )
        rows.append(
            CurrencyAccruals(
                currency=code,
                open_po_amount=snapshot.open_po_amount,
                received_amount=snapshot.received_amount,
                unposted_invoice_amount=snapshot.unposted_invoice_amount,
                total_accrual=snapshot.total_accrual,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Working-capital impact: "if we paid N days later, how much cash unlocked?"
# ---------------------------------------------------------------------------


def compute_working_capital_impact(
    *,
    avg_daily_outflow: Decimal,
    days_extended: int,
) -> Decimal:
    """The crude back-of-envelope CFOs ask: extending payment
    terms by N days unlocks `avg_daily_outflow * N` of working
    capital. Real model has terms-by-vendor + late-fee
    considerations; we leave those to the next iteration.
    Returns absolute dollars rounded to whole units."""
    if days_extended <= 0 or avg_daily_outflow <= 0:
        return Decimal("0")
    return (avg_daily_outflow * Decimal(days_extended)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )


# ---------------------------------------------------------------------------
# Supplier concentration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupplierConcentration:
    """How much spend is concentrated in the top N vendors.

    `flagged` is True iff the largest vendor's share REACHES OR
    exceeds the threshold (default 25%) — the canonical
    concentration-risk warning sign. The boundary is inclusive on
    purpose: a risk flag that stays dark at exactly the limit an
    org set is the wrong direction to be wrong in."""

    total_spend: Decimal
    top_10_share_pct: Decimal
    top_50_share_pct: Decimal
    largest_vendor: str | None
    largest_vendor_share_pct: Decimal
    flagged: bool


def compute_supplier_concentration(
    vendor_spend: list[dict],
    *,
    concentration_threshold_pct: Decimal = Decimal("25"),
) -> SupplierConcentration:
    """`vendor_spend` is a list of `{"vendor": str, "amount":
    Decimal}` sorted descending by amount.

    Pass the **whole** vendor set, never a pre-sliced top-N. `total` is derived
    from the list handed in and the top-10 / top-50 cuts are taken here, so a
    caller that slices first turns `total_spend` into that slice's subtotal,
    inflates every share against it, and makes `top_50_share_pct` exactly
    `100.0` by construction. Slice for display *after* this returns.

    Empty input returns a zeroed-out snapshot rather than raising
    — the dashboard renders "no data" rather than 500ing."""
    if not vendor_spend:
        return SupplierConcentration(
            total_spend=Decimal("0"),
            top_10_share_pct=Decimal("0"),
            top_50_share_pct=Decimal("0"),
            largest_vendor=None,
            largest_vendor_share_pct=Decimal("0"),
            flagged=False,
        )
    total = sum(
        (Decimal(str(r.get("amount", "0") or "0")) for r in vendor_spend),
        Decimal("0"),
    )
    if total <= 0:
        return SupplierConcentration(
            total_spend=Decimal("0"),
            top_10_share_pct=Decimal("0"),
            top_50_share_pct=Decimal("0"),
            largest_vendor=None,
            largest_vendor_share_pct=Decimal("0"),
            flagged=False,
        )

    def _share(top_n: list[dict]) -> Decimal:
        s = sum(
            (Decimal(str(r.get("amount", "0") or "0")) for r in top_n),
            Decimal("0"),
        )
        return (s / total * Decimal("100")).quantize(Decimal("0.1"))

    top_10 = _share(vendor_spend[:10])
    top_50 = _share(vendor_spend[:50])
    largest = vendor_spend[0]
    largest_share = (
        Decimal(str(largest.get("amount", "0") or "0")) / total * Decimal("100")
    ).quantize(Decimal("0.1"))
    return SupplierConcentration(
        total_spend=total.quantize(Decimal("0.01")),
        top_10_share_pct=top_10,
        top_50_share_pct=top_50,
        largest_vendor=str(largest.get("vendor") or ""),
        largest_vendor_share_pct=largest_share,
        flagged=largest_share >= concentration_threshold_pct,
    )


# ---------------------------------------------------------------------------
# Fraud-rate trend
# ---------------------------------------------------------------------------


def compute_fraud_rate_trend(
    monthly_buckets: list[dict],
) -> list[dict]:
    """`monthly_buckets`: list of `{"month": "YYYY-MM",
    "invoice_count": int, "exception_count": int,
    "by_type": {type: count}}`. Returns the same shape with
    `rate_pct` and `insufficient_data` appended per row.

    **A month with no invoices has no fraud rate — it does not have a rate of
    zero.** Returning `Decimal("0")` for an empty denominator reported the
    single most reassuring value on the chart for the one shape that carries
    no information at all, and did it hardest in the case that most warrants
    a look: zero invoices booked but exceptions raised anyway. `rate_pct` is
    therefore `None` there, flagged by `insufficient_data`, exactly as
    `compute_cash_conversion_cycle` returns `None` when a leg is unknown and
    `compute_effectiveness` refuses to fabricate a percentage under its
    minimum sample. See `docs/decisions.md` §34 — "cannot attest" must never
    render as "yes".
    """
    out: list[dict] = []
    for b in monthly_buckets:
        inv_count = int(b.get("invoice_count", 0) or 0)
        exc_count = int(b.get("exception_count", 0) or 0)
        rate = (
            (Decimal(exc_count) / Decimal(inv_count) * Decimal("100")).quantize(Decimal("0.1"))
            if inv_count > 0
            else None
        )
        out.append({**b, "rate_pct": rate, "insufficient_data": rate is None})
    return out


# ---------------------------------------------------------------------------
# Rebate yield
# ---------------------------------------------------------------------------


def compute_rebate_yield(
    *,
    rebates_total: Decimal,
    total_spend: Decimal,
    months_in_period: int = 12,
) -> dict:
    """Returns the rebate yield as a percentage of spend + an
    annualised run-rate dollars figure (useful for the CFO's
    investor deck)."""
    yield_pct = (
        (rebates_total / total_spend * Decimal("100")).quantize(Decimal("0.01"))
        if total_spend > 0
        else Decimal("0")
    )
    annualised = (
        (rebates_total * Decimal("12") / Decimal(months_in_period)).quantize(Decimal("0.01"))
        if months_in_period > 0
        else Decimal("0")
    )
    return {
        "rebates_total": rebates_total.quantize(Decimal("0.01")),
        "total_spend": total_spend.quantize(Decimal("0.01")),
        "yield_pct": yield_pct,
        "annualised_rebates": annualised,
    }


# ---------------------------------------------------------------------------
# Forecast variance
# ---------------------------------------------------------------------------


def compute_forecast_variance(
    monthly_forecast: list[dict],
) -> list[dict]:
    """`monthly_forecast`: `{"month": "YYYY-MM", "forecast":
    Decimal, "actual": Decimal}`. Adds `variance` (actual − forecast)
    and `variance_pct` per row. Positive variance means we paid out
    MORE than expected — the negative case is the operations win
    (came in under budget)."""
    out: list[dict] = []
    for b in monthly_forecast:
        forecast = Decimal(str(b.get("forecast", "0") or "0"))
        actual = Decimal(str(b.get("actual", "0") or "0"))
        var = (actual - forecast).quantize(Decimal("0.01"))
        var_pct = (
            (var / forecast * Decimal("100")).quantize(Decimal("0.1"))
            if forecast > 0
            else Decimal("0")
        )
        out.append(
            {
                **b,
                "forecast": forecast.quantize(Decimal("0.01")),
                "actual": actual.quantize(Decimal("0.01")),
                "variance": var,
                "variance_pct": var_pct,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Predictive cash-flow forecasting
# ---------------------------------------------------------------------------
#
# These four functions power `/api/analytics/cashflow_forecast`,
# `/cashflow_whatif`, and `/cash_position`. They are pure + sync like the
# rest of this module: the API layer hands in already-fetched commitment
# rows (one per open invoice) and serialises the Decimal results as exact
# decimal strings at the JSON boundary (`api/analytics._money`).
#
# A "commitment row" is a dict with:
#   {"due_date": date, "amount": Decimal, "committed": bool,
#    "discount_date": date | None, "discount_percent": Decimal | None}
# `committed` distinguishes firm commitments (approved → payment_scheduled)
# from the still-in-flight pipeline (new / pending / ready_for_review).

_CENTS = Decimal("0.01")


def _q(amount: Decimal) -> Decimal:
    """Quantize to cents, half-up — the money rounding used throughout."""
    return amount.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _period_bounds(d, granularity: str):
    """Return ``(period_key, period_start, period_end)`` for a date under
    the requested bucket granularity.

    - ``day``   → the date itself.
    - ``week``  → the ISO week (Monday-anchored) the date falls in.
    - ``month`` → the calendar month.

    ``period_key`` is a sortable ``YYYY-MM-DD`` string for day/week (week
    keyed on its Monday) and ``YYYY-MM`` for month so periods order
    naturally and group deterministically."""
    from datetime import timedelta

    if granularity == "day":
        return d.isoformat(), d, d
    if granularity == "week":
        monday = d - timedelta(days=d.weekday())
        sunday = monday + timedelta(days=6)
        return monday.isoformat(), monday, sunday
    if granularity == "month":
        start = d.replace(day=1)
        if start.month == 12:
            nxt = start.replace(year=start.year + 1, month=1)
        else:
            nxt = start.replace(month=start.month + 1)
        end = nxt - timedelta(days=1)
        return start.strftime("%Y-%m"), start, end
    raise ValueError(f"unknown granularity {granularity!r}")


def _row_amount(row) -> Decimal:
    return Decimal(str(row.get("amount", "0") or "0"))


def _discount_net(amount: Decimal, discount_percent) -> tuple[Decimal, Decimal]:
    """Return ``(net_outflow, discount_captured)`` for paying ``amount``
    on the discount date. ``discount_percent`` is a whole-number percent
    (e.g. ``Decimal("2")`` = 2%). Missing / non-positive percent → no
    discount (net == amount, captured == 0)."""
    pct = Decimal(str(discount_percent or "0"))
    if pct <= 0:
        return amount, Decimal("0")
    captured = amount * pct / Decimal("100")
    return amount - captured, captured


def bucket_outflows(rows: list[dict], *, granularity: str = "week", today=None) -> list[dict]:
    """Bucket projected AP outflows by ``day`` / ``week`` / ``month``.

    Each row is a commitment dict (see module header). Outflow is timed
    on the row's ``due_date``. Rows with no ``due_date`` are dropped (we
    can't time them). Returns one dict per non-empty period, sorted by
    ``period``:

        {period, period_start, period_end, scheduled_amount,
         committed_amount, pending_amount, discount_eligible_amount,
         count, unconverted_count}

    ``unconverted_count`` is how many of the period's rows arrived with a
    truthy ``unconverted`` flag — a foreign invoice whose reporting-currency
    figure could not be established, which
    ``api/analytics.py::_commitment_rows`` includes at FACE VALUE rather than
    dropping (dropping would understate the outflow). That face value is a
    different currency's number sitting in a reporting-currency total, so the
    count is what stops it being silent: a single unconverted ¥10,000,000
    invoice drags a $250,000 opening balance to a projected −$9.75M, and the
    shortfall sweep emails the finance leaders about it. The flag was computed
    on every row and read by nobody; carrying it here is what makes the
    module-header promise ("visible rather than silent") true.

    ``today`` does NOT filter which rows are bucketed — the API layer owns the
    horizon window. It governs one thing: whether a row's early-pay discount is
    still capturable (see ``discount_eligible_amount`` below). Defaults to the
    system date, mirroring ``apply_payment_timing_scenario``; every production
    caller passes it explicitly."""
    # UTC, not the host's local date: every other consumer of these rows
    # (`api/analytics`, `cash_flow_alerts`, the copilot tools) works in UTC,
    # and on a non-UTC host the local date shifts the discount-window cutoff
    # by a day relative to them.
    today = today or utc_today()
    buckets: dict[str, dict] = {}
    for r in rows:
        due = r.get("due_date")
        if due is None:
            continue
        key, start, end = _period_bounds(due, granularity)
        amount = _row_amount(r)
        b = buckets.setdefault(
            key,
            {
                "period": key,
                "period_start": start,
                "period_end": end,
                "scheduled_amount": Decimal("0"),
                "committed_amount": Decimal("0"),
                "pending_amount": Decimal("0"),
                "discount_eligible_amount": Decimal("0"),
                "count": 0,
                "unconverted_count": 0,
            },
        )
        b["scheduled_amount"] += amount
        if r.get("committed"):
            b["committed_amount"] += amount
        else:
            b["pending_amount"] += amount
        # `discount_eligible_amount` is "how much of this period's outflow can
        # still be paid early for a discount". An ELAPSED window can't be, so
        # counting it told a CFO to fund a run against savings that no longer
        # exist. The commitment rows this consumes are bounded on their DUE
        # date only (`api/analytics._commitment_rows`), so an in-horizon
        # invoice on 2/10-net-60 terms routinely arrives with a `discount_date`
        # that passed weeks ago. Same rule the discount optimizer
        # (`discount_optimizer.optimize`: "capturable only while the deadline
        # has not elapsed"), `discount_offers._tier_achievable` and the `early`
        # what-if scenario directly below already apply — the four consumers of
        # the same economics must not disagree about which discounts are on the
        # table.
        if (
            discount_window_open(r.get("discount_date"), today)
            and Decimal(str(r.get("discount_percent") or "0")) > 0
        ):
            b["discount_eligible_amount"] += amount
        b["count"] += 1
        if r.get("unconverted"):
            b["unconverted_count"] += 1

    out = []
    for key in sorted(buckets):
        b = buckets[key]
        out.append(
            {
                **b,
                "scheduled_amount": _q(b["scheduled_amount"]),
                "committed_amount": _q(b["committed_amount"]),
                "pending_amount": _q(b["pending_amount"]),
                "discount_eligible_amount": _q(b["discount_eligible_amount"]),
            }
        )
    return out


def apply_payment_timing_scenario(
    rows: list[dict],
    *,
    scenario: str,
    granularity: str = "week",
    grace_days: int = 15,
    today=None,
) -> dict:
    """What-if engine for payment timing. ``scenario``:

    - ``on_time`` → pay each row on its ``due_date``, full amount.
    - ``early``   → pay on ``discount_date`` when the window is STILL OPEN
      (``discount_date >= today``), taking the ``discount_percent``
      reduction; net outflow is reduced and the captured discount reported
      separately. Rows without a discount — and rows whose discount window
      has already ELAPSED — fall back to paying on ``due_date`` at full
      amount.
    - ``late``    → pay ``due_date + grace_days``, full amount, no
      discount (you forfeit any early-pay discount by paying late).

    The elapsed-window guard is load-bearing, not defensive coding. The
    commitment rows this consumes are bounded on their DUE date only (see
    ``api/analytics.py::_commitment_rows``), so an in-horizon invoice on
    ``2/10 net 60`` terms routinely arrives with a ``discount_date`` that
    passed weeks ago. Without the guard the ``early`` card claimed a
    discount nobody can still take, timed the outflow on a date in the
    PAST — producing periods entirely before ``today`` and a negative
    ``weighted_avg_pay_date_days`` — and told a CFO to fund a payment run
    against savings that do not exist. Same rule the discount optimizer
    already applies (``discount_optimizer.optimize``: "capturable only
    while the discount deadline has not elapsed"), so the what-if card and
    the optimizer can't disagree about which discounts are still on the
    table.

    Returns::

        {scenario, total_outflow, total_discount_captured,
         weighted_avg_pay_date_days, periods: [bucketed like
         bucket_outflows but on the scenario pay-date + net amount]}

    ``weighted_avg_pay_date_days`` is the amount-weighted mean number of
    days from ``today`` to each row's pay date — a single "how soon does
    the cash leave" number for the scenario card."""
    from datetime import timedelta

    if scenario not in ("early", "on_time", "late"):
        raise ValueError(f"unknown scenario {scenario!r}")
    # UTC — see `bucket_outflows`.
    today = today or utc_today()

    total_outflow = Decimal("0")
    total_discount = Decimal("0")
    weighted_days = Decimal("0")
    weight_total = Decimal("0")
    # Re-shape each row into a pseudo-commitment timed on the scenario's
    # pay-date with the scenario's net amount, then reuse bucket_outflows.
    timed_rows: list[dict] = []
    for r in rows:
        due = r.get("due_date")
        if due is None:
            continue
        amount = _row_amount(r)
        discount_date = r.get("discount_date")
        # An elapsed window is no longer capturable, so the row is NOT an
        # early-pay candidate at all (see the docstring). `discount_window_open`
        # is the one owner of that rule — see its own docstring for why.
        if scenario == "early" and discount_window_open(discount_date, today):
            pay_date = discount_date
            net, captured = _discount_net(amount, r.get("discount_percent"))
        elif scenario == "late":
            pay_date = due + timedelta(days=grace_days)
            net, captured = amount, Decimal("0")
        else:  # on_time, or early with no discount still available
            pay_date = due
            net, captured = amount, Decimal("0")

        total_outflow += net
        total_discount += captured
        days = Decimal((pay_date - today).days)
        weighted_days += days * net
        weight_total += net
        timed_rows.append(
            {
                "due_date": pay_date,
                "amount": net,
                "committed": r.get("committed", False),
                "discount_date": None,
                "discount_percent": None,
                # Re-timing doesn't make a row convertible — carry the flag so
                # the scenario's own periods report it too.
                "unconverted": bool(r.get("unconverted")),
            }
        )

    avg_days = (
        (weighted_days / weight_total).quantize(Decimal("0.1"))
        if weight_total > 0
        else Decimal("0")
    )
    periods = bucket_outflows(timed_rows, granularity=granularity, today=today)
    return {
        "scenario": scenario,
        "total_outflow": _q(total_outflow),
        "total_discount_captured": _q(total_discount),
        "weighted_avg_pay_date_days": avg_days,
        "periods": periods,
        # How much of `total_outflow` is a face-value figure in a currency we
        # could not convert (see `bucket_outflows`). Comparing two scenarios'
        # totals is only meaningful once this is 0.
        "unconverted_count": sum(p["unconverted_count"] for p in periods),
    }


def compute_cash_position(
    opening_balance: Decimal,
    outflow_periods: list[dict],
    *,
    inflow_periods: dict | None = None,
    min_balance_threshold: Decimal | None = None,
) -> list[dict]:
    """Running cash balance per period.

    ``outflow_periods`` is the list returned by ``bucket_outflows``.
    ``inflow_periods`` is an optional ``{period_key: Decimal}`` map of
    expected inflows — defaults to empty (AP-only product; receivables
    aren't modelled, mirroring the ``None`` handling in
    ``compute_cash_conversion_cycle``). The closing balance of each period
    carries forward as the next period's opening::

        closing = opening - outflow + inflow

    Returns one row per period::

        {period, period_start, period_end, opening, outflow, inflow,
         closing, below_threshold, unconverted_count}

    ``below_threshold`` is True iff a threshold is supplied and the
    period's closing balance falls below it.

    ``unconverted_count`` rides through from ``bucket_outflows`` (0 when the
    caller built the periods by hand). It is the caveat on the closing
    balance: those rows entered the outflow at face value in another currency,
    so a non-zero count means this period's `closing` — and every later
    period's, since the balance carries forward — is not a figure to act on
    until the conversion is resolved."""
    inflow_periods = inflow_periods or {}
    rows: list[dict] = []
    opening = Decimal(str(opening_balance or "0"))
    for p in outflow_periods:
        outflow = Decimal(str(p.get("scheduled_amount", "0") or "0"))
        inflow = Decimal(str(inflow_periods.get(p["period"], "0") or "0"))
        closing = opening - outflow + inflow
        below = min_balance_threshold is not None and closing < min_balance_threshold
        rows.append(
            {
                "period": p["period"],
                "period_start": p.get("period_start"),
                "period_end": p.get("period_end"),
                "opening": _q(opening),
                "outflow": _q(outflow),
                "inflow": _q(inflow),
                "closing": _q(closing),
                "below_threshold": below,
                "unconverted_count": int(p.get("unconverted_count", 0) or 0),
            }
        )
        opening = closing
    return rows


def detect_threshold_breaches(
    position_rows: list[dict], *, min_balance_threshold: Decimal
) -> list[dict]:
    """Return the subset of ``compute_cash_position`` rows whose closing
    balance dips below ``min_balance_threshold`` — the periods the CFO
    should be alerted about. Each breach reports the shortfall::

        {period, period_start, period_end, closing, shortfall}

    ``shortfall`` is how far below the threshold the period closed
    (always positive)."""
    threshold = Decimal(str(min_balance_threshold))
    breaches: list[dict] = []
    for r in position_rows:
        closing = Decimal(str(r.get("closing", "0") or "0"))
        if closing < threshold:
            breaches.append(
                {
                    "period": r["period"],
                    "period_start": r.get("period_start"),
                    "period_end": r.get("period_end"),
                    "closing": _q(closing),
                    "shortfall": _q(threshold - closing),
                }
            )
    return breaches
