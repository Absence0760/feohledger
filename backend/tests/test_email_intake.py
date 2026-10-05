"""Unit tests for email-to-invoice intake.

DB-free where possible; the full process_inbound_email path touches a
tenant engine so those cases are covered by the smoke stack. Here we
test: token generation + address rendering, token extraction, HMAC
verification, and the provider adapters' parsing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Token + address helpers
# ---------------------------------------------------------------------------


def test_generate_intake_token_is_url_safe():
    from app.services.email_intake import generate_intake_token

    token = generate_intake_token(length=16)
    assert len(token) == 16
    # token_urlsafe alphabet: letters, digits, underscore, hyphen
    assert all(c.isalnum() or c in "_-" for c in token)


def test_provision_intake_token_sets_enabled_flag_and_token():
    from app.services.email_intake import provision_intake_token

    org = SimpleNamespace(settings={})
    token = provision_intake_token(org)

    assert org.settings["email_intake"]["token"] == token
    assert org.settings["email_intake"]["enabled"] is True
    assert "rotated_at" in org.settings["email_intake"]


def test_provision_intake_token_preserves_other_settings():
    from app.services.email_intake import provision_intake_token

    org = SimpleNamespace(settings={"company": {"name": "Acme"}})
    provision_intake_token(org)

    assert org.settings["company"] == {"name": "Acme"}


def test_intake_address_returns_none_when_domain_unset():
    from app.services import email_intake

    org = SimpleNamespace(settings={"email_intake": {"token": "abc", "enabled": True}})
    with patch.object(email_intake.settings, "email_intake_domain", ""):
        assert email_intake.intake_address_for(org) is None


def test_intake_address_returns_none_when_token_missing():
    from app.services import email_intake

    org = SimpleNamespace(settings={})
    with patch.object(email_intake.settings, "email_intake_domain", "ap.example.com"):
        assert email_intake.intake_address_for(org) is None


def test_intake_address_renders_correctly():
    from app.services import email_intake

    org = SimpleNamespace(settings={"email_intake": {"token": "abc123", "enabled": True}})
    with patch.object(email_intake.settings, "email_intake_domain", "ap.example.com"):
        assert email_intake.intake_address_for(org) == "invoices+abc123@ap.example.com"


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "to,expected",
    [
        ("invoices+abc123@ap.example.com", "abc123"),
        ("INVOICES+abc_DEF-123@ap.example.com", "abc_DEF-123"),
        ('"AP Intake" <invoices+xyz@ap.example.com>', "xyz"),
        ("invoices@ap.example.com", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_token(to, expected):
    from app.services.email_intake import extract_token

    assert extract_token(to) == expected


# ---------------------------------------------------------------------------
# HMAC verification
# ---------------------------------------------------------------------------


def test_verify_signature_fails_closed_when_secret_unset_in_non_debug():
    """Production deploys that forget to set FEOH_EMAIL_INTAKE_SIGNING_SECRET
    must reject every webhook, not accept everything."""
    from app.services import email_intake

    with (
        patch.object(email_intake.settings, "email_intake_signing_secret", ""),
        patch.object(email_intake.settings, "debug", False),
    ):
        assert email_intake.verify_signature(b"anything", None) is False
        assert email_intake.verify_signature(b"anything", "wrong") is False


def test_verify_signature_skips_when_secret_unset_in_debug():
    """Dev convenience: FEOH_DEBUG=true allows running locally with no secret."""
    from app.services import email_intake

    with (
        patch.object(email_intake.settings, "email_intake_signing_secret", ""),
        patch.object(email_intake.settings, "debug", True),
    ):
        assert email_intake.verify_signature(b"anything", None) is True
        assert email_intake.verify_signature(b"anything", "wrong") is True


def test_verify_signature_rejects_missing_header_with_secret():
    from app.services import email_intake

    with patch.object(email_intake.settings, "email_intake_signing_secret", "super-secret"):
        assert email_intake.verify_signature(b"body", None) is False


async def test_inbound_webhook_returns_204_on_bad_signature():
    """Rejection paths must return 204 silently so the response can't
    enumerate which providers / signing secrets / payload shapes the
    tenant accepts. Distinct 401/400 codes leaked that information."""
    from unittest.mock import AsyncMock

    from app.api.email_intake import inbound_webhook

    request = SimpleNamespace(
        headers={"X-Signature": "wrong"},
        body=AsyncMock(return_value=b"{}"),
    )

    with (
        patch("app.api.email_intake.get_parser", return_value=lambda b, h: {"x": 1}),
        patch("app.api.email_intake.verify_signature", return_value=False),
    ):
        response = await inbound_webhook(provider="ses", request=request, ctrl_db=None)
    assert response.status_code == 204


async def test_inbound_webhook_returns_204_on_unknown_provider():
    from app.api.email_intake import inbound_webhook

    request = SimpleNamespace(headers={}, body=lambda: b"")

    with patch("app.api.email_intake.get_parser", return_value=None):
        response = await inbound_webhook(provider="bogus", request=request, ctrl_db=None)
    assert response.status_code == 204


async def test_inbound_webhook_returns_204_on_parse_error():
    """A signature-valid payload that the provider parser can't read
    must still rejected silently — distinct 400 would let an attacker
    grind the parser's accepted shapes."""
    from unittest.mock import AsyncMock

    from app.api.email_intake import inbound_webhook

    request = SimpleNamespace(
        headers={"X-Signature": "sig"},
        body=AsyncMock(return_value=b"{}"),
    )

    with (
        patch("app.api.email_intake.get_parser", return_value=lambda b, h: None),
        patch("app.api.email_intake.verify_signature", return_value=True),
    ):
        response = await inbound_webhook(provider="ses", request=request, ctrl_db=None)
    assert response.status_code == 204


