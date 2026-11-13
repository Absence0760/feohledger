"""Expense policy engine — pure, deterministic, no LLM, no DB, no network.

Given an ``Expense`` (or a report's expenses) and the set of active
``ExpensePolicy`` rows, derive the list of policy violations. The engine itself
does **no** I/O: the caller (``api/expenses.py`` / the report-submit route)
loads the active policies (and any approved pre-approvals) from the tenant DB
and hands them in, so this module stays trivially unit-testable.

Money is ``Decimal`` throughout — never ``float``. Mileage reimbursement,
per-diem caps, and category limits all compare ``Decimal`` to ``Decimal``.

A violation is a small JSON-serialisable dict::

    {"code": "category_limit", "message": "...", "policy_id": "<uuid>",
     "limit": "100.00", "actual": "150.00", "currency": "USD",
     "params": {"amount": "150.00", "amount_currency": "USD", "limit": "100.00",
                "currency": "USD", "comparison": "resolved", "category": "travel"}}

``code`` is one of the module-level ``VIOLATION_*`` constants. ``params`` is the
coded half a client states the violation from in the reader's language
(``VIOLATION_PARAMS`` is the catalogue of which keys each code carries; money is
an exact decimal string beside its ISO code, never a float). ``message`` stays
the server's English sentence for a client that predates a code, and the flat
``limit`` / ``actual`` / ``currency`` keys stay for the badges and the rows
already stored in ``expenses.policy_violations`` (``docs/decisions.md`` §378). ``BLOCKING_CODES``
names the subset that blocks report submission (missing required receipt, or a
required pre-approval that is absent / not approved). Everything else is
advisory and surfaces as a warning badge.

Every comparison is **currency-aware**. A policy's money thresholds
(``category_limit`` / ``per_diem_amount`` / ``requires_receipt_above`` /
``requires_preapproval_above`` / ``mileage_rate``) are denominated in the policy's
``threshold_currency``, and the expense is expressed in that currency before
being compared — a €200 EUR expense is never judged against a USD 100 limit as
bare numbers. ``threshold_currency`` is ``NULL`` on rows that predate the
column; the caller then supplies the org's **reporting currency** as
``default_threshold_currency`` (that is the unit the whole app already assumes
for a bare number — the CFO expense threshold, the policy table in the UI).

No FX call happens here: this engine is pure. The expense is expressed in the
threshold currency using only what a write path already locked
(``expense_currency.expense_amount_in_currency``). When that yields ``None`` —
a foreign-currency expense with no lock into the threshold currency — the
comparison genuinely **cannot** be made, and every rule then **fails closed**:
the violation is raised anyway, tagged ``comparison: "unresolved"``. A blocking
rule therefore demands the receipt / pre-approval instead of waving the expense
through on a comparison that was never performed.

See ``backend/docs/expense-management.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from app.services.expense_currency import expense_amount_in_currency, normalize_currency

# Violation codes ----------------------------------------------------------
VIOLATION_CATEGORY_LIMIT = "category_limit"
VIOLATION_RECEIPT_REQUIRED = "receipt_required"
VIOLATION_PREAPPROVAL_REQUIRED = "preapproval_required"
VIOLATION_PER_DIEM_EXCEEDED = "per_diem_exceeded"
VIOLATION_MILEAGE_MISMATCH = "mileage_amount_mismatch"

# The subset that blocks report submission. The rest are advisory.
#
# The two blocking codes are both *missing evidence the submitter can supply* —
# attach the receipt, get the pre-approval — so a 422 at submit is actionable.
# ``mileage_amount_mismatch`` is deliberately NOT one of them: it is a
# disagreement between two numbers the employee already supplied, and which of
# them is wrong is a human judgement (the rate changed mid-period, the line
# bundles a toll, the claim is deliberately *under* the entitlement). Blocking
# would strand a legitimate claim with no in-app override; the approver, who
# sees the badge carrying the exact expected figure, is the right control point.
BLOCKING_CODES = frozenset({VIOLATION_RECEIPT_REQUIRED, VIOLATION_PREAPPROVAL_REQUIRED})

# Rounding slack on the mileage comparison. ``miles`` is ``Numeric(10, 2)`` and
# ``mileage_rate`` ``Numeric(10, 4)``, so their product carries up to 6 dp while
# the claim is ``Numeric(15, 2)`` — a half-up 2 dp expectation and a claim
# rounded the other way legitimately differ by a cent. Same one-cent tolerance
# the settlement-amount check uses (``services/payment_settlement``).
MILEAGE_TOLERANCE = Decimal("0.01")
_MONEY_QUANT = Decimal("0.01")

# Marker on a violation raised without a performed comparison (see the module
# docstring): the expense could not be expressed in the threshold's currency, so
# the rule fell closed. Surfaced so the UI can explain the flag honestly.
COMPARISON_UNRESOLVED = "unresolved"
COMPARISON_RESOLVED = "resolved"

# Mileage-mismatch direction (``params.direction``): which side of the
# entitlement the claim fell on. ``None`` when the comparison was unresolved.
MILEAGE_OVER = "over"
MILEAGE_UNDER = "under"

#: The params every violation carries, by code — what a client may rely on to
#: state it (``frontend/src/lib/api/policyViolations.ts``). Common to all:
#:
#: - ``amount`` / ``amount_currency`` — the figure that was (or would have been)
#:   compared: the converted amount in ``currency`` when the comparison resolved,
#:   the expense's face amount in its own currency when it did not;
#: - ``limit`` / ``currency`` — the policy's threshold and its unit;
#: - ``comparison`` — ``"resolved"`` or ``"unresolved"`` (fell closed).
#:
#: ``category_limit`` adds ``category`` (the expense's own category, ``None``
#: when it has none); ``mileage_amount_mismatch`` adds ``miles``, ``rate``
#: (per mile, in ``currency``) and ``direction``. Every figure is ``str(Decimal)``.
_COMMON_PARAMS = ("amount", "amount_currency", "limit", "currency", "comparison")
VIOLATION_PARAMS: dict[str, tuple[str, ...]] = {
    VIOLATION_CATEGORY_LIMIT: (*_COMMON_PARAMS, "category"),
    VIOLATION_RECEIPT_REQUIRED: _COMMON_PARAMS,
    VIOLATION_PREAPPROVAL_REQUIRED: _COMMON_PARAMS,
    VIOLATION_PER_DIEM_EXCEEDED: _COMMON_PARAMS,
    VIOLATION_MILEAGE_MISMATCH: (*_COMMON_PARAMS, "miles", "rate", "direction"),
}


def _as_decimal(value: Any) -> Decimal | None:
    """Coerce a money-ish value to ``Decimal`` (via ``str`` for float safety).

    Returns ``None`` for ``None`` / unparseable input so callers can treat a
    missing limit as "no rule"."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _policy_applies(policy: Any, category: str | None) -> bool:
    """A policy applies to an expense when the policy is active AND its category
    is NULL (applies to all) or matches the expense's category."""
    if not getattr(policy, "active", False):
        return False
    pol_cat = getattr(policy, "category", None)
    return pol_cat is None or pol_cat == category


