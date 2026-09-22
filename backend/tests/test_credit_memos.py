"""Real-DB coverage for the credit-memos router.

Covers ``backend/app/api/credit_memos.py`` end-to-end against two live test
tenants: list/get (incl. search + the sort allowlist), the per-status summary
behind the filter chips, create (open + applied-at-creation), edit of an open
memo, apply-to-invoice, void, the 409 lifecycle guards, the row lock edit and
apply serialize on, RBAC, tenant isolation, and the Decimal money math
(amounts are ``Numeric(15, 2)`` and must round-trip exactly).
"""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, text, update

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


# ---------------------------------------------------------------------------
# create: request validation + the entity guard on both application paths
# ---------------------------------------------------------------------------


async def test_malformed_ids_are_422_not_500(realdb):
    """`vendor_id` / `invoice_id` were `str` and parsed by hand with
    `uuid.UUID(...)`, so a malformed id raised an unhandled ValueError — a 500."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    async with realdb.client(key="a", role="ap_manager") as c:
        bad_vendor = await c.post(
            "/api/credit-memos",
            json={"memo_number": "CM-BAD", "vendor_id": "not-a-uuid", "amount": "1.00"},
        )
        bad_invoice = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-BAD",
                "vendor_id": vendor_id,
                "amount": "1.00",
                "invoice_id": "not-a-uuid",
            },
        )
        memo_id = await _create_open_memo(c, vendor_id)
        bad_apply = await c.post(
            f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": "not-a-uuid"}
        )
    assert bad_vendor.status_code == 422
    assert bad_invoice.status_code == 422
    assert bad_apply.status_code == 422


async def test_create_currency_must_be_an_iso_shape(realdb):
    """A code no invoice can carry makes a memo that is unappliable from birth."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    base = {"memo_number": "CM-C", "vendor_id": vendor_id, "amount": "1"}
    async with realdb.client(key="a", role="ap_manager") as c:
        for bad in ("US", "EURO", "12$"):
            r = await c.post("/api/credit-memos", json={**base, "currency": bad})
            assert r.status_code == 422, bad
        ok = await c.post("/api/credit-memos", json={**base, "currency": " eur "})
        blank_number = await c.post("/api/credit-memos", json={**base, "memo_number": "   "})
    assert ok.status_code == 201, ok.text
    assert ok.json()["currency"] == "EUR"
    assert blank_number.status_code == 422


async def test_currency_guard_ignores_case_on_the_invoice_side(realdb):
    """The invoice schemas never normalised case, so a legacy `eur` invoice is
    the same currency as a `EUR` memo — refusing it would be a false mismatch."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(
        mk, org_id, vendor_id=vendor_id, number="INV-LC", currency="eur"
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-LC",
                "vendor_id": vendor_id,
                "amount": "10.00",
                "currency": "EUR",
                "invoice_id": invoice_id,
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "applied"


async def _cross_entity_pair(realdb) -> tuple[str, str, str]:
    """A vendor stamped in entity B whose id an entity-A invoice carries.

    `vendor_matching` never produces this link, so it is forced — the guard is
    for the drift case under the consolidated view, where no header confines
    either side, not for a path the app takes today. Returns
    ``(b_vendor_id, a_invoice_id, b_entity_id)``.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="admin") as c:
        default_id, other_id = await _entities(c, name="CM X Sub", slug="cm-x-sub")
    b_vendor, _ = await _seed_scoped_vendor_invoice(mk, org_id, entity_id=other_id, number="X-B")
    async with mk() as s:
        inv = Invoice(
            organization_id=org_id,
            entity_id=uuid.UUID(default_id),
            invoice_number="X-A-1",
            vendor_name="CM Scope X-B",
            vendor_id=uuid.UUID(b_vendor),
            amount=Decimal("500.00"),
            currency="USD",
            status=InvoiceStatus.approved,
        )
        s.add(inv)
        await s.commit()
        await s.refresh(inv)
        return b_vendor, str(inv.id), other_id


