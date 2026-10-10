"""The ERP provider catalogue and how `settings.erp` meets the credential store.

`erp_adapters/catalog` is the single source of truth for the Organization → ERP
form and names which ERP fields are secrets; those are sealed in
`provider_credentials` (decisions §266), never stored in the JSONB. Covers:

  * catalogue ↔ adapter-registry parity (both directions), and catalogue ↔
    `provider_credentials.SECRET_FIELDS` (every catalogue secret is sealed);
  * the catalogue's own shape (fields the frontend renders from);
  * `public_erp_config` / `merge_erp_update` / `secrets_to_drop` as pure
    functions;
  * the endpoints: `GET /organization/erp/providers` (admin only), no secret
    or OAuth capability on `GET /organization`, the PATCH round trip keeping
    the sealed secrets, a secret refused on PATCH, a changed destination or a
    different ERP dropping them (audited), and `test-erp` using them only for
    the saved connection — and failing closed when the store cannot be opened.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services import provider_credentials
from app.services.erp_adapters import catalog
from app.services.erp_adapters.dispatcher import MOCK_ADAPTER_KEY, list_available_adapters

# ---------- catalogue ↔ registry ---------------------------------------------


def test_every_direct_catalogue_entry_is_a_registered_or_pending_adapter():
    registered = set(list_available_adapters())
    for entry in catalog.ERP_PROVIDERS:
        assert entry["key"] in registered or entry["key"] in catalog.PENDING_ADAPTERS, (
            f"catalogue offers {entry['key']!r}, which has no adapter and is not pending"
        )


def test_pending_adapters_are_not_already_registered():
    """Empty PENDING_ADAPTERS as each adapter lands, so the set stays honest."""
    assert catalog.PENDING_ADAPTERS.isdisjoint(set(list_available_adapters()))
    assert catalog.PENDING_ADAPTERS <= {e["key"] for e in catalog.ERP_PROVIDERS}


def test_every_registered_direct_adapter_has_a_catalogue_entry():
    keys = {e["key"] for e in catalog.all_providers()}
    for adapter in set(list_available_adapters()) - {MOCK_ADAPTER_KEY, "merge_dev"}:
        assert adapter in keys, (
            f"adapter {adapter!r} is registered but the setup form can't offer it"
        )


def test_merge_dev_is_the_scale_plan_choice():
    assert catalog.MERGE_DEV_PROVIDER["key"] == "merge_dev"
    assert catalog.MERGE_DEV_PROVIDER["plan"] == "scale"
    assert {f["name"] for f in catalog.MERGE_DEV_PROVIDER["fields"] if f["secret"]} == {
        "api_key",
        "account_token",
    }
    long_tail = {e["value"] for e in catalog.MERGE_DEV_LONG_TAIL}
    assert {"sap_s4hana", "epicor", "acumatica"} <= long_tail
    # A direct ERP must not also be offered as a Merge.dev long-tail entry.
    assert long_tail.isdisjoint({e["key"] for e in catalog.ERP_PROVIDERS})


def test_catalogue_entries_are_well_formed():
    keys = [e["key"] for e in catalog.all_providers()]
    assert len(keys) == len(set(keys))
    for entry in catalog.all_providers():
        assert entry["auth"] in {"credentials", "oauth"}
        assert entry["docs_url"].startswith("https://")
        names = [f["name"] for f in entry["fields"]]
        assert len(names) == len(set(names)), entry["key"]
        for f in entry["fields"]:
            assert f["label_key"].startswith("org.erp."), f
            assert isinstance(f["secret"], bool) and isinstance(f["required"], bool)
            # The form drops "saved" from secrets when a destination field
            # changes; it learns which fields those are from this flag only.
            assert f["destination"] is (f["name"] in catalog.DESTINATION_KEYS), f
        if entry["auth"] == "oauth":
            # OAuth providers: the BYO-app pair is optional, never required.
            assert not any(f["required"] for f in entry["fields"] if f["secret"])
    assert {"US", "ZA"} <= {r for e in catalog.ERP_PROVIDERS for r in e["regions"]}


def test_every_catalogue_message_key_exists_in_the_frontend_catalogue():
    """`label_key` / `help_key` are frontend `MessageKey`s; a typo would render
    the raw key. `en.ts` is the source dict every locale is checked against."""
    import pathlib
    import re

    en = (
        pathlib.Path(__file__).resolve().parents[2] / "frontend/src/lib/i18n/locales/en.ts"
    ).read_text()
    defined = set(re.findall(r"^\s*'([^']+)':", en, re.MULTILINE))
    used = {
        key
        for entry in catalog.all_providers()
        for f in entry["fields"]
        for key in (f["label_key"], f.get("help_key"))
        if key
    }
    assert used - defined == set()


def test_existing_direct_adapters_keep_their_fields():
    by_key = {e["key"]: {f["name"]: f for f in e["fields"]} for e in catalog.ERP_PROVIDERS}
    assert set(by_key["netsuite"]) == {
        "account_id",
        "consumer_key",
        "consumer_secret",
        "token_id",
        "token_secret",
    }
    assert set(by_key["dynamics_365_bc"]) == {
        "base_url",
        "environment",
        "tenant_id",
        "client_id",
        "client_secret",
        "company_id",
    }
    assert by_key["netsuite"]["token_secret"]["secret"] is True
    assert by_key["dynamics_365_bc"]["client_secret"]["secret"] is True


def test_secret_keys_cover_every_secret_field_and_the_webhook_key():
    for entry in catalog.all_providers():
        for f in entry["fields"]:
            if f["secret"]:
                assert f["name"] in catalog.SECRET_KEYS
    assert "webhook_signing_secret" in catalog.SECRET_KEYS


def test_every_catalogue_secret_is_sealed_by_the_credential_store():
    """A catalogue secret the store does not list would be refused by `PUT
    /organization/credentials/erp` and — worse — not stripped from the JSONB, so
    a value that reached it some other way would be read back and used."""
    assert catalog.SECRET_KEYS <= provider_credentials.SECRET_FIELDS["erp"]


def test_the_oauth_tokens_are_service_only_paths():
    """Tokens are sealed beside the block's secrets but no admin path writes
    them: the endpoint's validator refuses them, the service writer admits them."""
    for path in ("oauth.access_token", "oauth.refresh_token"):
        with pytest.raises(provider_credentials.CredentialPathError):
            provider_credentials.validate_path("erp", path)
        provider_credentials.validate_path("erp", path, service=True)
    with pytest.raises(provider_credentials.CredentialPathError):
        provider_credentials.validate_update("erp", {"oauth.refresh_token": "forged"}, [])


