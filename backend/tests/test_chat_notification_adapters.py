"""Coverage for the outbound chat-notification adapters (Slack / Teams / mock)
and their wiring into the notification chokepoint.

All pure / mocked — no DB, no network. httpx is stubbed with a fake client (the
same approach as `test_fx_adapters.py`); the network is never touched.

Asserts:
- `mock` is the default provider and the unknown-key fallback.
- Per-org config overrides the platform default; unknown keys fall back to mock.
- Slack vs Teams render to their distinct JSON body shapes.
- PII (bank/tax/address/payment-method) never reaches the rendered message.
- A chat-send failure is swallowed by `_send_chat_best_effort` (it never
  propagates — the caller's transition survives).

Every `_send_chat_best_effort` call below passes the invoice PK as the
`invoice_id=` KEYWORD. That is deliberate and load-bearing: the parameter used
to be called `entity_id`, which in this codebase otherwise means the
multi-entity subsidiary FK, so it read as a tenant-scoping bug on sight.
Renaming it back would `TypeError` here, which is the pin.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from app.config import settings
from app.services.chat_notification_adapters import (
    ChatMessage,
    get_chat_notification_adapter,
    list_available_providers,
    render_chat_message,
)
from app.services.chat_notification_adapters.base import CHAT_EVENT_TYPES
from app.services.chat_notification_adapters.mock_adapter import (
    SENT,
    MockChatNotificationAdapter,
)
from app.services.chat_notification_adapters.slack_adapter import SlackChatNotificationAdapter
from app.services.chat_notification_adapters.teams_adapter import TeamsChatNotificationAdapter
from app.services.notification_templates import InvoiceContext

# asyncio_mode="auto" (pyproject) runs async tests with no mark; a module-level
# pytest.mark.asyncio would wrongly tag the sync token/shape tests too.


# ---------- fake httpx client (no network) ----------


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("bad", request=None, response=None)


def _make_fake_client(captured: dict, *, status_code: int = 200):
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse(status_code)

    return _FakeClient


def _ctx() -> InvoiceContext:
    return InvoiceContext(
        invoice_number="INV-42",
        vendor_name="Globex Corp",
        amount=Decimal("1234.50"),
        currency="USD",
    )


# ---------- provider selection ----------


def test_mock_is_default_provider():
    """With no per-org override and the platform default, `get` returns mock."""
    assert settings.chat_notification_provider == "mock"
    adapter = get_chat_notification_adapter()
    assert isinstance(adapter, MockChatNotificationAdapter)
    assert adapter.provider_name == "mock"


def test_per_org_override_selects_slack_then_teams():
    slack = get_chat_notification_adapter({"provider": "slack", "webhook_url": "https://x"})
    teams = get_chat_notification_adapter({"provider": "teams", "webhook_url": "https://y"})
    assert isinstance(slack, SlackChatNotificationAdapter)
    assert isinstance(teams, TeamsChatNotificationAdapter)


def test_unknown_provider_falls_back_to_mock():
    adapter = get_chat_notification_adapter({"provider": "does-not-exist"})
    assert isinstance(adapter, MockChatNotificationAdapter)


def test_registry_lists_all_three():
    providers = list_available_providers()
    assert {"mock", "slack", "teams"} <= set(providers)


# ---------- render (pure, PII-free) ----------


def test_render_known_and_unknown_events():
    for event in CHAT_EVENT_TYPES:
        msg = render_chat_message(
            event, invoice_number="INV-1", vendor_name="Acme", amount=Decimal("5.00")
        )
        assert msg is not None
        assert msg.invoice_number == "INV-1"
    # chat_message / contract_renewal aren't chat-notifiable approval events.
    assert render_chat_message("chat_message", invoice_number="INV-1", vendor_name="Acme") is None


def test_amount_rendered_from_decimal_not_float():
    msg = render_chat_message(
        "invoice_paid",
        invoice_number="INV-9",
        vendor_name="Acme",
        amount=Decimal("1000.10"),
        currency="EUR",
    )
    assert msg.amount_str() == "EUR 1,000.10"
    # No float coercion artifacts.
    assert ".1000000" not in msg.amount_str()


def test_render_has_no_pii():
    """The message must carry only invoice number, vendor, amount, status, link
    — never bank / tax / address / payment-method data."""
    msg = render_chat_message(
        "invoice_approved",
        invoice_number="INV-7",
        vendor_name="Globex",
        amount=Decimal("42.00"),
        link="http://acme.localhost/invoices/abc",
    )
    parts = [msg.title, msg.invoice_number, msg.vendor_name, msg.status, msg.link or ""]
    blob = " ".join(parts).lower()
    for forbidden in ("iban", "routing", "account number", "tax id", "ssn", "ein", "card number"):
        assert forbidden not in blob


# ---------- body shaping: Slack vs Teams ----------


async def test_slack_body_shape():
    msg = render_chat_message(
        "invoice_approved",
        invoice_number="INV-42",
        vendor_name="Globex Corp",
        amount=Decimal("1234.50"),
        currency="USD",
        link="http://acme.localhost/invoices/abc",
    )
    adapter = SlackChatNotificationAdapter({"webhook_url": "https://hooks.slack.com/x"})
    body = adapter.build_body(msg)
    # Slack shape: top-level text fallback + blocks list.
    assert body["text"] == msg.title
    assert isinstance(body["blocks"], list)
    assert body["blocks"][0]["type"] == "section"
    section_text = body["blocks"][0]["text"]["text"]
    assert "Globex Corp" in section_text
    assert "USD 1,234.50" in section_text
    assert "<http://acme.localhost/invoices/abc|View invoice>" in section_text
    # Teams-only keys must be absent.
    assert "@type" not in body

    captured: dict = {}
    with patch(
        "app.services.chat_notification_adapters.slack_adapter.httpx.AsyncClient",
        _make_fake_client(captured),
    ):
        await adapter.send(msg)
    assert captured["url"] == "https://hooks.slack.com/x"
    assert captured["json"]["text"] == msg.title


async def test_teams_body_shape():
    msg = render_chat_message(
        "invoice_paid",
        invoice_number="INV-99",
        vendor_name="Initech",
        amount=Decimal("88.00"),
        currency="GBP",
        link="http://acme.localhost/invoices/xyz",
    )
    adapter = TeamsChatNotificationAdapter({"webhook_url": "https://outlook.office.com/webhook/x"})
    body = adapter.build_body(msg)
    # Teams shape: MessageCard, distinct from Slack's {text, blocks}.
    assert body["@type"] == "MessageCard"
    assert body["summary"] == msg.title
    facts = body["sections"][0]["facts"]
    names = {f["name"]: f["value"] for f in facts}
    assert names["Vendor"] == "Initech"
    assert names["Status"] == "paid"
    assert names["Amount"] == "GBP 88.00"
    assert body["potentialAction"][0]["@type"] == "OpenUri"
    assert body["potentialAction"][0]["targets"][0]["uri"] == "http://acme.localhost/invoices/xyz"
    # Slack-only keys must be absent.
    assert "blocks" not in body
    assert "text" not in body


# ---------- fail-closed when no webhook URL ----------


async def test_slack_fails_closed_without_webhook_url():
    """No webhook URL → no-op, no network, no raise (fail closed)."""
    adapter = SlackChatNotificationAdapter({})
    captured: dict = {}
    with patch(
        "app.services.chat_notification_adapters.slack_adapter.httpx.AsyncClient",
        _make_fake_client(captured),
    ):
        msg = render_chat_message("invoice_approved", invoice_number="I", vendor_name="V")
        await adapter.send(msg)
    assert captured == {}  # never attempted a post
    assert await adapter.test_connection() is False


async def test_teams_fails_closed_without_webhook_url():
    adapter = TeamsChatNotificationAdapter({})
    captured: dict = {}
    with patch(
        "app.services.chat_notification_adapters.teams_adapter.httpx.AsyncClient",
        _make_fake_client(captured),
    ):
        msg = render_chat_message("invoice_paid", invoice_number="I", vendor_name="V")
        await adapter.send(msg)
    assert captured == {}
    assert await adapter.test_connection() is False


# ---------- mock adapter records, no network ----------


async def test_mock_adapter_records_send():
    SENT.clear()
    adapter = MockChatNotificationAdapter({})
    msg = render_chat_message("invoice_rejected", invoice_number="INV-5", vendor_name="Acme")
    await adapter.send(msg)
    assert SENT[-1].invoice_number == "INV-5"
    assert await adapter.test_connection() is True


# ---------- chokepoint wiring: best-effort, never propagates ----------


async def test_send_chat_best_effort_swallows_failure():
    """A raising adapter inside `_send_chat_best_effort` must NOT propagate —
    that is what keeps a chat misconfig from breaking an invoice transition."""
    import uuid

    from app.services import notification_dispatch as nd

    class _Boom(MockChatNotificationAdapter):
        async def send(self, message):  # noqa: D401
            raise RuntimeError("slack 500")

    async def _fake_cfg(_org_id):
        return ({"enabled": True, "provider": "boom"}, "acme", {})

    with (
        patch.object(nd, "_resolve_org_chat_config", _fake_cfg),
        patch(
            "app.services.chat_notification_adapters.get_chat_notification_adapter",
            lambda cfg: _Boom({}),
        ),
    ):
        # Must return normally (no exception) despite the adapter raising.
        await nd._send_chat_best_effort(
            organization_id=uuid.uuid4(),
            event_type="invoice_approved",
            invoice_ctx=_ctx(),
            invoice_id=uuid.uuid4(),
        )


async def test_send_chat_failure_never_logs_the_webhook_url(caplog):
    """A failed chat send must not write the org's webhook URL into the log.

    This is the real failure mode, not a hypothetical: both real adapters end in
    `response.raise_for_status()`, and httpx's `HTTPStatusError` message embeds
    the request URL verbatim — which for a Slack/Teams incoming webhook IS the
    credential. `logger.exception` attaches the traceback (and so that message)
    regardless of the format string, so the first 4xx from a dead or rotated
    webhook used to leak it. The adapter below raises the exact exception
    `raise_for_status` builds, so this pins the mechanism rather than a
    stand-in.
    """
    import logging
    import uuid

    import httpx

    from app.services import notification_dispatch as nd

    url = "https://hooks.slack.com/services/T0AAAAAAA/B0BBBBBBB/zzTOPSECRETzz"

    class _Boom(MockChatNotificationAdapter):
        async def send(self, message):  # noqa: D401
            request = httpx.Request("POST", url)
            response = httpx.Response(404, request=request)
            response.raise_for_status()

    async def _fake_cfg(_org_id):
        return ({"enabled": True, "provider": "slack"}, "acme", {})

    caplog.set_level(logging.DEBUG)
    with (
        patch.object(nd, "_resolve_org_chat_config", _fake_cfg),
        patch(
            "app.services.chat_notification_adapters.get_chat_notification_adapter",
            lambda cfg: _Boom({}),
        ),
    ):
        await nd._send_chat_best_effort(
            organization_id=uuid.uuid4(),
            event_type="invoice_approved",
            invoice_ctx=_ctx(),
            invoice_id=uuid.uuid4(),
        )

    assert "zzTOPSECRETzz" not in caplog.text
    assert "hooks.slack.com" not in caplog.text
    # …but the failure is still visible enough to act on.
    assert "chat send failed" in caplog.text
    assert "HTTPStatusError" in caplog.text


async def test_send_email_failure_never_logs_the_recipient_address(caplog):
    """Same mechanism, same file: `SMTPRecipientsRefused` stringifies as
    `{'someone@customer.com': (550, b'…')}`, so `logger.exception` there put the
    recipient address in the log — the one thing that call site has always
    promised not to log."""
    import logging
    import smtplib

    from app.services import notification_dispatch as nd

    class _Boom:
        async def send(self, message):  # noqa: D401
            raise smtplib.SMTPRecipientsRefused({"cfo@customer.example": (550, b"nope")})

    caplog.set_level(logging.DEBUG)
    with patch(
        "app.services.email_adapters.get_email_adapter",
        lambda: _Boom(),
    ):
        await nd._send_email_best_effort(
            "cfo@customer.example",
            "subject",
            "body",
            None,
            event_type="invoice_approved",
        )

    assert "cfo@customer.example" not in caplog.text
    assert "email send failed" in caplog.text
    assert "SMTPRecipientsRefused" in caplog.text


async def test_send_chat_best_effort_noop_when_disabled():
    """Org hasn't enabled chat → no adapter is ever built."""
    import uuid

    from app.services import notification_dispatch as nd

    built = {"count": 0}

    async def _fake_cfg(_org_id):
        return ({"enabled": False}, "acme", {})

    def _spy(cfg):
        built["count"] += 1
        return MockChatNotificationAdapter({})

    with (
        patch.object(nd, "_resolve_org_chat_config", _fake_cfg),
        patch("app.services.chat_notification_adapters.get_chat_notification_adapter", _spy),
    ):
        await nd._send_chat_best_effort(
            organization_id=uuid.uuid4(),
            event_type="invoice_approved",
            invoice_ctx=_ctx(),
            invoice_id=uuid.uuid4(),
        )
    assert built["count"] == 0