async def test_inbound_webhook_returns_uniform_ack_regardless_of_tenant_resolution():
    """Once the signature verifies, the response body/status must be
    IDENTICAL whether the recipient token resolved to a real tenant (and
    created invoices) or not — otherwise anyone holding the platform-wide
    signing secret could grind per-tenant intake tokens by watching for
    ``tenant_slug`` to populate in the response."""
    import json as json_mod
    from unittest.mock import AsyncMock

    from app.api.email_intake import inbound_webhook
    from app.services.email_intake import IntakeResult

    def _request():
        return SimpleNamespace(
            headers={"X-Signature": "sig"},
            body=AsyncMock(return_value=b"{}"),
        )

    hit = IntakeResult(tenant_slug="acme", invoices_created=[uuid.uuid4()])
    miss = IntakeResult(tenant_slug=None, error="Unknown or disabled intake address")

    with (
        patch("app.api.email_intake.get_parser", return_value=lambda b, h: {"x": 1}),
        patch("app.api.email_intake.verify_signature", return_value=True),
        patch("app.api.email_intake.process_inbound_email", AsyncMock(return_value=hit)),
    ):
        resp_hit = await inbound_webhook(provider="ses", request=_request(), ctrl_db=None)

    with (
        patch("app.api.email_intake.get_parser", return_value=lambda b, h: {"x": 1}),
        patch("app.api.email_intake.verify_signature", return_value=True),
        patch("app.api.email_intake.process_inbound_email", AsyncMock(return_value=miss)),
    ):
        resp_miss = await inbound_webhook(provider="ses", request=_request(), ctrl_db=None)

    assert resp_hit.status_code == 200
    assert resp_miss.status_code == 200
    assert resp_hit.status_code == resp_miss.status_code
    assert json_mod.loads(resp_hit.body) == json_mod.loads(resp_miss.body)


async def test_inbound_webhook_asks_for_redelivery_on_our_own_failure():
    """OUR failure is not a decision — ask the provider to redeliver.

    `process_inbound_email` releases its Redis dedup claim and re-raises on any
    downstream failure (S3 / tenant DB / Redis) precisely so "the NEXT delivery
    of the same message_id actually retries the work". Acking 200 told
    SES / Mailgun the message WAS delivered, so there was no next delivery: the
    vendor's invoice was lost with only a log line behind it, and the
    release-on-failure code was preparing for a retry that could never come.

    A 5xx here narrows the token-enumeration oracle the uniform ack exists to
    prevent down to "while the platform is already broken" — far smaller than
    losing invoices on every blip. The response is BODYLESS so it still carries
    no detail, no stack trace, no tenant.
    """
    from unittest.mock import AsyncMock

    from app.api.email_intake import inbound_webhook

    request = SimpleNamespace(
        headers={"X-Signature": "sig"},
        body=AsyncMock(return_value=b"{}"),
    )

    with (
        patch("app.api.email_intake.get_parser", return_value=lambda b, h: {"x": 1}),
        patch("app.api.email_intake.verify_signature", return_value=True),
        patch(
            "app.api.email_intake.process_inbound_email",
            AsyncMock(side_effect=RuntimeError("s3 unreachable")),
        ),
    ):
        response = await inbound_webhook(provider="ses", request=request, ctrl_db=None)

    assert response.status_code == 503
    assert response.body == b""


