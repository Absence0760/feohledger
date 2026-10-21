"""Production-shaped seed rows (#575 § 2).

The demo seed's strings are short and tidy — ``Office Supplies Co``,
``INV-2024-001``, ``demo@acme.com`` — and every vendor it links an invoice to
is verified. A real tenant's first day is not like that: the first invoice comes
from a supplier nobody has heard of (so its vendor is auto-created
*unverified*), the supplier's legal name is ninety characters of ERP export,
half of it is not ASCII, the invoice number has no natural break point, the
lines are net of tax with the tax stated once on the header, and the assignee is
a routing alias with a long subsidiary domain. Each of those hid a defect behind
the tidy fixtures (#573, #572, #574 and the PDF glyph boxes this module's tests
found), so the seed now carries one of each.

The VALUES are not spelled here. They live in ``fixtures/realistic_rows.json``,
which the Playwright layout specs (``frontend/tests-e2e/fixtures/realisticRows.ts``)
and the day-one journey (``frontend/tests-e2e/day-one/fixtures.ts``) read too, so
a layout test measures exactly the strings a developer sees in the seeded app.

Two entry points, one per seed shape:

- :func:`add_realistic_rows` — the full seed. ADDS vendors, invoices (one of
  them tax-exclusive with its line items), two open exceptions with a long
  assignee and a credit memo. Returns the invoices so ``seed_tenant`` threads
  them through its workflow + audit back-fill like every other invoice.
- :func:`realistic_lean_names` / :func:`tax_exclusive_lines` — the lean (CI e2e)
  seed. That one RESHAPES rows it already creates instead of adding any: the
  e2e suite reads row counts off the lean tenants, and an extra vendor or
  invoice would move every one of them. See ``seed.seed_tenant_lean``.

Never commits; the caller owns the transaction.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below —
# the same prologue as `seed.py` / `seed_extras.py`, for the same reason (a
# worktree reusing the primary checkout's venv otherwise resolves `app` from the
# OTHER tree). Inserted at 1 so `scripts/` keeps priority; idempotent.
# Deliberately NOT hoisted into a variable: ruff's E402 exempts `sys.path`
# manipulation before imports but not an assignment beside it.
# See `frontend/tests-e2e/README.md` § Running from a worktree.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.models.credit_memo import CreditMemo
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceLineItem
from app.models.vendor import Vendor

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "realistic_rows.json"


def load_realistic_rows() -> dict[str, Any]:
    """The shared value set. Read fresh each call so a test can't mutate a cache."""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def assignee_email(rows: dict[str, Any] | None = None) -> str:
    """The routing alias the seed assigns exceptions to (the e2e fixture adds a
    per-tenant `+tag` to the same local part and domain)."""
    rows = rows or load_realistic_rows()
    return f"{rows['assignee_local_part']}@{rows['mailbox_domain']}"


def tax_exclusive_lines(invoice_id: uuid.UUID, spec: dict[str, Any]) -> list[InvoiceLineItem]:
    """The tax-exclusive invoice's lines: each ``total`` is NET, so they sum to
    the header ``subtotal`` — not to ``amount``, which carries the tax."""
    return [
        InvoiceLineItem(
            invoice_id=invoice_id,
            line_number=n,
            description=line["description"],
            quantity=Decimal(line["quantity"]),
            unit_price=Decimal(line["unit_price"]),
            total=Decimal(line["total"]),
        )
        for n, line in enumerate(spec["lines"], start=1)
    ]


def tax_exclusive_header(spec: dict[str, Any]) -> dict[str, Any]:
    """Header columns of the tax-exclusive invoice, as `Invoice(**...)` kwargs."""
    return {
        "amount": Decimal(spec["amount"]),
        "subtotal": Decimal(spec["subtotal"]),
        "tax_amount": Decimal(spec["tax_amount"]),
        "tax_rate": Decimal(spec["tax_rate"]),
        "currency": spec["currency"],
    }


def realistic_lean_names() -> dict[str, str]:
    """Names the lean seed gives its existing vendors (see the module docstring)."""
    rows = load_realistic_rows()
    return {
        "long_vendor": rows["long_vendor_name"],
        "non_ascii_vendor": rows["non_ascii_vendor_name"],
        "long_invoice_number": rows["invoice_numbers"][0],
        "assignee_email": assignee_email(rows),
    }


