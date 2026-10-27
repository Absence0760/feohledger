"""Real-DB coverage for the vendor-statement reconciliation router
(``app/api/vendor_statement_recon.py``).

Exercises create-from-lines + CSV upload (with a 422 on a malformed CSV), the
list/filter, the detail-with-lines view, line resolution flipping a run to
``resolved``, the close-readiness materiality gate, delete-with-cascade, and
RBAC — end-to-end against the live test tenants. Reconciliation math is owned by
the (separately tested) ``services.vendor_statement_recon`` engine; here we
prove the HTTP surface wires it through correctly with exact ``Numeric`` money.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.entity import Entity
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.vendor import Vendor
from app.models.vendor_statement_recon import (
    VendorStatementReconciliation,
    VendorStatementReconLine,
)
from app.models.workflow import AuditLog

_TODAY = date.today()


async def _default_entity_id(s):
    return (
        await s.execute(select(Entity.id).where(Entity.is_default.is_(True)).limit(1))
    ).scalar_one()


async def _add_vendor(mk, org_id, name="Globex Industrial") -> str:
    async with mk() as s:
        v = Vendor(organization_id=org_id, name=name, entity_id=await _default_entity_id(s))
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return str(v.id)


async def _add_invoice(
    mk,
    org_id,
    *,
    vendor_id,
    invoice_number,
    amount="1000.00",
    status=InvoiceStatus.approved,
    invoice_date=None,
    currency="USD",
) -> str:
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=await _default_entity_id(s),
            invoice_number=invoice_number,
            vendor_name="Globex Industrial",
            vendor_id=uuid.UUID(vendor_id),
            amount=Decimal(amount),
            currency=currency,
            invoice_date=invoice_date or _TODAY,
            due_date=_TODAY + timedelta(days=30),
            status=status,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return str(inv.id)


def _line(invoice_number, amount, *, invoice_date=None, status="open"):
    return {
        "invoice_number": invoice_number,
        "invoice_date": (invoice_date or _TODAY).isoformat(),
        "amount": amount,
        "status": status,
    }


# ---------------------------------------------------------------------------
# create from lines — classification
# ---------------------------------------------------------------------------


async def test_create_from_lines_classifies_and_audits(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    # Two of our open invoices.
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="INV-100", amount="1000.00")
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="INV-200", amount="2000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": vendor_id,
                "statement_date": _TODAY.isoformat(),
                "currency": "USD",
                "lines": [
                    _line("INV-100", "1000.00"),  # matched
                    _line("INV-999", "500.00"),  # missing on our side
                    # INV-200 omitted → missing on their side
                ],
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    by_class = {}
    for ln in body["lines"]:
        by_class.setdefault(ln["classification"], []).append(ln)

    assert len(by_class["matched"]) == 1
    assert by_class["matched"][0]["statement_invoice_number"] == "INV-100"
    assert by_class["matched"][0]["matched_invoice_number"] == "INV-100"
    assert by_class["matched"][0]["statement_amount"] == 1000.0

    assert len(by_class["missing_on_our_side"]) == 1
    assert by_class["missing_on_our_side"][0]["statement_invoice_number"] == "INV-999"

    assert len(by_class["missing_on_their_side"]) == 1
    assert by_class["missing_on_their_side"][0]["matched_invoice_number"] == "INV-200"

    summary = body["summary"]
    assert summary["matched_count"] == 1
    assert summary["missing_our_side_count"] == 1
    assert summary["missing_their_side_count"] == 1
    assert body["status"] == "open"
    assert body["source_format"] == "manual"

    async with mk() as s:
        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "vendor_statement_recon.created",
                    AuditLog.entity_id == uuid.UUID(body["id"]),
                )
            )
        ).scalar_one()
        assert audit.entity_type == "vendor_statement_reconciliation"


async def test_ledger_candidates_are_scoped_to_the_statement_currency(realdb):
    """A statement is denominated in ONE currency, and the engine compares bare
    ``Decimal``s — it holds no rate and must not invent one.

    So the candidate ledger has to be filtered to the statement's currency
    before it reaches the engine. Without that filter a EUR 1 000 invoice
    amount-matches a USD 1 000 statement line and *displaces* the real USD
    invoice, which then falls out as ``missing_on_their_side``: both halves
    wrong from one mixed-currency candidate set. The EUR invoice must also not
    be reported missing — a supplier's USD statement of open items is simply
    not about it.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    # Same number so leg 1 (invoice-number) can't be what saves us, and the
    # same amount so leg 2 (amount + date) would happily take the EUR row.
    await _add_invoice(
        mk, org_id, vendor_id=vendor_id, invoice_number="INV-EUR", amount="1000.00", currency="EUR"
    )
    await _add_invoice(
        mk, org_id, vendor_id=vendor_id, invoice_number="INV-USD", amount="1000.00", currency="USD"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": vendor_id,
                "statement_date": _TODAY.isoformat(),
                "currency": "USD",
                "lines": [_line("INV-USD", "1000.00")],
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # Exactly one line: the USD invoice, matched. The EUR invoice is neither a
    # match candidate nor an orphan.
    assert body["summary"]["line_count"] == 1
    assert body["summary"]["matched_count"] == 1
    assert body["summary"]["missing_their_side_count"] == 0
    assert body["lines"][0]["classification"] == "matched"
    assert body["lines"][0]["matched_invoice_number"] == "INV-USD"


async def test_create_amount_mismatch_line(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="INV-300", amount="1000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": vendor_id,
                "statement_date": _TODAY.isoformat(),
                # Supplier claims 1100 vs our 1000 → mismatch of 100.
                "lines": [_line("INV-300", "1100.00")],
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    line = body["lines"][0]
    assert line["classification"] == "amount_mismatch"
    assert line["statement_amount"] == 1100.0
    assert line["ledger_amount"] == 1000.0
    assert line["amount_difference"] == 100.0
    assert body["summary"]["amount_mismatch_count"] == 1


async def test_create_fully_matched_run_is_resolved_immediately(realdb):
    """Issue #185: a statement that reconciles cleanly (every line `matched`,
    nothing actionable) has no `resolve_line` call ahead of it — the run must
    get its final status at creation, not sit `open` forever."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="CLEAN-1", amount="100.00")
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="CLEAN-2", amount="200.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": vendor_id,
                "statement_date": _TODAY.isoformat(),
                "lines": [_line("CLEAN-1", "100.00"), _line("CLEAN-2", "200.00")],
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert {ln["classification"] for ln in body["lines"]} == {"matched"}
    assert body["summary"]["matched_count"] == 2
    assert body["status"] == "resolved"

    # And it's actually persisted that way, not just in the response shape.
    async with mk() as s:
        run = (
            await s.execute(
                select(VendorStatementReconciliation).where(
                    VendorStatementReconciliation.id == uuid.UUID(body["id"])
                )
            )
        ).scalar_one()
        assert run.status == "resolved"


async def test_create_run_with_actionable_line_stays_open_until_resolved(realdb):
    """Regression: a run with at least one actionable (non-matched) line must
    still start `open`, and only flip to `resolved` once a human clears the
    last actionable line via `resolve_line` — the create-time recompute must
    not short-circuit that existing behavior."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="MIX-1", amount="100.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        create_resp = await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": vendor_id,
                "statement_date": _TODAY.isoformat(),
                # MIX-1 matches; MIX-2 has no corresponding invoice → actionable.
                "lines": [_line("MIX-1", "100.00"), _line("MIX-2", "50.00")],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        detail = create_resp.json()
        assert detail["status"] == "open"

        actionable = next(
            ln for ln in detail["lines"] if ln["classification"] == "missing_on_our_side"
        )
        resolved = (
            await c.post(
                f"/api/vendor-statements/{detail['id']}/lines/{actionable['id']}/resolve",
                json={"resolution_status": "resolved", "resolution_note": "created the invoice"},
            )
        ).json()
    assert resolved["status"] == "resolved"


async def test_create_404_unknown_vendor(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": str(uuid.uuid4()),
                "statement_date": _TODAY.isoformat(),
                "lines": [_line("INV-1", "10.00")],
            },
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# CSV upload
# ---------------------------------------------------------------------------


async def test_upload_csv_happy_path(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="INV-500", amount="750.00")

    csv_body = (
        b"invoice_number,date,amount,status\r\n"
        b"INV-500,2026-06-01,750.00,open\r\n"
        b"INV-501,2026-06-02,250.00,open\r\n"
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements/upload",
            data={
                "vendor_id": vendor_id,
                "statement_date": _TODAY.isoformat(),
                "statement_reference": "STMT-Q2",
                "currency": "USD",
            },
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_format"] == "csv"
    # The uploaded document is archived beside the run so the match can be
    # audited against the original, not only against what we parsed.
    assert body["has_source_file"] is True
    assert body["file_key"].startswith(f"{org_id}/vendor-statements/{body['id']}/")
    assert body["extraction"] is None  # no model read this — it was a CSV
    assert body["statement_reference"] == "STMT-Q2"
    classes = {ln["classification"] for ln in body["lines"]}
    assert "matched" in classes
    assert "missing_on_our_side" in classes


async def test_upload_csv_malformed_returns_422(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    # Header row only, no data rows → StatementParseError.
    bad = b"invoice_number,amount\r\n"
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements/upload",
            data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
            files={"file": ("bad.csv", bad, "text/csv")},
        )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# list + filter
# ---------------------------------------------------------------------------


async def test_list_filters_by_vendor_and_status(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    v1 = await _add_vendor(mk, org_id, name="Vendor One")
    v2 = await _add_vendor(mk, org_id, name="Vendor Two")

    async with realdb.client(key="a", role="ap_manager") as c:
        r1 = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": v1,
                    "statement_date": _TODAY.isoformat(),
                    "lines": [_line("A-1", "10.00")],
                },
            )
        ).json()
        await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": v2,
                "statement_date": _TODAY.isoformat(),
                "lines": [_line("B-1", "20.00")],
            },
        )

        only_v1 = (await c.get(f"/api/vendor-statements?vendor_id={v1}")).json()
        assert [i["id"] for i in only_v1["items"]] == [r1["id"]]
        assert only_v1["total"] == 1
        # List omits lines.
        assert only_v1["items"][0]["lines"] is None

        open_runs = (await c.get("/api/vendor-statements?status=open")).json()
        assert open_runs["total"] >= 2
        resolved = (await c.get("/api/vendor-statements?status=resolved")).json()
        assert resolved["total"] == 0


