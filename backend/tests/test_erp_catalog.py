"""The ERP provider catalogue and the write-only contract for `settings.erp`.

`erp_adapters/catalog` is the single source of truth for the Organization → ERP
form, and the authority for which `settings.erp` keys are secrets. Covers:

  * catalogue ↔ adapter-registry parity (both directions);
  * the catalogue's own shape (fields the frontend renders from);
  * `mask_erp_config` / `merge_erp_update` as pure functions;
  * the endpoints: `GET /organization/erp/providers` (admin only), masking on
    `GET /organization`, keep-on-blank / explicit replace / explicit clear on
    `PATCH`, the OAuth block preserved, the audit row, and `test-erp` filling a
    masked secret from the stored config.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.organization import Organization
from app.models.workflow import AuditLog
from app.services import erp_credentials
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


# ---------- the pure mask / merge --------------------------------------------

STORED = {
    "type": "netsuite",
    "integration_method": "direct",
    "account_id": "123",
    "consumer_key": "ck",
    "consumer_secret": "cs-STORED",
    "token_id": "tid",
    "token_secret": "ts-STORED",
    "webhook_signing_secret": "whs-STORED",
}


def test_mask_hides_every_secret_and_the_oauth_tokens():
    stored = {
        **STORED,
        "client_secret": "",
        "oauth": {"access_token": "at-SECRET", "refresh_token": "rt-SECRET"},
    }
    masked = catalog.mask_erp_config(stored)
    assert masked["consumer_secret"] == catalog.SECRET_MASK
    assert masked["token_secret"] == catalog.SECRET_MASK
    assert masked["webhook_signing_secret"] == catalog.SECRET_MASK
    assert masked["client_secret"] == ""  # never set reads as never set
    assert masked["oauth"] == {"connected": True}
    assert masked["account_id"] == "123"
    assert "SECRET" not in str(masked) and "STORED" not in str(masked)
    # Pure: the input is untouched.
    assert stored["oauth"]["access_token"] == "at-SECRET"


def test_mask_reports_a_tokenless_oauth_block_as_not_connected():
    assert catalog.mask_erp_config({"oauth": {}})["oauth"] == {"connected": False}
    assert "oauth" not in catalog.mask_erp_config({"type": "xero"})


@pytest.mark.parametrize("blank", ["", "   ", catalog.SECRET_MASK])
def test_merge_keeps_a_blank_or_masked_secret(blank):
    merged = catalog.merge_erp_update(
        STORED, {**catalog.mask_erp_config(STORED), "consumer_secret": blank, "token_id": "t2"}
    )
    assert merged["consumer_secret"] == "cs-STORED"
    assert merged["token_secret"] == "ts-STORED"
    assert merged["webhook_signing_secret"] == "whs-STORED"
    assert merged["token_id"] == "t2"


def test_merge_keeps_an_omitted_secret_for_the_same_erp():
    merged = catalog.merge_erp_update(
        STORED, {"type": "netsuite", "integration_method": "direct", "account_id": "123"}
    )
    assert merged["webhook_signing_secret"] == "whs-STORED"
    assert merged["consumer_secret"] == "cs-STORED"


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


def test_merge_takes_an_explicit_new_secret():
    merged = catalog.merge_erp_update(STORED, {**STORED, "consumer_secret": "cs-NEW"})
    assert merged["consumer_secret"] == "cs-NEW"


def test_merge_clears_a_secret_sent_as_null():
    merged = catalog.merge_erp_update(STORED, {**STORED, "token_secret": None})
    assert "token_secret" not in merged


def test_merge_does_not_carry_secrets_to_a_different_erp():
    """Business Central's client_secret must not become Xero's BYO app secret."""
    d365 = {"type": "dynamics_365_bc", "integration_method": "direct", "client_secret": "d365"}
    merged = catalog.merge_erp_update(
        d365, {"type": "xero", "integration_method": "direct", "client_secret": ""}
    )
    assert "client_secret" not in merged
    masked = catalog.merge_erp_update(
        d365, {"type": "xero", "integration_method": "direct", "client_secret": catalog.SECRET_MASK}
    )
    assert "client_secret" not in masked


SYSPRO_STORED = {
    "type": "syspro",
    "integration_method": "direct",
    "base_url": "https://syspro.example.com/Rest",
    "operator": "op",
    "operator_password": "opw-STORED",
    "company_id": "1",
    "company_password": "cpw-STORED",
    "webhook_signing_secret": "whs-STORED",
}


@pytest.mark.parametrize("blank", ["", catalog.SECRET_MASK, None])
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
def test_merge_does_not_carry_secrets_to_a_changed_destination(key, new_value, blank):
    """Same ERP type, new destination: a masked, blank or omitted secret is NOT
    carried — the stored password must never reach a host the save just named."""
    incoming = {**catalog.mask_erp_config(SYSPRO_STORED), key: new_value}
    if blank is None:
        incoming.pop("operator_password")
        incoming.pop("company_password")
    else:
        incoming["operator_password"] = blank
        incoming["company_password"] = blank
    merged = catalog.merge_erp_update(SYSPRO_STORED, incoming)
    assert "operator_password" not in merged
    assert "company_password" not in merged
    assert merged[key] == new_value
    # The inbound webhook HMAC key is never sent outbound, so it survives.
    assert merged["webhook_signing_secret"] == "whs-STORED"


def test_merge_takes_a_secret_retyped_for_a_changed_destination():
    merged = catalog.merge_erp_update(
        SYSPRO_STORED,
        {**SYSPRO_STORED, "base_url": "https://new.example.com", "operator_password": "opw-NEW"},
    )
    assert merged["operator_password"] == "opw-NEW"


def test_merge_keeps_secrets_while_the_destination_is_unchanged():
    """Blank vs absent and surrounding whitespace are the same destination."""
    incoming = {
        **catalog.mask_erp_config(SYSPRO_STORED),
        "base_url": " https://syspro.example.com/Rest ",
        "environment": "",
        "posting_period": "C",
    }
    merged = catalog.merge_erp_update(SYSPRO_STORED, incoming)
    assert merged["operator_password"] == "opw-STORED"
    assert merged["company_password"] == "cpw-STORED"


def test_destination_keys_cover_every_host_naming_catalogue_field():
    """A catalogue field naming a host must be a destination key, or a stored
    secret could be redirected to it."""
    for entry in catalog.all_providers():
        for f in entry["fields"]:
            name = f["name"]
            if name.endswith("_url") or "host" in name or name == "url":
                assert name in catalog.DESTINATION_KEYS, name


def test_merge_never_takes_oauth_from_the_request_and_keeps_the_stored_block():
    stored = {"type": "xero", "integration_method": "direct", "oauth": {"refresh_token": "rt"}}
    merged = catalog.merge_erp_update(
        stored,
        {"type": "xero", "integration_method": "direct", "oauth": {"refresh_token": "forged"}},
    )
    assert merged["oauth"] == {"refresh_token": "rt"}
    fresh = catalog.merge_erp_update({}, {"type": "xero", "oauth": {"refresh_token": "forged"}})
    assert "oauth" not in fresh


def test_changed_keys_are_names_only():
    after = catalog.merge_erp_update(STORED, {**STORED, "consumer_secret": "cs-NEW"})
    assert catalog.changed_keys(STORED, after) == ["consumer_secret"]


# ---------- the endpoints ----------------------------------------------------


async def _seed_erp(realdb, block: dict, key: str = "a") -> None:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        org.settings = {**(org.settings or {}), "erp": block}
        flag_modified(org, "settings")
        await s.commit()


async def _stored_erp(realdb, key: str = "a") -> dict:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
    return dict((org.settings or {}).get("erp") or {})


def _plain(stored: dict) -> dict:
    """``stored`` with every secret and token decrypted — after asserting each
    one actually IS a ciphertext in the row (nothing saved stays plaintext)."""
    assert not erp_credentials.has_plaintext(stored), "a credential was stored in plaintext"
    out = erp_credentials.decrypt_erp_config(stored)
    if "oauth" in out:
        out["oauth"] = erp_credentials.decrypt_oauth_tokens(out["oauth"])
    return out


@pytest.mark.asyncio
async def test_providers_endpoint_is_admin_only(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/organization/erp/providers")
    assert resp.status_code == 200
    body = resp.json()
    by_key = {p["key"]: p for p in body["providers"]}
    assert by_key["netsuite"]["available"] is True
    assert by_key["merge_dev"]["plan"] == "scale"
    assert body["secret_mask"] == catalog.SECRET_MASK
    assert body["merge_dev_long_tail"]
    for role in ("ap_clerk", "ap_manager", "cfo"):
        async with realdb.client(key="a", role=role) as c:
            assert (await c.get("/api/organization/erp/providers")).status_code == 403


@pytest.mark.asyncio
async def test_get_organization_masks_erp_secrets_for_an_admin(realdb):
    await _seed_erp(
        realdb, {**STORED, "oauth": {"access_token": "at-SECRET", "refresh_token": "rt-SECRET"}}
    )
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/organization")
    assert resp.status_code == 200
    erp = resp.json()["settings"]["erp"]
    assert erp["consumer_secret"] == catalog.SECRET_MASK
    assert erp["oauth"] == {"connected": True}
    for value in ("cs-STORED", "ts-STORED", "whs-STORED", "at-SECRET", "rt-SECRET"):
        assert value not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap_clerk", "ap_manager", "cfo"])
async def test_non_admin_reads_only_the_routing_mode(realdb, role):
    await _seed_erp(realdb, {**STORED, "oauth": {"refresh_token": "rt-SECRET"}})
    async with realdb.client(key="a", role=role) as c:
        resp = await c.get("/api/organization")
    assert resp.status_code == 200
    assert resp.json()["settings"]["erp"] == {"integration_method": "direct"}
    assert "STORED" not in resp.text and "rt-SECRET" not in resp.text


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_patch_round_trip_keeps_masked_secrets_and_the_oauth_block(realdb):
    await _seed_erp(realdb, {**STORED, "oauth": {"refresh_token": "rt-KEEP"}})
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        # The form sends back exactly what it was shown, one field edited.
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {**shown, "token_id": "tid-2", "consumer_secret": ""}}},
        )
    assert resp.status_code == 200, resp.text
    assert "cs-STORED" not in resp.text
    stored = _plain(await _stored_erp(realdb))
    assert stored["token_id"] == "tid-2"
    assert stored["consumer_secret"] == "cs-STORED"
    assert stored["token_secret"] == "ts-STORED"
    assert stored["webhook_signing_secret"] == "whs-STORED"
    assert stored["oauth"] == {"refresh_token": "rt-KEEP"}


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_patch_replaces_a_secret_explicitly_and_audits_names_only(realdb):
    await _seed_erp(realdb, dict(STORED))
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {**STORED, "token_secret": "ts-NEW-VALUE"}}},
        )
    assert resp.status_code == 200, resp.text
    assert "ts-NEW-VALUE" not in resp.text
    assert _plain(await _stored_erp(realdb))["token_secret"] == "ts-NEW-VALUE"

    async with realdb.sessionmaker("a")() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "organization.erp_updated")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].details["changed"] == ["token_secret"]
    assert rows[0].details["type"] == "netsuite"
    assert "ts-NEW-VALUE" not in str(rows[0].details)


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
                        "oauth": {"refresh_token": "forged", "external_tenant_id": "evil"},
                    }
                }
            },
        )
    assert resp.status_code == 200, resp.text
    assert "oauth" not in await _stored_erp(realdb)


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_clearing_the_erp_keeps_the_oauth_block(realdb):
    await _seed_erp(realdb, {**STORED, "oauth": {"refresh_token": "rt-KEEP"}})
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch("/api/organization", json={"settings": {"erp": None}})
    assert resp.status_code == 200, resp.text
    assert _plain(await _stored_erp(realdb)) == {"oauth": {"refresh_token": "rt-KEEP"}}


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_patch_refuses_a_non_object_erp(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch("/api/organization", json={"settings": {"erp": "netsuite"}})
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_test_erp_fills_masked_secrets_from_the_stored_config(realdb, monkeypatch):
    """The form tests what it shows; a masked secret must reach the adapter as
    the stored value, never as the mask."""
    from app.services.erp_adapters import netsuite

    seen: dict = {}

    async def fake_test_connection(self):
        seen.update(self.config)
        return True

    monkeypatch.setattr(netsuite.NetSuiteAdapter, "test_connection", fake_test_connection)
    await _seed_erp(realdb, dict(STORED))
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        resp = await c.post("/api/organization/test-erp", json=shown)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    assert seen["consumer_secret"] == "cs-STORED"
    assert seen["token_secret"] == "ts-STORED"


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_test_erp_does_not_send_a_stored_secret_to_a_changed_base_url(realdb, monkeypatch):
    """The redirect attack: same type, attacker's base_url, masked password.
    The adapter must receive no stored secret at all."""
    from app.services.erp_adapters import syspro

    seen: dict = {}

    async def fake_test_connection(self):
        seen.update(self.config)
        return False

    monkeypatch.setattr(syspro.SysproAdapter, "test_connection", fake_test_connection)
    await _seed_erp(realdb, dict(SYSPRO_STORED))
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
async def test_test_erp_keeps_stored_secrets_for_an_unchanged_destination(realdb, monkeypatch):
    from app.services.erp_adapters import syspro

    seen: dict = {}

    async def fake_test_connection(self):
        seen.update(self.config)
        return True

    monkeypatch.setattr(syspro.SysproAdapter, "test_connection", fake_test_connection)
    await _seed_erp(realdb, dict(SYSPRO_STORED))
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        resp = await c.post("/api/organization/test-erp", json=shown)
    assert resp.status_code == 200, resp.text
    assert seen["operator_password"] == "opw-STORED"


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_patch_with_a_changed_destination_drops_masked_secrets(realdb):
    await _seed_erp(realdb, dict(STORED))
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {**shown, "account_id": "attacker"}}},
        )
    assert resp.status_code == 200, resp.text
    stored = await _stored_erp(realdb)
    assert stored["account_id"] == "attacker"
    assert "consumer_secret" not in stored
    assert "token_secret" not in stored
    assert stored["webhook_signing_secret"] == "whs-STORED"
