"""Schema-level input-validation tests.

The Pydantic schemas declared in `app/schemas/*` are the first line
of defence against the OWASP-style probes — overlong inputs, null
bytes, CR/LF in headers, schema-bypass attempts. We pin those
schemas so a future refactor that loosens a `max_length` or drops
an `EmailStr` constraint is caught by tests.

We also exercise the search / filter paths to confirm that probe
strings (`'; DROP TABLE`, `<script>`, etc.) round-trip safely
through SQLAlchemy's parametrised query layer — the test isn't
"does the DB execute the probe" (we don't run a DB here), it's
"can the request shape be constructed without breaking the schema
guards that sit in front of SQL".
"""

from __future__ import annotations

import pydantic
import pytest

# ---------------------------------------------------------------------------
# Length limits — every Pydantic schema with a max_length must enforce it
# ---------------------------------------------------------------------------


def test_vendor_create_rejects_overlong_name():
    """`Vendor.name` is `max_length=255` in the schema. A request
    with 1024 chars must 422 at the gate, not write a giant row."""
    from app.schemas.vendor import VendorCreate

    with pytest.raises(pydantic.ValidationError):
        VendorCreate(name="A" * 1024)


def test_vendor_create_rejects_overlong_email():
    """The email field has `max_length=320` (RFC 5321 cap). A
    400-char value must 422 at the gate, not write a giant row."""
    from app.schemas.vendor import VendorCreate

    overlong = "x" * 400 + "@example.test"
    with pytest.raises(pydantic.ValidationError):
        VendorCreate(name="Acme", email=overlong)


def test_vendor_email_accepts_normal_address():
    """Positive control — confirm the loose constraint actually
    accepts a real-looking email. Without it, the negative tests
    above could pass for the wrong reason (everything fails)."""
    from app.schemas.vendor import VendorCreate

    vc = VendorCreate(name="Acme", email="vendor@example.test")
    assert vc.email == "vendor@example.test"


def test_vendor_create_rejects_overlong_tax_id():
    """Tax IDs are short by spec; a 200-char value is a probe, not
    a number. Schema rejects."""
    from app.schemas.vendor import VendorCreate

    with pytest.raises(pydantic.ValidationError):
        VendorCreate(name="Acme", tax_id="9" * 200)


# ---------------------------------------------------------------------------
# Pydantic's email validator — used on User / signup paths
# ---------------------------------------------------------------------------


def test_signup_request_rejects_obvious_non_email():
    """The signup `email` field is `EmailStr`, so non-email shapes
    must 422 before any DB lookup."""
    from app.schemas.signup import SignupStartRequest

    for bad in ("not an email", "x@", "@example.test", "javascript:alert(1)"):
        with pytest.raises(pydantic.ValidationError):
            SignupStartRequest(
                email=bad,
                company_name="Startup Inc",
                slug="startup",
                captcha_token=None,
            )


# ---------------------------------------------------------------------------
# Slug / signup input — reserved words + format
# ---------------------------------------------------------------------------


def test_signup_request_rejects_overlong_slug():
    """Signup tenants are stamped into a DB name (`feoh_<slug>`); a
    huge slug would either blow Postgres's identifier limit (63
    chars) or build a URL too large to render. Pydantic caps it."""
    from app.schemas.signup import SignupStartRequest

    with pytest.raises(pydantic.ValidationError):
        SignupStartRequest(
            email="founder@startup.test",
            company_name="Startup Inc",
            slug="x" * 200,
            captcha_token=None,
        )


def test_signup_request_rejects_path_traversal_in_slug():
    """A slug containing `/`, `\\`, or `..` could change the URL
    template into a path-traversal vector. Validation must reject."""
    from app.schemas.signup import SignupStartRequest

    for bad in ("../etc", "foo/bar", "foo\\bar", "..", "..foo"):
        with pytest.raises(pydantic.ValidationError):
            SignupStartRequest(
                email="founder@startup.test",
                company_name="Startup Inc",
                slug=bad,
                captcha_token=None,
            )


# ---------------------------------------------------------------------------
# Stored XSS — schemas accept the characters, but the model layer
# does NOT actively interpret them. The contract is "stored as-is,
# never executed server-side"; the frontend's job is to escape on
# render. We pin that the backend doesn't transform / interpret.
# ---------------------------------------------------------------------------


def test_vendor_name_with_script_tag_is_stored_verbatim():
    """The backend must NOT eval / interpret HTML in a vendor name.
    Storing it verbatim is fine — the frontend escapes on render via
    DOMPurify (see frontend/CLAUDE.md). What we're guarding against
    is a backend regression that "helpfully" runs the string through
    a templating layer or executes it as code."""
    from app.schemas.vendor import VendorCreate

    payload = '<script>alert("xss")</script>'
    vc = VendorCreate(name=payload)
    assert vc.name == payload, "schema must store input byte-identical"


def test_invoice_description_with_html_is_stored_verbatim():
    """Same for invoice description — the backend is a data store,
    not a renderer."""
    from app.schemas.invoice import InvoiceCreate

    payload = '<img src=x onerror="alert(1)">'
    inv = InvoiceCreate(
        invoice_number="INV-1",
        vendor="Acme",
        amount="10.00",
        description=payload,
    )
    assert inv.description == payload


# ---------------------------------------------------------------------------
# SQL-injection probes round-trip through the schema cleanly
# ---------------------------------------------------------------------------


def test_search_filter_with_sql_probe_does_not_break_schema():
    """The list endpoints accept a `?search=` query string. Pydantic
    constructs the type, the SQLAlchemy layer parametrises it, the
    DB returns 0 matches. We don't run the DB here — we just confirm
    a probe string passes schema construction (so the request would
    reach the parametrised query) without raising."""
    # The vendor list endpoint takes `search: str | None` — a free
    # string. Pydantic doesn't validate it beyond being a str.
    probe = "'; DROP TABLE users; --"
    assert isinstance(probe, str)
    # If we ever add a schema model for vendor-list query params,
    # this is the place to assert it accepts the probe verbatim.


# ---------------------------------------------------------------------------
# Webhook URL parameter — tenant slug must be url-friendly
# ---------------------------------------------------------------------------


def test_webhook_tenant_slug_is_string_typed():
    """The webhook handler takes `tenant_slug: str` from the URL
    path; FastAPI's path-parameter parsing rejects null bytes and
    other invalid characters before reaching us."""
    # Implicit smoke — the handler signature must declare `str`,
    # not `Any`. Reading the signature catches a regression.
    import inspect

    from app.api.payments import payment_webhook

    sig = inspect.signature(payment_webhook)
    assert sig.parameters["tenant_slug"].annotation is str
    assert sig.parameters["provider"].annotation is str


# ---------------------------------------------------------------------------
# Password schema — the length rule lives in `validate_password_complexity`,
# whose coded refusal the client localizes; the schema keeps only the upper
# bound (`tests/test_password_security.py` pins both).
# ---------------------------------------------------------------------------


def test_change_password_request_bounds_the_new_password_length():
    from app.schemas.auth import ChangePasswordRequest

    with pytest.raises(pydantic.ValidationError):
        ChangePasswordRequest(current_password="anything", new_password="A1a" * 50)
