"""`/api/organization/sso` — the one sanctioned, audited writer of `settings.sso`.

Before it, the generic `PATCH /api/organization` was the only way to configure
SSO. It wrote no audit row and merged per top-level key, so a
`{"sso": {"client_secret": ...}}` body (the rotation the runbook once
prescribed) replaced the whole block: `enabled`, `sso_only`, the IdP config and
the SCIM group state all went, silently.

Covers:

  * RBAC — admin-only on GET and PUT.
  * The client secret is write-only: no response carries it, and a refusal
    never echoes it — not even FastAPI's validation 422.
  * "Leave blank to keep": an omitted or blank secret keeps the stored one;
    removal is an explicit flag.
  * The SCIM token digest + group state are carried across every save, and so
    is the group → role map unless the request names one (which must name real
    roles).
  * `enabled` / `sso_only` must be stated — an omitted flag is not read as off.
  * `sso_only` over an IdP block that does not resolve is a coded 422 (§204),
    moved here from the PATCH.
  * Every save writes `organization.sso_updated` with key names and posture
    only.

`Organization.settings` is reset by the `realdb` harness before every test.
"""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.organization import Organization
from app.models.workflow import AuditLog

URL = "/api/organization/sso"
SECRET = "s3cr3t-client-value-that-must-not-echo"
SECRET_2 = "r0tated-client-value-that-must-not-echo"

OIDC_READY = {
    "enabled": True,
    "sso_only": True,
    "protocol": "oidc",
    "provider": "okta",
    "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
    "client_id": "feoh",
    "client_secret": SECRET,
}
SAML_READY = {
    "enabled": True,
    "sso_only": True,
    "protocol": "saml",
    "idp_entity_id": "https://idp.example.com/saml",
    "idp_sso_url": "https://idp.example.com/saml/sso",
    "idp_x509_cert": base64.b64encode(b"fake-but-valid-base64-der-bytes").decode(),
}
SCIM_STATE = {
    "scim_bearer_hash": "ab" * 32,
    "scim_groups": {"g1": {"displayName": "AP Managers", "members": []}},
    "scim_group_role_map": {"AP Managers": "ap_manager"},
}


async def _stored(realdb, key: str = "a") -> dict:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
    return dict((org.settings or {}).get("sso") or {})


async def _seed(realdb, block: dict, key: str = "a") -> None:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        org.settings = {**(org.settings or {}), "sso": block}
        flag_modified(org, "settings")
        await s.commit()


async def _put(realdb, body: dict, role: str = "admin"):
    async with realdb.client(key="a", role=role) as c:
        return await c.put(URL, json=body)


async def _audit_rows(realdb, key: str = "a") -> list[AuditLog]:
    async with realdb.sessionmaker(key)() as s:
        return list(
            (await s.execute(select(AuditLog).where(AuditLog.action == "organization.sso_updated")))
            .scalars()
            .all()
        )


