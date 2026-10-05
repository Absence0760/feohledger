"""Tests for the email-intake processing core.

``test_email_intake.py`` covers tokens, address parsing, signature verify,
and the provider adapters. This file covers the two untested pieces with
real security weight:

  * ``resolve_tenant_from_recipient`` — the tenant-resolution chokepoint:
    only an *enabled* token whose value matches resolves; a disabled or
    unknown token resolves to None.
  * ``process_inbound_email`` orchestration — unknown recipient short-circuits
    with no tenant engine / S3 write; the happy path creates one invoice per
    usable attachment and dispatches extraction once per invoice with the
    ``SYSTEM_ACTOR_ID`` (email intake has no human actor); a redelivery
    carrying the same Message-ID is deduped before any tenant engine opens
    (only the first delivery ever creates an invoice), while two distinct
    Message-IDs are both processed.
  * ``_create_invoice_from_attachment`` — the seeded invoice is ``pending``,
    ``amount == Decimal('0')`` (exact, not float), no human uploader, and the
    S3 object is written under the org-prefixed key.

Note on the audit trail: an intake invoice is *created* (not transitioned),
and — consistent with the regular ``POST /api/invoices`` create path, which
also writes no audit row on create — intake writes none either. The module
docstring's "every audit trail ... points at SYSTEM_ACTOR_ID" refers to the
actor carried into the downstream extraction trail (asserted below via the
dispatch_extraction actor), not an audit row on create. So there is no
inconsistency to fix here.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.invoice import InvoiceStatus
from app.services import email_intake
from app.services.email_intake import (
    InboundAttachment,
    InboundEmail,
    _create_invoice_from_attachment,
)

# ---------------------------------------------------------------------------
# resolve_tenant_from_recipient
# ---------------------------------------------------------------------------


def _org(*, token: str, enabled: bool, slug: str = "acme") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        slug=slug,
        db_name=f"feoh_{slug}",
        settings={"email_intake": {"token": token, "enabled": enabled}},
    )


class _FakeOrgsCtrl:
    """Control-plane session whose execute() yields a fixed org list."""

    def __init__(self, orgs: list) -> None:
        self._orgs = orgs

    async def execute(self, *_a, **_k):
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=self._orgs)
        result = MagicMock()
        result.scalars = MagicMock(return_value=scalars)
        return result


async def test_resolve_tenant_matches_enabled_token():
    org_a = _org(token="aaa", enabled=True, slug="acme")
    org_b = _org(token="bbb", enabled=True, slug="beta")
    ctrl = _FakeOrgsCtrl([org_a, org_b])
    got = await email_intake.resolve_tenant_from_recipient(ctrl, "invoices+bbb@ap.co")
    assert got is org_b


async def test_resolve_tenant_disabled_token_returns_none():
    org = _org(token="aaa", enabled=False)
    ctrl = _FakeOrgsCtrl([org])
    assert await email_intake.resolve_tenant_from_recipient(ctrl, "invoices+aaa@ap.co") is None


async def test_resolve_tenant_unknown_token_returns_none():
    org = _org(token="aaa", enabled=True)
    ctrl = _FakeOrgsCtrl([org])
    assert await email_intake.resolve_tenant_from_recipient(ctrl, "invoices+zzz@ap.co") is None


async def test_resolve_tenant_handles_non_string_stored_token():
    """A malformed settings blob (token not a string) must not raise — the
    constant-time compare guards with an isinstance check before ever calling
    hmac.compare_digest (which requires matching str/bytes types)."""
    org = SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        db_name="feoh_acme",
        settings={"email_intake": {"token": None, "enabled": True}},
    )
    ctrl = _FakeOrgsCtrl([org])
    assert await email_intake.resolve_tenant_from_recipient(ctrl, "invoices+aaa@ap.co") is None


# ---------------------------------------------------------------------------
# process_inbound_email — orchestration
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        # Multi-entity Phase 2: the process loop resolves the tenant's default
        # entity id (one SELECT) before creating invoices. Hand back a value so
        # that lookup succeeds.
        _result = MagicMock()
        _result.scalar_one_or_none = MagicMock(return_value=uuid.uuid4())
        self.execute = AsyncMock(return_value=_result)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False


def _pdf(name="bill.pdf") -> InboundAttachment:
    return InboundAttachment(filename=name, content_type="application/pdf", content=b"%PDF-1.4")


async def test_process_unknown_recipient_returns_error_and_touches_nothing():
    create_engine = MagicMock()
    get_client = MagicMock()
    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[])),
        patch("sqlalchemy.ext.asyncio.create_async_engine", create_engine),
        patch("app.services.storage._get_client", get_client),
    ):
        result = await email_intake.process_inbound_email(
            MagicMock(), InboundEmail(to="invoices+nope@ap.co", sender="v@x.com")
        )
    assert result.error == "Unknown or disabled intake address"
    assert result.invoices_created == []
    create_engine.assert_not_called()
    get_client.assert_not_called()


async def test_unresolved_recipient_is_logged_without_the_intake_token(caplog):
    """The recipient address is the tenant BEARER CREDENTIAL (`invoices+<token>@`),
    and this branch is reached with a live, correct token every time an org
    toggles intake off — so logging the address wrote a working credential (and
    a third party's email address) into the application log."""
    import logging

    caplog.set_level(logging.WARNING, logger="app.services.email_intake")
    token = "S3cretIntakeTok"
    with patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[])):
        await email_intake.process_inbound_email(
            MagicMock(),
            InboundEmail(to=f"invoices+{token}@ap.example.com", sender="vendor@supplier.test"),
        )

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert token not in logged
    assert "vendor@supplier.test" not in logged
    # Still diagnosable: an address with no `+token` at all (bad MX / wrong
    # address) is distinguishable from a token nothing matched.
    assert "token_present=True" in logged