async def test_send_chat_best_effort_per_event_toggle():
    """`events` map can suppress a single event while leaving chat enabled."""
    import uuid

    from app.services import notification_dispatch as nd

    SENT.clear()

    async def _fake_cfg(_org_id):
        return (
            {"enabled": True, "provider": "mock", "events": {"invoice_paid": False}},
            "acme",
            {},
        )

    with patch.object(nd, "_resolve_org_chat_config", _fake_cfg):
        # invoice_paid suppressed:
        await nd._send_chat_best_effort(
            organization_id=uuid.uuid4(),
            event_type="invoice_paid",
            invoice_ctx=_ctx(),
            invoice_id=uuid.uuid4(),
        )
        assert SENT == []
        # invoice_approved still on (default-on within an events map):
        await nd._send_chat_best_effort(
            organization_id=uuid.uuid4(),
            event_type="invoice_approved",
            invoice_ctx=_ctx(),
            invoice_id=uuid.uuid4(),
        )
        assert len(SENT) == 1
        assert SENT[0].event_type == "invoice_approved"


def test_chat_message_amount_str_absent():
    msg = ChatMessage(
        event_type="invoice_approved",
        title="t",
        invoice_number="I",
        vendor_name="V",
        status="approved",
    )
    assert msg.amount_str() == ""


def test_chat_message_states_a_code_less_amount_bare():
    """No currency: the figure is stated bare, never as dollars (decisions §466)."""
    from app.services.chat_notification_adapters.base import render_chat_message

    msg = render_chat_message(
        "invoice_approved", invoice_number="I", vendor_name="V", amount=Decimal("12.50")
    )
    assert msg is not None
    assert msg.currency is None
    assert msg.amount_str() == "12.50"