def test_a_token_copy_in_the_jsonb_is_stripped_and_never_reaches_an_adapter():
    stored = {"type": "xero", "oauth": {"connection_id": "c", "refresh_token": "rt-STRAY"}}
    stripped = provider_credentials.strip_secrets("erp", stored)
    assert stripped["oauth"] == {"connection_id": "c"}
    injected = provider_credentials.inject_secrets(
        "erp", stripped, {"oauth.refresh_token": "rt-SEALED", "client_secret": "cs"}
    )
    # A sealed token is not merged into the adapter's config either.
    assert "rt-SEALED" not in str(injected)
    assert injected["client_secret"] == "cs"


# ---------- the pure view / merge / drop -------------------------------------

STORED = {
    "type": "netsuite",
    "integration_method": "direct",
    "account_id": "123",
    "consumer_key": "ck",
    "token_id": "tid",
}
STORED_SECRETS = {
    "consumer_secret": "cs-STORED",
    "token_secret": "ts-STORED",
    "webhook_signing_secret": "whs-STORED",
}


def test_public_view_hides_the_oauth_metadata():
    stored = {**STORED, "oauth": {"connection_id": "conn-SECRET", "provider": "xero"}}
    shown = catalog.public_erp_config(stored)
    assert shown["oauth"] == {"connected": True}
    assert shown["account_id"] == "123"
    assert "conn-SECRET" not in str(shown)
    # Pure: the input is untouched.
    assert stored["oauth"]["connection_id"] == "conn-SECRET"