async def test_unresolved_recipient_without_a_token_reports_token_absent(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="app.services.email_intake")
    with patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[])):
        await email_intake.process_inbound_email(
            MagicMock(), InboundEmail(to="ap@ap.example.com", sender="v@x.com")
        )
    assert "token_present=False" in "\n".join(r.getMessage() for r in caplog.records)


async def test_process_no_usable_attachments_returns_error():
    org = _org(token="aaa", enabled=True)
    create_engine = MagicMock()
    bad = InboundAttachment(filename="memo.docx", content_type="application/msword", content=b"x")
    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org])),
        patch("sqlalchemy.ext.asyncio.create_async_engine", create_engine),
    ):
        result = await email_intake.process_inbound_email(
            MagicMock(),
            InboundEmail(to="invoices+aaa@ap.co", sender="v@x.com", attachments=[bad]),
        )
    assert result.error == "No usable PDF / image / XML attachments"
    assert "memo.docx" in result.skipped_attachments[0]
    create_engine.assert_not_called()


async def test_skipped_attachment_filename_is_sanitised():
    """A crafted path-traversal filename on a *skipped* attachment must be
    sanitised before it lands in result.skipped_attachments — that list is
    echoed back to the email provider (and may reach a log line / response
    body), so no path separators or '..' can survive. This exercises email
    intake's own _safe_filename call site in _usable_attachments."""
    org = _org(token="aaa", enabled=True)
    create_engine = MagicMock()
    bad = InboundAttachment(
        filename="../../etc/passwd",
        content_type="application/msword",  # disallowed → skipped
        content=b"x",
    )
    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org])),
        patch("sqlalchemy.ext.asyncio.create_async_engine", create_engine),
    ):
        result = await email_intake.process_inbound_email(
            MagicMock(),
            InboundEmail(to="invoices+aaa@ap.co", sender="v@x.com", attachments=[bad]),
        )

    assert result.error == "No usable PDF / image / XML attachments"
    assert len(result.skipped_attachments) == 1
    entry = result.skipped_attachments[0]
    assert "../" not in entry
    assert "/etc" not in entry
    assert "passwd" in entry  # the sanitised basename survives
    # No tenant engine touched — the skip path short-circuits before provisioning.
    create_engine.assert_not_called()


