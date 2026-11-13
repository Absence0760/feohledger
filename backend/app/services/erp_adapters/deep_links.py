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

* Xero — ``https://go.xero.com/organisationlogin/default.aspx?shortcode=<ShortCode>
  &redirecturl=/AccountsPayable/View.aspx?InvoiceID=<InvoiceID>``. Xero's
  organisation-login redirect selects the organisation named by its
  ``ShortCode`` (``GET /Organisation``, which the adapter reads at each
  confirmed push and hands over as ``ErpPostResult.erp_company_code``), then
  opens the bill, so a user with several organisations lands in the right one.
  It is the form Xero's own MCP server builds (``src/consts/deeplinks.ts`` in
  ``XeroAPI/xero-mcp-server``). With no ShortCode (the Organisation read
  failed) the link is the bare bill page, which opens in the user's currently
  selected organisation (decisions §354).
* QuickBooks Online — ``https://app.qbo.intuit.com/app/bill?txnId=<Bill.Id>``,
  ``app.sandbox.qbo.intuit.com`` for a sandbox company (the adapter's
  ``environment`` setting). Opens in the signed-in user's current company:
  Intuit documents no URL that selects a company by ``realmId`` (none on
  developer.intuit.com; its help centre's only route is Settings → Switch
  company), so none is guessed (decisions §354).
"""

from __future__ import annotations

from urllib.parse import quote

from app.services.erp_adapters.dispatcher import resolve_adapter_key

_XERO_BILL_PATH = "/AccountsPayable/View.aspx?InvoiceID={id}"
_XERO_HOST = "https://go.xero.com"
_XERO_ORG_LOGIN = _XERO_HOST + "/organisationlogin/default.aspx?shortcode={code}&redirecturl={path}"
_QBO_BILL = "https://app.qbo.intuit.com/app/bill?txnId={id}"
_QBO_SANDBOX_BILL = "https://app.sandbox.qbo.intuit.com/app/bill?txnId={id}"


def erp_document_url(
    erp_config: dict, document_id: str | None, *, company_code: str | None = None
) -> str | None:
    """The ERP's own URL for ``document_id``, or None when there is no stable one.

    ``company_code`` is the adapter's ``ErpPostResult.erp_company_code``: the
    ERP's code for the company the document lives in, used where the ERP's web
    UI can select a company from the link (Xero's ShortCode).
    """
    if not document_id:
        return None
    key = resolve_adapter_key(erp_config)
    doc = quote(str(document_id), safe="")
    if key == "xero":
        path = _XERO_BILL_PATH.format(id=doc)
        code = (company_code or "").strip()
        if not code:
            return _XERO_HOST + path
        # The redirect path stays readable, as Xero's own links send it: the id
        # is already fully quoted, so it holds no `&` or `#` to end the query.
        return _XERO_ORG_LOGIN.format(code=quote(code, safe="!"), path=path)
    if key == "quickbooks_online":
        sandbox = (erp_config.get("environment") or "production") == "sandbox"
        return (_QBO_SANDBOX_BILL if sandbox else _QBO_BILL).format(id=doc)
    return None
