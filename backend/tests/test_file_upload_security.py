"""File upload + download security tests.

Two attack surfaces meet here:
  1. Upload: the filename is part of the S3 key, so a malicious
     filename (path-traversal, control chars) could land the upload
     under another tenant's prefix.
  2. Download: `GET /api/workflow/file/{file_key:path}` accepts the
     S3 key in the URL. Without a cross-check on the user's org, an
     authenticated user from tenant A can download tenant B's files
     by passing a crafted key.

Tests:
  - Filename sanitiser strips `..`, slashes, control chars
  - Upload rejects oversize / wrong content-type
  - Download endpoint refuses keys whose org-prefix doesn't match
    the requesting user's organization
  - Same 404 for "wrong org" and "no such file" (no enumeration)
"""

from __future__ import annotations

import uuid

import pytest

# ---------------------------------------------------------------------------
# Filename sanitiser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_no",
    [
        ("../../etc/passwd", "../"),
        ("..\\..\\windows\\system32.exe", "\\"),
        ("normal.pdf", None),
        ("with spaces.pdf", None),
        ("invoice;rm -rf /;.pdf", ";"),
    ],
)
def test_safe_filename_strips_path_separators_and_dangerous_chars(
    raw: str, expected_no: str | None
):
    """Path-traversal probes must be flattened. The cleaned name
    must not contain `/`, `\\`, or `..` anywhere — those are what
    let an attacker steer the S3 key into another prefix."""
    from app.services.storage import _safe_filename

    cleaned = _safe_filename(raw)
    assert "/" not in cleaned
    assert "\\" not in cleaned
    assert ".." not in cleaned
    if expected_no is not None:
        assert expected_no not in cleaned, f"{expected_no!r} survived in {cleaned!r}"


def test_safe_filename_falls_back_when_input_is_empty_or_none():
    """A missing / fully-stripped filename must become a synthetic
    placeholder, never an empty string. An empty segment in the S3
    key would produce `org/invoice//` — an unusable double-slash
    key that's surprising downstream."""
    from app.services.storage import _safe_filename

    assert _safe_filename(None) == "upload"
    assert _safe_filename("") == "upload"
    assert _safe_filename("...") == "upload"  # leading dots stripped, body empty


def test_safe_filename_drops_leading_dots_to_prevent_dotfiles():
    """`.htaccess`-style names should not survive as dotfiles in
    storage — an attacker could shadow a server-side conf if any
    downstream tool reads the directory."""
    from app.services.storage import _safe_filename

    assert not _safe_filename(".htaccess").startswith(".")
    assert not _safe_filename(".env").startswith(".")


def test_safe_filename_strips_null_bytes_and_control_chars():
    """Null bytes are a common probe to confuse C-string handling
    or log parsers. The regex must reject anything outside the
    safe character set."""
    from app.services.storage import _safe_filename

    raw = "evil\x00.pdf\x07"
    cleaned = _safe_filename(raw)
    assert "\x00" not in cleaned
    assert "\x07" not in cleaned


def test_safe_filename_keeps_dots_and_dashes_and_underscores_within_the_name():
    """Hyphens, underscores, and intra-name dots are legitimate
    filename components — the sanitiser must not nuke them."""
    from app.services.storage import _safe_filename

    assert _safe_filename("Q1-invoice_v2.final.pdf") == "Q1-invoice_v2.final.pdf"


# ---------------------------------------------------------------------------
# Upload constraints — size + content type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_rejects_oversize_file(monkeypatch):
    """`MAX_FILE_SIZE` is the hard cap; the helper must `raise
    ValueError` rather than silently truncate or write a giant
    object. Without this, a vendor user could upload a 5 GB file and
    DoS the bucket."""
    from unittest.mock import AsyncMock, MagicMock

    from app.services import storage

    huge = b"A" * (storage.MAX_FILE_SIZE + 1)
    fake_file = MagicMock()
    fake_file.read = AsyncMock(return_value=huge)
    fake_file.content_type = "application/pdf"
    fake_file.filename = "huge.pdf"

    # The S3 client shouldn't even be reached on the failure path.
    monkeypatch.setattr(storage, "_get_client", lambda: pytest.fail("S3 must not be called"))

    with pytest.raises(ValueError, match="exceeds maximum size"):
        await storage.upload_invoice_file(uuid.uuid4(), uuid.uuid4(), fake_file)


