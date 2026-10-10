"""Tenant provider credentials: the one accessor, the one writer, the catalogue.

The ERP, payment-rail and card-issuer blocks of `Organization.settings` each
mix configuration (which provider, base URL, sandbox flag) with secrets (API
keys, OAuth client secrets, account tokens, webhook signing secrets). The
configuration stays in the JSONB. The secrets live sealed in
`provider_credentials` (`models/provider_credential.py`, envelope-encrypted by
`services/credential_crypto`) and are:

* **read through one accessor** — :func:`provider_config` returns the block
  exactly as the adapters always received it, secrets merged back in. Any
  secret-named key still sitting in the JSONB is ignored, so a stray plaintext
  value can never be what an adapter authenticates with.
* **written through one writer** — :func:`update_secrets`, called only by the
  audited `PUT /api/organization/credentials/{block}`. `PATCH
  /api/organization` refuses a non-blank secret in these blocks.
* **never read back** — :func:`credential_status` reports which paths are set,
  by name, and nothing else.

**What counts as a secret** is :data:`SECRET_FIELDS`, an allow-list per block.
The payments block also carries an optional multi-route list
(`payments.providers`, `services/corridor_quotes`), whose entries hold their own
secrets; those are addressed as ``providers.<provider>.<field>`` — keyed by the
entry's ``provider`` name, which is stable across list edits where an index is
not.

Values never enter a log line or an exception message here; errors name paths.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_credential import ProviderCredential
from app.services import credential_crypto

# The settings blocks whose secrets live here.
CREDENTIAL_BLOCKS: tuple[str, ...] = ("erp", "payments", "cards")

# Per block: the keys that are secrets. Everything else in the block is
# configuration and stays in the JSONB. Identifiers that are not secret on their
# own (an OAuth client id, a NetSuite consumer key / token id, an account id)
# stay configuration so the settings page can still show them.
SECRET_FIELDS: dict[str, frozenset[str]] = {
    "erp": frozenset(
        {
            "api_key",  # Merge.dev API key
            "account_token",  # Merge.dev linked-account token
            "client_secret",  # Business Central OAuth client secret
            "consumer_secret",  # NetSuite TBA
            "token_secret",  # NetSuite TBA
            "webhook_signing_secret",  # inbound ERP webhook HMAC key
        }
    ),
    "payments": frozenset(
        {
            "api_key",  # Modern Treasury / Column / Increase / Stripe / Checkeeper
            "client_secret",  # Dwolla
            "webhook_secret",  # inbound payment webhook HMAC key
        }
    ),
    "cards": frozenset(
        {
            "api_key",  # Lithic
            "client_secret",  # Nium
            "webhook_signing_secret",  # inbound card webhook HMAC key
        }
    ),
}

# Blocks with a list of per-provider sub-configs, keyed by the entry's
# `provider`, whose entries carry the same secret fields as the block.
_PROVIDER_LISTS: dict[str, str] = {"payments": "providers"}

_PROVIDER_NAME = re.compile(r"^[a-z0-9_]{1,50}$")
_MAX_SECRET_LENGTH = 8192


class CredentialPathError(ValueError):
    """A path names no secret slot of the block. The message names the path."""


def _context(org_id: uuid.UUID, block: str) -> dict[str, str]:
    return {
        "purpose": "feohledger/provider-credentials",
        "organization_id": str(org_id),
        "block": block,
    }


def _has_value(value: object) -> bool:
    return value is not None and value != ""


# ── pure shape helpers ───────────────────────────────────────────────────────


def validate_path(block: str, path: str) -> None:
    """Raise :class:`CredentialPathError` unless ``path`` is a secret slot."""
    fields = SECRET_FIELDS.get(block)
    if fields is None:
        raise CredentialPathError(f"{block} has no stored credentials.")
    if path in fields:
        return
    list_key = _PROVIDER_LISTS.get(block)
    parts = path.split(".")
    if (
        list_key
        and len(parts) == 3
        and parts[0] == list_key
        and _PROVIDER_NAME.match(parts[1])
        and parts[2] in fields
    ):
        return
    raise CredentialPathError(f"{block}.{path} is not a credential field.")


def extract_secrets(block: str, cfg: object) -> dict[str, object]:
    """Every non-blank secret value in a settings block, by path."""
    if not isinstance(cfg, Mapping):
        return {}
    fields = SECRET_FIELDS[block]
    found: dict[str, object] = {k: cfg[k] for k in fields if _has_value(cfg.get(k))}
    list_key = _PROVIDER_LISTS.get(block)
    entries = cfg.get(list_key) if list_key else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("provider") or "")
            for k in fields:
                if _has_value(entry.get(k)):
                    found[f"{list_key}.{name}.{k}"] = entry[k]
    return found


def strip_secrets(block: str, cfg: object) -> object:
    """A copy of ``cfg`` with every secret key removed (blank ones too).

    Non-mapping input is returned unchanged — there is nothing to strip.
    """
    if not isinstance(cfg, Mapping):
        return cfg
    fields = SECRET_FIELDS[block]
    out = {k: v for k, v in cfg.items() if k not in fields}
    list_key = _PROVIDER_LISTS.get(block)
    if list_key and isinstance(out.get(list_key), list):
        out[list_key] = [
            {k: v for k, v in e.items() if k not in fields} if isinstance(e, Mapping) else e
            for e in out[list_key]
        ]
    return out


def inject_secrets(block: str, cfg: Mapping, secrets: Mapping[str, object]) -> dict:
    """``cfg`` with ``secrets`` merged back in at their paths."""
    out = dict(cfg)
    fields = SECRET_FIELDS[block]
    for path, value in secrets.items():
        if path in fields:
            out[path] = value
    list_key = _PROVIDER_LISTS.get(block)
    if list_key and isinstance(out.get(list_key), list):
        merged = []
        for entry in out[list_key]:
            if isinstance(entry, Mapping):
                prefix = f"{list_key}.{entry.get('provider') or ''}."
                extra = {p[len(prefix) :]: v for p, v in secrets.items() if p.startswith(prefix)}
                entry = {**entry, **extra}
            merged.append(entry)
        out[list_key] = merged
    return out


def strip_all_blocks(settings: Mapping | None) -> dict:
    """``settings`` with every credential block's secret keys removed."""
    out = dict(settings or {})
    for block in CREDENTIAL_BLOCKS:
        if block in out:
            out[block] = strip_secrets(block, out[block])
    return out