# ---------- RBAC -------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_auth(realdb):
    async with realdb.client(key="a", role=None) as c:
        assert (await c.get(URL)).status_code == 401
        assert (await c.put(URL, json=OIDC_READY)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap_clerk", "ap_manager", "cfo"])
async def test_admin_only(realdb, role):
    async with realdb.client(key="a", role=role) as c:
        assert (await c.get(URL)).status_code == 403
        assert (await c.put(URL, json=OIDC_READY)).status_code == 403
    assert await _stored(realdb) == {}


# ---------- read -------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_defaults_for_an_unconfigured_org(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get(URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["sso_only"] is False
    assert body["protocol"] == "oidc"
    assert body["client_secret_configured"] is False
    assert body["password_sign_in_closed"] is False
    assert body["idp_config_missing"] == ["discovery_url", "client_id", "client_secret"]
    # What the admin registers at the IdP, computed server-side.
    assert body["oidc_redirect_uri"].endswith(settings.sso_redirect_path)
    assert realdb.info("a").slug in body["oidc_redirect_uri"]
    assert body["saml_acs_url"].endswith("/api/auth/saml/acs")
    assert realdb.info("a").slug in body["saml_sp_entity_id"]
    assert "client_secret" not in body


# ---------- the secret is write-only ----------------------------------------


@pytest.mark.asyncio
async def test_client_secret_never_appears_in_any_response(realdb):
    resp = await _put(realdb, OIDC_READY)
    assert resp.status_code == 200, resp.text
    assert SECRET not in resp.text
    assert resp.json()["client_secret_configured"] is True
    assert resp.json()["password_sign_in_closed"] is True
    assert (await _stored(realdb))["client_secret"] == SECRET

    async with realdb.client(key="a", role="admin") as c:
        get_sso = await c.get(URL)
        get_org = await c.get("/api/organization")
    assert SECRET not in get_sso.text
    assert SECRET not in get_org.text
    assert "client_secret" not in get_org.json()["settings"]["sso"]


@pytest.mark.asyncio
async def test_a_validation_error_does_not_echo_the_secret(realdb):
    """FastAPI's default 422 echoes the request object for a missing required
    field. `enabled` is required by the endpoint, not by Pydantic, so omitting
    it is refused without the body coming back."""
    body = {k: v for k, v in OIDC_READY.items() if k != "enabled"}
    resp = await _put(realdb, body)
    assert resp.status_code == 422, resp.text
    assert "sso.enabled" in resp.json()["detail"]
    assert SECRET not in resp.text
    assert await _stored(realdb) == {}


@pytest.mark.asyncio
async def test_a_non_text_secret_is_refused_without_its_value(realdb):
    resp = await _put(realdb, {**OIDC_READY, "client_secret": 987654321987})
    assert resp.status_code == 422
    assert "987654321987" not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("omitted", [{}, {"client_secret": ""}, {"client_secret": "   "}])
async def test_a_blank_or_omitted_secret_keeps_the_stored_one(realdb, omitted):
    await _seed(realdb, dict(OIDC_READY))
    body = {k: v for k, v in OIDC_READY.items() if k != "client_secret"} | omitted
    resp = await _put(realdb, {**body, "client_id": "feoh-renamed"})
    assert resp.status_code == 200, resp.text
    stored = await _stored(realdb)
    assert stored["client_secret"] == SECRET
    assert stored["client_id"] == "feoh-renamed"


@pytest.mark.asyncio
async def test_a_new_secret_replaces_the_stored_one(realdb):
    await _seed(realdb, dict(OIDC_READY))
    resp = await _put(realdb, {**OIDC_READY, "client_secret": f"  {SECRET_2}\n"})
    assert resp.status_code == 200, resp.text
    assert (await _stored(realdb))["client_secret"] == SECRET_2
    assert SECRET_2 not in resp.text


@pytest.mark.asyncio
async def test_clearing_the_secret_is_explicit(realdb):
    await _seed(realdb, dict(OIDC_READY))
    body = {k: v for k, v in OIDC_READY.items() if k != "client_secret"}
    resp = await _put(realdb, {**body, "sso_only": False, "clear_client_secret": True})
    assert resp.status_code == 200, resp.text
    assert "client_secret" not in await _stored(realdb)
    assert resp.json()["client_secret_configured"] is False


@pytest.mark.asyncio
async def test_clearing_the_secret_under_sso_only_is_refused(realdb):
    """Clearing it would leave an SSO-only block that cannot resolve."""
    await _seed(realdb, dict(OIDC_READY))
    body = {k: v for k, v in OIDC_READY.items() if k != "client_secret"}
    resp = await _put(realdb, {**body, "clear_client_secret": True})
    assert resp.status_code == 422
    assert resp.json()["detail"]["params"]["fields"] == ["client_secret"]
    assert (await _stored(realdb))["client_secret"] == SECRET


@pytest.mark.asyncio
async def test_new_secret_and_clear_together_is_refused(realdb):
    resp = await _put(realdb, {**OIDC_READY, "clear_client_secret": True})
    assert resp.status_code == 422
    assert SECRET not in resp.text


# ---------- carried keys -----------------------------------------------------


@pytest.mark.asyncio
async def test_scim_state_survives_a_save(realdb):
    """The bug this endpoint replaces: a config save un-provisioning groups."""
    await _seed(realdb, {**OIDC_READY, **SCIM_STATE})
    resp = await _put(realdb, {**SAML_READY})
    assert resp.status_code == 200, resp.text
    stored = await _stored(realdb)
    for key, value in SCIM_STATE.items():
        assert stored[key] == value
    assert stored["protocol"] == "saml"
    assert resp.json()["scim_token_configured"] is True
    assert resp.json()["scim_group_role_map"] == {"AP Managers": "ap_manager"}
    assert "scim_bearer_hash" not in resp.json()


@pytest.mark.asyncio
async def test_a_put_replaces_rather_than_merges(realdb):
    """Keys the request leaves out are removed — the stored block is what the
    admin saw on the form, with nothing left over from a replaced IdP."""
    await _seed(realdb, {**OIDC_READY, "allowed_email_domains": ["acme.com"]})
    resp = await _put(realdb, {**SAML_READY})
    assert resp.status_code == 200, resp.text
    stored = await _stored(realdb)
    assert "discovery_url" not in stored
    assert "client_id" not in stored
    assert "allowed_email_domains" not in stored
    # The secret is the exception: kept until cleared.
    assert stored["client_secret"] == SECRET


@pytest.mark.asyncio
async def test_the_role_map_is_replaced_when_named(realdb):
    await _seed(realdb, {**OIDC_READY, **SCIM_STATE})
    resp = await _put(realdb, {**OIDC_READY, "scim_group_role_map": {"Finance": "cfo"}})
    assert resp.status_code == 200, resp.text
    stored = await _stored(realdb)
    assert stored["scim_group_role_map"] == {"Finance": "cfo"}
    assert stored["scim_groups"] == SCIM_STATE["scim_groups"]


@pytest.mark.asyncio
async def test_a_role_map_naming_an_unknown_role_is_refused(realdb):
    await _seed(realdb, {**OIDC_READY, **SCIM_STATE})
    resp = await _put(realdb, {**OIDC_READY, "scim_group_role_map": {"Finance": "treasurer"}})
    assert resp.status_code == 422
    assert "treasurer" in resp.json()["detail"]
    assert (await _stored(realdb))["scim_group_role_map"] == SCIM_STATE["scim_group_role_map"]


# ---------- shape rules -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch,field",
    [
        pytest.param({"protocol": "ldap"}, "sso.protocol", id="unknown-protocol"),
        pytest.param({"allowed_email_domains": ["not a domain"]}, "sso.allowed_email_domains"),
        pytest.param({"allowed_email_domains": ["user@acme.com"]}, "sso.allowed_email_domains"),
        pytest.param({"provider": "x" * 65}, "sso.provider", id="provider-too-long"),
    ],
)
async def test_bad_shapes_are_refused(realdb, patch, field):
    resp = await _put(realdb, {**OIDC_READY, **patch})
    assert resp.status_code == 422, resp.text
    assert field in resp.json()["detail"]
    assert SECRET not in resp.text


