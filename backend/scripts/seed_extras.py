"""Add feature-page demo data to a tenant: contracts, credit memos, discount
offers, recurring invoice templates, expenses (reports + policies), and a
vendor-statement reconciliation run.

The main ``scripts/seed.py`` full seed builds invoices/vendors/POs/payments but
never populated the CLM (`/contracts`), credit-memo (`/credit-memos`),
dynamic-discounting (`/discounts`), recurring-invoice (`/recurring`), expense
(`/expenses`) or vendor-statement-reconciliation (`/vendor-statements`) pages —
so those list views render empty on a freshly-seeded tenant. This script fills
that gap.

It is **additive and idempotent**: it bails if the tenant already has contracts,
so re-running is safe, and `seed.py`'s `seed_tenant` calls `seed_extras()`
in-line so fresh full seeds include this data automatically.

Usage (from `backend/`):

    python scripts/seed_extras.py                       # default: feoh_acme
    python scripts/seed_extras.py --tenant feoh_techflow  # a specific tenant

`seed_extras(session, org_id)` is the reusable builder — it queries the
tenant's existing vendors/invoices/GL accounts from the session, so it works
both mid-`seed_tenant` (rows flushed, not yet committed) and standalone against
an already-seeded tenant. It never commits; the caller owns the transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below.
# Run as a script, sys.path[0] is `scripts/`, so `app` is not on sys.path at
# all — and a worktree reusing the primary checkout's `backend/.venv` resolves
# it through the editable install's baked-in `__editable___backend_0_1_0_finder`,
# acting on the WRONG tree with nothing on screen to say so. Inserted at 1 so
# the script's own directory keeps priority; idempotent, and a no-op in the
# checkout the venv was installed from.
#
# Deliberately NOT hoisted into a variable: ruff's E402 exempts `sys.path`
# manipulation before imports but not an assignment beside it, so naming the
# path here turns every import below into a lint error.
# See `frontend/tests-e2e/README.md` § Running from a worktree.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.database import _make_tenant_url, control_session_factory
from app.db_engine import create_async_engine
from app.models.contract import Contract, ContractLineItem, ContractStatus, ContractType
from app.models.credit_memo import CreditMemo
from app.models.discount import (
    OFFER_SCOPE_INVOICE,
    OFFER_SCOPE_VENDOR,
    OFFER_SOURCE_FINANCING,
    OFFER_SOURCE_SUPPLIER,
    OFFER_SOURCE_SYSTEM,
    OFFER_STATUS_ACCEPTED,
    OFFER_STATUS_CAPTURED,
    OFFER_STATUS_DECLINED,
    OFFER_STATUS_EXPIRED,
    OFFER_STATUS_OFFERED,
    DiscountOffer,
)
from app.models.expense import (
    Expense,
    ExpensePaymentMethod,
    ExpensePolicy,
    ExpenseReport,
    ExpenseReportStatus,
    ExpenseStatus,
)
from app.models.gl_account import GLAccount
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.recurring_invoice import (
    CADENCE_ANNUAL,
    CADENCE_MONTHLY,
    CADENCE_QUARTERLY,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    RecurringInvoiceTemplate,
)
from app.models.user import User
from app.models.vendor import Vendor
from app.models.vendor_statement_recon import (
    RESOLUTION_UNRESOLVED,
    SOURCE_MANUAL,
    STATUS_OPEN,
    VendorStatementReconciliation,
    VendorStatementReconLine,
)
from app.services import vendor_statement_recon as recon


def _q(amount: Decimal) -> Decimal:
    """Quantize to cents — money is exact (Numeric(15, 2))."""
    return amount.quantize(Decimal("0.01"))


async def _actor_user_id(org_id: uuid.UUID) -> uuid.UUID | None:
    """Any control-plane user in the org — used as contract owner / approver /
    employee on the seeded rows (these are plain UUID columns, no cross-DB FK)."""
    async with control_session_factory() as ctrl:
        return (
            await ctrl.execute(select(User.id).where(User.organization_id == org_id).limit(1))
        ).scalar()


async def seed_extras(session, org_id: uuid.UUID) -> dict[str, int]:
    """Add contracts, credit memos, discount offers, recurring templates, and
    expenses to a tenant.

    Idempotent: returns an empty tally and does nothing if the tenant already
    has contracts. Does **not** commit — the caller owns the transaction.
    """
    existing = (await session.execute(text("SELECT count(*) FROM contracts"))).scalar()
    if existing and existing > 0:
        print("  Extras already seeded (contracts exist). Skipping.")
        return {}

    # Default entity for entity-scoped reads. In seed_tenant this runs before
    # finalize_entities creates it (→ None, finalize backfills); standalone on a
    # finalized tenant it's present, so we stamp it directly.
    default_entity_id = (
        await session.execute(text("SELECT id FROM entities WHERE is_default LIMIT 1"))
    ).scalar()

    vendors = list(
        (
            await session.execute(
                select(Vendor).where(Vendor.status == "active").order_by(Vendor.name).limit(20)
            )
        )
        .scalars()
        .all()
    )
    if not vendors:
        print("  No active vendors — run scripts/seed.py first. Skipping extras.")
        return {}

    invoices = list(
        (await session.execute(select(Invoice).order_by(Invoice.invoice_number).limit(40)))
        .scalars()
        .all()
    )
    gl_accounts = list(
        (
            await session.execute(
                select(GLAccount)
                .where(GLAccount.account_type == "expense")
                .order_by(GLAccount.code)
            )
        )
        .scalars()
        .all()
    )
    actor_id = await _actor_user_id(org_id)
    today = date.today()

    def _ent(row):
        """Stamp the default entity_id (no-op when it doesn't exist yet)."""
        if default_entity_id is not None:
            row.entity_id = default_entity_id
        return row

    # ---- Contracts (+ line items) ----------------------------------------
    # Spread across every status + a representative set of types, with renewal
    # config so the `/contracts` page filters and the renewal sweep have spread.
    contract_specs = [
        ("MSA", ContractType.msa, ContractStatus.active, 120000, 100000, True, 60),
        ("Cloud hosting", ContractType.subscription, ContractStatus.active, 102000, None, True, 30),
        (
            "Janitorial services",
            ContractType.service,
            ContractStatus.active,
            48000,
            48000,
            False,
            45,
        ),
        ("Office lease", ContractType.lease, ContractStatus.active, 360000, None, False, 90),
        ("Hardware supply", ContractType.purchase, ContractStatus.active, 75000, 80000, False, 30),
        ("Support SLA", ContractType.sla, ContractStatus.active, 24000, None, True, 30),
        ("Marketing SOW", ContractType.sow, ContractStatus.draft, 30000, None, False, 30),
        ("Legal retainer", ContractType.service, ContractStatus.draft, 54000, None, False, 30),
        (
            "Freight agreement",
            ContractType.purchase,
            ContractStatus.expired,
            60000,
            60000,
            False,
            30,
        ),
        ("Catering MSA", ContractType.msa, ContractStatus.terminated, 18000, None, False, 30),
        (
            "Pilot subscription",
            ContractType.subscription,
            ContractStatus.cancelled,
            12000,
            None,
            False,
            30,
        ),
        ("Consulting SOW", ContractType.sow, ContractStatus.active, 90000, 90000, True, 30),
    ]
    contracts: list[Contract] = []
    for i, (title, ctype, status, total, limit, auto_renew, notice) in enumerate(contract_specs):
        vendor = vendors[i % len(vendors)]
        start = today - timedelta(days=300 - i * 20)
        # Active contracts end in the future; a couple end soon (renewal demo).
        if status == ContractStatus.active:
            end = today + timedelta(days=[20, 25, 200, 400][i % 4])
        elif status == ContractStatus.expired:
            end = today - timedelta(days=15)
        else:
            end = start + timedelta(days=365)
        c = _ent(
            Contract(
                organization_id=org_id,
                contract_number=f"CTR-2026-{i + 1:03d}",
                title=f"{title} — {vendor.name}",
                description=f"{ctype.value.upper()} contract with {vendor.name}.",
                contract_type=ctype,
                status=status,
                vendor_id=vendor.id,
                currency="USD",
                total_value=_q(Decimal(total)),
                spend_limit=_q(Decimal(limit)) if limit is not None else None,
                not_to_exceed=limit is not None,
                start_date=start,
                end_date=end,
                signed_date=start - timedelta(days=10) if status != ContractStatus.draft else None,
                auto_renew=auto_renew,
                renewal_term_months=12 if auto_renew else None,
                renewal_notice_days=notice,
                payment_terms=vendor.payment_terms or "Net 30",
                owner_user_id=actor_id,
                terms={
                    "allowed_gl_accounts": [g.code for g in gl_accounts[:3]],
                    "categories": [ctype.value],
                },
            )
        )
        contracts.append(c)
    session.add_all(contracts)
    await session.flush()

    # A few line items on the first three contracts.
    line_items: list[ContractLineItem] = []
    for c in contracts[:3]:
        for n in range(1, 3):
            unit = _q(Decimal("1500.00") * n)
            line_items.append(
                ContractLineItem(
                    contract_id=c.id,
                    line_number=n,
                    description=f"{c.title} — deliverable {n}",
                    quantity=Decimal("12"),
                    unit_price=unit,
                    total=_q(unit * 12),
                    gl_account=gl_accounts[n % len(gl_accounts)].code if gl_accounts else None,
                )
            )
    session.add_all(line_items)

    # Link a couple of invoices to active contracts for spend-to-contract.
    for inv, c in zip(
        invoices[:3], [c for c in contracts if c.status == ContractStatus.active][:3]
    ):
        inv.contract_id = c.id

    # ---- Credit memos -----------------------------------------------------
    memos: list[CreditMemo] = []
    cm_reasons = [
        "Damaged goods returned",
        "Volume rebate",
        "Pricing correction",
        "Short shipment",
        "Service credit — SLA miss",
    ]
    for i in range(10):
        vendor = vendors[i % len(vendors)]
        amount = _q(Decimal(str(75 + (i * 137) % 2000)) + Decimal("0.50"))
        if i < 4:
            status, applied_inv, applied_at, applied_by = "open", None, None, None
        elif i < 8:
            inv = invoices[i % len(invoices)] if invoices else None
            status = "applied"
            applied_inv = inv.id if inv else None
            applied_at = datetime(2026, 5, (i % 27) + 1, 12, 0, tzinfo=UTC)
            applied_by = "Marcus Manager"
        else:
            status, applied_inv, applied_at, applied_by = "void", None, None, None
        memos.append(
            _ent(
                CreditMemo(
                    organization_id=org_id,
                    memo_number=f"CM-2026-{i + 1:03d}",
                    vendor_id=vendor.id,
                    invoice_id=applied_inv,
                    amount=amount,
                    currency="USD",
                    issued_date=today - timedelta(days=60 - i * 5),
                    reason=cm_reasons[i % len(cm_reasons)],
                    status=status,
                    applied_at=applied_at,
                    applied_by=applied_by,
                )
            )
        )
    session.add_all(memos)

    # ---- Discount offers --------------------------------------------------
    # Sliding-scale early-pay offers across every lifecycle state + both scopes.
    def _tiers() -> list[dict]:
        return [
            {"days": 10, "percent": "2.00"},
            {"days": 20, "percent": "1.00"},
        ]

    offers: list[DiscountOffer] = []
    # Invoice-scoped offers on real invoices.
    inv_pool = invoices[:10] if invoices else []
    offer_plan = [
        (OFFER_STATUS_OFFERED, OFFER_SOURCE_SUPPLIER),
        (OFFER_STATUS_OFFERED, OFFER_SOURCE_SYSTEM),
        (OFFER_STATUS_ACCEPTED, OFFER_SOURCE_SUPPLIER),
        (OFFER_STATUS_CAPTURED, OFFER_SOURCE_SUPPLIER),
        (OFFER_STATUS_CAPTURED, OFFER_SOURCE_FINANCING),
        (OFFER_STATUS_DECLINED, OFFER_SOURCE_SUPPLIER),
        (OFFER_STATUS_EXPIRED, OFFER_SOURCE_SYSTEM),
    ]
    for i, (status, source) in enumerate(offer_plan):
        if not inv_pool:
            break
        inv = inv_pool[i % len(inv_pool)]
        base = _q(Decimal(inv.amount or Decimal("1000.00")))
        chosen = {"days": 10, "percent": "2.00"}
        captured = _q(base * Decimal("0.02")) if status == OFFER_STATUS_CAPTURED else None
        offers.append(
            _ent(
                DiscountOffer(
                    organization_id=org_id,
                    scope=OFFER_SCOPE_INVOICE,
                    invoice_id=inv.id,
                    vendor_id=inv.vendor_id,
                    source=source,
                    status=status,
                    tiers=_tiers(),
                    base_amount=base,
                    currency=inv.currency or "USD",
                    valid_from=today - timedelta(days=5),
                    valid_until=today + timedelta(days=15),
                    accepted_tier=chosen
                    if status in (OFFER_STATUS_ACCEPTED, OFFER_STATUS_CAPTURED)
                    else None,
                    accepted_at=datetime(2026, 6, (i % 27) + 1, 9, 0, tzinfo=UTC)
                    if status in (OFFER_STATUS_ACCEPTED, OFFER_STATUS_CAPTURED)
                    else None,
                    accepted_by=actor_id
                    if status in (OFFER_STATUS_ACCEPTED, OFFER_STATUS_CAPTURED)
                    else None,
                    captured_amount=captured,
                    captured_at=datetime(2026, 6, (i % 27) + 1, 10, 0, tzinfo=UTC)
                    if status == OFFER_STATUS_CAPTURED
                    else None,
                    financing_provider="mock" if source == OFFER_SOURCE_FINANCING else None,
                    notes="Early-payment offer (seed demo).",
                )
            )
        )
    # Vendor-scoped bulk offers across a vendor's open balance.
    for i, vendor in enumerate(vendors[:3]):
        offers.append(
            _ent(
                DiscountOffer(
                    organization_id=org_id,
                    scope=OFFER_SCOPE_VENDOR,
                    vendor_id=vendor.id,
                    source=OFFER_SOURCE_SUPPLIER,
                    status=OFFER_STATUS_OFFERED,
                    tiers=_tiers(),
                    base_amount=_q(Decimal(str(8000 + i * 4500))),
                    currency="USD",
                    valid_from=today - timedelta(days=2),
                    valid_until=today + timedelta(days=20),
                    notes=f"Bulk early-pay negotiation across {vendor.name} open invoices.",
                )
            )
        )
    session.add_all(offers)

    # ---- Recurring / subscription invoice templates -----------------------
    # A small spread so the `/recurring` page isn't empty: a couple of active
    # monthly templates (SaaS + lease), a quarterly active one, and a paused
    # one. `next_run_on` is set to a near-future date so the upcoming-schedule
    # preview + generate-now have a live period to project; the sweep itself is
    # off by default (FEOH_RECURRING_INVOICES_ENABLED=false) so nothing fires in
    # local dev. Money is exact (Numeric(15, 2)).
    recurring_specs = [
        # (name, cadence, status, amount, gl_idx, day_of_period, terms)
        ("Cloud hosting subscription", CADENCE_MONTHLY, STATUS_ACTIVE, "2400.00", 0, 1, "Net 30"),
        ("Office lease — HQ", CADENCE_MONTHLY, STATUS_ACTIVE, "12000.00", 1, 1, "Net 15"),
        ("Quarterly support retainer", CADENCE_QUARTERLY, STATUS_ACTIVE, "9000.00", 2, 5, "Net 30"),
        ("Annual insurance premium", CADENCE_ANNUAL, STATUS_ACTIVE, "18000.00", 0, 10, "Net 45"),
        ("Paused — legacy SaaS seats", CADENCE_MONTHLY, STATUS_PAUSED, "750.00", 1, 1, "Net 30"),
    ]
    recurring: list[RecurringInvoiceTemplate] = []
    for i, (name, cadence, status, amount, gl_idx, day, terms) in enumerate(recurring_specs):
        vendor = vendors[i % len(vendors)]
        gl = gl_accounts[gl_idx % len(gl_accounts)].code if gl_accounts else None
        # next_run_on: active templates roll in the next few days; paused get none.
        next_run = (today + timedelta(days=3 + i)) if status == STATUS_ACTIVE else None
        recurring.append(
            _ent(
                RecurringInvoiceTemplate(
                    organization_id=org_id,
                    name=name,
                    vendor_id=vendor.id,
                    vendor_name=vendor.name,
                    description=f"{name} — {vendor.name} (recurring).",
                    amount=_q(Decimal(amount)),
                    currency="USD",
                    gl_account=gl,
                    payment_terms=terms,
                    cadence=cadence,
                    day_of_period=day,
                    start_date=today - timedelta(days=90),
                    next_run_on=next_run,
                    status=status,
                    variance_tolerance_pct=Decimal("5.00"),
                    notes="Seed demo recurring template.",
                    # The employee whose standing instruction this is. The sweep
                    # has no live actor, so `generate_one` falls back to this
                    # when stamping the generated invoice's `uploaded_by_id` —
                    # seeding it keeps the demo data on the post-0096 shape and
                    # makes segregation of duties actually demonstrable locally.
                    created_by_user_id=actor_id,
                )
            )
        )
    session.add_all(recurring)

    # ---- Expense policies -------------------------------------------------
    policies = [
        _ent(
            ExpensePolicy(
                organization_id=org_id,
                name="Travel per-diem",
                active=True,
                category="Travel",
                per_diem_amount=_q(Decimal("75.00")),
                requires_receipt_above=_q(Decimal("25.00")),
                requires_preapproval_above=_q(Decimal("1000.00")),
            )
        ),
        _ent(
            ExpensePolicy(
                organization_id=org_id,
                name="Mileage reimbursement",
                active=True,
                category="Mileage",
                mileage_rate=Decimal("0.6700"),
            )
        ),
        _ent(
            ExpensePolicy(
                organization_id=org_id,
                name="Meals & entertainment cap",
                active=True,
                category="Meals",
                category_limit=_q(Decimal("150.00")),
                requires_receipt_above=_q(Decimal("25.00")),
            )
        ),
    ]
    session.add_all(policies)

    # ---- Expense reports + expenses --------------------------------------
    expense_gl = gl_accounts[0].id if gl_accounts else None
    report_specs = [
        ("Q2 client travel", ExpenseReportStatus.approved),
        ("June team offsite", ExpenseReportStatus.submitted),
        ("Conference attendance", ExpenseReportStatus.reimbursed),
        ("Software & subscriptions", ExpenseReportStatus.draft),
        ("Rejected — missing receipts", ExpenseReportStatus.rejected),
    ]
    expense_categories = ["Travel", "Meals", "Lodging", "Software", "Mileage", "Office"]
    merchants = ["United Airlines", "Marriott", "Olive Garden", "GitHub", "Uber", "Staples"]
    reports: list[ExpenseReport] = []
    all_expenses: list[Expense] = []
    for ri, (title, status) in enumerate(report_specs):
        report = _ent(
            ExpenseReport(
                organization_id=org_id,
                report_number=f"EXP-2026-{ri + 1:03d}",
                title=title,
                employee_user_id=actor_id or uuid.uuid4(),
                status=status,
                submitted_at=datetime(2026, 6, (ri % 27) + 1, 8, 0, tzinfo=UTC)
                if status != ExpenseReportStatus.draft
                else None,
                approved_at=datetime(2026, 6, (ri % 27) + 2, 8, 0, tzinfo=UTC)
                if status in (ExpenseReportStatus.approved, ExpenseReportStatus.reimbursed)
                else None,
                approved_by=actor_id
                if status in (ExpenseReportStatus.approved, ExpenseReportStatus.reimbursed)
                else None,
                currency="USD",
            )
        )
        reports.append(report)
    session.add_all(reports)
    await session.flush()

    estatus_for_report = {
        ExpenseReportStatus.approved: ExpenseStatus.approved,
        ExpenseReportStatus.reimbursed: ExpenseStatus.reimbursed,
        ExpenseReportStatus.submitted: ExpenseStatus.submitted,
        ExpenseReportStatus.rejected: ExpenseStatus.rejected,
        ExpenseReportStatus.draft: ExpenseStatus.draft,
    }
    line_no = 0
    for report, (_, rstatus) in zip(reports, report_specs):
        total = Decimal("0.00")
        for n in range(4):
            cat = expense_categories[line_no % len(expense_categories)]
            amount = _q(Decimal(str(40 + (line_no * 53) % 600)) + Decimal("0.25"))
            total += amount
            all_expenses.append(
                _ent(
                    Expense(
                        organization_id=org_id,
                        report_id=report.id,
                        expense_date=today - timedelta(days=30 - line_no),
                        merchant=merchants[line_no % len(merchants)],
                        category=cat,
                        description=f"{cat} expense — {merchants[line_no % len(merchants)]}",
                        amount=amount,
                        currency="USD",
                        gl_account_id=expense_gl,
                        payment_method=ExpensePaymentMethod.corporate_card
                        if line_no % 2
                        else ExpensePaymentMethod.out_of_pocket,
                        status=estatus_for_report[rstatus],
                        reimbursable=True,
                        mileage_miles=Decimal("42.0") if cat == "Mileage" else None,
                    )
                )
            )
            line_no += 1
        report.total_amount = _q(total)
    # A few un-reported standalone expenses (draft, not yet grouped).
    for n in range(3):
        cat = expense_categories[n % len(expense_categories)]
        all_expenses.append(
            _ent(
                Expense(
                    organization_id=org_id,
                    expense_date=today - timedelta(days=n + 1),
                    merchant=merchants[n % len(merchants)],
                    category=cat,
                    description=f"Unsubmitted {cat.lower()} expense",
                    amount=_q(Decimal(str(60 + n * 45)) + Decimal("0.75")),
                    currency="USD",
                    gl_account_id=expense_gl,
                    payment_method=ExpensePaymentMethod.out_of_pocket,
                    status=ExpenseStatus.draft,
                    reimbursable=True,
                )
            )
        )
    session.add_all(all_expenses)
    await session.flush()

    # ---- Vendor statement reconciliation ---------------------------------
    recon_runs = await seed_vendor_statement_recon(session, org_id)

    tally = {
        "contracts": len(contracts),
        "credit_memos": len(memos),
        "discount_offers": len(offers),
        "recurring_templates": len(recurring),
        "expense_policies": len(policies),
        "expense_reports": len(reports),
        "expenses": len(all_expenses),
        "statement_recons": recon_runs,
    }
    print(
        "  Seeded extras: "
        + ", ".join(f"{k}={v}" for k, v in tally.items())
        + (" (entity_id set)" if default_entity_id is not None else " (entity backfill pending)")
    )
    return tally


async def seed_vendor_statement_recon(session, org_id: uuid.UUID) -> int:
    """Add one vendor-statement-reconciliation run to a tenant so the
    `/vendor-statements` page isn't empty on a freshly seeded tenant.

    Picks an existing vendor that has a couple of open invoices, hand-builds a
    small supplier statement (lines that match those invoices, plus one phantom
    line that doesn't), and runs the **real** `reconcile` engine — then persists
    the run + its per-line results exactly like the API's
    `api.vendor_statement_recon._create_run`. The phantom line yields a genuine
    `missing_on_our_side` row, so the actionable review queue + close-readiness
    have real data.

    Idempotent: bails (returns 0) if the tenant already has a reconciliation
    run. Additive + skip-if-exists, like the rest of `seed_extras`. Does **not**
    commit — the caller owns the transaction. Returns the number of runs created
    (0 or 1).
    """
    existing = (
        await session.execute(text("SELECT count(*) FROM vendor_statement_reconciliations"))
    ).scalar()
    if existing and existing > 0:
        print("  Vendor statement recon already seeded. Skipping.")
        return 0

    default_entity_id = (
        await session.execute(text("SELECT id FROM entities WHERE is_default LIMIT 1"))
    ).scalar()
    actor_id = await _actor_user_id(org_id)
    today = date.today()

    # Find a vendor with at least two non-settled invoices to reconcile against.
    vendor = None
    ledger_invoices: list[recon.LedgerInvoice] = []
    vendors = list(
        (
            await session.execute(
                select(Vendor).where(Vendor.status == "active").order_by(Vendor.name).limit(20)
            )
        )
        .scalars()
        .all()
    )
    for cand in vendors:
        invoices = list(
            (
                await session.execute(
                    select(Invoice)
                    .where(
                        Invoice.vendor_id == cand.id,
                        Invoice.status.notin_(("paid", "done")),
                    )
                    .order_by(Invoice.invoice_number)
                    .limit(3)
                )
            )
            .scalars()
            .all()
        )
        if len(invoices) >= 2:
            vendor = cand
            ledger_invoices = [
                recon.LedgerInvoice(
                    id=inv.id,
                    invoice_number=inv.invoice_number,
                    amount=inv.amount,
                    invoice_date=inv.invoice_date,
                    currency=inv.currency or "USD",
                    status=str(inv.status),
                )
                for inv in invoices
            ]
            break

    if vendor is None:
        print("  No vendor with ≥2 open invoices — run scripts/seed.py first. Skipping recon.")
        return 0

    # Hand-build the supplier's statement: the first ledger invoice matches
    # exactly, the second is short-paid by $50 (→ amount_mismatch), and a third
    # phantom line has no matching invoice (→ missing_on_our_side). Any ledger
    # invoice the statement omits becomes missing_on_their_side automatically.
    statement_lines = [
        recon.StatementLine(
            invoice_number=ledger_invoices[0].invoice_number,
            invoice_date=ledger_invoices[0].invoice_date,
            amount=_q(ledger_invoices[0].amount),
            status="open",
            raw={"source": "seed"},
        ),
        recon.StatementLine(
            invoice_number=ledger_invoices[1].invoice_number,
            invoice_date=ledger_invoices[1].invoice_date,
            amount=_q(ledger_invoices[1].amount + Decimal("50.00")),
            status="open",
            raw={"source": "seed"},
        ),
        recon.StatementLine(
            invoice_number="SUP-PHANTOM-001",
            invoice_date=today - timedelta(days=20),
            amount=_q(Decimal("1875.00")),
            status="open",
            raw={"source": "seed"},
        ),
    ]

    results, summary = recon.reconcile(statement_lines, ledger_invoices)

    run = VendorStatementReconciliation(
        organization_id=org_id,
        entity_id=default_entity_id,
        vendor_id=vendor.id,
        vendor_name=vendor.name,
        statement_date=today,
        statement_reference="STMT-2026-06",
        currency="USD",
        source_format=SOURCE_MANUAL,
        file_key=None,
        status=STATUS_OPEN,
        statement_total=summary.statement_total,
        ledger_total=summary.ledger_total,
        line_count=summary.line_count,
        matched_count=summary.matched_count,
        amount_mismatch_count=summary.amount_mismatch_count,
        missing_our_side_count=summary.missing_our_side_count,
        missing_their_side_count=summary.missing_their_side_count,
        notes="Seed demo statement reconciliation.",
        created_by=actor_id,
    )
    session.add(run)
    await session.flush()

    for r in results:
        session.add(
            VendorStatementReconLine(
                reconciliation_id=run.id,
                organization_id=org_id,
                entity_id=default_entity_id,
                statement_invoice_number=r.statement_invoice_number,
                statement_date=r.statement_date,
                statement_amount=r.statement_amount,
                statement_status=r.statement_status,
                classification=r.classification,
                matched_invoice_id=r.matched_invoice_id,
                ledger_amount=r.ledger_amount,
                amount_difference=r.amount_difference,
                match_method=r.match_method,
                resolution_status=RESOLUTION_UNRESOLVED,
                raw=r.raw,
            )
        )
    await session.flush()

    print(
        f"  Seeded vendor statement recon: 1 run for {vendor.name} "
        f"(matched={summary.matched_count}, amount_mismatch={summary.amount_mismatch_count}, "
        f"missing_our_side={summary.missing_our_side_count}, "
        f"missing_their_side={summary.missing_their_side_count})"
    )
    return 1


async def _run(db_name: str) -> None:
    async with control_session_factory() as ctrl:
        org = (
            await ctrl.execute(select(Organization).where(Organization.db_name == db_name))
        ).scalar_one_or_none()
    if org is None:
        print(f"FAIL: no organization with db_name={db_name!r}. Run scripts/seed.py first.")
        return

    engine = create_async_engine(_make_tenant_url(db_name))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await seed_extras(session, org.id)
            # seed_extras short-circuits when contracts already exist, so on an
            # already-seeded tenant top up the statement-recon page separately
            # (it has its own skip-if-exists guard).
            await seed_vendor_statement_recon(session, org.id)
            await session.commit()
    finally:
        await engine.dispose()
    print(
        f"Done. Visit http://{org.slug}.localhost:7777/contracts "
        "(and /credit-memos, /discounts, /recurring, /expenses, /vendor-statements)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="feoh_acme", help="Tenant DB name (default: feoh_acme)")
    args = parser.parse_args()
    asyncio.run(_run(args.tenant))


if __name__ == "__main__":
    main()
