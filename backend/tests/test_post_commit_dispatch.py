"""Outbound notification legs run AFTER the caller's transaction commits.

`workflow_engine.transition_invoice` used to `await` the whole notification
fan-out — one email per recipient, serially, then a Slack/Teams POST with a
10-second httpx timeout — *inside* the caller's still-open transaction.
`payment_erp_sync._sync_one_payment` holds `SELECT … FOR UPDATE` on the invoice
until after the transition returns, and `review.approve_invoice` holds it on the
`WorkflowInstance`, so a hung chat webhook held a row lock on a live invoice for
its full timeout and N recipients multiplied the email leg linearly.

The observable contract, which these tests pin:

  * nothing is sent before the caller commits;
  * everything is sent after it commits;
  * a transaction that ROLLS BACK sends nothing at all — we do not email people
    about a status change that never happened;
  * the in-app `Notification` rows still ride the caller's commit, because they
    are DB writes and should.

Plus a drift guard: the two dispatch modules must not regain a direct `await`
of a transport at module scope of the in-transaction path.
"""

from __future__ import annotations

import ast
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.invoice import Invoice, InvoiceStatus
from app.models.notification import EVENT_INVOICE_APPROVED, Notification
from app.services.post_commit import drain_post_commit, enqueue_post_commit
from app.services.workflow_engine import transition_invoice


@pytest_asyncio.fixture(autouse=True)
async def _fresh_control_factory(realdb, monkeypatch):
    """Same isolation the notification-dispatch tests use — see that file."""
    monkeypatch.setattr("app.database.control_session_factory", realdb.control_sessionmaker())
    yield


class _SpyAdapter:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def send(self, message) -> None:  # noqa: ANN001
        self._sink.append(message)


async def _add_invoice(mk, org_id, *, uploaded_by_id, status=InvoiceStatus.ready_for_review):
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
            vendor_name="Globex Corp",
            amount=Decimal("500.00"),
            currency="USD",
            status=status,
            uploaded_by_id=uploaded_by_id,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return inv.id


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------


async def test_job_runs_only_after_commit(realdb):
    ran: list[str] = []

    async def _job() -> None:
        ran.append("yes")

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        enqueue_post_commit(s, _job, name="unit")
        assert ran == []  # queued, not run
        await s.commit()
    await drain_post_commit()
    assert ran == ["yes"]


async def test_job_is_dropped_when_the_transaction_rolls_back(realdb):
    """The status change never happened, so nobody is told it did."""
    ran: list[str] = []

    async def _job() -> None:
        ran.append("yes")

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        enqueue_post_commit(s, _job, name="unit-rollback")
        await s.rollback()
    await drain_post_commit()
    assert ran == []


async def test_a_savepoint_rollback_drops_only_the_jobs_queued_inside_it(realdb):
    """SQLAlchemy fires `after_rollback` (and `after_commit`, on release) for a
    SAVEPOINT too. Bulk approve skips a
    refused row by rolling back its savepoint; that used to drain every queued
    job, so the rows approved before it committed with no email, no chat post
    and no ERP auto-send."""
    ran: list[str] = []

    def _job(label: str):
        async def _run() -> None:
            ran.append(label)

        return _run

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        enqueue_post_commit(s, _job("outer"), name="unit-outer")
        async with s.begin_nested():
            enqueue_post_commit(s, _job("kept-savepoint"), name="unit-kept")
        # Releasing a savepoint fires `after_commit` too, but nothing has
        # committed: the queue must not run inside the open outer transaction.
        await drain_post_commit()
        assert ran == []
        try:
            async with s.begin_nested():
                enqueue_post_commit(s, _job("rolled-back"), name="unit-rolled")
                async with s.begin_nested():
                    enqueue_post_commit(s, _job("rolled-back-child"), name="unit-child")
                raise RuntimeError("refused row")
        except RuntimeError:
            pass
        enqueue_post_commit(s, _job("after"), name="unit-after")
        await s.commit()
    await drain_post_commit()
    assert sorted(ran) == ["after", "kept-savepoint", "outer"]


