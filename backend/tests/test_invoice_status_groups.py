"""The invoice status groupings have one owner (`app/models/invoice_status.py`).

Issue #575: "approved or beyond", "open AP", "pre-approval" and "post-approval
locked" were each restated as literal lists in several modules, and the copies
drifted — the budget's realised spend left out `sending_to_erp`, and three
fraud baselines left out `sending_to_erp`, `sent_to_erp` and `done`. Each copy
passed its own tests.

This file pins:

* the groups against `VALID_TRANSITIONS`, so a new state-machine edge that
  changes what a group should contain fails here instead of in production;
* the source tree, so a literal tuple/list/set of statuses equal to one of the
  groups anywhere under `app/` fails — import the named set instead. Two-member
  literals count too, since `SETTLED_STATUSES` (`paid`, `done`) is the set most
  often spelled out by hand; the same scan holds `PaymentStatus` literals to
  `payment_adapters/base.TERMINAL_STATUSES`;
* the single enum: `schemas.invoice.InvoiceStatus` IS the model's.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.models import invoice_status as groups
from app.models.invoice_status import (
    APPROVED_OR_BEYOND_STATUSES,
    FINANCIALLY_LOCKED_STATUSES,
    IN_REVIEW_STATUSES,
    OPEN_AP_STATUSES,
    PAYABLE_STATUSES,
    POST_APPROVAL_LOCKED_STATUSES,
    PRE_APPROVAL_STATUSES,
    SETTLED_STATUSES,
    InvoiceStatus,
    financially_locked,
    was_ever_approved,
)
from app.services.payment_adapters.base import TERMINAL_STATUSES as PAYMENT_TERMINAL_STATUSES
from app.services.workflow_engine import VALID_TRANSITIONS

APP = Path(__file__).resolve().parents[1] / "app"
OWNER = APP / "models" / "invoice_status.py"
S = InvoiceStatus


def _reachable(start: InvoiceStatus, *, stop: set[InvoiceStatus]) -> set[InvoiceStatus]:
    """Statuses reachable from `start`, not traversing INTO any of `stop`."""
    seen: set[InvoiceStatus] = set()
    frontier = [start]
    while frontier:
        cur = frontier.pop()
        if cur in seen or cur in stop:
            continue
        seen.add(cur)
        frontier.extend(VALID_TRANSITIONS.get(cur, set()))
    return seen


def test_one_enum():
    from app.models.invoice import InvoiceStatus as model_enum
    from app.schemas.invoice import InvoiceStatus as schema_enum

    assert schema_enum is model_enum is InvoiceStatus
    assert set(VALID_TRANSITIONS) == set(InvoiceStatus)


def test_groups_partition_the_statuses():
    """Every status is pre-approval, approved-or-beyond, or `failed` — exactly
    one of them. `failed` stays outside both because it is reachable from
    `pending` (never reviewed) and from `sending_to_erp` (approved)."""
    pre = set(PRE_APPROVAL_STATUSES)
    beyond = set(APPROVED_OR_BEYOND_STATUSES)
    assert pre.isdisjoint(beyond)
    assert pre | beyond | {S.failed} == set(InvoiceStatus)
    assert S.failed not in pre | beyond
    assert set(IN_REVIEW_STATUSES) == pre - {S.rejected}


#: The recall edge (decisions §374) takes an approval back to review; what
#: lies behind it reaches money only through a fresh approval, so the walks
#: from `approved` stop there.
_BEFORE_APPROVAL = set(PRE_APPROVAL_STATUSES)


def test_open_ap_is_what_approval_reaches_before_payment_or_close():
    assert set(OPEN_AP_STATUSES) == _reachable(
        S.approved, stop={S.paid, S.done, S.failed, *_BEFORE_APPROVAL}
    )


def test_approved_or_beyond_is_what_approval_reaches():
    assert set(APPROVED_OR_BEYOND_STATUSES) == _reachable(
        S.approved, stop={S.failed, *_BEFORE_APPROVAL}
    )


def test_approved_or_beyond_is_forward_closed():
    """Once in the set, no edge leaves it except into `failed`, or the recall
    of an approval back to review — so the set can never lose an invoice that
    is still on its way to being paid. A recalled invoice is not: it meets
    approval again (decisions §374)."""
    for status in APPROVED_OR_BEYOND_STATUSES:
        for nxt in VALID_TRANSITIONS[status]:
            recall = status is S.approved and nxt is S.ready_for_review
            assert nxt in APPROVED_OR_BEYOND_STATUSES or nxt is S.failed or recall, (status, nxt)


def test_the_only_edges_back_before_approval_are_the_recalls():
    """`approved` and `failed` reach `ready_for_review` only by the recall door
    (`review.recall_refusal`), and `failed → pending` is a re-extraction, which
    the engine refuses for an invoice that was ever approved
    (`workflow_engine._refuse_outside_the_recall`); no other approved-or-beyond
    status has an edge into the pre-approval statuses."""
    back = {
        (status, nxt)
        for status in (*APPROVED_OR_BEYOND_STATUSES, S.failed)
        for nxt in VALID_TRANSITIONS[status]
        if nxt in _BEFORE_APPROVAL
    }
    assert back == {
        (S.approved, S.ready_for_review),
        (S.failed, S.ready_for_review),
        (S.failed, S.pending),
    }


def test_settled_is_approved_or_beyond_past_open_ap():
    """Approval reaches open AP, then settles: `paid` (which a void can still
    take back to `approved`) or `done`, which nothing leaves."""
    assert set(SETTLED_STATUSES) == set(APPROVED_OR_BEYOND_STATUSES) - set(OPEN_AP_STATUSES)
    assert len(SETTLED_STATUSES) == len(set(SETTLED_STATUSES))
    assert VALID_TRANSITIONS[S.done] == set()


def test_payable_is_what_can_move_to_payment_scheduled():
    """A payment may be booked against an approved invoice that can step to
    `payment_scheduled` now, or one already there (a re-attempt)."""
    can_schedule = {
        s for s in APPROVED_OR_BEYOND_STATUSES if S.payment_scheduled in VALID_TRANSITIONS[s]
    }
    assert set(PAYABLE_STATUSES) == can_schedule | {S.payment_scheduled}
    assert set(PAYABLE_STATUSES) <= set(OPEN_AP_STATUSES)


def test_post_approval_locked_is_approved_or_beyond_minus_approved():
    assert POST_APPROVAL_LOCKED_STATUSES == set(APPROVED_OR_BEYOND_STATUSES) - {S.approved}


def test_financially_locked_is_post_approval_locked_plus_approved():
    assert FINANCIALLY_LOCKED_STATUSES == POST_APPROVAL_LOCKED_STATUSES | {S.approved}


def _row(status, *, approval_date=None, approved_by=None):
    return SimpleNamespace(status=status, approval_date=approval_date, approved_by=approved_by)


def test_financially_locked_is_the_set_plus_a_failed_push_after_approval():
    """`financially_locked` is written fail-closed (only pre-approval statuses
    and a never-approved `failed` are open); for every status that exists it
    must answer exactly "in FINANCIALLY_LOCKED_STATUSES, or `failed` and ever
    approved" (decisions §363, §370)."""
    stamps = {
        "never": {},
        "dated": {"approval_date": date(2026, 1, 1)},
        "named": {"approved_by": "Approver"},
        "blank-name": {"approved_by": ""},
    }
    for status in InvoiceStatus:
        for label, stamp in stamps.items():
            row = _row(status, **stamp)
            approved = was_ever_approved(row)
            assert approved is (label in {"dated", "named"}), label
            expected = status in FINANCIALLY_LOCKED_STATUSES or (status is S.failed and approved)
            assert financially_locked(row) is expected, (status, label)