async def test_inbound_webhook_decisions_all_share_the_same_200_ack():
    """The oracle guard: every DECISION after the signature verifies is one
    identical 200, so the 503 above can only ever mean "we broke", never "that
    token was real". Duplicate delivery and no-usable-attachment are the two
    decisions the uniform-ack test above doesn't already cover."""
    import json as json_mod
    from unittest.mock import AsyncMock

    from app.api.email_intake import inbound_webhook
    from app.services.email_intake import IntakeResult

    def _request():
        return SimpleNamespace(
            headers={"X-Signature": "sig"},
            body=AsyncMock(return_value=b"{}"),
        )

    outcomes = [
        IntakeResult(tenant_slug="acme", error="Duplicate delivery"),
        IntakeResult(
            tenant_slug="acme",
            error="No usable PDF / image / XML attachments",
            skipped_attachments=["notes.docx"],
        ),
        IntakeResult(tenant_slug="acme", invoices_created=[uuid.uuid4()]),
    ]

    responses = []
    for outcome in outcomes:
        with (
            patch("app.api.email_intake.get_parser", return_value=lambda b, h: {"x": 1}),
            patch("app.api.email_intake.verify_signature", return_value=True),
            patch("app.api.email_intake.process_inbound_email", AsyncMock(return_value=outcome)),
        ):
            responses.append(
                await inbound_webhook(provider="ses", request=_request(), ctrl_db=None)
            )

    assert {r.status_code for r in responses} == {200}
    bodies = {json_mod.dumps(json_mod.loads(r.body), sort_keys=True) for r in responses}
    assert len(bodies) == 1, f"a decision leaked into the ack body: {bodies}"


# ---------------------------------------------------------------------------
# Body size cap (memory-exhaustion DoS guard, GitHub issue #142)
#
# `inbound_webhook` used to `await request.body()` before any signature
# check, with no size cap — an unauthenticated attacker could POST a
# multi-gigabyte body and have it buffered fully into memory before the HMAC
# check ever ran. The guard bounds the body in two phases, mirroring
# `peppol_inbound_webhook`: reject on a declared Content-Length over the cap
# BEFORE reading the body at all, then re-check the actual read length in
# case the header lied or was absent (e.g. chunked transfer). Placed in the
# pre-signature block, so rejections return the plain 204 (not the opaque
# 200 ack reserved for post-signature outcomes).
# ---------------------------------------------------------------------------


async def test_inbound_webhook_content_length_over_cap_rejects_before_body_read():
    """A declared Content-Length over the cap must reject WITHOUT ever
    awaiting `request.body()`."""
    from unittest.mock import AsyncMock

    from app.api.email_intake import inbound_webhook
    from app.config import settings

    request = SimpleNamespace(
        headers={"content-length": "999999"},
        body=AsyncMock(return_value=b"{}"),
    )

    with (
        patch("app.api.email_intake.get_parser", return_value=lambda b, h: {"x": 1}),
        patch.object(settings, "email_intake_max_bytes", 1024),
    ):
        response = await inbound_webhook(provider="ses", request=request, ctrl_db=None)

    assert response.status_code == 204
    request.body.assert_not_awaited()


async def test_inbound_webhook_content_length_malformed_rejects_before_body_read():
    """A non-integer Content-Length header must also reject before reading."""
    from unittest.mock import AsyncMock

    from app.api.email_intake import inbound_webhook
    from app.config import settings

    request = SimpleNamespace(
        headers={"content-length": "not-a-number"},
        body=AsyncMock(return_value=b"{}"),
    )

    with (
        patch("app.api.email_intake.get_parser", return_value=lambda b, h: {"x": 1}),
        patch.object(settings, "email_intake_max_bytes", 1024),
    ):
        response = await inbound_webhook(provider="ses", request=request, ctrl_db=None)

    assert response.status_code == 204
    request.body.assert_not_awaited()


