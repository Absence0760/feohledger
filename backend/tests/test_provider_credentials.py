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
        ("extraction", "api_key"),
        ("extraction", "aws_secret_access_key"),
        ("sso", "client_secret"),
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
        # An identifier, not a secret: it stays configuration.
        ("extraction", "aws_access_key_id"),
        ("sso", "client_id"),
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
        assert resp.json() == {
            "erp": ["account_token", "api_key"],
            "payments": [],
            "cards": [],
            "commerce": [],
            "extraction": [],
        }
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
    # A block outside CREDENTIAL_BLOCKS (and outside DEDICATED_WRITER_BLOCKS,
    # pinned separately for `sso` below). `extraction` was this test's example
    # until the BYOK keys were sealed (decisions §444) and it became a real block.
    assert "nonexistent" not in provider_credentials.CREDENTIAL_BLOCKS
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(f"{URL}/nonexistent", json={"set": {"api_key": SECRET}})
    assert resp.status_code == 404
    assert SECRET not in resp.text
    assert await _rows(realdb) == []


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
        # `configured_at` is the server's stamp of when the ERP moved (§428):
        # this save moved it, so the stored key changed with the others.
        "changed": ["base_url", "configured_at", "integration_method", "type"],
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


@pytest.mark.asyncio
async def test_stored_secret_paths_names_only_this_block_and_never_unseals(realdb, monkeypatch):
    """The per-tenant ERP webhooks ask this before an unauthenticated request may
    make them unseal (decisions §408): names only, scoped to the one block, and an
    org with no row has nothing stored."""
    from app.services import provider_credentials

    org = await _org(realdb)
    async with realdb.control_sessionmaker()() as s:
        assert await provider_credentials.stored_secret_paths(org, "erp", db=s) == frozenset()

    await realdb.store_provider_secrets("a", "cards", {"api_key": SECRET})
    await realdb.store_provider_secrets("a", "erp", {"webhook_signing_secret": SECRET})

    def _no_unseal(*_a, **_k):
        raise AssertionError("stored_secret_paths must not decrypt")

    monkeypatch.setattr(provider_credentials, "_open_row", _no_unseal)
    monkeypatch.setattr(provider_credentials, "open_secrets_sync", _no_unseal)
    async with realdb.control_sessionmaker()() as s:
        erp = await provider_credentials.stored_secret_paths(org, "erp", db=s)
        cards = await provider_credentials.stored_secret_paths(org, "cards", db=s)
    assert any("webhook_signing_secret" in p for p in erp)
    assert not any("api_key" in p for p in erp)
    assert any("api_key" in p for p in cards)


# ---------- BYOK extraction keys + the OIDC client secret (migration 0135) ------


async def test_extraction_connection_test_joins_the_stored_key_only_for_the_saved_provider(
    monkeypatch,
):
    """The form posts every provider's fields and never `program_type`, so the
    stored key joins on the provider alone — never for another provider, which
    would send one vendor's secret to the other."""
    org = SimpleNamespace(
        id=uuid.uuid4(),
        settings={"extraction": {"program_type": "byok", "provider": "openai_vision"}},
    )

    async def _stored(org_id, block, *, db=None):
        assert block == "extraction"
        return {"api_key": SECRET}

    monkeypatch.setattr(provider_credentials, "load_secrets", _stored)
    form = {"provider": "openai_vision", "api_key": "", "base_url": "http://localhost:11434"}
    same = await config_for_connection_test(org, "extraction", form)
    assert same["api_key"] == SECRET

    other = await config_for_connection_test(
        org, "extraction", {**form, "provider": "claude_vision"}
    )
    assert "api_key" not in other

    typed = await config_for_connection_test(
        org, "extraction", {**form, "provider": "claude_vision", "api_key": SECRET_2}
    )
    assert typed["api_key"] == SECRET_2


@pytest.mark.parametrize(
    ("before", "after", "drop"),
    [
        ({"provider": "openai_vision"}, {"provider": "openai_vision"}, False),
        ({"provider": "openai_vision"}, {"provider": " openai_vision "}, False),
        ({"provider": "openai_vision"}, {"provider": "claude_vision"}, True),
        ({"provider": "aws_textract"}, {}, True),
        (None, {"provider": "claude_vision"}, True),
    ],
)
def test_extraction_secrets_follow_the_provider(before, after, drop):
    assert provider_credentials.extraction_secrets_to_drop(before, after) is drop


