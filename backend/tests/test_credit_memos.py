"""Real-DB coverage for the credit-memos router.

Covers ``backend/app/api/credit_memos.py`` end-to-end against two live test
tenants: list/get (incl. search + the sort allowlist), the per-status summary
behind the filter chips, create (open + applied-at-creation), apply-to-invoice,
void, the 409 lifecycle guards, RBAC, tenant isolation, and the Decimal money
math (amounts are ``Numeric(15, 2)`` and must round-trip exactly).
"""

from decimal import Decimal

from sqlalchemy import func, select

from app.models.credit_memo import CreditMemo
from app.models.invoice import Invoice, InvoiceStatus
from app.models.vendor import Vendor


async def _add_vendor(mk, org_id, name="Acme Supplies") -> str:
    async with mk() as s:
        v = Vendor(organization_id=org_id, name=name)
        s.add(v)
        await s.commit()
        await s.refresh(v)
        return str(v.id)


async def _add_invoice(mk, org_id, *, vendor_id=None, number="INV-1", currency="USD") -> str:
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            invoice_number=number,
            vendor_name="Acme Supplies",
            amount=Decimal("500.00"),
            currency=currency,
            status=InvoiceStatus.new,
            vendor_id=vendor_id,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return str(inv.id)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_open_memo(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-001",
                "vendor_id": vendor_id,
                "amount": "123.45",
                "currency": "USD",
                "reason": "Returned goods",
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["memo_number"] == "CM-001"
    assert body["vendor_id"] == vendor_id
    assert body["vendor_name"] == "Acme Supplies"
    # Open memo — no invoice link, no application metadata yet.
    assert body["status"] == "open"
    assert body["invoice_id"] is None
    assert body["applied_at"] is None
    assert body["applied_by"] is None
    # Decimal round-trips exactly through Numeric(15, 2).
    assert body["amount"] == 123.45

    # Persisted amount is an exact Decimal, not a lossy float.
    async with mk() as s:
        memo = (await s.execute(select(CreditMemo))).scalar_one()
        assert memo.amount == Decimal("123.45")
        assert memo.status == "open"
        assert memo.organization_id == org_id


async def test_create_applied_at_creation_with_invoice(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-100")

    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-002",
                "vendor_id": vendor_id,
                "amount": "50.00",
                "invoice_id": invoice_id,
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Linking an invoice at creation flips the memo straight to 'applied'.
    assert body["status"] == "applied"
    assert body["invoice_id"] == invoice_id
    assert body["invoice_number"] == "INV-100"
    assert body["applied_at"] is not None
    assert body["applied_by"] == "admin"  # seeded user's full_name == role name


async def test_create_unknown_vendor_404(realdb):
    import uuid

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-X",
                "vendor_id": str(uuid.uuid4()),
                "amount": "10.00",
            },
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Vendor not found"


async def test_create_unknown_invoice_404(realdb):
    import uuid

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-Y",
                "vendor_id": vendor_id,
                "amount": "10.00",
                "invoice_id": str(uuid.uuid4()),
            },
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Invoice not found"


async def test_create_missing_required_field_422(realdb):
    # vendor_id omitted.
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={"memo_number": "CM-Z", "amount": "10.00"},
        )
    assert resp.status_code == 422


async def test_create_non_positive_amount_422(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    # amount has Field(gt=0): zero and negative must be rejected before any DB hit.
    async with realdb.client(key="a", role="ap_manager") as c:
        for bad in ("0", "-5.00"):
            resp = await c.post(
                "/api/credit-memos",
                json={"memo_number": "CM-NEG", "vendor_id": vendor_id, "amount": bad},
            )
            assert resp.status_code == 422, bad


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_empty(realdb):
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get("/api/credit-memos")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == [] and body["total"] == 0


async def test_list_returns_memos_with_join_fields(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Globex")
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-LIST")

    async with realdb.client(key="a", role="ap_manager") as c:
        await c.post(
            "/api/credit-memos",
            json={"memo_number": "CM-L1", "vendor_id": vendor_id, "amount": "10.00"},
        )
        await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-L2",
                "vendor_id": vendor_id,
                "amount": "20.00",
                "invoice_id": invoice_id,
            },
        )
        resp = await c.get("/api/credit-memos")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    # The outer join exposes vendor_name on every row, invoice_number when linked.
    by_number = {m["memo_number"]: m for m in body["items"]}
    assert by_number["CM-L1"]["vendor_name"] == "Globex"
    assert by_number["CM-L1"]["invoice_number"] is None
    assert by_number["CM-L2"]["invoice_number"] == "INV-LIST"


