"""Provider credentials are sealed, written once through an audited door, never read back.

The ERP, payment-rail and card-issuer secrets used to sit in
`Organization.settings` as plain JSONB and came back verbatim on the admin
settings page. They now live envelope-encrypted in `provider_credentials`
(`services/credential_crypto`, `services/provider_credentials`):

* the crypto — local provider round-trips, binds each ciphertext to its org and
  block, refuses itself in a deployed env; the KMS provider passes the
  encryption context to KMS and caches the unwrapped data key;
* the shape helpers — what counts as a secret, including multi-route entries;
* the endpoint — `PUT /api/organization/credentials/{block}` is admin-only,
  audit-first, blank-keeps, never echoes a value; `GET` reports names only;
* the PATCH guard — a secret in the generic settings merge is refused;
* the accessor — adapters get configuration + secrets; a stray plaintext
  value in the JSONB is ignored;
* connection tests — a stored secret never follows an unsaved base URL;
* migration 0110 — moves plaintext out of the JSONB, sealed, idempotently.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.models.organization import Organization
from app.models.provider_credential import ProviderCredential
from app.models.workflow import AuditLog
from app.services import credential_crypto, provider_credentials
from app.services.credential_crypto import CredentialCryptoError
from app.services.provider_credentials import (
    CredentialPathError,
    config_for_connection_test,
    extract_secrets,
    inject_secrets,
    strip_secrets,
    validate_path,
    validate_update,
)

SECRET = "sk_live_must-never-echo-0001"
SECRET_2 = "sk_live_must-never-echo-0002"
CTX = {"purpose": "t", "organization_id": "o1", "block": "erp"}


# ---------- crypto: local provider ------------------------------------------


def test_local_round_trip_and_key_recorded():
    env = credential_crypto.seal_sync(b"hello", CTX)
    assert env.key_provider == credential_crypto.KEY_PROVIDER_LOCAL
    assert env.key_id.startswith("local:")
    assert b"hello" not in env.ciphertext
    assert credential_crypto.open_sync(env, CTX) == b"hello"


def test_two_seals_of_one_value_differ():
    a = credential_crypto.seal_sync(b"same", CTX)
    b = credential_crypto.seal_sync(b"same", CTX)
    assert a.ciphertext != b.ciphertext and a.wrapped_key != b.wrapped_key


@pytest.mark.parametrize("field", ["organization_id", "block"])
def test_ciphertext_is_bound_to_its_org_and_block(field):
    """A row copied onto another org (or block) must not open as that org's key."""
    env = credential_crypto.seal_sync(SECRET.encode(), CTX)
    with pytest.raises(CredentialCryptoError) as exc:
        credential_crypto.open_sync(env, {**CTX, field: "other"})
    assert SECRET not in str(exc.value)


def test_tampered_ciphertext_fails_closed():
    env = credential_crypto.seal_sync(b"x", CTX)
    flipped = env.ciphertext[:-1] + bytes([env.ciphertext[-1] ^ 1])
    tampered = credential_crypto.Envelope(flipped, env.wrapped_key, env.key_provider, env.key_id)
    with pytest.raises(CredentialCryptoError):
        credential_crypto.open_sync(tampered, CTX)


def test_local_key_is_refused_in_a_deployed_env(monkeypatch):
    """The local key derives from FEOH_SECRET_KEY — never a production control.
    `Settings` refuses to boot without a KMS key id; this is the second line."""
    env = credential_crypto.seal_sync(b"x", CTX)
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(CredentialCryptoError, match="FEOH_CREDENTIAL_KMS_KEY_ID"):
        credential_crypto.open_sync(env, CTX)
    with pytest.raises(CredentialCryptoError):
        credential_crypto.seal_sync(b"x", CTX)


def test_unknown_key_provider_fails_closed():
    env = credential_crypto.seal_sync(b"x", CTX)
    odd = credential_crypto.Envelope(env.ciphertext, env.wrapped_key, "vault", "?")
    with pytest.raises(CredentialCryptoError):
        credential_crypto.open_sync(odd, CTX)


# ---------- crypto: KMS provider (fake client) ------------------------------