@pytest.mark.asyncio
async def test_upload_rejects_disallowed_content_type(monkeypatch):
    """The allow-list is PDF + PNG/JPEG/TIFF. An attacker who tries
    to upload an HTML file (XSS-via-download) or an executable must
    be refused at the gate."""
    from unittest.mock import AsyncMock, MagicMock

    from app.services import storage

    fake_file = MagicMock()
    fake_file.read = AsyncMock(return_value=b"<html>evil</html>")
    fake_file.content_type = "text/html"
    fake_file.filename = "evil.html"

    monkeypatch.setattr(storage, "_get_client", lambda: pytest.fail("S3 must not be called"))

    with pytest.raises(ValueError, match="not allowed"):
        await storage.upload_invoice_file(uuid.uuid4(), uuid.uuid4(), fake_file)


@pytest.mark.asyncio
async def test_upload_rejects_executable_content_type(monkeypatch):
    """Same gate, second shape — application/x-msdownload is the
    Windows-executable mime. An attacker who flips their upload to
    that mime to bypass a UI filter must still be refused server-side."""
    from unittest.mock import AsyncMock, MagicMock

    from app.services import storage

    fake_file = MagicMock()
    fake_file.read = AsyncMock(return_value=b"MZ\x00\x00")
    fake_file.content_type = "application/x-msdownload"
    fake_file.filename = "trojan.exe"

    monkeypatch.setattr(storage, "_get_client", lambda: pytest.fail("S3 must not be called"))

    with pytest.raises(ValueError, match="not allowed"):
        await storage.upload_invoice_file(uuid.uuid4(), uuid.uuid4(), fake_file)


@pytest.mark.asyncio
async def test_upload_uses_sanitised_filename_in_s3_key(monkeypatch):
    """End-to-end: a path-traversal filename produces a sanitised
    key, not the attacker's literal."""
    from unittest.mock import AsyncMock, MagicMock

    from app.services import storage

    captured_key: dict = {}

    def fake_client_factory():
        client = MagicMock()
        client.head_bucket = MagicMock()
        client.put_object = MagicMock(
            side_effect=lambda **kwargs: captured_key.update(Key=kwargs["Key"])
        )
        return client

    monkeypatch.setattr(storage, "_get_client", fake_client_factory)

    fake_file = MagicMock()
    fake_file.read = AsyncMock(return_value=b"%PDF-1.4 valid")
    fake_file.content_type = "application/pdf"
    fake_file.filename = "../../other-org/secret.pdf"

    org_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    file_key, _url = await storage.upload_invoice_file(org_id, invoice_id, fake_file)

    # The literal traversal probe must not survive in the key.
    assert "../" not in file_key
    assert "other-org" not in file_key.split("/")[0]
    assert file_key.startswith(f"{org_id}/{invoice_id}/")
    assert captured_key["Key"] == file_key


