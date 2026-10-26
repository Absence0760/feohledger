"""Tests for the ERP adapter `list_vendors` contract.

Mirrors `test_erp_po_sync.py` / `test_erp_gl_sync.py`: the mock adapter's
catalogue is the contract `/api/vendors/sync-erp` relies on for local dev,
the real adapters are HTTP-mocked to lock the request shape and the
response→`VendorPayload` mapping, and adapters get the base's empty-list
default until overridden. `test_vendor_sync.py` covers the endpoint's
DB-level round trip through the mock adapter; this file is adapter-only.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the adapter modules so their @register_adapter decorators
# populate the dispatcher registry.
import app.services.erp_adapters.dynamics_365_bc  # noqa: F401, E402
import app.services.erp_adapters.merge_dev  # noqa: F401, E402
import app.services.erp_adapters.mock_adapter  # noqa: F401, E402
import app.services.erp_adapters.netsuite  # noqa: F401, E402
from app.services.erp_adapters.base import (
    SYNC_LISTING_MAX_ROWS,
    ErpListingIncomplete,
    ListingGap,
)


def _run(coro):
    return asyncio.run(coro)


def _make_mock_response(status_code: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    # Real bytes: the BC / NetSuite syncs parse `content` with `loads_exact_json`.
    resp.content = json.dumps(json_body).encode() if json_body is not None else b""
    resp.json = MagicMock(return_value=json_body or {})
    resp.headers = {"content-type": "application/json"}
    resp.raise_for_status = MagicMock()  # no-op by default; D365's _get_token calls it
    return resp


# ---------- Mock adapter ---------------------------------------------------


def test_mock_adapter_list_vendors_returns_seeded_catalogue():
    """The local-dev sync flow renders the same two vendors every time —
    this is the exact catalogue that used to be hardcoded inline in the
    `/api/vendors/sync-erp` endpoint."""
    from app.services.erp_adapters.dispatcher import get_erp_adapter

    adapter = get_erp_adapter({"type": "mock", "integration_method": "direct"})
    vendors = _run(adapter.list_vendors())

    assert {v.erp_vendor_id for v in vendors} == {"ERP-V001", "ERP-V002"}
    by_id = {v.erp_vendor_id: v for v in vendors}
    assert by_id["ERP-V001"].name == "Office Supplies Co"
    assert by_id["ERP-V001"].code == "OSC"
    assert by_id["ERP-V001"].email == "ap@officesupplies.com"
    assert by_id["ERP-V001"].payment_terms == "Net 30"
    assert by_id["ERP-V002"].name == "Cloud Services Inc"
    assert by_id["ERP-V002"].code == "CSI"
    assert by_id["ERP-V002"].email == "billing@cloudservices.com"
    assert by_id["ERP-V002"].payment_terms == "Net 20"


def test_mock_adapter_list_vendors_returns_independent_copies():
    """Mutating one call's return must not contaminate the next call —
    same contract as `list_pos` / `list_gl_accounts`."""
    from app.services.erp_adapters.dispatcher import get_erp_adapter

    adapter = get_erp_adapter({"type": "mock", "integration_method": "direct"})
    first = _run(adapter.list_vendors())
    first[0].name = "MUTATED"

    second = _run(adapter.list_vendors())
    assert second[0].name != "MUTATED"


# ---------- Base / unimplemented adapters --------------------------------


def test_base_adapter_list_vendors_default_is_empty_not_raises():
    """Belt-and-braces: a bare `ErpAdapter` subclass with no override must
    return [] from list_vendors. If someone changes the default to `raise
    NotImplementedError` they break every adapter that hasn't explicitly
    overridden it — see the identical guard for `list_pos`."""
    from app.services.erp_adapters.base import ErpAdapter

    class Bare(ErpAdapter):
        erp_type = "bare"

    result = _run(Bare({}).list_vendors())
    assert result == []


# ---------- Merge.dev adapter -------------------------------------------


def test_merge_dev_list_vendors_reads_supplier_contacts_not_a_vendors_path():
    """Merge's Accounting API has no ``/vendors`` path and no Vendor model: a
    supplier is a ``Contact`` listed at ``GET /contacts?is_supplier=true``
    (https://docs.merge.dev/openapi/accounting.json). The old adapter read
    ``/vendors``, which a live Merge account answers with an error, so every
    vendor sync failed and every bill push was refused ``vendor_not_linked``.
    ``addresses`` / ``phone_numbers`` are expanded so they arrive as objects."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, {"results": [], "next": None}))
        _run(adapter.list_vendors())

    call = client.get.await_args_list[0]
    assert call.args[0].endswith("/contacts")
    assert call.kwargs["params"]["is_supplier"] == "true"
    assert set(call.kwargs["params"]["expand"].split(",")) == {"addresses", "phone_numbers"}
    assert call.kwargs["params"]["page_size"] == "100"


def test_merge_dev_list_vendors_maps_a_supplier_contact_into_a_vendor_payload():
    """One Merge ``Contact`` page → VendorPayload. Locks the Contact keys we
    read: ``phone_numbers[].number``, ``addresses[].street_1/street_2``,
    ``tax_number``, ``email_address``. A Contact has no payment term, so
    ``payment_terms`` is None — never invented."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [
            {
                "id": "merge-vendor-1",
                "remote_id": "qbo-57",
                "name": "Acme Supplies",
                "is_supplier": True,
                "is_customer": False,
                "email_address": "ap@acme.test",
                "tax_number": "12-3456789",
                "status": "ACTIVE",
                "phone_numbers": [
                    {"number": None, "type": "Fax"},
                    {"number": "555-0100", "type": "Work"},
                ],
                "addresses": [
                    {
                        "type": "BILLING",
                        "street_1": "123 Main St",
                        "street_2": "Suite 4",
                        "city": "Springfield",
                        "state": "IL",
                        "zip_code": "62701",
                        "country": "US",
                    }
                ],
                "remote_was_deleted": False,
            }
        ],
        "next": None,
    }

    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        vendors = _run(adapter.list_vendors())

    assert len(vendors) == 1
    v = vendors[0]
    assert v.erp_vendor_id == "merge-vendor-1"
    assert v.name == "Acme Supplies"
    assert v.email == "ap@acme.test"
    assert v.phone == "555-0100"
    assert v.tax_id == "12-3456789"
    assert v.payment_terms is None
    assert v.address == "123 Main St, Suite 4, Springfield, IL, 62701, US"
    assert v.active is True


def test_merge_dev_list_vendors_reads_no_payment_term_even_if_a_payload_carries_one():
    """``payment_term`` is not a Contact field. A stray one (an integration's
    passthrough, or the old Vendor shape) is not read as the vendor's terms:
    the sync keeps the local value rather than adopting a guess."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [{"id": "c-1", "name": "Acme", "payment_term": {"name": "Net 30"}}],
        "next": None,
    }
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        vendors = _run(adapter.list_vendors())

    assert vendors[0].payment_terms is None