async def add_realistic_rows(session, org_id: uuid.UUID, *, today: date) -> list[Invoice]:
    """Add the production-shaped rows to a full-seed tenant. Returns the invoices.

    - a long-named vendor (active) with a `new` invoice;
    - a non-ASCII vendor, UNVERIFIED, with an `approved` invoice in the payment
      queue and a `new` one — every invoice carrying an ERP-shaped number;
    - the tax-exclusive invoice from a supplier seen for the first time
      (unverified), sitting in `ready_for_review` with its net lines and a PO
      number the tenant has no PO for;
    - open exceptions on those two assigned to the long routing alias;
    - an open credit memo with a long memo number against the long vendor.

    Amounts are large but not the `Numeric(15, 2)` ceiling the layout spec
    uses: this is the demo a developer clicks around, and a 13-digit invoice
    would swamp every dashboard total in it.
    """
    rows = load_realistic_rows()
    tax = rows["tax_exclusive_invoice"]
    email = assignee_email(rows)

    v_long = Vendor(
        organization_id=org_id,
        name=rows["long_vendor_name"],
        email=f"{rows['remittance_local_part']}@{rows['mailbox_domain']}",
        payment_terms="Net 45",
        status="active",
        source="manual",
    )
    v_non_ascii = Vendor(
        organization_id=org_id,
        name=rows["non_ascii_vendor_name"],
        status="unverified",
        source="ai_extracted",
    )
    v_new_supplier = Vendor(
        organization_id=org_id,
        name=tax["vendor_name"],
        address="Königsallee 92a, 40212 Düsseldorf, DE",
        status="unverified",
        source="ai_extracted",
    )
    session.add_all([v_long, v_non_ascii, v_new_supplier])
    await session.flush()

    numbers = rows["invoice_numbers"]
    amounts = rows["amounts"]

    def _invoice(vendor: Vendor, number: str, status: str, **extra) -> Invoice:
        return Invoice(
            organization_id=org_id,
            vendor_id=vendor.id,
            vendor_name=vendor.name,
            invoice_number=number,
            invoice_date=today - timedelta(days=12),
            received_date=today - timedelta(days=10),
            due_date=today + timedelta(days=18),
            payment_terms="Net 30",
            status=status,
            **({"amount": Decimal(amounts[1]), "currency": "USD"} | extra),
        )

    # The payment-queue row is the UNVERIFIED vendor's: #573 hid "Record as
    # paid" for exactly that row, and a tidy seed never had one.
    inv_queue = _invoice(
        v_non_ascii, numbers[0], "approved", approved_by="Marcus Manager", approval_date=today
    )
    inv_long_new = _invoice(v_long, numbers[2], "new", amount=Decimal(amounts[2]))
    inv_non_ascii = _invoice(v_non_ascii, numbers[1], "new", amount=Decimal("48210.00"))
    inv_tax = _invoice(
        v_new_supplier,
        tax["invoice_number"],
        "ready_for_review",
        po_number=tax["po_number"],
        assigned_to=rows["assignee_name"],
        **tax_exclusive_header(tax),
    )
    invoices = [inv_queue, inv_long_new, inv_non_ascii, inv_tax]
    session.add_all(invoices)
    await session.flush()

    session.add_all(tax_exclusive_lines(inv_tax.id, tax))
    session.add_all(
        [
            APException(
                organization_id=org_id,
                invoice_id=inv_tax.id,
                exception_type="po_mismatch",
                severity="warning",
                status="open",
                description=f"PO {tax['po_number']} {rows['exception_description']}",
                assigned_to=email,
            ),
            APException(
                organization_id=org_id,
                invoice_id=inv_non_ascii.id,
                exception_type="unverified_vendor",
                severity="warning",
                status="open",
                description=f"Invoice linked to unverified vendor: {v_non_ascii.name}",
                assigned_to=email,
            ),
            CreditMemo(
                organization_id=org_id,
                memo_number=rows["memo_number"],
                vendor_id=v_long.id,
                amount=Decimal("1250.00"),
                currency="USD",
                issued_date=today - timedelta(days=3),
                reason=rows["memo_reason"],
                status="open",
            ),
        ]
    )
    await session.flush()
    return invoices