async def test_create_refuses_to_credit_another_entitys_invoice(realdb):
    b_vendor, a_invoice, _ = await _cross_entity_pair(realdb)
    async with realdb.client(key="a", role="admin") as c:  # consolidated view
        resp = await c.post(
            "/api/credit-memos",
            json={
                "memo_number": "CM-X",
                "vendor_id": b_vendor,
                "amount": "10.00",
                "invoice_id": a_invoice,
            },
        )
    assert resp.status_code == 409, resp.text
    assert "entities" in resp.json()["detail"]
    async with realdb.sessionmaker("a")() as s:
        assert (await s.execute(select(func.count()).select_from(CreditMemo))).scalar() == 0


async def test_apply_refuses_to_credit_another_entitys_invoice(realdb):
    b_vendor, a_invoice, other_id = await _cross_entity_pair(realdb)
    async with realdb.client(key="a", role="admin") as c:
        c.headers["X-Entity-ID"] = other_id
        memo_id = await _create_open_memo(c, b_vendor, number="CM-X-OPEN")
        c.headers.pop("X-Entity-ID")  # consolidated: no header confines the invoice
        resp = await c.post(f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": a_invoice})
    assert resp.status_code == 409, resp.text
    assert "entities" in resp.json()["detail"]
    async with realdb.sessionmaker("a")() as s:
        memo = await s.get(CreditMemo, uuid.UUID(memo_id))
        assert memo.status == "open" and memo.invoice_id is None


# ---------------------------------------------------------------------------
# PATCH /credit-memos/{id} — correct a mis-keyed OPEN memo, never a settled one
# ---------------------------------------------------------------------------


async def _audit_rows(mk, memo_id: str, action: str) -> list[dict]:
    from app.models.workflow import AuditLog

    async with mk() as s:
        rows = (
            await s.execute(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "credit_memo",
                    AuditLog.entity_id == uuid.UUID(memo_id),
                    AuditLog.action == action,
                )
                .order_by(AuditLog.created_at)
            )
        ).scalars()
        return [{"actor_id": r.actor_id, "details": r.details} for r in rows]


