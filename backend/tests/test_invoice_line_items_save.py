"""`PUT /api/invoices/{id}/line-items` — audit trail + header reconciliation.

The endpoint used to delete and re-insert an invoice's line items with **zero**
audit dispatch, no re-derivation of the invoice's warnings, and no relationship
whatsoever between the re-summed lines and the header `amount` a payment run
pays. A reviewer correcting a line total in the invoice modal therefore left the
header stale, PO-match variance computed against a total the lines no longer
supported, and no record anywhere that the lines had moved.

Covered here:

  - the pure reconciliation primitive `invoice_warnings.reconcile_line_totals`:
    tax-inclusive lines, tax-exclusive lines against `subtotal`, tax-exclusive
    lines against the derived net, the one-cent tolerance, and a genuine
    divergence
  - a reconciling save writes an `invoice.line_items_edited` audit row (counts +
    exact string-Decimal money, no line text) and raises no mismatch signal
  - a diverging save leaves `invoice.amount` UNTOUCHED (the header is never
    silently overwritten from a line sum — that would move money with no
    approval behind it) and instead surfaces an `error` `line_total_mismatch`
    warning on the row plus an open exception in the queue
  - clearing the lines is audited too

The post-approval financial freeze on this endpoint is covered by
`test_invoice_critical_path.test_line_items_frozen_after_approved`.

Runs against the opt-in `realdb` fixture (skips without `pnpm db:up`).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.invoice_warnings import line_total_reconciles, reconcile_line_totals

# ---------------------------------------------------------------------------
# Pure reconciliation primitive — no DB
# ---------------------------------------------------------------------------


def _header(**kw):
    base = {
        "amount": Decimal("1500.00"),
        "currency": "USD",
        "subtotal": None,
        "tax_amount": None,
        "shipping_amount": None,
        "discount_amount": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_reconciles_when_lines_are_tax_inclusive():
    """The vision adapters emit a tax-INCLUSIVE line total, so the sum equals
    the header amount outright."""
    assert reconcile_line_totals(_header(), Decimal("1500.00")) is None


def test_reconciles_against_subtotal_when_lines_are_net_of_tax():
    """`e_invoice.mapper` maps the same column onto UBL LineExtensionAmount,
    which is net of tax — then the sum matches the stated subtotal."""
    inv = _header(subtotal=Decimal("1350.00"), tax_amount=Decimal("150.00"))
    assert reconcile_line_totals(inv, Decimal("1350.00")) is None


def test_reconciles_against_derived_net_when_no_subtotal():
    """No subtotal column: the net is derived from the header adjustments."""
    inv = _header(
        amount=Decimal("1600.00"),
        tax_amount=Decimal("150.00"),
        shipping_amount=Decimal("120.00"),
        discount_amount=Decimal("20.00"),
    )
    # 1600 - 150 - 120 + 20 = 1350
    assert reconcile_line_totals(inv, Decimal("1350.00")) is None


def test_one_cent_of_rounding_is_tolerated():
    assert reconcile_line_totals(_header(), Decimal("1500.01")) is None
    assert reconcile_line_totals(_header(), Decimal("1499.99")) is None


def test_shared_definition_accepts_each_basis_and_is_none_safe():
    """`line_total_reconciles` is the ONE definition the payment gate and
    extraction's self-correction pass share (issue #566 — they disagreed)."""
    us = {
        "amount": Decimal("2020.86"),
        "subtotal": Decimal("1919.15"),
        "tax_amount": Decimal("101.71"),
        "shipping_amount": None,
        "discount_amount": None,
    }
    assert line_total_reconciles(Decimal("1919.15"), **us)  # subtotal
    assert line_total_reconciles(Decimal("2020.86"), **us)  # amount
    assert line_total_reconciles(Decimal("1919.15"), **{**us, "subtotal": None})  # derived net
    assert not line_total_reconciles(Decimal("1500.00"), **us)
    # An absent subtotal is not a zero basis, and absent adjustments read as 0.
    assert not line_total_reconciles(
        Decimal("0"),
        amount=Decimal("10.00"),
        subtotal=None,
        tax_amount=None,
        shipping_amount=None,
        discount_amount=None,
    )


