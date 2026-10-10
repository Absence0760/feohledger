"""Tests for the ERP adapter `list_gl_accounts` contract.

Mirrors `test_erp_po_sync.py` — the mock adapter's catalogue is the
contract `/api/gl-accounts/sync-erp` relies on, the Merge.dev adapter
is HTTP-mocked to lock the request shape and the response→payload
mapping; NetSuite (SuiteQL) and Business Central (`accounts`) likewise.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

# Trigger @register_adapter side effects on these modules so tests
# can resolve adapters by type via the dispatcher.
import app.services.erp_adapters.dynamics_365_bc  # noqa: F401, E402
import app.services.erp_adapters.merge_dev  # noqa: F401, E402
import app.services.erp_adapters.mock_adapter  # noqa: F401, E402
import app.services.erp_adapters.netsuite  # noqa: F401, E402


def _run(coro):
    return asyncio.run(coro)


# ---------- Mock adapter --------------------------------------------------


def test_mock_adapter_list_gl_accounts_returns_canonical_catalogue():
    """The local-dev sync flow renders the same 20-row chart every
    time; the data lives in the adapter, not the API endpoint. The
    Auto GL Coding pipeline (chart-of-accounts injection + post-
    extraction validation) needs at least the seven expense rows
    that match the default GL list."""
    from app.services.erp_adapters.dispatcher import get_erp_adapter

    adapter = get_erp_adapter({"type": "mock", "integration_method": "direct"})
    accounts = _run(adapter.list_gl_accounts())

    codes = {a.code for a in accounts}
    # Spot-check the categories the AI prompt's default list includes.
    assert {"6100", "6200", "6300", "6400", "6500", "6600", "6700"} <= codes
    # `account_type` populated for every row so post-extraction
    # validation has a normalized value to render in the UI.
    for a in accounts:
        assert a.account_type in {"asset", "liability", "equity", "revenue", "expense"}
    # erp_account_id round-trips so re-syncs are idempotent.
    for a in accounts:
        assert a.erp_account_id == a.code


def test_mock_adapter_list_gl_accounts_returns_independent_payloads():
    """Caller-side mutation must not bleed into the next sync — same
    contract as `list_pos`. Two consecutive calls return independently
    constructed dataclasses."""
    from app.services.erp_adapters.dispatcher import get_erp_adapter

    adapter = get_erp_adapter({"type": "mock", "integration_method": "direct"})
    first = _run(adapter.list_gl_accounts())
    first[0].name = "MUTATED"
    second = _run(adapter.list_gl_accounts())
    assert second[0].name != "MUTATED"


# ---------- Business Central (API v2.0 `accounts`) ------------------------

_BC_API = "http://fake-erp:12112/d365"


def _bc_adapter(monkeypatch):
    from app.config import settings
    from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter

    # Operator override: keeps the SSRF guard's DNS lookup out of a unit test.
    monkeypatch.setattr(settings, "erp_d365_api_base", _BC_API)
    monkeypatch.setattr(settings, "erp_d365_token_url", f"{_BC_API}/oauth2/token")
    return BusinessCentralAdapter(
        {"client_id": "c", "client_secret": "s", "environment": "sandbox", "company_id": "co"}
    )


def test_bc_list_gl_accounts_maps_accounts_into_payloads(monkeypatch):
    """`erp_account_id` is BC's account GUID — what `post_invoice` sends as a
    line's `accountId` — and `code` is the account No. Headings / totals and
    blocked accounts are skipped: BC refuses a line on either."""
    body = {
        "value": [
            {
                "id": "g-6100",
                "number": "6100",
                "displayName": "Office Supplies",
                "category": "Expense",
                "accountType": "Posting",
                "blocked": False,
            },
            {
                "id": "g-2100",
                "number": "2100",
                "displayName": "Accounts Payable",
                "category": "Liabilities",
                "accountType": "Posting",
                "blocked": False,
            },
            {
                "id": "g-1000",
                "number": "1000",
                "displayName": "Cash",
                "category": "Assets",
                "accountType": "Posting",
            },
            {"id": "g-3000", "number": "3000", "displayName": "Capital", "category": "Equity"},
            {"id": "g-4000", "number": "4000", "displayName": "Sales", "category": "Income"},
            {
                "id": "g-5000",
                "number": "5000",
                "displayName": "COGS",
                "category": "Cost of Goods Sold",
                "accountType": "Posting",
            },
            # Blank category -> unclassified, still synced.
            {"id": "g-9000", "number": "9000", "displayName": "Misc", "category": " "},
            {"id": "g-6000", "number": "6000", "displayName": "Opex", "accountType": "Heading"},
            {"id": "g-6999", "number": "6999", "displayName": "Total", "accountType": "End-Total"},
            {"id": "g-6900", "number": "6900", "displayName": "Old", "blocked": True},
            {"id": None, "number": "7000", "displayName": "No id"},
            {"id": "g-x", "number": "", "displayName": "No number"},
        ]
    }
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(200, {"access_token": "tok"}))
        client.get = AsyncMock(return_value=_mock_response(200, body))
        out = _run(_bc_adapter(monkeypatch).list_gl_accounts())

    assert [(a.code, a.name, a.account_type, a.erp_account_id) for a in out] == [
        ("6100", "Office Supplies", "expense", "g-6100"),
        ("2100", "Accounts Payable", "liability", "g-2100"),
        ("1000", "Cash", "asset", "g-1000"),
        ("3000", "Capital", "equity", "g-3000"),
        ("4000", "Sales", "revenue", "g-4000"),
        ("5000", "COGS", "expense", "g-5000"),
        ("9000", "Misc", None, "g-9000"),
    ]
    url = client.get.await_args.args[0]
    assert url == f"{_BC_API}/sandbox/api/v2.0/companies(co)/accounts"
    headers = client.get.await_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Prefer"] == "odata.maxpagesize=100"


def test_bc_list_gl_accounts_follows_next_link_and_caps_at_1000_rows(monkeypatch):
    """`@odata.nextLink` is followed, but never past 1000 rows (10 pages of
    100), however many pages BC says remain."""

    def page(n: int) -> MagicMock:
        rows = [
            {"id": f"g-{n}-{i}", "number": f"{n:02d}{i:03d}", "displayName": "A"}
            for i in range(100)
        ]
        return _mock_response(200, {"value": rows, "@odata.nextLink": f"{_BC_API}/next/{n + 1}"})

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(200, {"access_token": "tok"}))
        client.get = AsyncMock(side_effect=[page(n) for n in range(12)])
        out = _run(_bc_adapter(monkeypatch).list_gl_accounts())

    assert len(out) == 1000
    assert client.get.await_count == 10
    assert client.get.await_args_list[1].args[0] == f"{_BC_API}/next/1"


def test_bc_list_gl_accounts_degrades_on_error_keeping_what_it_read(monkeypatch):
    import httpx

    first = _mock_response(
        200,
        {
            "value": [{"id": "g-1", "number": "6100", "displayName": "A"}],
            "@odata.nextLink": f"{_BC_API}/next",
        },
    )
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(200, {"access_token": "tok"}))
        client.get = AsyncMock(side_effect=[first, _mock_response(500, {"error": "x"})])
        assert [a.code for a in _run(_bc_adapter(monkeypatch).list_gl_accounts())] == ["6100"]

        client.get = AsyncMock(side_effect=httpx.ConnectError("dns"))
        assert _run(_bc_adapter(monkeypatch).list_gl_accounts()) == []

        bad_token = _mock_response(401, {"error": "invalid_client"})
        bad_token.raise_for_status = MagicMock(side_effect=RuntimeError("401"))
        client.post = AsyncMock(return_value=bad_token)
        client.get = AsyncMock()
        assert _run(_bc_adapter(monkeypatch).list_gl_accounts()) == []
        client.get.assert_not_awaited()


# ---------- NetSuite (SuiteQL) --------------------------------------------


def _netsuite_adapter():
    from app.services.erp_adapters.netsuite import NetSuiteAdapter

    return NetSuiteAdapter(
        {
            "account_id": "TSTDRV_SB1",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "ti",
            "token_secret": "ts",
        }
    )


def test_netsuite_list_gl_accounts_maps_suiteql_rows_into_payloads():
    """The chart sync is what gives NetSuite's expense lines their account
    ids: `erp_account_id` is the internal id, never the number."""
    import json

    body = {
        "items": [
            {
                "id": "120",
                "acctnumber": "6100",
                "fullname": "Office Supplies",
                "accttype": "Expense",
                "isinactive": "F",
            },
            {
                "id": "7",
                "acctnumber": "2000",
                "fullname": "Accounts Payable",
                "accttype": "AcctPay",
                "isinactive": "F",
            },
            # No number ("Use Account Numbers" off) — keyed by its name.
            {
                "id": "130",
                "acctnumber": None,
                "fullname": "Travel",
                "accttype": "OthExpense",
                "isinactive": "F",
            },
            # Inactive — nothing should be newly coded to it.
            {
                "id": "140",
                "acctnumber": "6900",
                "fullname": "Old",
                "accttype": "Expense",
                "isinactive": "T",
            },
            # A name too long to be a code is skipped, never truncated.
            {
                "id": "150",
                "acctnumber": None,
                "fullname": "x" * 51,
                "accttype": "Expense",
                "isinactive": "F",
            },
        ],
        "hasMore": False,
    }
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(200, body))
        out = _run(_netsuite_adapter().list_gl_accounts())

    assert [(a.code, a.erp_account_id, a.account_type) for a in out] == [
        ("6100", "120", "expense"),
        ("2000", "7", "liability"),
        ("Travel", "130", "expense"),
    ]
    url = client.post.await_args.args[0]
    assert url.startswith(
        "https://tstdrv-sb1.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql?"
    )
    headers = client.post.await_args.kwargs["headers"]
    assert headers["Prefer"] == "transient"
    assert headers["Authorization"].startswith("OAuth ")
    assert "FROM account" in json.loads(client.post.await_args.kwargs["content"])["q"]


def test_netsuite_list_gl_accounts_follows_has_more_and_degrades_on_error():
    page1 = {
        "items": [
            {
                "id": "1",
                "acctnumber": "6100",
                "fullname": "A",
                "accttype": "Expense",
                "isinactive": "F",
            }
        ],
        "hasMore": True,
    }
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(side_effect=[_mock_response(200, page1), _mock_response(500, {})])
        out = _run(_netsuite_adapter().list_gl_accounts())

    assert [a.code for a in out] == ["6100"]
    urls = [c.args[0] for c in client.post.await_args_list]
    assert "offset=0" in urls[0] and "offset=100" in urls[1]


# ---------- Merge.dev mapping --------------------------------------------


def _mock_response(status: int, body: dict | None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    # Real bytes: the BC / NetSuite list syncs parse `content` with
    # `loads_exact_json` (money as Decimal), not `.json()`.
    resp.content = json.dumps(body).encode() if body is not None else b""
    resp.json = MagicMock(return_value=body or {})
    resp.headers = {"content-type": "application/json"}
    resp.raise_for_status = MagicMock()
    return resp


def test_merge_dev_list_gl_accounts_maps_response_into_payloads():
    """One Merge page → list of GLAccountPayload with the right field
    mapping. The endpoint upserts on `code`; the test locks the keys
    we read from upstream so a future refactor can't silently drop
    `account_number`."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [
            {
                "id": "merge-uuid-1",
                "name": "Office Supplies",
                "account_number": "6100",
                "classification": "EXPENSE",
            },
            {
                "id": "merge-uuid-2",
                "name": "Cash on Hand",
                "account_number": "1000",
                "classification": "ASSET",
            },
        ],
        "next": None,
    }
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, body))
        out = _run(adapter.list_gl_accounts())

    assert len(out) == 2
    by_code = {a.code: a for a in out}
    assert by_code["6100"].name == "Office Supplies"
    assert by_code["6100"].account_type == "expense"
    assert by_code["6100"].erp_account_id == "merge-uuid-1"
    assert by_code["1000"].account_type == "asset"


