"""Sealed third-party provider credentials — control-plane.

One row per (organization, settings block): the ERP, payment-rail or
card-issuer secrets of that block, JSON-encoded and envelope-encrypted as ONE
value (`services/credential_crypto`). The non-secret half of each block (which
provider, base URL, sandbox flag, …) stays in `Organization.settings`; the
single accessor `services/provider_credentials.provider_config` merges the two
for the adapters.

Placement: control plane, beside `organizations`, because that is where the
values it replaces lived (`Organization.settings`) and where every reader
already resolves the org — including the public webhook routes, which find the
tenant in the control plane before any tenant DB is open. It also lets
migration 0110 move each org's plaintext into this table and strip it from the
JSONB in ONE transaction on ONE database. A per-tenant table would have
needed a tenant migration that reads the control plane and a second pass to
strip the source after every tenant had copied, with no atomicity between them.
Tenant DBs share the app's database role, so a per-DB placement would add no
isolation against it; isolation here comes from the encryption context, which
binds each ciphertext to its organization id and block — a row copied to another
org fails to open. Registered in `tenant_provisioning.CONTROL_TABLES`.

`secret_fields` holds the NAMES of the paths sealed in the row (never values) so
"is a secret set?" — the only read the settings page gets — needs no decrypt.
"""

import uuid

from sqlalchemy import ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ProviderCredential(Base, TimestampMixin):
    __tablename__ = "provider_credentials"
    __table_args__ = (
        UniqueConstraint("organization_id", "block", name="uq_provider_credentials_org_block"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Plain reference, no ON DELETE CASCADE: `tenant_deletion.CONTROL_DELETIONS`
    # sweeps this table explicitly, and a cascade would let a forgotten table
    # slip out of that list unnoticed.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    block: Mapped[str] = mapped_column(String(32), nullable=False)
    secret_fields: Mapped[list[str]] = mapped_column(ARRAY(String(128)), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_provider: Mapped[str] = mapped_column(String(16), nullable=False)
    key_id: Mapped[str] = mapped_column(String(255), nullable=False)