async def test_summary_is_whole_set_status_counts_and_open_discrepancies(realdb):
    """`GET /api/vendor-statements/summary` counts the whole matching set by
    status and counts the still-open actionable lines across it — the figures
    the page derived from the loaded page (`openCount`, `totalDiscrepancies`).
    It honours the same vendor_id / status filters as the list."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    v1 = await _add_vendor(mk, org_id, name="Recon KPI One")
    v2 = await _add_vendor(mk, org_id, name="Recon KPI Two")
    await _add_invoice(mk, org_id, vendor_id=v1, invoice_number="RK-1", amount="1000.00")
    await _add_invoice(mk, org_id, vendor_id=v2, invoice_number="RK-2", amount="500.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        # v1: supplier claims 1100 vs our 1000 → amount_mismatch, run stays open.
        await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": v1,
                "statement_date": _TODAY.isoformat(),
                "lines": [_line("RK-1", "1100.00")],
            },
        )
        # v2: clean match → run resolves immediately, no discrepancies.
        await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": v2,
                "statement_date": _TODAY.isoformat(),
                "lines": [_line("RK-2", "500.00")],
            },
        )

        allsum = (await c.get("/api/vendor-statements/summary")).json()
        assert allsum["total"] >= 2
        assert allsum["by_status"].get("open", 0) >= 1
        assert allsum["by_status"].get("resolved", 0) >= 1
        assert allsum["open_discrepancies"] >= 1

        # vendor filter narrows the rollup exactly like the list
        v1sum = (await c.get(f"/api/vendor-statements/summary?vendor_id={v1}")).json()
        assert v1sum["total"] == 1
        assert v1sum["by_status"] == {"open": 1}
        assert v1sum["open_discrepancies"] == 1

        v2sum = (await c.get(f"/api/vendor-statements/summary?vendor_id={v2}")).json()
        assert v2sum["open_discrepancies"] == 0

    async with realdb.client(key="a", role="ap_clerk") as c:
        assert (await c.get("/api/vendor-statements/summary")).status_code == 200


async def test_summary_open_discrepancies_falls_when_a_line_is_resolved(realdb):
    """Resolving (and ignoring) a line LOWERS `open_discrepancies`.

    Regression: the KPI summed the run's import-time
    `amount_mismatch_count` / `missing_our_side_count` /
    `missing_their_side_count` columns, which `resolve_line` never updates — so
    the figure was monotonically non-decreasing and a clerk who cleared every
    difference still saw the day-one number above a table of `resolved` rows.
    It now counts the LIVE lines under the same predicate `_recompute_run_status`
    and `close-readiness` use.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Recon KPI Falls")
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="RKF-1", amount="1000.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        detail = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor_id,
                    "statement_date": _TODAY.isoformat(),
                    # RKF-1 at the wrong amount -> amount_mismatch (actionable);
                    # RKF-9 has no invoice at all -> missing_on_our_side (actionable).
                    "lines": [_line("RKF-1", "1100.00"), _line("RKF-9", "50.00")],
                },
            )
        ).json()

        before = (await c.get(f"/api/vendor-statements/summary?vendor_id={vendor_id}")).json()
        assert before["open_discrepancies"] == 2

        mismatch = next(ln for ln in detail["lines"] if ln["classification"] == "amount_mismatch")
        missing = next(
            ln for ln in detail["lines"] if ln["classification"] == "missing_on_our_side"
        )

        await c.post(
            f"/api/vendor-statements/{detail['id']}/lines/{mismatch['id']}/resolve",
            json={"resolution_status": "resolved"},
        )
        mid = (await c.get(f"/api/vendor-statements/summary?vendor_id={vendor_id}")).json()
        assert mid["open_discrepancies"] == 1

        # `ignored` is a decision too — a known, accepted difference is not open.
        await c.post(
            f"/api/vendor-statements/{detail['id']}/lines/{missing['id']}/resolve",
            json={"resolution_status": "ignored"},
        )
        after = (await c.get(f"/api/vendor-statements/summary?vendor_id={vendor_id}")).json()
        assert after["open_discrepancies"] == 0
        assert after["by_status"] == {"resolved": 1}

        # Re-opening a line puts it back.
        await c.post(
            f"/api/vendor-statements/{detail['id']}/lines/{mismatch['id']}/resolve",
            json={"resolution_status": "unresolved"},
        )
        reopened = (await c.get(f"/api/vendor-statements/summary?vendor_id={vendor_id}")).json()
        assert reopened["open_discrepancies"] == 1