async def test_xml_attachment_is_accepted_not_skipped():
    """A structured e-invoice arrives as an .xml attachment — it must pass the
    content-type gate (application/xml) and create an invoice, not be skipped."""
    org = _org(token="aaa", enabled=True, slug="acme")
    session = _FakeSession()
    engine = MagicMock(dispose=AsyncMock())
    dispatch = AsyncMock()
    xml = InboundAttachment(
        filename="invoice.xml", content_type="application/xml", content=b"<Invoice/>"
    )
    inv_id = uuid.uuid4()
    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org])),
        patch.object(
            email_intake, "_create_invoice_from_attachment", AsyncMock(return_value=inv_id)
        ),
        patch(
            "app.database._make_tenant_url",
            MagicMock(return_value="postgresql+asyncpg://x/feoh_acme"),
        ),
        patch("sqlalchemy.ext.asyncio.create_async_engine", MagicMock(return_value=engine)),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", MagicMock(return_value=lambda: session)),
        patch("app.services.storage._get_client", MagicMock(return_value=MagicMock())),
        patch("app.services.storage._ensure_bucket", MagicMock()),
        patch("app.services.extraction_dispatch.dispatch_extraction", dispatch),
    ):
        result = await email_intake.process_inbound_email(
            MagicMock(),
            InboundEmail(to="invoices+aaa@ap.co", sender="v@x.com", attachments=[xml]),
        )
    assert result.error is None
    assert result.skipped_attachments == []
    assert result.invoices_created == [inv_id]
    dispatch.assert_awaited_once()


async def test_process_creates_invoice_per_attachment_and_dispatches_system_extraction():
    org = _org(token="aaa", enabled=True, slug="acme")
    ids = [uuid.uuid4(), uuid.uuid4()]
    session = _FakeSession()
    engine = MagicMock(dispose=AsyncMock())
    dispatch = AsyncMock()
    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org])),
        patch.object(email_intake, "_create_invoice_from_attachment", AsyncMock(side_effect=ids)),
        patch(
            "app.database._make_tenant_url",
            MagicMock(return_value="postgresql+asyncpg://x/feoh_acme"),
        ),
        patch("sqlalchemy.ext.asyncio.create_async_engine", MagicMock(return_value=engine)),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", MagicMock(return_value=lambda: session)),
        patch("app.services.storage._get_client", MagicMock(return_value=MagicMock())),
        patch("app.services.storage._ensure_bucket", MagicMock()),
        patch("app.services.extraction_dispatch.dispatch_extraction", dispatch),
    ):
        result = await email_intake.process_inbound_email(
            MagicMock(),
            InboundEmail(
                to="invoices+aaa@ap.co",
                sender="v@x.com",
                attachments=[_pdf("a.pdf"), _pdf("b.pdf")],
            ),
        )

    assert result.tenant_slug == "acme"
    assert result.invoices_created == ids
    session.commit.assert_awaited_once()
    engine.dispose.assert_awaited_once()
    # One extraction dispatch per invoice, each attributed to the system actor.
    assert dispatch.await_count == 2
    for call, inv_id in zip(dispatch.await_args_list, ids, strict=True):
        assert call.args == (inv_id, org.id, email_intake.SYSTEM_ACTOR_ID)


async def test_process_dedupes_identical_message_id_across_deliveries():
    """A provider retry (SES/Mailgun retry-on-timeout) or a duplicate
    delivery carrying the SAME Message-ID must create only ONE invoice, not
    two. The dedup guard runs right after tenant resolution and before any
    tenant engine is opened, so a replay never touches the tenant DB or S3."""
    org = _org(token="aaa", enabled=True, slug="acme")
    ids = [uuid.uuid4()]
    session = _FakeSession()
    engine = MagicMock(dispose=AsyncMock())
    dispatch = AsyncMock()
    create_engine = MagicMock(return_value=engine)
    email = InboundEmail(
        to="invoices+aaa@ap.co",
        sender="v@x.com",
        message_id="<same-delivery@vendor.example.com>",
        attachments=[_pdf("bill.pdf")],
    )

    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org])),
        patch.object(email_intake, "_create_invoice_from_attachment", AsyncMock(side_effect=ids)),
        patch(
            "app.database._make_tenant_url",
            MagicMock(return_value="postgresql+asyncpg://x/feoh_acme"),
        ),
        patch("sqlalchemy.ext.asyncio.create_async_engine", create_engine),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", MagicMock(return_value=lambda: session)),
        patch("app.services.storage._get_client", MagicMock(return_value=MagicMock())),
        patch("app.services.storage._ensure_bucket", MagicMock()),
        patch("app.services.extraction_dispatch.dispatch_extraction", dispatch),
    ):
        first = await email_intake.process_inbound_email(MagicMock(), email)
        second = await email_intake.process_inbound_email(MagicMock(), email)

    assert first.error is None
    assert first.invoices_created == ids
    assert second.error == "Duplicate delivery"
    assert second.invoices_created == []
    assert second.tenant_slug == "acme"  # tenant still resolved before the dedup check

    # Only the FIRST delivery ever opened a tenant engine / dispatched extraction.
    create_engine.assert_called_once()
    dispatch.assert_awaited_once()