def test_genuine_divergence_is_reported_against_the_header_amount():
    mismatch = reconcile_line_totals(_header(), Decimal("1750.00"))
    assert mismatch is not None
    # Exact decimal strings — never float.
    assert mismatch["line_items_total"] == "1750.00"
    assert mismatch["header_amount"] == "1500.00"
    assert mismatch["difference"] == "250.00"
    assert mismatch["currency"] == "USD"
    assert all(isinstance(v, str) for v in mismatch.values())


# ---------------------------------------------------------------------------
# HTTP — audit trail + reconciliation over the real app
# ---------------------------------------------------------------------------


async def _seed_invoice(mk, org_id, *, amount: str, number: str, **cols) -> uuid.UUID:
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=org_id,
                invoice_number=number,
                vendor_name="Line Item Vendor",
                amount=Decimal(amount),
                currency="USD",
                status=InvoiceStatus.ready_for_review,
                **cols,
            )
        )
        await s.commit()
    return inv_id


async def _audit_rows(mk, invoice_id, action: str) -> list[AuditLog]:
    async with mk() as s:
        return list(
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == invoice_id,
                        AuditLog.action == action,
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_reconciling_save_writes_audit_row_and_no_mismatch(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, amount="1500.00", number="LI-OK-1")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[
                {"description": "Consulting", "quantity": "1", "total": "1000.00"},
                {"description": "Support", "quantity": "1", "total": "500.00"},
            ],
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["saved"] == 2
    assert body["reconciles_with_header"] is True
    # Money crosses the wire as an exact decimal string, never a float.
    assert body["line_items_total"] == "1500.00"
    assert body["header_amount"] == "1500.00"

    rows = await _audit_rows(mk, inv_id, "invoice.line_items_edited")
    assert len(rows) == 1, rows
    details = rows[0].details
    assert details["reconciles_with_header"] is True
    assert details["changes"]["line_item_count"] == {"old": 0, "new": 2}
    assert details["changes"]["line_items_total"]["new"] == "1500.00"
    # PII-free: counts + money only, never the free-form line text.
    assert "Consulting" not in str(details)

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        types = {w["type"] for w in (inv.warnings or [])}
        assert "line_total_mismatch" not in types
        open_exc = (
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == inv_id,
                        APException.exception_type == "line_total_mismatch",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert open_exc == []


@pytest.mark.asyncio
async def test_diverging_save_flags_mismatch_and_never_overwrites_the_header(realdb):
    """The core failure this endpoint used to allow: a corrected line leaves the
    header at its old value and nothing says so. The header must stay put (only
    an approval may move the payable amount) AND the divergence must be loud."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, amount="1500.00", number="LI-BAD-1")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[{"description": "Consulting", "quantity": "1", "total": "1750.00"}],
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reconciles_with_header"] is False
    assert body["line_items_total"] == "1750.00"
    assert body["header_amount"] == "1500.00"

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        # NEVER silently recomputed from the lines.
        assert inv.amount == Decimal("1500.00")
        flags = [w for w in (inv.warnings or []) if w["type"] == "line_total_mismatch"]
        assert len(flags) == 1, inv.warnings
        assert flags[0]["severity"] == "error"
        assert flags[0]["line_items_total"] == "1750.00"
        assert flags[0]["header_amount"] == "1500.00"

        excs = (
            (
                await s.execute(
                    select(APException).where(
                        APException.invoice_id == inv_id,
                        APException.exception_type == "line_total_mismatch",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(excs) == 1
        assert excs[0].status == "open"
        assert excs[0].severity == "error"

    rows = await _audit_rows(mk, inv_id, "invoice.line_items_edited")
    assert len(rows) == 1
    assert rows[0].details["reconciles_with_header"] is False


@pytest.mark.asyncio
async def test_correcting_a_line_back_into_agreement_clears_the_flag(realdb):
    """The warning engine re-derives from scratch, so fixing the lines (or the
    header, via the normal PATCH) must retire the mismatch — a stuck flag would
    be as useless as no flag."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, amount="1500.00", number="LI-FIX-1")

    async with realdb.client(key="a", role="ap_manager") as c:
        bad = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[{"description": "Consulting", "total": "1750.00"}],
        )
        assert bad.status_code == 200, bad.text
        assert bad.json()["reconciles_with_header"] is False

        good = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[{"description": "Consulting", "total": "1500.00"}],
        )
    assert good.status_code == 200, good.text
    assert good.json()["reconciles_with_header"] is True

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        assert not [w for w in (inv.warnings or []) if w["type"] == "line_total_mismatch"]

    # Both saves are on the record.
    rows = await _audit_rows(mk, inv_id, "invoice.line_items_edited")
    assert len(rows) == 2, rows


