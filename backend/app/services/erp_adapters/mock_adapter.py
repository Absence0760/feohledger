"""Mock ERP adapter for development and testing."""

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from app.services.erp_adapters.base import (
    ErpAdapter,
    ErpInvoiceStatus,
    ErpPostResult,
    GLAccountPayload,
    InvoicePayload,
    PoLinePayload,
    PoPayload,
    VendorPayload,
)
from app.services.erp_adapters.dispatcher import register_adapter

# Deterministic mock PO catalogue. Used by the /sync-erp endpoint when
# the org is configured against the mock ERP — keeps the demo flow
# working without standing up a real ERP. Vendor names match the seed
# script so vendor linking succeeds out of the box on the acme tenant.
# Each PO states its currency the way a real ERP record does (Merge's unified
# model carries one); the sync stores it on `PurchaseOrder.currency`.
_MOCK_POS: list[PoPayload] = [
    PoPayload(
        po_number="PO-2024-200",
        currency="USD",
        vendor_name="Office Supplies Co",
        total=Decimal("2500.00"),
        status="open",
        expected_delivery_date=date(2024, 6, 15),
        line_items=[
            PoLinePayload(
                description="Printer paper - bulk",
                quantity=Decimal("20"),
                unit_price=Decimal("45.00"),
                total=Decimal("900.00"),
            ),
            PoLinePayload(
                description="Ink cartridges",
                quantity=Decimal("10"),
                unit_price=Decimal("80.00"),
                total=Decimal("800.00"),
            ),
            PoLinePayload(
                description="Desk organizers",
                quantity=Decimal("16"),
                unit_price=Decimal("50.00"),
                total=Decimal("800.00"),
            ),
        ],
    ),
    PoPayload(
        po_number="PO-2024-201",
        currency="USD",
        vendor_name="Cloud Services Inc",
        total=Decimal("15000.00"),
        status="open",
        expected_delivery_date=date(2024, 7, 1),
        line_items=[
            PoLinePayload(
                description="Annual SaaS license",
                quantity=Decimal("1"),
                unit_price=Decimal("12000.00"),
                total=Decimal("12000.00"),
            ),
            PoLinePayload(
                description="Premium support addon",
                quantity=Decimal("1"),
                unit_price=Decimal("3000.00"),
                total=Decimal("3000.00"),
            ),
        ],
    ),
    PoPayload(
        po_number="PO-2024-202",
        currency="USD",
        vendor_name="Tech Hardware Corp",
        total=Decimal("24000.00"),
        status="open",
        # Deliberately no expected_delivery_date — a real ERP often omits the
        # promised date, so the catalogue keeps one PO without it to exercise
        # the "leave None, don't fabricate" branch end-to-end.
        line_items=[
            PoLinePayload(
                description="Laptop Model X Pro",
                quantity=Decimal("10"),
                unit_price=Decimal("2400.00"),
                total=Decimal("24000.00"),
            ),
        ],
    ),
]