async def test_process_does_not_dedupe_distinct_message_ids():
    """Two genuinely different emails (distinct Message-IDs) must both be
    processed — the dedup guard must not over-match."""
    org = _org(token="aaa", enabled=True, slug="acme")
    ids = [uuid.uuid4(), uuid.uuid4()]
    session = _FakeSession()
    engine = MagicMock(dispose=AsyncMock())
    dispatch = AsyncMock()
    create_engine = MagicMock(return_value=engine)

    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org])),
        patch.object(email_intake, "_create_invoice_from_attachment", AsyncMock(side_effect=ids)),
        patch(
            "app.database._make_tenant_url",
            MagicMock(return_value="postgresql+asyncpg://x/feoh_acme"),
        ),
        patch("sqlalchemy.ext.asyncio.create_async_engine", create_engine),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", MagicMock(return_value=lambda: session)),
        patch("app.services.storage._get_client", MagicMock(return_value=MagicMock())),
        patch("app.services.storage._ensure_bucket", MagicMock()),
        patch("app.services.extraction_dispatch.dispatch_extraction", dispatch),
    ):
        first = await email_intake.process_inbound_email(
            MagicMock(),
            InboundEmail(
                to="invoices+aaa@ap.co",
                sender="v@x.com",
                message_id="<first@vendor.example.com>",
                attachments=[_pdf("bill1.pdf")],
            ),
        )
        second = await email_intake.process_inbound_email(
            MagicMock(),
            InboundEmail(
                to="invoices+aaa@ap.co",
                sender="v@x.com",
                message_id="<second@vendor.example.com>",
                attachments=[_pdf("bill2.pdf")],
            ),
        )

    assert first.error is None
    assert second.error is None
    assert first.invoices_created == [ids[0]]
    assert second.invoices_created == [ids[1]]
    assert create_engine.call_count == 2
    assert dispatch.await_count == 2


@contextmanager
def _patched_tenant_io(create_invoice, dispatch):
    """The patches every orchestration test below needs, minus tenant resolution."""
    patches = (
        patch.object(email_intake, "_create_invoice_from_attachment", create_invoice),
        patch(
            "app.database._make_tenant_url",
            MagicMock(return_value="postgresql+asyncpg://x/feoh_any"),
        ),
        patch(
            "sqlalchemy.ext.asyncio.create_async_engine",
            MagicMock(side_effect=lambda *_a, **_k: MagicMock(dispose=AsyncMock())),
        ),
        patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker",
            MagicMock(side_effect=lambda *_a, **_k: _FakeSession),
        ),
        patch("app.services.extraction_dispatch.dispatch_extraction", dispatch),
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