async def test_summary_open_discrepancies_excludes_missing_their_side(realdb):
    """`missing_on_their_side` is outside `_ACTIONABLE_CLASSES` and must never
    reach the KPI.

    Regression: the KPI summed `missing_their_side_count` alongside the two
    actionable counters, so an invoice on OUR ledger the supplier simply hadn't
    billed inflated a figure the rest of the feature (run status,
    close-readiness) ignores entirely — the run reads `resolved` while the card
    above it reports open discrepancies.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Recon KPI Their Side")
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="RKT-1", amount="100.00")
    # On our ledger, absent from the statement -> missing_on_their_side.
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="RKT-2", amount="200.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        detail = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor_id,
                    "statement_date": _TODAY.isoformat(),
                    "lines": [_line("RKT-1", "100.00")],
                },
            )
        ).json()
        assert detail["summary"]["missing_their_side_count"] == 1
        # Nothing actionable is open, so the run itself is already resolved.
        assert detail["status"] == "resolved"

        rollup = (await c.get(f"/api/vendor-statements/summary?vendor_id={vendor_id}")).json()
        assert rollup["by_status"] == {"resolved": 1}
        assert rollup["open_discrepancies"] == 0


# ---------------------------------------------------------------------------
# detail
# ---------------------------------------------------------------------------


async def test_get_detail_includes_lines(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="D-1", amount="100.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        run_id = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor_id,
                    "statement_date": _TODAY.isoformat(),
                    "lines": [_line("D-1", "100.00")],
                },
            )
        ).json()["id"]
        # ap_clerk can read.
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get(f"/api/vendor-statements/{run_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["lines"] is not None
    assert body["lines"][0]["matched_invoice_number"] == "D-1"


async def test_get_detail_404_cross_tenant(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    async with realdb.client(key="a", role="ap_manager") as c:
        run_id = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor_id,
                    "statement_date": _TODAY.isoformat(),
                    "lines": [_line("X-1", "10.00")],
                },
            )
        ).json()["id"]
    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get(f"/api/vendor-statements/{run_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# resolve a line → run flips to resolved
# ---------------------------------------------------------------------------


async def test_resolve_only_actionable_line_resolves_run(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    # One matched (non-actionable) + one missing-our-side (the only actionable).
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="R-1", amount="100.00")

    async with realdb.client(key="a", role="ap_manager") as c:
        detail = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor_id,
                    "statement_date": _TODAY.isoformat(),
                    "lines": [_line("R-1", "100.00"), _line("R-2", "50.00")],
                },
            )
        ).json()
        assert detail["status"] == "open"
        actionable = next(
            ln for ln in detail["lines"] if ln["classification"] == "missing_on_our_side"
        )

        resolved = (
            await c.post(
                f"/api/vendor-statements/{detail['id']}/lines/{actionable['id']}/resolve",
                json={"resolution_status": "resolved", "resolution_note": "invoice created"},
            )
        ).json()

    assert resolved["status"] == "resolved"
    flipped = next(ln for ln in resolved["lines"] if ln["id"] == actionable["id"])
    assert flipped["resolution_status"] == "resolved"
    assert flipped["resolution_note"] == "invoice created"
    assert flipped["resolved_at"] is not None

    async with mk() as s:
        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "vendor_statement_recon.line_resolved",
                    AuditLog.entity_id == uuid.UUID(detail["id"]),
                )
            )
        ).scalar_one()
        assert audit.entity_type == "vendor_statement_reconciliation"


async def test_resolve_back_to_unresolved_clears_and_reopens(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        detail = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor_id,
                    "statement_date": _TODAY.isoformat(),
                    "lines": [_line("U-1", "75.00")],  # missing_on_our_side, actionable
                },
            )
        ).json()
        line_id = detail["lines"][0]["id"]
        # Resolve → run resolved.
        r1 = (
            await c.post(
                f"/api/vendor-statements/{detail['id']}/lines/{line_id}/resolve",
                json={"resolution_status": "ignored"},
            )
        ).json()
        assert r1["status"] == "resolved"
        # Back to unresolved → run open again + resolved_* cleared.
        r2 = (
            await c.post(
                f"/api/vendor-statements/{detail['id']}/lines/{line_id}/resolve",
                json={"resolution_status": "unresolved"},
            )
        ).json()
    assert r2["status"] == "open"
    assert r2["lines"][0]["resolution_status"] == "unresolved"
    assert r2["lines"][0]["resolved_at"] is None


# ---------------------------------------------------------------------------
# close-readiness
# ---------------------------------------------------------------------------


async def test_close_readiness_flags_over_threshold_clears_under(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Big Balance Co")

    async with realdb.client(key="a", role="ap_manager") as c:
        # A 5000 missing-our-side line → unreconciled 5000.
        await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": vendor_id,
                "statement_date": _TODAY.isoformat(),
                "lines": [_line("BIG-1", "5000.00")],
            },
        )

        # Materiality 1000 → vendor is blocking.
        over = (await c.get("/api/vendor-statements/close-readiness?materiality=1000")).json()
        assert over["is_close_ready"] is False
        assert over["materiality_threshold"] == 1000.0
        blocked = [v for v in over["blocking_vendors"] if v["vendor_id"] == vendor_id]
        assert len(blocked) == 1
        assert blocked[0]["unreconciled_amount"] == 5000.0
        assert blocked[0]["missing_our_side_count"] == 1

        # Materiality 10000 → under threshold, not blocking.
        under = (await c.get("/api/vendor-statements/close-readiness?materiality=10000")).json()
        assert all(v["vendor_id"] != vendor_id for v in under["blocking_vendors"])


async def test_close_readiness_ignores_resolved_lines(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Cleared Co")

    async with realdb.client(key="a", role="ap_manager") as c:
        detail = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor_id,
                    "statement_date": _TODAY.isoformat(),
                    "lines": [_line("CL-1", "5000.00")],
                },
            )
        ).json()
        line_id = detail["lines"][0]["id"]
        await c.post(
            f"/api/vendor-statements/{detail['id']}/lines/{line_id}/resolve",
            json={"resolution_status": "resolved"},
        )
        # The run flipped to resolved, so close-readiness (which only looks at
        # OPEN runs) no longer considers this vendor.
        res = (await c.get("/api/vendor-statements/close-readiness?materiality=1000")).json()
    assert all(v["vendor_id"] != vendor_id for v in res["blocking_vendors"])


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_cascades_lines(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        run_id = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor_id,
                    "statement_date": _TODAY.isoformat(),
                    "lines": [_line("DEL-1", "10.00"), _line("DEL-2", "20.00")],
                },
            )
        ).json()["id"]
        resp = await c.delete(f"/api/vendor-statements/{run_id}")
    assert resp.status_code == 204

    async with mk() as s:
        run = (
            await s.execute(
                select(VendorStatementReconciliation).where(
                    VendorStatementReconciliation.id == uuid.UUID(run_id)
                )
            )
        ).scalar_one_or_none()
        assert run is None
        remaining = (
            (
                await s.execute(
                    select(VendorStatementReconLine).where(
                        VendorStatementReconLine.reconciliation_id == uuid.UUID(run_id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


async def test_clerk_cannot_create(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/vendor-statements",
            json={
                "vendor_id": vendor_id,
                "statement_date": _TODAY.isoformat(),
                "lines": [_line("RB-1", "10.00")],
            },
        )
    assert resp.status_code == 403


async def test_clerk_cannot_upload_a_statement(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/vendor-statements/upload",
            data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
            files={"file": ("s.csv", b"invoice,amount\nX,1\n", "text/csv")},
        )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# PDF upload — routed through the extraction pipeline
# ---------------------------------------------------------------------------


def _statement_pdf(rows: list[str]) -> bytes:
    """A real PDF carrying a text layer, so the `mock` adapter's offline reader
    is exercised end to end rather than stubbed out."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page()
    text = "Statement of Account\nInvoice     Date          Amount\n" + "\n".join(rows)
    page.insert_text((40, 60), text, fontsize=9, fontname="cour")
    return doc.tobytes()


