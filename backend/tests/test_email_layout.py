"""The shared HTML email layout — app/services/email_adapters/layout.py.

Every email goes out with an HTML part in one layout, and a message with no HTML
of its own gets a body derived from its plaintext (docs/decisions.md §280).
These tests pin the derivation rules, the escaping every block applies, that
only a URL the sender built ever becomes a link, the document shell, and that
the adapters really attach the HTML part — while the plaintext part stays
exactly what the send site wrote.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.config import settings
from app.services.branding import EMAIL_MARK_PATH, get_brand_context
from app.services.email_adapters import EmailMessage
from app.services.email_adapters.layout import (
    _readable_on,
    email_button,
    email_credentials,
    email_fallback_link,
    email_note,
    email_paragraph,
    render_email,
    text_to_html,
)
from app.services.email_adapters.smtp_adapter import SmtpAdapter

HOSTILE = '<script>alert(1)</script> & "Acme" <img src=x onerror=alert(1)>'
# A name a hostile signup or vendor record could carry: a phishing link.
PHISH = "your account is locked, sign in at https://evil.example/login"
APP_ORIGIN = "https://app.example.test"
RESET = "https://app.test/reset?token=abc"


def _platform():
    return get_brand_context(None)


def _derive(text: str, links: tuple[str, ...] = ()) -> str:
    return text_to_html(
        text, _platform(), cta_label="Open FeohLedger", links=links, fallback_intro="Paste:"
    )


def _hrefs(html: str) -> list[str]:
    return re.findall(r'href="([^"]*)"', html)


# ---------------------------------------------------------------------------
# Plaintext → HTML derivation
# ---------------------------------------------------------------------------


def test_blank_lines_split_paragraphs_and_single_newlines_break_lines():
    html = _derive("Hi Jared,\n\nFirst line\nsecond line")
    assert html.count("<p ") == 2
    assert "First line<br>second line" in html


def test_a_declared_url_on_its_own_line_becomes_the_button():
    html = _derive(f"Hi,\n\n  {RESET}\n\nThanks", links=(RESET,))
    assert f'href="{RESET.replace("&", "&amp;")}"' in html
    assert ">Open FeohLedger</a>" in html
    # The fallback repeats the raw link for a client that won't follow the button.
    assert html.rstrip().endswith("</a></p>")
    assert "Paste:" in html


def test_a_lead_in_ending_in_a_colon_labels_the_button():
    link = "https://app.test/chat"
    html = _derive(f"Hi,\n\nView the conversation and reply:\n  {link}", links=(link,))
    assert ">View the conversation and reply</a>" in html
    assert "Open FeohLedger" not in html


def test_an_undeclared_url_on_its_own_line_stays_text():
    html = _derive(f"Hi,\n\n  {RESET}\n\nThanks")
    assert _hrefs(html) == []
    assert RESET in html


def test_a_url_inside_prose_is_never_linked_even_when_declared():
    html = _derive("See https://app.test/x. Then reply.", links=("https://app.test/x",))
    assert _hrefs(html) == []


def test_a_hostile_name_carrying_a_url_never_becomes_a_link():
    # The sender's own link is declared; the URL inside the name is not.
    body = f"Hi {PHISH},\n\n{PHISH}\n\n  https://evil.example/login\n\n  {RESET}"
    html = _derive(body, links=(RESET,))
    assert _hrefs(html) == [RESET.replace("&", "&amp;")] * 2  # button + fallback
    assert "https://evil.example/login" in html  # still readable, as inert text


def test_indented_label_value_lines_become_the_credentials_panel():
    url = "https://acme.app.test"
    html = _derive(
        f"Hi,\n\n  URL:      {url}\n  Email:    a@b.test\n  Password: https://x.test/pw",
        links=(url,),
    )
    assert ">Password</td>" in html
    assert "monospace" in html
    # Only the declared URL is a link; a password that looks like a URL is text.
    assert _hrefs(html) == [url]
    assert ">https://x.test/pw</td>" in html


def test_long_blocks_stay_monospace():
    csv = "a,b\n1,2\n3,4\n5,6"
    html = _derive(f"Attached:\n\n{csv}")
    assert "<pre" in html
    assert "1,2\n3,4" in html


def test_derivation_escapes_everything():
    html = _derive(f"Hi {HOSTILE},\n\n{HOSTILE}\n\n  Name: {HOSTILE}")
    assert "<script" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html
    assert "&quot;Acme&quot;" in html


def test_text_blocks_do_not_link_urls():
    assert "<a" not in email_paragraph(PHISH)
    assert "<a" not in email_note(PHISH)
    assert "<a" not in email_credentials([("Password", "https://evil.example/x")])


def test_a_url_carrying_a_quote_cannot_break_out_of_the_href():
    html = email_button('https://app.test/x"onmouseover="alert(1)', "Go", _platform())
    assert '"onmouseover="' not in html


def test_blocks_escape_their_input():
    assert "<b>" not in email_paragraph("<b>x</b>")
    assert "<b>" not in email_credentials([("<b>k</b>", "<b>v</b>")])
    assert "<b>" not in email_button("https://x.test", "<b>go</b>", _platform())


# ---------------------------------------------------------------------------
# Only absolute http(s) URLs reach an href
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        " JavaScript:alert(1)",
        "data:text/html,<b>x</b>",
        "vbscript:x",
        "mailto:a@b.test",
        "/relative/path",
        "//evil.example/x",
        "https://",
        "",
    ],
)
def test_buttons_and_fallback_links_refuse_a_non_http_url(url):
    with pytest.raises(ValueError):
        email_button(url, "Go", _platform())
    with pytest.raises(ValueError):
        email_fallback_link("Paste:", url)


def test_a_declared_non_http_link_is_never_a_button():
    html = _derive("Hi,\n\n  javascript:alert(1)", links=("javascript:alert(1)",))
    assert _hrefs(html) == []


# ---------------------------------------------------------------------------
# Button contrast
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("accent", "expected"),
    [
        ("#000000", "#ffffff"),
        ("#1d4ed8", "#ffffff"),
        ("#ffffff", "#111827"),
        ("#fde047", "#111827"),
    ],
)
def test_button_text_is_whichever_of_black_or_white_reads_on_the_accent(accent, expected):
    assert _readable_on(accent) == expected


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def test_document_shell_carries_color_scheme_preheader_and_lang():
    doc = render_email(
        brand=_platform(),
        body_html="<p>x</p>",
        public_url=APP_ORIGIN,
        preheader="Preview line",
        lang="de",
    )
    assert doc.startswith("<!DOCTYPE html>")
    assert '<html lang="de">' in doc
    assert '<meta name="color-scheme" content="light dark">' in doc
    assert "prefers-color-scheme: dark" in doc
    assert "Preview line" in doc
    assert "max-width:600px" in doc
    assert f'<img src="{APP_ORIGIN}{EMAIL_MARK_PATH}"' in doc


def test_document_footer_carries_the_support_link():
    brand = get_brand_context({"brand": {"support_url": "https://help.acme.test"}})
    doc = render_email(brand=brand, body_html="", public_url=APP_ORIGIN)
    assert 'href="https://help.acme.test"' in doc


# ---------------------------------------------------------------------------
# The adapters attach it
# ---------------------------------------------------------------------------


def _smtp_parts(msg: EmailMessage, monkeypatch) -> tuple[str, str]:
    monkeypatch.setattr(settings, "public_url", APP_ORIGIN)
    mime = SmtpAdapter({"from_address": "no-reply@platform.test"})._build(msg)
    html = mime.get_body(preferencelist=("html",)).get_content()
    text = mime.get_body(preferencelist=("plain",)).get_content()
    return html, text


def test_a_text_only_message_still_goes_out_with_the_html_layout(monkeypatch):
    body = "Hi Jared,\n\nClick below:\n\n  https://app.test/reset\n\nThanks."
    msg = EmailMessage(
        to="a@b.test", subject="s", body_text=body, links=("https://app.test/reset",)
    )
    html, text = _smtp_parts(msg, monkeypatch)
    assert html.startswith("<!DOCTYPE html>")
    assert 'href="https://app.test/reset"' in html
    # The plaintext part is exactly what the send site wrote.
    assert text.rstrip("\n") == body


def test_a_derived_email_keeps_the_white_label_header(monkeypatch):
    brand = get_brand_context({"brand": {"product_name": "Acme Pay", "accent_color": "#112233"}})
    html, _ = _smtp_parts(
        EmailMessage(
            to="a@b.test",
            subject="s",
            body_text="Hello\n\n  https://x.test",
            brand=brand,
            links=("https://x.test",),
        ),
        monkeypatch,
    )
    assert "Acme Pay" in html
    assert "#112233" in html
    # The default button label names the tenant's product, not the platform's.
    assert ">Open Acme Pay</a>" in html
    assert "<img" not in html  # renamed without a logo → name only


def test_the_default_button_label_follows_the_message_locale(monkeypatch):
    html, _ = _smtp_parts(
        EmailMessage(
            to="a@b.test",
            subject="s",
            body_text="Hallo\n\n  https://x.test",
            locale="de",
            links=("https://x.test",),
        ),
        monkeypatch,
    )
    assert ">FeohLedger öffnen</a>" in html
    assert '<html lang="de">' in html


# ---------------------------------------------------------------------------
# The inbox preview
# ---------------------------------------------------------------------------


def _preheader(html: str) -> str:
    """The hidden preview text, or '' when the document sends none."""
    match = re.search(r'<div style="display:none;[^"]*">(.*?)(?:&#8204;|</div>)', html)
    return match.group(1) if match else ""


def test_no_preheader_is_derived_from_the_body(monkeypatch):
    html, _ = _smtp_parts(
        EmailMessage(to="a@b.test", subject="s", body_text="Hi,\n\nYour code is: 123456"),
        monkeypatch,
    )
    assert _preheader(html) == ""


class _Capture:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)


@pytest.mark.parametrize("module", ["app.api.auth", "app.api.portal_auth"])
async def test_the_sign_in_code_email_previews_without_the_code(module, monkeypatch):
    import importlib
    from types import SimpleNamespace

    mod = importlib.import_module(module)
    capture = _Capture()
    monkeypatch.setattr(mod, "get_email_adapter", lambda: capture)
    who = SimpleNamespace(email="a@b.test", full_name="Jared")
    sender = mod._send_email_otp if module == "app.api.auth" else mod._send_vendor_email_otp
    await sender(who, "482913")

    (msg,) = capture.sent
    html, _ = _smtp_parts(msg, monkeypatch)
    preview = _preheader(html)
    assert preview  # a fixed line, so the client never falls back to the body
    assert "482913" not in preview
    assert not re.search(r"\d{4,}", preview)
    assert "482913" in msg.body_text  # the code itself is still in the email


async def test_the_password_reset_email_previews_without_the_link(monkeypatch):
    from types import SimpleNamespace

    from app.api import auth

    capture = _Capture()
    monkeypatch.setattr(auth, "get_email_adapter", lambda: capture)
    await auth._send_password_reset_email(
        SimpleNamespace(email="a@b.test", full_name="Jared"), RESET
    )
    (msg,) = capture.sent
    html, _ = _smtp_parts(msg, monkeypatch)
    assert "token" not in _preheader(html)
    assert "http" not in _preheader(html)
    # The reset link is declared, so it is the button.
    assert f'href="{RESET.replace("&", "&amp;")}"' in html


def test_ses_sends_the_html_part_for_a_text_only_message(monkeypatch):
    import asyncio

    from app.services.email_adapters import ses_adapter

    sent: list[dict] = []

    class _Client:
        def send_email(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(ses_adapter.boto3, "client", lambda *a, **k: _Client())
    adapter = ses_adapter.SesAdapter({"from_address": "no-reply@platform.test"})
    asyncio.run(adapter.send(EmailMessage(to="a@b.test", subject="s", body_text="Plain body")))
    body = sent[0]["Message"]["Body"]
    assert body["Text"]["Data"] == "Plain body"
    assert body["Html"]["Data"].startswith("<!DOCTYPE html>")
    assert "Plain body" in body["Html"]["Data"]


# ---------------------------------------------------------------------------
# Send sites that bring their own HTML escape what they interpolate
# ---------------------------------------------------------------------------


def test_notification_email_html_escapes_the_vendor_name_and_reason():
    from app.models.notification import EVENT_INVOICE_REJECTED
    from app.services.notification_templates import InvoiceContext, render

    rendered = render(
        EVENT_INVOICE_REJECTED,
        InvoiceContext(
            invoice_number="INV-1",
            vendor_name=HOSTILE,
            amount=Decimal("10.00"),
            reason="<b>bad</b>",
        ),
    )
    assert "<script" not in rendered.body_html
    assert "<b>bad" not in rendered.body_html
    assert "&lt;script&gt;" in rendered.body_html
    # The plaintext (also the in-app body) is unchanged by the escaping.
    assert HOSTILE in rendered.body_text


def test_no_send_site_builds_email_html_from_a_bare_fstring():
    """A raw ``<p>{x}</p>`` body is how the unescaped vendor-name bodies got in.

    Bodies are built from the layout blocks, which escape; this fails on a send
    site that goes back to interpolating straight into markup.
    """
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        src = path.read_text()
        if re.search(r"""body_html\s*=\s*\(?\s*f["']<""", src) or re.search(
            r"""body_html=f["']<""", src
        ):
            offenders.append(str(path.relative_to(app_dir)))
    assert offenders == []
