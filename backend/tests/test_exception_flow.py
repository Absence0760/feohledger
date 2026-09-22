"""Exception + warning generation — pins which signals fire which
warnings, which severity those warnings get, and the rules that gate
fraud-rule firing. Auto-creating an `Exception` row from a warning is
how invoices land in the exception queue; misfires either flood the
queue (noise → AP team ignores it → real signal gets buried) or
silently drop a real fraud flag.

The fraud rules each have a master switch in `Organization.settings.
fraud_rules` plus tunable thresholds. These tests pin both directions:
  - default config fires when the signal is present
  - explicit `*_enabled=False` suppresses
  - threshold overrides take effect
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.invoice import InvoiceStatus
from app.services.invoice_warnings import (
    DEFAULT_FRAUD_RULES,
    _fraud_config,
    _status_str,
    refresh_warnings,
)
from app.utils.dates import utc_today


def _invoice(**overrides):
    base = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),  # multi-entity P2: exception inherits invoice entity
        # A real Invoice always carries one, and the exception-lifecycle audit
        # row files under it (services/exception_lifecycle). Without it here the
        # stand-in silently drove the DB-lookup fallback against a mock session
        # instead of the path production takes.
        correlation_id=uuid.uuid4(),
        vendor_id=None,
        vendor_name="Acme Corp",
        invoice_number="INV-1",
        invoice_date=date(2026, 5, 1),
        due_date=date(2026, 6, 1),
        amount=Decimal("250.00"),
        # Read by every money-bearing warning sentence, which names the
        # invoice's own currency rather than a hardcoded `$` (decisions.md §157).
        currency="USD",
        status=InvoiceStatus.new,
        po_number=None,
        po_match=None,
        contract_id=None,
        remit_to_address=None,
        warnings=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _mk_db(dup_count: int = 0, exception_count: int = 0):
    """Bare-bones mock: returns 0 for the dup-count query, 0 for the
    exception-existence query, and lets db.add() collect new rows.
    The vendor-scoped branches are skipped because we leave
    `vendor_id=None` on the invoice."""
    db = AsyncMock()

    # Each execute() call returns a result whose `.scalar()` matches a
    # caller-controlled queue. The endpoint calls scalar() for both
    # the dup-count and the exception-count.
    results = [
        _mk_scalar(dup_count),
        _mk_scalar(exception_count),
        _mk_scalar(exception_count),
        _mk_scalar(exception_count),
        _mk_scalar(exception_count),
    ]
    db.execute = AsyncMock(side_effect=results)
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _mk_scalar(val):
    m = MagicMock()
    m.scalar = MagicMock(return_value=val)
    return m


# ---------------------------------------------------------------------------
# _fraud_config — the merge contract.
# ---------------------------------------------------------------------------


def test_fraud_config_returns_defaults_when_no_overrides():
    cfg = _fraud_config(None)
    for k, v in DEFAULT_FRAUD_RULES.items():
        assert cfg[k] == v


def test_fraud_config_lets_org_override_individual_keys():
    cfg = _fraud_config({"fraud_rules": {"round_amount_min": "5000", "future_date_enabled": False}})
    assert cfg["round_amount_min"] == "5000"
    assert cfg["future_date_enabled"] is False
    # Untouched keys still default.
    assert cfg["rush_payment_enabled"] is True


def test_fraud_config_drops_unknown_override_keys_silently():
    """Old code paths or typos in tenant settings should NOT inject
    rogue keys into the rule dict. A regression that accepts unknown
    keys could let a tenant add arbitrary rule names that downstream
    code might read."""
    cfg = _fraud_config({"fraud_rules": {"made_up_rule": True}})
    assert "made_up_rule" not in cfg


# ---------------------------------------------------------------------------
# _status_str — the three shapes.
# ---------------------------------------------------------------------------


def test_status_str_normalizes_enum_string_and_namespace():
    """A real 500 lived here — `update_invoice` once set the status
    to a plain string. The helper must accept all three shapes."""
    assert _status_str(InvoiceStatus.approved) == "approved"
    assert _status_str("approved") == "approved"
    assert _status_str(SimpleNamespace(value="approved")) == "approved"


# ---------------------------------------------------------------------------
# Missing-field warnings.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_vendor_name_raises_error_warning():
    """No vendor name → an `error`-severity missing_field warning.
    Required for the AP team to even see the invoice."""
    inv = _invoice(vendor_name="")
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    types = {w["type"] for w in warnings}
    assert "missing_field" in types
    assert any(w["type"] == "missing_field" and "vendor" in w["message"].lower() for w in warnings)


@pytest.mark.asyncio
async def test_missing_amount_raises_error_warning():
    """Zero or null amount → error warning. Catches a broken
    extraction that left amount unset."""
    inv = _invoice(amount=Decimal("0"))
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    assert any(w["type"] == "missing_field" and "amount" in w["message"].lower() for w in warnings)


@pytest.mark.asyncio
async def test_missing_data_after_extraction_creates_exception():
    """A `missing_field` warning on a non-`new` invoice means
    extraction landed but didn't fill in the basics — that's an
    exception, not just a warning. Pin that the exception fires
    when status moved past `new`."""
    inv = _invoice(amount=None, status=InvoiceStatus.pending)
    db = _mk_db()
    await refresh_warnings(db, inv)
    added_kinds = [getattr(call.args[0], "exception_type", None) for call in db.add.call_args_list]
    assert "missing_data" in added_kinds


@pytest.mark.asyncio
async def test_new_invoice_with_missing_fields_does_not_open_exception():
    """A `new` invoice hasn't finished extraction yet — missing
    fields are expected. Don't spam the queue with these. (The
    warning still appears, but no Exception row is created.)"""
    inv = _invoice(amount=None, status=InvoiceStatus.new)
    db = _mk_db()
    await refresh_warnings(db, inv)
    added_kinds = [getattr(call.args[0], "exception_type", None) for call in db.add.call_args_list]
    assert "missing_data" not in added_kinds


# ---------------------------------------------------------------------------
# Duplicate detection.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_invoice_number_creates_warning_and_exception():
    """A second invoice with the same (vendor, invoice_number) is a
    `duplicate` warning and queues a `duplicate` exception."""
    inv = _invoice()
    db = _mk_db(dup_count=1)
    warnings = await refresh_warnings(db, inv)
    assert any(w["type"] == "duplicate" for w in warnings)
    added_kinds = [getattr(call.args[0], "exception_type", None) for call in db.add.call_args_list]
    assert "duplicate" in added_kinds


# ---------------------------------------------------------------------------
# Fraud rule: round amount.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_amount_fires_at_default_threshold():
    """$5000.00 is round and >= the $1000 default → fraud_round_amount."""
    inv = _invoice(amount=Decimal("5000.00"))
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    assert any(w["type"] == "fraud_round_amount" for w in warnings)


@pytest.mark.asyncio
async def test_round_amount_below_threshold_does_not_fire():
    """$500 is below the default $1000 floor — even though it's
    round, it shouldn't flag every petty-cash receipt."""
    inv = _invoice(amount=Decimal("500.00"))
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    assert not any(w["type"] == "fraud_round_amount" for w in warnings)


