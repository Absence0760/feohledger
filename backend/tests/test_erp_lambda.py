"""Tests for the SQS-triggered ERP Lambda (erp_mode = "lambda").

Same two-engine consumer shape as the extraction Lambda: a control engine
resolves the tenant DB name, a tenant engine loads the invoice and runs the
ERP send. DB-free with mocked engines/sessions. Covers the deployed entry
point and the branches the existing suite never executes:

  * the tenant engine targets ``org.db_name`` — guards against routing a
    stale / cross-org SQS message into an arbitrary tenant DB
  * org-not-found and invoice-not-found short-circuits don't call the ERP and
    dispose engines
  * a send failure rolls back AND re-raises so SQS redelivers the ERP send
    rather than silently dropping it
  * the handler SQS batch loop parses each record and returns statusCode 200
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import erp_lambda

# Fake orgs carry their provider secrets inline; see the fixture.
pytestmark = pytest.mark.usefixtures("provider_store_from_settings")

BASE_URL = "postgresql+asyncpg://u:p@host:5432/feohledger"


def _body(**overrides) -> dict:
    body = {
        "invoice_id": str(uuid.uuid4()),
        "org_id": str(uuid.uuid4()),
        "actor_id": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


def _result(scalar):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=scalar)
    return r


class _FakeSession:
    def __init__(self, exec_result) -> None:
        self._exec_result = exec_result
        self.rollback = AsyncMock()

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def execute(self, *_a, **_k):
        return self._exec_result


@contextmanager
def _harness(*, org, invoice, send: AsyncMock | None = None):
    control_engine = MagicMock(dispose=AsyncMock())
    tenant_engine = MagicMock(dispose=AsyncMock())
    ctrl_session = _FakeSession(_result(org))
    tenant_session = _FakeSession(_result(invoice))
    send_mock = send if send is not None else AsyncMock()
    with (
        patch.dict("os.environ", {"DATABASE_URL": BASE_URL}),
        patch.object(
            erp_lambda,
            "create_async_engine",
            MagicMock(side_effect=[control_engine, tenant_engine]),
        ) as create_engine,
        patch.object(
            erp_lambda,
            "async_sessionmaker",
            MagicMock(
                side_effect=[
                    MagicMock(return_value=ctrl_session),
                    MagicMock(return_value=tenant_session),
                ]
            ),
        ),
        patch("app.services.erp.send_to_erp_internal", send_mock),
    ):
        yield SimpleNamespace(
            create_engine=create_engine,
            control_engine=control_engine,
            tenant_engine=tenant_engine,
            send=send_mock,
        )


async def test_process_message_routes_to_tenant_db_and_sends():
    body = _body()
    invoice = SimpleNamespace(id=uuid.UUID(body["invoice_id"]))
    erp_cfg = {"type": "netsuite", "integration_method": "direct", "account_id": "ACCT"}
    org = SimpleNamespace(
        id=uuid.UUID(body["org_id"]), db_name="feoh_acme", settings={"erp": erp_cfg}
    )
    with _harness(org=org, invoice=invoice) as h:
        await erp_lambda._process_message(body)

    h.send.assert_awaited_once()
    assert h.send.await_args.args[1] is invoice
    assert h.send.await_args.kwargs["actor_id"] == uuid.UUID(body["actor_id"])
    # The org's configured ERP rides into the send — without it the lambda
    # path posts via the mock adapter no matter what the tenant configured.
    assert h.send.await_args.kwargs["erp_config"] == erp_cfg
    assert (
        h.create_engine.call_args_list[1].args[0] == "postgresql+asyncpg://u:p@host:5432/feoh_acme"
    )
    h.control_engine.dispose.assert_awaited_once()
    h.tenant_engine.dispose.assert_awaited_once()


async def test_process_message_unknown_org_short_circuits_without_tenant_engine():
    with _harness(org=None, invoice=None) as h:
        await erp_lambda._process_message(_body())
    h.send.assert_not_awaited()
    h.control_engine.dispose.assert_awaited_once()
    h.tenant_engine.dispose.assert_not_awaited()
    assert h.create_engine.call_count == 1


async def test_process_message_unknown_invoice_is_noop_and_disposes_both():
    org = SimpleNamespace(id=uuid.uuid4(), db_name="feoh_acme", settings=None)
    with _harness(org=org, invoice=None) as h:
        await erp_lambda._process_message(_body())
    h.send.assert_not_awaited()
    h.control_engine.dispose.assert_awaited_once()
    h.tenant_engine.dispose.assert_awaited_once()


async def test_process_message_send_failure_rolls_back_and_reraises():
    org = SimpleNamespace(id=uuid.uuid4(), db_name="feoh_acme", settings=None)
    invoice = SimpleNamespace(id=uuid.uuid4())
    boom = AsyncMock(side_effect=RuntimeError("erp 500"))
    with _harness(org=org, invoice=invoice, send=boom) as h:
        with pytest.raises(RuntimeError, match="erp 500"):
            await erp_lambda._process_message(_body())
    h.control_engine.dispose.assert_awaited_once()
    h.tenant_engine.dispose.assert_awaited_once()


def test_handler_processes_each_record_and_returns_200():
    bodies = [_body(), _body()]
    event = {"Records": [{"body": json.dumps(b)} for b in bodies]}
    seen: list[dict] = []
    fake_loop = MagicMock(run_until_complete=MagicMock())
    with (
        patch.object(erp_lambda.asyncio, "get_event_loop", return_value=fake_loop),
        patch.object(erp_lambda, "_process_message", new=MagicMock(side_effect=seen.append)),
    ):
        result = erp_lambda.handler(event, None)
    assert result == {"statusCode": 200}
    assert [b["invoice_id"] for b in seen] == [b["invoice_id"] for b in bodies]