def threshold_currency_for(policy: Any, default_currency: str) -> str:
    """The currency a policy's money thresholds are denominated in.

    ``ExpensePolicy.threshold_currency`` when the admin has set one; otherwise
    ``default_currency``, which callers resolve as the org's reporting currency
    (``currency_conversion.resolve_reporting_currency``). ``NULL`` is the state
    of every row created before the column existed: a tenant-DB migration cannot
    read the control-plane org settings, so the unit is resolved here, at
    evaluation time, from live config rather than frozen to a guess at upgrade
    time. Never raises."""
    return normalize_currency(getattr(policy, "threshold_currency", None), default=default_currency)


@dataclass(frozen=True)
class MileageExpectation:
    """What the org's own policy says a logged trip is worth.

    ``amount`` is the exact product (``miles * rate``, unrounded — the figure
    ``mileage_reimbursement`` has always returned); ``rounded`` is that product
    quantized to the 2 dp a claim is stored at, which is what a claim is
    compared against. ``currency`` is the unit — the rate's policy's
    ``threshold_currency``, exactly like every other money threshold on the row.
    """

    policy_id: str | None
    currency: str
    miles: Decimal
    rate: Decimal
    amount: Decimal
    rounded: Decimal


def resolve_mileage_expectation(
    expense: Any,
    policies: list[Any],
    *,
    default_threshold_currency: str = "USD",
) -> MileageExpectation | None:
    """The single owner of "what is this trip worth, and in what currency".

    Returns ``None`` when the question doesn't arise: the expense logs no
    positive ``mileage_miles``, or no applicable active policy carries a usable
    ``mileage_rate``. Both `mileage_reimbursement` (the amount) and
    ``evaluate_expense``'s mileage rule (the violation) read this, so the figure
    a violation quotes can never disagree with the figure the helper computes.

    **A rate must be strictly positive to count.** ``mileage_rate`` is nullable,
    so NULL is how "this org does not reimburse mileage" is expressed; a literal
    ``0.0000`` is far more likely a form artifact than a deliberate zero-rate
    policy. Treating it as unset also stops a zero-rate policy from shadowing a
    later applicable policy that does carry a real rate, and stops every mileage
    claim in the tenant from flagging against an expectation of nothing.

    Pure ``Decimal`` math, no float, never raises."""
    miles = _as_decimal(getattr(expense, "mileage_miles", None))
    if miles is None or miles <= 0:
        return None
    category = getattr(expense, "category", None)
    for policy in policies:
        if not _policy_applies(policy, category):
            continue
        rate = _as_decimal(getattr(policy, "mileage_rate", None))
        if rate is None or rate <= 0:
            continue
        amount = miles * rate
        return MileageExpectation(
            policy_id=str(getattr(policy, "id", "")) or None,
            currency=threshold_currency_for(policy, default_threshold_currency),
            miles=miles,
            rate=rate,
            amount=amount,
            rounded=amount.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP),
        )
    return None


