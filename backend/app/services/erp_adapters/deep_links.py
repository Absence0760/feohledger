"""Deep links from a FeohLedger invoice to its bill in the ERP.

The one provider -> URL mapping. ``services/erp._call_erp`` calls
:func:`erp_document_url` when a push is confirmed and stores the result on
``invoices.erp_document_url``; the invoice modal renders whatever is stored, so
no client carries its own copy of these URL shapes.

It runs at push time, not on read, because the link depends on the adapter
config that posted the bill (QuickBooks sandbox and production companies live
on different hosts), and an org may change ERP after the bill was posted.

Only providers whose URL is stable and addressable by the document id alone
get a link. Everything else (Merge.dev, whose id is Merge's own common-model
id; the mock; providers whose web UI keys documents differently) returns None,
and the modal shows the id without a link.

* Xero — ``https://go.xero.com/AccountsPayable/View.aspx?InvoiceID=<InvoiceID>``.
  Opens in the user's currently selected Xero organisation. Xero's own MCP
  server wraps the same page in an ``organisationlogin/default.aspx?shortcode=``
  redirect to pick the organisation; we do not hold the shortcode
  (``docs/followups.md``).
* QuickBooks Online — ``https://app.qbo.intuit.com/app/bill?txnId=<Bill.Id>``,
  ``app.sandbox.qbo.intuit.com`` for a sandbox company (the adapter's
  ``environment`` setting). Opens in the signed-in user's current company.
"""

from __future__ import annotations

from urllib.parse import quote

from app.services.erp_adapters.dispatcher import resolve_adapter_key

_XERO_BILL = "https://go.xero.com/AccountsPayable/View.aspx?InvoiceID={id}"
_QBO_BILL = "https://app.qbo.intuit.com/app/bill?txnId={id}"
_QBO_SANDBOX_BILL = "https://app.sandbox.qbo.intuit.com/app/bill?txnId={id}"


def erp_document_url(erp_config: dict, document_id: str | None) -> str | None:
    """The ERP's own URL for ``document_id``, or None when there is no stable one."""
    if not document_id:
        return None
    key = resolve_adapter_key(erp_config)
    doc = quote(str(document_id), safe="")
    if key == "xero":
        return _XERO_BILL.format(id=doc)
    if key == "quickbooks_online":
        sandbox = (erp_config.get("environment") or "production") == "sandbox"
        return (_QBO_SANDBOX_BILL if sandbox else _QBO_BILL).format(id=doc)
    return None