def test_merge_dev_list_vendors_handles_missing_optional_fields():
    """Real ERP data is messy — no email/phone/address. Unexpanded relations
    arrive as bare id strings, not objects; the adapter maps what's there and
    leaves the rest None, never crashing."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [
            {"id": "merge-vendor-2", "name": "Bare Vendor"},
            {
                "id": "merge-vendor-3",
                "name": "Unexpanded",
                "addresses": ["addr-uuid"],
                "phone_numbers": ["phone-uuid"],
                "status": None,
            },
        ],
        "next": None,
    }

    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        vendors = _run(adapter.list_vendors())

    assert [v.erp_vendor_id for v in vendors] == ["merge-vendor-2", "merge-vendor-3"]
    for v in vendors:
        assert v.email is None
        assert v.phone is None
        assert v.address is None
        assert v.tax_id is None
        assert v.payment_terms is None
        assert v.active is True


def test_merge_dev_archived_or_deleted_contact_is_emitted_inactive():
    """An ``ARCHIVED`` contact, or one deleted in the underlying ERP
    (``remote_was_deleted``), still exists as a link target but takes no new
    bill: emitted ``active=False`` (not dropped), as the other adapters do, so
    the sync deactivates a linked vendor and never creates a payee for it."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [
            {"id": "c-live", "name": "Live", "status": "ACTIVE"},
            {"id": "c-arch", "name": "Archived", "status": "ARCHIVED"},
            {"id": "c-del", "name": "Deleted", "status": "ACTIVE", "remote_was_deleted": True},
        ],
        "next": None,
    }
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        vendors = _run(adapter.list_vendors())

    assert {v.erp_vendor_id: v.active for v in vendors} == {
        "c-live": True,
        "c-arch": False,
        "c-del": False,
    }