def mileage_reimbursement(
    expense: Any,
    policies: list[Any],
    *,
    default_threshold_currency: str = "USD",
) -> Decimal:
    """Reimbursable mileage amount = ``mileage_miles * mileage_rate``.

    The rate comes from the first applicable active policy that carries one, and
    the result is denominated in that policy's ``threshold_currency``. Returns
    ``Decimal("0")`` when the expense logs no miles or no policy sets a rate.
    Pure ``Decimal`` math (no float).

    A bare ``Decimal`` carries no unit, so prefer
    ``resolve_mileage_expectation`` when the currency matters; this stays as the
    amount-only view for callers that already know it."""
    expectation = resolve_mileage_expectation(
        expense, policies, default_threshold_currency=default_threshold_currency
    )
    return expectation.amount if expectation is not None else Decimal("0")


def _violation(
    code: str,
    *,
    message: str,
    policy_id: str | None,
    limit: Decimal,
    actual: Decimal,
    currency: str,
    expense_currency: str,
    unresolved: bool,
    extra: dict | None = None,
    extra_params: dict | None = None,
) -> dict:
    """Build one violation dict. PII-free by construction — amounts, ISO
    currency codes and the policy's own category only, never a merchant, a
    person, or a bank detail.

    ``params`` carries exactly the keys ``VIOLATION_PARAMS[code]`` names."""
    params: dict = {
        "amount": str(actual),
        "amount_currency": expense_currency if unresolved else currency,
        "limit": str(limit),
        "currency": currency,
        "comparison": COMPARISON_UNRESOLVED if unresolved else COMPARISON_RESOLVED,
    }
    if extra_params:
        params.update(extra_params)
    entry = {
        "code": code,
        "message": message,
        "params": params,
        "policy_id": policy_id,
        "limit": str(limit),
        "actual": str(actual),
        # The unit BOTH ``limit`` and (when resolved) ``actual`` are in.
        "currency": currency,
    }
    if extra:
        entry.update(extra)
    if unresolved:
        # ``actual`` is the expense's own face amount here, in its own currency —
        # no conversion existed to express it in ``currency``.
        entry["comparison"] = COMPARISON_UNRESOLVED
        entry["expense_currency"] = expense_currency
    return entry


