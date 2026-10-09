"""ERP credentials are encrypted at rest (`utils/credential_crypto`, `services/erp_credentials`).

Covers:

  * the primitive: round trip, fresh nonce per value, tamper / wrong-field /
    malformed / unknown-key refusals, the keyring parser and the boot check;
  * the empty keyring failing closed — in a deployed environment too — while
    legacy plaintext stays readable;
  * key rotation: decrypt under the old key id, re-seal under the new one, and
    a dropped key making its ciphertexts an error rather than garbage;
  * the `settings.erp` seams: what is encrypted, idempotence, the masked read
    unchanged, `get_erp_adapter` handing the adapter plaintext;
  * the endpoints: a save stores no plaintext, `/test-erp` decrypts, a save with
    no keyring is refused and stores nothing;
  * the inbound ERP webhook verifying against an encrypted signing secret;
  * migration 0110 encrypting legacy plaintext, idempotently, and reversing.

The OAuth token seams (connect, refresh, revoke) are covered in
`test_erp_oauth.py` beside the flow they belong to.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import Settings, settings
from app.models.organization import Organization
from app.services import erp_credentials
from app.services.erp_adapters import catalog, get_erp_adapter
from app.utils import credential_crypto
from app.utils.credential_crypto import (
    CredentialDecryptError,
    CredentialKeyMissingError,
    parse_keyring,
)
from tests.conftest import TEST_CREDENTIAL_KEYS


def _key(seed: str) -> str:
    return base64.b64encode(seed.encode().ljust(32, b"0")[:32]).decode()


K1 = f"k1:{_key('key-one')}"
K2 = f"k2:{_key('key-two')}"

NETSUITE = {
    "type": "netsuite",
    "integration_method": "direct",
    "account_id": "123",
    "consumer_key": "ck",
    "consumer_secret": "cs-PLAIN",
    "token_id": "tid",
    "token_secret": "ts-PLAIN",
    "webhook_signing_secret": "whs-PLAIN",
}


@pytest.fixture
def keyring(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(settings, "credential_encryption_keys", value)

    return _set


# ---------- the primitive -----------------------------------------------------


def test_round_trip_and_format():
    sealed = credential_crypto.encrypt("s3cret", context="erp.client_secret")
    assert sealed.startswith("enc:v1:dev1:")
    assert "s3cret" not in sealed
    assert credential_crypto.decrypt(sealed, context="erp.client_secret") == "s3cret"


def test_each_encryption_uses_a_fresh_nonce():
    a = credential_crypto.encrypt("same", context="erp.api_key")
    b = credential_crypto.encrypt("same", context="erp.api_key")
    assert a != b


def test_unicode_round_trips():
    sealed = credential_crypto.encrypt("pässwörd-✓", context="erp.password")
    assert credential_crypto.decrypt(sealed, context="erp.password") == "pässwörd-✓"


def _flip_one_byte(sealed: str) -> str:
    prefix, body = sealed.rsplit(":", 1)
    raw = bytearray(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    raw[-1] ^= 0x01
    return f"{prefix}:{base64.urlsafe_b64encode(bytes(raw)).rstrip(b'=').decode()}"


def test_a_tampered_ciphertext_is_an_error_not_garbage():
    sealed = credential_crypto.encrypt("s3cret", context="erp.client_secret")
    with pytest.raises(CredentialDecryptError, match="failed authentication"):
        credential_crypto.decrypt(_flip_one_byte(sealed), context="erp.client_secret")


def test_a_ciphertext_moved_to_another_field_does_not_decrypt():
    sealed = credential_crypto.encrypt("s3cret", context="erp.client_secret")
    with pytest.raises(CredentialDecryptError):
        credential_crypto.decrypt(sealed, context="erp.oauth.refresh_token")


@pytest.mark.parametrize(
    "bad", ["enc:v1:", "enc:v1:dev1", "enc:v1:dev1:", "enc:v1:dev1:AAAA", "enc:v1:dev1:!!!!"]
)
def test_malformed_ciphertexts_are_errors(bad):
    with pytest.raises(CredentialDecryptError):
        credential_crypto.decrypt(bad, context="erp.api_key")


def test_errors_never_carry_the_value():
    sealed = credential_crypto.encrypt("TOP-SECRET-VALUE", context="erp.api_key")
    with pytest.raises(CredentialDecryptError) as exc:
        credential_crypto.decrypt(_flip_one_byte(sealed), context="erp.api_key")
    assert "TOP-SECRET-VALUE" not in str(exc.value)
    assert sealed not in str(exc.value)


def test_legacy_plaintext_and_non_strings_pass_through_decrypt():
    assert credential_crypto.decrypt("legacy-plain", context="erp.api_key") == "legacy-plain"
    assert credential_crypto.decrypt(None, context="erp.api_key") is None
    assert credential_crypto.decrypt("", context="erp.api_key") == ""


def test_encrypting_an_existing_ciphertext_keeps_it_byte_for_byte():
    sealed = credential_crypto.encrypt("s3cret", context="erp.api_key")
    assert credential_crypto.encrypt(sealed, context="erp.api_key") == sealed


def test_a_forged_ciphertext_cannot_be_stored():
    """A value posing as `enc:v1:` must decrypt for its field to be kept."""
    with pytest.raises(CredentialDecryptError):
        credential_crypto.encrypt("enc:v1:dev1:Zm9yZ2VkLWJ5LWFuLWFkbWlu", context="erp.api_key")


# ---------- the keyring -------------------------------------------------------


def test_parse_keyring_first_entry_is_active():
    ring = parse_keyring(f"{K2},{K1}")
    assert ring.active_id == "k2"
    assert set(ring.keys) == {"k1", "k2"}


def test_parse_keyring_accepts_standard_and_urlsafe_base64():
    raw = bytes(range(250, 256)) * 5 + b"\xfb\xff"  # 32 bytes; forces '+' / '/' in standard b64
    std = base64.b64encode(raw).decode()
    assert "+" in std or "/" in std
    assert parse_keyring(f"x:{std}").keys["x"] == raw
    assert parse_keyring(f"x:{base64.urlsafe_b64encode(raw).decode()}").keys["x"] == raw


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_parse_keyring_empty_is_none(empty):
    assert parse_keyring(empty) is None


@pytest.mark.parametrize(
    "bad",
    [
        "no-colon-here",
        f"bad id:{_key('x')}",
        "k1:dG9vLXNob3J0",  # "too-short"
        f"k1:{_key('a')},k1:{_key('b')}",
        f"{'x' * 33}:{_key('a')}",
    ],
)
def test_parse_keyring_rejects_malformed_values_without_echoing_a_key(bad):
    with pytest.raises(ValueError) as exc:
        parse_keyring(bad)
    assert _key("a") not in str(exc.value)


def test_a_malformed_keyring_refuses_boot():
    with pytest.raises(ValidationError, match="FEOH_CREDENTIAL_ENCRYPTION_KEYS"):
        Settings(credential_encryption_keys="k1:not-32-bytes")


def test_an_empty_keyring_boots():
    assert Settings(credential_encryption_keys="").credential_encryption_keys == ""


@pytest.mark.parametrize("environment", ["development", "production"])
def test_an_empty_keyring_refuses_to_store_or_read(keyring, monkeypatch, environment):
    sealed = credential_crypto.encrypt("s3cret", context="erp.api_key")
    monkeypatch.setattr(settings, "environment", environment)
    keyring("")
    with pytest.raises(CredentialKeyMissingError, match="FEOH_CREDENTIAL_ENCRYPTION_KEYS"):
        credential_crypto.encrypt("s3cret", context="erp.api_key")
    with pytest.raises(CredentialKeyMissingError):
        credential_crypto.decrypt(sealed, context="erp.api_key")
    with pytest.raises(CredentialKeyMissingError):
        erp_credentials.encrypt_erp_config(dict(NETSUITE))
    # Legacy plaintext is still readable: refusing it would not make it secret.
    assert erp_credentials.decrypt_erp_config(dict(NETSUITE)) == NETSUITE


def test_a_block_with_no_secret_needs_no_keyring(keyring):
    keyring("")
    block = {"type": "mock", "integration_method": "direct"}
    assert erp_credentials.encrypt_erp_config(block) == block


# ---------- rotation ----------------------------------------------------------


def test_rotation_decrypts_under_the_old_key_id_and_reseals_under_the_new(keyring):
    keyring(K1)
    old = credential_crypto.encrypt("rotate-me", context="erp.api_key")
    assert credential_crypto.key_id_of(old) == "k1"

    keyring(f"{K2},{K1}")
    assert credential_crypto.active_key_id() == "k2"
    assert credential_crypto.decrypt(old, context="erp.api_key") == "rotate-me"
    assert credential_crypto.key_id_of(credential_crypto.encrypt("x", context="c")) == "k2"
    # Kept as it is by a save (encrypt is idempotent)...
    assert credential_crypto.encrypt(old, context="erp.api_key") == old
    # ...and moved by the rotation step.
    resealed = credential_crypto.reencrypt(old, context="erp.api_key")
    assert credential_crypto.key_id_of(resealed) == "k2"
    assert credential_crypto.reencrypt(resealed, context="erp.api_key") == resealed

    keyring(K2)  # the old key dropped
    assert credential_crypto.decrypt(resealed, context="erp.api_key") == "rotate-me"
    with pytest.raises(CredentialDecryptError, match="'k1'"):
        credential_crypto.decrypt(old, context="erp.api_key")


def test_reencrypt_erp_config_moves_every_credential_to_the_active_key(keyring):
    keyring(K1)
    stored = erp_credentials.encrypt_erp_config(
        {**NETSUITE, "oauth": {"access_token": "at", "refresh_token": "rt", "org_id": "o"}}
    )
    keyring(f"{K2},{K1}")
    resealed = erp_credentials.reencrypt_erp_config(stored)
    for key in ("consumer_secret", "token_secret", "webhook_signing_secret"):
        assert credential_crypto.key_id_of(resealed[key]) == "k2"
    for key in ("access_token", "refresh_token"):
        assert credential_crypto.key_id_of(resealed["oauth"][key]) == "k2"
    assert resealed["oauth"]["org_id"] == "o"
    keyring(K2)
    plain = erp_credentials.decrypt_erp_config(resealed)
    assert plain["consumer_secret"] == "cs-PLAIN"
    assert erp_credentials.decrypt_oauth_tokens(resealed["oauth"])["refresh_token"] == "rt"


# ---------- the settings.erp seams --------------------------------------------


def test_encrypt_erp_config_encrypts_secrets_and_tokens_only():
    block = {
        **NETSUITE,
        "oauth": {
            "provider": "quickbooks_online",
            "access_token": "at-PLAIN",
            "refresh_token": "rt-PLAIN",
            "connection_id": "conn",
            "org_id": "org",
        },
    }
    stored = erp_credentials.encrypt_erp_config(block)
    for key in ("consumer_secret", "token_secret", "webhook_signing_secret"):
        assert credential_crypto.is_encrypted(stored[key])
    for key in ("type", "integration_method", "account_id", "consumer_key", "token_id"):
        assert stored[key] == block[key]
    assert credential_crypto.is_encrypted(stored["oauth"]["access_token"])
    assert credential_crypto.is_encrypted(stored["oauth"]["refresh_token"])
    assert stored["oauth"]["connection_id"] == "conn"
    assert stored["oauth"]["org_id"] == "org"
    assert not erp_credentials.has_plaintext(stored)
    assert erp_credentials.has_plaintext(block)
    assert "PLAIN" not in json.dumps(stored)
    # The input is not mutated.
    assert block["consumer_secret"] == "cs-PLAIN"
    # Idempotent: a save that keeps everything changes nothing.
    assert erp_credentials.encrypt_erp_config(stored) == stored


def test_every_catalogue_secret_is_encrypted():
    block = {key: f"{key}-PLAIN" for key in catalog.SECRET_KEYS}
    stored = erp_credentials.encrypt_erp_config(block)
    assert not erp_credentials.has_plaintext(stored)
    assert erp_credentials.decrypt_erp_config(stored) == block


def test_empty_secrets_stay_empty():
    stored = erp_credentials.encrypt_erp_config({"api_key": "", "password": None})
    assert stored == {"api_key": "", "password": None}


def test_the_masked_read_is_unchanged_by_encryption():
    block = {**NETSUITE, "client_secret": "", "oauth": {"refresh_token": "rt"}}
    stored = erp_credentials.encrypt_erp_config(block)
    assert catalog.mask_erp_config(stored) == catalog.mask_erp_config(block)
    masked = catalog.mask_erp_config(stored)
    assert masked["consumer_secret"] == catalog.SECRET_MASK
    assert masked["client_secret"] == ""
    assert masked["oauth"] == {"connected": True}
    assert "enc:v1" not in json.dumps(masked)


def test_get_erp_adapter_receives_decrypted_values():
    stored = erp_credentials.encrypt_erp_config(dict(NETSUITE))
    adapter = get_erp_adapter(stored)
    assert adapter.config["consumer_secret"] == "cs-PLAIN"
    assert adapter.config["token_secret"] == "ts-PLAIN"
    # The stored dict itself is untouched (still ciphertext).
    assert credential_crypto.is_encrypted(stored["consumer_secret"])


def test_get_erp_adapter_refuses_a_tampered_credential():
    stored = erp_credentials.encrypt_erp_config(dict(NETSUITE))
    stored["consumer_secret"] = _flip_one_byte(stored["consumer_secret"])
    with pytest.raises(CredentialDecryptError, match="erp.consumer_secret"):
        get_erp_adapter(stored)


def test_get_erp_adapter_still_reads_legacy_plaintext():
    adapter = get_erp_adapter(dict(NETSUITE))
    assert adapter.config["consumer_secret"] == "cs-PLAIN"


# ---------- the endpoints (realdb) --------------------------------------------


async def _seed_erp(realdb, block: dict | None, key: str = "a") -> None:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        settings_ = dict(org.settings or {})
        if block is None:
            settings_.pop("erp", None)
        else:
            settings_["erp"] = block
        org.settings = settings_
        flag_modified(org, "settings")
        await s.commit()


async def _stored_erp(realdb, key: str = "a") -> dict | None:
    async with realdb.control_sessionmaker()() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
    return (org.settings or {}).get("erp")


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_a_save_stores_no_plaintext_and_reads_back_masked(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch("/api/organization", json={"settings": {"erp": dict(NETSUITE)}})
        assert resp.status_code == 200, resp.text
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
    stored = await _stored_erp(realdb)
    assert not erp_credentials.has_plaintext(stored)
    assert "PLAIN" not in json.dumps(stored)
    assert erp_credentials.decrypt_erp_config(stored)["token_secret"] == "ts-PLAIN"
    assert shown["consumer_secret"] == catalog.SECRET_MASK
    assert "enc:v1" not in json.dumps(shown)


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_a_save_that_keeps_secrets_leaves_their_ciphertext_and_audit_alone(realdb):
    async with realdb.client(key="a", role="admin") as c:
        await c.patch("/api/organization", json={"settings": {"erp": dict(NETSUITE)}})
        first = await _stored_erp(realdb)
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        resp = await c.patch(
            "/api/organization", json={"settings": {"erp": {**shown, "token_id": "tid-2"}}}
        )
    assert resp.status_code == 200, resp.text
    second = await _stored_erp(realdb)
    assert second["token_id"] == "tid-2"
    for key in ("consumer_secret", "token_secret", "webhook_signing_secret"):
        assert second[key] == first[key]

    from app.models.workflow import AuditLog

    async with realdb.sessionmaker("a")() as s:
        rows = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "organization.erp_updated")))
            .scalars()
            .all()
        )
    # Two saves, two rows; the second names only what changed by value.
    assert len(rows) == 2
    assert ["token_id"] in [r.details["changed"] for r in rows]


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_a_save_encrypts_legacy_plaintext_it_keeps(realdb):
    await _seed_erp(realdb, dict(NETSUITE))
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        resp = await c.patch("/api/organization", json={"settings": {"erp": shown}})
    assert resp.status_code == 200, resp.text
    stored = await _stored_erp(realdb)
    assert not erp_credentials.has_plaintext(stored)
    assert erp_credentials.decrypt_erp_config(stored)["consumer_secret"] == "cs-PLAIN"


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_a_save_with_no_keyring_is_refused_and_stores_nothing(realdb, keyring):
    await _seed_erp(realdb, None)
    keyring("")
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch("/api/organization", json={"settings": {"erp": dict(NETSUITE)}})
    assert resp.status_code == 503
    assert "FEOH_CREDENTIAL_ENCRYPTION_KEYS" in resp.json()["detail"]
    assert "PLAIN" not in resp.text
    assert await _stored_erp(realdb) is None


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_a_save_carrying_a_forged_ciphertext_is_refused(realdb):
    await _seed_erp(realdb, None)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {**NETSUITE, "token_secret": "enc:v1:dev1:Zm9yZ2Vk"}}},
        )
    assert resp.status_code == 422
    assert await _stored_erp(realdb) is None


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_test_erp_decrypts_the_stored_credentials(realdb, monkeypatch):
    from app.services.erp_adapters import netsuite

    seen: dict = {}

    async def fake_test_connection(self):
        seen.update(self.config)
        return True

    monkeypatch.setattr(netsuite.NetSuiteAdapter, "test_connection", fake_test_connection)
    await _seed_erp(realdb, erp_credentials.encrypt_erp_config(dict(NETSUITE)))
    async with realdb.client(key="a", role="admin") as c:
        shown = (await c.get("/api/organization")).json()["settings"]["erp"]
        masked = await c.post("/api/organization/test-erp", json=shown)
        saved = await c.post("/api/organization/test-erp", json={})
    assert masked.json()["success"] is True and saved.json()["success"] is True
    assert seen["consumer_secret"] == "cs-PLAIN"
    assert seen["token_secret"] == "ts-PLAIN"


@pytest.mark.asyncio
@pytest.mark.plan("scale")
async def test_test_erp_reports_an_undecryptable_credential(realdb, keyring):
    await _seed_erp(realdb, erp_credentials.encrypt_erp_config(dict(NETSUITE)))
    keyring(K2)  # the key that sealed them is gone
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post("/api/organization/test-erp", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "could not be decrypted" in body["message"]
    assert "enc:v1" not in resp.text


@pytest.mark.asyncio
@pytest.mark.plan("scale")
@pytest.mark.parametrize(
    "path",
    ["/api/vendors/sync-erp", "/api/purchase-orders/sync-erp", "/api/gl-accounts/sync-erp"],
)
async def test_a_sync_with_an_undecryptable_credential_is_a_clear_409(realdb, keyring, path):
    await _seed_erp(realdb, erp_credentials.encrypt_erp_config(dict(NETSUITE)))
    keyring(K2)  # the key that sealed them is gone
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.post(path)
    assert resp.status_code == 409, resp.text
    assert "could not be decrypted" in resp.json()["detail"]
    assert "enc:v1" not in resp.text and "PLAIN" not in resp.text


# ---------- the inbound ERP webhook -------------------------------------------


@pytest.mark.asyncio
async def test_webhook_verifies_against_an_encrypted_signing_secret(fake_redis):
    from types import SimpleNamespace

    from app.models.invoice import InvoiceStatus
    from tests.test_erp_webhook_dedup_regression import _post

    secret = erp_credentials.encrypt_secret("whs-PLAIN", key="webhook_signing_secret")
    org = SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        db_name="feoh_acme",
        settings={"erp": {"webhook_signing_secret": secret}},
    )
    correlation_id = str(uuid.uuid4())
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.UUID(correlation_id), status=InvoiceStatus.sent_to_erp
    )
    calls = await _post(
        org=org,
        invoice=invoice,
        status_value="posted",
        correlation_id=correlation_id,
        erp_document_id="doc_1",
        secret="whs-PLAIN",
    )
    assert [c["target"] for c in calls] == [InvoiceStatus.posted_in_erp]

    # Signed with the CIPHERTEXT (what a reader of the row would have): refused.
    invoice.status = InvoiceStatus.sent_to_erp
    calls = await _post(
        org=org,
        invoice=invoice,
        status_value="posted",
        correlation_id=correlation_id,
        erp_document_id="doc_2",
        secret=secret,
    )
    assert calls == []


@pytest.mark.asyncio
async def test_webhook_with_an_undecryptable_secret_is_dropped(fake_redis):
    from types import SimpleNamespace

    from app.models.invoice import InvoiceStatus
    from tests.test_erp_webhook_dedup_regression import _post

    secret = _flip_one_byte(
        erp_credentials.encrypt_secret("whs-PLAIN", key="webhook_signing_secret")
    )
    org = SimpleNamespace(
        id=uuid.uuid4(),
        slug="acme",
        db_name="feoh_acme",
        settings={"erp": {"webhook_signing_secret": secret}},
    )
    correlation_id = str(uuid.uuid4())
    invoice = SimpleNamespace(
        id=uuid.uuid4(), correlation_id=uuid.UUID(correlation_id), status=InvoiceStatus.sent_to_erp
    )
    calls = await _post(
        org=org,
        invoice=invoice,
        status_value="posted",
        correlation_id=correlation_id,
        erp_document_id="doc_1",
        secret="whs-PLAIN",
    )
    assert calls == []


@pytest.fixture
def fake_redis(monkeypatch):
    from tests.test_erp_webhook_dedup_regression import _FakeRedis

    fake = _FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr("app.services.webhook_security.get_redis", _get_redis)
    return fake


# ---------- migration 0110 ----------------------------------------------------

REVISION = (
    Path(__file__).resolve().parents[1] / "alembic/versions/0110_erp_credentials_encrypted.py"
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("_mig_0110", REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run_revision(realdb, fn) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = realdb.control_sessionmaker().kw["bind"]
    async with engine.begin() as conn:

        def _do(sync_conn):
            with Operations.context(MigrationContext.configure(sync_conn)):
                fn()

        await conn.run_sync(_do)


def test_the_revision_freezes_the_current_secret_keys():
    assert set(_load_revision()._SECRET_KEYS) == set(catalog.SECRET_KEYS)


@pytest.mark.asyncio
async def test_0110_encrypts_legacy_plaintext_idempotently_and_reverses(realdb):
    mig = _load_revision()
    legacy = {
        **NETSUITE,
        "oauth": {"provider": "quickbooks_online", "access_token": "at", "refresh_token": "rt"},
    }
    already = erp_credentials.encrypt_erp_config({"type": "sage_accounting_za", "api_key": "k"})
    await _seed_erp(realdb, legacy, key="a")
    await _seed_erp(realdb, already, key="b")

    await _run_revision(realdb, mig.upgrade)
    a = await _stored_erp(realdb, "a")
    assert not erp_credentials.has_plaintext(a)
    assert "PLAIN" not in json.dumps(a)
    assert erp_credentials.decrypt_erp_config(a)["consumer_secret"] == "cs-PLAIN"
    assert erp_credentials.decrypt_oauth_tokens(a["oauth"])["refresh_token"] == "rt"
    assert a["account_id"] == "123" and a["oauth"]["provider"] == "quickbooks_online"
    # An already-encrypted row is left byte-for-byte as it was.
    assert await _stored_erp(realdb, "b") == already

    # Re-running changes nothing.
    await _run_revision(realdb, mig.upgrade)
    assert await _stored_erp(realdb, "a") == a

    await _run_revision(realdb, mig.downgrade)
    assert await _stored_erp(realdb, "a") == legacy
    await _run_revision(realdb, mig.upgrade)  # leave the row upgraded
    assert not erp_credentials.has_plaintext(await _stored_erp(realdb, "a"))


@pytest.mark.asyncio
async def test_0110_refuses_plaintext_with_no_keyring_and_writes_nothing(realdb, keyring):
    mig = _load_revision()
    await _seed_erp(realdb, dict(NETSUITE))
    keyring("")
    with pytest.raises(CredentialKeyMissingError):
        await _run_revision(realdb, mig.upgrade)
    assert await _stored_erp(realdb) == NETSUITE


@pytest.mark.asyncio
async def test_0110_needs_no_keyring_when_nothing_is_plaintext(realdb, keyring):
    mig = _load_revision()
    await _seed_erp(realdb, {"type": "mock", "integration_method": "direct"})
    keyring("")
    await _run_revision(realdb, mig.upgrade)
    assert await _stored_erp(realdb) == {"type": "mock", "integration_method": "direct"}


def test_the_suite_keyring_is_the_committed_dev_key():
    """`.env.development` and the pytest keyring must agree, so a value a test
    seeds reads the same under `pnpm dev`."""
    env = (Path(__file__).resolve().parents[1] / ".env.development").read_text()
    assert f"FEOH_CREDENTIAL_ENCRYPTION_KEYS={TEST_CREDENTIAL_KEYS}\n" in env