def test_merge_dev_never_lists_a_customer_contact_as_a_vendor():
    """The listing filters on ``is_supplier=true``; a record that still says
    ``is_supplier: false`` is a customer, never a payee."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [
            {"id": "c-sup", "name": "Supplier", "is_supplier": True},
            {"id": "c-cust", "name": "Customer", "is_supplier": False, "is_customer": True},
            {"id": "c-unknown", "name": "Unflagged", "is_supplier": None},
        ],
        "next": None,
    }
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        vendors = _run(adapter.list_vendors())

    assert [v.erp_vendor_id for v in vendors] == ["c-sup", "c-unknown"]


def test_merge_dev_vendor_id_is_the_id_a_po_and_a_bill_reference():
    """A Merge PurchaseOrder's ``vendor`` and an Invoice's ``contact`` are both
    the Contact's Merge ``id`` (uuid). The id ``list_vendors`` reports is the
    one ``list_pos`` reads into ``vendor_erp_id`` and ``post_invoice`` sends,
    so the PO sync links by id and the push names the right contact."""
    from decimal import Decimal

    from app.services.erp_adapters.base import InvoicePayload
    from app.services.erp_adapters.merge_dev import MergeDevAdapter, _merge_po_to_payload

    contact_id = "6c4f1f0e-0d36-4d3b-9b0f-4f3e2b1a0c11"
    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    listing = {"results": [{"id": contact_id, "remote_id": "57", "name": "Acme"}], "next": None}
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, listing))
        [vendor] = _run(adapter.list_vendors())

    # The PO names its vendor by the Contact id — bare, or expanded.
    assert (
        _merge_po_to_payload({"purchase_order_number": "PO-1", "vendor": contact_id}).vendor_erp_id
        == vendor.erp_vendor_id
    )
    expanded = _merge_po_to_payload(
        {"purchase_order_number": "PO-1", "vendor": {"id": contact_id, "name": "Acme"}}
    )
    assert expanded.vendor_erp_id == vendor.erp_vendor_id

    # And the bill posts that same id as its `contact`.
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(
            return_value=_make_mock_response(201, {"model": {"id": "inv-1", "number": "INV-1"}})
        )
        result = _run(
            adapter.post_invoice(
                InvoicePayload(
                    correlation_id="corr-1",
                    invoice_number="INV-1",
                    vendor_name="Acme",
                    amount=Decimal("10.00"),
                    vendor_erp_id=vendor.erp_vendor_id,
                )
            )
        )
    assert result.success
    sent = json.loads(client.post.await_args.kwargs["content"])
    assert sent["model"]["contact"] == contact_id


def test_merge_dev_skips_a_vendor_with_no_merge_id_never_keys_it_otherwise():
    """The bill posts ``contact`` = the Merge id. A ``remote_id`` is the
    underlying ERP's key, which Merge refuses there, and a name keyed two
    same-named vendors to one id, each relinking one row past the sync's
    ambiguous-name guard (§284). Neither is ever an ``erp_vendor_id``."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [
            {"id": "merge-vendor-1", "name": "Acme"},
            {"remote_id": "qbo-57", "name": "Acme"},  # no Merge id
            {"name": "Acme"},  # no id at all
            {"id": "", "remote_id": "qbo-58", "name": "Beta"},
        ],
        "next": None,
    }
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        vendors = _run(adapter.list_vendors())

    assert [(v.erp_vendor_id, v.name) for v in vendors] == [("merge-vendor-1", "Acme")]


