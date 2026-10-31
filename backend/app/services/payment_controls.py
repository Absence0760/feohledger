"""Human-authorization controls on the payment money path.

Two controls live here, both about *who* may commit an outflow rather than
*whether* the outflow is arithmetically sound:

* **Maker-checker** (`check_run_segregation`) — identity-level segregation of
  duties on a payment run.
* **The CFO-approval threshold** (`cfo_approval_decision`) — the money-size gate
  above which a second, more senior human must sign off.

Identity-level maker-checker for payment runs: the user who CREATED a run must
not be the one who CFO-approves it or EXECUTES it (the actual money movement).

This is orthogonal to the role/permission split (`require_permission`): that
separates duties by ROLE, but a single user holding a role with both
`payment_run.approve` and `payment_execute` — the default `ap_manager` does —
could still create and execute the same run end-to-end with no second human.
This control closes that gap by comparing the actor's identity to the run's
`initiated_by`.

Mirrors `approval_chain.check_segregation` (uploader != invoice approver): it is
**default-on** and disabled only by an explicit per-org opt-out for genuine
single-operator accounts —
``Organization.settings.payments.require_run_segregation: false``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException, status

from app.api.refusals import ActionRefusal


def run_segregation_enabled(payment_config: dict | None) -> bool:
    """Whether maker-checker is enforced for this org's payment runs.

    Default ON. Only an explicit ``false`` on
    ``settings.payments.require_run_segregation`` turns it off (a missing key,
    ``None``, or any other value keeps the secure default).
    """
    return (payment_config or {}).get("require_run_segregation", True) is not False


def check_run_segregation(
    initiated_by: uuid.UUID | None,
    actor_id: uuid.UUID,
    payment_config: dict | None,
    *,
    action: str,
) -> None:
    """Raise 403 if ``actor_id`` is the user who created the run.

    ``action`` is a human verb for the message ("execute" / "approve"). Skips
    when the org opted out, or when ``initiated_by`` is NULL (a legacy run with
    no recorded creator — nothing to compare against).
    """
    if run_segregation_violated(initiated_by, actor_id, payment_config):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_run_segregation_message(action),
        )


def run_segregation_violated(
    initiated_by: uuid.UUID | None, actor_id: uuid.UUID | None, payment_config: dict | None
) -> bool:
    """The predicate :func:`check_run_segregation` raises on — the run's
    creator acting on it, unless the org opted out or no creator is recorded."""
    if not run_segregation_enabled(payment_config):
        return False
    if initiated_by is None or actor_id is None:
        return False
    return initiated_by == actor_id


def _run_segregation_message(action: str) -> str:
    return (
        f"Segregation of duties: the user who created this payment run "
        f"cannot also {action} it. A different user must {action} it."
    )


# ── Run action verdicts ──────────────────────────────────────────────────
#
# What `POST /runs/{id}/approve`, `/execute` and `/cancel` would answer a given
# actor, decided by ONE function per action that the endpoint itself raises
# from. `GET /runs/{id}` ships the same answers as `actions` so the run dialog
# offers a control exactly when its endpoint would accept it (issue #575) —
# it used to show a CFO who created the run an "Approve as CFO" and the
# creator an "Execute" that could only 403.
#
# The endpoints keep their plain-string `detail` (every client, mobile
# included, reads it); the verdict carries the same sentence under a code.

RUN_NOT_DRAFT = "payment_run_not_draft"
RUN_CFO_NOT_REQUIRED = "payment_run_cfo_not_required"
RUN_ALREADY_CFO_APPROVED = "payment_run_already_cfo_approved"
RUN_SEGREGATION = "payment_run_segregation"
RUN_AWAITING_CFO = "payment_run_awaiting_cfo"


def _not_draft(run, verb: str) -> ActionRefusal:
    return ActionRefusal(
        RUN_NOT_DRAFT, f"Can only {verb} 'draft' runs, not '{run.status}'", status.HTTP_409_CONFLICT
    )


def _segregation(action: str) -> ActionRefusal:
    return ActionRefusal(
        RUN_SEGREGATION, _run_segregation_message(action), status.HTTP_403_FORBIDDEN
    )


def run_approve_refusal(
    run, actor_id: uuid.UUID, payment_config: dict | None
) -> ActionRefusal | None:
    """Why ``POST /runs/{id}/approve`` would refuse ``actor_id`` (who already
    holds the CFO role — the endpoint's dependency), or ``None``."""
    if run.status != "draft":
        return _not_draft(run, "approve")
    if not run.requires_cfo_approval:
        return ActionRefusal(
            RUN_CFO_NOT_REQUIRED, "This run does not require CFO approval", status.HTTP_409_CONFLICT
        )
    if run.cfo_approved_at is not None:
        return ActionRefusal(
            RUN_ALREADY_CFO_APPROVED, "Run is already CFO-approved", status.HTTP_409_CONFLICT
        )
    # Maker-checker: the user who created the run cannot also sign it off — a
    # self-approval defeats the entire purpose of the CFO gate.
    if run_segregation_violated(run.initiated_by, actor_id, payment_config):
        return _segregation("approve")
    return None


def run_execute_refusal(
    run, actor_id: uuid.UUID, payment_config: dict | None
) -> ActionRefusal | None:
    """Why ``POST /runs/{id}/execute`` would refuse ``actor_id`` before any
    money moves, or ``None``. The processor resolution that follows (an
    unsupported provider, a record-only tenant) is org configuration the page
    reads from ``GET /api/payments/execution-mode`` instead."""
    if run.status != "draft":
        return _not_draft(run, "execute")
    # Maker-checker on the money-movement step. Default-on; per-org opt-out for
    # single-operator accounts. The role/permission split can't enforce this —
    # one user holding both perms (the default ap_manager) would otherwise run
    # the whole payment lifecycle solo.
    if run_segregation_violated(run.initiated_by, actor_id, payment_config):
        return _segregation("execute")
    if run.requires_cfo_approval and run.cfo_approved_at is None:
        return ActionRefusal(
            RUN_AWAITING_CFO,
            "This run exceeds the org's CFO-approval threshold and is awaiting "
            "sign-off from a user with the CFO role.",
            status.HTTP_403_FORBIDDEN,
        )
    return None


def run_cancel_refusal(run) -> ActionRefusal | None:
    """Why ``POST /runs/{id}/cancel`` would refuse, or ``None``."""
    if run.status != "draft":
        return _not_draft(run, "cancel")
    return None


RUN_NOT_RETRYABLE = "payment_run_not_retryable"
RUN_NOTHING_TO_RETRY = "payment_run_nothing_to_retry"

#: The run statuses that can carry a failed payment worth re-attempting. A
#: `draft` run has never been dispatched (use `/execute`), an `executing` one is
#: either mid-flight or crashed (use `/resume`), and a `cancelled` one has no
#: payments left at all.
RETRYABLE_RUN_STATUSES = ("partial", "failed")


def run_retry_failed_refusal(
    run,
    effective_status: str,
    retryable_failures: int,
    actor_id: uuid.UUID,
    payment_config: dict | None,
) -> ActionRefusal | None:
    """Why ``POST /runs/{id}/retry-failed`` would refuse ``actor_id`` before
    anything is booked, or ``None`` — the ONE definition the endpoint raises
    from and the run dialog's ``actions.retry_failed`` is read from
    (``docs/decisions.md`` §305).

    ``effective_status`` is the run status its ACTIVE payments support
    (``payment_runs.derive_run_status``), never the stored column, which no
    webhook or reconciler rewrites. ``retryable_failures`` is the count of
    active failed payments that are provably retry-safe
    (``payment_runs.is_retry_safe``): a run whose every failure might have
    reached the processor has nothing a retry may send, and the endpoint
    refuses it instead of claiming the run for an empty dispatch pass.

    Re-attempting moves money exactly like ``/execute``, so the same
    maker-checker and CFO-threshold gates apply, in the same order and with the
    same sentences. The processor resolution that follows (a record-only
    tenant, an unsupported provider) is org configuration the page reads from
    ``GET /api/payments/execution-mode``, as for Execute.
    """
    if effective_status not in RETRYABLE_RUN_STATUSES:
        return ActionRefusal(
            RUN_NOT_RETRYABLE,
            "Can only retry a run that finished with failures "
            f"({' or '.join(RETRYABLE_RUN_STATUSES)}), not '{effective_status}'",
            status.HTTP_409_CONFLICT,
        )
    if run_segregation_violated(run.initiated_by, actor_id, payment_config):
        return _segregation("execute")
    if run.requires_cfo_approval and run.cfo_approved_at is None:
        return ActionRefusal(
            RUN_AWAITING_CFO,
            "This run exceeds the org's CFO-approval threshold and is awaiting "
            "sign-off from a user with the CFO role.",
            status.HTTP_403_FORBIDDEN,
        )
    if retryable_failures <= 0:
        return ActionRefusal(
            RUN_NOTHING_TO_RETRY,
            "None of this run's failed payments can be sent again safely: each may "
            "have reached the payment processor. Check each one with the processor, "
            "then void it or record what happened.",
            status.HTTP_409_CONFLICT,
        )
    return None


# ── Payment void verdict ─────────────────────────────────────────────────

PAYMENT_VOID_TERMINAL = "payment_void_terminal"
PAYMENT_VOID_FAILED = "payment_void_failed"
PAYMENT_VOID_COMPLIANCE_HOLD = "payment_void_compliance_hold"
PAYMENT_VOID_NOT_SENT = "payment_void_not_sent"

#: The statuses a void reverses: money that has left, or is leaving, the rail.
VOIDABLE_PAYMENT_STATUSES = frozenset({"submitted", "processing", "completed"})


def payment_void_refusal(payment) -> ActionRefusal | None:
    """Why ``POST /api/payments/{id}/void`` would refuse ``payment``, or
    ``None`` — the ONE definition the endpoint raises from and the History
    list's ``void_refusal`` is read from (issue #575).

    A void reverses money that has LEFT, or is leaving, through the rail:
    ``submitted`` / ``processing`` / ``completed``. Everything else has a
    purpose-built exit and is refused here:

    * ``voided`` / ``cancelled`` — already terminal;
    * ``failed`` — it never settled;
    * ``pending_compliance`` — nothing reached the processor; Release or
      Dismiss resolve the hold AND its ``payment_compliance_hold`` exception,
      which a void used to leave open beside a re-queued invoice;
    * ``pending`` on a RUN leg — a leg of a run that has not been sent. On a
      draft run the exit is Cancel run; on an exported run it is Bank rejected
      the file or Record run as paid. A void here used to put the invoice back
      in the queue while the uploaded bank file could still pay it — a second
      payment of the same invoice.

    A ``pending`` payment with NO run (booked by ``POST /api/payments``) stays
    voidable: nothing will ever send it, it holds the invoice's one live-payment
    slot, and the void is its only undo — refusing it would leave recording a
    payment that never happened as the sole way out.
    """
    if payment.status in ("voided", "cancelled"):
        return ActionRefusal(
            PAYMENT_VOID_TERMINAL, f"Payment already {payment.status}", status.HTTP_409_CONFLICT
        )
    if payment.status == "failed":
        return ActionRefusal(
            PAYMENT_VOID_FAILED,
            "Cannot void a failed payment (it never settled)",
            status.HTTP_409_CONFLICT,
        )
    if payment.status == "pending_compliance":
        return ActionRefusal(
            PAYMENT_VOID_COMPLIANCE_HOLD,
            "This payment is held for a compliance check and never reached the processor. "
            "Release or dismiss the hold instead.",
            status.HTTP_409_CONFLICT,
        )
    if payment.status == "pending" and payment.payment_run_id is None:
        return None
    if payment.status == "pending":
        return ActionRefusal(
            PAYMENT_VOID_NOT_SENT,
            "This payment has not been sent. Cancel its draft run, or, for a bank file, "
            "mark the file rejected or record the run as paid.",
            status.HTTP_409_CONFLICT,
        )
    if payment.status not in VOIDABLE_PAYMENT_STATUSES:
        # A status this function predates is refused, never voided by omission.
        return ActionRefusal(
            PAYMENT_VOID_TERMINAL,
            f"A '{payment.status}' payment cannot be voided",
            status.HTTP_409_CONFLICT,
        )
    return None


# ── Compliance-hold verdicts ─────────────────────────────────────────────

PAYMENT_NOT_ON_COMPLIANCE_HOLD = "payment_not_on_compliance_hold"


def compliance_hold_refusal(payment, verb: str) -> ActionRefusal | None:
    """Why ``POST /api/payments/{id}/compliance/{release|dismiss}`` would refuse
    ``payment`` on its status, or ``None`` — the ONE definition both endpoints
    raise from and the History list's ``release_refusal`` / ``dismiss_refusal``
    are read from (issue #575). Only a payment the sanctions/KYC gate parked at
    ``pending_compliance`` has a hold to release or dismiss. ``verb`` is
    ``"release"`` or ``"dismiss"``, for the sentence only."""
    if payment.status == "pending_compliance":
        return None
    return ActionRefusal(
        PAYMENT_NOT_ON_COMPLIANCE_HOLD,
        f"Can only {verb} a payment stuck 'pending_compliance', not '{payment.status}'",
        status.HTTP_409_CONFLICT,
    )


# ── CFO-approval threshold ───────────────────────────────────────────────


#: Why the CFO gate fired. A fixed, PII-free vocabulary — these strings ride
#: audit rows and logs.
CFO_REASON_ABOVE_THRESHOLD = "above_threshold"
CFO_REASON_THRESHOLD_UNPARSEABLE = "threshold_unparseable"
CFO_REASON_AMOUNT_NOT_EXPRESSIBLE = "amount_not_expressible_in_reporting_currency"


@dataclass(frozen=True)
class CfoThresholdDecision:
    """Whether an outflow needs CFO sign-off, and why.

    ``evaluated_amount`` / ``currency`` record WHAT was compared, because for a
    foreign-currency payable that is not the figure the run reports as its
    total — a GBP 9,000 run is gated on its USD 11,400 equivalent. Both are
    PII-free and belong on the audit row so the decision is reconstructable.
    ``evaluated_amount`` is ``None`` when no comparison was possible.
    """

    required: bool
    reason: str | None = None
    threshold: Decimal | None = None
    evaluated_amount: Decimal | None = None
    currency: str | None = None


def cfo_approval_decision(
    *,
    payment_config: dict | None,
    reporting_amount: Decimal,
    reporting_currency: str,
    unconverted: bool,
) -> CfoThresholdDecision:
    """Does this outflow clear the org's ``payments.cfo_approval_above`` gate?

    **The threshold is a bare number denominated in the org's REPORTING
    currency**, exactly like ``settings.expense_approval.cfo_threshold`` — so
    the amount has to be expressed in that currency before it can be compared.
    It used to be compared against the raw invoice-currency figure, which made
    the gate fail OPEN for every foreign-currency payable priced below the
    threshold in its own units: a GBP 9,000 run (USD 11,400) slipped under a
    USD 10,000 threshold and executed with no CFO sign-off. Refusing a
    *mixed*-currency run — which ``create_payment_run_for_invoices`` already
    does — closes the batch half of that hole and none of this one.

    Fails **closed** on both kinds of missing information, matching
    ``services/expense_currency``:

    * an unparseable threshold (a typo'd settings value) requires sign-off
      rather than silently disabling the control; and
    * an amount that cannot be expressed in the reporting currency (no rate
      locked on the invoice row, or a lock that no longer describes its
      currency pair) is treated as *over* the threshold, never under it.

    A threshold that is absent, ``0`` or negative disables the gate entirely —
    unchanged, and the only path that returns ``required=False`` without a
    comparison. Pure: no DB, no clock, no FX call.
    """
    raw = (payment_config or {}).get("cfo_approval_above")
    currency = (reporting_currency or "USD").strip().upper()
    if raw is None:
        return CfoThresholdDecision(required=False, currency=currency)

    try:
        threshold = Decimal(str(raw))
    except (ValueError, ArithmeticError):
        return CfoThresholdDecision(
            required=True,
            reason=CFO_REASON_THRESHOLD_UNPARSEABLE,
            currency=currency,
        )

    if threshold <= 0:
        return CfoThresholdDecision(required=False, threshold=threshold, currency=currency)

    if unconverted:
        return CfoThresholdDecision(
            required=True,
            reason=CFO_REASON_AMOUNT_NOT_EXPRESSIBLE,
            threshold=threshold,
            currency=currency,
        )

    # Strict `>` matches the setting name (`cfo_approval_above`): a total
    # exactly AT the threshold does not require sign-off.
    over = reporting_amount > threshold
    return CfoThresholdDecision(
        required=over,
        reason=(CFO_REASON_ABOVE_THRESHOLD if over else None),
        threshold=threshold,
        evaluated_amount=reporting_amount,
        currency=currency,
    )