async def test_a_failing_job_never_propagates(realdb):
    async def _boom() -> None:
        raise RuntimeError("smtp down")

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        enqueue_post_commit(s, _boom, name="unit-boom")
        await s.commit()  # must not raise
    await drain_post_commit()


# ---------------------------------------------------------------------------
# The real path
# ---------------------------------------------------------------------------


async def test_transition_sends_no_email_until_the_caller_commits(realdb, monkeypatch):
    sent: list = []
    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _SpyAdapter(sent))

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    uploader = realdb.info("a").users["ap_clerk"]
    inv_id = await _add_invoice(mk, org_id, uploaded_by_id=uploader)

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.approved, action_name="invoice.approved")
        # THE ASSERTION. The transition has run its whole notification hook and
        # the transaction — with its row locks — is still open. Not one byte has
        # gone to a third party.
        assert sent == []
        await s.commit()

    await drain_post_commit()
    assert [m.to for m in sent]  # ...and now it has.


async def test_in_app_rows_still_ride_the_callers_commit(realdb, monkeypatch):
    """Only the outbound legs moved. The DB writes did not."""
    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _SpyAdapter([]))

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    uploader = realdb.info("a").users["ap_clerk"]
    inv_id = await _add_invoice(mk, org_id, uploaded_by_id=uploader)

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.approved, action_name="invoice.approved")
        # Added to the caller's session, so it is visible in-transaction and
        # commits atomically with the status change.
        pending = (
            await s.execute(
                select(func.count())
                .select_from(Notification)
                .where(
                    Notification.recipient_user_id == uploader,
                    Notification.event_type == EVENT_INVOICE_APPROVED,
                    Notification.entity_id == inv_id,
                )
            )
        ).scalar_one()
        assert pending == 1
        await s.commit()
    await drain_post_commit()


async def test_rolled_back_transition_emails_nobody(realdb, monkeypatch):
    sent: list = []
    monkeypatch.setattr("app.services.email_adapters.get_email_adapter", lambda: _SpyAdapter(sent))

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    uploader = realdb.info("a").users["ap_clerk"]
    inv_id = await _add_invoice(mk, org_id, uploaded_by_id=uploader)

    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await transition_invoice(s, inv, InvoiceStatus.approved, action_name="invoice.approved")
        await s.rollback()

    await drain_post_commit()
    assert sent == []
    # And the invoice really did not move.
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        assert inv.status == InvoiceStatus.ready_for_review


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------

_APP = Path(__file__).resolve().parent.parent / "app"

# (module, function that must not send, transports it must not await directly)
_GUARDED = [
    ("services/notification_dispatch.py", "notify_event", {"_send_email_best_effort"}),
    (
        "services/vendor_notifications.py",
        "notify_vendor_of_invoice_event",
        {"_send_vendor_email_best_effort"},
    ),
]


def _direct_calls_in_body(fn: ast.AsyncFunctionDef) -> set[str]:
    """Names called in `fn`'s own body, NOT inside a nested function.

    The post-commit closure is a nested `async def`, so anything it calls is
    correctly excluded — that is exactly the distinction being guarded.
    """
    nested = {
        n
        for node in ast.walk(fn)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node is not fn
        for n in ast.walk(node)
    }
    names: set[str] = set()
    for node in ast.walk(fn):
        if node in nested or not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


@pytest.mark.parametrize(("rel_path", "fn_name", "transports"), _GUARDED)
def test_dispatch_does_not_send_inside_the_callers_transaction(rel_path, fn_name, transports):
    tree = ast.parse((_APP / rel_path).read_text())
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == fn_name
    )
    called = _direct_calls_in_body(fn)

    offenders = called & transports
    assert not offenders, (
        f"{rel_path}::{fn_name} awaits {sorted(offenders)} directly. That runs a "
        "third-party transport inside the caller's open transaction, holding its "
        "row locks. Move it into the post-commit closure."
    )
    assert "enqueue_post_commit" in called, (
        f"{rel_path}::{fn_name} no longer queues its outbound legs post-commit."
    )