@pytest.mark.asyncio
async def test_email_domains_are_normalized(realdb):
    resp = await _put(
        realdb, {**OIDC_READY, "allowed_email_domains": [" ACME.com ", "@acme.com", "x.io", ""]}
    )
    assert resp.status_code == 200, resp.text
    assert (await _stored(realdb))["allowed_email_domains"] == ["acme.com", "x.io"]


# ---------- sso_only needs a resolving IdP (§204) -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block,fields",
    [
        pytest.param(
            {"enabled": True, "sso_only": True},
            ["discovery_url", "client_id", "client_secret"],
            id="oidc-empty",
        ),
        pytest.param(
            {k: v for k, v in OIDC_READY.items() if k != "discovery_url"},
            ["discovery_url"],
            id="oidc-one-missing",
        ),
        pytest.param(
            {**OIDC_READY, "discovery_url": "file:///etc/passwd"},
            ["discovery_url"],
            id="oidc-not-a-url",
        ),
        pytest.param(
            {"enabled": True, "sso_only": True, "protocol": "saml"},
            ["idp_entity_id", "idp_sso_url", "idp_x509_cert"],
            id="saml-empty",
        ),
        pytest.param(
            {**SAML_READY, "idp_x509_cert": "not base64 !!"},
            ["idp_x509_cert"],
            id="saml-bad-cert",
        ),
        pytest.param(
            # A complete OIDC block does not satisfy a tenant set to SAML.
            {**OIDC_READY, "protocol": "saml"},
            ["idp_entity_id", "idp_sso_url", "idp_x509_cert"],
            id="protocol-selects-the-block",
        ),
    ],
)
async def test_sso_only_over_an_unresolvable_idp_is_refused(realdb, block, fields):
    """Refused at save with a coded refusal naming every offending key and never
    a value, and the stored block is left as it was."""
    await _seed(realdb, {**SCIM_STATE})
    resp = await _put(realdb, block)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "sso_only_idp_unresolved"
    assert detail["params"]["fields"] == fields
    for name in fields:
        assert f"sso.{name}" in detail["message"]
    assert SECRET not in resp.text
    assert await _stored(realdb) == SCIM_STATE
    assert await _audit_rows(realdb) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block",
    [
        pytest.param(OIDC_READY, id="oidc-complete"),
        pytest.param(SAML_READY, id="saml-complete"),
        # Staging the flag before the IdP is ready closes nothing.
        pytest.param({"enabled": False, "sso_only": True}, id="sso_only-with-sso-off"),
        # An incomplete block that does NOT ask for SSO-only locks nobody out.
        pytest.param(
            {"enabled": True, "sso_only": False, "client_id": "feoh"},
            id="incomplete-without-sso_only",
        ),
    ],
)
async def test_an_sso_block_that_cannot_lock_anyone_out_is_accepted(realdb, block):
    resp = await _put(realdb, block)
    assert resp.status_code == 200, resp.text
    stored = await _stored(realdb)
    for key, value in block.items():
        assert stored[key] == value