@pytest.mark.asyncio
async def test_round_amount_threshold_override_lifts_floor():
    """Org sets `round_amount_min` to 10000 → $5000 shouldn't fire."""
    inv = _invoice(amount=Decimal("5000.00"))
    db = _mk_db()
    settings = {"fraud_rules": {"round_amount_min": "10000"}}
    warnings = await refresh_warnings(db, inv, org_settings=settings)
    assert not any(w["type"] == "fraud_round_amount" for w in warnings)


@pytest.mark.asyncio
async def test_round_amount_disabled_suppresses_warning():
    """`round_amount_enabled=False` suppresses both the warning AND
    the exception — the per-rule opt-out invariant."""
    inv = _invoice(amount=Decimal("5000.00"))
    db = _mk_db()
    settings = {"fraud_rules": {"round_amount_enabled": False}}
    warnings = await refresh_warnings(db, inv, org_settings=settings)
    assert not any(w["type"] == "fraud_round_amount" for w in warnings)
    added_kinds = [getattr(call.args[0], "exception_type", None) for call in db.add.call_args_list]
    assert "fraud_flag" not in added_kinds


# ---------------------------------------------------------------------------
# Fraud rule: future invoice date.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_future_invoice_date_warns():
    """A future-dated invoice is a forgery red flag."""
    inv = _invoice(invoice_date=utc_today() + timedelta(days=10))
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    assert any(w["type"] == "fraud_future_date" for w in warnings)


# ---------------------------------------------------------------------------
# Fraud rule: rush payment.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rush_payment_fires_when_due_date_inside_window():
    """Due within 3 days of invoice_date → fraud_rush_payment.
    Standard social-engineering signal."""
    today = utc_today()
    inv = _invoice(invoice_date=today, due_date=today + timedelta(days=2))
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    assert any(w["type"] == "fraud_rush_payment" for w in warnings)


@pytest.mark.asyncio
async def test_rush_payment_does_not_fire_for_long_terms():
    """Net-30 invoice with a 30-day gap doesn't match the rule."""
    today = utc_today()
    inv = _invoice(invoice_date=today, due_date=today + timedelta(days=30))
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    assert not any(w["type"] == "fraud_rush_payment" for w in warnings)