@pytest.mark.asyncio
async def test_clearing_all_lines_is_audited(realdb):
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, amount="1500.00", number="LI-CLEAR-1")

    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[{"description": "Consulting", "total": "1500.00"}],
        )
        assert first.status_code == 200, first.text
        cleared = await c.put(f"/api/invoices/{inv_id}/line-items", json=[])
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["saved"] == 0
    assert cleared.json()["line_items_total"] is None
    # No lines left to disagree with the header.
    assert cleared.json()["reconciles_with_header"] is True

    rows = await _audit_rows(mk, inv_id, "invoice.line_items_edited")
    assert len(rows) == 2, rows
    last = sorted(rows, key=lambda r: r.created_at)[-1]
    assert last.details["changes"]["line_item_count"] == {"old": 1, "new": 0}


@pytest.mark.asyncio
async def test_gl_only_recode_is_audited(realdb):
    """A re-code that swaps the GL account moves neither the count nor the
    total — but it is still a change to financial coding, so the trail must
    record it. Change detection compares every column, not just the money."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, amount="1500.00", number="LI-GL-1")

    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[{"description": "Consulting", "total": "1500.00", "gl_account": "6100"}],
        )
        assert first.status_code == 200, first.text
        recoded = await c.put(
            f"/api/invoices/{inv_id}/line-items",
            json=[{"description": "Consulting", "total": "1500.00", "gl_account": "6200"}],
        )
    assert recoded.status_code == 200, recoded.text

    rows = await _audit_rows(mk, inv_id, "invoice.line_items_edited")
    assert len(rows) == 2, rows
    last = sorted(rows, key=lambda r: r.created_at)[-1]
    # Money didn't move, so only the GL codes show up in the diff.
    assert last.details["changes"]["gl_accounts"] == {"old": ["6100"], "new": ["6200"]}
    assert "line_items_total" not in last.details["changes"]


@pytest.mark.asyncio
async def test_resaving_identical_lines_writes_no_audit_row(realdb):
    """Nothing changed, so nothing is logged — the trail must not fill with
    no-op rows every time the modal saves. The two sides are compared by VALUE
    (Postgres hands back `Decimal("1.0000")` where the request sent `1`)."""
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = await _seed_invoice(mk, info.org_id, amount="1500.00", number="LI-NOOP-1")

    payload = [
        {
            "line_number": 1,
            "description": "Consulting",
            "quantity": "1",
            "unit_price": "1500.00",
            "total": "1500.00",
            "gl_account": "6100",
        }
    ]
    async with realdb.client(key="a", role="ap_manager") as c:
        first = await c.put(f"/api/invoices/{inv_id}/line-items", json=payload)
        assert first.status_code == 200, first.text
        again = await c.put(f"/api/invoices/{inv_id}/line-items", json=payload)
    assert again.status_code == 200, again.text

    rows = await _audit_rows(mk, inv_id, "invoice.line_items_edited")
    assert len(rows) == 1, rows
