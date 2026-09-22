"""Tests for the ERP adapter `list_pos` contract.

The mock adapter is what the local-dev `/api/purchase-orders/sync-erp`
endpoint runs against, so its shape is the contract the API endpoint
relies on. Merge.dev coverage is HTTP-mocked: full live coverage needs
a sandbox account, but we can lock the request shape (path, headers,
pagination cursor) and the response → `PoPayload` mapping that drives
real customer syncs.

Adapters that don't yet implement PO sync (NetSuite, Business Central)
inherit the base's `[]` default. We assert that explicitly so a future
"raise NotImplementedError" doesn't silently 500 the sync endpoint.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the adapter modules so their @register_adapter decorators
# populate the dispatcher registry. Tests use `get_erp_adapter` to
# resolve adapters by type — without this the registry is empty.
import app.services.erp_adapters.dynamics_365_bc  # noqa: F401, E402
import app.services.erp_adapters.merge_dev  # noqa: F401, E402
import app.services.erp_adapters.mock_adapter  # noqa: F401, E402
import app.services.erp_adapters.netsuite  # noqa: F401, E402


def _run(coro):
    return asyncio.run(coro)


# ---------- Mock adapter ---------------------------------------------------


def test_mock_adapter_list_pos_returns_seeded_catalogue():
    """The local-dev sync flow renders the same three POs every time;
    the data lives in the adapter, not the API endpoint."""
    from app.services.erp_adapters.dispatcher import get_erp_adapter

    adapter = get_erp_adapter({"type": "mock", "integration_method": "direct"})
    pos = _run(adapter.list_pos())
    assert {p.po_number for p in pos} == {"PO-2024-200", "PO-2024-201", "PO-2024-202"}

    # Every PO carries a Decimal total + status + at least one line item.
    for p in pos:
        assert isinstance(p.total, Decimal)
        assert p.status in {"open", "closed", "cancelled"}
        assert len(p.line_items) > 0
        for li in p.line_items:
            # Decimals everywhere money lives — see project invariant
            # "Money is exact" in CLAUDE.md.
            for field in (li.quantity, li.unit_price, li.total):
                assert field is None or isinstance(field, Decimal)


def test_mock_adapter_list_pos_emits_deterministic_expected_delivery_dates():
    """The mock catalogue carries deterministic ``expected_delivery_date``s on
    some POs (so local-first dev exercises the on-time-delivery auto-population
    path end-to-end) and deliberately leaves one PO without one (so the
    "no promised date → leave None, don't fabricate" branch is exercised too)."""
    from app.services.erp_adapters.dispatcher import get_erp_adapter

    adapter = get_erp_adapter({"type": "mock", "integration_method": "direct"})
    pos = {p.po_number: p for p in _run(adapter.list_pos())}

    # Stable across calls — no clock / randomness in the fixture.
    assert pos["PO-2024-200"].expected_delivery_date == date(2024, 6, 15)
    assert pos["PO-2024-201"].expected_delivery_date == date(2024, 7, 1)
    # At least one PO without a promised date — never fabricated.
    assert pos["PO-2024-202"].expected_delivery_date is None


def test_mock_adapter_list_pos_returns_independent_copies():
    """Mutating one call's return must not contaminate the next call.
    Otherwise concurrent requests on the same worker could see each
    other's edits — a sneaky source of nondeterministic test failures."""
    from app.services.erp_adapters.dispatcher import get_erp_adapter

    adapter = get_erp_adapter({"type": "mock", "integration_method": "direct"})
    first = _run(adapter.list_pos())
    first[0].po_number = "MUTATED"
    first[0].line_items[0].description = "MUTATED"

    second = _run(adapter.list_pos())
    assert second[0].po_number != "MUTATED"
    assert second[0].line_items[0].description != "MUTATED"