@pytest.mark.asyncio
async def test_rush_payment_does_not_fire_for_negative_window():
    """Already-past-due invoices have due_date BEFORE invoice_date
    (mis-extracted dates). The `>= 0` guard keeps these out of the
    rush-payment bucket; they'd show up as `past_due` instead."""
    today = utc_today()
    inv = _invoice(invoice_date=today, due_date=today - timedelta(days=10))
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    assert not any(w["type"] == "fraud_rush_payment" for w in warnings)


# ---------------------------------------------------------------------------
# Past-due flag — informational, but gated by status.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_past_due_fires_for_open_statuses():
    """`new`/`pending`/`ready_for_review` invoices past their due
    date raise the past_due warning. Closed invoices don't."""
    today = utc_today()
    yesterday = today - timedelta(days=1)
    for status in (InvoiceStatus.new, InvoiceStatus.pending, InvoiceStatus.ready_for_review):
        inv = _invoice(due_date=yesterday, status=status)
        db = _mk_db()
        warnings = await refresh_warnings(db, inv)
        assert any(w["type"] == "past_due" for w in warnings), f"past_due missing for {status}"


@pytest.mark.asyncio
async def test_past_due_does_not_fire_after_payment_scheduled():
    """Once payment is scheduled, the past-due nag is moot — the
    money is on its way. Pin that the warning is gated by status."""
    today = utc_today()
    yesterday = today - timedelta(days=1)
    inv = _invoice(due_date=yesterday, status=InvoiceStatus.payment_scheduled)
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    assert not any(w["type"] == "past_due" for w in warnings)


# ---------------------------------------------------------------------------
# Exception write — invoice.warnings is persisted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warnings_are_persisted_on_invoice_after_refresh():
    """`refresh_warnings` writes the computed list onto
    `invoice.warnings`. A regression that forgot the assignment
    would mean the UI never sees the flags."""
    inv = _invoice(amount=Decimal("5000.00"))  # triggers round_amount
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    assert inv.warnings == warnings
    assert len(warnings) >= 1


@pytest.mark.asyncio
async def test_no_warnings_persists_none_not_empty_list():
    """No warnings → `invoice.warnings` is None, not `[]`. The JSONB
    column is queried with `IS NULL` semantics in dashboards, so the
    distinction matters."""
    today = utc_today()
    inv = _invoice(
        amount=Decimal("123.45"),
        invoice_date=today - timedelta(days=2),
        due_date=today + timedelta(days=28),
    )
    db = _mk_db()
    warnings = await refresh_warnings(db, inv)
    assert warnings == []
    assert inv.warnings is None


# ---------------------------------------------------------------------------
# /api/exceptions/summary — the type-filter chips beside the queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_by_type_follows_the_status_filter(realdb):
    """`by_type` drives the type-filter chips, so it must count within the
    status the user is actually looking at.

    It was computed `WHERE status = 'open'` unconditionally. Under the
    Escalated / Resolved / All views the chips therefore showed open-only
    tallies (a chip reading `duplicate 12` beside 2 rows) and — worse — a type
    that exists ONLY among resolved exceptions got no chip at all, so it could
    not be filtered to.
    """
    import uuid as _uuid

    from app.models.exception import Exception as APException
    from app.models.invoice import Invoice

    org_id = realdb.info("a").org_id
    mk = realdb.sessionmaker("a")

    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"EXSUM-{_uuid.uuid4().hex[:6]}",
            vendor_name="Summary Vendor",
            amount=Decimal("100.00"),
            status=InvoiceStatus.new,
        )
        s.add(inv)
        await s.flush()
        s.add(
            APException(
                invoice_id=inv.id,
                exception_type="duplicate",
                status="open",
                organization_id=org_id,
            )
        )
        # Only ever RESOLVED — the type that used to be unreachable.
        s.add(
            APException(
                invoice_id=inv.id,
                exception_type="price_variance",
                status="resolved",
                organization_id=org_id,
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="admin") as c:
        open_view = (await c.get("/api/exceptions/summary?status=open")).json()
        resolved_view = (await c.get("/api/exceptions/summary?status=resolved")).json()
        all_view = (await c.get("/api/exceptions/summary?status=all")).json()
        bare = (await c.get("/api/exceptions/summary")).json()

    # The open view — what the queue opens on — counts open types only.
    assert open_view["by_type"].get("duplicate") == 1
    assert "price_variance" not in open_view["by_type"]

    # The resolved view now offers a chip for the resolved-only type.
    assert resolved_view["by_type"].get("price_variance") == 1
    assert "duplicate" not in resolved_view["by_type"]

    # `all` counts both.
    assert all_view["by_type"].get("duplicate") == 1
    assert all_view["by_type"].get("price_variance") == 1

    # No `status` at all means what it means on the list — every status. This
    # used to fall back to `open`, so a bare summary described a different set
    # from a bare `GET /api/exceptions`.
    assert bare["by_type"] == all_view["by_type"]
