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
from pathlib import Path

from app.models import invoice_status as groups
from app.models.invoice_status import (
    APPROVED_OR_BEYOND_STATUSES,
    IN_REVIEW_STATUSES,
    OPEN_AP_STATUSES,
    POST_APPROVAL_LOCKED_STATUSES,
    PRE_APPROVAL_STATUSES,
    InvoiceStatus,
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


def test_open_ap_is_what_approval_reaches_before_payment_or_close():
    assert set(OPEN_AP_STATUSES) == _reachable(S.approved, stop={S.paid, S.done, S.failed})


def test_approved_or_beyond_is_what_approval_reaches():
    assert set(APPROVED_OR_BEYOND_STATUSES) == _reachable(S.approved, stop={S.failed})


def test_approved_or_beyond_is_forward_closed():
    """Once in the set, no edge leaves it except into `failed` — so the set can
    never lose an invoice that is still on its way to being paid."""
    for status in APPROVED_OR_BEYOND_STATUSES:
        for nxt in VALID_TRANSITIONS[status]:
            assert nxt in APPROVED_OR_BEYOND_STATUSES or nxt is S.failed, (status, nxt)


def test_post_approval_locked_is_approved_or_beyond_minus_approved():
    assert POST_APPROVAL_LOCKED_STATUSES == set(APPROVED_OR_BEYOND_STATUSES) - {S.approved}


def test_consumers_read_the_owner():
    from app.api import analytics as api_analytics
    from app.api import invoices as api_invoices
    from app.services import analytics as svc_analytics
    from app.services import budget_service, gl_recode, invoice_warnings

    assert api_invoices.IMMUTABLE_STATUSES is POST_APPROVAL_LOCKED_STATUSES
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