def test_unknown_erp_type_fails_closed_instead_of_becoming_mock():
    """This test previously asserted the opposite — that a typo'd `type` fell
    back to `mock` "so it doesn't blow up sync-erp".

    That fallback is the bug: `MockAdapter.post_invoice` returns
    `success=True` with a fabricated `MOCK-…` document id, so `services/erp`
    walked the invoice `sending_to_erp → sent_to_erp → done` and recorded an
    ERP reference pointing at nothing. `POST /api/organization/test-erp`
    confirmed the misconfiguration for the same reason
    (`mock.test_connection()` is True). Every caller now turns the refusal
    into a specific outcome — 400 on the sync endpoints, a failed pass in
    `payment_erp_sync`, a named message on test-erp — so nothing "blows up",
    it just stops lying.
    """
    from app.services.erp_adapters.dispatcher import (
        UnknownErpAdapterError,
        get_erp_adapter,
    )

    with pytest.raises(UnknownErpAdapterError) as ei:
        get_erp_adapter({"type": "totally_unknown", "integration_method": "direct"})
    assert ei.value.adapter_key == "totally_unknown"
    # Actionable: names the real alternatives.
    assert "mock" in str(ei.value)


def test_erp_dispatcher_still_resolves_every_registered_adapter():
    """The guard must not break the ordinary path."""
    from app.services.erp_adapters.dispatcher import get_erp_adapter

    assert get_erp_adapter({"type": "mock", "integration_method": "direct"}).erp_type == "mock"
    # `integration_method` defaults to merge_dev — unchanged by the guard.
    assert get_erp_adapter({"type": "netsuite"}).erp_type == "merge_dev"


# ---------- Base / unimplemented adapters --------------------------------


@pytest.mark.parametrize("erp_type", ["dynamics_365_bc", "netsuite"])
def test_unimplemented_adapter_list_pos_returns_empty_list(erp_type: str):
    """Adapters that don't override `list_pos` MUST inherit the base's
    empty-list default. Anything else (raise / None) breaks the sync
    endpoint for any tenant pointed at one of these ERPs."""
    from app.services.erp_adapters.dispatcher import _ADAPTER_REGISTRY

    cls = _ADAPTER_REGISTRY[erp_type]
    # Direct integration_method bypasses Merge.dev so we hit the real
    # adapter class — minimal config is fine since list_pos is a no-op.
    adapter = cls({"type": erp_type, "integration_method": "direct"})
    result = _run(adapter.list_pos())
    assert result == []


# ---------- Merge.dev adapter -------------------------------------------


