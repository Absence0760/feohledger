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
  groups anywhere under `app/` fails — import the named set instead;
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
    POST_APPROVAL_LOCKED_STATUSES,
    PRE_APPROVAL_STATUSES,
    InvoiceStatus,
    financially_locked,
    was_ever_approved,
)
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
    (`review.recall_refusal`), and `failed → pending` is the re-extraction
    `/extract` refuses once an invoice was approved; no other approved-or-beyond
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
    from app.api import analytics as api_analytics
    from app.api import invoice_entry
    from app.api import invoices as api_invoices
    from app.services import analytics as svc_analytics
    from app.services import budget_service, gl_recode, invoice_warnings

    assert api_invoices.IMMUTABLE_STATUSES is POST_APPROVAL_LOCKED_STATUSES
    # One financial-lock predicate, one approval read (decisions §370).
    assert invoice_entry.financially_locked is financially_locked
    assert invoice_entry.was_ever_approved is was_ever_approved
    assert gl_recode._IMMUTABLE_STATUSES is POST_APPROVAL_LOCKED_STATUSES
    assert invoice_warnings._PRE_APPROVAL_STATUSES is PRE_APPROVAL_STATUSES
    assert svc_analytics.OPEN_AP_STATUSES == tuple(s.value for s in OPEN_AP_STATUSES)
    assert api_analytics._COMMITTED_STATUSES == svc_analytics.OPEN_AP_STATUSES
    assert set(budget_service.REALISED_INVOICE_STATUSES) == set(APPROVED_OR_BEYOND_STATUSES)


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
    )
}


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


def _literal_restatements() -> list[str]:
    hits: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if path == OWNER:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Tuple | ast.List | ast.Set) or len(node.elts) < 3:
                continue
            values = [_status_value(e) for e in node.elts]
            if any(v is None for v in values):
                continue
            for name, members in _GROUPS.items():
                if frozenset(values) == members:
                    rel = path.relative_to(APP.parent)
                    hits.append(f"{rel}:{node.lineno} restates {name}")
    return hits


def test_no_module_restates_a_status_group():
    assert _literal_restatements() == [], (
        "Import the named set from app/models/invoice_status.py instead of "
        "listing its members again (issue #575)."
    )


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