async def _set_extraction_provider(realdb, key: str, provider: str) -> None:
    """Point the tenant's org at a specific extraction provider — `mock` is the
    offline, credential-free reader, which is what makes this whole path
    testable (and locally runnable) with no cloud account."""
    org_id = realdb.info(key).org_id
    async with realdb.control_sessionmaker()() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        org.settings = {
            **(org.settings or {}),
            "extraction": {"program_type": "byok", "provider": provider},
        }
        await s.commit()


async def test_upload_pdf_routes_through_extraction_and_reconciles(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_extraction_provider(realdb, "a", "mock")
    vendor_id = await _add_vendor(mk, org_id)
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="PDF-100", amount="1200.00")
    await _add_invoice(mk, org_id, vendor_id=vendor_id, invoice_number="PDF-200", amount="2000.00")

    pdf = _statement_pdf(
        [
            "PDF-100    2026-01-15    1,200.00",  # matches our ledger
            "PDF-900    2026-01-20    500.00",  # supplier billed it, we have nothing
            # PDF-200 omitted → missing on their side
        ]
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements/upload",
            data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
            files={"file": ("statement.pdf", pdf, "application/pdf")},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_format"] == "pdf"

    by_class: dict[str, list] = {}
    for ln in body["lines"]:
        by_class.setdefault(ln["classification"], []).append(ln)
    assert [ln["statement_invoice_number"] for ln in by_class["matched"]] == ["PDF-100"]
    # Exact money survived the extractor → Decimal boundary (a "1,200.00" read
    # off the page is 1200.00, not 1.0).
    assert by_class["matched"][0]["statement_amount"] == 1200.0
    assert [ln["statement_invoice_number"] for ln in by_class["missing_on_our_side"]] == ["PDF-900"]
    assert [ln["matched_invoice_number"] for ln in by_class["missing_on_their_side"]] == ["PDF-200"]

    # Provenance: a reviewer can see these lines were machine-read, and by what.
    assert body["extraction"]["method"] == "ai_extraction"
    assert body["extraction"]["provider"] == "mock"
    assert body["extraction"]["line_count"] == 2
    assert body["has_source_file"] is True

    async with mk() as s:
        audit = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.action == "vendor_statement_recon.created",
                    AuditLog.entity_id == uuid.UUID(body["id"]),
                )
            )
        ).scalar_one()
        assert audit.details["source_format"] == "pdf"


