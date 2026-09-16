"""The three real ERP adapters post money EXACTLY — no `float()` on the wire.

Project invariant #1: "Money is exact. Amounts use `Decimal` (never `float`)."
`post_invoice` is where an approved invoice becomes a row in the customer's own
general ledger, so it is the least forgiving place in the app to break it. Each
adapter used to build its request body with `float(payload.amount)` and hand the
result to httpx's `json=`, which re-encodes it with `repr`.

That is wrong in three separable ways, and the tests below pin all three:

  * **Digits** — a double holds ~15 significant decimal digits, so
    `Decimal("99999999999999.99")` serialises as `99999999999999.98`: a one-cent
    error, posted, in a ledger. Today's `Numeric(15, 2)` columns happen to stay
    inside the exactly-representable range, so nothing is currently corrupted —
    which is precisely why this needs a test rather than a comment. The
    protection is a property of the current schema, not of the code, and the
    first money column widened past 15 digits silently loses cents.
  * **Scale** — `Decimal("1250.00")` becomes `1250.0`. The ledger's own notion
    of the amount is two decimal places; the wire should say so.
  * **Notation** — `float(Decimal("0.00001"))` renders `1e-05`. Legal JSON,
    routinely rejected by an ERP's numeric field parser.

The fix keeps the wire contract identical — these fields are JSON **numbers** in
all three target APIs (Merge.dev's unified `Invoice` declares `"type": "number"`,
NetSuite's `vendorBill` and BC's `purchaseInvoiceLines` document numeric
examples), so quoting them would be a contract change rather than a rounding
fix. `utils/json_money.dumps_exact_json` emits the Decimal's own literal as a
JSON number, and the adapters pass it as `content=` because httpx's `json=`
cannot carry a Decimal.

Assertions are made against the raw request BODY TEXT, never `json.loads` of it
— parsing turns the literal straight back into a Python float and would pass
against exactly the bug this file exists to catch.

HTTP is mocked with the same `patch("httpx.AsyncClient")` style as
`test_erp_adapter_idempotency.py` — no live fake-erp container needed.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services.erp_adapters.base import InvoicePayload, LineItemPayload
from app.services.erp_adapters.dynamics_365_bc import BusinessCentralAdapter
from app.services.erp_adapters.merge_dev import MergeDevAdapter
from app.services.erp_adapters.netsuite import NetSuiteAdapter
from app.utils.json_money import dumps_exact_json, exact_number_literal

ERP_ADAPTER_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "erp_adapters"

# An amount whose double is NOT the amount. `float()` renders it one cent low.
LOSSY_AMOUNT = Decimal("99999999999999.99")
LOSSY_AMOUNT_AS_FLOAT = "99999999999999.98"

# Two decimal places that a float round-trip flattens to one.
SCALED_AMOUNT = Decimal("1250.00")


def _run(coro):
    return asyncio.run(coro)


def _mock_response(status: int, body: dict | None, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"{}" if body is not None else b""
    resp.json = MagicMock(return_value=body or {})
    resp.headers = {"content-type": "application/json", **(headers or {})}
    resp.raise_for_status = MagicMock()
    return resp


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        invoice_number="INV-1",
        vendor_name="Acme",
        amount=LOSSY_AMOUNT,
        currency="USD",
        invoice_date=date(2026, 1, 1),
        correlation_id="corr-money-1",
        subtotal=SCALED_AMOUNT,
        tax_amount=Decimal("0.10"),
        discount_amount=Decimal("5.50"),
        gl_account="6000",
        line_items=[
            LineItemPayload(
                line_number=1,
                description="Widgets",
                quantity=Decimal("3.5000"),
                unit_price=SCALED_AMOUNT,
                total=LOSSY_AMOUNT,
                gl_account="6000",
            )
        ],
    )
    base.update(overrides)
    return InvoicePayload(**base)


def _posted_body_text(client, call_index: int = 0) -> str:
    """The exact bytes the adapter put on the wire, as text.

    `content=` (not `json=`) is itself part of the contract: httpx's `json=`
    re-encodes through `json.dumps`, which cannot serialise a Decimal at all.
    """
    call = client.post.await_args_list[call_index]
    assert "json" not in call.kwargs, (
        "adapter must serialise its own body (`content=`) so Decimals survive; "
        "httpx's `json=` cannot encode a Decimal"
    )
    content = call.kwargs["content"]
    return content.decode() if isinstance(content, bytes) else content


def _assert_exact(body_text: str, *fields: str) -> None:
    for field in fields:
        assert f'"{field}":{LOSSY_AMOUNT}' in body_text or f'"{field}":{SCALED_AMOUNT}' in body_text
    assert LOSSY_AMOUNT_AS_FLOAT not in body_text, (
        f"a float-rounded amount reached the wire: {body_text}"
    )


# ---------------------------------------------------------------------------
# The encoder itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (Decimal("99999999999999.99"), "99999999999999.99"),  # digits survive
        (Decimal("1250.00"), "1250.00"),  # scale survives
        (Decimal("0.00001"), "0.00001"),  # no exponent notation
        (Decimal("1E+3"), "1000"),  # a Decimal already in exponent form
        (Decimal("-0.01"), "-0.01"),
        (Decimal("0"), "0"),
    ],
)
def test_exact_number_literal_renders_the_decimals_own_digits(value: Decimal, expected: str):
    assert exact_number_literal(value) == expected
    # And it is still a JSON *number*, not a string — the wire contract.
    assert json.loads(expected) is not None or expected == "0"


def test_exact_number_literal_refuses_non_finite():
    """NaN / Infinity have no JSON form. Coercing one would post a fabricated
    amount; raising surfaces the upstream bug instead."""
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError, match="non-finite"):
            exact_number_literal(bad)


def test_dumps_exact_json_emits_numbers_not_strings():
    """Quoting money would be a wire-contract change: every field here is typed
    `number` by the ERP that receives it."""
    text = dumps_exact_json({"total_amount": Decimal("1250.00")})
    assert text == '{"total_amount":1250.00}'
    assert '"1250.00"' not in text


def test_dumps_exact_json_handles_nesting_and_leaves_everything_else_to_stdlib():
    body = {
        "model": {
            "memo": 'He said "hi" — ünïcode',
            "nothing": None,
            "flag": True,
            "count": 3,
            "lines": [{"total": Decimal("21.00")}, {"total": Decimal("50.10")}],
        }
    }
    text = dumps_exact_json(body)
    assert '"lines":[{"total":21.00},{"total":50.10}]' in text
    # Non-Decimal values render exactly as json.dumps would.
    parsed = json.loads(text)
    assert parsed["model"]["memo"] == 'He said "hi" — ünïcode'
    assert parsed["model"]["nothing"] is None
    assert parsed["model"]["flag"] is True
    assert parsed["model"]["count"] == 3


def test_dumps_exact_json_refuses_non_string_keys():
    """json.dumps silently coerces an int key to a string; a wire body whose
    field names are fixed literals has no business carrying one."""
    with pytest.raises(TypeError, match="keys must be str"):
        dumps_exact_json({1: Decimal("1.00")})


def test_dumps_exact_json_still_refuses_unserialisable_values():
    with pytest.raises(TypeError):
        dumps_exact_json({"when": date(2026, 1, 1)})


# ---------------------------------------------------------------------------
# merge_dev
# ---------------------------------------------------------------------------


def test_merge_dev_posts_exact_decimal_amounts():
    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(201, {"model": {"id": "m1"}}))
        result = _run(adapter.post_invoice(_payload()))

    assert result.success
    body = _posted_body_text(client)
    _assert_exact(body, "total_amount", "sub_total", "total_line_amount", "unit_price")
    assert '"quantity":3.5000' in body
    assert '"total_tax_amount":0.10' in body
    assert '"total_discount":5.50' in body
    # Still a JSON object the far end can parse, with the header it advertises.
    assert json.loads(body)["model"]["number"] == "INV-1"
    assert client.post.await_args.kwargs["headers"]["Content-Type"] == "application/json"


def test_merge_dev_omits_absent_optional_money_unchanged():
    """The `if x else None` shape around every optional amount is behaviour the
    fix had to preserve, not change — a zero subtotal still sends null."""
    adapter = MergeDevAdapter({"api_key": "k", "account_token": "tok"})
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.post = AsyncMock(return_value=_mock_response(201, {"model": {"id": "m1"}}))
        _run(adapter.post_invoice(_payload(subtotal=None, tax_amount=Decimal("0.00"))))

    parsed = json.loads(_posted_body_text(client))
    assert parsed["model"]["sub_total"] is None
    assert parsed["model"]["total_tax_amount"] is None


# ---------------------------------------------------------------------------
# netsuite
# ---------------------------------------------------------------------------


def _netsuite_adapter() -> NetSuiteAdapter:
    return NetSuiteAdapter(
        {
            "account_id": "123456",
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "token_id": "tid",
            "token_secret": "ts",
        }
    )


def test_netsuite_posts_exact_decimal_rates():
    adapter = _netsuite_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, {"items": [], "count": 0}))
        client.post = AsyncMock(
            return_value=_mock_response(204, None, headers={"Location": "https://x/vendorBill/42"})
        )
        result = _run(adapter.post_invoice(_payload()))

    assert result.success
    body = _posted_body_text(client)
    _assert_exact(body, "rate")
    assert '"quantity":3.5000' in body
    assert client.post.await_args.kwargs["headers"]["Content-Type"] == "application/json"


def test_netsuite_header_only_invoice_posts_the_exact_amount():
    """With no line items the header amount becomes the single line's `rate` —
    the one number that lands in the ledger."""
    adapter = _netsuite_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, {"items": [], "count": 0}))
        client.post = AsyncMock(
            return_value=_mock_response(204, None, headers={"Location": "https://x/vendorBill/43"})
        )
        _run(adapter.post_invoice(_payload(line_items=[])))

    body = _posted_body_text(client)
    assert f'"rate":{LOSSY_AMOUNT}' in body
    assert LOSSY_AMOUNT_AS_FLOAT not in body


def test_netsuite_line_without_unit_price_falls_back_to_the_exact_total():
    adapter = _netsuite_adapter()
    line = LineItemPayload(
        line_number=1, quantity=Decimal("1"), unit_price=None, total=SCALED_AMOUNT
    )
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, {"items": [], "count": 0}))
        client.post = AsyncMock(
            return_value=_mock_response(204, None, headers={"Location": "https://x/vendorBill/44"})
        )
        _run(adapter.post_invoice(_payload(line_items=[line])))

    assert f'"rate":{SCALED_AMOUNT}' in _posted_body_text(client)


# ---------------------------------------------------------------------------
# dynamics_365_bc
# ---------------------------------------------------------------------------


def _bc_adapter() -> BusinessCentralAdapter:
    return BusinessCentralAdapter(
        {
            "tenant_id": "tid-1",
            "client_id": "cid",
            "client_secret": "sec",
            "environment": "sandbox",
            "company_id": "c-1",
            "base_url": "https://api.businesscentral.dynamics.com/v2.0",
        }
    )


def test_d365_posts_exact_decimal_unit_costs(monkeypatch):
    # Operator override keeps the SSRF guard (and its DNS lookup) out of a unit
    # test — same technique as test_erp_base_url_overrides.py.
    monkeypatch.setattr(settings, "erp_d365_api_base", "http://fake-erp:12112/d365")
    monkeypatch.setattr(settings, "erp_d365_token_url", "http://fake-erp:12112/token")
    adapter = _bc_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, {"value": []}))
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),  # token exchange
                _mock_response(201, {"id": "bc-1", "number": "PI-1"}),  # create
                _mock_response(200, {}),  # Microsoft.NAV.post finalize
            ]
        )
        result = _run(adapter.post_invoice(_payload()))

    assert result.success
    # call 0 is the token exchange (form-encoded); call 1 is the create.
    body = _posted_body_text(client, call_index=1)
    _assert_exact(body, "unitCost")
    assert '"quantity":3.5000' in body
    assert client.post.await_args_list[1].kwargs["headers"]["Content-Type"] == "application/json"


def test_d365_header_only_invoice_posts_the_exact_amount(monkeypatch):
    monkeypatch.setattr(settings, "erp_d365_api_base", "http://fake-erp:12112/d365")
    monkeypatch.setattr(settings, "erp_d365_token_url", "http://fake-erp:12112/token")
    adapter = _bc_adapter()
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_mock_response(200, {"value": []}))
        client.post = AsyncMock(
            side_effect=[
                _mock_response(200, {"access_token": "tok"}),
                _mock_response(201, {"id": "bc-2", "number": "PI-2"}),
                _mock_response(200, {}),
            ]
        )
        _run(adapter.post_invoice(_payload(line_items=[])))

    body = _posted_body_text(client, call_index=1)
    assert f'"unitCost":{LOSSY_AMOUNT}' in body
    assert LOSSY_AMOUNT_AS_FLOAT not in body


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


def test_no_erp_adapter_casts_anything_to_float():
    """`float()` anywhere in an ERP adapter is the regression this file exists
    for — every value these modules handle is either money, a quantity, or a
    rate, and none of them survives a double intact."""
    offenders: list[str] = []
    for path in sorted(ERP_ADAPTER_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "ERP adapters must keep money as Decimal and serialise with "
        f"utils/json_money.dumps_exact_json — float() found at: {offenders}"
    )
