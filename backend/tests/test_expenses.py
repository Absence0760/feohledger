"""Real-DB coverage for the expenses + expense-reports routers.

Covers ``backend/app/api/expenses.py`` end-to-end against the live test
tenants: expense CRUD, receipt upload + download round-trip, report
create/attach/detach with exact ``total_amount`` recompute, RBAC, tenant
isolation, audit rows, and exact ``Numeric`` money round-trips. A passing
upload/CRUD run against the ``realdb`` fixture (whose tenant tables are built
via ``Base.metadata.create_all``) is the create_all parity proof for all five
new tables incl. the circular FK; an explicit table-existence test pins it.
"""

import uuid
from decimal import Decimal

from sqlalchemy import select, text, update

from app.models.expense import Expense, ExpenseReport
from app.models.workflow import AuditLog

# ---------------------------------------------------------------------------
# create_all parity
# ---------------------------------------------------------------------------


async def test_all_expense_tables_exist(realdb):
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        for t in (
            "expense_reports",
            "expenses",
            "expense_policies",
            "corporate_card_transactions",
            "expense_preapprovals",
        ):
            await s.execute(text(f"SELECT 1 FROM {t} LIMIT 1"))  # raises if missing


# ---------------------------------------------------------------------------
# expense CRUD
# ---------------------------------------------------------------------------


async def test_create_expense(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/expenses",
            json={
                "expense_date": "2026-06-01",
                "merchant": "Uber",
                "category": "travel",
                "amount": "42.50",
                "currency": "USD",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["merchant"] == "Uber"
    assert body["amount"] == 42.5
    assert body["status"] == "draft"
    assert body["payment_method"] == "out_of_pocket"

    # Exact Decimal round-trip through Numeric(15, 2).
    async with mk() as s:
        e = (await s.execute(select(Expense))).scalar_one()
        assert e.amount == Decimal("42.50")
        assert e.organization_id == org_id
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.entity_type == "expense")))
            .scalars()
            .all()
        )
        assert "expense.created" in actions


