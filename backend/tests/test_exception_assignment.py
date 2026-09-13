"""Tests for the exception-queue improvements:
  - SLA + auto-assignment baked in at creation (`_ensure_exception`)
  - `exception_lifecycle.apply_resolution` computes
    time_to_resolution_seconds in terminal states only
  - Response shape (`_exception_dict`) carries SLA + overdue flags

End-to-end coverage of the new endpoints (assign, bulk/resolve) lives
in the e2e suite — these tests pin the pure-Python edges so a logic
regression doesn't have to wait for the Playwright run.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# ---------- _ensure_exception: SLA + auto-assign --------------------------


def _invoice():
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),  # multi-entity P2: exception inherits invoice entity
        vendor_id=None,
    )


def _capture_db():
    """An AsyncMock session that captures `db.add(...)` calls so tests
    can inspect what got persisted."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=0)))
    db.add = MagicMock()
    return db


def _added_exception(db):
    """The `Exception` row `_ensure_exception` persisted.

    Creating an exception now adds TWO rows in one call — the exception itself
    and its append-only `exception.raised` audit row (see
    services/exception_lifecycle) — so a test must name which one it means
    rather than reading whichever `db.add` happened to be last."""
    from app.models.exception import Exception as APException

    rows = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], APException)]
    assert len(rows) == 1, f"expected exactly one persisted Exception, got {len(rows)}"
    return rows[0]


def test_ensure_exception_writes_due_at_when_org_sla_set():
    from app.services.invoice_warnings import _ensure_exception

    inv = _invoice()
    db = _capture_db()
    org_settings = {"exceptions": {"default_sla_hours": 4}}

    asyncio.run(_ensure_exception(db, inv, "fraud_flag", "warning", "x", org_settings=org_settings))

    persisted = _added_exception(db)
    assert persisted.due_at is not None
    # Within ~5s of (now + 4h) — wall-clock tolerance.
    delta = persisted.due_at - datetime.now(UTC)
    assert timedelta(hours=3, minutes=59) < delta < timedelta(hours=4, minutes=1)


def test_ensure_exception_per_type_sla_overrides_default():
    from app.services.invoice_warnings import _ensure_exception

    inv = _invoice()
    db = _capture_db()
    org_settings = {
        "exceptions": {
            "default_sla_hours": 24,
            "sla_hours_by_type": {"fraud_flag": 2},
        }
    }
    asyncio.run(_ensure_exception(db, inv, "fraud_flag", "error", "x", org_settings=org_settings))

    persisted = _added_exception(db)
    delta = persisted.due_at - datetime.now(UTC)
    assert timedelta(hours=1, minutes=59) < delta < timedelta(hours=2, minutes=1)


def test_ensure_exception_no_sla_leaves_due_at_null():
    """Without org config, SLA is opt-in — leave due_at null so the UI
    doesn't render a meaningless 'overdue' badge."""
    from app.services.invoice_warnings import _ensure_exception

    inv = _invoice()
    db = _capture_db()
    asyncio.run(_ensure_exception(db, inv, "duplicate", "warning", "x"))

    persisted = _added_exception(db)
    assert persisted.due_at is None


def test_ensure_exception_auto_assigns_user_when_routing_set():
    from app.services.invoice_warnings import _ensure_exception

    inv = _invoice()
    db = _capture_db()
    target = uuid.uuid4()
    org_settings = {"exceptions": {"auto_assign_by_type": {"fraud_flag": str(target)}}}
    asyncio.run(_ensure_exception(db, inv, "fraud_flag", "warning", "x", org_settings=org_settings))

    persisted = _added_exception(db)
    assert persisted.assigned_to_user_id == target


def test_ensure_exception_skips_invalid_assignee_uuid():
    """Misconfigured org settings shouldn't break exception creation —
    log + leave assignee unset."""
    from app.services.invoice_warnings import _ensure_exception

    inv = _invoice()
    db = _capture_db()
    org_settings = {"exceptions": {"auto_assign_by_type": {"fraud_flag": "not-a-uuid"}}}

    asyncio.run(_ensure_exception(db, inv, "fraud_flag", "warning", "x", org_settings=org_settings))
    persisted = _added_exception(db)
    assert persisted.assigned_to_user_id is None