async def test_inbound_webhook_oversized_body_without_content_length_rejects_after_read():
    """Simulates chunked transfer (no Content-Length header): the body is
    read once, then rejected by the post-read length check."""
    from unittest.mock import AsyncMock

    from app.api.email_intake import inbound_webhook
    from app.config import settings

    big_body = b"x" * 2048
    request = SimpleNamespace(
        headers={},
        body=AsyncMock(return_value=big_body),
    )

    with (
        patch("app.api.email_intake.get_parser", return_value=lambda b, h: {"x": 1}),
        patch.object(settings, "email_intake_max_bytes", 1024),
    ):
        response = await inbound_webhook(provider="ses", request=request, ctrl_db=None)

    assert response.status_code == 204
    request.body.assert_awaited_once()


async def test_inbound_webhook_content_length_understates_actual_size_still_rejects():
    """A Content-Length header that lies (understates the real body) must
    still be caught by the post-read re-check, not trusted blindly."""
    from unittest.mock import AsyncMock

    from app.api.email_intake import inbound_webhook
    from app.config import settings

    big_body = b"x" * 2048
    request = SimpleNamespace(
        headers={"content-length": "10"},
        body=AsyncMock(return_value=big_body),
    )

    with (
        patch("app.api.email_intake.get_parser", return_value=lambda b, h: {"x": 1}),
        patch.object(settings, "email_intake_max_bytes", 1024),
    ):
        response = await inbound_webhook(provider="ses", request=request, ctrl_db=None)

    assert response.status_code == 204
    request.body.assert_awaited_once()


async def test_inbound_webhook_normal_signed_request_under_cap_still_succeeds():
    """Regression guard: the new size-cap check must not break the existing
    valid-signature path (default cap is a few MB; this body is tiny)."""
    from unittest.mock import AsyncMock

    from app.api.email_intake import inbound_webhook
    from app.services.email_intake import IntakeResult

    request = SimpleNamespace(
        headers={"X-Signature": "sig", "content-length": "2"},
        body=AsyncMock(return_value=b"{}"),
    )
    hit = IntakeResult(tenant_slug="acme", invoices_created=[uuid.uuid4()])

    with (
        patch("app.api.email_intake.get_parser", return_value=lambda b, h: {"x": 1}),
        patch("app.api.email_intake.verify_signature", return_value=True),
        patch("app.api.email_intake.process_inbound_email", AsyncMock(return_value=hit)),
    ):
        response = await inbound_webhook(provider="ses", request=request, ctrl_db=None)

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "received"}


def test_verify_signature_accepts_valid_signature():
    from app.services import email_intake

    body = b"payload"
    secret = "s3cr3t"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    with patch.object(email_intake.settings, "email_intake_signing_secret", secret):
        assert email_intake.verify_signature(body, sig) is True
        assert email_intake.verify_signature(body, "deadbeef") is False


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------


def test_generic_adapter_parses_normalized_json():
    from app.services.email_intake_adapters import get_parser

    pdf = b"%PDF-1.4 fake"
    body = json.dumps(
        {
            "to": "invoices+tok@ap.example.com",
            "from": "ap@vendor.com",
            "subject": "Invoice 42",
            "attachments": [
                {
                    "filename": "inv.pdf",
                    "content_type": "application/pdf",
                    "content_base64": base64.b64encode(pdf).decode(),
                }
            ],
        }
    ).encode()

    parser = get_parser("generic")
    parsed = parser(body, {})

    assert parsed is not None
    assert parsed.to == "invoices+tok@ap.example.com"
    assert parsed.sender == "ap@vendor.com"
    assert parsed.subject == "Invoice 42"
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].content == pdf
    assert parsed.attachments[0].content_type == "application/pdf"


def test_generic_adapter_returns_none_on_malformed_json():
    from app.services.email_intake_adapters import get_parser

    parser = get_parser("generic")
    assert parser(b"not-json", {}) is None


def test_generic_adapter_rejects_missing_to_or_from():
    from app.services.email_intake_adapters import get_parser

    parser = get_parser("generic")
    assert parser(b'{"to": "", "from": "x@y.com"}', {}) is None
    assert parser(b'{"to": "x@y.com", "from": ""}', {}) is None