def test_financially_locked_fails_closed_on_a_status_it_does_not_know():
    """A status added to the enum later is frozen until someone decides
    otherwise — the open side is the explicit list."""
    assert financially_locked(_row("some_future_status")) is True


def test_financially_locked_reads_the_wire_string_too():
    """`Invoice.status` is the StrEnum, but a raw string compares equal."""
    assert financially_locked(_row("new")) is False
    assert financially_locked(_row("failed", approved_by="Approver")) is True


def test_consumers_read_the_owner():
    from app.api import invoice_entry
    from app.api import invoices as api_invoices
    from app.api import payments as api_payments
    from app.api import vendor_statement_recon as api_statement_recon
    from app.services import analytics as svc_analytics
    from app.services import (
        ap_commitments,
        budget_service,
        csv_import,
        erp_status_sync,
        gl_recode,
        invoice_warnings,
        payment_reconciler,
        retention_sweep,
        vendor_merge,
    )
    from app.services.payment_adapters import base as payment_base

    assert api_invoices.IMMUTABLE_STATUSES is POST_APPROVAL_LOCKED_STATUSES
    # One financial-lock predicate, one approval read (decisions §370).
    assert invoice_entry.financially_locked is financially_locked
    assert invoice_entry.was_ever_approved is was_ever_approved
    assert gl_recode._IMMUTABLE_STATUSES is POST_APPROVAL_LOCKED_STATUSES
    assert invoice_warnings._PRE_APPROVAL_STATUSES is PRE_APPROVAL_STATUSES
    assert svc_analytics.OPEN_AP_STATUSES == tuple(s.value for s in OPEN_AP_STATUSES)
    assert ap_commitments.COMMITTED_STATUSES == svc_analytics.OPEN_AP_STATUSES
    assert set(budget_service.REALISED_INVOICE_STATUSES) == set(APPROVED_OR_BEYOND_STATUSES)
    assert api_statement_recon._LEDGER_EXCLUDED_STATUSES is SETTLED_STATUSES
    assert retention_sweep._ARCHIVABLE_INVOICE_STATES is SETTLED_STATUSES
    assert vendor_merge._MONEY_MOVED_STATUSES is SETTLED_STATUSES
    assert csv_import._HISTORICAL_INVOICE_STATUSES == {s.value for s in SETTLED_STATUSES}
    assert csv_import._IMPORTABLE_INVOICE_STATUSES == {"new", "rejected", "paid", "done"}
    assert set(svc_analytics.TOUCHLESS_REVIEW_EVIDENCE_STATUSES) == {"paid", "done", "failed"}
    assert invoice_warnings._ERP_SYNC_SKIPPED_STATUSES == ("paid", "done")
    assert erp_status_sync.PAYABLE_HERE == frozenset(PAYABLE_STATUSES)
    assert payment_reconciler.TERMINAL_STATUSES is payment_base.TERMINAL_STATUSES
    # Not yet repointed (see `_AWAITING_REPOINT`), but already the same set.
    assert api_payments.PAYABLE_INVOICE_STATUSES == tuple(s.value for s in PAYABLE_STATUSES)