async def test_upload_pdf_archives_the_source_document_for_download(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_extraction_provider(realdb, "a", "mock")
    vendor_id = await _add_vendor(mk, org_id)
    pdf = _statement_pdf(["ARCH-1    2026-01-15    100.00"])

    async with realdb.client(key="a", role="ap_manager") as c:
        run_id = (
            await c.post(
                "/api/vendor-statements/upload",
                data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
                files={"file": ("statement.pdf", pdf, "application/pdf")},
            )
        ).json()["id"]
        got = await c.get(f"/api/vendor-statements/{run_id}/file")

    assert got.status_code == 200, got.text
    assert got.content == pdf
    assert "attachment" in got.headers["content-disposition"]


async def test_storage_failure_does_not_cost_the_clerk_the_reconciliation(realdb, monkeypatch):
    """Archiving the document is best-effort. A storage outage must not fail an
    upload that already reconciled — but it must be visible, not swallowed."""
    from app.services import storage as storage_mod

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    csv_body = b"invoice_number,date,amount\r\nSTORE-1,2026-06-01,750.00\r\n"

    async def _explode(*args, **kwargs):
        raise RuntimeError("minio is down")

    monkeypatch.setattr(storage_mod, "upload_vendor_statement_file", _explode)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements/upload",
            data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
            files={"file": ("statement.csv", csv_body, "text/csv")},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["summary"]["line_count"] == 1
    # The run stands; the client is told there is no document to fetch.
    assert body["file_key"] is None
    assert body["has_source_file"] is False

    async with mk() as s:
        run = (
            await s.execute(
                select(VendorStatementReconciliation).where(
                    VendorStatementReconciliation.id == uuid.UUID(body["id"])
                )
            )
        ).scalar_one()
        assert run.meta["raw_file_stored"] is False

    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.get(f"/api/vendor-statements/{body['id']}/file")).status_code == 404