async def test_one_message_id_reaching_two_tenants_is_delivered_to_both():
    """The dedup claim is per TENANT, not per Message-ID.

    ``Message-ID`` names the email, not a delivery. A vendor billing two
    customers that both run on this platform sends ONE message to both intake
    addresses, and Mailgun forwards it once per matched recipient carrying the
    same Message-ID. With a platform-global key the first tenant's delivery
    claimed it and the second tenant's invoice was silently dropped as a
    "Duplicate delivery" — a payable that customer never saw.
    """
    org_a = _org(token="aaa", enabled=True, slug="acme")
    org_b = _org(token="bbb", enabled=True, slug="beta")
    ids = [uuid.uuid4(), uuid.uuid4()]
    dispatch = AsyncMock()
    msg_id = "<one-email-two-customers@vendor.example.com>"

    def _email(to: str) -> InboundEmail:
        return InboundEmail(to=to, sender="v@x.com", message_id=msg_id, attachments=[_pdf()])

    resolve = AsyncMock(side_effect=[[org_a], [org_b]])
    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", resolve),
        _patched_tenant_io(AsyncMock(side_effect=ids), dispatch),
    ):
        to_a = await email_intake.process_inbound_email(MagicMock(), _email("invoices+aaa@ap.co"))
        to_b = await email_intake.process_inbound_email(MagicMock(), _email("invoices+bbb@ap.co"))

    assert to_a.error is None and to_a.invoices_created == [ids[0]]
    assert to_b.error is None, "the second tenant's copy was deduped against the first's"
    assert to_b.invoices_created == [ids[1]]
    assert [c.args[1] for c in dispatch.await_args_list] == [org_a.id, org_b.id]


async def test_a_redelivery_to_the_same_tenant_is_still_deduped_after_scoping():
    """Scoping the key by tenant must not weaken the dedup it exists for."""
    org = _org(token="aaa", enabled=True, slug="acme")
    dispatch = AsyncMock()
    email = InboundEmail(
        to="invoices+aaa@ap.co", sender="v@x.com", message_id="<m@v>", attachments=[_pdf()]
    )
    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org])),
        _patched_tenant_io(AsyncMock(side_effect=[uuid.uuid4()]), dispatch),
    ):
        await email_intake.process_inbound_email(MagicMock(), email)
        again = await email_intake.process_inbound_email(MagicMock(), email)
    assert again.error == "Duplicate delivery"
    dispatch.assert_awaited_once()


async def test_one_notification_naming_two_intake_addresses_reaches_both_tenants():
    """SES lists every recipient its receipt rule matched in ONE notification,
    so one payload can name two tenants' intake addresses. Each is that
    tenant's delivery — resolving only the first left the other with nothing.
    """
    org_a = _org(token="aaa", enabled=True, slug="acme")
    org_b = _org(token="bbb", enabled=True, slug="beta")
    ids = [uuid.uuid4(), uuid.uuid4()]
    dispatch = AsyncMock()
    with (
        patch.object(
            email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org_a, org_b])
        ),
        _patched_tenant_io(AsyncMock(side_effect=ids), dispatch),
    ):
        result = await email_intake.process_inbound_email(
            MagicMock(),
            InboundEmail(
                to="invoices+aaa@ap.co, invoices+bbb@ap.co",
                sender="v@x.com",
                message_id="<both@v>",
                attachments=[_pdf()],
            ),
        )
    assert result.error is None
    assert result.invoices_created == ids
    assert [c.args[1] for c in dispatch.await_args_list] == [org_a.id, org_b.id]


async def test_resolve_tenants_returns_every_enabled_recipient_once_in_order():
    org_a = _org(token="aaa", enabled=True, slug="acme")
    org_b = _org(token="bbb", enabled=True, slug="beta")
    org_off = _org(token="ccc", enabled=False, slug="off")
    ctrl = _FakeOrgsCtrl([org_a, org_b, org_off])
    got = await email_intake.resolve_tenants_from_recipient(
        ctrl, "invoices+bbb@ap.co, invoices+ccc@ap.co, invoices+aaa@ap.co, invoices+bbb@ap.co"
    )
    assert got == [org_b, org_a]