@pytest.mark.asyncio
async def test_w9_upload_uses_sanitised_filename_in_s3_key(monkeypatch):
    """The 1099 W-9 upload path constructs its own S3 key (not via
    ``upload_invoice_file``) so it needed its own ``_safe_filename``
    call. Verify a path-traversal probe doesn't survive the round trip."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from app.api import tax as tax_mod
    from app.services import storage

    captured: dict = {}

    def fake_client_factory():
        client = MagicMock()
        client.put_object = MagicMock(
            side_effect=lambda **kwargs: captured.update(Key=kwargs["Key"])
        )
        return client

    monkeypatch.setattr(storage, "_get_client", fake_client_factory)
    monkeypatch.setattr(storage, "_ensure_bucket", lambda c: None)

    vendor = SimpleNamespace(
        id=uuid4(),
        w9_file_key=None,
        w9_received_date=None,
        is_1099_eligible=False,
        tax_classification=None,
        tax_id=None,
    )
    monkeypatch.setattr(tax_mod, "_get_vendor_or_404", AsyncMock(return_value=vendor))
    monkeypatch.setattr(
        tax_mod, "_vendor_tax_response", lambda v: {"id": str(v.id), "file_key": v.w9_file_key}
    )

    fake_file = MagicMock()
    fake_file.read = AsyncMock(return_value=b"%PDF-1.4 W9")
    fake_file.content_type = "application/pdf"
    fake_file.filename = "../../other-org/secret.pdf"

    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    org_id = uuid4()
    await tax_mod.upload_vendor_w9(
        vendor_id=vendor.id,
        file=fake_file,
        tax_classification=None,
        is_1099_eligible=True,
        db=db,
        user=SimpleNamespace(id=uuid4()),
        org_id=org_id,
        # Consolidated view — called directly, so the Depends default is not
        # resolved and would otherwise reach `ensure_in_entity_scope`.
        entity_id=None,
    )

    assert "../" not in captured["Key"]
    assert "other-org" not in captured["Key"].split("/")[0]
    assert captured["Key"].startswith(f"{org_id}/w9/{vendor.id}/")


# ---------------------------------------------------------------------------
# Download endpoint — cross-tenant file access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_invoice_file_refuses_cross_tenant_file_key():
    """A user in org A who calls `/api/workflow/file/<orgB-uuid>/...`
    must get a 404 — not the file. The endpoint's auth dep only
    proves the user is *some* authenticated user; the file key tells
    us which tenant owns it. Cross-check is mandatory."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from app.api.workflow import get_invoice_file

    user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
    other_org = uuid.uuid4()
    cross_tenant_key = f"{other_org}/some-invoice/file.pdf"

    with patch("app.api.workflow.get_file", AsyncMock()) as mk_get_file:
        with pytest.raises(HTTPException) as exc:
            await get_invoice_file(file_key=cross_tenant_key, user=user)

    assert exc.value.status_code == 404
    mk_get_file.assert_not_called(), "S3 must not be touched when org check fails"


@pytest.mark.asyncio
async def test_get_invoice_file_same_org_succeeds():
    """Positive control — when the file key's first segment IS the
    user's org, the file is fetched and returned. Without this, the
    cross-tenant test could pass because every request 404s."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from app.api.workflow import get_invoice_file

    user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
    same_org_key = f"{user.organization_id}/inv-1/file.pdf"

    with patch(
        "app.api.workflow.get_file",
        AsyncMock(return_value=(b"PDF content", "application/pdf")),
    ):
        resp = await get_invoice_file(file_key=same_org_key, user=user)

    # Response body is the file content.
    assert resp.body == b"PDF content"
    assert resp.media_type == "application/pdf"


@pytest.mark.asyncio
async def test_get_invoice_file_returns_same_404_for_wrong_org_and_missing_file():
    """No enumeration: "wrong org" and "no such file in your org"
    must produce the same 404 with the same detail. A diff would
    let an attacker map other tenants' UUID prefixes."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from app.api.workflow import get_invoice_file

    user = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())

    # Wrong-org case
    with pytest.raises(HTTPException) as exc_wrong_org:
        await get_invoice_file(
            file_key=f"{uuid.uuid4()}/inv/x.pdf",
            user=user,
        )

    # Missing-file case (same-org key but S3 raises NoSuchKey)
    with patch("app.api.workflow.get_file", AsyncMock(side_effect=Exception("NoSuchKey"))):
        with pytest.raises(HTTPException) as exc_missing:
            await get_invoice_file(
                file_key=f"{user.organization_id}/inv/x.pdf",
                user=user,
            )

    assert exc_wrong_org.value.status_code == exc_missing.value.status_code == 404
    assert exc_wrong_org.value.detail == exc_missing.value.detail
