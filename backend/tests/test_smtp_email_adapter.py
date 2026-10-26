"""The smtp email adapter sends via the configured SMTP server (Mailpit, etc.).

Mocks smtplib so it's deterministic + CI-safe (no container). Covers: the
dispatcher selects + configures the adapter from settings; send() connects to
the configured host/port and sends a MIME message with text + HTML parts.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.services.email_adapters.base import EmailMessage


@pytest.fixture
def fake_smtp(monkeypatch):
    """Replace smtplib.SMTP with a context-manager mock; capture init + sends."""
    captured: dict = {"init": None, "sent": [], "noop": 0}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured["init"] = {"host": host, "port": port, "timeout": timeout}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            captured["starttls"] = True

        def login(self, user, pw):
            captured["login"] = (user, pw)

        def send_message(self, mime):
            captured["sent"].append(mime)

        def noop(self):
            captured["noop"] += 1

    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    return captured


def test_dispatcher_builds_smtp_adapter_from_settings(fake_smtp, monkeypatch):
    monkeypatch.setattr(settings, "email_provider", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "mailpit-host")
    monkeypatch.setattr(settings, "smtp_port", 1025)
    # Import after the package registers adapters.
    from app.services.email_adapters.dispatcher import get_email_adapter

    adapter = get_email_adapter()
    assert adapter.provider_name == "smtp"
    assert adapter.host == "mailpit-host"
    assert adapter.port == 1025


def test_send_connects_and_sends_mime(fake_smtp):
    from app.services.email_adapters.smtp_adapter import SmtpAdapter

    adapter = SmtpAdapter(
        {"smtp_host": "localhost", "smtp_port": 1025, "from_address": "no-reply@acme.test"}
    )
    msg = EmailMessage(
        to="dev@acme.test",
        subject="Welcome",
        body_text="hello",
        body_html="<p>hello</p>",
    )
    asyncio.run(adapter.send(msg))

    assert fake_smtp["init"] == {"host": "localhost", "port": 1025, "timeout": 15}
    assert len(fake_smtp["sent"]) == 1
    mime = fake_smtp["sent"][0]
    assert mime["To"] == "dev@acme.test"
    # White-label: the adapter brands the From display name. With no brand on the
    # message it uses the platform-default product name; the deliverable address
    # is unchanged. (Branded-message coverage lives in tests/test_branding.py.)
    assert mime["From"] == "FeohLedger <no-reply@acme.test>"
    assert mime["Subject"] == "Welcome"
    # text + html alternative both present
    assert mime.is_multipart()
    subtypes = {part.get_content_subtype() for part in mime.iter_parts()}
    assert {"plain", "html"} <= subtypes


def test_test_connection_true_when_reachable(fake_smtp):
    from app.services.email_adapters.smtp_adapter import SmtpAdapter

    adapter = SmtpAdapter({"smtp_host": "localhost", "smtp_port": 1025})
    assert asyncio.run(adapter.test_connection()) is True
    assert fake_smtp["noop"] == 1


@pytest.mark.parametrize(
    "name",
    ["Acme\r\nBcc: victim@evil.test", "Acme\nX-Injected: 1", "Acme Ltd", "Acme\x85Ltd"],
)
def test_a_line_break_in_an_interpolated_subject_is_folded(fake_smtp, name):
    # Senders put names nobody validated for control characters into subjects
    # (an organisation name in the sub-processor notice). EmailMessage folds
    # them onto one line, so the message neither injects a header nor is
    # refused by the email policy, which raises on a linefeed in a header.
    from app.services.email_adapters.smtp_adapter import SmtpAdapter

    adapter = SmtpAdapter({"smtp_host": "localhost", "smtp_port": 1025})
    message = EmailMessage(to="dev@acme.test", subject=f"Notice for {name}", body_text="x")
    asyncio.run(adapter.send(message))
    mime = fake_smtp["sent"][0]
    assert "\n" not in mime["Subject"] and "\r" not in mime["Subject"]
    assert mime["Bcc"] is None and mime["X-Injected"] is None
    assert mime["Subject"].startswith("Notice for Acme ")
