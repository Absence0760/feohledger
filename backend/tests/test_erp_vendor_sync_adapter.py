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

# Import the adapter modules so their @register_adapter decorators
# populate the dispatcher registry.
import app.services.erp_adapters.dynamics_365_bc  # noqa: F401, E402
import app.services.erp_adapters.merge_dev  # noqa: F401, E402
import app.services.erp_adapters.mock_adapter  # noqa: F401, E402
import app.services.erp_adapters.netsuite  # noqa: F401, E402


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


def test_merge_dev_list_vendors_maps_response_into_vendor_payloads():
    """One Merge.dev page → list of VendorPayload with the right field
    mapping. Locks the keys we read from the upstream JSON."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [
            {
                "id": "merge-vendor-1",
                "name": "Acme Supplies",
                "email_address": "ap@acme.test",
                "phone_number": "555-0100",
                "tax_number": "12-3456789",
                "payment_term": {"name": "Net 45"},
                "addresses": [
                    {
                        "line1": "123 Main St",
                        "city": "Springfield",
                        "state": "IL",
                        "zip_code": "62701",
                        "country": "US",
                    }
                ],
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
    assert v.payment_terms == "Net 45"
    assert v.address == "123 Main St, Springfield, IL, 62701, US"


def test_merge_dev_list_vendors_handles_missing_optional_fields():
    """Real ERP data is messy — no email/phone/address/payment term. The
    adapter maps what's there and leaves the rest None, never crashing."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [{"id": "merge-vendor-2", "name": "Bare Vendor"}],
        "next": None,
    }

    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        vendors = _run(adapter.list_vendors())

    assert len(vendors) == 1
    v = vendors[0]
    assert v.erp_vendor_id == "merge-vendor-2"
    assert v.name == "Bare Vendor"
    assert v.email is None
    assert v.phone is None
    assert v.address is None
    assert v.tax_id is None
    assert v.payment_terms is None


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


def test_merge_dev_list_vendors_returns_empty_on_http_error():
    """Adapter degrades gracefully — the sync endpoint shows "0 new
    vendors" instead of bubbling a 502 to the operator clicking the
    button."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(500, {"detail": "boom"}))
        vendors = _run(adapter.list_vendors())
    assert vendors == []


def test_merge_dev_list_vendors_returns_empty_on_network_error():
    """Total network failure must not raise out of the adapter."""
    import httpx

    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=httpx.ConnectError("dns fail"))
        vendors = _run(adapter.list_vendors())
    assert vendors == []


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

    assert [(v.erp_vendor_id, v.name, v.code) for v in vendors] == [
        ("25", "Acme Supplies Ltd", "ACME-01"),
        ("26", "Jane Contractor", "Jane Contractor"),
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


def test_netsuite_list_vendors_follows_has_more_and_caps_at_1000_rows():
    from app.services.erp_adapters.netsuite import NetSuiteAdapter

    adapter = NetSuiteAdapter(_netsuite_config())

    def page(n: int) -> MagicMock:
        rows = [{"id": f"{n}-{i}", "entityid": f"V{n}-{i}"} for i in range(100)]
        return _make_mock_response(200, {"items": rows, "hasMore": True})

    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(side_effect=[page(n) for n in range(12)])
        vendors = _run(adapter.list_vendors())

    assert len(vendors) == 1000
    assert client.post.await_count == 10
    urls = [c.args[0] for c in client.post.await_args_list]
    assert "offset=0" in urls[0] and "offset=100" in urls[1] and "offset=900" in urls[9]


def test_netsuite_list_vendors_returns_what_it_read_on_http_error():
    from app.services.erp_adapters.netsuite import NetSuiteAdapter

    adapter = NetSuiteAdapter(_netsuite_config())
    first = _make_mock_response(200, {"items": [{"id": "1", "entityid": "V1"}], "hasMore": True})
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_make_mock_response(401, {"detail": "no"}))
        assert _run(adapter.list_vendors()) == []
        client.post = AsyncMock(side_effect=[first, _make_mock_response(500, {})])
        assert [v.erp_vendor_id for v in _run(adapter.list_vendors())] == ["1"]


def test_netsuite_list_vendors_returns_empty_on_network_error():
    import httpx

    from app.services.erp_adapters.netsuite import NetSuiteAdapter

    adapter = NetSuiteAdapter(_netsuite_config())
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(side_effect=httpx.ConnectError("dns fail"))
        vendors = _run(adapter.list_vendors())
    assert vendors == []


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


def test_d365_list_vendors_returns_empty_on_http_error():
    from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter

    adapter = BusinessCentralAdapter(_d365_config())
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_D365_TOKEN_RESPONSE)
        client.get = AsyncMock(return_value=_make_mock_response(500, {"detail": "boom"}))
        vendors = _run(adapter.list_vendors())
    assert vendors == []


def test_d365_list_vendors_returns_empty_on_token_failure():
    """A failed OAuth token exchange must degrade to an empty list, not
    propagate — `_get_token()` calls `raise_for_status()` internally."""
    from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter

    adapter = BusinessCentralAdapter(_d365_config())
    bad_token_resp = _make_mock_response(400, {"error": "invalid_client"})
    bad_token_resp.raise_for_status = MagicMock(side_effect=RuntimeError("token exchange failed"))
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=bad_token_resp)
        vendors = _run(adapter.list_vendors())
    assert vendors == []


def test_d365_list_vendors_returns_empty_on_network_error():
    import httpx

    from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter

    adapter = BusinessCentralAdapter(_d365_config())
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_D365_TOKEN_RESPONSE)
        client.get = AsyncMock(side_effect=httpx.ConnectError("dns fail"))
        vendors = _run(adapter.list_vendors())
    assert vendors == []


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