# ---------------------------------------------------------------------------
# Source scan: no literal restatement of a group
# ---------------------------------------------------------------------------

_VALUES = {s.value for s in InvoiceStatus}
_GROUPS = {
    name: frozenset(s.value for s in getattr(groups, name))
    for name in (
        "IN_REVIEW_STATUSES",
        "PRE_APPROVAL_STATUSES",
        "OPEN_AP_STATUSES",
        "APPROVED_OR_BEYOND_STATUSES",
        "POST_APPROVAL_LOCKED_STATUSES",
        "FINANCIALLY_LOCKED_STATUSES",
        "SETTLED_STATUSES",
        "PAYABLE_STATUSES",
    )
}

#: Restatements a later change repoints at the named set; each entry is
#: `(path relative to backend/, group)`. Shrink only: an entry whose literal is
#: gone fails `test_awaiting_repoint_entries_are_still_live`.
_AWAITING_REPOINT = frozenset(
    {
        ("app/api/payments.py", "PAYABLE_STATUSES"),
        ("app/api/vendors.py", "SETTLED_STATUSES"),
        ("app/services/discount_offers.py", "SETTLED_STATUSES"),
    }
)

#: `PaymentStatus` members by name; matched only in attribute form, since
#: `"failed"` is an invoice status string too.
_PAYMENT_TERMINAL = frozenset(s.name for s in PAYMENT_TERMINAL_STATUSES)
_PAYMENT_OWNER = APP / "services" / "payment_adapters" / "base.py"