async def test_create_expense_rejects_nonpositive_amount(realdb):
    """A negative / zero expense is a 422 — it must never net a report under the
    CFO approval threshold while hiding a genuinely large line (issue #156)."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        for bad in ("-5001.00", "0", "0.00"):
            resp = await c.post(
                "/api/expenses",
                json={"expense_date": "2026-06-01", "amount": bad},
            )
            assert resp.status_code == 422, f"{bad}: {resp.text}"

        # And a PATCH cannot flip a valid expense negative afterwards.
        eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "10.00"})
        ).json()["id"]
        patched = await c.patch(f"/api/expenses/{eid}", json={"amount": "-1.00"})
        assert patched.status_code == 422, patched.text


async def test_expense_date_cannot_be_cleared_or_omitted(realdb):
    """``expenses.expense_date`` is NOT NULL, so neither create nor update may
    end up writing a null there.

    Create already required it. The PATCH schema, however, typed it
    ``date | None``: an explicit ``null`` passed validation, reached ``setattr``
    in the handler and raised ``NotNullViolationError`` — a bare 500 on what is
    plainly a client error. (The frontend's expense modal sent exactly that when
    the date field was left blank.) It is now a 422; OMITTING the key still
    leaves the stored date untouched, which is what makes the PATCH partial.
    """
    async with realdb.client(key="a", role="ap_clerk") as c:
        # Create without a date is a 422 (unchanged — the field is required).
        assert (await c.post("/api/expenses", json={"amount": "10.00"})).status_code == 422
        # …and an explicit null on create is too.
        assert (
            await c.post("/api/expenses", json={"expense_date": None, "amount": "10.00"})
        ).status_code == 422

        eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "10.00"})
        ).json()["id"]

        # Explicitly clearing it: a 422, NOT a 500 from the DB constraint.
        cleared = await c.patch(f"/api/expenses/{eid}", json={"expense_date": None})
        assert cleared.status_code == 422, cleared.text

        # Omitting it leaves the stored date alone — partial PATCH still works.
        untouched = await c.patch(f"/api/expenses/{eid}", json={"merchant": "Lyft"})
        assert untouched.status_code == 200, untouched.text
        assert untouched.json()["expense_date"] == "2026-06-01"

        # And a real date still updates.
        moved = await c.patch(f"/api/expenses/{eid}", json={"expense_date": "2026-06-09"})
        assert moved.status_code == 200, moved.text
        assert moved.json()["expense_date"] == "2026-06-09"


async def test_not_null_columns_cannot_be_cleared_by_an_explicit_patch_null(realdb):
    """The sibling NOT NULL columns get the same treatment ``expense_date`` does.

    ``expense_date`` was fixed; ``amount``, ``currency``, ``payment_method`` and
    ``reimbursable`` were left typed ``| None`` on ``ExpenseUpdate``, and the
    same PATCH-``null`` reached ``setattr`` and raised ``NotNullViolationError``
    — a bare 500 each. ``amount`` was the sharpest: the handler also re-locks the
    line's FX conversion off ``expense.amount or 0``, so a ``null`` re-priced the
    owning report's line at zero on its way to the DB error.

    The same rule holds on a report (``report_number`` / ``currency``) and on a
    policy (``name`` / ``active`` / ``per_diem_currency``). Genuinely nullable
    columns are unaffected — ``null`` still clears them.
    """
    async with realdb.client(key="a", role="ap_manager") as c:
        eid = (
            await c.post(
                "/api/expenses",
                json={"expense_date": "2026-06-01", "amount": "10.00", "currency": "USD"},
            )
        ).json()["id"]
        for field in ("amount", "currency", "payment_method", "reimbursable"):
            cleared = await c.patch(f"/api/expenses/{eid}", json={field: None})
            assert cleared.status_code == 422, f"{field}: {cleared.status_code} {cleared.text}"

        # The row is untouched by all that, and a real edit still lands.
        after = await c.get(f"/api/expenses/{eid}")
        assert after.json()["amount"] == 10.0
        assert after.json()["currency"] == "USD"
        assert (await c.patch(f"/api/expenses/{eid}", json={"amount": "12.50"})).json()[
            "amount"
        ] == 12.5
        # A nullable column still clears on an explicit null.
        assert (await c.patch(f"/api/expenses/{eid}", json={"merchant": None})).status_code == 200

        rid = (
            await c.post(
                "/api/expense-reports", json={"report_number": "R-NULLS", "currency": "USD"}
            )
        ).json()["id"]
        for field in ("report_number", "currency"):
            cleared = await c.patch(f"/api/expense-reports/{rid}", json={field: None})
            assert cleared.status_code == 422, f"{field}: {cleared.status_code} {cleared.text}"
        assert (
            await c.patch(f"/api/expense-reports/{rid}", json={"notes": None})
        ).status_code == 200

        pid = (await c.post("/api/expense-policies", json={"name": "Nulls"})).json()["id"]
        for field in ("name", "active", "per_diem_currency"):
            cleared = await c.patch(f"/api/expense-policies/{pid}", json={field: None})
            assert cleared.status_code == 422, f"{field}: {cleared.status_code} {cleared.text}"
        assert (
            await c.patch(f"/api/expense-policies/{pid}", json={"category_limit": None})
        ).status_code == 200


async def test_list_filter_and_get(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "10.00"})
        eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-02", "amount": "20.00"})
        ).json()["id"]

        listing = await c.get("/api/expenses")
        assert listing.status_code == 200
        assert listing.json()["total"] >= 2

        one = await c.get(f"/api/expenses/{eid}")
        assert one.status_code == 200
        assert one.json()["amount"] == 20.0


async def test_list_search_matches_merchant_description_and_category(realdb):
    """The list had NO `search` param, so the page could only filter the rows it
    had already loaded — a term matching an expense past the first page read as
    "nothing matched". The three free-text columns the row renders are the ones
    a user expects to search."""
    async with realdb.client(key="a", role="ap_clerk") as c:
        await c.post(
            "/api/expenses",
            json={
                "expense_date": "2026-06-01",
                "merchant": "Hilton Garden Inn",
                "category": "lodging",
                "description": "Two nights, client visit",
                "amount": "310.00",
            },
        )
        await c.post(
            "/api/expenses",
            json={
                "expense_date": "2026-06-02",
                "merchant": "Uber",
                "category": "travel",
                "description": "Airport transfer",
                "amount": "42.50",
            },
        )

        # merchant
        by_merchant = await c.get("/api/expenses", params={"search": "hilton"})
        assert by_merchant.status_code == 200
        assert by_merchant.json()["total"] == 1
        assert by_merchant.json()["items"][0]["merchant"] == "Hilton Garden Inn"

        # category
        by_category = await c.get("/api/expenses", params={"search": "TRAVEL"})
        assert by_category.json()["total"] == 1
        assert by_category.json()["items"][0]["merchant"] == "Uber"

        # description
        by_description = await c.get("/api/expenses", params={"search": "client visit"})
        assert by_description.json()["total"] == 1
        assert by_description.json()["items"][0]["merchant"] == "Hilton Garden Inn"

        # no match is an empty page, not an error
        none_found = await c.get("/api/expenses", params={"search": "zzz-no-such-thing"})
        assert none_found.status_code == 200
        assert none_found.json()["total"] == 0


async def test_list_search_composes_with_the_status_filter(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        keep = (
            await c.post(
                "/api/expenses",
                json={"expense_date": "2026-06-01", "merchant": "Delta Air", "amount": "500.00"},
            )
        ).json()["id"]
        await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-02", "merchant": "Delta Air", "amount": "600.00"},
        )

        # Flip one row's status directly — the point is the two predicates AND
        # together, not how the status got there.
        async with mk() as s:
            row = (
                await s.execute(select(Expense).where(Expense.id == uuid.UUID(keep)))
            ).scalar_one()
            row.status = "submitted"
            await s.commit()

        resp = await c.get("/api/expenses", params={"search": "delta", "status": "submitted"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == keep


async def test_list_search_is_tenant_scoped(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-01", "merchant": "Secret Merchant", "amount": "10.00"},
        )

    async with realdb.client(key="b", role="ap_clerk") as c:
        resp = await c.get("/api/expenses", params={"search": "secret"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


async def test_update_expense(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "10.00"})
        ).json()["id"]
        resp = await c.patch(f"/api/expenses/{eid}", json={"amount": "55.00", "merchant": "Lyft"})
    assert resp.status_code == 200
    assert resp.json()["amount"] == 55.0
    assert resp.json()["merchant"] == "Lyft"

    async with mk() as s:
        e = (await s.execute(select(Expense).where(Expense.id == uuid.UUID(eid)))).scalar_one()
        assert e.amount == Decimal("55.00")
        actions = (
            (await s.execute(select(AuditLog.action).where(AuditLog.action == "expense.updated")))
            .scalars()
            .all()
        )
        assert len(actions) >= 1


async def test_update_expense_audit_names_exactly_what_changed(realdb):
    """`expense.updated` names every field that moved and nothing else (#575):
    a GL recode alone used to write no row, a report move beside a field edit
    dropped `report_id`, and a resent unchanged value is not a change."""
    from app.models.gl_account import GLAccount

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with mk() as s:
        gl = GLAccount(
            organization_id=org_id,
            code=f"6{uuid.uuid4().int % 100000:05d}",
            name="Travel",
            account_type="expense",
        )
        s.add(gl)
        await s.commit()
        gl_id = str(gl.id)

    async with realdb.client(key="a", role="ap_clerk") as c:
        eid = (
            await c.post(
                "/api/expenses",
                json={"expense_date": "2026-06-01", "amount": "10.00", "merchant": "Uber"},
            )
        ).json()["id"]
        rid = (
            await c.post("/api/expense-reports", json={"report_number": f"AUD-{eid[:6]}"})
        ).json()["id"]
        steps = [
            {"amount": "10.00", "merchant": "Uber"},  # resent, nothing changed
            {"gl_account_id": gl_id},  # GL recode alone
            {"report_id": rid, "merchant": "Lyft", "amount": "10.00"},  # move + edit
        ]
        for body in steps:
            resp = await c.patch(f"/api/expenses/{eid}", json=body)
            assert resp.status_code == 200, resp.text

    async with mk() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.action == "expense.updated",
                        AuditLog.entity_id == uuid.UUID(eid),
                    )
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert [sorted(r.details["fields"]) for r in rows] == [
        ["gl_account_id"],
        ["merchant", "report_id"],
    ]


async def test_delete_expense(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "10.00"})
        ).json()["id"]
        gone = await c.delete(f"/api/expenses/{eid}")
        assert gone.status_code == 204
        missing = await c.get(f"/api/expenses/{eid}")
        assert missing.status_code == 404


# ---------------------------------------------------------------------------
# receipt upload + download proxy
# ---------------------------------------------------------------------------


async def test_receipt_upload_roundtrip(realdb):
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="ap_clerk") as c:
        eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "10.00"})
        ).json()["id"]
        resp = await c.post(
            f"/api/expenses/{eid}/receipt",
            files={"file": ("receipt.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert resp.status_code == 200, resp.text
        key = resp.json()["receipt_file_key"]
        assert key.startswith(f"{org_id}/expenses/{eid}/")
        assert resp.json()["receipt_url"] == f"/api/expenses/receipt/{key}"

        got = await c.get(f"/api/expenses/receipt/{key}")
        assert got.status_code == 200
        assert got.content == b"%PDF-1.4 fake"


async def test_receipt_cross_tenant_404(realdb):
    org_a = realdb.info("a").org_id
    async with realdb.client(key="a", role="ap_clerk") as c:
        eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "10.00"})
        ).json()["id"]
        key = (
            await c.post(
                f"/api/expenses/{eid}/receipt",
                files={"file": ("r.pdf", b"%PDF-1.4 fake", "application/pdf")},
            )
        ).json()["receipt_file_key"]

    # Tenant B (different org_id prefix) cannot read tenant A's receipt key.
    assert key.startswith(f"{org_a}/")
    async with realdb.client(key="b", role="ap_clerk") as c:
        denied = await c.get(f"/api/expenses/receipt/{key}")
        assert denied.status_code == 404


# ---------------------------------------------------------------------------
# expense reports — create + attach/detach + total recompute
# ---------------------------------------------------------------------------


async def test_report_attach_recomputes_total(realdb):
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        e1 = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "100.00"})
        ).json()["id"]
        e2 = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-02", "amount": "250.50"})
        ).json()["id"]

        report = (
            await c.post(
                "/api/expense-reports",
                json={"report_number": "EXP-2026-001", "title": "June travel"},
            )
        ).json()
        rid = report["id"]
        assert report["total_amount"] == 0.0
        assert report["employee_user_id"]  # defaulted to the caller

        attached = await c.post(
            f"/api/expense-reports/{rid}/expenses", json={"expense_ids": [e1, e2]}
        )
        assert attached.status_code == 200, attached.text
        assert attached.json()["total_amount"] == 350.5
        assert len(attached.json()["expenses"]) == 2

        # Detach one → total recomputes.
        detached = await c.post(
            f"/api/expense-reports/{rid}/expenses",
            json={"expense_ids": [e1], "detach": True},
        )
        assert detached.json()["total_amount"] == 250.5
        assert len(detached.json()["expenses"]) == 1

    async with mk() as s:
        r = (
            await s.execute(select(ExpenseReport).where(ExpenseReport.id == uuid.UUID(rid)))
        ).scalar_one()
        assert r.total_amount == Decimal("250.50")  # exact, not float
        actions = (
            (
                await s.execute(
                    select(AuditLog.action).where(AuditLog.entity_type == "expense_report")
                )
            )
            .scalars()
            .all()
        )
        assert "expense_report.created" in actions
        assert "expense_report.expenses_attached" in actions


async def test_attach_moves_expense_recomputes_source_report(realdb):
    """Reassigning an expense from report A to report B must recompute A's
    total too — not just B's — or A's Numeric total goes stale."""
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        e1 = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "100.00"})
        ).json()["id"]
        e2 = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-02", "amount": "40.00"})
        ).json()["id"]

        ra = (await c.post("/api/expense-reports", json={"report_number": "MOVE-A"})).json()["id"]
        rb = (await c.post("/api/expense-reports", json={"report_number": "MOVE-B"})).json()["id"]

        # Both expenses start on report A → total 140.00.
        attached = await c.post(
            f"/api/expense-reports/{ra}/expenses", json={"expense_ids": [e1, e2]}
        )
        assert attached.json()["total_amount"] == 140.0

        # Move e1 onto report B. B gains 100; A must drop to 40 (not stay 140).
        moved = await c.post(f"/api/expense-reports/{rb}/expenses", json={"expense_ids": [e1]})
        assert moved.status_code == 200, moved.text
        assert moved.json()["total_amount"] == 100.0

        report_a = (await c.get(f"/api/expense-reports/{ra}")).json()
        assert report_a["total_amount"] == 40.0
        assert len(report_a["expenses"]) == 1

    async with mk() as s:
        a = (
            await s.execute(select(ExpenseReport).where(ExpenseReport.id == uuid.UUID(ra)))
        ).scalar_one()
        assert a.total_amount == Decimal("40.00")  # exact, recomputed


async def test_create_expense_under_report_updates_total(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        rid = (await c.post("/api/expense-reports", json={"report_number": "EXP-2026-002"})).json()[
            "id"
        ]
        # Creating an expense already pointed at the report bumps the total.
        await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-01", "amount": "75.00", "report_id": rid},
        )
        report = (await c.get(f"/api/expense-reports/{rid}")).json()
    assert report["total_amount"] == 75.0


# ---------------------------------------------------------------------------
# RBAC + tenant isolation
# ---------------------------------------------------------------------------


async def test_cfo_cannot_create_expense(realdb):
    # CFO is read-only on mutations.
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "5.00"})
    assert resp.status_code == 403


async def test_cfo_can_read_expenses(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "5.00"})
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/expenses")
    assert resp.status_code == 200


async def test_tenant_isolation(realdb):
    async with realdb.client(key="a", role="ap_manager") as c:
        eid = (
            await c.post("/api/expenses", json={"expense_date": "2026-06-01", "amount": "5.00"})
        ).json()["id"]
    async with realdb.client(key="b", role="ap_manager") as c:
        assert (await c.get(f"/api/expenses/{eid}")).status_code == 404


# ---------------------------------------------------------------------------
# whole-set summary (KPI rollup)
# ---------------------------------------------------------------------------


async def test_summary_covers_the_whole_set_not_one_page(realdb):
    """The KPI row summed the LOADED page.

    `/expenses` fetches 20 rows at a time, so "Period total" and "Pending" were
    tallies of the first page rendered beside a whole-set "Expenses" count that
    contradicted them. The summary is computed in SQL over every matching row.
    """
    async with realdb.client(key="a", role="ap_clerk") as c:
        for i in range(25):
            resp = await c.post(
                "/api/expenses",
                json={
                    "expense_date": "2026-06-01",
                    "merchant": f"Vendor {i}",
                    "amount": "10.00",
                    "currency": "USD",
                },
            )
            assert resp.status_code == 201, resp.text

        page = await c.get("/api/expenses", params={"page_size": 20})
        assert len(page.json()["items"]) == 20  # the page the KPI used to sum

        summary = await c.get("/api/expenses/summary")
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert body["total"] == 25
        assert body["by_status"]["draft"] == 25
        assert body["by_currency"] == [{"currency": "USD", "total": "250.00", "count": 25}]


async def test_summary_never_adds_across_currencies(realdb):
    """EUR + USD is not a total; it is a figure denominated in nothing.

    Each currency keeps its own exact subtotal, and the totals are exact decimal
    strings — 10.10 + 20.20 is "30.30", not a float 30.299999999999997.
    """
    async with realdb.client(key="a", role="ap_clerk") as c:
        for amount, currency in (("10.10", "USD"), ("20.20", "USD"), ("5.05", "EUR")):
            await c.post(
                "/api/expenses",
                json={
                    "expense_date": "2026-06-01",
                    "merchant": "Mixed",
                    "amount": amount,
                    "currency": currency,
                },
            )

        body = (await c.get("/api/expenses/summary")).json()

    assert body["by_currency"] == [
        {"currency": "EUR", "total": "5.05", "count": 1},
        {"currency": "USD", "total": "30.30", "count": 2},
    ]


async def test_currency_codes_are_normalized_on_write(realdb):
    """A currency code is only a *label* until something groups or compares on it.

    ``max_length=3`` alone accepted ``""``, ``"us"`` and ``"usd"`` on every
    expense / report / pre-approval write (``ExpensePolicy.threshold_currency``
    was shape-checked from the start; its older siblings were not). Two
    consequences were live:

    * ``""`` is read by ``expense_currency.normalize_currency(code, default=…)``
      as "whatever the target is" — the silent assume-the-default that locked-FX
      exists to eliminate; and
    * ``"usd"`` and ``"USD"`` are two ``GROUP BY`` keys, so
      ``GET /api/expenses/summary`` returned two ``by_currency`` entries the
      response then relabelled identically, splitting one currency's money
      across two rows both reading ``USD``.

    The code is now uppercased + shape-checked at the boundary, and the summary
    groups on the uppercased column so a row written before that still rolls up
    into one bucket.
    """
    async with realdb.client(key="a", role="ap_clerk") as c:
        for bad in ("", "us", "usdd", "12$"):
            refused = await c.post(
                "/api/expenses",
                json={"expense_date": "2026-06-01", "amount": "1.00", "currency": bad},
            )
            assert refused.status_code == 422, f"{bad!r}: {refused.status_code}"

        created = await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-01", "amount": "10.00", "currency": " eur "},
        )
        assert created.status_code == 201, created.text
        assert created.json()["currency"] == "EUR"

        report = await c.post(
            "/api/expense-reports", json={"report_number": "R-CUR", "currency": "gbp"}
        )
        assert report.status_code == 201, report.text
        assert report.json()["currency"] == "GBP"

        preapproval = await c.post(
            "/api/expense-preapprovals",
            json={"title": "Trip", "estimated_amount": "50.00", "currency": "jpy"},
        )
        assert preapproval.status_code == 201, preapproval.text
        assert preapproval.json()["currency"] == "JPY"

    # A row that predates the validator still rolls into ONE bucket.
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        await s.execute(update(Expense).where(Expense.currency == "EUR").values(currency="eur"))
        await s.commit()

    async with realdb.client(key="a", role="ap_clerk") as c:
        await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-02", "amount": "5.00", "currency": "EUR"},
        )
        body = (await c.get("/api/expenses/summary")).json()

    eur = [row for row in body["by_currency"] if row["currency"] == "EUR"]
    assert eur == [{"currency": "EUR", "total": "15.00", "count": 2}], body["by_currency"]


async def test_summary_applies_the_same_filters_as_the_list(realdb):
    """The rollup and the table must describe ONE set — they share
    `_expense_list_filters`, and this pins that they can't drift apart."""
    mk = realdb.sessionmaker("a")
    async with realdb.client(key="a", role="ap_clerk") as c:
        keep = (
            await c.post(
                "/api/expenses",
                json={"expense_date": "2026-06-01", "merchant": "Delta Air", "amount": "500.00"},
            )
        ).json()["id"]
        await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-02", "merchant": "Delta Air", "amount": "600.00"},
        )
        await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-03", "merchant": "Hilton", "amount": "700.00"},
        )

        async with mk() as s:
            row = (
                await s.execute(select(Expense).where(Expense.id == uuid.UUID(keep)))
            ).scalar_one()
            row.status = "submitted"
            await s.commit()

        for params in (
            {},
            {"search": "delta"},
            {"status": "submitted"},
            {"search": "delta", "status": "submitted"},
        ):
            listed = (await c.get("/api/expenses", params=params)).json()
            rolled = (await c.get("/api/expenses/summary", params=params)).json()
            assert rolled["total"] == listed["total"], params


async def test_summary_is_tenant_scoped(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        await c.post(
            "/api/expenses",
            json={"expense_date": "2026-06-01", "merchant": "Only Tenant A", "amount": "99.00"},
        )

    async with realdb.client(key="b", role="ap_clerk") as c:
        body = (await c.get("/api/expenses/summary")).json()
    assert body["total"] == 0
    assert body["by_currency"] == []