def test_merge_dev_list_gl_accounts_classification_normalization():
    """Merge ships several classifications we map down to the same
    internal type. Lock the table — losing one (e.g. EXPENSES vs
    EXPENSE) would silently misclassify whole categories."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    cases = {
        "EXPENSE": "expense",
        "EXPENSES": "expense",
        "COST_OF_GOODS_SOLD": "expense",
        "INCOME": "revenue",
        "REVENUE": "revenue",
        "ASSET": "asset",
        "LIABILITY": "liability",
        "EQUITY": "equity",
        "novel_thing": None,  # default fallback
    }
    for raw, expected in cases.items():
        body = {
            "results": [{"name": "x", "account_number": "1", "classification": raw}],
            "next": None,
        }
        with patch("httpx.AsyncClient") as cm:
            client = cm.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=_mock_response(200, body))
            out = _run(adapter.list_gl_accounts())
        assert out[0].account_type == expected, f"{raw} should map to {expected}"


def test_merge_dev_list_gl_accounts_drops_account_with_no_code_and_no_name():
    """Both keys missing means the upstream record is unkeyable for our
    upsert. Better to drop than to import a row that the next sync
    can't find again (and would re-create as a duplicate)."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {"results": [{"id": "merge-blank", "classification": "EXPENSE"}], "next": None}
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, body))
        out = _run(adapter.list_gl_accounts())
    assert out == []


