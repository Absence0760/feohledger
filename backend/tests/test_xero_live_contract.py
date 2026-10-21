"""Live contract check: the Xero behaviour the adapter relies on, against real Xero.

Opt-in and local only (``pnpm test:erp:xero-live``, which sets
``FEOH_XERO_LIVE=1``). Every other run skips this module. It reads the Xero
connection a local tenant already holds (default ``acme``; override with
``FEOH_XERO_LIVE_TENANT``), so connect that tenant to a Xero org first
(Organization → ERP Integration → Connect to Xero, with a real Xero app's
credentials in ``backend/.env``). A Demo Company is the intended target.

Why it exists: ``tools/fake-erp`` stands in for Xero in CI, and it is only as
right as what we believe Xero does. Two of those beliefs were wrong until the
2026-10-20 Demo Company test: a contact only sent purchase orders is not
``IsSupplier``, and the vendor sync skipped it. This module asserts the
behaviour the adapter (and the fake) depend on, so a Xero change, or a fake
that drifted from Xero, surfaces as a named failure instead of a quiet wrong
sync.

Read-only: every request is a GET. Nothing is created, changed or deleted in
the Xero organisation.

Not run in CI: it needs an interactive OAuth consent, Xero rotates refresh
tokens, a Demo Company resets every 28 days, and this repository is public.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

LIVE = os.environ.get("FEOH_XERO_LIVE") == "1"
TENANT = os.environ.get("FEOH_XERO_LIVE_TENANT", "acme")

pytestmark = pytest.mark.skipif(
    not LIVE, reason="live Xero contract check — opt in with `pnpm test:erp:xero-live`"
)

#: The PO statuses the vendor sync filters on, plus the one it skips.
_PO_STATUSES = ("DRAFT", "SUBMITTED", "AUTHORISED", "BILLED")


def _load_local_settings() -> None:
    """Give this process the same settings `python main.py` would have.

    ``app.config.settings`` was built when the suite imported ``app``, from the
    bare process environment. The tenant's sealed Xero tokens and the app
    credentials live in ``backend/.env`` / ``.env.development``, which only the
    local entrypoint loads, so load them the same way (``.env`` first, neither
    overriding the real environment) and refresh the settings object in place.
    """
    from dotenv import load_dotenv

    from app.config import Settings, settings

    backend = Path(__file__).resolve().parents[1]
    load_dotenv(backend / ".env", override=False)
    load_dotenv(backend / ".env.development", override=False)
    fresh = Settings()
    for name in type(fresh).model_fields:
        setattr(settings, name, getattr(fresh, name))


@pytest.fixture(scope="module")
async def xero():
    """The tenant's live Xero adapter, built the way a sync builds it."""
    _load_local_settings()
    from sqlalchemy import select

    from app.database import control_session_factory
    from app.models.organization import Organization
    from app.services.erp_adapters import get_erp_adapter
    from app.services.provider_credentials import provider_config

    async with control_session_factory() as db:
        org = (
            await db.execute(select(Organization).where(Organization.slug == TENANT))
        ).scalar_one_or_none()
        assert org is not None, f"no local tenant '{TENANT}' (FEOH_XERO_LIVE_TENANT)"
        config = await provider_config(org, "erp", db=db) or {}
    # Opted in but not connected is a failure, not a skip: a green run must
    # mean Xero was actually asked.
    assert config.get("type") == "xero", (
        f"tenant '{TENANT}' is not set to Xero — connect it under Organization → ERP Integration"
    )
    return get_erp_adapter(config)


async def _get(adapter, path: str, params: dict | None = None) -> dict:
    """One GET against the tenant's Xero org, asserting it succeeded."""
    from app.services.erp_adapters import xero

    headers = await adapter._headers()
    async with adapter.oauth_client(timeout=30) as client:
        resp = await client.get(f"{xero._api_base()}/{path}", params=params, headers=headers)
    assert resp.status_code == 200, f"GET {path} → {resp.status_code}"
    return resp.json()


async def test_the_connection_is_live(xero):
    assert await xero.test_connection() is True, (
        f"Xero refused tenant '{TENANT}'s stored connection — reconnect it under "
        "Organization → ERP Integration"
    )


async def test_the_supplier_filter_returns_only_suppliers(xero):
    rows = (await _get(xero, "Contacts", {"where": "IsSupplier==true", "page": 1}))["Contacts"]
    assert rows, "the org has no supplier contacts to check"
    assert all(r.get("IsSupplier") is True for r in rows)
    assert all(r.get("ContactID") and r.get("Name") for r in rows)


@pytest.mark.parametrize("status", _PO_STATUSES)
async def test_the_purchase_order_status_filter_is_honoured(xero, status):
    """`list_vendors` reads each unbilled status separately; an ignored filter
    would make it read every PO in the org three times."""
    rows = (await _get(xero, "PurchaseOrders", {"Status": status, "page": 1}))["PurchaseOrders"]
    assert all(r.get("Status") == status for r in rows), {r.get("Status") for r in rows}


async def test_every_purchase_order_maps_and_names_its_contact(xero):
    from app.services.erp_adapters.xero import _PO_STATUS_MAP

    rows = (await _get(xero, "PurchaseOrders", {"page": 1}))["PurchaseOrders"]
    assert rows, "the org has no purchase orders to check"
    # An unmapped status silently becomes "open" in the sync.
    assert {r.get("Status") for r in rows} <= set(_PO_STATUS_MAP)
    for po in rows:
        assert po.get("PurchaseOrderNumber")
        assert (po.get("Contact") or {}).get("ContactID")


async def test_contacts_can_be_fetched_by_id(xero):
    """How `list_vendors` reads the contacts on unbilled POs."""
    pos = (await _get(xero, "PurchaseOrders", {"page": 1}))["PurchaseOrders"]
    ids = sorted({po["Contact"]["ContactID"] for po in pos if po.get("Contact")})[:3]
    assert ids, "the org has no purchase orders to take contact ids from"
    rows = (await _get(xero, "Contacts", {"IDs": ",".join(ids)}))["Contacts"]
    assert sorted(r["ContactID"] for r in rows) == ids


async def test_every_account_class_maps(xero):
    from app.services.erp_adapters.xero import _ACCOUNT_CLASS_MAP

    rows = (await _get(xero, "Accounts"))["Accounts"]
    assert {r.get("Class") for r in rows} <= set(_ACCOUNT_CLASS_MAP)


async def test_every_bill_status_maps(xero):
    from app.services.erp_adapters.xero import _STATUS_MAP

    rows = (await _get(xero, "Invoices", {"where": 'Type=="ACCPAY"', "page": 1}))["Invoices"]
    if not rows:
        pytest.skip("the org has no bills yet")
    assert {r.get("Status") for r in rows} <= set(_STATUS_MAP)
    for bill in rows:
        assert bill.get("InvoiceID") and bill.get("Total") is not None