def test_public_view_reports_an_unconsented_oauth_block_as_not_connected():
    assert catalog.public_erp_config({"oauth": {}})["oauth"] == {"connected": False}
    assert "oauth" not in catalog.public_erp_config({"type": "xero"})


def test_merge_is_configuration_only():
    """A secret in the request never lands in the JSONB, blank or not (the
    endpoint refuses a non-blank one before the merge)."""
    merged = catalog.merge_erp_update(
        STORED, {**STORED, "consumer_secret": "", "token_secret": "x", "token_id": "t2"}
    )
    assert merged["token_id"] == "t2"
    assert set(merged).isdisjoint(catalog.SECRET_KEYS)


BLACKBAUD = {
    "type": "blackbaud_fe_nxt",
    "integration_method": "direct",
    "ap_account_number": "2000",
    "currency": "USD",
    "transaction_code_values": [{"id": 7, "value": "General"}],
}


def test_merge_keeps_a_key_the_form_never_renders():
    """An API-set key the form has no field for survives a form save."""
    form = {k: v for k, v in BLACKBAUD.items() if k != "transaction_code_values"}
    merged = catalog.merge_erp_update(BLACKBAUD, {**form, "currency": "ZAR"})
    assert merged["transaction_code_values"] == [{"id": 7, "value": "General"}]
    assert merged["currency"] == "ZAR"


def test_merge_still_clears_a_rendered_field_left_out():
    form = {k: v for k, v in BLACKBAUD.items() if k != "project_id"}
    merged = catalog.merge_erp_update({**BLACKBAUD, "project_id": "P-1"}, form)
    assert "project_id" not in merged


def test_merge_drops_unrendered_keys_when_the_erp_changes():
    merged = catalog.merge_erp_update(BLACKBAUD, {"type": "xero", "integration_method": "direct"})
    assert "transaction_code_values" not in merged


def test_merge_never_re_persists_a_token_copy_left_in_the_oauth_block():
    stored = {
        "type": "xero",
        "integration_method": "direct",
        "oauth": {"connection_id": "c", "refresh_token": "rt-STRAY", "access_token": "at-STRAY"},
    }
    merged = catalog.merge_erp_update(stored, {"type": "xero", "integration_method": "direct"})
    assert merged["oauth"] == {"connection_id": "c"}


def test_merge_never_takes_oauth_from_the_request_and_keeps_the_stored_block():
    stored = {"type": "xero", "integration_method": "direct", "oauth": {"connection_id": "c"}}
    merged = catalog.merge_erp_update(
        stored,
        {"type": "xero", "integration_method": "direct", "oauth": {"connection_id": "forged"}},
    )
    assert merged["oauth"] == {"connection_id": "c"}
    fresh = catalog.merge_erp_update({}, {"type": "xero", "oauth": {"connection_id": "forged"}})
    assert "oauth" not in fresh


SYSPRO = {
    "type": "syspro",
    "integration_method": "direct",
    "base_url": "https://syspro.example.com/Rest",
    "operator": "op",
    "company_id": "1",
}
SYSPRO_SECRETS = {
    "operator_password": "opw-STORED",
    "company_password": "cpw-STORED",
    "webhook_signing_secret": "whs-STORED",
}


@pytest.mark.parametrize(
    "key, new_value",
    [
        ("base_url", "https://attacker.tld"),
        ("company_id", "2"),
        ("environment", "sandbox"),
        ("tenant_id", "other-tenant"),
        ("account_id", "attacker"),
    ],
)
def test_a_changed_destination_drops_every_outbound_secret(key, new_value):
    """Same ERP type, new destination: the stored password must never reach a
    host the save just named. The inbound webhook key is never sent outbound."""
    drop = catalog.secrets_to_drop(SYSPRO, {**SYSPRO, key: new_value})
    assert {"operator_password", "company_password"} <= drop
    assert "webhook_signing_secret" not in drop