def test_merge_dev_list_gl_accounts_falls_back_to_id_when_no_code():
    """When Merge ships a name but no account_number, we still want
    the row — fall back to the upstream id as the upsert key. Common
    on QuickBooks-connected tenants where small businesses skip
    numbering custom expense buckets."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [{"id": "merge-id-77", "name": "Custom Bucket", "classification": "EXPENSE"}],
        "next": None,
    }
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, body))
        out = _run(adapter.list_gl_accounts())
    assert len(out) == 1
    assert out[0].code == "merge-id-77"


def test_merge_dev_list_gl_accounts_follows_pagination_cursor():
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    page1 = {"results": [{"name": "A", "account_number": "1"}], "next": "cursor-2"}
    page2 = {"results": [{"name": "B", "account_number": "2"}], "next": None}
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=[_mock_response(200, page1), _mock_response(200, page2)])
        out = _run(adapter.list_gl_accounts())
        # Cursor passed through on second call.
        assert client.get.await_args_list[1].kwargs["params"]["cursor"] == "cursor-2"
    assert {a.code for a in out} == {"1", "2"}


def test_merge_dev_list_gl_accounts_returns_empty_on_http_error():
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(500, {"detail": "boom"}))
        assert _run(adapter.list_gl_accounts()) == []


def test_merge_dev_list_gl_accounts_returns_empty_on_network_error():
    import httpx

    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=httpx.ConnectError("dns fail"))
        assert _run(adapter.list_gl_accounts()) == []


# ---------- API endpoint integration -------------------------------------


def test_sync_endpoint_uses_dispatcher_not_hardcoded_data():
    """Lock the wiring: the endpoint imports get_erp_adapter and
    calls list_gl_accounts. Catches a regression where someone
    reverts to the old inline 20-row mock list."""
    import inspect

    from app.api import gl_accounts

    src = inspect.getsource(gl_accounts.sync_gl_accounts_from_erp)
    assert "get_erp_adapter" in src
    assert "list_gl_accounts" in src
    # The old hardcoded list lived inline — make sure no one quietly
    # reintroduces it. Pick one of the rows that's distinctive.
    assert "Office Supplies & Expenses" not in src
    assert "Payroll Expense" not in src


def test_base_adapter_list_gl_accounts_default_is_empty_not_raises():
    from app.services.erp_adapters.base import ErpAdapter

    class Bare(ErpAdapter):
        erp_type = "bare"

    assert _run(Bare({}).list_gl_accounts()) == []