async def test_patch_open_memo_rewrites_fields_and_audits_before_after(realdb):
    mk = realdb.sessionmaker("a")
    info = realdb.info("a")
    vendor_id = await _add_vendor(mk, info.org_id)
    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id, number="CM-TYPO", amount="100.00")
        resp = await c.patch(
            f"/api/credit-memos/{memo_id}",
            json={
                "memo_number": " CM-FIXED ",
                "amount": "150.25",
                "currency": "eur",
                "issued_date": "2026-09-01",
                "reason": "Short shipment",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["memo_number"] == "CM-FIXED"
    assert body["amount"] == 150.25
    assert body["currency"] == "EUR"
    assert body["issued_date"] == "2026-09-01"
    assert body["reason"] == "Short shipment"
    assert body["status"] == "open"
    assert body["vendor_name"] == "Acme Supplies"

    async with mk() as s:
        memo = await s.get(CreditMemo, uuid.UUID(memo_id))
        assert memo.amount == Decimal("150.25")  # exact through Numeric(15, 2)
        assert memo.currency == "EUR"

    rows = await _audit_rows(mk, memo_id, "credit_memo.updated")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == info.users["ap_manager"]
    changes = rows[0]["details"]["changes"]
    assert changes["memo_number"] == {"old": "CM-TYPO", "new": "CM-FIXED"}
    # string-Decimal on both sides, never a float
    assert changes["amount"] == {"old": "100.00", "new": "150.25"}
    assert changes["currency"] == {"old": "USD", "new": "EUR"}
    assert changes["issued_date"] == {"old": None, "new": "2026-09-01"}
    assert changes["reason"] == {"old": None, "new": "Short shipment"}
    # Unchanged fields are not restated.
    assert "vendor_id" not in changes and "entity_id" not in changes


async def test_patch_fixes_the_wrong_currency_dead_end(realdb):
    """The follow-up's own scenario: a memo keyed in the wrong currency was
    unappliable AND uncorrectable. It is now one PATCH away from applying."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(
        mk, org_id, vendor_id=vendor_id, number="INV-EUR", currency="EUR"
    )
    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id, number="CM-WRONG-CCY")
        apply_url = f"/api/credit-memos/{memo_id}/apply"
        refused = await c.post(apply_url, json={"invoice_id": invoice_id})
        assert refused.status_code == 409
        fixed = await c.patch(f"/api/credit-memos/{memo_id}", json={"currency": "EUR"})
        assert fixed.status_code == 200, fixed.text
        applied = await c.post(apply_url, json={"invoice_id": invoice_id})
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"


async def test_patch_refused_once_applied_and_leaves_the_record_untouched(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-SETTLED")
    async with realdb.client(key="a", role="admin") as c:
        memo_id = await _create_open_memo(c, vendor_id, number="CM-SETTLED", amount="40.00")
        applied = await c.post(
            f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": invoice_id}
        )
        assert applied.status_code == 200, applied.text
        resp = await c.patch(f"/api/credit-memos/{memo_id}", json={"amount": "400.00"})
    assert resp.status_code == 409, resp.text
    assert "applied" in resp.json()["detail"]
    async with mk() as s:
        memo = await s.get(CreditMemo, uuid.UUID(memo_id))
        assert memo.amount == Decimal("40.00")
    assert await _audit_rows(mk, memo_id, "credit_memo.updated") == []


async def test_patch_refused_on_a_voided_memo(realdb):
    mk = realdb.sessionmaker("a")
    vendor_id = await _add_vendor(mk, realdb.info("a").org_id)
    async with realdb.client(key="a", role="admin") as c:
        memo_id = await _create_open_memo(c, vendor_id)
        assert (await c.post(f"/api/credit-memos/{memo_id}/void")).status_code == 200
        resp = await c.patch(f"/api/credit-memos/{memo_id}", json={"reason": "undo"})
    assert resp.status_code == 409
    assert "void" in resp.json()["detail"]


async def test_patch_refused_on_any_trace_of_an_application(realdb):
    """No path writes `open` with an invoice link or an `applied_at` today —
    application is all-or-nothing and nothing reverts it. The guard still
    refuses such a row, so a future "reopen" path can never make a settled
    record editable by flipping the status alone."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-TRACE")
    async with realdb.client(key="a", role="admin") as c:
        linked = await _create_open_memo(c, vendor_id, number="CM-LINKED")
        stamped = await _create_open_memo(c, vendor_id, number="CM-STAMPED")
    async with mk() as s:
        (await s.get(CreditMemo, uuid.UUID(linked))).invoice_id = uuid.UUID(invoice_id)
        (await s.get(CreditMemo, uuid.UUID(stamped))).applied_at = datetime.now(UTC)
        await s.commit()
    async with realdb.client(key="a", role="admin") as c:
        for memo_id in (linked, stamped):
            resp = await c.patch(f"/api/credit-memos/{memo_id}", json={"amount": "1.00"})
            assert resp.status_code == 409, memo_id


async def test_patch_vendor_is_validated_like_create_and_entity_follows_it(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="admin") as c:
        default_id, other_id = await _entities(c, name="CM Patch Sub", slug="cm-patch-sub")
    b_vendor, _ = await _seed_scoped_vendor_invoice(mk, org_id, entity_id=other_id, number="PV-B")
    a_vendor, _ = await _seed_scoped_vendor_invoice(mk, org_id, entity_id=default_id, number="PV-A")
    a_vendor_2, _ = await _seed_scoped_vendor_invoice(
        mk, org_id, entity_id=default_id, number="PV-A2"
    )

    async with realdb.client(key="a", role="admin") as c:
        c.headers["X-Entity-ID"] = default_id
        memo_id = await _create_open_memo(c, a_vendor, number="CM-PV")
        url = f"/api/credit-memos/{memo_id}"
        unknown = await c.patch(url, json={"vendor_id": str(uuid.uuid4())})
        # Another entity's vendor is as unreachable here as it is on create.
        foreign = await c.patch(url, json={"vendor_id": b_vendor})
        ok = await c.patch(url, json={"vendor_id": a_vendor_2})
        # The consolidated view can reach entity B's vendor; the memo follows it.
        c.headers.pop("X-Entity-ID")
        moved = await c.patch(url, json={"vendor_id": b_vendor})

    assert unknown.status_code == 404 and unknown.json()["detail"] == "Vendor not found"
    assert foreign.status_code == 404
    assert ok.status_code == 200 and ok.json()["vendor_name"] == "CM Scope PV-A2"
    assert moved.status_code == 200, moved.text
    async with mk() as s:
        memo = await s.get(CreditMemo, uuid.UUID(memo_id))
        assert str(memo.vendor_id) == b_vendor
        assert str(memo.entity_id) == other_id
    rows = await _audit_rows(mk, memo_id, "credit_memo.updated")
    assert len(rows) == 2
    assert rows[0]["details"]["changes"]["vendor_id"] == {"old": a_vendor, "new": a_vendor_2}
    assert "entity_id" not in rows[0]["details"]["changes"]  # same entity
    assert rows[1]["details"]["changes"]["entity_id"] == {"old": default_id, "new": other_id}


async def test_patch_request_validation(realdb):
    mk = realdb.sessionmaker("a")
    vendor_id = await _add_vendor(mk, realdb.info("a").org_id)
    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id)
        url = f"/api/credit-memos/{memo_id}"
        for bad in (
            {},  # nothing to change
            {"invoice_id": str(uuid.uuid4())},  # linking IS applying — its own endpoint
            {"status": "void"},
            {"memo_number": None},  # the NOT NULL fields cannot be blanked
            {"amount": None},
            {"currency": None},
            {"vendor_id": None},
            {"memo_number": "  "},
            {"amount": "0"},
            {"amount": "-1.00"},
            {"amount": "1.005"},
            {"amount": "10000000000000.00"},
            {"currency": "US"},
            {"vendor_id": "not-a-uuid"},
        ):
            r = await c.patch(url, json=bad)
            assert r.status_code == 422, (bad, r.text)
        # The nullable fields CAN be cleared (they are already empty here, so
        # nothing changes and nothing is audited).
        cleared = await c.patch(url, json={"reason": None, "issued_date": None})
    assert cleared.status_code == 200, cleared.text
    assert await _audit_rows(mk, memo_id, "credit_memo.updated") == []