async def test_process_releases_dedup_claim_on_downstream_failure_so_retry_succeeds():
    """If invoice creation blows up AFTER the dedup claim is made (e.g. a
    transient S3/tenant-DB outage), the claim must be released so the
    provider's next redelivery of the SAME Message-ID can retry — otherwise
    the message would be wrongly treated as a duplicate and the invoice
    would never be created, for the full dedup TTL window."""
    org = _org(token="aaa", enabled=True, slug="acme")
    session = _FakeSession()
    engine = MagicMock(dispose=AsyncMock())
    dispatch = AsyncMock()
    email = InboundEmail(
        to="invoices+aaa@ap.co",
        sender="v@x.com",
        message_id="<retry-me@vendor.example.com>",
        attachments=[_pdf("bill.pdf")],
    )

    # First delivery: invoice creation blows up mid-flight.
    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org])),
        patch.object(
            email_intake,
            "_create_invoice_from_attachment",
            AsyncMock(side_effect=RuntimeError("s3 unreachable")),
        ),
        patch(
            "app.database._make_tenant_url",
            MagicMock(return_value="postgresql+asyncpg://x/feoh_acme"),
        ),
        patch("sqlalchemy.ext.asyncio.create_async_engine", MagicMock(return_value=engine)),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", MagicMock(return_value=lambda: session)),
        patch("app.services.storage._get_client", MagicMock(return_value=MagicMock())),
        patch("app.services.storage._ensure_bucket", MagicMock()),
        patch("app.services.extraction_dispatch.dispatch_extraction", dispatch),
        pytest.raises(RuntimeError, match="s3 unreachable"),
    ):
        await email_intake.process_inbound_email(MagicMock(), email)

    # Second delivery of the SAME message_id: creation now succeeds. If the
    # dedup claim from the failed first attempt weren't released, this
    # would be wrongly short-circuited as "Duplicate delivery".
    inv_id = uuid.uuid4()
    session2 = _FakeSession()
    engine2 = MagicMock(dispose=AsyncMock())
    dispatch2 = AsyncMock()
    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org])),
        patch.object(
            email_intake, "_create_invoice_from_attachment", AsyncMock(return_value=inv_id)
        ),
        patch(
            "app.database._make_tenant_url",
            MagicMock(return_value="postgresql+asyncpg://x/feoh_acme"),
        ),
        patch("sqlalchemy.ext.asyncio.create_async_engine", MagicMock(return_value=engine2)),
        patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker", MagicMock(return_value=lambda: session2)
        ),
        patch("app.services.storage._get_client", MagicMock(return_value=MagicMock())),
        patch("app.services.storage._ensure_bucket", MagicMock()),
        patch("app.services.extraction_dispatch.dispatch_extraction", dispatch2),
    ):
        result = await email_intake.process_inbound_email(MagicMock(), email)

    assert result.error is None
    assert result.invoices_created == [inv_id]
    dispatch2.assert_awaited_once()


async def test_post_commit_failure_keeps_the_dedup_claim_so_a_retry_cannot_duplicate():
    """The mirror of the test above, and the line between them is the COMMIT.

    Releasing the claim is correct only while nothing it guards is durable.
    Once the tenant transaction has committed the invoices exist, so a failure
    after that point — `dispatch_extraction`, which in `lambda` mode is a real
    boto3 SQS round trip — must NOT release: the provider's redelivery would
    then sail past the dedup check and create a SECOND payable per attachment
    for the same email.

    `api/payments.py`'s webhook already draws this line the right way (its
    post-commit ERP sync sits outside the try that releases); this pins it for
    email intake.
    """
    org = _org(token="aaa", enabled=True, slug="acme")
    session = _FakeSession()
    engine = MagicMock(dispose=AsyncMock())
    inv_id = uuid.uuid4()
    released: list[tuple[str, str]] = []

    async def _record_release(provider, event_id):
        released.append((provider, event_id))

    async def _dispatch_boom(*_a, **_k):
        raise RuntimeError("SQS unavailable")

    email = InboundEmail(
        to="invoices+aaa@ap.co",
        sender="v@x.com",
        message_id="<already-committed@vendor.example.com>",
        attachments=[_pdf("bill.pdf")],
    )

    with (
        patch.object(email_intake, "resolve_tenants_from_recipient", AsyncMock(return_value=[org])),
        patch.object(
            email_intake, "_create_invoice_from_attachment", AsyncMock(return_value=inv_id)
        ),
        patch.object(email_intake, "is_event_already_processed", AsyncMock(return_value=False)),
        patch.object(email_intake, "release_event_claim", _record_release),
        patch(
            "app.database._make_tenant_url",
            MagicMock(return_value="postgresql+asyncpg://x/feoh_acme"),
        ),
        patch("sqlalchemy.ext.asyncio.create_async_engine", MagicMock(return_value=engine)),
        patch("sqlalchemy.ext.asyncio.async_sessionmaker", MagicMock(return_value=lambda: session)),
        patch("app.services.storage._get_client", MagicMock(return_value=MagicMock())),
        patch("app.services.storage._ensure_bucket", MagicMock()),
        patch("app.services.extraction_dispatch.dispatch_extraction", _dispatch_boom),
    ):
        result = await email_intake.process_inbound_email(MagicMock(), email)

    # The invoices committed, so the message WAS processed…
    session.commit.assert_awaited_once()
    assert result.invoices_created == [inv_id]
    # …the dispatch failure is swallowed (the route must not ask for a
    # redelivery of work that already landed)…
    assert result.error is None
    # …and above all the claim still stands.
    assert released == [], (
        "the dedup claim was released after the tenant commit — the provider's "
        "redelivery would create a duplicate invoice per attachment"
    )