def test_merge_dev_list_vendors_follows_pagination_cursor():
    """A real Merge response splits big vendor lists across pages joined
    by a `next` cursor — same contract as `list_pos`."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    page1 = {"results": [{"id": "v-1", "name": "V1"}], "next": "cursor-page-2"}
    page2 = {"results": [{"id": "v-2", "name": "V2"}], "next": None}

    responses = [_make_mock_response(200, page1), _make_mock_response(200, page2)]
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=responses)
        vendors = _run(adapter.list_vendors())

        second_call_kwargs = client.get.await_args_list[1].kwargs
        assert second_call_kwargs["params"]["cursor"] == "cursor-page-2"

    assert [v.erp_vendor_id for v in vendors] == ["v-1", "v-2"]


def test_merge_dev_list_vendors_raises_on_http_error_after_a_good_page():
    """An empty or partial vendor list is not the ERP's vendor list: the sync
    would unlink every vendor the unread pages held (decisions §284). So a
    failed page raises, and the endpoint answers 502 without syncing."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    page1 = {"results": [{"id": "v-1", "name": "Acme"}], "next": "c2"}
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(
            side_effect=[
                _make_mock_response(200, page1),
                _make_mock_response(500, {"detail": "boom"}),
            ]
        )
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(adapter.list_vendors())
    assert exc.value.gap is ListingGap.fetch_failed
    assert "boom" not in str(exc.value)


def test_merge_dev_list_vendors_raises_on_network_error():
    import httpx

    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=httpx.ConnectError("dns fail"))
        with pytest.raises(ErpListingIncomplete):
            _run(adapter.list_vendors())


# ---------- NetSuite adapter ----------------------------------------------


def _netsuite_config() -> dict:
    return {
        "account_id": "1234567",
        "consumer_key": "ck",
        "consumer_secret": "cs",
        "token_id": "tid",
        "token_secret": "ts",
    }


def test_netsuite_list_vendors_maps_suiteql_rows_into_vendor_payloads():
    """Vendors come from one SuiteQL query: the REST `/vendor` collection holds
    only ids and links, so it could never have supplied a name. The id is what
    a bill's `entity` is posted against; inactive vendors are skipped."""
    from app.services.erp_adapters.netsuite import NetSuiteAdapter

    adapter = NetSuiteAdapter(_netsuite_config())
    body = {
        "items": [
            {
                "id": "25",
                "entityid": "ACME-01",
                "companyname": "Acme Supplies Ltd",
                "email": "a@vendor.test",
                "phone": "555-0100",
                "terms": "Net 30",
                "isinactive": "F",
            },
            # An individual vendor: no company name, so the entity id names it.
            {"id": "26", "entityid": "Jane Contractor", "companyname": None, "isinactive": "F"},
            {"id": "27", "entityid": "Old Co", "companyname": "Old Co", "isinactive": "T"},
            {"id": None, "entityid": "No id"},
        ],
        "hasMore": False,
    }

    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_make_mock_response(200, body))
        vendors = _run(adapter.list_vendors())

    # An inactive vendor is emitted as such (§284): the sync never creates a
    # payee for it, but a vendor linked to it is not mistaken for a link to a
    # deleted record.
    assert [(v.erp_vendor_id, v.name, v.code, v.active) for v in vendors] == [
        ("25", "Acme Supplies Ltd", "ACME-01", True),
        ("26", "Jane Contractor", "Jane Contractor", True),
        ("27", "Old Co", "Old Co", False),
    ]
    assert vendors[0].email == "a@vendor.test"
    assert vendors[0].phone == "555-0100"
    assert vendors[0].payment_terms == "Net 30"
    assert vendors[1].email is None and vendors[1].payment_terms is None

    url = client.post.await_args.args[0]
    assert url.startswith(
        "https://1234567.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql?limit=100"
    )
    headers = client.post.await_args.kwargs["headers"]
    assert headers["Prefer"] == "transient"
    assert headers["Authorization"].startswith("OAuth ")
    query = json.loads(client.post.await_args.kwargs["content"])["q"]
    assert "FROM vendor" in query and "ORDER BY id" in query