def _status_value(node: ast.expr) -> str | None:
    """`"approved"`, `InvoiceStatus.approved`, `X.approved.value` → `"approved"`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value in _VALUES else None
    if isinstance(node, ast.Attribute) and node.attr == "value":
        node = node.value
    if (
        isinstance(node, ast.Attribute)
        and node.attr in _VALUES
        and isinstance(node.value, ast.Name)
        and node.value.id.endswith("InvoiceStatus")
    ):
        return node.attr
    return None


def _payment_status_name(node: ast.expr) -> str | None:
    """`PaymentStatus.completed` → `"completed"`; anything else → None."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "PaymentStatus"
    ):
        return node.attr
    return None


def _restatements_in(tree: ast.AST) -> list[tuple[int, str]]:
    """`(lineno, group)` for every literal collection in `tree` equal to a group."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple | ast.List | ast.Set) or len(node.elts) < 2:
            continue
        values = [_status_value(e) for e in node.elts]
        if not any(v is None for v in values):
            for name, members in _GROUPS.items():
                if frozenset(values) == members:
                    hits.append((node.lineno, name))
        names = [_payment_status_name(e) for e in node.elts]
        if not any(n is None for n in names) and frozenset(names) == _PAYMENT_TERMINAL:
            hits.append((node.lineno, "payment_adapters.base.TERMINAL_STATUSES"))
    return hits


def _literal_restatements() -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in sorted(APP.rglob("*.py")):
        if path in (OWNER, _PAYMENT_OWNER):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        rel = str(path.relative_to(APP.parent))
        hits += [(rel, line, name) for line, name in _restatements_in(tree)]
    return hits


def test_no_module_restates_a_status_group():
    hits = [
        f"{rel}:{line} restates {name}"
        for rel, line, name in _literal_restatements()
        if (rel, name) not in _AWAITING_REPOINT
    ]
    assert hits == [], (
        "Import the named set from app/models/invoice_status.py (or "
        "payment_adapters/base.TERMINAL_STATUSES) instead of listing its "
        "members again (issue #575)."
    )


def test_awaiting_repoint_entries_are_still_live():
    """The allowance only shrinks: once a file imports the named set, its
    entry has to go too."""
    found = {(rel, name) for rel, _line, name in _literal_restatements()}
    assert _AWAITING_REPOINT <= found, sorted(_AWAITING_REPOINT - found)


def test_scan_catches_a_restatement(tmp_path):
    """The scan is not vacuous: a hand-listed copy of a group is found."""
    tree = ast.parse(
        'X = ("approved", "sending_to_erp", "sent_to_erp", "posted_in_erp", "payment_scheduled")'
    )
    node = tree.body[0].value
    assert frozenset(_status_value(e) for e in node.elts) == _GROUPS["OPEN_AP_STATUSES"]
    tree = ast.parse(
        "X = (InvoiceStatus.new, InvoiceStatus.pending.value, InvoiceStatus.ready_for_review)"
    )
    node = tree.body[0].value
    assert frozenset(_status_value(e) for e in node.elts) == _GROUPS["IN_REVIEW_STATUSES"]


def test_scan_catches_the_new_groups():
    """Two-member settled literals, in every spelling, the payable set, and a
    hand-listed copy of the payment rails' terminal set are all found; a pair
    that is not a group, and a mixed collection, are not."""
    src = "\n".join(
        [
            "A = (InvoiceStatus.paid, InvoiceStatus.done)",
            'B = frozenset({"done", "paid"})',
            "C = [InvoiceStatus.done.value, InvoiceStatus.paid.value]",
            "D = (InvoiceStatus.approved.value, InvoiceStatus.posted_in_erp.value,"
            " InvoiceStatus.payment_scheduled.value)",
            "E = (PaymentStatus.completed, PaymentStatus.failed, PaymentStatus.cancelled)",
            'F = ("paid", "rejected")',
            'G = ("paid", "done", other)',
            'H = ("completed", "failed", "cancelled")',
        ]
    )
    assert sorted(_restatements_in(ast.parse(src))) == [
        (1, "SETTLED_STATUSES"),
        (2, "SETTLED_STATUSES"),
        (3, "SETTLED_STATUSES"),
        (4, "PAYABLE_STATUSES"),
        (5, "payment_adapters.base.TERMINAL_STATUSES"),
    ]
