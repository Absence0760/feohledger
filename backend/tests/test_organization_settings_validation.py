"""`PATCH /api/organization` type validation for well-known settings sub-keys.

The generic `settings` merge on this endpoint accepts an almost-arbitrary
client dict and merges it straight into `Organization.settings`, so a bad type
(a numeric `currency`, a non-numeric `cfo_approval_above`) used to persist
silently instead of being rejected at save. `_validate_settings_patch`
(app/api/organization.py) closes just these two specific type-confusion holes
— it is deliberately not a schema for the whole freeform settings bag.

The `sso` block gets one refusal of its own: `sso_only` over an identity
provider that does not resolve (`_refuse_unresolvable_sso_only`,
docs/decisions.md §204).
"""

from __future__ import annotations

import base64

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
# `sso.sso_only` needs an identity provider that resolves (§204)
#
# Password sign-in is closed only when the selected protocol's IdP block
# resolves; otherwise the password stays open as the escape hatch, because the
# login page has no SSO button to offer. Saving such a block would leave the
# admin believing SSO is enforced when it is not, so the save is refused, with
# the offending keys named and no value echoed.
# ---------------------------------------------------------------------------

_SECRET = "s3cr3t-client-value-that-must-not-echo"
_OIDC_READY = {
    "enabled": True,
    "sso_only": True,
    "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    "client_id": "feoh",
    "client_secret": _SECRET,
}
_SAML_READY = {
    "enabled": True,
    "sso_only": True,
    "protocol": "saml",
    "idp_entity_id": "https://idp.example.com/saml",
    "idp_sso_url": "https://idp.example.com/saml/sso",
    "idp_x509_cert": base64.b64encode(b"fake-but-valid-base64-der-bytes").decode(),
}


async def _patch_sso(realdb, block):
    async with realdb.client(key="a", role="admin") as c:
        return await c.patch("/api/organization", json={"settings": {"sso": block}})


async def _stored_sso(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/organization")
    return resp.json()["settings"].get("sso")


async def _clear_sso(realdb) -> None:
    resp = await _patch_sso(realdb, {})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block,named",
    [
        pytest.param(
            {"enabled": True, "sso_only": True},
            ["sso.discovery_url", "sso.client_id", "sso.client_secret"],
            id="oidc-empty",
        ),
        pytest.param(
            {k: v for k, v in _OIDC_READY.items() if k != "discovery_url"},
            ["sso.discovery_url"],
            id="oidc-one-missing",
        ),
        pytest.param(
            {**_OIDC_READY, "discovery_url": "file:///etc/passwd"},
            ["sso.discovery_url"],
            id="oidc-not-a-url",
        ),
        pytest.param(
            {"enabled": True, "sso_only": True, "protocol": "saml"},
            ["sso.idp_entity_id", "sso.idp_sso_url", "sso.idp_x509_cert"],
            id="saml-empty",
        ),
        pytest.param(
            {**_SAML_READY, "idp_x509_cert": "not base64 !!"},
            ["sso.idp_x509_cert"],
            id="saml-bad-cert",
        ),
        pytest.param(
            # A complete OIDC block does not satisfy a tenant set to SAML.
            {**_OIDC_READY, "protocol": "saml"},
            ["sso.idp_entity_id", "sso.idp_sso_url", "sso.idp_x509_cert"],
            id="protocol-selects-the-block",
        ),
    ],
)
async def test_sso_only_over_an_unresolvable_idp_is_refused(realdb, block, named):
    """Refused at save, naming every offending key and never a value, and the
    stored block is left as it was."""
    try:
        before = await _stored_sso(realdb)
        resp = await _patch_sso(realdb, block)
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert isinstance(detail, str)
        for name in named:
            assert name in detail
        assert "sso_only" in detail
        assert _SECRET not in resp.text
        assert await _stored_sso(realdb) == before
    finally:
        await _clear_sso(realdb)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block",
    [
        pytest.param(_OIDC_READY, id="oidc-complete"),
        pytest.param(_SAML_READY, id="saml-complete"),
        # Staging the flag before the IdP is ready closes nothing, so it is fine.
        pytest.param({"enabled": False, "sso_only": True}, id="sso_only-with-sso-off"),
        # An incomplete block that does NOT ask for SSO-only locks nobody out:
        # the password form stays, and there is simply no SSO button yet.
        pytest.param({"enabled": True, "client_id": "feoh"}, id="incomplete-without-sso_only"),
    ],
)
async def test_an_sso_block_that_cannot_lock_anyone_out_is_accepted(realdb, block):
    try:
        resp = await _patch_sso(realdb, block)
        assert resp.status_code == 200, resp.text
        assert await _stored_sso(realdb) == block
    finally:
        await _clear_sso(realdb)


@pytest.mark.asyncio
async def test_a_stored_unresolvable_block_does_not_block_an_unrelated_save(realdb):
    """The refusal applies to a PATCH that writes `sso`. A block that got into
    the row some other way (a DB edit) is already harmless, because the
    password stays open over it, and it must not hold every other setting
    hostage."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.organization import Organization

    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
        org.settings = {**(org.settings or {}), "sso": {"enabled": True, "sso_only": True}}
        flag_modified(org, "settings")
        await s.commit()
    try:
        async with realdb.client(key="a", role="admin") as c:
            resp = await c.patch(
                "/api/organization",
                json={"settings": {"invoice_defaults": {"currency": "EUR"}}},
            )
        assert resp.status_code == 200, resp.text
    finally:
        await _clear_sso(realdb)
        await _reset(realdb, "a")