async def test_source_download_404s_when_nothing_was_archived(realdb):
    """A pasted-lines run has no document — the same opaque 404 as an unknown
    run, so the endpoint never enumerates."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    async with realdb.client(key="a", role="ap_manager") as c:
        run_id = (
            await c.post(
                "/api/vendor-statements",
                json={
                    "vendor_id": vendor_id,
                    "statement_date": _TODAY.isoformat(),
                    "lines": [_line("NOFILE-1", "10.00")],
                },
            )
        ).json()["id"]
        resp = await c.get(f"/api/vendor-statements/{run_id}/file")
    assert resp.status_code == 404


async def test_source_download_is_tenant_scoped(realdb):
    """Tenant B may not fetch tenant A's statement document — the run lookup is
    tenant/entity-scoped, so a cross-tenant id is an ordinary 404."""
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    await _set_extraction_provider(realdb, "a", "mock")
    vendor_id = await _add_vendor(mk_a, org_a)
    pdf = _statement_pdf(["XT-1    2026-01-15    100.00"])
    async with realdb.client(key="a", role="ap_manager") as c:
        run_id = (
            await c.post(
                "/api/vendor-statements/upload",
                data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
                files={"file": ("statement.pdf", pdf, "application/pdf")},
            )
        ).json()["id"]

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get(f"/api/vendor-statements/{run_id}/file")
    assert resp.status_code == 404


async def test_deleting_a_run_removes_its_archived_document(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_extraction_provider(realdb, "a", "mock")
    vendor_id = await _add_vendor(mk, org_id)
    pdf = _statement_pdf(["DELF-1    2026-01-15    100.00"])

    async with realdb.client(key="a", role="ap_manager") as c:
        created = (
            await c.post(
                "/api/vendor-statements/upload",
                data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
                files={"file": ("statement.pdf", pdf, "application/pdf")},
            )
        ).json()
        assert (await c.delete(f"/api/vendor-statements/{created['id']}")).status_code == 204

    from botocore.exceptions import ClientError

    from app.services import storage

    with pytest.raises(ClientError):
        await storage.get_file(created["file_key"])


async def test_upload_unreadable_pdf_refuses_instead_of_inventing_lines(realdb):
    """A document the configured provider can't read must 422 — never a run
    with fabricated open items, and never a run asserting the supplier listed
    nothing (which reads as "we owe them nothing")."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_extraction_provider(realdb, "a", "mock")
    vendor_id = await _add_vendor(mk, org_id)
    unreadable = _statement_pdf([])  # header furniture only → no open-item rows

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements/upload",
            data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
            files={"file": ("scan.pdf", unreadable, "application/pdf")},
        )
    assert resp.status_code == 422, resp.text
    assert "CSV" in resp.json()["detail"]

    async with mk() as s:
        runs = (await s.execute(select(VendorStatementReconciliation))).scalars().all()
        assert runs == []