async def test_list_status_filter(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-F")

    async with realdb.client(key="a", role="ap_manager") as c:
        await c.post(
            "/api/credit-memos",
            json={"memo_number": "CM-OPEN", "vendor_id": vendor_id, "amount": "10.00"},
        )
        await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-APPLIED",
                "vendor_id": vendor_id,
                "amount": "20.00",
                "invoice_id": invoice_id,
            },
        )
        open_resp = await c.get("/api/credit-memos", params={"status": "open"})
        applied_resp = await c.get("/api/credit-memos", params={"status": "applied"})

    assert open_resp.json()["total"] == 1
    assert open_resp.json()["items"][0]["memo_number"] == "CM-OPEN"
    assert applied_resp.json()["total"] == 1
    assert applied_resp.json()["items"][0]["memo_number"] == "CM-APPLIED"


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


async def _create_open_memo(c, vendor_id, *, number="CM-A", amount="100.00") -> str:
    resp = await c.post(
        "/api/credit-memos",
        json={"memo_number": number, "vendor_id": vendor_id, "amount": amount},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_apply_open_memo_to_invoice(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-APPLY")

    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id)
        resp = await c.post(
            f"/api/credit-memos/{memo_id}/apply",
            json={"invoice_id": invoice_id},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "applied"
    assert body["invoice_id"] == invoice_id
    assert body["invoice_number"] == "INV-APPLY"
    assert body["applied_at"] is not None
    assert body["applied_by"] == "ap_manager"

    async with mk() as s:
        memo = (await s.execute(select(CreditMemo))).scalar_one()
        assert memo.status == "applied"
        assert str(memo.invoice_id) == invoice_id


async def test_apply_memo_not_found_404(realdb):
    import uuid

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    invoice_id = await _add_invoice(mk, org_id, number="INV-NF")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            f"/api/credit-memos/{uuid.uuid4()}/apply",
            json={"invoice_id": invoice_id},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Credit memo not found"


async def test_apply_invoice_not_found_404(realdb):
    import uuid

    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id)
        resp = await c.post(
            f"/api/credit-memos/{memo_id}/apply",
            json={"invoice_id": str(uuid.uuid4())},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Invoice not found"


async def test_apply_already_applied_409(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-APPLIED")

    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id)
        first = await c.post(f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": invoice_id})
        assert first.status_code == 200
        # Re-applying an already-applied memo is a conflict, not a re-write.
        second = await c.post(f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": invoice_id})
    assert second.status_code == 409
    assert "applied" in second.json()["detail"]


async def test_apply_vendor_mismatch_409(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    memo_vendor = await _add_vendor(mk, org_id, name="Memo Vendor")
    other_vendor = await _add_vendor(mk, org_id, name="Other Vendor")
    # Invoice belongs to a *different* vendor than the memo.
    invoice_id = await _add_invoice(mk, org_id, vendor_id=other_vendor, number="INV-MM")

    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, memo_vendor)
        resp = await c.post(
            f"/api/credit-memos/{memo_id}/apply",
            json={"invoice_id": invoice_id},
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Credit memo vendor does not match invoice vendor"


async def test_apply_currency_mismatch_409(realdb):
    """A EUR memo can't be applied to a USD invoice — the remaining-balance math
    subtracts the amounts directly, so mixed currencies would corrupt it."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="FX Vendor")
    # _add_invoice leaves currency at the USD default.
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-FX")

    async with realdb.client(key="a", role="ap_manager") as c:
        memo = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-EUR",
                "vendor_id": vendor_id,
                "amount": "50.00",
                "currency": "EUR",
            },
        )
        assert memo.status_code == 201, memo.text
        resp = await c.post(
            f"/api/credit-memos/{memo.json()['id']}/apply",
            json={"invoice_id": invoice_id},
        )
    assert resp.status_code == 409, resp.text
    assert "currency" in resp.json()["detail"].lower()


async def test_create_with_invoice_currency_mismatch_409(realdb):
    """Creating a memo directly against an invoice (invoice_id at create) applies
    it immediately — so the same currency guard as /apply must reject a EUR memo
    against a USD invoice, or the remaining-balance math mixes currencies."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="FX Create Vendor")
    # _add_invoice leaves currency at the USD default.
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-FXC")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-EURC",
                "vendor_id": vendor_id,
                "invoice_id": invoice_id,
                "amount": "50.00",
                "currency": "EUR",
            },
        )
    assert resp.status_code == 409, resp.text
    assert "currency" in resp.json()["detail"].lower()


async def test_apply_invoice_without_vendor_refused(realdb):
    """An invoice with no resolved vendor cannot be credited — fail-closed.

    A NULL ``vendor_id`` does not mean "any vendor"; it means the invoice's
    vendor cannot be established, so there is nothing to prove the memo's
    vendor against. The old guard (``if invoice.vendor_id and ...``) skipped
    entirely on NULL, which let one vendor's credit reduce another vendor's
    balance on every invoice created without extraction.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=None, number="INV-NOVEN")

    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id)
        resp = await c.post(
            f"/api/credit-memos/{memo_id}/apply",
            json={"invoice_id": invoice_id},
        )
    assert resp.status_code == 409, resp.text
    assert "no linked vendor" in resp.json()["detail"]

    # The memo stayed open — nothing was credited.
    async with mk() as s:
        memo = (await s.execute(select(CreditMemo))).scalar_one()
        assert memo.status == "open"
        assert memo.invoice_id is None


async def test_apply_manually_created_invoice_of_other_vendor_refused(realdb):
    """Issue #138, verbatim: vendor A's memo against a MANUALLY-entered
    vendor-B invoice must be refused.

    ``POST /api/invoices`` is the no-OCR manual-entry path; it used to leave
    ``vendor_id`` NULL, so the vendor guard never fired and the credit landed
    on the wrong vendor's balance. The invoice now resolves its vendor link on
    create, so the mismatch is caught.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_a = await _add_vendor(mk, org_id, name="Vendor Alpha")
    await _add_vendor(mk, org_id, name="Vendor Beta")

    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post(
            "/api/invoices",
            json={
                "vendor": "Vendor Beta",
                "invoice_number": "INV-MANUAL-138",
                "amount": "500.00",
                "currency": "USD",
            },
        )
        assert created.status_code == 201, created.text
        # Manual entry resolves the vendor link — that is what makes the guard
        # able to fire at all.
        assert created.json()["vendor_id"] is not None
        invoice_id = created.json()["id"]

        memo_id = await _create_open_memo(c, vendor_a, number="CM-138")
        resp = await c.post(
            f"/api/credit-memos/{memo_id}/apply",
            json={"invoice_id": invoice_id},
        )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Credit memo vendor does not match invoice vendor"

    async with mk() as s:
        memo = (await s.execute(select(CreditMemo))).scalar_one()
        assert memo.status == "open"


async def test_apply_manually_created_invoice_same_vendor_allowed(realdb):
    """The other half: the memo's OWN vendor's manually-keyed invoice still
    takes the credit — the fix closes the hole without stranding the flow."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Vendor Alpha")

    async with realdb.client(key="a", role="ap_manager") as c:
        created = await c.post(
            "/api/invoices",
            json={
                "vendor": "Vendor Alpha",
                "invoice_number": "INV-MANUAL-OK",
                "amount": "500.00",
                "currency": "USD",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["vendor_id"] == vendor_id
        invoice_id = created.json()["id"]

        memo_id = await _create_open_memo(c, vendor_id, number="CM-OK")
        resp = await c.post(
            f"/api/credit-memos/{memo_id}/apply",
            json={"invoice_id": invoice_id},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"


async def test_create_applied_memo_against_unlinked_invoice_refused(realdb):
    """The create-with-invoice_id path applies a credit too, so it carries the
    same fail-closed guard — an unlinked invoice is refused there as well."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=None, number="INV-CR-NOVEN")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-CR-NOVEN",
                "vendor_id": vendor_id,
                "amount": "50.00",
                "invoice_id": invoice_id,
            },
        )
    assert resp.status_code == 409, resp.text
    assert "no linked vendor" in resp.json()["detail"]

    # Nothing was persisted — the guard runs before the memo row is added.
    async with mk() as s:
        assert (await s.execute(select(func.count()).select_from(CreditMemo))).scalar_one() == 0


async def test_resaving_vendor_resolves_a_legacy_unlinked_invoice(realdb):
    """No backfill migration: an invoice that predates the create-time vendor
    resolution is un-creditable until a human re-saves its vendor, which
    re-runs the matcher and links it. That is the supported recovery path."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Acme Supplies")
    # Legacy shape: vendor_name set, vendor_id NULL.
    invoice_id = await _add_invoice(mk, org_id, vendor_id=None, number="INV-LEGACY")

    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id, number="CM-LEGACY")
        blocked = await c.post(
            f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": invoice_id}
        )
        assert blocked.status_code == 409, blocked.text

        # Re-save the vendor name — unchanged text, but the link was missing,
        # so the matcher runs and resolves it.
        patched = await c.patch(f"/api/invoices/{invoice_id}", json={"vendor": "Acme Supplies"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["vendor_id"] == vendor_id

        allowed = await c.post(
            f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": invoice_id}
        )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "applied"


async def test_clearing_the_vendor_name_clears_the_link_and_blocks_the_credit(realdb):
    """Blanking the vendor name must drop the link, not orphan it.

    ``match_and_link_vendor`` no-ops on an empty name, so without an explicit
    clear a nameless invoice would keep pointing at its old vendor — a link
    nothing visible corroborates, which the credit guard would still accept."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Acme Supplies")
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-CLEARED")

    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id, number="CM-CLEARED")
        patched = await c.patch(f"/api/invoices/{invoice_id}", json={"vendor": ""})
        assert patched.status_code == 200, patched.text
        assert patched.json()["vendor_id"] is None

        resp = await c.post(f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": invoice_id})
    assert resp.status_code == 409, resp.text
    assert "no linked vendor" in resp.json()["detail"]


async def test_renaming_the_vendor_relinks_and_blocks_the_stale_memo(realdb):
    """A rename must move the LINK too. Otherwise vendor A's memo would still
    apply to an invoice that now names vendor B (the guard compares
    ``vendor_id``, not the free-text name)."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_a = await _add_vendor(mk, org_id, name="Vendor Alpha")
    vendor_b = await _add_vendor(mk, org_id, name="Vendor Beta")
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_a, number="INV-RENAME")

    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_a, number="CM-RENAME")
        patched = await c.patch(f"/api/invoices/{invoice_id}", json={"vendor": "Vendor Beta"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["vendor_id"] == vendor_b

        resp = await c.post(f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": invoice_id})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Credit memo vendor does not match invoice vendor"


# ---------------------------------------------------------------------------
# void
# ---------------------------------------------------------------------------


async def test_void_open_memo(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)

    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id)
        resp = await c.post(f"/api/credit-memos/{memo_id}/void")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "void"

    async with mk() as s:
        memo = (await s.execute(select(CreditMemo))).scalar_one()
        assert memo.status == "void"


async def test_void_applied_memo_409(realdb):
    # Applied credit memos are immutable for audit — voiding them is blocked.
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-VA")

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-VA",
                "vendor_id": vendor_id,
                "amount": "10.00",
                "invoice_id": invoice_id,
            },
        )
        memo_id = resp.json()["id"]
        void_resp = await c.post(f"/api/credit-memos/{memo_id}/void")
    assert void_resp.status_code == 409
    assert "Applied" in void_resp.json()["detail"]


async def test_void_not_found_404(realdb):
    import uuid

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(f"/api/credit-memos/{uuid.uuid4()}/void")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Credit memo not found"


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


async def test_list_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.get("/api/credit-memos")
    assert resp.status_code == 401


async def test_create_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        resp = await c.post(
            "/api/credit-memos",
            json={"memo_number": "CM", "vendor_id": "x", "amount": "1.00"},
        )
    assert resp.status_code == 401


async def test_list_allows_cfo_and_clerk(realdb):
    # list permits admin/ap_manager/ap_clerk/cfo.
    for role in ("cfo", "ap_clerk"):
        async with realdb.client(key="a", role=role) as c:
            resp = await c.get("/api/credit-memos")
        assert resp.status_code == 200, role


async def test_create_forbidden_for_clerk(realdb):
    # create is admin/ap_manager only — ap_clerk is denied.
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={"memo_number": "CM-CLERK", "vendor_id": vendor_id, "amount": "10.00"},
        )
    assert resp.status_code == 403


async def test_create_forbidden_for_cfo(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={"memo_number": "CM-CFO", "vendor_id": vendor_id, "amount": "10.00"},
        )
    assert resp.status_code == 403


async def test_apply_forbidden_for_clerk(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-CLERK")
    async with realdb.client(key="a", role="ap_manager") as mgr:
        memo_id = await _create_open_memo(mgr, vendor_id)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(
            f"/api/credit-memos/{memo_id}/apply",
            json={"invoice_id": invoice_id},
        )
    assert resp.status_code == 403


async def test_void_forbidden_for_clerk(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    async with realdb.client(key="a", role="ap_manager") as mgr:
        memo_id = await _create_open_memo(mgr, vendor_id)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.post(f"/api/credit-memos/{memo_id}/void")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# tenant isolation
# ---------------------------------------------------------------------------


async def test_tenant_isolation_list(realdb):
    # A memo created under tenant 'a' must not appear when tenant 'b' lists.
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    vendor_a = await _add_vendor(mk_a, org_a)
    async with realdb.client(key="a", role="ap_manager") as c:
        await _create_open_memo(c, vendor_a, number="CM-A-ONLY")

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.get("/api/credit-memos")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0

    # And tenant b's DB physically has no rows.
    mk_b = realdb.sessionmaker("b")
    async with mk_b() as s:
        count = (await s.execute(select(func.count()).select_from(CreditMemo))).scalar()
        assert count == 0


async def test_tenant_isolation_apply_cross_tenant(realdb):
    # A memo in tenant 'a' is invisible to tenant 'b' — apply returns 404.
    import uuid

    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    vendor_a = await _add_vendor(mk_a, org_a)
    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_a)

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.post(
            f"/api/credit-memos/{memo_id}/apply",
            json={"invoice_id": str(uuid.uuid4())},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Credit memo not found"


async def test_tenant_isolation_void_cross_tenant(realdb):
    mk_a = realdb.sessionmaker("a")
    org_a = realdb.info("a").org_id
    vendor_a = await _add_vendor(mk_a, org_a)
    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_a)

    async with realdb.client(key="b", role="ap_manager") as c:
        resp = await c.post(f"/api/credit-memos/{memo_id}/void")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# entity scoping
# ---------------------------------------------------------------------------


async def _entities(client, *, name: str, slug: str) -> tuple[str, str]:
    """Create a second entity; return (default_entity_id, new_entity_id)."""
    r = await client.post("/api/entities", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    other_id = r.json()["id"]
    listing = await client.get("/api/entities")
    default_id = next(e["id"] for e in listing.json() if e["is_default"])
    return default_id, other_id


async def _seed_scoped_vendor_invoice(mk, org_id, *, entity_id, number: str) -> tuple[str, str]:
    import uuid as _uuid

    async with mk() as s:
        v = Vendor(
            organization_id=org_id, entity_id=_uuid.UUID(entity_id), name=f"CM Scope {number}"
        )
        s.add(v)
        await s.flush()
        inv = Invoice(
            organization_id=org_id,
            entity_id=_uuid.UUID(entity_id),
            invoice_number=number,
            vendor_name=v.name,
            vendor_id=v.id,
            amount=Decimal("1000.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(v)
        await s.refresh(inv)
        return str(v.id), str(inv.id)


async def test_credit_memo_mutations_are_entity_scoped(realdb):
    """Applying a credit reduces what a payment run pays, so naming another
    subsidiary's ids must not reach across the entity boundary.

    `list_credit_memos` honoured `X-Entity-ID` from the start, but every by-id
    mutation resolved on the primary key alone — so an entity-A user could
    create, apply or void a memo against entity B's invoice, cutting B's next
    payment, and then not even see the memo in their own list. Opaque 404 on
    every path, mirroring `api/payments.py::_get_scoped_payment`.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id

    async with realdb.client(key="a", role="admin") as c:
        default_id, other_id = await _entities(c, name="CM Sub", slug="cm-sub")

    b_vendor, b_invoice = await _seed_scoped_vendor_invoice(
        mk, org_id, entity_id=other_id, number="CMSCOPE-B-1"
    )

    async with realdb.client(key="a", role="admin") as c:
        # Entity A selected: entity B's vendor is not reachable.
        c.headers["X-Entity-ID"] = default_id
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-SCOPE-1",
                "vendor_id": b_vendor,
                "amount": "400.00",
                "invoice_id": b_invoice,
            },
        )
        assert resp.status_code == 404, resp.text

        # Create the memo properly from entity B, then try to reach it from A.
        c.headers["X-Entity-ID"] = other_id
        made = await c.post(
            "/api/credit-memos",
            json={"memo_number": "CM-SCOPE-2", "vendor_id": b_vendor, "amount": "100.00"},
        )
        assert made.status_code == 201, made.text
        memo_id = made.json()["id"]

        c.headers["X-Entity-ID"] = default_id
        applied = await c.post(f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": b_invoice})
        assert applied.status_code == 404, applied.text
        voided = await c.post(f"/api/credit-memos/{memo_id}/void")
        assert voided.status_code == 404, voided.text

    # Nothing was applied against entity B's invoice.
    async with mk() as s:
        total = (
            await s.execute(
                select(func.coalesce(func.sum(CreditMemo.amount), Decimal("0"))).where(
                    CreditMemo.status == "applied"
                )
            )
        ).scalar_one()
        assert total == Decimal("0")


# ---------------------------------------------------------------------------
# Currency resolution on create — a non-USD tenant must not be dead-ended
# ---------------------------------------------------------------------------


async def _set_org_reporting_currency(realdb, code: str | None):
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.organization import Organization

    org_id = realdb.info("a").org_id
    ctrl = realdb.control_sessionmaker()
    async with ctrl() as s:
        org = (await s.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        cfg = dict(org.settings or {})
        if code is None:
            cfg.pop("reporting_currency", None)
        else:
            cfg["reporting_currency"] = code
        org.settings = cfg
        flag_modified(org, "settings")
        await s.commit()


async def test_create_without_currency_inherits_the_invoice_currency(realdb):
    """A memo created against a named invoice takes THAT invoice's currency.

    The schema used to default `currency` to "USD", so a EUR tenant's memo was
    stamped USD and then 409'd by the very currency guard on the same request —
    and with no PATCH on credit memos, the memo could never be applied or
    corrected. The memo now inherits rather than asserting.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Euro Supplies")
    invoice_id = await _add_invoice(
        mk, org_id, vendor_id=vendor_id, number="INV-EUR-1", currency="EUR"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-EUR-INHERIT",
                "vendor_id": vendor_id,
                "amount": "100.00",
                "invoice_id": invoice_id,
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["currency"] == "EUR"
    # And it actually applied — the guard it used to trip is satisfied.
    assert body["status"] == "applied"
    assert body["invoice_id"] == invoice_id


async def test_create_without_currency_falls_back_to_org_reporting_currency(realdb):
    """An unlinked memo takes the ORG's reporting currency, not a hardcoded USD.

    A single-currency EUR tenant should never have to name a currency, and must
    never be handed a USD memo that its own invoices refuse.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Reporting Currency Co")

    await _set_org_reporting_currency(realdb, "EUR")
    try:
        async with realdb.client(key="a", role="ap_manager") as c:
            resp = await c.post(
                "/api/credit-memos",
                json={
                    "memo_number": "CM-EUR-ORG",
                    "vendor_id": vendor_id,
                    "amount": "42.00",
                },
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["currency"] == "EUR"

        async with mk() as s:
            memo = (
                await s.execute(select(CreditMemo).where(CreditMemo.memo_number == "CM-EUR-ORG"))
            ).scalar_one()
            assert memo.currency == "EUR"
            assert memo.amount == Decimal("42.00")  # money stays exact
    finally:
        await _set_org_reporting_currency(realdb, None)


async def test_explicit_currency_still_wins_and_still_guards(realdb):
    """An explicitly asserted currency is still checked against the invoice —
    inheriting must not become a way to silently reconcile a real mismatch."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id, name="Mismatch Co")
    invoice_id = await _add_invoice(
        mk, org_id, vendor_id=vendor_id, number="INV-GBP-1", currency="GBP"
    )

    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-MISMATCH",
                "vendor_id": vendor_id,
                "amount": "10.00",
                "currency": "EUR",
                "invoice_id": invoice_id,
            },
        )
    assert resp.status_code == 409, resp.text
    assert "currency" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# search + sort (issue #443) — a server-side leg, never a page-local filter
# ---------------------------------------------------------------------------


async def _seed_search_set(realdb) -> None:
    """Five memos across two vendors: three open, one applied, one void."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    globex = await _add_vendor(mk, org_id, name="Globex Corporation")
    initech = await _add_vendor(mk, org_id, name="Initech")
    inv = await _add_invoice(mk, org_id, vendor_id=globex, number="INV-SRCH")
    async with realdb.client(key="a", role="ap_manager") as c:
        for number, vendor, amount, issued in (
            ("CM-300", globex, "30.00", "2026-03-01"),
            ("CM-100", initech, "10.00", "2026-01-01"),
            ("RTN_50%", globex, "20.00", "2026-02-01"),
        ):
            r = await c.post(
                "/api/credit-memos",
                json={
                    "memo_number": number,
                    "vendor_id": vendor,
                    "amount": amount,
                    "issued_date": issued,
                },
            )
            assert r.status_code == 201, r.text
        applied = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-APP",
                "vendor_id": globex,
                "amount": "5.00",
                "invoice_id": inv,
            },
        )
        assert applied.status_code == 201, applied.text
        void_id = await _create_open_memo(c, initech, number="CM-VOIDED", amount="7.00")
        assert (await c.post(f"/api/credit-memos/{void_id}/void")).status_code == 200


async def test_list_search_matches_memo_number_and_vendor_name(realdb):
    await _seed_search_set(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        by_vendor = (await c.get("/api/credit-memos", params={"search": "globex"})).json()
        by_number = (await c.get("/api/credit-memos", params={"search": "cm-1"})).json()
        # LIKE metacharacters are literal text, not wildcards: `%` must not
        # match every row, `_` must not match any single character.
        literal = (await c.get("/api/credit-memos", params={"search": "_50%"})).json()
        underscore = (await c.get("/api/credit-memos", params={"search": "CM_"})).json()
        blank = (await c.get("/api/credit-memos", params={"search": "   "})).json()

    assert {m["memo_number"] for m in by_vendor["items"]} == {"CM-300", "RTN_50%", "CM-APP"}
    assert by_vendor["total"] == 3
    assert [m["memo_number"] for m in by_number["items"]] == ["CM-100"]
    assert [m["memo_number"] for m in literal["items"]] == ["RTN_50%"]
    assert underscore["total"] == 0
    # A whitespace-only term is no filter at all, not "match nothing".
    assert blank["total"] == 5


async def test_list_search_composes_with_the_status_filter(realdb):
    await _seed_search_set(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.get("/api/credit-memos", params={"search": "globex", "status": "open"})
    body = resp.json()
    assert {m["memo_number"] for m in body["items"]} == {"CM-300", "RTN_50%"}
    assert body["total"] == 2


async def test_list_sort_allowlist(realdb):
    await _seed_search_set(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:

        async def numbers(**params) -> list[str]:
            r = await c.get("/api/credit-memos", params={"status": "open", **params})
            assert r.status_code == 200, r.text
            return [m["memo_number"] for m in r.json()["items"]]

        assert await numbers(sort="amount", order="asc") == ["CM-100", "RTN_50%", "CM-300"]
        assert await numbers(sort="amount", order="desc") == ["CM-300", "RTN_50%", "CM-100"]
        assert await numbers(sort="issued_date", order="asc") == ["CM-100", "RTN_50%", "CM-300"]
        assert await numbers(sort="memo_number", order="asc") == ["CM-100", "CM-300", "RTN_50%"]

        # Anything outside the allowlist is refused, never silently ignored —
        # including real columns that are simply not offered for sorting.
        for bad in ("vendor_id", "created_at", "amount; DROP TABLE credit_memos"):
            r = await c.get("/api/credit-memos", params={"sort": bad})
            assert r.status_code == 422, bad
            assert "issued_date" in r.json()["detail"], bad
        assert (await c.get("/api/credit-memos", params={"order": "sideways"})).status_code == 422


# ---------------------------------------------------------------------------
# GET /credit-memos/summary — the chip counts
# ---------------------------------------------------------------------------


async def test_summary_counts_every_status_over_the_whole_set(realdb):
    await _seed_search_set(realdb)
    async with realdb.client(key="a", role="ap_clerk") as c:
        resp = await c.get("/api/credit-memos/summary")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"total": 5, "by_status": {"open": 3, "applied": 1, "void": 1}}


async def test_summary_zero_fills_every_known_status(realdb):
    async with realdb.client(key="a", role="cfo") as c:
        resp = await c.get("/api/credit-memos/summary")
    assert resp.json() == {"total": 0, "by_status": {"open": 0, "applied": 0, "void": 0}}


async def test_summary_describes_exactly_the_rows_the_list_returns(realdb):
    """Each chip's count must equal the list's total for that chip under the
    same search — they come from one filter builder, and this is the pin."""
    await _seed_search_set(realdb)
    async with realdb.client(key="a", role="ap_manager") as c:
        for term in (None, "globex", "initech", "cm-", "nothing-matches"):
            params = {"search": term} if term else {}
            summary = (await c.get("/api/credit-memos/summary", params=params)).json()
            listed_all = (await c.get("/api/credit-memos", params=params)).json()["total"]
            assert summary["total"] == listed_all, term
            for status_key, count in summary["by_status"].items():
                listed = (
                    await c.get("/api/credit-memos", params={**params, "status": status_key})
                ).json()["total"]
                assert count == listed, (term, status_key)


async def test_summary_is_entity_scoped(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="admin") as c:
        default_id, other_id = await _entities(c, name="CM Sum Sub", slug="cm-sum-sub")
    b_vendor, _ = await _seed_scoped_vendor_invoice(
        mk, org_id, entity_id=other_id, number="CMSUM-B-1"
    )
    a_vendor, _ = await _seed_scoped_vendor_invoice(
        mk, org_id, entity_id=default_id, number="CMSUM-A-1"
    )
    async with realdb.client(key="a", role="admin") as c:
        c.headers["X-Entity-ID"] = other_id
        await _create_open_memo(c, b_vendor, number="CM-B")
        c.headers["X-Entity-ID"] = default_id
        await _create_open_memo(c, a_vendor, number="CM-A")

        scoped = (await c.get("/api/credit-memos/summary")).json()
        c.headers.pop("X-Entity-ID")
        consolidated = (await c.get("/api/credit-memos/summary")).json()

    assert scoped == {"total": 1, "by_status": {"open": 1, "applied": 0, "void": 0}}
    assert consolidated["total"] == 2


async def test_summary_rbac_matches_the_list(realdb):
    async with realdb.client(key="a", role=None) as c:
        assert (await c.get("/api/credit-memos/summary")).status_code == 401
    for role in ("admin", "ap_manager", "ap_clerk", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            assert (await c.get("/api/credit-memos/summary")).status_code == 200, role
