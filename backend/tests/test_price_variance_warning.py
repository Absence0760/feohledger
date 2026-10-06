"""Tests for the persisted line-item price-variance warning/exception rule in
``services.invoice_warnings`` (the data-enrichment follow-up).

Price-variance detection already existed as a compute-on-read signal in
``services.vendor_enrichment.detect_price_variance``; this rule wires that *same
pure math* into the ``refresh_warnings`` write chokepoint so a deviating line
persists as an ``Invoice.warnings`` entry AND a de-duped ``price_variance``
``Exception`` row — mirroring how ``fraud_stat_anomaly`` and the other rules
raise both.

Hermetic: ``refresh_warnings`` runs against an ``AsyncMock`` session whose
``execute`` dispatches by query shape (the established pattern in
``test_fraud_rules.py``). The draft + history line-item rows are canned; the math
is the real ``detect_price_variance``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------- Helpers --------------------------------------------------------


def _invoice(**overrides):
    base = dict(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
        vendor_name="Acme Corp",
        invoice_number="INV-200",
        amount=Decimal("500.00"),
        currency="USD",
        invoice_date=date(2026, 5, 1),
        due_date=date(2026, 6, 1),  # normal window — don't trip rush rule
        status="ready_for_review",  # extracted (not `new`) so the rule runs
        po_number=None,
        po_match=None,
        contract_id=None,
        warnings=None,
        vendor_id=uuid.uuid4(),
        remit_to_address=None,
        recurring_template_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _vendor(**overrides):
    base = dict(
        id=uuid.uuid4(),
        name="Acme Corp",
        email="ap@acme.com",
        status="active",
        created_at=datetime.now(UTC) - timedelta(days=200),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _line(item_code=None, description=None, unit_price=None, currency=None):
    """A mapping-shaped row (mirrors SQLAlchemy ``row._mapping``)."""
    d = {"item_code": item_code, "description": description, "unit_price": unit_price}
    if currency is not None:
        d["currency"] = currency
    return d


def _make_db(
    *,
    vendor=None,
    draft_lines=None,
    history_lines=None,
    existing_price_exc=0,
):
    """AsyncMock session dispatching by query shape.

    Queries ``refresh_warnings`` issues that we care about:
      - vendor lookup                          → scalar_one_or_none()
      - duplicate count / generic counts       → scalar()
      - existing price_variance exception count → scalar()
      - draft line items (this invoice)        → .all() rows w/ _mapping
      - history line items (vendor, joined)    → .all() rows w/ _mapping
    """
    draft_lines = draft_lines or []
    history_lines = history_lines or []

    def _scalar_result(value):
        r = MagicMock()
        r.scalar = MagicMock(return_value=value)
        r.scalar_one_or_none = MagicMock(return_value=value)
        return r

    def _rows_result(mappings):
        rows = []
        for m in mappings:
            row = MagicMock()
            row._mapping = m
            rows.append(row)
        r = MagicMock()
        r.all = MagicMock(return_value=rows)
        return r

    async def _execute(*args, **_kw):
        q = str(args[0]).lower() if args else ""
        if "from vendors" in q:
            return _scalar_result(vendor)
        if "from invoice_line_items" in q and "join invoices" in q:
            return _rows_result(history_lines)
        if "from invoice_line_items" in q:
            return _rows_result(draft_lines)
        if "from exceptions" in q:
            # price_variance dedup count (or any _ensure_exception precheck)
            return _scalar_result(existing_price_exc)
        if "count(" in q:
            return _scalar_result(0)  # duplicate count etc.
        # historical amounts (stat anomaly) etc.
        r = MagicMock()
        r.scalar = MagicMock(return_value=0)
        r.scalar_one_or_none = MagicMock(return_value=None)
        r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return r

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.add = MagicMock()
    return db


def _types(warnings):
    return {w["type"] for w in warnings}


def _added_exception_types(db):
    from app.models.exception import Exception as APException

    return [
        c.args[0].exception_type
        for c in db.add.call_args_list
        if c.args and isinstance(c.args[0], APException)
    ]


# ---------- Config helper --------------------------------------------------


def test_price_variance_settings_defaults():
    from app.services.invoice_warnings import _price_variance_settings
    from app.services.vendor_enrichment import (
        PRICE_ESCALATE_PCT,
        PRICE_MIN_HISTORY,
        PRICE_TOLERANCE_PCT,
    )

    cfg = _price_variance_settings(None)
    assert cfg["tolerance_pct"] == PRICE_TOLERANCE_PCT
    assert cfg["escalate_pct"] == PRICE_ESCALATE_PCT
    assert cfg["min_history"] == PRICE_MIN_HISTORY


def test_price_variance_settings_override_from_enrichment_block():
    from app.services.invoice_warnings import _price_variance_settings

    cfg = _price_variance_settings(
        {"enrichment": {"price_tolerance_pct": "5", "price_min_history": 4}}
    )
    assert cfg["tolerance_pct"] == Decimal("5")
    assert cfg["min_history"] == 4


def test_price_variance_settings_bad_value_falls_back():
    from app.services.invoice_warnings import _price_variance_settings
    from app.services.vendor_enrichment import PRICE_TOLERANCE_PCT

    cfg = _price_variance_settings({"enrichment": {"price_tolerance_pct": "abc"}})
    assert cfg["tolerance_pct"] == PRICE_TOLERANCE_PCT


def test_price_variance_in_default_fraud_rules():
    from app.services.invoice_warnings import DEFAULT_FRAUD_RULES

    assert DEFAULT_FRAUD_RULES["price_variance_enabled"] is True


# ---------- Warning + exception on deviation -------------------------------


@pytest.mark.asyncio
async def test_price_variance_flags_overpriced_line():
    """A draft line 50% above the vendor's per-item median trips a warning
    (severity warning past the 30% escalate threshold) + an exception row."""
    from app.services.invoice_warnings import refresh_warnings

    draft = [_line(item_code="WIDGET-A", unit_price=Decimal("15.00"))]
    history = [
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
    ]
    db = _make_db(vendor=_vendor(), draft_lines=draft, history_lines=history)
    warnings = await refresh_warnings(db, _invoice())

    pv = [w for w in warnings if w["type"] == "price_variance"]
    assert len(pv) == 1
    assert pv[0]["severity"] == "warning"  # +50% >= 30% escalate
    assert "price_variance" in _added_exception_types(db)


@pytest.mark.asyncio
async def test_price_variance_info_severity_below_escalate():
    """A 20% deviation is past the 15% tolerance but below the 30% escalate —
    severity `info`."""
    from app.services.invoice_warnings import refresh_warnings

    draft = [_line(item_code="WIDGET-A", unit_price=Decimal("12.00"))]
    history = [
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
    ]
    db = _make_db(vendor=_vendor(), draft_lines=draft, history_lines=history)
    warnings = await refresh_warnings(db, _invoice())

    pv = [w for w in warnings if w["type"] == "price_variance"]
    assert len(pv) == 1
    assert pv[0]["severity"] == "info"


@pytest.mark.asyncio
async def test_price_variance_not_flagged_within_tolerance():
    """A price within tolerance produces no warning and no exception."""
    from app.services.invoice_warnings import refresh_warnings

    draft = [_line(item_code="WIDGET-A", unit_price=Decimal("10.50"))]  # +5%
    history = [
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
    ]
    db = _make_db(vendor=_vendor(), draft_lines=draft, history_lines=history)
    warnings = await refresh_warnings(db, _invoice())

    assert "price_variance" not in _types(warnings)
    assert "price_variance" not in _added_exception_types(db)


@pytest.mark.asyncio
async def test_price_variance_skipped_without_enough_history():
    """One prior price is below PRICE_MIN_HISTORY (2) — no baseline, no flag."""
    from app.services.invoice_warnings import refresh_warnings

    draft = [_line(item_code="WIDGET-A", unit_price=Decimal("20.00"))]
    history = [_line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD")]
    db = _make_db(vendor=_vendor(), draft_lines=draft, history_lines=history)
    warnings = await refresh_warnings(db, _invoice())

    assert "price_variance" not in _types(warnings)


# ---------- Gating / idempotency -------------------------------------------


@pytest.mark.asyncio
async def test_price_variance_disabled_via_org_setting():
    from app.services.invoice_warnings import refresh_warnings

    draft = [_line(item_code="WIDGET-A", unit_price=Decimal("15.00"))]
    history = [
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
    ]
    db = _make_db(vendor=_vendor(), draft_lines=draft, history_lines=history)
    warnings = await refresh_warnings(
        db, _invoice(), org_settings={"fraud_rules": {"price_variance_enabled": False}}
    )
    assert "price_variance" not in _types(warnings)
    assert "price_variance" not in _added_exception_types(db)


@pytest.mark.asyncio
async def test_price_variance_skipped_on_new_draft():
    """A `new` (un-extracted) invoice has no line items yet — rule is skipped."""
    from app.services.invoice_warnings import refresh_warnings

    draft = [_line(item_code="WIDGET-A", unit_price=Decimal("15.00"))]
    history = [
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
    ]
    db = _make_db(vendor=_vendor(), draft_lines=draft, history_lines=history)
    warnings = await refresh_warnings(db, _invoice(status="new"))
    assert "price_variance" not in _types(warnings)


@pytest.mark.asyncio
async def test_price_variance_idempotent_when_exception_exists():
    """Re-running with an existing open price_variance exception still emits the
    warning but does NOT add a second exception row (dedup via _ensure_exception)."""
    from app.services.invoice_warnings import refresh_warnings

    draft = [_line(item_code="WIDGET-A", unit_price=Decimal("15.00"))]
    history = [
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
        _line(item_code="WIDGET-A", unit_price=Decimal("10.00"), currency="USD"),
    ]
    db = _make_db(
        vendor=_vendor(),
        draft_lines=draft,
        history_lines=history,
        existing_price_exc=1,  # one already open
    )
    warnings = await refresh_warnings(db, _invoice())
    assert "price_variance" in _types(warnings)
    assert "price_variance" not in _added_exception_types(db)


@pytest.mark.asyncio
async def test_price_variance_no_vendor_no_flag():
    """A vendor-less invoice has no attributable history — rule skipped."""
    from app.services.invoice_warnings import refresh_warnings

    db = _make_db(vendor=None, draft_lines=[], history_lines=[])
    warnings = await refresh_warnings(db, _invoice(vendor_id=None))
    assert "price_variance" not in _types(warnings)


@pytest.mark.asyncio
async def test_price_variance_message_is_pii_free():
    """The warning/exception message carries only item label + prices/percent —
    no bank/tax/address PII (it's built from line-item fields only)."""
    from app.services.invoice_warnings import refresh_warnings

    draft = [_line(description="Premium Widget", unit_price=Decimal("15.00"))]
    history = [
        _line(description="Premium Widget", unit_price=Decimal("10.00"), currency="USD"),
        _line(description="Premium Widget", unit_price=Decimal("10.00"), currency="USD"),
    ]
    db = _make_db(vendor=_vendor(), draft_lines=draft, history_lines=history)
    warnings = await refresh_warnings(db, _invoice())
    msg = next(w["message"] for w in warnings if w["type"] == "price_variance")
    assert "Premium Widget" in msg
    assert "15.00" in msg and "10.00" in msg


def _added_exceptions(db):
    from app.models.exception import Exception as APException

    return [
        c.args[0] for c in db.add.call_args_list if c.args and isinstance(c.args[0], APException)
    ]


@pytest.mark.asyncio
async def test_price_variance_exception_carries_the_lines_decomposed_not_joined():
    """Two flagged lines raise ONE exception whose description is the frame
    code + the two line warnings themselves (migration 0103) — not a
    server-joined English summary, and no `$` on a rand invoice. Each finding
    is the SAME dict the invoice's warning list carries, so the queue and the
    invoice state each line in one wording."""
    from app.services.invoice_warnings import refresh_warnings

    draft = [
        _line(item_code="WIDGET-A", unit_price=Decimal("15.00")),
        _line(item_code="BOLT-B", unit_price=Decimal("5.00")),
    ]
    history = [
        _line(item_code=code, unit_price=Decimal("10.00"), currency="ZAR")
        for code in ("WIDGET-A", "WIDGET-A", "BOLT-B", "BOLT-B")
    ]
    db = _make_db(vendor=_vendor(), draft_lines=draft, history_lines=history)
    warnings = await refresh_warnings(db, _invoice(currency="ZAR"))

    line_warnings = [w for w in warnings if w["type"] == "price_variance"]
    (exc,) = [e for e in _added_exceptions(db) if e.exception_type == "price_variance"]
    assert exc.description_code == "exception.price_variance_findings"
    assert exc.description_params["count"] == 2
    assert [
        {"code": f["code"], "params": f["params"]} for f in exc.description_params["findings"]
    ] == [{"code": w["code"], "params": w["params"]} for w in line_warnings]
    assert "$" not in exc.description
    assert "ZAR" in exc.description
