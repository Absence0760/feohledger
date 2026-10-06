"""Touchless (straight-through-processing) rate — the definition's guards.

One definition, shared by the dashboard KPI and the experiments readout: an
invoice is touchless when it reached approval-or-later with NO person
intervening — approved automatically (`invoice.auto_approved`), no human field
or line-item correction, no exception a person decided. The denominator is
every invoice that reached a review decision (cleared or rejected). The
per-invoice classification is SQL over the audit trail
(`services/touchless`); the population's status legs and the arithmetic are
pure (`services/analytics`).

This file pins what can be pinned without a database:

  * the arithmetic (`compute_touchless_rate`);
  * the status legs, re-derived from the state machine and the CSV importer —
    terminal status alone is not evidence of review (`new -> done` skips it;
    the importer plants `done`/`paid` rows the workflow engine never saw);
  * the audit actions the definition reads, re-derived from `app/` so a
    renamed action cannot silently drop out of the predicate.

The behaviour end-to-end — human vs automatic approval, corrections, exception
decisions, evidence-gated `done`/`paid`/`failed`, imported rows out of both
legs, and the dashboard and experiments agreeing — is pinned against Postgres
in `test_dashboard_aggregates.py` § 3–3c.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.dialects import postgresql

from app.services.analytics import (
    TOUCHLESS_BOUNCED_STATUSES,
    TOUCHLESS_CLEARED_STATUSES,
    TOUCHLESS_REVIEW_EVIDENCE_STATUSES,
    compute_touchless_rate,
)
from app.services.touchless import (
    AUTO_APPROVAL_ACTION,
    HUMAN_EXCEPTION_DECISION_ACTIONS,
    HUMAN_INVOICE_TOUCH_ACTIONS,
    touchless_clause,
    touchless_decided_clause,
)
from app.services.workflow_engine import VALID_TRANSITIONS

_APP = Path(__file__).resolve().parents[1] / "app"

# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def test_rate_is_touchless_over_decided_as_an_exact_decimal():
    assert compute_touchless_rate(touchless_count=49, decided_count=100) == Decimal("49.0")
    assert compute_touchless_rate(touchless_count=1, decided_count=3) == Decimal("33.3")
    # Half-up, like the experiments readout's other rates.
    assert compute_touchless_rate(touchless_count=1, decided_count=8) == Decimal("12.5")
    assert compute_touchless_rate(touchless_count=2, decided_count=3) == Decimal("66.7")
    assert isinstance(compute_touchless_rate(touchless_count=1, decided_count=2), Decimal)


def test_nothing_decided_is_zero_not_a_zero_division():
    assert compute_touchless_rate(touchless_count=0, decided_count=0) == Decimal("0.0")


def test_rate_stays_in_range_against_a_mismatched_pair():
    """The two figures come from one query, so the numerator cannot exceed the
    denominator — but a caller that sourced them separately must still get a
    rate in [0, 100], never the negative BUG 9 produced."""
    assert compute_touchless_rate(touchless_count=99, decided_count=10) == Decimal("100.0")
    assert compute_touchless_rate(touchless_count=-5, decided_count=10) == Decimal("0.0")
    assert compute_touchless_rate(touchless_count=3, decided_count=-1) == Decimal("0.0")


# ---------------------------------------------------------------------------
# The audit actions the definition reads must be ones the app writes
# ---------------------------------------------------------------------------


def test_every_action_the_definition_reads_is_written_somewhere_in_app():
    """A predicate keyed on an action nobody writes is silently inert: rename
    `invoice.line_items_edited` and every corrected invoice becomes touchless.
    Each action must appear as a string literal outside `services/touchless`."""
    source = "\n".join(p.read_text() for p in _APP.rglob("*.py") if p.name != "touchless.py")
    for action in (
        AUTO_APPROVAL_ACTION,
        *HUMAN_INVOICE_TOUCH_ACTIONS,
        *HUMAN_EXCEPTION_DECISION_ACTIONS,
    ):
        assert f'"{action}"' in source, f"{action!r} is read but never written"


def test_the_only_automatic_approval_action_is_not_a_human_one():
    assert AUTO_APPROVAL_ACTION not in HUMAN_INVOICE_TOUCH_ACTIONS
    assert "invoice.approved" in HUMAN_INVOICE_TOUCH_ACTIONS
    assert "invoice.rejected" in HUMAN_INVOICE_TOUCH_ACTIONS


def test_only_the_two_auto_approve_paths_write_the_automatic_action():
    """`invoice.auto_approved` is the evidence that NOBODY approved. If a third
    writer appears it must be reviewed against that meaning (a person-driven
    path writing it would inflate the rate) and added here deliberately."""
    writers = sorted(
        str(p.relative_to(_APP))
        for p in _APP.rglob("*.py")
        if p.name != "touchless.py"
        and re.search(r'["\']invoice\.auto_approved["\']', p.read_text())
        and "action_name=" in p.read_text()
    )
    assert writers == ["api/workflow.py", "services/extraction.py"], writers


def test_imported_rows_are_outside_both_legs_by_construction():
    """Both predicates carry the native-provenance clause, so a CSV-imported
    row can be in neither the numerator nor the denominator."""
    for clause in (touchless_clause(), touchless_decided_clause()):
        sql = str(clause.compile(dialect=postgresql.dialect()))
        assert "NOT (invoices.meta IS NOT NULL" in sql, sql


# ---------------------------------------------------------------------------
# Structural guards — the status legs have to keep saying what they claim
# ---------------------------------------------------------------------------


def test_the_three_legs_are_disjoint():
    legs = [
        set(TOUCHLESS_CLEARED_STATUSES),
        set(TOUCHLESS_REVIEW_EVIDENCE_STATUSES),
        set(TOUCHLESS_BOUNCED_STATUSES),
    ]
    for i, a in enumerate(legs):
        for b in legs[i + 1 :]:
            assert not (a & b)


def test_no_pre_review_status_has_an_edge_into_the_unconditionally_cleared_set():
    """The comment above `TOUCHLESS_CLEARED_STATUSES` claims those statuses are
    proof of a cleared review. Re-derive that from the state machine rather
    than trusting the comment.

    `approved` is the set's single entry point — every writer of it
    (`services/review`, `api/workflow`'s below-threshold auto-approve,
    `services/extraction`'s auto-approve) stamps `Invoice.approval_date`, and
    a CSV import is explicitly forbidden from landing there. So no status that
    sits BEFORE review (`new`, `pending`, `ready_for_review`, `rejected`) may
    have an edge into the set other than into `approved` itself; such an edge
    would be a way in that never passed approval, and its target would belong
    in the evidence-gated set instead.

    Sources that are themselves evidence-gated are skipped: `failed ->
    sending_to_erp` is the ERP-export retry, which presupposes the approval
    that got the invoice to `sending_to_erp` in the first place — a runtime
    property the graph cannot express, and exactly why `failed` needs the
    stamp rather than a graph rule.
    """
    cleared = set(TOUCHLESS_CLEARED_STATUSES)
    assert "approved" in cleared
    exempt_sources = cleared | set(TOUCHLESS_REVIEW_EVIDENCE_STATUSES)
    checked = 0
    for source, targets in VALID_TRANSITIONS.items():
        source_name = source.value if hasattr(source, "value") else str(source)
        if source_name in exempt_sources:
            continue
        checked += 1
        for target in targets:
            target_name = target.value if hasattr(target, "value") else str(target)
            if target_name == "approved":
                # The auto-approve edges (`new`/`pending` -> `approved`) ARE a
                # cleared review — that is precisely what touchless means.
                continue
            assert target_name not in cleared, (
                f"{source_name} -> {target_name} reaches a status listed as "
                "unconditionally cleared without passing through approval"
            )
    assert checked, "the pre-review statuses vanished from VALID_TRANSITIONS"


def test_every_status_the_csv_importer_can_plant_is_handled():
    """A CSV import bypasses the workflow engine entirely, so any status it can
    land at is un-evidenced by construction. Each must be either
    evidence-gated or outside the metric — never unconditionally cleared."""
    from app.services.csv_import import _IMPORTABLE_INVOICE_STATUSES

    for status in _IMPORTABLE_INVOICE_STATUSES:
        assert status not in TOUCHLESS_CLEARED_STATUSES, (
            f"{status!r} is CSV-importable, so status alone cannot prove it cleared review"
        )


def test_the_importer_marks_every_invoice_row_it_creates():
    """The metric's exclusion is only as good as the marker. Guard the two
    halves of the contract that live in `services/csv_import`: the reserved
    key, and the fact that the marker records WHAT wrote it and WHEN.
    """
    from app.services.csv_import import (
        IMPORT_PROVENANCE_KEY,
        IMPORT_PROVENANCE_SOURCE,
        build_import_provenance,
    )

    assert IMPORT_PROVENANCE_KEY == "imported"
    # Must not collide with the other tenants of `Invoice.meta`.
    assert IMPORT_PROVENANCE_KEY not in {"audit_summary", "archived_at"}
    marker = build_import_provenance()
    assert marker["source"] == IMPORT_PROVENANCE_SOURCE
    # An ISO-8601 instant, timezone-aware (UTC), round-trippable.
    parsed = datetime.fromisoformat(marker["at"])
    assert parsed.tzinfo is not None
