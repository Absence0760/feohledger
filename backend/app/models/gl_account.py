"""Chart of Accounts — GL account codes synced from ERP or manually managed."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, TimestampMixin

#: How long a GL code may be: the width of every column that RECORDS one —
#: ``invoices.gl_account``, ``invoice_line_items.gl_account``,
#: ``recurring_invoice_templates.gl_account`` and ``contract_line_items
#: .gl_account`` are all ``String(100)`` — so a chart code always fits where it is
#: coded. Was 50 until migration 0114, which cut QuickBooks sub-account paths
#: ("Landscaping Services:Job Materials:Decks and Patios" is 51) out of the
#: chart (docs/decisions.md §284).
GL_CODE_MAX_LENGTH = 100


class GLAccount(Base, EntityMixin, TimestampMixin):
    __tablename__ = "gl_accounts"

    # A GL code must resolve to exactly ONE account in any chart that uses it —
    # two rows answering to one code makes "which account was this invoice
    # coded to?" unanswerable, and `gl_recode._ActiveChart` / the extraction
    # catalog both treat a code as a set member, so the second row is invisible
    # right up until someone reconciles the GL.
    #
    # It takes TWO partial indexes, not one `UNIQUE (organization_id,
    # entity_id, code)`, because `entity_id` NULL is *meaningful* here (the
    # SHARED chart — unlike every other business table, where NULL is an
    # unstamped legacy row) and NULLs never compare equal in a unique index:
    # a plain 3-column unique would let the shared chart hold "6000" any
    # number of times, which is precisely the duplicate this exists to stop.
    #
    #   * shared rows  → unique on (organization_id, code) WHERE entity_id IS NULL
    #   * entity rows  → unique on (organization_id, entity_id, code) WHERE NOT NULL
    #
    # A shared "6000" and an entity's own "6000" therefore coexist by design —
    # that is an entity OVERRIDING the shared account, and the effective chart
    # `shared ∪ own` still resolves it to one row per entity because
    # `services/gl_account_sync._preference` prefers the entity's own.
    # Migration 0088 installs both on existing tenants.
    __table_args__ = (
        Index(
            "uq_gl_accounts_org_shared_code",
            "organization_id",
            "code",
            unique=True,
            postgresql_where=text("entity_id IS NULL"),
        ),
        Index(
            "uq_gl_accounts_org_entity_code",
            "organization_id",
            "entity_id",
            "code",
            unique=True,
            postgresql_where=text("entity_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(GL_CODE_MAX_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str | None] = mapped_column(
        String(50)
    )  # expense, asset, liability, revenue
    parent_code: Mapped[str | None] = mapped_column(String(GL_CODE_MAX_LENGTH))
    is_active: Mapped[bool] = mapped_column(default=True)
    erp_account_id: Mapped[str | None] = mapped_column(String(255))
    #: The ERP company ``erp_account_id`` was linked under
    #: (``services/erp_link_realm``). An id only means something in the company
    #: that issued it — QuickBooks numbers accounts per company — so a link from
    #: another realm is never posted to. NULL: linked before migration 0115.
    erp_link_realm: Mapped[str | None] = mapped_column(Text)
    #: Set when a person chose this row's ERP account among the candidates a
    #: sync could not choose between (``POST /api/gl-accounts/{id}/erp-link``).
    #: A confirmed link is not re-judged by the sync's ambiguity rules (3 and 4,
    #: ``services/gl_account_sync``); it still dies with its realm.
    erp_link_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    erp_link_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    #: ``{"realm", "reason", "accounts": [{"erp_account_id", "code", "name"}]}``
    #: — the ERP accounts the last sync found this UNLINKED row could mean and
    #: would not pick between. Non-NULL is "needs a decision"; cleared whenever
    #: the row is linked.
    erp_link_candidates: Mapped[dict | None] = mapped_column(JSONB)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