class _FakeKms:
    """Wraps a DEK with a fixed XOR so the test can see it go round-trip."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.threads: list[int] = []

    def generate_data_key(self, *, KeyId, KeySpec, EncryptionContext):  # noqa: N803
        self.calls.append(("generate", dict(EncryptionContext)))
        dek = bytes(range(32))
        return {
            "Plaintext": dek,
            "CiphertextBlob": bytes(b ^ 0x5A for b in dek) + json.dumps(EncryptionContext).encode(),
            "KeyId": "arn:aws:kms:us-east-1:111:key/abc",
        }

    def decrypt(self, *, CiphertextBlob, EncryptionContext):  # noqa: N803
        self.calls.append(("decrypt", dict(EncryptionContext)))
        self.threads.append(threading.get_ident())
        if not CiphertextBlob.endswith(json.dumps(EncryptionContext).encode()):
            raise RuntimeError("InvalidCiphertextException")
        return {"Plaintext": bytes(b ^ 0x5A for b in CiphertextBlob[:32])}


@pytest.fixture
def fake_kms(monkeypatch):
    kms = _FakeKms()
    monkeypatch.setattr(settings, "credential_kms_key_id", "alias/feohledger-app-test")
    monkeypatch.setattr(credential_crypto, "_kms_client", lambda: kms)
    credential_crypto.clear_dek_cache()
    yield kms
    credential_crypto.clear_dek_cache()


def test_kms_provider_selected_by_key_id_and_passes_the_context(fake_kms):
    env = credential_crypto.seal_sync(b"payload", CTX)
    assert env.key_provider == credential_crypto.KEY_PROVIDER_KMS
    assert env.key_id.startswith("arn:aws:kms:")
    assert fake_kms.calls == [("generate", CTX)]
    assert credential_crypto.open_sync(env, CTX) == b"payload"
    assert fake_kms.calls[-1] == ("decrypt", CTX)


def test_kms_unwrap_is_cached(fake_kms):
    env = credential_crypto.seal_sync(b"payload", CTX)
    for _ in range(3):
        assert credential_crypto.open_sync(env, CTX) == b"payload"
    assert [c[0] for c in fake_kms.calls] == ["generate", "decrypt"]


def test_kms_error_names_no_value(fake_kms):
    env = credential_crypto.seal_sync(SECRET.encode(), CTX)
    with pytest.raises(CredentialCryptoError) as exc:
        credential_crypto.open_sync(env, {**CTX, "organization_id": "other"})
    assert "KMS Decrypt failed" in str(exc.value)
    assert SECRET not in str(exc.value)


async def test_kms_unwrap_runs_off_the_event_loop(fake_kms):
    """boto3 is synchronous — the KMS round trip must not stall the loop."""
    env = await credential_crypto.seal(b"payload", CTX)
    assert await credential_crypto.open_envelope(env, CTX) == b"payload"
    assert fake_kms.threads and fake_kms.threads[0] != threading.get_ident()


# ---------- shape helpers ----------------------------------------------------


PAYMENTS = {
    "provider": "column",
    "api_key": "k1",
    "webhook_secret": "",
    "bank_account_id": "ba",
    "providers": [
        {"provider": "column", "api_key": "k2", "bank_account_id": "x"},
        {"provider": "increase", "api_key": "k3"},
    ],
}


def test_extract_strip_inject_round_trip():
    found = extract_secrets("payments", PAYMENTS)
    assert found == {
        "api_key": "k1",
        "providers.column.api_key": "k2",
        "providers.increase.api_key": "k3",
    }
    public = strip_secrets("payments", PAYMENTS)
    assert "api_key" not in public and "webhook_secret" not in public
    assert all("api_key" not in e for e in public["providers"])
    assert inject_secrets("payments", public, found) == {
        **{k: v for k, v in PAYMENTS.items() if k != "webhook_secret"},
    }


@pytest.mark.parametrize(
    ("block", "path"),
    [
        ("erp", "client_secret"),
        ("erp", "token_secret"),
        ("payments", "providers.column.api_key"),
        ("cards", "api_key"),
    ],
)
def test_validate_path_accepts_secret_slots(block, path):
    validate_path(block, path)


@pytest.mark.parametrize(
    ("block", "path"),
    [
        ("erp", "base_url"),
        ("erp", "client_id"),
        ("cards", "providers.x.api_key"),
        ("payments", "providers.Bad Name.api_key"),
        ("payments", "providers.column.base_url"),
        ("extraction", "api_key"),
    ],
)
def test_validate_path_refuses_everything_else(block, path):
    with pytest.raises(CredentialPathError):
        validate_path(block, path)


def test_validate_update_drops_blanks_and_refuses_conflicts():
    to_set, to_clear = validate_update("erp", {"api_key": "  ", "client_secret": " v "}, [])
    assert to_set == {"client_secret": "v"} and to_clear == set()
    with pytest.raises(CredentialPathError, match="both set and cleared"):
        validate_update("erp", {"api_key": "v"}, ["api_key"])
    with pytest.raises(CredentialPathError, match="must be text") as exc:
        validate_update("erp", {"api_key": 12345}, [])
    assert "12345" not in str(exc.value)


# ---------- connection tests never carry a stored secret elsewhere -----------


async def test_connection_test_uses_stored_secret_only_for_the_saved_config(monkeypatch):
    org = SimpleNamespace(
        id=uuid.uuid4(),
        settings={"erp": {"type": "dynamics_365_bc", "base_url": "https://bc.example.com"}},
    )

    async def _stored(org_id, block, *, db=None):
        return {"client_secret": SECRET}

    monkeypatch.setattr(provider_credentials, "load_secrets", _stored)

    same = await config_for_connection_test(
        org,
        "erp",
        {"type": "dynamics_365_bc", "base_url": "https://bc.example.com", "client_secret": ""},
    )
    assert same["client_secret"] == SECRET

    elsewhere = await config_for_connection_test(
        org, "erp", {"type": "dynamics_365_bc", "base_url": "https://attacker.invalid"}
    )
    assert "client_secret" not in elsewhere

    typed = await config_for_connection_test(
        org,
        "erp",
        {
            "type": "dynamics_365_bc",
            "base_url": "https://new.example.com",
            "client_secret": SECRET_2,
        },
    )
    assert typed["client_secret"] == SECRET_2


# ---------- the endpoint + accessor (real Postgres) ---------------------------


URL = "/api/organization/credentials"


async def _rows(realdb, key="a") -> list[ProviderCredential]:
    async with realdb.control_sessionmaker()() as s:
        return list(
            (
                await s.execute(
                    select(ProviderCredential).where(
                        ProviderCredential.organization_id == realdb.info(key).org_id
                    )
                )
            )
            .scalars()
            .all()
        )


async def _org(realdb, key="a") -> Organization:
    async with realdb.control_sessionmaker()() as s:
        return (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()


async def _audit(realdb, action: str, key="a") -> list[AuditLog]:
    async with realdb.sessionmaker(key)() as s:
        return list(
            (await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap_manager", "ap_clerk", "cfo"])
async def test_only_admins_reach_the_credentials(realdb, role):
    async with realdb.client(key="a", role=role) as c:
        assert (await c.get(URL)).status_code == 403
        assert (await c.put(f"{URL}/erp", json={"set": {"api_key": SECRET}})).status_code == 403
    assert await _rows(realdb) == []


@pytest.mark.asyncio
async def test_put_seals_audits_and_never_reads_back(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(f"{URL}/erp", json={"set": {"api_key": SECRET, "account_token": "t"}})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"erp": ["account_token", "api_key"], "payments": [], "cards": []}
        assert SECRET not in resp.text
        status = await c.get(URL)
        org_resp = await c.get("/api/organization")
    assert SECRET not in status.text and SECRET not in org_resp.text

    (row,) = await _rows(realdb)
    assert row.block == "erp" and row.key_provider == "local"
    assert SECRET.encode() not in bytes(row.ciphertext)
    async with realdb.control_sessionmaker()() as s:
        raw = (await s.execute(text("SELECT settings::text FROM organizations"))).scalars().all()
    assert all(SECRET not in (r or "") for r in raw)

    org = await _org(realdb)
    resolved = await provider_credentials.provider_config(org, "erp")
    assert resolved == {"api_key": SECRET, "account_token": "t"}

    (audit,) = await _audit(realdb, "organization.credentials_updated")
    assert audit.details["block"] == "erp"
    assert audit.details["changed"] == ["account_token", "api_key"]
    assert SECRET not in json.dumps(audit.details)


@pytest.mark.asyncio
async def test_blank_keeps_clear_removes_and_no_op_writes_nothing(realdb):
    await realdb.store_provider_secrets("a", "cards", {"api_key": SECRET})
    async with realdb.client(key="a", role="admin") as c:
        keep = await c.put(f"{URL}/cards", json={"set": {"api_key": ""}})
        assert keep.json()["cards"] == ["api_key"]
        assert await _audit(realdb, "organization.credentials_updated") == []

        rotate = await c.put(f"{URL}/cards", json={"set": {"api_key": SECRET_2}})
        assert rotate.status_code == 200
        org = await _org(realdb)
        assert (await provider_credentials.provider_config(org, "cards"))["api_key"] == SECRET_2

        cleared = await c.put(f"{URL}/cards", json={"clear": ["api_key"]})
        assert cleared.json()["cards"] == []
    assert await _rows(realdb) == []  # the last secret gone → the row gone
    audits = await _audit(realdb, "organization.credentials_updated")
    assert sorted(a.details.get("cleared") == ["api_key"] for a in audits) == [False, True]


@pytest.mark.asyncio
async def test_no_audit_row_means_no_save(realdb, monkeypatch):
    from app.services import audit_dispatch

    async def _fail(**_kwargs):
        raise ConnectionError("audit store down")

    monkeypatch.setattr(audit_dispatch, "_write_auth_audit", _fail)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(f"{URL}/payments", json={"set": {"api_key": SECRET}})
    assert resp.status_code == 503, resp.text
    assert SECRET not in resp.text
    assert await _rows(realdb) == []


@pytest.mark.asyncio
async def test_a_seal_failure_writes_no_audit_row_and_saves_nothing(realdb, monkeypatch):
    """Sealing precedes the audit row: a KMS failure must not leave a record
    of a credential change that never happened."""

    async def _kms_down(plaintext, context):
        raise CredentialCryptoError("KMS GenerateDataKey failed (EndpointConnectionError).")

    monkeypatch.setattr(credential_crypto, "seal", _kms_down)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(f"{URL}/erp", json={"set": {"api_key": SECRET}})
    assert resp.status_code == 503
    assert SECRET not in resp.text
    assert await _rows(realdb) == []
    assert await _audit(realdb, "organization.credentials_updated") == []


@pytest.mark.asyncio
@pytest.mark.plan("scale")  # a live ERP type is a Growth feature (§258)
async def test_a_config_change_that_cannot_be_audited_is_not_saved(realdb, monkeypatch):
    """Where a stored credential is sent is configuration; an unrecorded change
    to it is refused like an unrecorded credential change."""
    from app.services import audit_dispatch

    async def _fail(**_kwargs):
        raise ConnectionError("audit store down")

    monkeypatch.setattr(audit_dispatch, "_write_auth_audit", _fail)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {"type": "netsuite", "integration_method": "direct"}}},
        )
    assert resp.status_code == 503, resp.text
    assert (await _org(realdb)).settings.get("erp") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [SECRET],
        SECRET,
        {"set": {"api_key": [SECRET]}},
        {"set": {"base_url": SECRET}},
        {"set": {"api_key": SECRET}, "clear": ["api_key"]},
        {"set": {"api_key": SECRET}, "extra": SECRET},
    ],
)
async def test_bad_bodies_are_refused_without_echoing_the_value(realdb, body):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(f"{URL}/erp", json=body)
    assert resp.status_code == 422, resp.text
    assert SECRET not in resp.text
    assert await _rows(realdb) == []


@pytest.mark.asyncio
async def test_unknown_block_is_404(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(f"{URL}/extraction", json={"set": {"api_key": SECRET}})
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.plan("scale")  # a live ERP type is a Growth feature (§258)
async def test_patch_refuses_a_secret_and_names_the_endpoint(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"erp": {"type": "netsuite", "token_secret": SECRET}}},
        )
    assert resp.status_code == 422
    assert "/api/organization/credentials/erp" in resp.json()["detail"]
    assert "token_secret" in resp.json()["detail"]
    assert SECRET not in resp.text
    assert (await _org(realdb)).settings.get("erp") is None


@pytest.mark.asyncio
@pytest.mark.plan("scale")  # a live ERP type is a Growth feature (§258)
async def test_patch_drops_blank_secrets_keeps_stored_ones_and_audits_config(realdb):
    """The form sends blank secret fields ("leave blank to keep"); the save
    keeps the sealed values, and a change to WHERE they are sent is audited."""
    await realdb.store_provider_secrets("a", "erp", {"client_secret": SECRET})
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={
                "settings": {
                    "erp": {
                        "type": "dynamics_365_bc",
                        "integration_method": "direct",
                        "base_url": "https://bc.example.com",
                        "client_secret": "",
                    }
                }
            },
        )
    assert resp.status_code == 200, resp.text
    org = await _org(realdb)
    assert "client_secret" not in org.settings["erp"]
    resolved = await provider_credentials.provider_config(org, "erp")
    assert resolved["client_secret"] == SECRET
    assert resolved["base_url"] == "https://bc.example.com"
    (audit,) = await _audit(realdb, "organization.provider_config_updated")
    assert audit.details == {
        "block": "erp",
        "changed": ["base_url", "integration_method", "type"],
    }


@pytest.mark.asyncio
async def test_accessor_ignores_plaintext_left_in_the_jsonb(realdb):
    """Only the sealed store authenticates: a stray plaintext key in the JSONB
    (a hand edit, a restored pre-0110 backup) is never what an adapter uses."""
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            text("UPDATE organizations SET settings = CAST(:s AS jsonb) WHERE id = :id"),
            {
                "s": json.dumps({"payments": {"provider": "column", "api_key": "stray"}}),
                "id": realdb.info("a").org_id,
            },
        )
        await s.commit()
    org = await _org(realdb)
    assert await provider_credentials.provider_config(org, "payments") == {"provider": "column"}
    await realdb.store_provider_secrets("a", "payments", {"api_key": SECRET})
    assert (await provider_credentials.provider_config(org, "payments"))["api_key"] == SECRET


@pytest.mark.asyncio
async def test_accessor_returns_none_when_nothing_is_configured(realdb):
    org = await _org(realdb)
    assert await provider_credentials.provider_config(org, "erp") is None


# ---------- migration 0110 ----------------------------------------------------


_MIGRATION = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _migration():
    path = _MIGRATION / "0110_provider_credentials.py"
    spec = importlib.util.spec_from_file_location("_mig_0110", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    mig = _migration()
    assert mig.revision == "0110_provider_credentials"
    assert mig.down_revision == "0109_goods_receipt_entry"
    assert len(mig.revision) <= 32


async def _run_upgrade(realdb) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    mig = _migration()

    def _go(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            mig.upgrade()

    async with realdb.control_sessionmaker()() as s:
        conn = await s.connection()
        await conn.run_sync(_go)
        await s.commit()


@pytest.mark.asyncio
async def test_migration_moves_plaintext_into_the_sealed_store(realdb):
    legacy = {
        "erp": {
            "type": "netsuite",
            "integration_method": "direct",
            "consumer_key": "ck",
            "consumer_secret": SECRET,
            "token_secret": "ts",
            "api_key": "",
        },
        "payments": {"provider": "column", "providers": [{"provider": "column", "api_key": "k"}]},
        "company": {"address": "1 Main St"},
    }
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            text("UPDATE organizations SET settings = CAST(:s AS jsonb) WHERE id = :id"),
            {"s": json.dumps(legacy), "id": realdb.info("a").org_id},
        )
        await s.commit()
    # An existing sealed value wins over a plaintext one for the same path.
    await realdb.store_provider_secrets("a", "erp", {"token_secret": "newer"})

    await _run_upgrade(realdb)
    await _run_upgrade(realdb)  # idempotent: nothing left to move

    org = await _org(realdb)
    assert SECRET not in json.dumps(org.settings)
    assert org.settings["erp"] == {
        "type": "netsuite",
        "integration_method": "direct",
        "consumer_key": "ck",
    }
    assert org.settings["company"] == {"address": "1 Main St"}
    erp = await provider_credentials.provider_config(org, "erp")
    assert erp["consumer_secret"] == SECRET
    assert erp["token_secret"] == "newer"
    pay = await provider_credentials.provider_config(org, "payments")
    assert pay["providers"] == [{"provider": "column", "api_key": "k"}]


@pytest.mark.asyncio
async def test_an_erp_send_without_openable_credentials_lands_failed(realdb):
    """The dispatchers must not fall back to `erp_config=None` (the MOCK
    adapter, which reports the invoice posted). The send lands `failed`, where
    `retry-erp` can re-drive it."""
    from decimal import Decimal

    from app.models.invoice import Invoice, InvoiceStatus
    from app.services.erp import ERP_CREDENTIALS_UNAVAILABLE, fail_erp_send_without_credentials

    async with realdb.sessionmaker("a")() as s:
        inv = Invoice(
            invoice_number="INV-CRED-1",
            vendor_name="Vendor Co",
            amount=Decimal("10.00"),
            status=InvoiceStatus.sending_to_erp,
            organization_id=realdb.info("a").org_id,
        )
        s.add(inv)
        await s.commit()
        await fail_erp_send_without_credentials(s, inv, actor_id=uuid.uuid4())
        await s.refresh(inv)
        assert inv.status == InvoiceStatus.failed
        (row,) = (
            (await s.execute(select(AuditLog).where(AuditLog.action == "invoice.erp_failed")))
            .scalars()
            .all()
        )
    assert row.details["error"] == ERP_CREDENTIALS_UNAVAILABLE