def test_ses_adapter_parses_mime():
    """SES posts a full MIME message inside SNS's envelope; the adapter
    unwraps both layers and surfaces the first PDF attachment."""
    from app.services.email_intake_adapters import get_parser

    pdf = b"%PDF-1.4 fake"
    pdf_b64 = base64.b64encode(pdf).decode()
    raw_mime = (
        "From: ap@vendor.com\r\n"
        "To: invoices+tok@ap.example.com\r\n"
        "Subject: Monthly invoice\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="boundary"\r\n'
        "\r\n"
        "--boundary\r\n"
        "Content-Type: text/plain\r\n\r\n"
        "See attached.\r\n"
        "--boundary\r\n"
        "Content-Type: application/pdf\r\n"
        "Content-Transfer-Encoding: base64\r\n"
        'Content-Disposition: attachment; filename="invoice.pdf"\r\n\r\n'
        f"{pdf_b64}\r\n"
        "--boundary--\r\n"
    )
    envelope = {"Message": json.dumps({"content": raw_mime})}
    body = json.dumps(envelope).encode()

    parser = get_parser("ses")
    parsed = parser(body, {})

    assert parsed is not None
    assert parsed.sender == "ap@vendor.com"
    assert "invoices+tok@ap.example.com" in parsed.to
    pdfs = [a for a in parsed.attachments if a.content_type == "application/pdf"]
    assert len(pdfs) == 1
    assert pdfs[0].filename == "invoice.pdf"
    assert pdfs[0].content == pdf


def _ses_body(raw_mime: str, **notification) -> bytes:
    return json.dumps({"Message": json.dumps({"content": raw_mime, **notification})}).encode()


_REDIRECTED_MIME = (
    # What an AP mailbox's redirect rule (or a vendor's Bcc) delivers: the
    # header still names the customer's own mailbox, never the intake address.
    "From: ap@vendor.com\r\n"
    "To: accounts-payable@customer.example\r\n"
    "Subject: Invoice\r\n"
    "\r\n"
    "body\r\n"
)


def test_ses_adapter_routes_on_the_envelope_recipient_not_the_to_header():
    """A Bcc'd or redirected intake message has no intake address in `To:` —
    only the SMTP envelope (`receipt.recipients`) knows where SES delivered it.
    Reading the header dropped every such message as an unknown address."""
    from app.services.email_intake import extract_tokens
    from app.services.email_intake_adapters import get_parser

    parsed = get_parser("ses")(
        _ses_body(
            _REDIRECTED_MIME,
            mail={"destination": ["invoices+tok@ap.example.com"]},
            receipt={"recipients": ["invoices+tok@ap.example.com"]},
        ),
        {},
    )
    assert parsed is not None
    assert extract_tokens(parsed.to) == ["tok"]


def test_ses_adapter_keeps_every_envelope_recipient():
    from app.services.email_intake import extract_tokens
    from app.services.email_intake_adapters import get_parser

    parsed = get_parser("ses")(
        _ses_body(
            _REDIRECTED_MIME,
            receipt={"recipients": ["invoices+one@ap.example.com", "invoices+two@ap.example.com"]},
        ),
        {},
    )
    assert extract_tokens(parsed.to) == ["one", "two"]


def test_ses_adapter_falls_back_to_mail_destination_then_the_header():
    from app.services.email_intake import extract_tokens
    from app.services.email_intake_adapters import get_parser

    parser = get_parser("ses")
    via_destination = parser(
        _ses_body(_REDIRECTED_MIME, mail={"destination": ["invoices+dest@ap.example.com"]}), {}
    )
    assert extract_tokens(via_destination.to) == ["dest"]

    header_only = parser(
        _ses_body("From: a@b.c\r\nTo: invoices+hdr@ap.example.com\r\n\r\nx\r\n", receipt="junk"),
        {},
    )
    assert extract_tokens(header_only.to) == ["hdr"]


def test_mailgun_adapter_parses_json_form():
    from app.services.email_intake_adapters import get_parser

    pdf = b"%PDF fake"
    body = json.dumps(
        {
            "recipient": "invoices+tok@ap.example.com",
            "sender": "ap@vendor.com",
            "subject": "Inv",
            "attachment-count": 1,
            "attachment-1": {
                "filename": "inv.pdf",
                "content-type": "application/pdf",
                "content_base64": base64.b64encode(pdf).decode(),
            },
        }
    ).encode()

    parser = get_parser("mailgun")
    parsed = parser(body, {})

    assert parsed is not None
    assert parsed.to == "invoices+tok@ap.example.com"
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].content == pdf