# Demo chart of accounts. Used by `MockAdapter.list_gl_accounts` so the
# /api/gl-accounts/sync-erp flow has something realistic to populate
# when an org is wired against the mock ERP. `erp_account_id` mirrors
# the code so subsequent pulls are idempotent.
_MOCK_GL_ACCOUNTS: list[dict] = [
    {
        "code": "1000",
        "name": "Cash and Cash Equivalents",
        "account_type": "asset",
        "erp_account_id": "1000",
    },
    {
        "code": "1200",
        "name": "Accounts Receivable",
        "account_type": "asset",
        "erp_account_id": "1200",
    },
    {
        "code": "1500",
        "name": "Fixed Assets - Equipment",
        "account_type": "asset",
        "erp_account_id": "1500",
    },
    {
        "code": "2000",
        "name": "Accounts Payable",
        "account_type": "liability",
        "erp_account_id": "2000",
    },
    {
        "code": "2100",
        "name": "Accrued Liabilities",
        "account_type": "liability",
        "erp_account_id": "2100",
    },
    {"code": "3000", "name": "Owner's Equity", "account_type": "equity", "erp_account_id": "3000"},
    {
        "code": "4000",
        "name": "Revenue - Services",
        "account_type": "revenue",
        "erp_account_id": "4000",
    },
    {
        "code": "4100",
        "name": "Revenue - Products",
        "account_type": "revenue",
        "erp_account_id": "4100",
    },
    {
        "code": "6100",
        "name": "Office Supplies & Expenses",
        "account_type": "expense",
        "erp_account_id": "6100",
    },
    {
        "code": "6200",
        "name": "Software & Cloud Services",
        "account_type": "expense",
        "erp_account_id": "6200",
    },
    {
        "code": "6300",
        "name": "Facilities & Maintenance",
        "account_type": "expense",
        "erp_account_id": "6300",
    },
    {
        "code": "6400",
        "name": "Marketing & Advertising",
        "account_type": "expense",
        "erp_account_id": "6400",
    },
    {
        "code": "6500",
        "name": "Legal & Professional Fees",
        "account_type": "expense",
        "erp_account_id": "6500",
    },
    {
        "code": "6600",
        "name": "Meals & Entertainment",
        "account_type": "expense",
        "erp_account_id": "6600",
    },
    {
        "code": "6700",
        "name": "Shipping & Freight",
        "account_type": "expense",
        "erp_account_id": "6700",
    },
    {
        "code": "6800",
        "name": "Travel & Transportation",
        "account_type": "expense",
        "erp_account_id": "6800",
    },
    {
        "code": "6900",
        "name": "Utilities & Telecom",
        "account_type": "expense",
        "erp_account_id": "6900",
    },
    {"code": "7000", "name": "Insurance", "account_type": "expense", "erp_account_id": "7000"},
    {
        "code": "7100",
        "name": "Depreciation & Amortization",
        "account_type": "expense",
        "erp_account_id": "7100",
    },
    {
        "code": "8000",
        "name": "Payroll Expense",
        "account_type": "expense",
        "erp_account_id": "8000",
    },
]


# Deterministic mock vendor catalogue. Used by the /api/vendors/sync-erp
# endpoint when the org is configured against the mock ERP — preserves the
# exact local-dev behavior of the pre-`list_vendors()` hardcoded list.
_MOCK_VENDORS: list[VendorPayload] = [
    VendorPayload(
        erp_vendor_id="ERP-V001",
        name="Office Supplies Co",
        code="OSC",
        email="ap@officesupplies.com",
        payment_terms="Net 30",
    ),
    VendorPayload(
        erp_vendor_id="ERP-V002",
        name="Cloud Services Inc",
        code="CSI",
        email="billing@cloudservices.com",
        payment_terms="Net 20",
    ),
]


@register_adapter("mock")
class MockAdapter(ErpAdapter):
    erp_type = "mock"

    async def post_invoice(self, payload: InvoicePayload) -> ErpPostResult:
        await asyncio.sleep(0.1)  # simulate latency
        doc_id = f"MOCK-{uuid.uuid4().hex[:12].upper()}"
        return ErpPostResult(
            success=True,
            erp_document_id=doc_id,
            erp_document_number=doc_id,
            message="Mock ERP accepted invoice",
        )

    async def get_invoice_status(self, erp_document_id: str) -> ErpInvoiceStatus:
        await asyncio.sleep(0.05)
        return ErpInvoiceStatus.open

    async def void_invoice(self, erp_document_id: str) -> bool:
        await asyncio.sleep(0.05)
        return True

    async def list_gl_accounts(self) -> list[GLAccountPayload]:
        await asyncio.sleep(0.05)
        return [GLAccountPayload(**a) for a in _MOCK_GL_ACCOUNTS]

    async def list_pos(self) -> list[PoPayload]:
        await asyncio.sleep(0.05)
        # Return copies so the caller can't mutate the shared catalogue.
        return [
            PoPayload(
                po_number=p.po_number,
                vendor_name=p.vendor_name,
                total=p.total,
                status=p.status,
                expected_delivery_date=p.expected_delivery_date,
                currency=p.currency,
                line_items=[
                    PoLinePayload(
                        description=li.description,
                        quantity=li.quantity,
                        unit_price=li.unit_price,
                        total=li.total,
                        gl_account=li.gl_account,
                    )
                    for li in p.line_items
                ],
            )
            for p in _MOCK_POS
        ]

    async def list_vendors(self) -> list[VendorPayload]:
        await asyncio.sleep(0.05)
        # Return copies so the caller can't mutate the shared catalogue.
        return [
            VendorPayload(
                erp_vendor_id=v.erp_vendor_id,
                name=v.name,
                code=v.code,
                email=v.email,
                phone=v.phone,
                address=v.address,
                tax_id=v.tax_id,
                payment_terms=v.payment_terms,
            )
            for v in _MOCK_VENDORS
        ]

    async def test_connection(self) -> bool:
        return True