# ---------- audit --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_save_is_audited_with_key_names_only(realdb):
    await _seed(realdb, {**OIDC_READY, **SCIM_STATE})
    resp = await _put(
        realdb, {**OIDC_READY, "client_secret": SECRET_2, "client_id": "feoh-2", "sso_only": False}
    )
    assert resp.status_code == 200, resp.text
    rows = await _audit_rows(realdb)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id == realdb.info("a").users["admin"]
    assert row.entity_type == "organization"
    assert row.details["changed"] == ["client_id", "client_secret", "sso_only"]
    assert row.details["enabled"] is True
    assert row.details["sso_only"] is False
    assert row.details["protocol"] == "oidc"
    flat = str(row.details)
    for value in (SECRET, SECRET_2, "feoh-2", "idp.example.com"):
        assert value not in flat


@pytest.mark.asyncio
async def test_a_first_save_does_not_report_respelled_keys(realdb):
    """A block written before this endpoint (no `protocol`, an empty
    allowlist) re-saved unchanged reports nothing changed."""
    legacy = {k: v for k, v in OIDC_READY.items() if k != "protocol"}
    await _seed(realdb, {**legacy, "allowed_email_domains": []})
    resp = await _put(realdb, OIDC_READY)
    assert resp.status_code == 200, resp.text
    assert (await _audit_rows(realdb))[0].details["changed"] == []


# ---------- audit-first and the row lock ----------------------------------------


@pytest.mark.asyncio
async def test_no_audit_row_means_no_save(realdb, monkeypatch):
    """A sign-in policy change that the trail cannot record is refused, not
    applied unrecorded — the same call the break-glass makes."""
    from app.services import audit_dispatch

    await _seed(realdb, {**OIDC_READY, "sso_only": False})

    async def _fail(**_kwargs):
        raise ConnectionError("audit store down")

    monkeypatch.setattr(audit_dispatch, "_write_auth_audit", _fail)
    resp = await _put(realdb, {**OIDC_READY, "client_secret": SECRET_2})
    assert resp.status_code == 503, resp.text
    assert SECRET_2 not in resp.text
    stored = await _stored(realdb)
    assert stored["client_secret"] == SECRET
    assert stored["sso_only"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param([SECRET], id="array-body"),
        pytest.param({**OIDC_READY, "enabled": "maybe"}, id="enabled-wrong-type"),
        pytest.param({**OIDC_READY, "allowed_email_domains": "acme.com"}, id="domains-not-list"),
    ],
)
async def test_framework_validation_errors_do_not_echo_the_secret(realdb, body):
    resp = await _put(realdb, body)
    assert resp.status_code == 422, resp.text
    assert SECRET not in resp.text


@pytest.mark.asyncio
async def test_lock_organization_refreshes_a_stale_snapshot(realdb):
    """Every settings writer reads `org.settings` after `lock_organization`, so
    a snapshot loaded before another writer committed is refreshed rather than
    written back over that commit."""
    from app.tenant import lock_organization

    org_id = realdb.info("a").org_id
    async with realdb.control_sessionmaker()() as stale:
        org = (
            await stale.execute(select(Organization).where(Organization.id == org_id))
        ).scalar_one()
        assert "sso" not in (org.settings or {})
        # Another writer commits while this session still holds its snapshot
        # (READ COMMITTED, and the plain SELECT above took no lock).
        await _seed(realdb, {**OIDC_READY, "sso_only": False})
        locked = await lock_organization(stale, org)
        assert locked is org
        assert org.settings["sso"]["client_id"] == "feoh"
        await stale.rollback()