async def test_patch_that_changes_nothing_writes_no_audit_row(realdb):
    mk = realdb.sessionmaker("a")
    vendor_id = await _add_vendor(mk, realdb.info("a").org_id)
    async with realdb.client(key="a", role="ap_manager") as c:
        memo_id = await _create_open_memo(c, vendor_id, number="CM-SAME", amount="100.00")
        resp = await c.patch(
            f"/api/credit-memos/{memo_id}",
            json={"memo_number": "CM-SAME", "amount": "100", "vendor_id": vendor_id},
        )
    assert resp.status_code == 200, resp.text
    assert await _audit_rows(mk, memo_id, "credit_memo.updated") == []


async def test_patch_rbac_matches_create(realdb):
    mk = realdb.sessionmaker("a")
    vendor_id = await _add_vendor(mk, realdb.info("a").org_id)
    async with realdb.client(key="a", role="admin") as c:
        memo_id = await _create_open_memo(c, vendor_id)
    url = f"/api/credit-memos/{memo_id}"
    async with realdb.client(key="a", role=None) as c:
        assert (await c.patch(url, json={"reason": "x"})).status_code == 401
    for role in ("ap_clerk", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            assert (await c.patch(url, json={"reason": "x"})).status_code == 403, role
    for role in ("admin", "ap_manager"):
        async with realdb.client(key="a", role=role) as c:
            assert (await c.patch(url, json={"reason": role})).status_code == 200, role


async def test_patch_is_entity_and_tenant_scoped(realdb):
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    async with realdb.client(key="a", role="admin") as c:
        default_id, other_id = await _entities(c, name="CM PS Sub", slug="cm-ps-sub")
    b_vendor, _ = await _seed_scoped_vendor_invoice(mk, org_id, entity_id=other_id, number="PS-B")
    async with realdb.client(key="a", role="admin") as c:
        c.headers["X-Entity-ID"] = other_id
        memo_id = await _create_open_memo(c, b_vendor, number="CM-PS", amount="10.00")
        c.headers["X-Entity-ID"] = default_id
        cross_entity = await c.patch(f"/api/credit-memos/{memo_id}", json={"amount": "99.00"})
    async with realdb.client(key="b", role="admin") as c:
        cross_tenant = await c.patch(f"/api/credit-memos/{memo_id}", json={"amount": "99.00"})
    # Opaque 404 on both — an out-of-scope id must not be distinguishable.
    assert cross_entity.status_code == 404
    assert cross_entity.json()["detail"] == "Credit memo not found"
    assert cross_tenant.status_code == 404
    async with mk() as s:
        memo = await s.get(CreditMemo, uuid.UUID(memo_id))
        assert memo.amount == Decimal("10.00")


# ---------------------------------------------------------------------------
# concurrency — edit and apply serialize on the memo row
# ---------------------------------------------------------------------------


async def _wait_for_lock_waiter(mk, *, timeout: float = 15.0) -> bool:
    """True once Postgres reports another backend here waiting on a lock.

    Asked of the server's own wait state from an independent session, never
    slept for — the same signal `test_payment_concurrency.py` waits on. The
    timeout only bounds a failure; the pass path returns as soon as the
    waiter exists.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        async with mk() as s:
            waiting = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND pid <> pg_backend_pid() "
                        "AND wait_event_type = 'Lock'"
                    )
                )
            ).scalar_one()
        if waiting:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_edit_cannot_interleave_with_a_concurrent_apply(realdb):
    """An apply that commits while an edit is in flight must win: the edit then
    sees `applied` and refuses, instead of rewriting a memo that has already
    reduced a payable.

    Without `FOR UPDATE` on the edit's read, the edit read `open` from the last
    committed row, passed its guard, and its UPDATE merely queued behind the
    apply's lock — then overwrote the amount of an APPLIED memo.
    """
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-RACE-E")
    async with realdb.client(key="a", role="admin") as c:
        memo_id = await _create_open_memo(c, vendor_id, number="CM-RACE-E", amount="50.00")
    memo_uuid = uuid.UUID(memo_id)

    async with mk() as held:
        # Stand-in for an apply holding the row, mid-transaction.
        await held.execute(select(CreditMemo).where(CreditMemo.id == memo_uuid).with_for_update())
        await held.execute(
            update(CreditMemo)
            .where(CreditMemo.id == memo_uuid)
            .values(status="applied", invoice_id=uuid.UUID(invoice_id))
        )
        async with realdb.client(key="a", role="admin") as c:
            edit = asyncio.create_task(
                c.patch(f"/api/credit-memos/{memo_id}", json={"amount": "450.00"})
            )
            assert await _wait_for_lock_waiter(mk), "the edit never queued behind the apply"
            await held.commit()
            resp = await edit

    assert resp.status_code == 409, resp.text
    async with mk() as s:
        memo = await s.get(CreditMemo, memo_uuid)
        assert memo.status == "applied"
        assert memo.amount == Decimal("50.00")


async def test_apply_reads_the_amount_a_concurrent_edit_committed(realdb):
    """The reverse interleaving: an edit raising the amount commits while an
    apply is in flight. The apply must check the balance against the NEW
    amount. Without `FOR UPDATE` on the apply's memo read it checked the old
    one, passed, and its UPDATE then stamped `applied` onto a memo worth more
    than the invoice — a negative payable."""
    mk = realdb.sessionmaker("a")
    org_id = realdb.info("a").org_id
    vendor_id = await _add_vendor(mk, org_id)
    # `_add_invoice` books 500.00.
    invoice_id = await _add_invoice(mk, org_id, vendor_id=vendor_id, number="INV-RACE-A")
    async with realdb.client(key="a", role="admin") as c:
        memo_id = await _create_open_memo(c, vendor_id, number="CM-RACE-A", amount="100.00")
    memo_uuid = uuid.UUID(memo_id)

    async with mk() as held:
        # Stand-in for an edit holding the row, mid-transaction.
        await held.execute(select(CreditMemo).where(CreditMemo.id == memo_uuid).with_for_update())
        await held.execute(
            update(CreditMemo).where(CreditMemo.id == memo_uuid).values(amount=Decimal("600.00"))
        )
        async with realdb.client(key="a", role="admin") as c:
            apply = asyncio.create_task(
                c.post(f"/api/credit-memos/{memo_id}/apply", json={"invoice_id": invoice_id})
            )
            assert await _wait_for_lock_waiter(mk), "the apply never queued behind the edit"
            await held.commit()
            resp = await apply

    assert resp.status_code == 409, resp.text
    assert "exceeds" in resp.json()["detail"]
    async with mk() as s:
        memo = await s.get(CreditMemo, memo_uuid)
        assert memo.status == "open"
        assert memo.invoice_id is None
