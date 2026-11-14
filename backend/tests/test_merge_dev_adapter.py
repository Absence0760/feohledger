"""Merge.dev's bill push against Merge's published write contract (decisions §399).

Checked against https://docs.merge.dev/openapi/accounting.json and Merge's
write-error reference: a bill line's amount is `total_amount`, the create
answers `{model, warnings, errors}` with each error's `problem_type`, and only
that code (never `detail`) decides what the refusal is. The idempotency
lookup and header are in `test_erp_adapter_idempotency.py`, the exact-money
encoding in `test_erp_adapter_money_exact.py`, the webhook in
`test_merge_webhook.py`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.erp_adapters.base import InvoicePayload, LineItemPayload
from app.services.erp_adapters.merge_dev import MergeDevAdapter


def _run(coro):
    return asyncio.run(coro)


def _resp(status: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=body if body is not None else {})
    return resp


def _payload(**overrides) -> InvoicePayload:
    base = dict(
        invoice_number="INV-7",
        vendor_name="Acme",
        amount=Decimal("120.00"),
        currency="USD",
        invoice_date=date(2026, 11, 1),
        correlation_id="corr-merge-7",
        vendor_erp_id="merge-vendor-701",
        gl_account="6100",
        gl_account_erp_id="merge-acct-6100",
        line_items=[
            LineItemPayload(
                line_number=1,
                description="Paper",
                total=Decimal("100.00"),
                gl_account="6100",
                gl_account_erp_id="merge-acct-6100",
            ),
            LineItemPayload(
                line_number=2,
                description="Licence",
                total=Decimal("20.00"),
                gl_account="6200",
                gl_account_erp_id="merge-acct-6200",
            ),
        ],
    )
    base.update(overrides)
    return InvoicePayload(**base)


_NO_BILL = _resp(200, {"next": None, "results": []})
_ADAPTER = {"api_key": "k", "account_token": "tok"}


def _push(create: MagicMock, *, reads: dict[str, MagicMock] | None = None, payload=None):
    """Post through the adapter: the lookup finds nothing, the create answers
    ``create``, and a record read (``contacts/…`` / ``accounts/…``) answers
    from ``reads`` (404 when absent)."""
    reads = reads or {}

    async def get(url, **kwargs):
        if url.endswith("/invoices"):
            return _NO_BILL
        for suffix, resp in reads.items():
            if url.endswith(suffix):
                return resp
        return _resp(404)

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(side_effect=get)
        client.post = AsyncMock(return_value=create)
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(payload or _payload()))
    return result, client


def _refused(*problems: str, status: int = 400) -> MagicMock:
    return _resp(
        status,
        {"model": None, "errors": [{"problem_type": p, "detail": "x"} for p in problems]},
    )


# ---------------------------------------------------------------------------
# what is sent
# ---------------------------------------------------------------------------


def test_each_line_is_sent_with_its_amount_as_total_amount_on_its_own_account():
    created = _resp(201, {"model": {"id": "m1", "number": "INV-7", "total_amount": 120.00}})
    result, client = _push(created)
    assert result.success and result.erp_document_id == "m1"
    model = json.loads(client.post.await_args.kwargs["content"])["model"]
    assert model["line_items"] == [
        {"description": "Paper", "total_amount": 100.00, "account": "merge-acct-6100"},
        {"description": "Licence", "total_amount": 20.00, "account": "merge-acct-6200"},
    ]
    assert model["total_amount"] == 120.00 and model["inclusive_of_tax"] is True
    # Fields Merge's Invoice does not have, or a marker it never echoes back.
    for absent in ("purchase_order_number", "integration_params", "sub_total"):
        assert absent not in model


@pytest.mark.parametrize(
    ("lines", "reason"),
    [
        # Lines that do not add up to the approved amount: never posted.
        (
            [LineItemPayload(line_number=1, total=Decimal("99.00"), gl_account_erp_id="a")],
            "amount_mismatch",
        ),
        # A line with no amount and nothing to derive one from.
        ([LineItemPayload(line_number=1, gl_account_erp_id="a")], "line_amount_missing"),
        # A coded line whose account never synced.
        (
            [LineItemPayload(line_number=1, total=Decimal("120.00"), gl_account="6900")],
            "account_not_linked",
        ),
    ],
)
def test_a_bill_whose_lines_cannot_be_stated_is_refused_before_any_call(lines, reason):
    with patch("httpx.AsyncClient") as cm:
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(_payload(line_items=lines)))
    assert result.refusal_code == reason and result.not_posted and not result.retryable
    cm.assert_not_called()


# ---------------------------------------------------------------------------
# what a 2xx means
# ---------------------------------------------------------------------------


def test_a_posted_total_other_than_ours_is_never_success():
    result, _ = _push(_resp(201, {"model": {"id": "m1", "total_amount": 132.00}}))
    assert not result.success and not result.retryable
    assert result.refusal_code == "posted_total_mismatch"
    assert result.erp_document_id == "m1"  # Merge cannot void: left for a person


def test_a_created_model_with_no_total_is_unconfirmed():
    result, _ = _push(_resp(201, {"model": {"id": "m1"}}))
    assert not result.success and result.refusal_code == "posted_total_unconfirmed"


def test_a_2xx_with_no_model_id_is_not_a_confirmed_bill():
    result, _ = _push(_resp(201, {"model": {}, "errors": []}))
    assert not result.success and not result.retryable
    assert result.refusal_code == "bill_refused"
    assert result.not_posted is False  # nothing says it was not created


def test_a_2xx_carrying_errors_keeps_the_model_id_it_names():
    result, _ = _push(
        _resp(201, {"model": {"id": "m9"}, "errors": [{"problem_type": "PROVIDER_ERROR"}]})
    )
    assert result.refusal_code == "bill_refused" and result.not_posted is False
    assert result.erp_document_id == "m9"


# ---------------------------------------------------------------------------
# what a refusal means
# ---------------------------------------------------------------------------


def test_a_401_is_not_connected_and_not_retried():
    result, _ = _push(_resp(401, {"detail": "Invalid token"}))
    assert result.refusal_code == "not_connected"
    assert not result.retryable and result.not_posted


@pytest.mark.parametrize("problem", ["RELINK_NEEDED", "CREDENTIAL_REFRESH_FAILED"])
def test_a_link_that_needs_relinking_is_not_connected(problem):
    result, _ = _push(_refused(problem))
    assert result.refusal_code == "not_connected" and result.not_posted
    assert result.refusal_params == {"merge_problem": problem}


@pytest.mark.parametrize(
    "problem", ["DUPLICATE_MODEL_EXISTS", "MODEL_WITH_DUPLICATE_UNIQUE_FIELD_EXISTS"]
)
def test_a_duplicate_is_coded_but_stays_possibly_posted(problem):
    """The record holding the number may be our own earlier attempt, which
    Merge's synced copy did not yet show the lookup."""
    result, _ = _push(_refused(problem))
    assert result.refusal_code == "duplicate_document_number"
    assert result.not_posted is False and not result.retryable