def evaluate_expense(
    expense: Any,
    policies: list[Any],
    *,
    approved_preapproval_amount: Decimal | None = None,
    default_threshold_currency: str = "USD",
) -> list[dict]:
    """Evaluate a single expense against the active policies.

    Returns a list of violation dicts (empty when clean). Every money rule
    compares the expense **in the policy's threshold currency**
    (``threshold_currency_for``), using only a conversion a write path already
    locked onto the row — never a fresh rate. Rules, per applicable policy:

    - ``category_limit``: the expense exceeds the policy's ``category_limit``.
    - ``receipt_required``: the expense exceeds ``requires_receipt_above`` and no
      ``receipt_file_key`` is present (BLOCKING).
    - ``preapproval_required``: the expense exceeds ``requires_preapproval_above``
      and no approved pre-approval covers it (BLOCKING). ``approved_preapproval_amount``
      is the estimated amount of a linked approved pre-approval (the caller
      resolves it from the DB, currency-matched to the expense); when present and
      >= the expense amount the rule is satisfied.
    - ``per_diem_exceeded``: the expense exceeds the policy's ``per_diem_amount``.
    - ``mileage_amount_mismatch``: the expense logs ``mileage_miles`` and an
      applicable policy sets a ``mileage_rate``, but the claimed amount is not
      ``miles * rate`` (± one cent). Advisory, and raised in **both** directions
      — an over-claim is the money leak, an under-claim means one of the two
      numbers the employee typed is still wrong. At most one is raised per
      expense: unlike a category limit (each policy is its own ceiling), a
      mileage rate is *the* rate, so two contradictory expectations would be
      worse than the one ``resolve_mileage_expectation`` picks.

    ``default_threshold_currency`` is the unit for policies with no
    ``threshold_currency`` set — callers pass the org's reporting currency.

    **Fail closed.** When the expense cannot be expressed in a policy's threshold
    currency, that policy's rules are raised anyway, tagged
    ``comparison: "unresolved"``: a receipt / pre-approval is *demanded* rather
    than waived on a comparison that never happened, and the advisory limits flag
    for a human instead of silently passing. The one thing that still clears a
    blocking rule is evidence that does not depend on the rate — an attached
    receipt, or a pre-approval covering the expense in the expense's own currency.

    All comparisons are ``Decimal``. Best-effort: an unparseable amount yields no
    violations rather than raising (callers wrap this anyway)."""
    amount = _as_decimal(getattr(expense, "amount", None))
    if amount is None:
        return []
    category = getattr(expense, "category", None)
    receipt_key = getattr(expense, "receipt_file_key", None)
    covered = _as_decimal(approved_preapproval_amount)
    expense_currency = normalize_currency(
        getattr(expense, "currency", None), default=default_threshold_currency
    )

    violations: list[dict] = []
    for policy in policies:
        if not _policy_applies(policy, category):
            continue
        policy_id = str(getattr(policy, "id", "")) or None
        tcur = threshold_currency_for(policy, default_threshold_currency)
        comparable = expense_amount_in_currency(expense, target_currency=tcur)
        # Each rule's breach test below reads ``unresolved or comparable > limit``:
        # a comparison that cannot be made counts as a breach (fail closed).
        unresolved = comparable is None
        # ``actual`` reports the figure that was (or would have been) compared —
        # the converted figure, or the face amount when nothing could convert it.
        actual = amount if unresolved else comparable

        cat_limit = _as_decimal(getattr(policy, "category_limit", None))
        if cat_limit is not None and (unresolved or comparable > cat_limit):
            label = category or "category"
            message = (
                f"Amount {amount} {expense_currency} cannot be converted to {tcur} to check "
                f"the {label} limit of {cat_limit} {tcur}; flagged for review."
                if unresolved
                else f"Amount {actual} {tcur} exceeds the {label} limit of {cat_limit} {tcur}."
            )
            violations.append(
                _violation(
                    VIOLATION_CATEGORY_LIMIT,
                    message=message,
                    policy_id=policy_id,
                    limit=cat_limit,
                    actual=actual,
                    currency=tcur,
                    expense_currency=expense_currency,
                    unresolved=unresolved,
                    # The expense's category, as the English names it (the
                    # policy's whenever it names one; NULL = uncategorised).
                    extra_params={"category": category},
                )
            )

        receipt_above = _as_decimal(getattr(policy, "requires_receipt_above", None))
        if (
            receipt_above is not None
            and (unresolved or comparable > receipt_above)
            and not receipt_key
        ):
            message = (
                f"A receipt is required: amount {amount} {expense_currency} cannot be converted "
                f"to {tcur} to test the {receipt_above} {tcur} receipt threshold."
                if unresolved
                else f"A receipt is required for amounts above {receipt_above} {tcur}."
            )
            violations.append(
                _violation(
                    VIOLATION_RECEIPT_REQUIRED,
                    message=message,
                    policy_id=policy_id,
                    limit=receipt_above,
                    actual=actual,
                    currency=tcur,
                    expense_currency=expense_currency,
                    unresolved=unresolved,
                )
            )

        preapproval_above = _as_decimal(getattr(policy, "requires_preapproval_above", None))
        if preapproval_above is not None and (unresolved or comparable > preapproval_above):
            # ``covered`` is currency-matched to the expense by the caller, so
            # this check stands on its own even when the threshold is unresolved.
            satisfied = covered is not None and covered >= amount
            if not satisfied:
                message = (
                    f"Pre-approval is required: amount {amount} {expense_currency} cannot be "
                    f"converted to {tcur} to test the {preapproval_above} {tcur} threshold."
                    if unresolved
                    else f"Pre-approval is required for amounts above {preapproval_above} {tcur}."
                )
                violations.append(
                    _violation(
                        VIOLATION_PREAPPROVAL_REQUIRED,
                        message=message,
                        policy_id=policy_id,
                        limit=preapproval_above,
                        actual=actual,
                        currency=tcur,
                        expense_currency=expense_currency,
                        unresolved=unresolved,
                    )
                )

        per_diem = _as_decimal(getattr(policy, "per_diem_amount", None))
        if per_diem is not None and (unresolved or comparable > per_diem):
            message = (
                f"Amount {amount} {expense_currency} cannot be converted to {tcur} to check "
                f"the per-diem cap of {per_diem} {tcur}; flagged for review."
                if unresolved
                else f"Amount {actual} {tcur} exceeds the per-diem cap of {per_diem} {tcur}."
            )
            violations.append(
                _violation(
                    VIOLATION_PER_DIEM_EXCEEDED,
                    message=message,
                    policy_id=policy_id,
                    limit=per_diem,
                    actual=actual,
                    currency=tcur,
                    expense_currency=expense_currency,
                    unresolved=unresolved,
                )
            )

    mileage = _mileage_violation(
        expense,
        policies,
        amount=amount,
        expense_currency=expense_currency,
        default_threshold_currency=default_threshold_currency,
    )
    if mileage is not None:
        violations.append(mileage)

    return violations