def test_an_unchanged_destination_drops_nothing():
    """Blank vs absent and surrounding whitespace are the same destination; a
    non-destination field may change freely."""
    incoming = {
        **SYSPRO,
        "base_url": " https://syspro.example.com/Rest ",
        "environment": "",
        "posting_period": "C",
    }
    assert catalog.secrets_to_drop(SYSPRO, incoming) == frozenset()


def test_a_different_erp_or_a_cleared_one_drops_every_secret():
    """Business Central's client_secret must not become Xero's BYO app secret."""
    d365 = {"type": "dynamics_365_bc", "integration_method": "direct"}
    xero = {"type": "xero", "integration_method": "direct"}
    assert catalog.secrets_to_drop(d365, xero) == catalog.SECRET_KEYS
    assert catalog.secrets_to_drop(d365, None) == catalog.SECRET_KEYS
    assert catalog.secrets_to_drop(d365, {"oauth": {"connection_id": "c"}}) == catalog.SECRET_KEYS


@pytest.mark.parametrize("stored", [None, {}, {"oauth": {"connection_id": "c"}}])
def test_a_block_selecting_no_erp_yet_binds_nothing(stored):
    """Secrets PUT before the first configuration save belong to the ERP that
    save names; they are not dropped by it."""
    assert catalog.secrets_to_drop(stored, {"type": "xero", "integration_method": "direct"}) == (
        frozenset()
    )


def test_destination_keys_cover_every_host_naming_catalogue_field():
    """A catalogue field naming a host must be a destination key, or a stored
    secret could be redirected to it."""
    for entry in catalog.all_providers():
        for f in entry["fields"]:
            name = f["name"]
            if name.endswith("_url") or "host" in name or name == "url":
                assert name in catalog.DESTINATION_KEYS, name


# ---------- the endpoints ----------------------------------------------------


async def _seed_erp(realdb, block: dict, secrets: dict | None = None, key: str = "a") -> None:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        org.settings = {**(org.settings or {}), "erp": block}
        flag_modified(org, "settings")
        await s.commit()
    if secrets:
        await realdb.store_provider_secrets(key, "erp", secrets)


async def _stored_erp(realdb, key: str = "a") -> dict:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
    return dict((org.settings or {}).get("erp") or {})


async def _sealed(realdb, key: str = "a") -> dict:
    async with realdb.control_sessionmaker()() as s:
        return await provider_credentials.load_secrets(realdb.info(key).org_id, "erp", db=s)