@pytest.mark.asyncio
async def test_extraction_key_is_written_and_reported_through_the_credentials_door(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.put(f"{URL}/extraction", json={"set": {"aws_secret_access_key": SECRET}})
        assert resp.status_code == 200, resp.text
        assert resp.json()["extraction"] == ["aws_secret_access_key"]
        assert SECRET not in resp.text
        org_resp = await c.get("/api/organization")
    assert SECRET not in org_resp.text
    (audit,) = await _audit(realdb, "organization.credentials_updated")
    assert audit.details["block"] == "extraction"
    assert SECRET not in json.dumps(audit.details)


@pytest.mark.asyncio
async def test_the_sso_secret_is_not_reachable_through_the_generic_door(realdb):
    """`sso` is sealed in the same store but has its own writer
    (`PUT /api/organization/sso`); the generic door neither writes nor reports it."""
    await realdb.store_provider_secrets("a", "sso", {"client_secret": SECRET})
    async with realdb.client(key="a", role="admin") as c:
        put = await c.put(f"{URL}/sso", json={"set": {"client_secret": SECRET_2}})
        status = await c.get(URL)
    assert put.status_code == 404
    assert "sso" not in status.json()
    async with realdb.control_sessionmaker()() as s:
        sealed = await provider_credentials.load_secrets(realdb.info("a").org_id, "sso", db=s)
    assert sealed == {"client_secret": SECRET}


@pytest.mark.asyncio
async def test_patch_refuses_an_extraction_key_and_names_the_endpoint(realdb):
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={
                "settings": {
                    "extraction": {
                        "program_type": "byok",
                        "provider": "claude_vision",
                        "api_key": SECRET,
                    }
                }
            },
        )
    assert resp.status_code == 422
    assert "/api/organization/credentials/extraction" in resp.json()["detail"]
    assert SECRET not in resp.text
    assert (await _org(realdb)).settings.get("extraction") is None


@pytest.mark.asyncio
async def test_patch_keeps_the_key_for_the_same_provider_and_drops_it_for_another(realdb):
    """A blank key field keeps the sealed key while the provider is unchanged;
    switching provider drops it (audited) so it is never sent to the new one."""
    form = {"program_type": "byok", "provider": "openai_vision", "api_key": ""}
    await _set_settings(realdb, {"extraction": {k: v for k, v in form.items() if v}})
    await realdb.store_provider_secrets("a", "extraction", {"api_key": SECRET})
    async with realdb.client(key="a", role="admin") as c:
        same = await c.patch("/api/organization", json={"settings": {"extraction": form}})
        assert same.status_code == 200, same.text
        org = await _org(realdb)
        byok = await provider_credentials.byok_extraction_config(org.id, org.settings)
        assert byok["api_key"] == SECRET
        assert "api_key" not in org.settings["extraction"]

        switched = await c.patch(
            "/api/organization",
            json={"settings": {"extraction": {**form, "provider": "claude_vision"}}},
        )
        assert switched.status_code == 200, switched.text
    org = await _org(realdb)
    byok = await provider_credentials.byok_extraction_config(org.id, org.settings)
    assert byok == {"program_type": "byok", "provider": "claude_vision"}
    drops = [
        a.details
        for a in await _audit(realdb, "organization.credentials_updated")
        if a.details.get("reason") == "extraction_provider_changed"
    ]
    assert drops[-1] == {
        "block": "extraction",
        "changed": ["api_key"],
        "cleared": ["api_key"],
        "reason": "extraction_provider_changed",
    }


@pytest.mark.asyncio
async def test_byok_accessor_ignores_a_plaintext_key_and_skips_platform_orgs(realdb, monkeypatch):
    org = await _org(realdb)
    stray = {"extraction": {"program_type": "byok", "provider": "claude_vision", "api_key": "x"}}
    assert await provider_credentials.byok_extraction_config(org.id, stray) == {
        "program_type": "byok",
        "provider": "claude_vision",
    }

    def _no_read(*_a, **_k):
        raise AssertionError("a platform org must not open the sealed store")

    monkeypatch.setattr(provider_credentials, "load_secrets", _no_read)
    platform = {"extraction": {"program_type": "platform"}}
    assert await provider_credentials.byok_extraction_config(org.id, platform) is None
    assert await provider_credentials.byok_extraction_config(org.id, {}) is None


