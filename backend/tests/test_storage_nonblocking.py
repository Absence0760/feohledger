"""S3/MinIO I/O never runs on the event loop, and nothing bypasses the chokepoint.

`boto3` is a synchronous client, and every file surface in this app — invoice
upload, contract/expense/tax-form/chat attachments, the Positive Pay export, the
inbound email and PEPPOL webhooks, the extraction fetch, every download proxy —
is reached from an `async def`. A bare `put_object` / `get_object` there charges
a full S3 round trip (up to `MAX_FILE_SIZE` of body) to the event loop, and for
that whole window the worker serves no other request.

`services/storage` is the single chokepoint: `_put_object` / `_get_object` /
`_delete_object` / `delete_prefix` are the only places boto3 is touched, and
each hands the blocking call to `asyncio.to_thread`. These tests pin both
halves — the calls really do leave the loop thread, and no module under `app/`
reaches around them.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import threading
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.services import storage

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

# The boto3 operations that must only ever be issued from inside storage's
# thread-offloaded primitives. `list_objects_v2` / `delete_objects` are the
# bulk pair `delete_prefix` uses to sweep a deleted tenant's documents: a
# traversal is the LONGEST blocking call in the module (many round trips, not
# one), so it is the last thing that should run on the loop.
BLOCKING_S3_OPS = {
    "put_object",
    "get_object",
    "delete_object",
    "delete_objects",
    "list_objects_v2",
}

# `storage.py` owns the document bucket. The audit-shipping WORM adapter talks
# to its OWN Object-Lock bucket and already wraps every call in
# `asyncio.to_thread` itself, so it satisfies the same property by its own
# means — it is exempt from the chokepoint, not from the rule.
OFFLOADS_ITS_OWN = {
    APP_DIR / "services" / "storage.py",
    APP_DIR / "services" / "audit_shipping" / "s3_objectlock_adapter.py",
}


def _recording_client(calls: list[int | None]) -> MagicMock:
    """A boto3 stand-in that records the thread each operation runs on."""

    def _note(*_args, **_kwargs):
        calls.append(threading.current_thread().ident)
        body = MagicMock()
        body.read = MagicMock(return_value=b"BYTES")
        return {"Body": body, "ContentType": "application/pdf"}

    client = MagicMock()
    client.put_object = MagicMock(side_effect=_note)
    client.get_object = MagicMock(side_effect=_note)
    client.delete_object = MagicMock(side_effect=_note)
    client.delete_objects = MagicMock(side_effect=_note)
    client.head_bucket = MagicMock(side_effect=_note)

    paginator = MagicMock()
    paginator.paginate = MagicMock(
        side_effect=lambda **_kw: iter([{"Contents": [{"Key": "org/one.pdf"}]}])
    )
    client.get_paginator = MagicMock(return_value=paginator)
    return client


@pytest.mark.asyncio
async def test_put_get_delete_all_run_off_the_event_loop_thread():
    loop_thread = threading.current_thread().ident
    calls: list[int | None] = []
    key = f"{uuid.uuid4()}/invoices/{uuid.uuid4()}/x.pdf"

    with patch.object(storage, "_get_client", return_value=_recording_client(calls)):
        await storage._put_object(key, b"BYTES", "application/pdf")
        content, content_type = await storage._get_object(key)
        await storage._delete_object(key)
        await storage.delete_prefix("org/")

    assert content == b"BYTES"
    assert content_type == "application/pdf"
    assert calls, "no S3 operation was issued"
    assert all(tid != loop_thread for tid in calls), (
        "a boto3 call ran on the event loop thread — every request on the worker "
        "waits behind the S3 round trip"
    )


@pytest.mark.asyncio
async def test_public_upload_helper_offloads_too():
    """The `upload_*` helpers are the real call sites; they must inherit it."""
    loop_thread = threading.current_thread().ident
    calls: list[int | None] = []

    upload = MagicMock()
    upload.filename = "invoice.pdf"
    upload.content_type = "application/pdf"

    async def _read():
        return b"%PDF-1.4"

    upload.read = _read

    with patch.object(storage, "_get_client", return_value=_recording_client(calls)):
        file_key, file_url = await storage.upload_invoice_file(uuid.uuid4(), uuid.uuid4(), upload)

    assert file_key.endswith("invoice.pdf")
    assert file_url.startswith("/api/invoices/file/")
    assert calls and all(tid != loop_thread for tid in calls)


@pytest.mark.asyncio
async def test_get_file_and_delete_file_are_coroutines():
    """The public read/delete API is awaitable — a sync call site would be a
    silently-unawaited coroutine that never touches storage at all."""
    assert inspect.iscoroutinefunction(storage.get_file)
    assert inspect.iscoroutinefunction(storage.delete_file)


def test_no_module_issues_a_raw_boto3_s3_call_outside_the_chokepoint():
    """Nothing under `app/` reaches around `storage`'s thread offload.

    Scoped to the S3 *bucket* operations this app performs on invoice / document
    objects. The audit-shipping and SES adapters talk to different AWS services
    through their own already-offloaded clients, so they are not in
    `BLOCKING_S3_OPS`.
    """
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path in OFFLOADS_ITS_OWN:
            continue
        source = path.read_text()
        if not any(op in source for op in BLOCKING_S3_OPS):
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in BLOCKING_S3_OPS:
                offenders.append(
                    f"{path.relative_to(APP_DIR.parent)}:{node.lineno} .{func.attr}(...)"
                )

    assert offenders == [], (
        "S3 object I/O must go through services/storage's `_put_object` / "
        "`_get_object` / `_delete_object` (which offload to a thread); found: "
        + ", ".join(offenders)
    )