async def _audit_rows(realdb, action: str) -> list[AuditLog]:
    async with realdb.sessionmaker("a")() as s:
        return list(
            (await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
        )


@pytest.mark.asyncio
async def test_providers_endpoint_is_admin_only(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/organization/erp/providers")
    assert resp.status_code == 200
    body = resp.json()
    by_key = {p["key"]: p for p in body["providers"]}
    assert by_key["netsuite"]["available"] is True
    assert by_key["merge_dev"]["plan"] == "scale"
    assert body["merge_dev_long_tail"]
    for role in ("ap_clerk", "ap_manager", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            assert (await c.get("/api/organization/erp/providers")).status_code == 403


@pytest.mark.asyncio
async def test_get_organization_never_returns_an_erp_secret_or_token(realdb):
    await _seed_erp(
        realdb,
        {**STORED, "oauth": {"provider": "xero", "connection_id": "conn-SECRET"}},
        {**STORED_SECRETS},
    )
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/organization")
        status = (await c.get("/api/organization/credentials")).json()
    assert resp.status_code == 200
    erp = resp.json()["settings"]["erp"]
    assert erp["oauth"] == {"connected": True}
    assert erp["account_id"] == "123"
    for value in ("cs-STORED", "ts-STORED", "whs-STORED", "conn-SECRET"):
        assert value not in resp.text
    # Which secrets are stored is reported by name, by the credentials endpoint.
    assert set(status["erp"]) == set(STORED_SECRETS)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap_clerk", "ap_manager", "cfo"])
async def test_non_admin_reads_only_the_routing_mode(realdb, role):
    await _seed_erp(realdb, {**STORED, "oauth": {"connection_id": "c"}}, {**STORED_SECRETS})
    async with realdb.client(key="a", role=role) as c:
        resp = await c.get("/api/organization")
    assert resp.status_code == 200
    assert resp.json()["settings"]["erp"] == {"integration_method": "direct"}
    assert "STORED" not in resp.text


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_patch_round_trip_keeps_sealed_secrets_and_the_oauth_block(realdb):
    oauth = {"provider": "netsuite", "connection_id": "c-KEEP"}
    await _seed_erp(realdb, {**STORED, "oauth": oauth}, {**STORED_SECRETS})
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        # The form sends back what it was shown, one field edited and the
        # secret inputs left blank ("leave blank to keep").
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {**shown, "token_id": "tid-2", "consumer_secret": ""}}},
        )
    assert resp.status_code == 200, resp.text
    stored = await _stored_erp(realdb)
    assert stored["token_id"] == "tid-2"
    assert stored["oauth"] == oauth
    assert set(stored).isdisjoint(catalog.SECRET_KEYS)
    assert await _sealed(realdb) == STORED_SECRETS
    # A configuration change is audited by key name.
    rows = await _audit_rows(realdb, "organization.provider_config_updated")
    assert [r.details for r in rows] == [{"block": "erp", "changed": ["token_id"]}]


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_patch_refuses_a_catalogue_secret_and_names_only_the_path(realdb):
    """A secret has one writer, `PUT /organization/credentials/erp`; the merge
    must not drop it silently and report the save as a success."""
    await _seed_erp(realdb, dict(SYSPRO), {**SYSPRO_SECRETS})
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {**SYSPRO, "operator_password": "opw-NEW-VALUE"}}},
        )
    assert resp.status_code == 422
    assert "operator_password" in resp.text and "opw-NEW-VALUE" not in resp.text
    assert await _sealed(realdb) == SYSPRO_SECRETS


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_patch_cannot_write_the_oauth_block(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={
                "settings": {
                    "erp": {
                        "type": "netsuite",
                        "integration_method": "direct",
                        "oauth": {"connection_id": "forged", "external_tenant_id": "evil"},
                    }
                }
            },
        )
    assert resp.status_code == 200, resp.text
    assert "oauth" not in await _stored_erp(realdb)


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_clearing_the_erp_keeps_the_oauth_block_and_drops_its_secrets(realdb):
    oauth = {"provider": "xero", "connection_id": "c-KEEP"}
    await _seed_erp(realdb, {**STORED, "oauth": oauth}, {**STORED_SECRETS})
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch("/api/organization", json={"settings": {"erp": None}})
    assert resp.status_code == 200, resp.text
    assert await _stored_erp(realdb) == {"oauth": oauth}
    assert await _sealed(realdb) == {}


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_patch_refuses_a_non_object_erp(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch("/api/organization", json={"settings": {"erp": "netsuite"}})
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_patch_with_a_changed_destination_drops_the_sealed_secrets(realdb):
    await _seed_erp(realdb, dict(STORED), {**STORED_SECRETS})
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {**shown, "account_id": "attacker"}}},
        )
    assert resp.status_code == 200, resp.text
    assert (await _stored_erp(realdb))["account_id"] == "attacker"
    # The inbound webhook HMAC key is never sent outbound, so it survives.
    assert await _sealed(realdb) == {"webhook_signing_secret": "whs-STORED"}
    rows = await _audit_rows(realdb, "organization.credentials_updated")
    assert len(rows) == 1
    assert rows[0].details == {
        "block": "erp",
        "changed": ["consumer_secret", "token_secret"],
        "cleared": ["consumer_secret", "token_secret"],
        "reason": "erp_destination_changed",
    }


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_patch_switching_erp_drops_every_sealed_secret(realdb):
    await _seed_erp(
        realdb,
        {"type": "dynamics_365_bc", "integration_method": "direct", "tenant_id": "t"},
        {"client_secret": "d365-STORED", "webhook_signing_secret": "whs-STORED"},
    )
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {"type": "xero", "integration_method": "direct"}}},
        )
    assert resp.status_code == 200, resp.text
    assert await _sealed(realdb) == {}


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_test_erp_uses_the_sealed_secrets_for_the_saved_connection(realdb, monkeypatch):
    """The form tests what it shows (secrets blank); the adapter gets the
    stored values."""
    from app.services.erp_adapters import netsuite

    seen: dict = {}

    async def fake_test_connection(self):
        seen.update(self.config)
        return True

    monkeypatch.setattr(netsuite.NetSuiteAdapter, "test_connection", fake_test_connection)
    await _seed_erp(realdb, dict(STORED), {**STORED_SECRETS})
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        resp = await c.post("/api/organization/test-erp", json={**shown, "consumer_secret": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    assert seen["consumer_secret"] == "cs-STORED"
    assert seen["token_secret"] == "ts-STORED"


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_test_erp_does_not_send_a_stored_secret_to_a_changed_base_url(realdb, monkeypatch):
    """The redirect attack: same type, attacker's base_url, secrets blank. The
    adapter must receive no stored secret at all."""
    from app.services.erp_adapters import syspro

    seen: dict = {}

    async def fake_test_connection(self):
        seen.update(self.config)
        return False

    monkeypatch.setattr(syspro.SysproAdapter, "test_connection", fake_test_connection)
    await _seed_erp(realdb, dict(SYSPRO), {**SYSPRO_SECRETS})
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        resp = await c.post(
            "/api/organization/test-erp", json={**shown, "base_url": "https://attacker.tld"}
        )
    assert resp.status_code == 200, resp.text
    assert seen["base_url"] == "https://attacker.tld"
    assert "operator_password" not in seen
    assert "company_password" not in seen
    assert "opw-STORED" not in str(seen) and "cpw-STORED" not in str(seen)


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_test_erp_keeps_stored_secrets_when_only_a_non_destination_field_changes(
    realdb, monkeypatch
):
    from app.services.erp_adapters import syspro

    seen: dict = {}

    async def fake_test_connection(self):
        seen.update(self.config)
        return True

    monkeypatch.setattr(syspro.SysproAdapter, "test_connection", fake_test_connection)
    await _seed_erp(realdb, dict(SYSPRO), {**SYSPRO_SECRETS})
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        resp = await c.post("/api/organization/test-erp", json={**shown, "posting_period": "P"})
    assert resp.status_code == 200, resp.text
    assert seen["operator_password"] == "opw-STORED"
    assert seen["posting_period"] == "P"


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_test_erp_reports_an_unopenable_store_and_never_falls_back(realdb, monkeypatch):
    from app.services.credential_crypto import CredentialCryptoError

    async def broken(*_a, **_k):
        raise CredentialCryptoError("open failed (kms)")

    monkeypatch.setattr(provider_credentials, "load_secrets", broken)
    await _seed_erp(realdb, dict(STORED))
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/organization/test-erp")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "success": False,
        "message": provider_credentials.CREDENTIALS_UNAVAILABLE_DETAIL,
    }


@pytest.mark.asyncio
@pytest.mark.plan("scale")
@pytest.mark.parametrize(
    "path", ["/api/vendors/sync-erp", "/api/gl-accounts/sync-erp", "/api/purchase-orders/sync-erp"]
)
async def test_a_sync_with_an_unopenable_store_is_a_503(realdb, monkeypatch, path):
    from app.services.credential_crypto import CredentialCryptoError

    async def broken(*_a, **_k):
        raise CredentialCryptoError("open failed (kms)")

    monkeypatch.setattr(provider_credentials, "load_secrets", broken)
    await _seed_erp(realdb, dict(STORED))
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(path)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == provider_credentials.CREDENTIALS_UNAVAILABLE_DETAIL
