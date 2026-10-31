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


# ── Settlement-acceptance verdict ────────────────────────────────────────

PAYMENT_SETTLEMENT_NOT_COMPLETED = "payment_settlement_not_completed"
PAYMENT_SETTLEMENT_COVERED = "payment_settlement_covered"
PAYMENT_SETTLEMENT_NOT_HELD = "payment_settlement_not_held"


def payment_settlement_coverage(payment, invoice):
    """``payment_settlement.settlement_coverage`` over a payment and the
    invoice it pays — the one spelling of the arguments, shared by
    ``POST /{id}/settlement/accept`` (which records the result on its audit
    row) and :func:`settlement_accept_refusal`."""
    from app.services.payment_settlement import settlement_coverage

    return settlement_coverage(
        settled_amount=payment.settled_amount,
        settled_currency=payment.settled_currency,
        target_amount=payment.amount,
        target_currency=(invoice.currency if invoice is not None else None),
        source_amount=payment.source_amount,
        source_currency=payment.source_currency,
        settled_amount_unstorable=payment.settled_amount_unstorable,
    )


def settlement_accept_refusal(payment, invoice) -> ActionRefusal | None:
    """Why ``POST /api/payments/{id}/settlement/accept`` would refuse, or
    ``None`` — the ONE definition the endpoint raises from and the History
    list's ``accept_settlement_refusal`` is read from (issue #575).

    ``invoice`` is the invoice the payment pays (``None`` when it is gone).
    There is something to accept only when all three hold, checked in the
    endpoint's order:

    * the payment ``completed`` — a settlement actually happened;
    * the recorded settlement does NOT discharge the invoice
      (``settlement_coverage`` is ``short`` or ``uncertain``) — a covered one
      reaches ``paid`` through the ordinary sync, and accepting it would make
      this a general "force the invoice to paid" lever. An unreported figure
      reads ``covered`` too, so a payment no rail reported on is never offered
      it;
    * the invoice is still held at ``payment_scheduled`` — the release this
      performs. A repeat acceptance finds it ``paid`` and is refused, so the
      immutable trail never carries a second justification.

    The page used to offer Accept on every ``completed`` payment with any
    reported figure, then fetch the invoice to discover whether it was held.
    """
    if payment.status != "completed":
        return ActionRefusal(
            PAYMENT_SETTLEMENT_NOT_COMPLETED,
            f"Can only accept the settlement of a 'completed' payment, not '{payment.status}'",
            status.HTTP_409_CONFLICT,
        )
    if payment_settlement_coverage(payment, invoice).completes_invoice:
        return ActionRefusal(
            PAYMENT_SETTLEMENT_COVERED,
            "This payment's settlement already covers the invoice; there is nothing to accept.",
            status.HTTP_409_CONFLICT,
        )
    invoice_status = getattr(getattr(invoice, "status", None), "value", None)
    if invoice is None or invoice_status != "payment_scheduled":
        return ActionRefusal(
            PAYMENT_SETTLEMENT_NOT_HELD,
            "No held invoice to release for this payment.",
            status.HTTP_409_CONFLICT,
        )
    return None


# ── Run ERP sync-back verdict ────────────────────────────────────────────

RUN_NOTHING_SETTLED = "payment_run_nothing_settled"


def run_sync_erp_refusal(settled_payments: int) -> ActionRefusal | None:
    """Why ``POST /runs/{id}/sync-erp`` would refuse, or ``None`` — the ONE
    definition the endpoint raises from and the Runs list's
    ``actions.sync_erp`` is read from (issue #575).

    ``settled_payments`` counts the run's ACTIVE payments (a superseded retry
    attempt excluded, as everywhere a run is rolled up) that are
    ``completed``. The sync-back only ever reports money that already moved,
    so a run with none has nothing to send. The page used to offer the action
    on every run that was not ``draft`` / ``cancelled`` — a ``failed`` run,
    or one still ``executing``, got a button that could only 409.
    """
    if settled_payments <= 0:
        return ActionRefusal(
            RUN_NOTHING_SETTLED,
            "This run has no settled payments to sync to the ERP.",
            status.HTTP_409_CONFLICT,
        )
    return None


# ── Paying a run outside FeohLedger: record / bank file / bank rejected ──

RUN_NOT_RECORDABLE = "payment_run_not_recordable"
RUN_NOTHING_TO_RECORD = "payment_run_nothing_to_record"

#: The run statuses `/record-outside` closes out: `exported` (the customer
#: uploaded the run's NACHA file and its bank took it) and `draft` (paid some
#: other way — the bank's own portal, a file it built itself).
RECORDABLE_RUN_STATUSES = ("draft", "exported")


def _awaiting_cfo() -> ActionRefusal:
    return ActionRefusal(
        RUN_AWAITING_CFO,
        "This run exceeds the org's CFO-approval threshold and is awaiting "
        "sign-off from a user with the CFO role.",
        status.HTTP_403_FORBIDDEN,
    )


def _segregation_for(action: str, step: str) -> ActionRefusal:
    """Maker-checker for a step other than approve / execute: the same code,
    with the step named by a stable token in ``params.step``
    (``record_outside`` / ``nacha_export``) so the page can say which —
    ``action`` is only the English verb for the sentence."""
    return ActionRefusal(
        RUN_SEGREGATION,
        _run_segregation_message(action),
        status.HTTP_403_FORBIDDEN,
        params={"step": step},
    )