def _netsuite_vendor_pages(count: int, *, last_has_more: bool) -> list[MagicMock]:
    def page(n: int) -> MagicMock:
        rows = [{"id": f"{n}-{i}", "entityid": f"V{n}-{i}"} for i in range(100)]
        more = n < count - 1 or last_has_more
        return _make_mock_response(200, {"items": rows, "hasMore": more})

    return [page(n) for n in range(count)]


def test_netsuite_list_vendors_follows_has_more_to_the_end():
    """Past the PO list's 1000-row bound: a vendor listing reads them all."""
    from app.services.erp_adapters.netsuite import NetSuiteAdapter

    adapter = NetSuiteAdapter(_netsuite_config())
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(side_effect=_netsuite_vendor_pages(12, last_has_more=False))
        vendors = _run(adapter.list_vendors())

    assert len(vendors) == 1200
    urls = [c.args[0] for c in client.post.await_args_list]
    assert "offset=0" in urls[0] and "offset=100" in urls[1] and "offset=1100" in urls[11]


def test_netsuite_list_vendors_past_the_sync_bound_raises_never_truncates():
    from app.services.erp_adapters.netsuite import NetSuiteAdapter

    pages = SYNC_LISTING_MAX_ROWS // 100
    adapter = NetSuiteAdapter(_netsuite_config())
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(side_effect=_netsuite_vendor_pages(pages, last_has_more=True))
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(adapter.list_vendors())
    assert exc.value.gap is ListingGap.truncated
    assert client.post.await_count == pages


def test_netsuite_list_vendors_raises_on_any_failed_page():
    """It used to return the first page as if it were every vendor."""
    from app.services.erp_adapters.netsuite import NetSuiteAdapter

    adapter = NetSuiteAdapter(_netsuite_config())
    first = _make_mock_response(200, {"items": [{"id": "1", "entityid": "V1"}], "hasMore": True})
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_make_mock_response(401, {"detail": "no"}))
        with pytest.raises(ErpListingIncomplete):
            _run(adapter.list_vendors())
        client.post = AsyncMock(side_effect=[first, _make_mock_response(500, {})])
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(adapter.list_vendors())
    assert exc.value.gap is ListingGap.fetch_failed


def test_netsuite_list_vendors_raises_on_network_error():
    import httpx

    from app.services.erp_adapters.netsuite import NetSuiteAdapter

    adapter = NetSuiteAdapter(_netsuite_config())
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(side_effect=httpx.ConnectError("dns fail"))
        with pytest.raises(ErpListingIncomplete):
            _run(adapter.list_vendors())


# ---------- Dynamics 365 Business Central adapter -------------------------


def _d365_config() -> dict:
    return {
        "base_url": "https://api.businesscentral.dynamics.com/v2.0",
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "client_secret": "secret-1",
        "environment": "production",
        "company_id": "company-1",
    }


_D365_TOKEN_RESPONSE = _make_mock_response(200, {"access_token": "tok"})


def test_d365_list_vendors_maps_response_into_vendor_payloads():
    """The adapter fetches a token (POST) before its GET — same two-step
    shape `test_erp_adapter_idempotency.py` uses for D365's other calls."""
    from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter

    adapter = BusinessCentralAdapter(_d365_config())
    body = {
        "value": [
            {
                "id": "d365-vendor-1",
                "number": "V0001",
                "displayName": "Fake ERP Vendor A",
                "email": "ap@fake-erp.test",
                "phoneNumber": "555-0200",
                "taxRegistrationNumber": "TAX-1",
                "paymentTermsId": "NET30",
            }
        ]
    }

    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_D365_TOKEN_RESPONSE)
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        vendors = _run(adapter.list_vendors())

    assert len(vendors) == 1
    v = vendors[0]
    assert v.erp_vendor_id == "d365-vendor-1"
    assert v.name == "Fake ERP Vendor A"
    assert v.code == "V0001"
    assert v.email == "ap@fake-erp.test"
    assert v.phone == "555-0200"
    assert v.tax_id == "TAX-1"
    assert v.payment_terms == "NET30"


