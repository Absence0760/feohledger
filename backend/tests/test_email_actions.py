"""Integration tests for email approval — the public confirm/perform endpoints
and the assigned-invoice email link injection.

Exercises the real ASGI app against a live Postgres (``realdb``):
  - GET renders a confirmation page and does NOT mutate (prefetch-safe);
  - POST approve → invoice approved + immutable audit row, single-use replay;
  - POST reject (with reason) → invoice rejected + exception row;
  - invalid / tampered / no-key tokens → friendly 400, no mutation;
  - segregation of duties + non-approver role are enforced (no weaker door);
  - wrong-status invoice is a no-op;
  - the invoice_assigned email carries a working Approve/Reject link.

Auth-gating is covered separately by test_rbac.py (both routes are public).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.config import settings
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.workflow import AuditLog
from app.services.email_action_token import (
    ACTION_APPROVE,
    ACTION_REJECT,
    build_action_token,
    digest_of_invoice,
    verify_action_token,
)

pytestmark = pytest.mark.asyncio

_KEY = "integration-email-action-key"


@pytest_asyncio.fixture
async def fresh_control_factory(realdb, monkeypatch):
    """Point the module-global ``control_session_factory`` (used by
    ``notify_event`` / ``_resolve_org_slug``) at an engine bound to THIS test's
    loop AND this slot's own control-plane DB — mirrors the same fixture in
    test_notification_dispatch.py. Must go through ``realdb.control_sessionmaker()``
    (not a bare ``create_async_engine(settings.database_url)``): the harness's
    orgs live in this process's per-slot control-plane database, not the real,
    shared one — see ``control_db_name_for_slot`` in conftest.py."""
    monkeypatch.setattr(
        "app.database.control_session_factory",
        realdb.control_sessionmaker(),
    )
    yield


@pytest.fixture
def signing_key(monkeypatch):
    monkeypatch.setattr(settings, "email_action_signing_key", _KEY)
    monkeypatch.setattr(settings, "email_action_ttl_hours", 168)
    return _KEY


#: The displayed-facts digest of every invoice `_make_invoice` wrote, so a
#: token minted here binds the version the test seeded — what the assignment
#: message would have shown (`email_action_token.digest_of_invoice`).
_SEEDED_FACTS: dict[uuid.UUID, str] = {}


def _facts(invoice_id: uuid.UUID) -> str:
    # An id no test seeded (a token for a missing invoice) gets a placeholder;
    # those tests fail before the facts are ever compared.
    return _SEEDED_FACTS.get(invoice_id, "0" * 32)


async def _make_invoice(
    realdb,
    *,
    status: InvoiceStatus = InvoiceStatus.ready_for_review,
    uploaded_by_id: uuid.UUID | None = None,
    amount: str = "100.00",
    number: str | None = None,
) -> uuid.UUID:
    info = realdb.info("a")
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = Invoice(
            invoice_number=number or f"EA-{uuid.uuid4().hex[:8]}",
            vendor_name="Test Vendor",
            amount=Decimal(amount),
            currency="USD",
            status=status,
            organization_id=info.org_id,
            uploaded_by_id=uploaded_by_id,
        )
        s.add(inv)
        await s.commit()
        _SEEDED_FACTS[inv.id] = digest_of_invoice(inv)
        return inv.id


def _token(realdb, invoice_id, *, action=ACTION_APPROVE, role="ap_manager", key=_KEY):
    info = realdb.info("a")
    return build_action_token(
        tenant_slug=info.slug,
        invoice_id=invoice_id,
        facts=_facts(invoice_id),
        actor_id=info.users[role],
        action=action,
        signing_key=key,
        ttl_hours=168,
    )


async def _status(realdb, invoice_id) -> str:
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        return (
            await s.execute(select(Invoice.status).where(Invoice.id == invoice_id))
        ).scalar_one()


# ---------------------------------------------------------------------------
# GET confirmation page — read-only
# ---------------------------------------------------------------------------


async def test_get_confirm_page_renders_and_does_not_mutate(realdb, signing_key):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _token(realdb, inv_id)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.get(f"/api/invoices/email-action/{token}")
    assert resp.status_code == 200
    assert "Confirm approve" in resp.text
    # Prefetch safety: a bare GET must not approve.
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_get_invalid_token_shows_invalid_page(realdb, signing_key):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/invoices/email-action/garbage.deadbeef")
    assert resp.status_code == 400
    assert "invalid or has expired" in resp.text


# ---------------------------------------------------------------------------
# POST approve
# ---------------------------------------------------------------------------


async def test_post_approve_happy_path_and_single_use(realdb, signing_key):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _token(realdb, inv_id)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(f"/api/invoices/email-action/{token}/confirm")
        assert resp.status_code == 200
        assert "approved" in resp.text.lower()
        assert await _status(realdb, inv_id) == InvoiceStatus.approved

        # Immutable audit row written by the normal review path.
        mk = realdb.sessionmaker("a")
        async with mk() as s:
            n = await s.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_id == inv_id, AuditLog.action == "invoice.approved")
            )
            assert n.scalar_one() == 1

        # Single-use: the same link cannot be replayed.
        replay = await c.post(f"/api/invoices/email-action/{token}/confirm")
        assert replay.status_code == 200
        assert "already been used" in replay.text.lower()


async def test_post_tampered_token_rejected(realdb, signing_key):
    inv_id = await _make_invoice(realdb)
    token = _token(realdb, inv_id)
    body, _, sig = token.rpartition(".")
    bad = f"{body}.{sig[:-1]}" + ("a" if sig[-1] != "a" else "b")
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(f"/api/invoices/email-action/{bad}/confirm")
    assert resp.status_code == 400
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_no_key_configured_rejects_token(realdb, monkeypatch):
    # Build a valid token, then disable the feature — every token must fail closed.
    monkeypatch.setattr(settings, "email_action_signing_key", _KEY)
    inv_id = await _make_invoice(realdb)
    token = _token(realdb, inv_id)
    monkeypatch.setattr(settings, "email_action_signing_key", "")
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get(f"/api/invoices/email-action/{token}")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST reject
# ---------------------------------------------------------------------------


async def test_post_reject_with_reason(realdb, signing_key):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _token(realdb, inv_id, action=ACTION_REJECT)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            f"/api/invoices/email-action/{token}/confirm",
            data={"reason": "Wrong amount"},
        )
    assert resp.status_code == 200
    assert "rejected" in resp.text.lower()
    assert await _status(realdb, inv_id) == InvoiceStatus.rejected

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        exc = (
            await s.execute(
                select(APException).where(
                    APException.invoice_id == inv_id,
                    APException.exception_type == "review_rejected",
                )
            )
        ).scalar_one()
        assert exc.description == "Wrong amount"


# ---------------------------------------------------------------------------
# One decision per email — the Approve and Reject links are a PAIR
#
# The Redis consume used to key on each token's own `jti`, so redeeming Reject
# burned only the Reject link. After the supplier resubmitted, the invoice was
# back in `ready_for_review` and the UNUSED Approve link from the same,
# superseded email still approved it — the reject→resubmit replay the consume
# was documented to close.
# ---------------------------------------------------------------------------


def _email_link_pair(realdb, invoice_id, *, role="ap_manager") -> tuple[str, str]:
    from urllib.parse import unquote

    from app.services.email_action_token import build_email_action_links

    info = realdb.info("a")
    text, _html = build_email_action_links(
        api_base_url="http://api.test",
        tenant_slug=info.slug,
        invoice_id=invoice_id,
        facts=_facts(invoice_id),
        actor_id=info.users[role],
        signing_key=_KEY,
        ttl_hours=168,
    )
    approve_line, reject_line = text.splitlines()
    return (
        unquote(approve_line.split("email-action/")[1]),
        unquote(reject_line.split("email-action/")[1]),
    )


async def test_rejecting_burns_the_sibling_approve_link(realdb, signing_key):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    approve, reject = _email_link_pair(realdb, inv_id)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            f"/api/invoices/email-action/{reject}/confirm", data={"reason": "Wrong PO"}
        )
        assert "rejected" in resp.text.lower()
        assert await _status(realdb, inv_id) == InvoiceStatus.rejected

        # The supplier corrects it and resubmits — back in review.
        mk = realdb.sessionmaker("a")
        async with mk() as s:
            inv = await s.get(Invoice, inv_id)
            inv.status = InvoiceStatus.ready_for_review
            await s.commit()

        replay = await c.post(f"/api/invoices/email-action/{approve}/confirm")
    assert "already been used" in replay.text.lower(), replay.text
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_a_refused_attempt_does_not_burn_the_pair(realdb, signing_key):
    """The release path frees the PAIR, so a reviewer refused from email (here
    by segregation) can still use the other link — the claim is only kept on a
    genuine decision."""
    reviewer = realdb.info("a").users["ap_manager"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=reviewer)
    approve, reject = _email_link_pair(realdb, inv_id)

    async with realdb.client(key="a", role=None) as c:
        refused = await c.post(f"/api/invoices/email-action/{approve}/confirm")
        assert "segregation" in refused.text.lower(), refused.text
        resp = await c.post(f"/api/invoices/email-action/{reject}/confirm")
    assert "rejected" in resp.text.lower(), resp.text
    assert await _status(realdb, inv_id) == InvoiceStatus.rejected


# ---------------------------------------------------------------------------
# Authorization parity — segregation + role gate
# ---------------------------------------------------------------------------


async def test_segregation_blocks_self_approval(realdb, signing_key):
    # Reviewer is the uploader → SoD must block, same as the in-app path.
    reviewer = realdb.info("a").users["ap_manager"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=reviewer)
    token = _token(realdb, inv_id, role="ap_manager")

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(f"/api/invoices/email-action/{token}/confirm")
    assert resp.status_code == 200
    assert "not allowed" in resp.text.lower() or "segregation" in resp.text.lower()
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_non_approver_role_rejected(realdb, signing_key):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _token(realdb, inv_id, role="ap_clerk")  # clerk can't approve

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(f"/api/invoices/email-action/{token}/confirm")
    assert resp.status_code == 200
    assert "not permitted" in resp.text.lower()
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_wrong_status_is_noop(realdb, signing_key):
    inv_id = await _make_invoice(realdb, status=InvoiceStatus.approved)
    token = _token(realdb, inv_id)
    async with realdb.client(key="a", role=None) as c:
        get = await c.get(f"/api/invoices/email-action/{token}")
        assert "no longer awaiting review" in get.text.lower()
        post = await c.post(f"/api/invoices/email-action/{token}/confirm")
        assert "no longer awaiting review" in post.text.lower()
    assert await _status(realdb, inv_id) == InvoiceStatus.approved


# ---------------------------------------------------------------------------
# Email link injection
# ---------------------------------------------------------------------------


async def test_assigned_email_includes_action_links(
    realdb, signing_key, fresh_control_factory, monkeypatch
):
    from app.models.notification import EVENT_INVOICE_ASSIGNED
    from app.services import notification_dispatch
    from app.services.notification_templates import InvoiceContext

    captured = []

    class _CapturingAdapter:
        async def send(self, message):
            captured.append(message)

    monkeypatch.setattr(
        "app.services.email_adapters.get_email_adapter", lambda: _CapturingAdapter()
    )

    info = realdb.info("a")
    reviewer = info.users["ap_manager"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=info.users["admin"])

    mk = realdb.sessionmaker("a")
    async with mk() as s:
        invoice = (await s.execute(select(Invoice).where(Invoice.id == inv_id))).scalar_one()
        await notification_dispatch.notify_event(
            s,
            correlation_id=uuid.uuid4(),
            organization_id=info.org_id,
            event_type=EVENT_INVOICE_ASSIGNED,
            entity_id=inv_id,
            recipient_user_ids=[reviewer],
            invoice_ctx=InvoiceContext(
                invoice_number="EA-NOTIFY",
                vendor_name="Test Vendor",
                amount=Decimal("100.00"),
                currency="USD",
            ),
            action_facts=digest_of_invoice(invoice),
        )
        # The email leg runs AFTER the caller's commit (services/post_commit),
        # so the caller has to actually commit — a dispatch whose transaction
        # never lands deliberately sends nothing.
        await s.commit()

    from app.services.post_commit import drain_post_commit

    await drain_post_commit()

    assert len(captured) == 1
    msg = captured[0]
    assert "/api/invoices/email-action/" in msg.body_text
    assert msg.body_html and "Approve" in msg.body_html
    # The embedded approve token must verify and bind to this reviewer + invoice.
    line = next(ln for ln in msg.body_text.splitlines() if "Approve:" in ln)
    token = line.split("email-action/")[1]
    decoded = verify_action_token(token, _KEY)
    assert decoded is not None
    assert decoded.invoice_id == inv_id
    assert decoded.actor_id == reviewer
    assert decoded.action == ACTION_APPROVE

    # End to end, with no hand-built digest anywhere: the link the
    # notification minted approves the invoice it announced, unchanged
    # (decisions §263). A drift between how the two sides digest the row
    # would refuse every real link, and this is what would catch it.
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(f"/api/invoices/email-action/{token}/confirm")
    assert resp.status_code == 200, resp.text
    assert "changed after this message was sent" not in resp.text
    assert await _status(realdb, inv_id) == InvoiceStatus.approved


# ---------------------------------------------------------------------------
# The gate is the granular permission, not a hardcoded role-name set
#
# The in-app decision route is `require_permission(PERM_INVOICE_APPROVE)`, so an
# org can mint a custom role that carries `invoice.approve`. This surface used to
# check `{"admin", "ap_manager", "cfo"}` by NAME instead — equivalent for the
# four system roles, but it dead-ended every approval email, Slack button and
# Teams card sent to such a reviewer on "not permitted" while they could approve
# perfectly well in the app. Both directions are pinned here.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def custom_role_reviewer(realdb):
    """Factory: a fresh user whose ONLY role is a custom one with `permissions`.

    Control-plane rows (users / roles / user_roles) persist across tests, so
    everything created here is torn down on exit.
    """
    from app.models.user import Role, User, UserRole

    mk = realdb.control_sessionmaker()
    created: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def _make(permissions: list[str]) -> uuid.UUID:
        org_id = realdb.info("a").org_id
        role_id, user_id = uuid.uuid4(), uuid.uuid4()
        async with mk() as s:
            s.add(
                Role(
                    id=role_id,
                    name=f"Email Approver {role_id.hex[:8]}",
                    organization_id=org_id,
                    permissions=permissions,
                )
            )
            s.add(
                User(
                    id=user_id,
                    email=f"custom-role-{user_id}@example.test",
                    full_name="Custom Role Reviewer",
                    hashed_password=None,
                    organization_id=org_id,
                    is_active=True,
                    must_change_password=False,
                )
            )
            await s.flush()
            s.add(UserRole(user_id=user_id, role_id=role_id))
            await s.commit()
        created.append((user_id, role_id))
        return user_id

    yield _make

    from sqlalchemy import delete

    async with mk() as s:
        for user_id, role_id in created:
            await s.execute(delete(UserRole).where(UserRole.user_id == user_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.execute(delete(Role).where(Role.id == role_id))
        await s.commit()


def _token_for(realdb, invoice_id, actor_id, *, action=ACTION_APPROVE):
    return build_action_token(
        tenant_slug=realdb.info("a").slug,
        invoice_id=invoice_id,
        facts=_facts(invoice_id),
        actor_id=actor_id,
        action=action,
        signing_key=_KEY,
        ttl_hours=168,
    )


async def test_custom_role_with_invoice_approve_may_decide_from_email(
    realdb, signing_key, custom_role_reviewer
):
    """A custom role granting `invoice.approve` works out-of-app, as in-app."""
    reviewer_id = await custom_role_reviewer(["invoice.approve"])
    inv_id = await _make_invoice(realdb, uploaded_by_id=realdb.info("a").users["admin"])
    token = _token_for(realdb, inv_id, reviewer_id)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(f"/api/invoices/email-action/{token}/confirm")
    assert resp.status_code == 200, resp.text
    assert "approved" in resp.text.lower(), resp.text
    assert await _status(realdb, inv_id) == InvoiceStatus.approved


async def test_custom_role_without_invoice_approve_is_still_refused(
    realdb, signing_key, custom_role_reviewer
):
    """The gate is not simply removed — a custom role granting nothing is out."""
    reviewer_id = await custom_role_reviewer([])
    inv_id = await _make_invoice(realdb, uploaded_by_id=realdb.info("a").users["admin"])
    token = _token_for(realdb, inv_id, reviewer_id)

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(f"/api/invoices/email-action/{token}/confirm")
    assert resp.status_code == 200
    assert "not permitted" in resp.text.lower()
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


# ---------------------------------------------------------------------------
# An Approve link approves only the figures its message showed (decisions §263)
# ---------------------------------------------------------------------------


async def _change_amount(realdb, invoice_id, amount: str) -> None:
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        inv = (await s.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
        inv.amount = Decimal(amount)
        await s.commit()


async def test_confirm_page_refuses_an_approve_link_whose_invoice_changed(realdb, signing_key):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _token(realdb, inv_id)
    await _change_amount(realdb, inv_id, "9500.00")

    async with realdb.client(key="a", role=None) as c:
        resp = await c.get(f"/api/invoices/email-action/{token}")
    assert "changed after this message was sent" in resp.text
    # Never asks the reviewer to confirm figures the email did not show.
    assert "Confirm approve" not in resp.text


async def test_approve_post_refuses_when_the_invoice_changed_after_the_email(realdb, signing_key):
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _token(realdb, inv_id)
    await _change_amount(realdb, inv_id, "9500.00")

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(f"/api/invoices/email-action/{token}/confirm")
    assert "changed after this message was sent" in resp.text
    assert await _status(realdb, inv_id) == InvoiceStatus.ready_for_review


async def test_reject_link_still_works_after_the_invoice_changed(realdb, signing_key):
    """Sending an invoice back is safe whatever it now says."""
    admin = realdb.info("a").users["admin"]
    inv_id = await _make_invoice(realdb, uploaded_by_id=admin)
    token = _token(realdb, inv_id, action=ACTION_REJECT)
    await _change_amount(realdb, inv_id, "9500.00")

    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(f"/api/invoices/email-action/{token}/confirm", data={"reason": "x"})
    assert resp.status_code == 200, resp.text
    assert await _status(realdb, inv_id) == InvoiceStatus.rejected