def _migration_0135():
    path = _MIGRATION / "0135_seal_extraction_sso.py"
    spec = importlib.util.spec_from_file_location("_mig_0135", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain_0135():
    mig = _migration_0135()
    assert mig.revision == "0135_seal_extraction_sso"
    assert mig.down_revision == "0134_expense_creator"
    assert len(mig.revision) <= 32


async def _run_0135(realdb, step: str) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    mig = _migration_0135()

    def _go(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(mig, step)()

    async with realdb.control_sessionmaker()() as s:
        conn = await s.connection()
        await conn.run_sync(_go)
        await s.commit()


async def _set_settings(realdb, settings_: dict, key: str = "a") -> None:
    async with realdb.control_sessionmaker()() as s:
        await s.execute(
            text("UPDATE organizations SET settings = CAST(:s AS jsonb) WHERE id = :id"),
            {"s": json.dumps(settings_), "id": realdb.info(key).org_id},
        )
        await s.commit()


@pytest.mark.asyncio
async def test_migration_0135_seals_extraction_and_sso_plaintext(realdb):
    from app.services.sso import is_sso_only

    sso = {
        "enabled": True,
        "sso_only": True,
        "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
        "client_id": "feoh",
        "client_secret": SECRET,
        "scim_bearer_hash": "ab" * 32,
    }
    legacy = {
        "extraction": {
            "program_type": "byok",
            "provider": "aws_textract",
            "aws_access_key_id": "AKIAEXAMPLE",
            "aws_secret_access_key": SECRET_2,
            "api_key": "",
        },
        "sso": sso,
        "company": {"address": "1 Main St"},
    }
    await _set_settings(realdb, legacy)
    # Org b: an SSO block with no secret gains no marker; a sealed row wins.
    await _set_settings(
        realdb,
        {"extraction": {"program_type": "byok", "provider": "claude_vision", "api_key": "old"}},
        key="b",
    )
    await realdb.store_provider_secrets("b", "extraction", {"api_key": "newer"})

    await _run_0135(realdb, "upgrade")
    await _run_0135(realdb, "upgrade")  # idempotent: nothing left to move

    org = await _org(realdb)
    flat = json.dumps(org.settings)
    assert SECRET not in flat and SECRET_2 not in flat
    assert org.settings["extraction"] == {
        "program_type": "byok",
        "provider": "aws_textract",
        "aws_access_key_id": "AKIAEXAMPLE",
    }
    assert org.settings["sso"] == {
        **{k: v for k, v in sso.items() if k != "client_secret"},
        "client_secret_configured": True,
    }
    assert org.settings["company"] == {"address": "1 Main St"}
    # The sign-in verdict is unchanged across the move.
    assert is_sso_only(org.settings) is True
    byok = await provider_credentials.byok_extraction_config(org.id, org.settings)
    assert byok["aws_secret_access_key"] == SECRET_2
    async with realdb.control_sessionmaker()() as s:
        sealed = await provider_credentials.load_secrets(org.id, "sso", db=s)
    assert sealed == {"client_secret": SECRET}

    org_b = await _org(realdb, key="b")
    assert "api_key" not in org_b.settings["extraction"]
    byok_b = await provider_credentials.byok_extraction_config(org_b.id, org_b.settings)
    assert byok_b["api_key"] == "newer"

    await _run_0135(realdb, "downgrade")
    org = await _org(realdb)
    assert org.settings["sso"]["client_secret"] == SECRET
    assert "client_secret_configured" not in org.settings["sso"]
    assert org.settings["extraction"]["aws_secret_access_key"] == SECRET_2
    assert {r.block for r in await _rows(realdb)} == set()


async def _run_0135_on_tenant(realdb, step: str, key: str = "a") -> bool:
    """Run one 0135 step against a TENANT database; return what its gate said."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    mig = _migration_0135()
    verdict: list[bool] = []

    def _go(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            verdict.append(mig._is_control_db())
            getattr(mig, step)()

    async with realdb.sessionmaker(key)() as s:
        conn = await s.connection()
        await conn.run_sync(_go)
        await s.commit()
    return verdict[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["upgrade", "downgrade"])
async def test_migration_0135_is_a_no_op_on_a_tenant_db(realdb, step):
    """`migrate_all_tenants.py` runs every revision on every tenant DB, which
    has neither `organizations` nor `provider_credentials`: the gate must say
    "not the control plane" and the step must return without touching
    anything (an ungated run would fail on the missing table)."""
    plaintext = {"extraction": {"program_type": "byok", "api_key": SECRET}}
    await _set_settings(realdb, plaintext)

    assert await _run_0135_on_tenant(realdb, step) is False

    # Nothing on the control plane moved either.
    assert (await _org(realdb)).settings == plaintext
    assert await _rows(realdb) == []


@pytest.mark.asyncio
async def test_migration_0135_gate_recognises_the_control_plane(realdb):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    mig = _migration_0135()
    verdict: list[bool] = []

    def _go(sync_conn):
        with Operations.context(MigrationContext.configure(sync_conn)):
            verdict.append(mig._is_control_db())

    async with realdb.control_sessionmaker()() as s:
        conn = await s.connection()
        await conn.run_sync(_go)
    assert verdict == [True]


@pytest.mark.asyncio
async def test_migration_0135_kms_error_rolls_back_leaving_plaintext_in_place(realdb, fake_kms):
    """A KMS failure part-way through raises and the transaction unwinds: the
    block sealed before the failure leaves no row, and every plaintext value is
    still where the running code reads it — never a secret in neither place."""
    legacy = {
        "extraction": {"program_type": "byok", "provider": "claude_vision", "api_key": SECRET},
        "sso": {"enabled": True, "client_id": "feoh", "client_secret": SECRET_2},
    }
    await _set_settings(realdb, legacy)

    real_generate = fake_kms.generate_data_key
    generated = 0

    def _second_call_fails(**kwargs):
        nonlocal generated
        generated += 1
        if generated > 1:
            raise RuntimeError("KMSInternalException")
        return real_generate(**kwargs)

    fake_kms.generate_data_key = _second_call_fails

    with pytest.raises(CredentialCryptoError) as exc:
        await _run_0135(realdb, "upgrade")
    assert SECRET not in str(exc.value) and SECRET_2 not in str(exc.value)
    # The first block was sealed before the failure — the rollback is real.
    assert generated == 2

    assert (await _org(realdb)).settings == legacy
    assert await _rows(realdb) == []