def _make_mock_response(status_code: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"{}" if json_body is not None else b""
    resp.json = MagicMock(return_value=json_body or {})
    resp.headers = {"content-type": "application/json"}
    return resp


def test_merge_dev_list_pos_maps_response_into_po_payloads():
    """One Merge.dev page → list of PoPayload with the right field
    mapping. Locks the keys we read from the upstream JSON so a future
    refactor can't silently start dropping line items."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body = {
        "results": [
            {
                "id": "merge-id-1",
                "number": "PO-9001",
                "status": "OPEN",
                "total_amount": "1250.00",
                "vendor": {"id": "v-1", "name": "Acme Supplies"},
                "line_items": [
                    {
                        "description": "Toner",
                        "quantity": "5",
                        "unit_price": "100",
                        "total_line_amount": "500",
                        "account": "6010",
                    },
                    {
                        "description": "Paper",
                        "quantity": "30",
                        "unit_price": "25",
                        "total_line_amount": "750",
                    },
                ],
            }
        ],
        "next": None,
    }

    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        pos = _run(adapter.list_pos())

    assert len(pos) == 1
    p = pos[0]
    assert p.po_number == "PO-9001"
    assert p.vendor_name == "Acme Supplies"
    assert p.total == Decimal("1250.00")
    assert p.status == "open"
    assert len(p.line_items) == 2
    assert p.line_items[0].description == "Toner"
    assert p.line_items[0].quantity == Decimal("5")
    assert p.line_items[0].unit_price == Decimal("100")
    assert p.line_items[0].total == Decimal("500")
    assert p.line_items[0].gl_account == "6010"
    assert p.line_items[1].gl_account is None  # absent in payload


def test_merge_dev_list_pos_maps_expected_delivery_date():
    """Merge exposes the promised delivery date under a few field names; the
    mapper maps the first present one onto ``expected_delivery_date`` and parses
    ISO date / datetime strings, falling back to None on anything unparseable
    (never fabricates a date for a real adapter)."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})

    cases = [
        ({"number": "P", "delivery_date": "2025-03-10"}, date(2025, 3, 10)),
        # full ISO datetime → date part
        ({"number": "P", "delivery_date": "2025-03-10T12:00:00Z"}, date(2025, 3, 10)),
        # alternate field names
        ({"number": "P", "expected_delivery_date": "2025-04-01"}, date(2025, 4, 1)),
        ({"number": "P", "requested_delivery_date": "2025-05-02"}, date(2025, 5, 2)),
        # absent → None (no fabrication)
        ({"number": "P"}, None),
        # unparseable garbage → None, must not raise
        ({"number": "P", "delivery_date": "not-a-date"}, None),
    ]
    for raw, expected in cases:
        body = {"results": [raw], "next": None}
        with patch("httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=_make_mock_response(200, body))
            pos = _run(adapter.list_pos())
        assert pos[0].expected_delivery_date == expected, raw


def test_merge_dev_list_pos_maps_currency_and_never_defaults_it():
    """Merge's unified PurchaseOrder names its ``currency`` beside
    ``total_amount``; the mapper passes it through, and a record with none
    maps to None — a real adapter never fills in a default (decisions §197).
    The mock adapter states one on every PO, as the ERP record would."""
    from app.services.erp_adapters.dispatcher import get_erp_adapter
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    cases = [
        ({"number": "P", "currency": "EUR"}, "EUR"),
        ({"number": "P"}, None),
        ({"number": "P", "currency": None}, None),
        ({"number": "P", "currency": 978}, None),  # a numeric ISO code is not ours to guess
    ]
    for raw, expected in cases:
        body = {"results": [raw], "next": None}
        with patch("httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=_make_mock_response(200, body))
            pos = _run(adapter.list_pos())
        assert pos[0].currency == expected, raw

    from app.services.erp_adapters.base import PoPayload

    assert PoPayload(po_number="x").currency is None
    mock = get_erp_adapter({"type": "mock", "integration_method": "direct"})
    assert {p.currency for p in _run(mock.list_pos())} == {"USD"}


def test_po_payload_expected_delivery_date_default_is_none():
    """A real adapter that doesn't set the field must leave it None — the
    on-time scorer treats None as "no promised date" (excluded), so a bogus
    default would silently corrupt every vendor's on-time sub-score."""
    from app.services.erp_adapters.base import PoPayload

    assert PoPayload(po_number="x").expected_delivery_date is None


def test_merge_dev_list_pos_status_mapping_uses_internal_vocab():
    """Whatever Merge calls a status, we normalize into open/closed/
    cancelled — `PurchaseOrder.status` only accepts those values, and
    a stray "FULFILLED" reaching the DB would either error or display
    as junk in the UI."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    cases = {
        "OPEN": "open",
        "CLOSED": "closed",
        "FULFILLED": "closed",
        "CANCELLED": "cancelled",
        "CANCELED": "cancelled",
        "VOIDED": "cancelled",
        "something_weird": "open",  # default fallback
    }

    for raw_status, expected in cases.items():
        body = {"results": [{"number": "P", "status": raw_status}], "next": None}
        with patch("httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=_make_mock_response(200, body))
            pos = _run(adapter.list_pos())
        assert pos[0].status == expected, f"{raw_status} should map to {expected}"


def test_merge_dev_list_pos_follows_pagination_cursor():
    """A real Merge response splits big PO lists across pages joined by
    a `next` cursor. The adapter has to walk the chain or it'll silently
    return only the first 100 POs."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    page1 = {
        "results": [{"number": "P-1"}, {"number": "P-2"}],
        "next": "cursor-page-2",
    }
    page2 = {
        "results": [{"number": "P-3"}],
        "next": None,
    }

    responses = [_make_mock_response(200, page1), _make_mock_response(200, page2)]
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=responses)
        pos = _run(adapter.list_pos())

        # Second call must include the cursor from page 1.
        second_call_kwargs = client.get.await_args_list[1].kwargs
        assert second_call_kwargs["params"]["cursor"] == "cursor-page-2"

    assert [p.po_number for p in pos] == ["P-1", "P-2", "P-3"]


def test_merge_dev_list_pos_returns_empty_on_http_error():
    """Adapter degrades gracefully — the sync endpoint shows "0 new
    POs" instead of bubbling a 502 to the operator clicking the
    button."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(500, {"detail": "boom"}))
        pos = _run(adapter.list_pos())
    assert pos == []


def test_merge_dev_list_pos_returns_empty_on_network_error():
    """Total network failure must not raise out of the adapter — the
    API endpoint converts adapter exceptions to 502, but a `[]` return
    is the friendlier "ERP unreachable, no new POs" outcome."""
    import httpx

    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=httpx.ConnectError("dns fail"))
        pos = _run(adapter.list_pos())
    assert pos == []


# ---------- API endpoint integration --------------------------------------


def test_sync_endpoint_uses_dispatcher_not_hardcoded_data():
    """Smoke test the wiring: the API endpoint imports
    `get_erp_adapter`, not a private mock list. Catches a regression
    where someone reverts to the old hardcoded-mock pattern."""
    import inspect

    from app.api import purchase_orders

    src = inspect.getsource(purchase_orders.sync_pos_from_erp)
    assert "get_erp_adapter" in src
    assert "list_pos" in src
    # The old hardcoded mock list lived inline — make sure no one
    # quietly puts it back.
    assert "PO-2024-200" not in src
    assert "PO-2024-201" not in src


def test_base_adapter_list_pos_default_is_empty_not_raises():
    """Belt-and-braces: a bare `ErpAdapter` subclass with no override
    must return [] from list_pos. If someone changes the default to
    `raise NotImplementedError` they break every adapter that hasn't
    explicitly overridden it."""
    from app.services.erp_adapters.base import ErpAdapter

    class Bare(ErpAdapter):
        erp_type = "bare"

    result = _run(Bare({}).list_pos())
    assert result == []


def test_po_payload_total_default_is_decimal_not_float():
    """Project invariant: money is Decimal. The dataclass default must
    not be 0.0 — a float default would survive into the DB and tests
    would only catch it the next time someone summed totals."""
    from app.services.erp_adapters.base import PoPayload

    payload = PoPayload(po_number="x")
    assert isinstance(payload.total, Decimal)


def test_merge_dev_list_pos_handles_missing_optional_fields():
    """Real ERP data is messy — vendor unset, no line items, totals
    missing. The adapter should map what's there and substitute safe
    defaults for the rest, not crash."""
    from app.services.erp_adapters.merge_dev import MergeDevAdapter

    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    body: dict[str, Any] = {
        "results": [
            {
                # No number, no vendor, no lines, no total. Worst-case payload.
                "id": "fallback-id",
            }
        ],
        "next": None,
    }
    with patch("httpx.AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_make_mock_response(200, body))
        pos = _run(adapter.list_pos())

    assert len(pos) == 1
    p = pos[0]
    assert p.po_number == "fallback-id"  # falls back to id
    assert p.vendor_name is None
    assert p.total == Decimal("0")
    assert p.status == "open"
    assert p.line_items == []