def test_a_provider_error_names_an_archived_vendor():
    result, client = _push(
        _refused("PROVIDER_ERROR"),
        reads={"contacts/merge-vendor-701": _resp(200, {"id": "v", "status": "ARCHIVED"})},
    )
    assert result.refusal_code == "vendor_inactive" and result.not_posted


def test_a_provider_error_names_each_inactive_account_by_our_code():
    result, _ = _push(
        _refused("PROVIDER_ERROR"),
        reads={
            "contacts/merge-vendor-701": _resp(200, {"status": "ACTIVE"}),
            "accounts/merge-acct-6200": _resp(200, {"status": "INACTIVE"}),
            "accounts/merge-acct-6100": _resp(200, {"status": "ACTIVE"}),
        },
    )
    assert result.refusal_code == "account_inactive" and result.not_posted
    assert result.refusal_params == {"merge_problem": "PROVIDER_ERROR", "accounts": ["6200"]}


def test_a_provider_error_with_nothing_inactive_or_unreadable_is_bill_refused():
    """An unreadable record is "don't know", never "inactive". And a bare
    PROVIDER_ERROR is the integration's catch-all, which says nothing about
    whether a multi-step write left a bill: kept possibly posted."""
    result, _ = _push(_refused("PROVIDER_ERROR"))
    assert result.refusal_code == "bill_refused" and result.not_posted is False


def test_a_documented_not_created_problem_is_bill_refused():
    result, _ = _push(_refused("MISSING_REQUIRED_FIELD", "INVALID_FIELD_CHOICE"))
    assert result.refusal_code == "bill_refused" and result.not_posted
    assert result.refusal_params == {"merge_problem": "MISSING_REQUIRED_FIELD"}


@pytest.mark.parametrize(
    "create",
    [
        _refused("ERROR"),  # generic: says nothing about whether it landed
        _refused("MISSING_REQUIRED_FIELD", "SOMETHING_UNDOCUMENTED"),
        _resp(400, {"detail": "no errors array"}),
    ],
)
def test_an_undocumented_or_absent_problem_stays_possibly_posted(create):
    result, _ = _push(create)
    assert not result.success and not result.retryable
    assert result.refusal_code is None and result.not_posted is False
    assert result.message.startswith("Merge.dev post failed: HTTP 400")


