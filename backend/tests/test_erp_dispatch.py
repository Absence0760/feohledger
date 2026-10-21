"""Tests for erp_dispatch routing — the inverse of test_extraction_dispatch's
lambda-routing test, which erp_dispatch lacked despite identical structure.

DB-free. Covers:
  * dispatch_erp routes to SQS in lambda mode and runs nothing locally
  * dispatch_erp schedules the local send on the CALLER's event loop (never a
    detached thread with its own loop) and never touches SQS
  * the fire-and-forget task is strongly referenced, so it can't be collected
    mid-await
  * _send_to_sqs message body + FIFO MessageGroupId shape (a silent Lambda
    payload regression double-posts to the ERP, so the contract is pinned)

The aws_endpoint_url → s3_endpoint_url fallback for this module's _send_to_sqs
is pinned alongside the extraction variant in test_aws_endpoint_override.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import MagicMock, patch

from app.config import settings
from app.services import erp_dispatch


async def test_dispatch_erp_lambda_routes_to_sqs_and_runs_nothing_locally(monkeypatch):
    monkeypatch.setattr(settings, "erp_mode", "lambda")
    with (
        patch.object(erp_dispatch, "_send_to_sqs") as send,
        patch.object(erp_dispatch, "_run_local") as run_local,
    ):
        await erp_dispatch.dispatch_erp(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    send.assert_called_once()
    run_local.assert_not_called()


async def test_dispatch_erp_local_runs_on_the_callers_loop_not_a_new_one(monkeypatch):
    """Local mode must schedule the send on the loop it was called from.

    It used to run in a detached thread on a brand-new loop. The send reaches
    `transition_invoice`, whose notification / audit / webhook hooks resolve
    through the app-loop engines in `app.database`; driving those from a second
    loop raises `RuntimeError: got Future attached to a different loop` and can
    return the half-used connection to the pool the REQUEST path draws from, so
    unrelated endpoints hang behind it. Asserting loop identity is what stops
    the thread being "restored" for symmetry with `extraction_dispatch`.
    """
    monkeypatch.setattr(settings, "erp_mode", "local")
    seen: dict[str, object] = {}
    started = asyncio.Event()

    async def _fake_run_local(invoice_id, org_id, actor_id):
        seen["loop"] = asyncio.get_running_loop()
        seen["args"] = (invoice_id, org_id, actor_id)
        started.set()

    ids = (uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    with (
        patch.object(erp_dispatch, "_send_to_sqs") as send,
        patch.object(erp_dispatch, "_run_local", _fake_run_local),
    ):
        await erp_dispatch.dispatch_erp(*ids)
        await asyncio.wait_for(started.wait(), timeout=5)

    send.assert_not_called()
    assert seen["args"] == ids
    assert seen["loop"] is asyncio.get_running_loop(), (
        "the ERP send ran on a different event loop than its caller — the "
        "shared control-plane engine cannot be used across loops"
    )


async def test_dispatch_erp_keeps_a_strong_reference_to_the_task(monkeypatch):
    """`asyncio` holds only a weak reference to a running task, so a
    fire-and-forget send with no referent can be collected mid-await and vanish
    — the invoice would sit at `sending_to_erp` with nothing to re-invoke."""
    monkeypatch.setattr(settings, "erp_mode", "local")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking_run_local(invoice_id, org_id, actor_id):
        entered.set()
        await release.wait()

    with patch.object(erp_dispatch, "_run_local", _blocking_run_local):
        await erp_dispatch.dispatch_erp(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert len(erp_dispatch._dispatch_tasks) == 1, "in-flight task is not strongly referenced"
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if not erp_dispatch._dispatch_tasks:
                break

    assert erp_dispatch._dispatch_tasks == set(), "completed task was not discarded"


def test_send_to_sqs_message_body_and_group_id_shape(monkeypatch):
    invoice_id, org_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(settings, "aws_endpoint_url", "http://aws:4566")
    monkeypatch.setattr(settings, "s3_access_key", "ak")
    monkeypatch.setattr(settings, "s3_secret_key", "sk")
    monkeypatch.setattr(settings, "sqs_erp_queue_url", "http://q/erp.fifo")

    client = MagicMock()
    with patch.object(erp_dispatch.boto3, "client", MagicMock(return_value=client)):
        erp_dispatch._send_to_sqs(invoice_id, org_id, actor_id)

    send = client.send_message.call_args.kwargs
    assert send["QueueUrl"] == "http://q/erp.fifo"
    # FIFO ordering keyed on the invoice — relies on content-based dedup.
    assert send["MessageGroupId"] == str(invoice_id)
    body = json.loads(send["MessageBody"])
    assert body == {
        "invoice_id": str(invoice_id),
        "org_id": str(org_id),
        "actor_id": str(actor_id),
    }


async def test_dispatch_erp_inline_awaits_the_send_on_a_worker_loop(monkeypatch):
    """The extraction worker auto-sends an invoice it auto-approved (decisions
    §304) from its own short-lived loop, in-process or in Lambda, which closes
    the moment the job returns. A task started there would be abandoned
    mid-send, so the caller asks for the send to be awaited inline."""
    monkeypatch.setattr(settings, "erp_mode", "local")
    finished: list[tuple] = []

    async def _fake_run_local(invoice_id, org_id, actor_id):
        await asyncio.sleep(0)
        finished.append((invoice_id, org_id, actor_id))

    ids = (uuid.uuid4(), uuid.uuid4(), None)
    with patch.object(erp_dispatch, "_run_local", _fake_run_local):
        await erp_dispatch.dispatch_erp(*ids, inline=True)
        # Done by the time dispatch_erp returns: nothing left in flight.
        assert finished == [ids]
        assert erp_dispatch._dispatch_tasks == set()


def test_send_to_sqs_carries_a_null_actor(monkeypatch):
    """No person started an auto-send after a system auto-approval: the message
    says so with a JSON null, never the string "None" the Lambda cannot parse."""
    monkeypatch.setattr(settings, "sqs_erp_queue_url", "http://q/erp.fifo")
    client = MagicMock()
    with patch.object(erp_dispatch.boto3, "client", MagicMock(return_value=client)):
        erp_dispatch._send_to_sqs(uuid.uuid4(), uuid.uuid4(), None)

    body = json.loads(client.send_message.call_args.kwargs["MessageBody"])
    assert body["actor_id"] is None
