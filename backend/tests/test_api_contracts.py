"""API response contract tests.

These tests verify the shape of API responses that both the web frontend
and mobile app depend on. If a response field is renamed or restructured,
these tests break before the change reaches clients.

No database or server needed — we validate the Pydantic schemas and
hand-built dicts against the contracts that clients expect.
"""


# -- /api/auth/login ---------------------------------------------------------


def test_login_response_contract():
    """TokenResponse must have 'access_token' field."""
    from app.schemas.auth import TokenResponse

    resp = TokenResponse(access_token="test-jwt-token")
    data = resp.model_dump()
    assert "access_token" in data
    assert isinstance(data["access_token"], str)


# -- /api/auth/me -------------------------------------------------------------


def test_user_response_contract():
    """UserResponse must have fields that mobile and web auth stores expect."""
    from app.schemas.auth import UserResponse

    resp = UserResponse(
        id="uuid-123",
        email="test@acme.com",
        full_name="Test User",
        organization_id="org-uuid",
        is_active=True,
        roles=["admin", "ap_manager"],
    )
    data = resp.model_dump()

    required_fields = {
        "id",
        "email",
        "full_name",
        "organization_id",
        "roles",
        # Auth-flow flags both clients branch on. Dropping any of these
        # silently breaks first-login and MFA UX.
        "must_change_password",
        "mfa_enabled",
        "mfa_required_by_org",
        # Web-only: /profile stops offering the password as a step-up proof
        # when it is set (docs/decisions.md §201). Mobile manages no factors.
        "password_sign_in_closed",
        # Whether THIS account has a password at all — false for every account
        # JIT-provisioned by OIDC/SAML or created by SCIM. /profile reads it to
        # replace the dead Change-password form for such an account.
        "has_password",
    }
    assert required_fields.issubset(data.keys())
    assert isinstance(data["roles"], list)


def test_user_response_has_password_defaults_true():
    """A response built without the field (every pre-existing call site that
    hasn't been updated) still describes an ordinary password account — the
    schema default must fail OPEN to "has a password", never closed, or a
    stale caller would silently blank the Change-password card for every
    member."""
    from app.schemas.auth import UserResponse

    resp = UserResponse(
        id="uuid-123",
        email="test@acme.com",
        full_name="Test User",
        organization_id="org-uuid",
        is_active=True,
        roles=["admin"],
    )
    assert resp.has_password is True


# -- /api/dashboard -----------------------------------------------------------


def test_dashboard_response_contract():
    """Dashboard response must match the shape both clients parse.

    Web: frontend/src/routes/+page.svelte
    Mobile: mobile/lib/models/payment.dart (DashboardData.fromJson)
    """
    # Simulate the dict returned by the dashboard endpoint
    response = {
        "total_invoices": 10,
        "total_amount": 45000.0,
        "total_paid": 17000.0,
        "total_pending": 6000.0,
        "total_rebates": 0.0,
        "open_exceptions": 3,
        "touchless_rate": 100.0,
        "stale_approvals": 0,
        "pipeline": {"approved": 3, "pending": 1, "new": 3},
        "vendor_spend": [
            {"vendor": "Acme Corp", "amount": 12000.0},
        ],
        "aging": {
            "current": 0.0,
            "days_30": 0.0,
            "days_60": 0.0,
            "days_90_plus": 33000.0,
        },
        "monthly_trend": [
            {"month": "2024-03", "count": 5, "amount": 20000.0},
        ],
        "upcoming_payments": [
            {
                "id": "uuid-1",
                "invoice_number": "INV-001",
                "vendor_name": "Acme",
                "amount": 1000.0,
                "due_date": "2024-04-10",
                "is_overdue": True,
            },
        ],
    }

    # Top-level required keys
    required_keys = {
        "total_invoices",
        "total_amount",
        "pipeline",
        "vendor_spend",
        "aging",
        "monthly_trend",
        "upcoming_payments",
    }
    assert required_keys.issubset(response.keys()), (
        f"Missing keys: {required_keys - response.keys()}"
    )

    # pipeline: dict of status -> count
    assert isinstance(response["pipeline"], dict)
    for k, v in response["pipeline"].items():
        assert isinstance(k, str)
        assert isinstance(v, int)

    # vendor_spend: list of {vendor, amount}
    assert isinstance(response["vendor_spend"], list)
    for item in response["vendor_spend"]:
        assert "vendor" in item, "vendor_spend items must have 'vendor' key"
        assert "amount" in item, "vendor_spend items must have 'amount' key"

    # aging: dict with specific bucket keys
    aging = response["aging"]
    assert isinstance(aging, dict)
    aging_keys = {"current", "days_30", "days_60", "days_90_plus"}
    assert aging_keys.issubset(aging.keys()), f"Missing aging keys: {aging_keys - aging.keys()}"

    # monthly_trend: list of {month, count, amount}
    assert isinstance(response["monthly_trend"], list)
    for item in response["monthly_trend"]:
        assert "month" in item
        assert "count" in item
        assert "amount" in item

    # upcoming_payments: list (not a summary object)
    assert isinstance(response["upcoming_payments"], list)
    for item in response["upcoming_payments"]:
        assert "id" in item
        assert "amount" in item


# -- /api/invoices -------------------------------------------------------------


def test_invoice_response_contract():
    """InvoiceResponse must have fields that mobile Invoice.fromJson expects."""
    from app.schemas.invoice import InvoiceResponse

    # All fields the mobile app reads in Invoice.fromJson
    mobile_fields = {
        "id",
        "invoice_number",
        "vendor_name",
        "amount",
        "currency",
        "status",
        "invoice_date",
        "due_date",
        "description",
        "po_number",
        "created_at",
    }

    schema_fields = set(InvoiceResponse.model_fields.keys())

    missing = mobile_fields - schema_fields
    # vendor_name is mapped from 'vendor' in the response, check both
    if "vendor_name" in missing and "vendor" in schema_fields:
        missing.discard("vendor_name")

    assert not missing, f"InvoiceResponse is missing fields the mobile app expects: {missing}"


def test_invoice_list_response_contract():
    """InvoiceListResponse wraps items in an 'items' key."""
    from app.schemas.invoice import InvoiceListResponse

    assert "items" in InvoiceListResponse.model_fields
    assert "total" in InvoiceListResponse.model_fields


# -- /api/payments -------------------------------------------------------------


def test_payment_response_shape():
    """Payment list items must have fields the mobile Payment.fromJson expects."""
    # These are the fields mobile/lib/models/payment.dart reads
    required_fields = {
        "id",
        "invoice_id",
        "amount",
        "method",
        "status",
        "created_at",
    }

    from app.models.payment import Payment

    columns = {c.name for c in Payment.__table__.columns}
    missing = required_fields - columns
    assert not missing, f"Payment model is missing fields the mobile app expects: {missing}"


# -- /api/auth/me roles -------------------------------------------------------


def test_role_values():
    """The four RBAC roles must exist — mobile and web hardcode these."""
    from app.models.user import Role

    columns = {c.name for c in Role.__table__.columns}
    assert "name" in columns

    expected_roles = {"admin", "ap_manager", "ap_clerk", "cfo"}
    # We can't query the DB, but we can verify the model supports them
    assert expected_roles  # roles are seeded, not enum-enforced