def test_merges_rate_limit_is_retried():
    result, _ = _push(_refused("RATE_LIMITED", status=429))
    assert not result.success and result.retryable and result.not_posted is False


@pytest.mark.parametrize("status", [500, 502, 503, 408])
def test_a_create_that_may_have_landed_is_not_retried_automatically(status):
    """The automatic retry would come within seconds, before Merge's listing
    may show a bill written through it, so the lookup could miss it and post a
    second one (the Idempotency-Key beta may be off). Possibly posted; a
    person's retry runs the lookup again (decisions §399)."""
    result, _ = _push(_refused("ERROR", status=status))
    assert not result.success and not result.retryable and result.not_posted is False


@pytest.mark.parametrize("exc", ["ReadTimeout", "RemoteProtocolError", "WriteTimeout"])
def test_a_lost_create_response_is_not_retried_automatically(exc):
    import httpx

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_NO_BILL)
        client.post = AsyncMock(side_effect=getattr(httpx, exc)("t"))
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(_payload()))
    assert not result.success and not result.retryable and result.not_posted is False
    assert "outcome unknown" in result.message


@pytest.mark.parametrize("exc", ["ConnectError", "ConnectTimeout"])
def test_a_create_that_never_connected_is_retried(exc):
    import httpx

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_NO_BILL)
        client.post = AsyncMock(side_effect=getattr(httpx, exc)("t"))
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(_payload()))
    assert not result.success and result.retryable


def test_a_401_on_the_lookup_is_not_connected_before_anything_is_sent():
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(401))
        client.post = AsyncMock()
        result = _run(MergeDevAdapter(_ADAPTER).post_invoice(_payload()))
    assert result.refusal_code == "not_connected" and result.not_posted
    client.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# the connection test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "ok"),
    [
        (200, {"status": "COMPLETE"}, True),
        (200, {"status": "IDLE"}, True),
        (200, {}, True),
        # Merge answers 200 for a link that cannot write.
        (200, {"status": "RELINK_NEEDED"}, False),
        (200, {"status": "INCOMPLETE"}, False),
        (401, {}, False),
    ],
)
def test_the_connection_test_reads_the_linked_accounts_status(status, body, ok):
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(status, body))
        assert _run(MergeDevAdapter(_ADAPTER).test_connection()) is ok


# ---------------------------------------------------------------------------
# an unknown region, outside the push
# ---------------------------------------------------------------------------


def test_an_unknown_region_fails_a_listing_as_incomplete_not_a_500():
    from app.services.erp_adapters.base import ErpListingIncomplete

    adapter = MergeDevAdapter({**_ADAPTER, "region": "mars"})
    for listing in (adapter.list_vendors, adapter.list_gl_accounts, adapter.list_pos_report):
        with patch("httpx.AsyncClient") as cm, pytest.raises(ErpListingIncomplete):
            _run(listing())
        cm.assert_not_called()


def test_an_unknown_region_reads_status_unknown():
    from app.services.erp_adapters.base import ErpInvoiceStatus

    with patch("httpx.AsyncClient") as cm:
        status = _run(MergeDevAdapter({**_ADAPTER, "region": "mars"}).get_invoice_status("b"))
    assert status is ErpInvoiceStatus.unknown
    cm.assert_not_called()


# ---------------------------------------------------------------------------
# the bill read the webhook makes
# ---------------------------------------------------------------------------


def test_bill_report_reads_the_bill_back_from_merge():
    from app.services.erp_adapters.base import ErpBillState

    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(200, {"id": "b1", "status": "VOID"}))
        report = _run(MergeDevAdapter(_ADAPTER).bill_report("b1"))
    assert report.state is ErpBillState.deleted and report.bill_id == "b1"
    assert client.get.await_args.args[0].endswith("/invoices/b1")


def test_bill_report_never_reads_an_unreadable_bill_as_gone():
    from app.services.erp_adapters.merge_dev import MergeBillNotFound

    adapter = MergeDevAdapter(_ADAPTER)
    with patch("httpx.AsyncClient") as cm:
        client = cm.return_value.__aenter__.return_value
        client.get = AsyncMock(return_value=_resp(404))
        with pytest.raises(MergeBillNotFound):
            _run(adapter.bill_report("b1"))
        client.get = AsyncMock(return_value=_resp(500))
        with pytest.raises(RuntimeError):
            _run(adapter.bill_report("b1"))
        # A body for another bill is not this bill's state.
        client.get = AsyncMock(return_value=_resp(200, {"id": "b2", "status": "VOID"}))
        with pytest.raises(RuntimeError):
            _run(adapter.bill_report("b1"))