async def test_upload_pdf_refused_when_provider_cannot_read_statements(realdb):
    """openai_vision hasn't implemented the capability — the upload is refused
    with an actionable message rather than silently mis-read."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_extraction_provider(realdb, "a", "openai_vision")
    vendor_id = await _add_vendor(mk, org_id)
    pdf = _statement_pdf(["NOPE-1    2026-01-15    100.00"])

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements/upload",
            data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
            files={"file": ("statement.pdf", pdf, "application/pdf")},
        )
    assert resp.status_code == 422, resp.text
    assert "CSV" in resp.json()["detail"]


async def test_oversized_upload_is_refused_before_any_extraction_call(realdb, monkeypatch):
    """The cap is enforced ahead of the provider call, so an oversized post
    never becomes a paid extraction (or a 25 MB provider request)."""
    from app.services import storage as storage_mod
    from app.services import vendor_statement_extraction as vse

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    monkeypatch.setattr(storage_mod, "MAX_FILE_SIZE", 64)

    async def _boom(**kwargs):  # pragma: no cover - must never run
        raise AssertionError("extraction must not be reached for an oversized upload")

    monkeypatch.setattr(vse, "extract_statement_lines", _boom)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements/upload",
            data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
            files={"file": ("big.pdf", b"%PDF-1.4" + b"x" * 512, "application/pdf")},
        )
    assert resp.status_code == 413, resp.text


async def test_pdf_posted_as_octet_stream_is_still_routed_to_extraction(realdb):
    """A browser posting a PDF as application/octet-stream must not be fed to
    the CSV parser — magic bytes decide."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    await _set_extraction_provider(realdb, "a", "mock")
    vendor_id = await _add_vendor(mk, org_id)
    pdf = _statement_pdf(["OCT-1    2026-01-15    100.00"])

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/vendor-statements/upload",
            data={"vendor_id": vendor_id, "statement_date": _TODAY.isoformat()},
            files={"file": ("statement.bin", pdf, "application/octet-stream")},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["source_format"] == "pdf"