# ── sealing ──────────────────────────────────────────────────────────────────


def seal_secrets_sync(
    org_id: uuid.UUID, block: str, secrets: Mapping[str, object]
) -> credential_crypto.Envelope:
    """Seal a block's secret map. **Blocking** with KMS (used by migration 0110)."""
    plaintext = json.dumps(dict(secrets), sort_keys=True).encode()
    return credential_crypto.seal_sync(plaintext, _context(org_id, block))


def open_secrets_sync(
    org_id: uuid.UUID, block: str, envelope: credential_crypto.Envelope
) -> dict[str, object]:
    """Open a block's sealed secret map. **Blocking** with KMS."""
    return json.loads(credential_crypto.open_sync(envelope, _context(org_id, block)))


def _envelope(row: ProviderCredential) -> credential_crypto.Envelope:
    return credential_crypto.Envelope(
        ciphertext=bytes(row.ciphertext),
        wrapped_key=bytes(row.wrapped_key),
        key_provider=row.key_provider,
        key_id=row.key_id,
    )


async def _open_row(row: ProviderCredential) -> dict[str, object]:
    raw = await credential_crypto.open_envelope(
        _envelope(row), _context(row.organization_id, row.block)
    )
    return json.loads(raw)


async def _row(db: AsyncSession, org_id: uuid.UUID, block: str, *, for_update: bool = False):
    stmt = select(ProviderCredential).where(
        ProviderCredential.organization_id == org_id, ProviderCredential.block == block
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


# ── the accessor ─────────────────────────────────────────────────────────────


async def load_secrets(
    org_id: uuid.UUID, block: str, *, db: AsyncSession | None = None
) -> dict[str, object]:
    """The decrypted secrets of one block (``{}`` when none are stored)."""
    if block not in SECRET_FIELDS:
        raise CredentialPathError(f"{block} has no stored credentials.")
    if db is not None:
        row = await _row(db, org_id, block)
    else:
        from app.database import control_session_factory

        async with control_session_factory() as session:
            row = await _row(session, org_id, block)
    return await _open_row(row) if row is not None else {}


async def resolve_block(
    org_id: uuid.UUID,
    settings: Mapping | None,
    block: str,
    *,
    db: AsyncSession | None = None,
) -> dict | None:
    """The ``settings[block]`` an adapter needs: configuration + secrets.

    THE read path for a credential block — adapters, connection tests and
    webhook verifiers all come through here (or :func:`provider_config`, its
    ORM-row spelling). ``None`` when the org has neither configuration nor
    secrets for the block, matching ``settings.get(block)``, which is what
    every caller used to read. ``db`` is a control-plane session to reuse;
    without one the accessor opens its own.
    """
    public = (settings or {}).get(block)
    secrets = await load_secrets(org_id, block, db=db)
    if public is None and not secrets:
        return None
    if not isinstance(public, Mapping):
        public = {}
    return inject_secrets(block, strip_secrets(block, public), secrets)


async def provider_config(org, block: str, *, db: AsyncSession | None = None) -> dict | None:
    """:func:`resolve_block` for an ``Organization`` row."""
    return await resolve_block(org.id, org.settings, block, db=db)


def _comparable(block: str, cfg: object) -> dict:
    stripped = strip_secrets(block, cfg if isinstance(cfg, Mapping) else {})
    return {k: v for k, v in stripped.items() if _has_value(v)}


async def config_for_connection_test(
    org, block: str, request_cfg: Mapping | None, *, db: AsyncSession | None = None
) -> dict | None:
    """The config a "Test connection" button should exercise.

    No request body → the saved block, resolved. A request body (the form,
    possibly unsaved) → its configuration plus only the secrets it carries
    itself; the STORED secrets fill in only when the request's configuration
    is the saved configuration. Otherwise a test would be a way to send a
    sealed credential to a base URL the admin has typed but not saved — reading
    it back by proxy, unaudited.
    """
    if not request_cfg:
        return await provider_config(org, block, db=db)
    own = extract_secrets(block, request_cfg)
    public = strip_secrets(block, request_cfg)
    if _comparable(block, request_cfg) == _comparable(block, (org.settings or {}).get(block)):
        stored = await load_secrets(org.id, block, db=db)
        return inject_secrets(block, public, {**stored, **own})
    return inject_secrets(block, public, own)


async def settings_with_secrets(org, *blocks: str, db: AsyncSession | None = None) -> dict:
    """``org.settings`` with the named credential blocks resolved.

    For a callee that takes the whole settings dict (`corridor_quotes`).
    """
    out = dict(org.settings or {})
    for block in blocks:
        resolved = await provider_config(org, block, db=db)
        if resolved is not None:
            out[block] = resolved
    return out


async def credential_status(db: AsyncSession, org_id: uuid.UUID) -> dict[str, list[str]]:
    """Which secret paths are set, per block — names only, no decrypt."""
    rows = (
        (
            await db.execute(
                select(ProviderCredential).where(ProviderCredential.organization_id == org_id)
            )
        )
        .scalars()
        .all()
    )
    status: dict[str, list[str]] = {block: [] for block in CREDENTIAL_BLOCKS}
    for row in rows:
        if row.block in status:
            status[row.block] = sorted(row.secret_fields or [])
    return status


# ── the writer ───────────────────────────────────────────────────────────────


def validate_update(
    block: str, set_values: Mapping[str, object], clear: Iterable[str]
) -> tuple[dict[str, str], set[str]]:
    """Normalise a write: blank values dropped, every path checked.

    Raises :class:`CredentialPathError` naming the offending path (never a
    value). A path both set and cleared is refused rather than guessed at.
    """
    to_set: dict[str, str] = {}
    for path, value in set_values.items():
        validate_path(block, path)
        if value is None:
            continue
        if not isinstance(value, str):
            raise CredentialPathError(f"{block}.{path} must be text.")
        value = value.strip()
        if not value:
            continue
        if len(value) > _MAX_SECRET_LENGTH:
            raise CredentialPathError(f"{block}.{path} is too long.")
        to_set[path] = value
    to_clear = set()
    for path in clear:
        validate_path(block, path)
        to_clear.add(path)
    both = sorted(to_clear & set(to_set))
    if both:
        raise CredentialPathError(f"{block}.{both[0]} is both set and cleared.")
    return to_set, to_clear


async def update_secrets(
    db: AsyncSession,
    org_id: uuid.UUID,
    block: str,
    to_set: Mapping[str, str],
    to_clear: Iterable[str],
    *,
    before_write: Callable[[list[str]], Awaitable[None]] | None = None,
) -> list[str]:
    """Apply a validated write and return the changed path NAMES.

    The caller has validated (:func:`validate_update`) and taken the org row
    lock; it commits. ``before_write`` receives the changed names once they are
    known and the new value is sealed, and before anything is persisted — the
    endpoint writes its audit row there, so a change with no record is
    impossible (it raises to abort), and a seal failure leaves no record of a
    change that never happened. It is not called when nothing would change. A
    block left with no secrets loses its row, so "is anything stored" is "is
    there a row".
    """
    row = await _row(db, org_id, block, for_update=True)
    current = await _open_row(row) if row is not None else {}
    merged = dict(current)
    changed: set[str] = set()
    for path, value in to_set.items():
        if merged.get(path) != value:
            changed.add(path)
        merged[path] = value
    for path in to_clear:
        if path in merged:
            changed.add(path)
            merged.pop(path)
    if not changed:
        return []
    # Seal BEFORE the audit hook: sealing can fail (KMS unreachable), and an
    # audit row for a change that was then never written would be a false
    # record. Audit-first only has to precede the database write.
    envelope = None
    if merged:
        envelope = await credential_crypto.seal(
            json.dumps(merged, sort_keys=True).encode(), _context(org_id, block)
        )
    if before_write is not None:
        await before_write(sorted(changed))
    if envelope is None:
        await db.execute(delete(ProviderCredential).where(ProviderCredential.id == row.id))
        return sorted(changed)
    if row is None:
        row = ProviderCredential(organization_id=org_id, block=block)
        db.add(row)
    row.secret_fields = sorted(merged)
    row.ciphertext = envelope.ciphertext
    row.wrapped_key = envelope.wrapped_key
    row.key_provider = envelope.key_provider
    row.key_id = envelope.key_id
    await db.flush()
    return sorted(changed)
