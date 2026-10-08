"""`PATCH /api/organization` type validation for well-known settings sub-keys.

The generic `settings` merge on this endpoint accepts an almost-arbitrary
client dict and merges it straight into `Organization.settings`, so a bad type
(a numeric `currency`, a non-numeric `cfo_approval_above`) used to persist
silently instead of being rejected at save. `_validate_settings_patch`
(app/api/organization.py) closes just these two specific type-confusion holes
— it is deliberately not a schema for the whole freeform settings bag.

The `sso` key is refused outright: its one writer is the audited
`PUT /api/organization/sso` (`test_organization_sso_settings.py`).
"""

from __future__ import annotations

import pytest


async def _reset(realdb, key: str) -> None:
    async with realdb.client(key=key, role="admin") as c:
        await c.patch(
            "/api/organization",
            json={
                "settings": {
                    "invoice_defaults": {"currency": "USD"},
                    "payments": {"cfo_approval_above": None},
                }
            },
        )


@pytest.mark.asyncio
async def test_numeric_currency_rejected(realdb):
    """A numeric `invoice_defaults.currency` must 422, not persist."""
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.patch(
                "/api/organization",
                json={"settings": {"invoice_defaults": {"currency": 840}}},
            )
        assert resp.status_code == 422, resp.text
        assert "currency" in resp.json()["detail"].lower()

        async with realdb.client(key="a", role="admin") as c:
            get_resp = await c.get("/api/organization")
        assert get_resp.json()["settings"]["invoice_defaults"]["currency"] != 840
    finally:
        await _reset(realdb, "a")


@pytest.mark.asyncio
async def test_wrong_length_currency_rejected(realdb):
    """A 2-letter (or otherwise non-3-letter) currency code must 422."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"invoice_defaults": {"currency": "US"}}},
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_valid_currency_accepted(realdb):
    """A well-formed 3-letter currency code saves and round-trips."""
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.patch(
                "/api/organization",
                json={"settings": {"invoice_defaults": {"currency": "EUR"}}},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["settings"]["invoice_defaults"]["currency"] == "EUR"
    finally:
        await _reset(realdb, "a")


@pytest.mark.asyncio
async def test_non_numeric_cfo_threshold_rejected(realdb):
    """A string `payments.cfo_approval_above` must 422, not persist."""
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.patch(
                "/api/organization",
                json={"settings": {"payments": {"cfo_approval_above": "lots"}}},
            )
        assert resp.status_code == 422, resp.text
        assert "cfo_approval_above" in resp.json()["detail"].lower()

        async with realdb.client(key="a", role="admin") as c:
            get_resp = await c.get("/api/organization")
        payments_after = get_resp.json()["settings"].get("payments") or {}
        assert payments_after.get("cfo_approval_above") != "lots"
    finally:
        await _reset(realdb, "a")


@pytest.mark.asyncio
async def test_valid_numeric_cfo_threshold_accepted(realdb):
    """A well-typed numeric threshold saves and round-trips."""
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.patch(
                "/api/organization",
                json={"settings": {"payments": {"cfo_approval_above": 5000}}},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["settings"]["payments"]["cfo_approval_above"] == 5000
    finally:
        await _reset(realdb, "a")


@pytest.mark.asyncio
async def test_null_cfo_threshold_accepted(realdb):
    """`null` clears the threshold — explicitly allowed, not a type error."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"payments": {"cfo_approval_above": None}}},
        )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# `sso` is not this endpoint's to write
#
# `PUT /api/organization/sso` is its one sanctioned, audited writer. This merge
# replaced the whole block per top-level key, so a secret-only PATCH dropped
# `enabled`, `sso_only`, the IdP config and the SCIM group state, unaudited.
# The `sso_only` refusal (§204) moved with it — see
# `test_organization_sso_settings.py`.
# ---------------------------------------------------------------------------

_SECRET = "s3cr3t-client-value-that-must-not-echo"


async def _seed_sso(realdb, block: dict) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.organization import Organization

    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
        org.settings = {**(org.settings or {}), "sso": block}
        flag_modified(org, "settings")
        await s.commit()


async def _stored_sso_raw(realdb) -> dict | None:
    from sqlalchemy import select

    from app.models.organization import Organization

    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
    return (org.settings or {}).get("sso")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block",
    [
        pytest.param({"client_secret": "rotated"}, id="secret-only-rotation"),
        pytest.param({}, id="empty-block"),
        pytest.param(None, id="null"),
    ],
)
async def test_patch_refuses_the_sso_key_and_names_its_endpoint(realdb, block):
    stored = {
        "enabled": True,
        "sso_only": False,
        "client_id": "feoh",
        "client_secret": _SECRET,
        "scim_groups": {"g1": {"displayName": "X", "members": []}},
    }
    await _seed_sso(realdb, stored)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"name": "Renamed", "settings": {"sso": block}},
        )
    assert resp.status_code == 422, resp.text
    assert "/api/organization/sso" in resp.json()["detail"]
    assert _SECRET not in resp.text
    assert await _stored_sso_raw(realdb) == stored


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block",
    [
        pytest.param({"ai_overage_reported": {"2026-10": 999999}}, id="overage-marker"),
        pytest.param({"provider": "mock"}, id="provider"),
        pytest.param({"monthly_spend_cap": "0.00"}, id="cap-bypassing-its-endpoint"),
    ],
)
async def test_patch_refuses_the_billing_key(realdb, block):
    """`settings.billing` drives charges (§255); a tenant admin writing it could
    zero their own overage, re-route it to `mock`, or bypass the audited cap."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch("/api/organization", json={"settings": {"billing": block}})
    assert resp.status_code == 422, resp.text
    assert "/api/billing/spending-cap" in resp.json()["detail"]
    from app.models.organization import Organization

    async with realdb.control_sessionmaker()() as s:
        org = await s.get(Organization, realdb.info("a").org_id)
        assert "billing" not in (org.settings or {})


@pytest.mark.asyncio
async def test_a_stored_unresolvable_block_does_not_block_an_unrelated_save(realdb):
    """A block that got into the row some other way (a DB edit) is already
    harmless, because the password stays open over it, and it must not hold
    every other setting hostage."""
    await _seed_sso(realdb, {"enabled": True, "sso_only": True})
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.patch(
                "/api/organization",
                json={"settings": {"invoice_defaults": {"currency": "EUR"}}},
            )
        assert resp.status_code == 200, resp.text
        assert await _stored_sso_raw(realdb) == {"enabled": True, "sso_only": True}
    finally:
        await _reset(realdb, "a")