def test_ensure_exception_no_op_when_already_open():
    """The dedup check at the top of _ensure_exception should fire if
    an open exception of the same type already exists."""
    from app.services.invoice_warnings import _ensure_exception

    inv = _invoice()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=1)))
    db.add = MagicMock()

    asyncio.run(_ensure_exception(db, inv, "fraud_flag", "warning", "x"))
    db.add.assert_not_called()


# ---------- apply_resolution: time-to-resolution computation --------------


def _exception(*, status="open", created_at=None):
    """Stand-in with the columns apply_resolution touches."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        created_at=created_at or (datetime.now(UTC) - timedelta(hours=3)),
        resolution=None,
        resolved_by=None,
        resolved_at=None,
        time_to_resolution_seconds=None,
    )


def test_apply_resolution_resolved_writes_time_to_resolution():
    from app.services.exception_lifecycle import apply_resolution

    exc = _exception(created_at=datetime.now(UTC) - timedelta(hours=2, minutes=30))
    apply_resolution(exc, "resolve", "rule tuned", "Demo Admin")

    assert exc.status == "resolved"
    assert exc.resolved_by == "Demo Admin"
    assert exc.resolved_at is not None
    assert 8990 < exc.time_to_resolution_seconds < 9010  # ~2h30m


def test_apply_resolution_dismissed_writes_time_to_resolution():
    """Dismiss is a terminal state too — the SLA clock stops here."""
    from app.services.exception_lifecycle import apply_resolution

    exc = _exception(created_at=datetime.now(UTC) - timedelta(hours=1))
    apply_resolution(exc, "dismiss", "false positive", "Demo Admin")
    assert exc.status == "dismissed"
    assert exc.time_to_resolution_seconds is not None


def test_apply_resolution_escalate_does_not_close_sla_clock():
    """Escalation is intermediate — the original SLA still applies
    once a downstream resolver acts. Don't burn the
    time_to_resolution slot, and don't claim the row was resolved:
    a still-open exception advertising a resolver + a resolution
    timestamp misleads an auditor. Who escalated and when lives on
    the immutable `exception.escalated` audit row instead."""
    from app.services.exception_lifecycle import apply_resolution

    exc = _exception(created_at=datetime.now(UTC) - timedelta(hours=1))
    apply_resolution(exc, "escalate", "needs CFO", "Demo Manager")

    assert exc.status == "escalated"
    assert exc.time_to_resolution_seconds is None
    # The reason IS kept — the next human reads it in the queue.
    assert exc.resolution == "needs CFO"
    assert exc.resolved_by is None
    assert exc.resolved_at is None


def test_apply_resolution_rejects_unknown_action():
    """The service raises ValueError; the API layer maps it to 400."""
    import pytest

    from app.services.exception_lifecycle import apply_resolution

    exc = _exception()
    with pytest.raises(ValueError, match="Unknown action"):
        apply_resolution(exc, "obliterate", "x", "Demo Admin")
    assert exc.status == "open", "a rejected action must not half-mutate the row"


# ---------- _exception_dict: SLA / overdue surface ------------------------


def test_exception_dict_marks_overdue_when_past_due_at():
    from app.api.exceptions import _exception_dict

    exc = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        exception_type="fraud_flag",
        severity="warning",
        description="x",
        status="open",
        resolution=None,
        resolved_by=None,
        resolved_at=None,
        assigned_to=None,
        assigned_to_user_id=None,
        due_at=datetime.now(UTC) - timedelta(hours=1),
        time_to_resolution_seconds=None,
        created_at=datetime.now(UTC) - timedelta(hours=8),
    )
    body = _exception_dict(exc, None)
    assert body["is_overdue"] is True
    assert body["time_to_resolution_hours"] is None


def test_exception_dict_does_not_mark_terminal_states_overdue():
    """A resolved-but-late exception isn't 'overdue' anymore — the
    work is done. Only open/escalated rows can be overdue."""
    from app.api.exceptions import _exception_dict

    exc = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        exception_type="fraud_flag",
        severity="warning",
        description="x",
        status="resolved",
        resolution="x",
        resolved_by="Demo",
        resolved_at=datetime.now(UTC),
        assigned_to=None,
        assigned_to_user_id=None,
        due_at=datetime.now(UTC) - timedelta(hours=1),  # was overdue
        time_to_resolution_seconds=72000,
        created_at=datetime.now(UTC) - timedelta(hours=20),
    )
    body = _exception_dict(exc, None)
    assert body["is_overdue"] is False
    # Surfaces in hours rounded to 2dp.
    assert body["time_to_resolution_hours"] == 20.0


def test_exception_dict_carries_assignee_uuid():
    from app.api.exceptions import _exception_dict

    target = uuid.uuid4()
    exc = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        exception_type="fraud_flag",
        severity="warning",
        description="x",
        status="open",
        resolution=None,
        resolved_by=None,
        resolved_at=None,
        assigned_to="Demo Manager",
        assigned_to_user_id=target,
        due_at=None,
        time_to_resolution_seconds=None,
        created_at=datetime.now(UTC),
    )
    body = _exception_dict(exc, None)
    assert body["assigned_to_user_id"] == str(target)
    assert body["assigned_to"] == "Demo Manager"


# ---------- get_exception: single-row detail endpoint ---------------------


def test_get_exception_returns_full_dict_for_found_row():
    """The detail endpoint returns the same `_exception_dict` shape the
    list/assign handlers do, joining the invoice."""
    from app.api.exceptions import get_exception

    exc = SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        exception_type="po_mismatch",
        severity="warning",
        description="amount off",
        status="open",
        resolution=None,
        resolved_by=None,
        resolved_at=None,
        assigned_to="Demo Manager",
        assigned_to_user_id=uuid.uuid4(),
        due_at=None,
        time_to_resolution_seconds=None,
        created_at=datetime.now(UTC),
    )
    # `currency` too: a real `Invoice` always carries one, and it is what the
    # queue formats the amount against.
    inv = SimpleNamespace(invoice_number="INV-100", vendor_name="Acme", amount=250, currency="USD")

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=(exc, inv))))
    user = SimpleNamespace(full_name="Demo Admin", organization_id=uuid.uuid4())

    body = asyncio.run(get_exception(exc.id, db=db, user=user, entity_id=None))

    assert body["id"] == str(exc.id)
    assert body["invoice_number"] == "INV-100"
    assert body["vendor_name"] == "Acme"
    assert body["amount"] == 250.0
    assert body["exception_type"] == "po_mismatch"
    assert body["assigned_to"] == "Demo Manager"


def test_get_exception_404_when_missing():
    """A missing / out-of-scope id is an opaque 404 (no enumeration)."""
    import pytest
    from fastapi import HTTPException

    from app.api.exceptions import get_exception

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))
    user = SimpleNamespace(full_name="Demo Admin", organization_id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_exception(uuid.uuid4(), db=db, user=user, entity_id=None))
    assert exc_info.value.status_code == 404


# ---------- _exception_dict: the amount's currency -----------------------


def _exc_row():
    """A minimal exception row for the serializer's pure edges."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        invoice_id=uuid.uuid4(),
        exception_type="po_mismatch",
        severity="warning",
        description="x",
        status="open",
        resolution=None,
        resolved_by=None,
        resolved_at=None,
        assigned_to=None,
        assigned_to_user_id=None,
        due_at=None,
        time_to_resolution_seconds=None,
        created_at=datetime.now(UTC),
    )


def test_exception_dict_names_the_invoice_currency_beside_the_amount():
    """`amount` IS the joined invoice's amount, so it only means something
    beside the code that invoice carries.

    Without this field every client had to pick a currency for the figure
    itself: the mobile exception list and detail screens stamped a `$` on it,
    and the org's REPORTING currency would have been no better — a
    GBP-reporting tenant holds USD invoices, so that is a different wrong
    answer rather than a fix.
    """
    from app.api.exceptions import _exception_dict

    inv = SimpleNamespace(
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=Decimal("250.00"),
        currency="EUR",
    )
    body = _exception_dict(_exc_row(), inv)

    assert body["amount"] == 250.0
    assert body["currency"] == "EUR"


def test_exception_dict_currency_is_none_when_no_invoice_is_joined():
    """An exception can outlive / precede its invoice join, and `None` is not
    a licence to substitute a default (`docs/decisions.md` §79/§82) — the
    client renders the bare figure instead."""
    from app.api.exceptions import _exception_dict

    body = _exception_dict(_exc_row(), None)

    assert body["amount"] is None
    assert body["currency"] is None