def test_d365_skips_a_vendor_with_no_id_never_keys_it_by_number_or_name():
    """The bill posts ``vendorId`` = the vendor's GUID ``id``. The vendor No.
    would be posted as a GUID and refused, and a name keyed two same-named
    vendors to one id past the sync's ambiguous-name guard (§284)."""
    from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter

    adapter = BusinessCentralAdapter(_d365_config())
    body = {
        "value": [
            {"id": "d365-vendor-1", "number": "V0001", "displayName": "Acme"},
            {"number": "V0002", "displayName": "Acme"},  # no id
            {"displayName": "Acme"},  # neither id nor No.
            {"id": None, "number": "V0003", "displayName": "Beta"},
        ]
    }
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_D365_TOKEN_RESPONSE)
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        vendors = _run(adapter.list_vendors())

    assert [(v.erp_vendor_id, v.code, v.name) for v in vendors] == [
        ("d365-vendor-1", "V0001", "Acme")
    ]


def test_d365_list_vendors_follows_odata_next_link():
    from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter

    adapter = BusinessCentralAdapter(_d365_config())
    page1 = {
        "value": [{"id": "v1", "displayName": "V1"}],
        "@odata.nextLink": "https://api.businesscentral.dynamics.com/v2.0/next-page",
    }
    page2 = {"value": [{"id": "v2", "displayName": "V2"}]}

    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_D365_TOKEN_RESPONSE)
        client.get = AsyncMock(
            side_effect=[_make_mock_response(200, page1), _make_mock_response(200, page2)]
        )
        vendors = _run(adapter.list_vendors())

    assert [v.erp_vendor_id for v in vendors] == ["v1", "v2"]
    second_url = client.get.await_args_list[1].args[0]
    assert second_url == "https://api.businesscentral.dynamics.com/v2.0/next-page"


def test_d365_list_vendors_raises_on_http_error():
    from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter

    adapter = BusinessCentralAdapter(_d365_config())
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_D365_TOKEN_RESPONSE)
        client.get = AsyncMock(return_value=_make_mock_response(500, {"detail": "boom"}))
        with pytest.raises(ErpListingIncomplete) as exc:
            _run(adapter.list_vendors())
    assert exc.value.gap is ListingGap.fetch_failed


def test_d365_list_vendors_raises_on_token_failure():
    """A failed OAuth token exchange is a failure, not "no vendors": it
    propagates, and the endpoint answers 502 without syncing."""
    from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter

    adapter = BusinessCentralAdapter(_d365_config())
    bad_token_resp = _make_mock_response(400, {"error": "invalid_client"})
    bad_token_resp.raise_for_status = MagicMock(side_effect=RuntimeError("token exchange failed"))
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=bad_token_resp)
        with pytest.raises(RuntimeError):
            _run(adapter.list_vendors())


def test_d365_list_vendors_raises_on_network_error():
    import httpx

    from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter

    adapter = BusinessCentralAdapter(_d365_config())
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_D365_TOKEN_RESPONSE)
        client.get = AsyncMock(side_effect=httpx.ConnectError("dns fail"))
        with pytest.raises(ErpListingIncomplete):
            _run(adapter.list_vendors())


# ---------- API endpoint wiring --------------------------------------------


def test_sync_vendors_endpoint_uses_dispatcher_not_hardcoded_data():
    """Smoke test the wiring: the API endpoint calls `adapter.list_vendors()`
    via the dispatcher, not a private hardcoded list. Catches a regression
    where someone reverts to the old inline-mock pattern."""
    import inspect

    from app.api import vendors as vendors_module

    src = inspect.getsource(vendors_module.sync_vendors_from_erp_endpoint)
    assert "get_erp_adapter" in src
    assert "list_vendors" in src
    # The old hardcoded mock list lived inline — make sure no one quietly
    # puts it back into the endpoint itself.
    assert "ERP-V001" not in src
    assert "ERP-V002" not in src