def run_record_outside_refusal(
    run,
    actor_id: uuid.UUID,
    payment_config: dict | None,
    pending_payments: int | None,
) -> ActionRefusal | None:
    """Why ``POST /runs/{id}/record-outside`` would refuse ``actor_id`` at the
    run level, or ``None`` — the ONE definition the endpoint raises from and
    the run dialog's ``actions.record_outside`` is read from (issue #575).

    Recording closes the run out, so the run's own controls hold exactly as
    for ``/execute``: maker-checker and CFO sign-off above the threshold.
    ``pending_payments`` is how many of the run's ACTIVE payments are still
    ``pending``; ``None`` skips that check, which is how the endpoint asks
    for the cheap refusals before it takes the invoice locks the count needs
    (it then asks again with the count).

    Each payment's own refusal (segregation against the invoice, a
    payment-blocking exception — ``external_payment.run_payment_refusal``)
    is NOT here: it is per invoice, needs the invoice locked, and refuses the
    whole run naming that invoice. The verdict can therefore be ``None`` and
    the POST still refuse on one of those, which the page reports beside the
    form.
    """
    if run.status not in RECORDABLE_RUN_STATUSES:
        return ActionRefusal(
            RUN_NOT_RECORDABLE,
            f"Only a 'draft' or 'exported' run can be recorded as paid, not '{run.status}'",
            status.HTTP_409_CONFLICT,
        )
    if run_segregation_violated(run.initiated_by, actor_id, payment_config):
        return _segregation_for("record as paid", "record_outside")
    if run.requires_cfo_approval and run.cfo_approved_at is None:
        return _awaiting_cfo()
    if pending_payments is not None and pending_payments <= 0:
        return ActionRefusal(
            RUN_NOTHING_TO_RECORD,
            "This run has no payments left to record",
            status.HTTP_409_CONFLICT,
        )
    return None


NACHA_REQUIRES_RECORD_ONLY = "nacha_requires_record_only"
NACHA_NOT_CONFIGURED = "nacha_not_configured"
NACHA_ALREADY_EXPORTED = "nacha_already_exported"
NACHA_RUN_NOT_EXPORTABLE = "nacha_run_not_exportable"
NACHA_NOTHING_TO_REGENERATE = "nacha_nothing_to_regenerate"
NACHA_NOTHING_TO_EXPORT = "nacha_nothing_to_export"
NACHA_RUN_NOT_EXPORTED = "nacha_run_not_exported"


def _nacha(code: str, message: str, http_status: int = status.HTTP_409_CONFLICT, **params):
    # The export always answered with a coded detail; it keeps doing so.
    return ActionRefusal(code, message, http_status, params=params or None, coded=True)


def run_nacha_export_refusal(
    run,
    actor_id: uuid.UUID,
    payment_config: dict | None,
    *,
    regenerate: bool,
    record_only: bool,
    originator_problems: list[str],
    pending_payments: int,
) -> ActionRefusal | None:
    """Why ``GET /runs/{id}/nacha`` would refuse ``actor_id`` before reading a
    single payment's bank data, or ``None`` — the ONE definition the endpoint
    raises from and the run dialog's ``actions.nacha_export``
    (``regenerate=False``) and ``actions.nacha_regenerate``
    (``regenerate=True``) are read from (issue #575).

    In order: the run's state (a first file needs ``draft``; a second needs
    ``exported`` AND ``regenerate``); the tenant records rather than sends
    payments (``record_only``, from ``payment_execution_mode``); the bank file
    is configured (``originator_problems``, from ``nacha.originator_problems``
    — names the missing settings); maker-checker; CFO sign-off; and at least
    one ``pending`` payment to put in the file.

    ``regenerate`` on a run that was never exported is refused
    (``nacha_nothing_to_regenerate``): it used to export a first file under a
    flag that says "a second one", so a reader whose run had been released
    back to ``draft`` (the bank rejected the file) re-claimed it without being
    told. Each payment's own checks (``dispatch_preflight``, ACH / USD / bank
    details) stay per payment in the endpoint and refuse the whole file
    naming the invoice.
    """
    if regenerate:
        if run.status != "exported":
            return _nacha(
                NACHA_NOTHING_TO_REGENERATE,
                "This run has no exported bank file to regenerate.",
            )
    elif run.status == "exported":
        return _nacha(
            NACHA_ALREADY_EXPORTED,
            "This run's bank file was already exported. Uploading a second file "
            "would pay these suppliers twice. Regenerate only if your bank never "
            "took the first one.",
        )
    elif run.status != "draft":
        return _nacha(
            NACHA_RUN_NOT_EXPORTABLE,
            f"Only a 'draft' run can be exported as a bank file, not '{run.status}'",
        )
    if not record_only:
        return _nacha(
            NACHA_REQUIRES_RECORD_ONLY,
            "A bank file can only be exported when FeohLedger records payments rather "
            "than sending them — otherwise the run could be paid twice.",
        )
    if originator_problems:
        return _nacha(
            NACHA_NOT_CONFIGURED,
            "Set up the bank file in Settings → Payments first (company name, company "
            "ID and your bank's routing number).",
            missing=list(originator_problems),
        )
    if run_segregation_violated(run.initiated_by, actor_id, payment_config):
        return _segregation_for("export", "nacha_export")
    if run.requires_cfo_approval and run.cfo_approved_at is None:
        return _awaiting_cfo()
    if pending_payments <= 0:
        return _nacha(NACHA_NOTHING_TO_EXPORT, "This run has no payments left to export")
    return None


def run_nacha_void_refusal(run) -> ActionRefusal | None:
    """Why ``POST /runs/{id}/nacha/void`` ("the bank rejected the file") would
    refuse, or ``None`` — the ONE definition the endpoint raises from and the
    run dialog's ``actions.nacha_void`` is read from (issue #575). Only an
    ``exported`` run has a file to release."""
    if run.status != "exported":
        return ActionRefusal(
            NACHA_RUN_NOT_EXPORTED,
            f"Only an 'exported' run's bank file can be voided, not '{run.status}'",
            status.HTTP_409_CONFLICT,
        )
    return None


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