# ---------------------------------------------------------------------------
# _create_invoice_from_attachment
# ---------------------------------------------------------------------------


class _FakeTenantDB:
    """Tenant session that assigns a real id on flush (mirrors the PK default)."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()


async def test_create_invoice_is_pending_zero_decimal_and_org_prefixed_key():
    db = _FakeTenantDB()
    s3 = MagicMock()
    org_id = uuid.uuid4()
    att = _pdf("bill.pdf")

    entity_id = uuid.uuid4()
    # The upload goes through `storage._put_object` (which offloads boto3 to a
    # worker thread), so the client is patched at its owner, not passed in.
    with (
        patch("app.services.storage._get_client", MagicMock(return_value=s3)),
        patch("app.services.workflow_engine.create_workflow_instance", AsyncMock()),
    ):
        invoice_id = await _create_invoice_from_attachment(
            tenant_db=db,
            org_id=org_id,
            entity_id=entity_id,
            sender="vendor@x.com",
            subject="March invoice",
            attachment=att,
        )

    invoice = db.added[0]
    assert invoice.status == InvoiceStatus.pending
    assert invoice.amount == Decimal("0")
    assert isinstance(invoice.amount, Decimal)
    assert invoice.currency == "USD"
    assert invoice.uploaded_by_id is None
    assert invoice.organization_id == org_id
    assert invoice.entity_id == entity_id  # lands under the resolved entity

    expected_key = f"{org_id}/{invoice_id}/bill.pdf"
    assert invoice.file_key == expected_key
    put = s3.put_object.call_args.kwargs
    assert put["Key"] == expected_key
    assert put["ContentType"] == "application/pdf"
    assert put["Body"] == b"%PDF-1.4"


async def test_create_invoice_freezes_a_workflow_instance_at_ingest():
    """Email intake is a first-class ingress, so it owes the invoice the same
    frozen workflow snapshot every other ingress creates.

    Without it the invoice has no `WorkflowInstance` at all: a later edit to
    the tenant's workflow definition retroactively governs it (breaking the
    per-invoice frozen-snapshot invariant for exactly the unattended paths), it
    has no `WorkflowStep` rows — so it is invisible to the step-based approval
    queue and to `GET /api/invoices/{id}/workflow` — and it is never assigned
    an A/B experiment variant.
    """
    db = _FakeTenantDB()
    create_instance = AsyncMock()

    with (
        patch("app.services.storage._get_client", MagicMock(return_value=MagicMock())),
        patch("app.services.workflow_engine.create_workflow_instance", create_instance),
    ):
        await _create_invoice_from_attachment(
            tenant_db=db,
            org_id=uuid.uuid4(),
            entity_id=uuid.uuid4(),
            sender="vendor@x.com",
            subject="March invoice",
            attachment=_pdf("bill.pdf"),
        )

    create_instance.assert_awaited_once()
    args = create_instance.await_args.args
    assert args[0] is db
    assert args[1] is db.added[0]
    # Created BEFORE the S3 upload returns an id-bearing row is not enough —
    # the instance must be built from an invoice that already has its PK, so
    # `WorkflowInstance.invoice_id` is real.
    assert args[1].id is not None
