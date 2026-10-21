import uuid

from sqlalchemy import ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    # The SCIM auth path resolves a tenant from the bearer digest on every
    # provisioning call. UNIQUE — two orgs sharing a digest would make that
    # lookup ambiguous — and partial, because the overwhelming majority of orgs
    # have SCIM off (NULL) and NULLs must not collide. Migration 0021 created
    # it; declared here so `create_all` builds it too.
    __table_args__ = (
        Index(
            "ix_organizations_scim_bearer_hash",
            "scim_bearer_hash",
            unique=True,
            postgresql_where=text("scim_bearer_hash IS NOT NULL"),
        ),
        # The realm index (migration 0117, decisions §295): one tenant per
        # OAuth ERP company. QuickBooks' webhooks name the company alone, and
        # this is how they find the tenant; two tenants on one company would
        # post into each other's books, so the database refuses the second.
        Index(
            "uq_organizations_erp_oauth_company",
            text("((settings -> 'erp' -> 'oauth') ->> 'provider')"),
            text("((settings -> 'erp' -> 'oauth') ->> 'external_tenant_id')"),
            unique=True,
            postgresql_where=text(
                "(((settings -> 'erp' -> 'oauth') ->> 'external_tenant_id') IS NOT NULL)"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    settings: Mapped[dict | None] = mapped_column(JSONB, default=dict)
    plan: Mapped[str] = mapped_column(String(50), default="free")
    db_name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    # sha256(hex) digest of the per-tenant SCIM bearer token. The plaintext
    # is shown to the admin exactly once at mint time and never persisted
    # again. Indexed (unique, partial) so the SCIM auth path can resolve
    # the tenant in O(log n) instead of scanning every org.
    scim_bearer_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)

    # Reseller / partner hierarchy (white-label). A nullable self-FK: when set,
    # THIS org is a branded CHILD tenant administered by the parent (partner /
    # reseller) org. NULL = a standalone tenant with no parent. A "partner" org
    # is simply any org referenced here by >= 1 child — there is no separate
    # flag, so a partner can't claim children it didn't actually parent. The
    # `/api/partner` surface scopes every child query to
    # `parent_org_id == <caller's org id>`, so the partner only ever sees /
    # affects its own children (the JWT org-claim cross-check in
    # `app.tenant.get_tenant` gates which org the caller resolves to first).
    # Control-plane only (orgs live in the control plane) — migration 0065.
    # See `docs/white-label.md` § Partner / reseller admin.
    parent_org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        default=None,
    )

    # Relationships (only users live in control-plane DB alongside orgs)
    users: Mapped[list["User"]] = relationship(back_populates="organization")  # noqa: F821
    children: Mapped[list["Organization"]] = relationship(
        back_populates="parent",
        foreign_keys=[parent_org_id],
    )
    parent: Mapped["Organization | None"] = relationship(
        back_populates="children",
        remote_side=[id],
        foreign_keys=[parent_org_id],
    )
