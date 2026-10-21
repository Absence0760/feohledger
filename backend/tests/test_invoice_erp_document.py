"""The bill an invoice was posted as, on the invoice and in its API response.

Found in a live Xero test: FEOH-INV-001 was pushed and Xero returned its
InvoiceID, but FeohLedger kept it only in ``workflow_instances.state_data`` and
the ``invoice.erp_confirmed`` audit row, so nothing a user reads led from the
invoice to the bill. Pinned here:

* ``erp_adapters.deep_links`` — the one provider -> URL mapping (Xero, both
  QuickBooks hosts, no link for Merge.dev / the mock / an empty id, the id
  URL-quoted);
* on real Postgres: a confirmed push stores the id and link on the invoice row,
  and ``GET /api/invoices/{id}`` returns both.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.invoice import Invoice, InvoiceStatus
from app.services.erp_adapters.base import ErpPostResult
from app.services.erp_adapters.deep_links import erp_document_url

XERO = {"type": "xero", "integration_method": "direct"}
BILL = "8f1c2a3b-0000-4000-8000-1234567890ab"


# ---------------------------------------------------------------------------
# the mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "doc", "expected"),
    [
        (XERO, BILL, f"https://go.xero.com/AccountsPayable/View.aspx?InvoiceID={BILL}"),
        (
            {"type": "quickbooks_online", "integration_method": "direct"},
            "146",
            "https://app.qbo.intuit.com/app/bill?txnId=146",
        ),
        (
            {
                "type": "quickbooks_online",
                "integration_method": "direct",
                "environment": "sandbox",
            },
            "146",
            "https://app.sandbox.qbo.intuit.com/app/bill?txnId=146",
        ),
        # Merge.dev wins over `type`, and its id is Merge's, not Xero's.
        ({"type": "xero"}, BILL, None),
        ({"type": "mock", "integration_method": "direct"}, "MOCK-1", None),
        ({"type": "netsuite", "integration_method": "direct"}, "77", None),
        (XERO, None, None),
        (XERO, "", None),
        # An id is a path-free query value, whatever the ERP sent.
        (
            {"type": "quickbooks_online", "integration_method": "direct"},
            "1&x=<y>",
            "https://app.qbo.intuit.com/app/bill?txnId=1%26x%3D%3Cy%3E",
        ),
        (XERO, "a/b?c#d", "https://go.xero.com/AccountsPayable/View.aspx?InvoiceID=a%2Fb%3Fc%23d"),
    ],
)
def test_erp_document_url(config, doc, expected):
    assert erp_document_url(config, doc) == expected


# ---------------------------------------------------------------------------
# end to end on a real tenant
# ---------------------------------------------------------------------------


class _XeroLike:
    async def post_invoice(self, payload):
        return ErpPostResult(success=True, erp_document_id=BILL, erp_document_number="INV-9")


async def test_a_confirmed_push_is_on_the_invoice_and_its_api_response(realdb):
    from app.services.erp import send_to_erp_internal

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    inv_id = uuid.uuid4()
    async with mk() as s:
        s.add(
            Invoice(
                id=inv_id,
                organization_id=info.org_id,
                invoice_number="FEOH-INV-001",
                vendor_name="Northwind Supplies",
                amount=Decimal("1234.56"),
                currency="USD",
                status=InvoiceStatus.sending_to_erp,
                uploaded_by_id=None,  # seeded directly: no employee created it
                approved_by="Approver",
            )
        )
        await s.commit()

    async with realdb.client(key="a", role="ap_manager") as c:
        before = await c.get(f"/api/invoices/{inv_id}")
    assert before.status_code == 200, before.text
    assert before.json()["erp_document_id"] is None
    assert before.json()["erp_document_url"] is None

    async with mk() as s:
        inv = await s.get(Invoice, inv_id)
        with patch("app.services.erp.get_erp_adapter", return_value=_XeroLike()):
            await send_to_erp_internal(s, inv, actor_id=None, erp_config=XERO)

    link = f"https://go.xero.com/AccountsPayable/View.aspx?InvoiceID={BILL}"
    async with mk() as s:
        row = await s.get(Invoice, inv_id)
        assert row.status == InvoiceStatus.done
        assert (row.erp_document_id, row.erp_document_url) == (BILL, link)

    async with realdb.client(key="a", role="ap_manager") as c:
        after = await c.get(f"/api/invoices/{inv_id}")
    assert after.status_code == 200, after.text
    body = after.json()
    assert (body["erp_document_id"], body["erp_document_url"]) == (BILL, link)