def _mileage_violation(
    expense: Any,
    policies: list[Any],
    *,
    amount: Decimal,
    expense_currency: str,
    default_threshold_currency: str,
) -> dict | None:
    """The ``mileage_amount_mismatch`` rule — see ``evaluate_expense``.

    Lives outside the per-policy loop because the expectation is resolved once
    for the whole expense (``resolve_mileage_expectation``), not once per policy.
    """
    expectation = resolve_mileage_expectation(
        expense, policies, default_threshold_currency=default_threshold_currency
    )
    if expectation is None:
        return None

    cur = expectation.currency
    expected = expectation.rounded
    comparable = expense_amount_in_currency(expense, target_currency=cur)
    unresolved = comparable is None
    actual = amount if unresolved else comparable
    basis = f"{expectation.miles} mi x {expectation.rate}"

    direction: str | None
    if unresolved:
        direction = None
        message = (
            f"Claimed {amount} {expense_currency} cannot be converted to {cur} to check the "
            f"mileage entitlement of {expected} {cur} ({basis}); flagged for review."
        )
    elif comparable > expected + MILEAGE_TOLERANCE:
        direction = MILEAGE_OVER
        message = (
            f"Claimed {actual} {cur} exceeds the mileage entitlement of {expected} {cur} ({basis})."
        )
    elif comparable < expected - MILEAGE_TOLERANCE:
        direction = MILEAGE_UNDER
        message = (
            f"Claimed {actual} {cur} is below the mileage entitlement of "
            f"{expected} {cur} ({basis})."
        )
    else:
        return None

    return _violation(
        VIOLATION_MILEAGE_MISMATCH,
        message=message,
        policy_id=expectation.policy_id,
        limit=expected,
        actual=actual,
        currency=cur,
        expense_currency=expense_currency,
        unresolved=unresolved,
        # Exact strings so an approver UI can show the working without
        # re-deriving it (and without money ever becoming a JS number).
        extra={"miles": str(expectation.miles), "rate": str(expectation.rate)},
        extra_params={
            "miles": str(expectation.miles),
            "rate": str(expectation.rate),
            "direction": direction,
        },
    )


def evaluate_report(
    report: Any,
    expenses: list[Any],
    policies: list[Any],
    *,
    preapproval_amount_by_expense: dict | None = None,
    default_threshold_currency: str = "USD",
) -> list[dict]:
    """Aggregate per-expense violations across a report.

    Returns a flat list of violation dicts, each annotated with the source
    ``expense_id`` so the caller can map a blocking violation back to a line.
    ``preapproval_amount_by_expense`` optionally maps an expense id (str) to the
    estimated amount of an approved pre-approval covering it.
    ``default_threshold_currency`` is forwarded to ``evaluate_expense``."""
    cover = preapproval_amount_by_expense or {}
    aggregate: list[dict] = []
    for expense in expenses:
        eid = str(getattr(expense, "id", "")) or None
        per_expense = evaluate_expense(
            expense,
            policies,
            approved_preapproval_amount=cover.get(eid),
            default_threshold_currency=default_threshold_currency,
        )
        for violation in per_expense:
            entry = dict(violation)
            entry["expense_id"] = eid
            aggregate.append(entry)
    return aggregate


def blocking_violations(violations: list[dict]) -> list[dict]:
    """Filter a violation list to the submission-blocking subset."""
    return [v for v in violations if v.get("code") in BLOCKING_CODES]
