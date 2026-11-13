"""The gates for expense-report decisions and corporate-card reconciliation —
each the ONE definition its endpoint raises from and its list row's verdict is
read from (issue #575 § One rule, one place; ``docs/decisions.md`` §305).

Every gate returns an :class:`~app.api.refusals.ActionRefusal` or ``None``. The
endpoint raises ``refusal.http()``, whose ``detail`` is the coded
``{code, message, params}`` (every gate here is ``coded=True``; the English
``message`` is the sentence the endpoint answered with as a plain string before
``docs/decisions.md`` §361, so a client that reads it as text still shows it).
``api/expenses.report_action_refusals`` and
``api/expense_cards.card_txn_action_refusals`` ship ``refusal.verdict()`` on the
row, after the endpoint's own role predicate.

The order inside each composite gate is the endpoint's order, so a row that
fails two rules shows the refusal the endpoint would answer with first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException, status

from app.api.refusals import ActionRefusal, coded_refusal
from app.models.expense import (
    CorporateCardTransaction,
    ExpenseReport,
    ExpenseReportStatus,
    ReconciliationStatus,
)

# ── Expense reports ─────────────────────────────────────────────────────────

#: Only the owner may compose or submit a report (`_require_report_owner`).
EXPENSE_REPORT_NOT_OWNER = "expense_report_not_owner"
#: Submit / attach / detach on a report that has left `draft`.
EXPENSE_REPORT_NOT_DRAFT = "expense_report_not_draft"
#: Approve / reject on a report that is not `submitted`.
EXPENSE_REPORT_NOT_SUBMITTED = "expense_report_not_submitted"
#: The report's owner deciding to reject their own report.
EXPENSE_REPORT_SELF_REJECT = "expense_report_self_reject"
#: The report CFO gate — the report-side sibling of
#: `services/review.APPROVAL_CFO_REQUIRED`.
EXPENSE_CFO_REQUIRED = "expense_cfo_required"

#: Editing, deleting or moving a line on a report whose total is locked in for
#: an approval decision (`report_unlocked_refusal`, issue #155).
EXPENSE_REPORT_LOCKED = "expense_report_locked"
#: Attaching a line in another currency with no FX rate source configured.
EXPENSE_FX_SOURCE_MISSING = "expense_fx_source_missing"
#: Attaching a line the FX source has no usable rate for
#: (`expense_currency.ExpenseConversionError`).
EXPENSE_FX_RATE_UNAVAILABLE = "expense_fx_rate_unavailable"
#: Deleting an expense a card transaction is reconciled to.
EXPENSE_CARD_RECONCILED = "expense_card_reconciled"
#: Submit refused for BLOCKING policy violations. The 422's `detail` keeps its
#: `violations` list beside the coded keys, each entry itself coded by the
#: policy engine (`expense_policy.VIOLATION_PARAMS`; `docs/decisions.md` §378).
EXPENSE_REPORT_POLICY_BLOCKED = "expense_report_policy_blocked"
#: Submit refused because attached lines have no rate locked into the report
#: currency (`ReportRollup.unconverted_ids`); `detail.expense_ids` is kept.
EXPENSE_REPORT_LINES_UNCONVERTED = "expense_report_lines_unconverted"

#: A report in one of these states has its total locked in for an approval
#: decision: `submitted` / `pending_approval` (awaiting it) and `approved` /
#: `reimbursed` (signed against it). `draft` is freely editable; `rejected` /
#: `cancelled` are terminal and their lines may be corrected and re-reported.
LOCKED_REPORT_STATUSES = frozenset(
    {
        ExpenseReportStatus.submitted,
        ExpenseReportStatus.pending_approval,
        ExpenseReportStatus.approved,
        ExpenseReportStatus.reimbursed,
    }
)

#: Platform default CFO threshold; per-org override in
#: `Organization.settings.expense_approval.cfo_threshold`, denominated in the
#: org REPORTING currency.
DEFAULT_CFO_THRESHOLD = Decimal("5000")


def report_policy_blocked_detail(violations: list[dict]) -> dict:
    """The submit 422's `detail` for blocking policy violations: the coded
    `{code, message, params}` plus the `violations` list, whose key and entries
    are unchanged for a client that reads them (§378). `params.count` is the
    number of blocking violations."""
    return {
        **coded_refusal(
            EXPENSE_REPORT_POLICY_BLOCKED,
            "Report has blocking policy violations and cannot be submitted.",
            count=len(violations),
        ),
        "violations": violations,
    }


def report_lines_unconverted_detail(
    count: int, expense_ids: list[str], report_currency: str
) -> dict:
    """The submit 422's `detail` for lines with no locked rate: the coded
    `{code, message, params}` (`count`, `currency` — the report's) plus the
    `expense_ids` list, kept as it was (§378). `count` is the rollup's
    `unconverted_count`, never the id list's length: a line with no id still
    counts (`expense_currency.rollup_report_lines`)."""
    return {
        **coded_refusal(
            EXPENSE_REPORT_LINES_UNCONVERTED,
            f"{count} expense(s) have no exchange rate locked into the report currency "
            f"{report_currency} and cannot be totalled. Re-attach them to lock a rate.",
            count=count,
            currency=report_currency,
        ),
        "expense_ids": list(expense_ids),
    }


def report_owner_refusal(report: ExpenseReport, actor_id: uuid.UUID) -> ActionRefusal | None:
    """Only a report's owner (``employee_user_id``) may compose, edit or submit
    it — report SoD names exactly one implicated actor, so nobody else may
    author what the approver signs (``api/expenses._require_report_owner``)."""
    if report.employee_user_id != actor_id:
        return ActionRefusal(
            EXPENSE_REPORT_NOT_OWNER,
            "Only the report's owner can change its lines or submit it.",
            status.HTTP_403_FORBIDDEN,
            coded=True,
        )
    return None


def _not_draft(report: ExpenseReport, message: str, http_status: int) -> ActionRefusal | None:
    if report.status != ExpenseReportStatus.draft:
        return ActionRefusal(
            EXPENSE_REPORT_NOT_DRAFT,
            message,
            http_status,
            params={"status": str(report.status)},
            coded=True,
        )
    return None


def report_submit_refusal(report: ExpenseReport, actor_id: uuid.UUID) -> ActionRefusal | None:
    """Why ``POST /expense-reports/{id}/submit`` would refuse before it rolls
    the lines up: not the owner (403), then not a draft (422). The blocking
    policy violations and an unconvertible line are the submit's own 422s —
    they need the policy engine and the rollup, and come back as a list."""
    return report_owner_refusal(report, actor_id) or _not_draft(
        report,
        f"Cannot submit a report in '{report.status}' state",
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


def report_draft_refusal(report: ExpenseReport) -> ActionRefusal | None:
    """A report can only be *built up* while it is a draft; one already
    submitted / approved / terminal never accepts new lines (409, issue #155).
    The attach target's gate, and a PATCH moving a line onto a report."""
    return _not_draft(
        report,
        f"Cannot modify a report in '{report.status}' state; it has left draft.",
        status.HTTP_409_CONFLICT,
    )


def report_unlocked_refusal(report: ExpenseReport) -> ActionRefusal | None:
    """A line on a report whose total is locked (:data:`LOCKED_REPORT_STATUSES`)
    cannot be edited, deleted or moved off it (409): that would silently bypass
    the CFO gate and stale-sign the approval (issue #155)."""
    if report.status in LOCKED_REPORT_STATUSES:
        return ActionRefusal(
            EXPENSE_REPORT_LOCKED,
            f"Cannot modify a report in '{report.status}' state; its total is locked.",
            status.HTTP_409_CONFLICT,
            params={"status": str(report.status)},
            coded=True,
        )
    return None


def report_compose_refusal(report: ExpenseReport, actor_id: uuid.UUID) -> ActionRefusal | None:
    """Why ``POST /expense-reports/{id}/expenses`` (attach / detach) would
    refuse on the TARGET report: not a draft (409), then not the owner (403).
    A line moved off another report is gated on that report too, by the line
    chosen."""
    return report_draft_refusal(report) or report_owner_refusal(report, actor_id)


def _not_submitted(report: ExpenseReport, verb: str) -> ActionRefusal | None:
    if report.status != ExpenseReportStatus.submitted:
        return ActionRefusal(
            EXPENSE_REPORT_NOT_SUBMITTED,
            f"Cannot {verb} a report in '{report.status}' state",
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            params={"status": str(report.status)},
            coded=True,
        )
    return None


def _segregation_subject(report: ExpenseReport) -> SimpleNamespace:
    """The report as the shared segregation rule reads an invoice."""
    return SimpleNamespace(
        uploaded_by_id=report.employee_user_id,
        # No second implicated actor exists to name: only the owner may
        # compose, edit or submit a report (`report_owner_refusal`), so the
        # owner IS the complete author set. Stated rather than left absent: a
        # missing attribute on a fraud control reads as an oversight, and
        # `violates_segregation`'s getattr default would silently supply it.
        segregation_actor_ids=None,
    )


def report_segregation_refusal(report: ExpenseReport, actor_id: uuid.UUID) -> ActionRefusal | None:
    """Approver ≠ the report's owner, through the shared invoice helper so the
    rule and its coded 403 stay one (``approval_chain.check_segregation``)."""
    from app.services.approval_chain import check_segregation

    try:
        check_segregation(_segregation_subject(report), actor_id, {"require_segregation": True})
    except HTTPException as exc:
        detail = exc.detail
        return ActionRefusal(
            detail["code"],
            detail["message"],
            exc.status_code,
            params=detail.get("params") or None,
            coded=True,
        )
    return None


@dataclass(frozen=True)
class CfoGate:
    """The report CFO gate's answer: the refusal (or ``None``) plus the exact
    figure it compared and its currency, which the approval's audit row
    records so an auditor can replay the decision."""

    refusal: ActionRefusal | None
    gate_total: Decimal | None
    reporting_currency: str


def report_cfo_gate(report: ExpenseReport, role_names, org_settings: dict | None) -> CfoGate:
    """Above ``settings.expense_approval.cfo_threshold`` (default 5000) only
    ``cfo`` / ``admin`` may approve.

    The threshold is a bare number in the ORG REPORTING currency, so the
    comparison uses the total locked into that currency at submit (issue #157)
    — without it a 4 900 EUR report slips under a 5 000 USD threshold. A total
    that cannot be expressed in it (``None``) fails CLOSED: the gate applies.
    ``cfo_gate_applies`` is the shared fail-closed parse, so a malformed
    threshold demands CFO / admin rather than raising.
    """
    from app.api.deps import ROLE_ADMIN, ROLE_CFO
    from app.schemas.money import json_money_string
    from app.services.approval_chain import _to_decimal, cfo_gate_applies
    from app.services.currency_conversion import resolve_reporting_currency
    from app.services.expense_currency import normalize_currency, report_amount_for_gate

    expense_cfg = (org_settings or {}).get("expense_approval") or {}
    cfo_threshold_raw = expense_cfg.get("cfo_threshold", DEFAULT_CFO_THRESHOLD)
    reporting_currency = resolve_reporting_currency(org_settings)
    gate_total = report_amount_for_gate(report, reporting_currency=reporting_currency)
    gate_applies = gate_total is None or cfo_gate_applies(cfo_threshold_raw, gate_total)
    held = set(role_names)
    if not gate_applies or ROLE_CFO in held or ROLE_ADMIN in held:
        return CfoGate(None, gate_total, reporting_currency)

    threshold_dec = _to_decimal(cfo_threshold_raw)
    if threshold_dec is not None and not threshold_dec.is_finite():
        threshold_dec = None
    limit = f"{threshold_dec}" if threshold_dec is not None else "the configured limit"
    report_currency = normalize_currency(report.currency)
    if gate_total is None:
        message = (
            f"Report total cannot be expressed in {reporting_currency} "
            f"(no rate from {report_currency}), so it cannot be "
            f"cleared against the {limit} limit. CFO approval required."
        )
    else:
        message = (
            f"Report total {gate_total} {reporting_currency} exceeds {limit}. "
            "CFO approval required."
        )
    # Every figure is an exact string beside its currency. `amount` is None
    # exactly when the total could not be expressed in the reporting currency,
    # `limit` when the threshold itself is malformed.
    refusal = ActionRefusal(
        EXPENSE_CFO_REQUIRED,
        message,
        status.HTTP_403_FORBIDDEN,
        params={
            "amount": json_money_string(gate_total),
            "currency": reporting_currency,
            "limit": json_money_string(threshold_dec),
            "report_currency": report_currency,
        },
        coded=True,
    )
    return CfoGate(refusal, gate_total, reporting_currency)


def report_approve_check(
    report: ExpenseReport, actor_id: uuid.UUID, role_names, org_settings: dict | None
) -> tuple[ActionRefusal | None, CfoGate | None]:
    """Why ``POST /expense-reports/{id}/approve`` would refuse: not submitted
    (422), the owner approving their own report (403 ``approval_segregation``),
    then the CFO gate (403 ``expense_cfo_required``).

    Returns the refusal and, once the CFO gate has run, its :class:`CfoGate`
    — the endpoint records that figure on the audit row, so the gate runs once
    per approval (it logs on a malformed threshold or an unpriceable total)."""
    early = _not_submitted(report, "approve") or report_segregation_refusal(report, actor_id)
    if early is not None:
        return early, None
    cfo = report_cfo_gate(report, role_names, org_settings)
    return cfo.refusal, cfo


def report_approve_refusal(
    report: ExpenseReport, actor_id: uuid.UUID, role_names, org_settings: dict | None
) -> ActionRefusal | None:
    """The refusal half of :func:`report_approve_check` — the verdict's view."""
    return report_approve_check(report, actor_id, role_names, org_settings)[0]


def report_reject_refusal(report: ExpenseReport, actor_id: uuid.UUID) -> ActionRefusal | None:
    """Why ``POST /expense-reports/{id}/reject`` would refuse: not submitted
    (422), then the owner deciding their own report (403
    ``approval_segregation``).

    The decider is never the submitting employee, for reject as for approve —
    the rule ``docs/authentication.md`` states and the pre-approval reject
    already enforced. The endpoint used to skip it, so an owner holding a
    manager role could "reject" their own submitted report: an
    ``expense_report.rejected`` row naming the claimant as the reviewer, and a
    back door that unlocked a submitted report's lines for editing. Only the
    page stopped it, by hiding the button.

    Its own code and sentence (``expense_report_self_reject``): the shared
    ``approval_segregation`` sentence names an invoice and an approval.
    """
    from app.services.approval_chain import violates_segregation

    refusal = _not_submitted(report, "reject")
    if refusal is not None:
        return refusal
    if violates_segregation(_segregation_subject(report), actor_id, {"require_segregation": True}):
        return ActionRefusal(
            EXPENSE_REPORT_SELF_REJECT,
            "Segregation of duties: you cannot reject your own expense report. "
            "Another reviewer must decide it.",
            status.HTTP_403_FORBIDDEN,
            coded=True,
        )
    return None


# ── Corporate-card reconciliation ───────────────────────────────────────────

CARD_TXN_ALREADY_MATCHED = "card_txn_already_matched"
CARD_TXN_NOT_MATCHED = "card_txn_not_matched"
CARD_TXN_MATCHED_NOT_IGNORABLE = "card_txn_matched_not_ignorable"
CARD_TXN_ALREADY_IGNORED = "card_txn_already_ignored"
#: `/match`'s expense-side refusals — they depend on the expense chosen, so
#: they are not in the row's verdict (§348).
CARD_TXN_EXPENSE_ALREADY_MATCHED = "card_txn_expense_already_matched"
CARD_TXN_CURRENCY_MISMATCH = "card_txn_currency_mismatch"


def _is_matched(txn: CorporateCardTransaction) -> bool:
    return (
        txn.matched_expense_id is not None
        or txn.reconciliation_status == ReconciliationStatus.matched
    )


def card_txn_link_refusal(txn: CorporateCardTransaction) -> ActionRefusal | None:
    """Why ``POST /{id}/match`` or ``/create-expense`` would refuse on the
    transaction's side: it is already matched (409). An ``ignored``
    transaction may still be matched or turned into an expense — that is how
    an ignore is undone. Match's expense-side refusals (that expense already
    matched, a currency mismatch) depend on the expense chosen."""
    if _is_matched(txn):
        return ActionRefusal(
            CARD_TXN_ALREADY_MATCHED,
            "Transaction is already matched",
            status.HTTP_409_CONFLICT,
            coded=True,
        )
    return None


def card_txn_unmatch_refusal(txn: CorporateCardTransaction) -> ActionRefusal | None:
    """Why ``POST /{id}/unmatch`` would refuse: the transaction is not matched."""
    if txn.reconciliation_status != ReconciliationStatus.matched:
        return ActionRefusal(
            CARD_TXN_NOT_MATCHED, "Transaction is not matched", status.HTTP_409_CONFLICT, coded=True
        )
    return None


def card_txn_ignore_refusal(txn: CorporateCardTransaction) -> ActionRefusal | None:
    """Why ``POST /{id}/ignore`` would refuse: the transaction is matched
    (unmatch it first — ignoring it used to strand both FK legs), or it is
    already ignored (a repeat ignore used to succeed and write a second
    ``card_txn.ignored`` audit row for nothing)."""
    if _is_matched(txn):
        return ActionRefusal(
            CARD_TXN_MATCHED_NOT_IGNORABLE,
            "Transaction is matched — unmatch it before ignoring.",
            status.HTTP_409_CONFLICT,
            coded=True,
        )
    if txn.reconciliation_status == ReconciliationStatus.ignored:
        return ActionRefusal(
            CARD_TXN_ALREADY_IGNORED,
            "Transaction is already ignored",
            status.HTTP_409_CONFLICT,
            coded=True,
        )
    return None


def card_match_expense_refusal(txn: CorporateCardTransaction, expense) -> ActionRefusal | None:
    """Why ``POST /{id}/match`` would refuse on the chosen EXPENSE's side: it is
    already reconciled to a card line (409), or it is in another currency
    (409) — a EUR 100.00 expense is not the same money as a USD 100.00 card
    line, and linking them stamps a ``payment_method`` onto an expense the card
    never paid. Re-checked here, not just filtered out of ``suggest_matches``:
    the client sends an arbitrary ``expense_id``."""
    if expense.card_transaction_id is not None:
        return ActionRefusal(
            CARD_TXN_EXPENSE_ALREADY_MATCHED,
            "Expense is already matched",
            status.HTTP_409_CONFLICT,
            coded=True,
        )
    if (expense.currency or "").upper() != (txn.currency or "").upper():
        return ActionRefusal(
            CARD_TXN_CURRENCY_MISMATCH,
            f"Currency mismatch: transaction is {txn.currency}, expense is {expense.currency}.",
            status.HTTP_409_CONFLICT,
            params={"transaction_currency": txn.currency, "expense_currency": expense.currency},
            coded=True,
        )
    return None