def test_unknown_provider_returns_none():
    from app.services.email_intake_adapters import get_parser

    assert get_parser("totally-made-up") is None


# ---------------------------------------------------------------------------
# Attachment filtering
# ---------------------------------------------------------------------------


def test_usable_attachments_drops_wrong_content_type():
    from app.services.email_intake import (
        InboundAttachment,
        IntakeResult,
        _usable_attachments,
    )

    atts = [
        InboundAttachment(filename="a.pdf", content_type="application/pdf", content=b"x"),
        InboundAttachment(filename="b.exe", content_type="application/octet-stream", content=b"x"),
        InboundAttachment(filename="c.jpg", content_type="image/jpeg", content=b"x"),
        InboundAttachment(filename="d.pdf", content_type="application/pdf", content=b""),
    ]
    result = IntakeResult()

    kept = list(_usable_attachments(atts, result))

    assert len(kept) == 2
    assert {a.filename for a in kept} == {"a.pdf", "c.jpg"}
    assert any("b.exe" in s for s in result.skipped_attachments)
    assert any("d.pdf" in s for s in result.skipped_attachments)


# ---------------------------------------------------------------------------
# Real-DB e2e: the admin rotate-token endpoint actually changes the intake
# address and the prior address stops resolving (role-gated to admin).
# ---------------------------------------------------------------------------


async def test_admin_rotate_token_changes_address_and_kills_old(realdb, monkeypatch):
    from app.config import settings as cfg
    from app.services.email_intake import resolve_tenant_from_recipient

    # An intake domain must be configured for an address to render.
    monkeypatch.setattr(cfg, "email_intake_domain", "intake.test")

    # Non-admins cannot rotate.
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.post("/api/organization/email-intake/rotate-token")).status_code == 403

    async with realdb.client(key="a", role="admin") as c:
        first = (await c.post("/api/organization/email-intake/rotate-token")).json()["address"]
        second = (await c.post("/api/organization/email-intake/rotate-token")).json()["address"]
        shown = await c.get("/api/organization/email-intake")

    assert first.startswith("invoices+") and first.endswith("@intake.test")
    assert second != first  # rotation minted a new token → new address
    body = shown.json()
    assert body["address"] == second
    assert body["enabled"] is True
    assert body["domain_configured"] is True

    # The old address no longer resolves; the current one does. Goes through
    # realdb.control_sessionmaker() (not a bare create_async_engine(cfg.database_url))
    # since the harness's org lives in this process's per-slot control-plane
    # database, not the real, shared one.
    mk = realdb.control_sessionmaker()
    async with mk() as s:
        assert await resolve_tenant_from_recipient(s, first) is None
        org = await resolve_tenant_from_recipient(s, second)
        assert org is not None
        assert org.slug == realdb.info("a").slug


async def test_admin_intake_read_distinguishes_no_domain_from_no_token(realdb, monkeypatch):
    """`address: null` has two causes and the UI needs opposite copy for each.

    With no `FEOH_EMAIL_INTAKE_DOMAIN` the deployment cannot do email intake at
    all; with a domain but no token the org simply has not created its address
    yet. Both render `address: null`, so without `domain_configured` an admin
    panel had to mint a throwaway token to tell them apart.
    """
    from app.config import settings as cfg

    # No platform domain: intake is unavailable regardless of any token.
    monkeypatch.setattr(cfg, "email_intake_domain", "")
    async with realdb.client(key="a", role="admin") as c:
        body = (await c.get("/api/organization/email-intake")).json()
    assert body["address"] is None
    assert body["domain_configured"] is False

    # Domain configured, org not yet provisioned: still no address, but now the
    # panel can offer to create one instead of claiming the feature is off.
    monkeypatch.setattr(cfg, "email_intake_domain", "intake.test")
    async with realdb.client(key="a", role="admin") as c:
        body = (await c.get("/api/organization/email-intake")).json()
    assert body["domain_configured"] is True


async def test_admin_intake_read_is_admin_only(realdb, monkeypatch):
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "email_intake_domain", "intake.test")
    async with realdb.client(key="a", role="ap_manager") as c:
        assert (await c.get("/api/organization/email-intake")).status_code == 403
