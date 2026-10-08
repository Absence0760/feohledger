"""``void_invoice`` on the Business Central and NetSuite adapters.

Neither ERP's API can void a posted bill. BC's API v2.0 purchaseInvoice has a
DELETE and one bound action (``Microsoft.NAV.post``); NetSuite's REST record
actions name neither ``vendorBill`` nor a void. Both can DELETE a bill that has
not reached the ledger, so each adapter deletes a draft (BC ``Draft``, NetSuite
``Pending Approval``) and returns False for anything else, without a request
that could change it. Sage Intacct's equivalent lives in
``test_erp_sage_intacct_adapter.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services.erp_adapters.base import ErpInvoiceStatus
from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter
from app.services.erp_adapters.netsuite import NetSuiteAdapter

_BC_API = "http://fake-erp:12112/d365"


def _run(coro):
    return asyncio.run(coro)


def _resp(status: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=body or {})
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def bc(monkeypatch) -> BusinessCentralAdapter:
    monkeypatch.setattr(settings, "erp_d365_api_base", _BC_API)
    monkeypatch.setattr(settings, "erp_d365_token_url", f"{_BC_API}/oauth2/token")
    return BusinessCentralAdapter(
        {"client_id": "c", "client_secret": "s", "environment": "sandbox", "company_id": "co"}
    )


@pytest.fixture
def ns() -> NetSuiteAdapter:
    return NetSuiteAdapter(
        {
            "account_id": "1234567",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "ti",
            "token_secret": "ts",
        }
    )


# ---------------------------------------------------------------------------
# Business Central
# ---------------------------------------------------------------------------


def test_bc_void_deletes_a_draft_with_its_etag(bc):
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_resp(200, {"access_token": "tok"}))
        client.get = AsyncMock(
            return_value=_resp(200, {"id": "pi-1", "status": "Draft", "@odata.etag": 'W/"x1"'})
        )
        client.delete = AsyncMock(return_value=_resp(204))
        assert _run(bc.void_invoice("pi-1")) is True

    url = f"{_BC_API}/sandbox/api/v2.0/companies(co)/purchaseInvoices(pi-1)"
    assert client.get.await_args.args[0] == url
    assert client.delete.await_args.args[0] == url
    # The etag read with the status: an invoice posted in between is refused.
    headers = client.delete.await_args.kwargs["headers"]
    assert headers["If-Match"] == 'W/"x1"'
    assert headers["Authorization"] == "Bearer tok"


@pytest.mark.parametrize("status", ["Open", "Paid", "Canceled", "Corrective", "In Review", ""])
def test_bc_void_leaves_anything_but_a_draft_alone(bc, status):
    """A posted invoice needs a corrective credit memo, which API v2.0 does not
    expose — it is never deleted or otherwise touched."""
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_resp(200, {"access_token": "tok"}))
        client.get = AsyncMock(return_value=_resp(200, {"id": "pi-1", "status": status}))
        client.delete = AsyncMock()
        assert _run(bc.void_invoice("pi-1")) is False
    client.delete.assert_not_awaited()
    assert client.post.await_count == 1  # the token exchange; no bound action


@pytest.mark.parametrize(("get_status", "delete_status"), [(404, None), (200, 400), (200, 412)])
def test_bc_void_reports_false_when_bc_refuses(bc, get_status, delete_status):
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_resp(200, {"access_token": "tok"}))
        client.get = AsyncMock(return_value=_resp(get_status, {"status": "Draft"}))
        client.delete = AsyncMock(return_value=_resp(delete_status or 500))
        assert _run(bc.void_invoice("pi-1")) is False
    if get_status != 200:
        client.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# NetSuite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        {"id": "pendingApproval", "refName": "Pending Approval"},
        {"refName": "Pending Approval"},
    ],
)
def test_netsuite_void_deletes_a_bill_pending_approval(ns, status):
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(200, {"id": "1001", "status": status}))
        client.delete = AsyncMock(return_value=_resp(204))
        assert _run(ns.void_invoice("1001")) is True

    url = "https://1234567.suitetalk.api.netsuite.com/services/rest/record/v1/vendorBill/1001"
    assert client.delete.await_args.args[0] == url
    assert client.delete.await_args.kwargs["headers"]["Authorization"].startswith("OAuth ")


@pytest.mark.parametrize(
    "status",
    [
        {"id": "open", "refName": "Open"},
        {"id": "paidInFull", "refName": "Paid In Full"},
        {"id": "cancelled", "refName": "Cancelled"},
        {},
    ],
)
def test_netsuite_void_leaves_an_approved_bill_alone(ns, status):
    """An approved bill has posted to the GL; deleting it would erase history
    rather than reverse it, so nothing is sent."""
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(200, {"id": "1001", "status": status}))
        client.delete = AsyncMock()
        assert _run(ns.void_invoice("1001")) is False
    client.delete.assert_not_awaited()


def test_netsuite_void_reports_false_when_netsuite_refuses(ns):
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(404))
        client.delete = AsyncMock()
        assert _run(ns.void_invoice("1001")) is False
        client.delete.assert_not_awaited()

        client.get = AsyncMock(return_value=_resp(200, {"status": {"id": "pendingApproval"}}))
        client.delete = AsyncMock(return_value=_resp(400))
        assert _run(ns.void_invoice("1001")) is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # Real NetSuite: `id` is camelCase, `refName` has spaces. Both must map.
        ({"id": "paidInFull", "refName": "Paid In Full"}, ErpInvoiceStatus.paid),
        ({"refName": "Paid In Full"}, ErpInvoiceStatus.paid),
        ({"id": "pendingApproval", "refName": "Pending Approval"}, ErpInvoiceStatus.draft),
        ({"id": "open", "refName": "Open"}, ErpInvoiceStatus.open),
        ({"id": "somethingElse"}, ErpInvoiceStatus.unknown),
    ],
)
def test_netsuite_status_reads_the_real_status_shape(ns, status, expected):
    """`refName` "Paid In Full" lower-cased is "paid in full", which never
    matched the "paidinfull" key: a paid bill polled as unknown."""
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(200, {"status": status}))
        assert _run(ns.get_invoice_status("1001")) == expected
