import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, EntityMixin, TimestampMixin


class InvoiceStatus(enum.StrEnum):
    new = "new"
    pending = "pending"
    ready_for_review = "ready_for_review"
    approved = "approved"
    rejected = "rejected"
    sending_to_erp = "sending_to_erp"
    sent_to_erp = "sent_to_erp"
    posted_in_erp = "posted_in_erp"
    payment_scheduled = "payment_scheduled"
    paid = "paid"
    done = "done"
    failed = "failed"


class Invoice(Base, EntityMixin, TimestampMixin):
    __tablename__ = "invoices"
    # `updated_at` carries a server-side `onupdate=func.now()` (TimestampMixin);
    # without `eager_defaults`, SQLAlchemy just marks it expired after a flush
    # that touches the row, so the NEXT plain attribute read (e.g.
    # `InvoiceResponse.from_db`'s `inv.updated_at.isoformat()`, needed for the
    # optimistic-concurrency token on PATCH — see `schemas/invoice.py`) tries a
    # lazy re-SELECT outside an awaited context and raises `MissingGreenlet`
    # under the async ORM. `eager_defaults=True` uses PostgreSQL's `RETURNING`
    # on the same INSERT/UPDATE statement instead, so the attribute is already
    # populated by the time flush() returns — no separate refresh needed at any
    # of the many call sites that build an `InvoiceResponse` right after a
    # mutation. Same pitfall `api/scim.py::update_user` hit (fixed there with an
    # explicit `await db.refresh(user)`); this is the structural fix so a new
    # response field never has to remember the workaround.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), default=uuid.uuid4, unique=True, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    vendor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    # Materialized conversion of `amount` into the org's reporting (base)
    # currency. Populated by `services.currency_conversion.materialize_reporting_amount`
    # whenever the invoice is created / mutated, locking the FX rate AT THAT
    # TIME so historical rollups never silently recompute with today's rate
    # (project invariant: money is exact + auditable). NULL until first
    # materialized; when `currency == reporting_currency` the rate is 1 and
    # `reporting_amount == amount`. See backend/docs/multi-currency.md.
    reporting_currency: Mapped[str | None] = mapped_column(String(3))
    reporting_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    reporting_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    # WHICH currency `reporting_fx_rate` was fetched FOR. Without it the row
    # records a rate but not its pair, so a currency correction between two
    # FOREIGN currencies (EUR -> GBP on a USD-reporting org) leaves a stale
    # product that every rollup still labels 'converted'. Written by
    # `currency_conversion.materialize_reporting_amount` alongside the rate;
    # NULL on a row locked before migration 0086, where the check falls back
    # to the rate-shape heuristic. See backend/docs/multi-currency.md.
    reporting_source_currency: Mapped[str | None] = mapped_column(String(3))
    reporting_fx_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    received_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    payment_terms: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, native_enum=False, length=30),
        default=InvoiceStatus.new,
    )
    po_number: Mapped[str | None] = mapped_column(String(100))
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    shipping_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    remit_to_address: Mapped[str | None] = mapped_column(Text)
    bill_to_address: Mapped[str | None] = mapped_column(Text)
    vendor_address: Mapped[str | None] = mapped_column(Text)
    vendor_tax_id: Mapped[str | None] = mapped_column(String(50))
    ship_to_address: Mapped[str | None] = mapped_column(Text)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    payment_method: Mapped[str | None] = mapped_column(String(50))
    reference_number: Mapped[str | None] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text)
    approval_date: Mapped[date | None] = mapped_column(Date)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    rejected_by: Mapped[str | None] = mapped_column(String(255))
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Control-plane user ids (stringified UUIDs) implicated in this payable's
    # terms BESIDES the one in `uploaded_by_id`.
    # `approval_chain.violates_segregation` refuses an approval by anyone named
    # here exactly as it refuses the uploader, so segregation of duties keys on
    # a SET of people rather than a single column.
    #
    # One column could only ever name one person, and a payable can be shaped by
    # more than one: a recurring template's author writes the standing
    # instruction, and anyone who later repoints its vendor or amount shapes the
    # payable just as much (see `recurring_invoice_templates.material_editor_ids`
    # and `recurring_invoices.implicated_actor_ids`). Two writers:
    # `recurring_invoices.generate_one` (the template's author ∪ material
    # editors) and `intercompany.route_intercompany_invoice` (the source
    # payable's whole implicated set, carried onto its mirror). Every other
    # creation path has no second actor to name and leaves this NULL.
    #
    # NULL / empty means "nobody beyond the uploader", which is why it does not
    # disturb the fail-open NULL-uploader reading the three actor-less ingestion
    # channels (email intake, inbound PEPPOL, supplier-portal submit) depend on.
    # Strings, not UUIDs: this is JSONB, and a uuid has no JSON representation.
    segregation_actor_ids: Mapped[list | None] = mapped_column(JSONB)
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    assigned_to: Mapped[str | None] = mapped_column(String(255))
    # Budget-dimension attributes. Procurement budgets attribute realised
    # invoice spend by an equality against one of these four columns — see
    # `services.budget_service._DIMENSION_MATCH_COLUMN` — so ALL FOUR are
    # indexed. They were not always: `cost_center` / `gl_account` predate
    # migration 0044 and carried no index until 0090, which made the same query
    # shape seq-scan `invoices` on two of the four dimensions (measured 7.7 ms
    # vs 1.8 ms at 40k invoices, 15.9 ms vs 7.4 ms at 200k, on the path
    # `GET /budgets/check` sits in before every requisition submit). Keep the
    # four symmetric: a new budget dimension needs its column indexed here AND
    # in a tenant-fanned migration, or it silently joins the slow half.
    gl_account: Mapped[str | None] = mapped_column(String(100), index=True)
    cost_center: Mapped[str | None] = mapped_column(String(100), index=True)
    department: Mapped[str | None] = mapped_column(String(100), index=True)
    project: Mapped[str | None] = mapped_column(String(100), index=True)
    file_url: Mapped[str | None] = mapped_column(String(1024))
    file_key: Mapped[str | None] = mapped_column(String(512))
    warnings: Mapped[list | None] = mapped_column(JSONB)
    # Latest PO match result. Shape: {status, match_type, po_id, po_number,
    # po_total, invoice_amount, variance, variance_pct, within_tolerance,
    # has_gr, gr_id, issues}. Populated by `services.invoice_warnings.refresh_warnings`
    # whenever the invoice changes. NULL when the invoice has no `po_number`.
    po_match: Mapped[dict | None] = mapped_column(JSONB)
    # Free-form per-invoice metadata bag. Currently holds the cached
    # audit-log summary under `meta["audit_summary"]`:
    #   {"text": str, "confidence_context": str|None,
    #    "source_fingerprint": {"count": int, "last_at": str|None},
    #    "generated_at": str, "model": str}
    # The summary is regenerated lazily (services.audit_summary) whenever the
    # audit-log fingerprint for this invoice's correlation_id changes — see
    # backend/docs/audit-summary.md. No PII / banking data is stored here.
    meta: Mapped[dict | None] = mapped_column(JSONB)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id")
    )
    # Spend-to-contract link. Set via POST /api/invoices/{id}/link-contract;
    # drives the contract spend rollup (services.contract_spend) and compliance
    # monitoring (services.contract_compliance). NULL = off-contract spend.
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id"), index=True
    )
    # Set on invoices auto-generated from a RecurringInvoiceTemplate. The
    # (template_id, period_key) pair is the idempotency key — a partial unique
    # index below makes a double-fire of the same period impossible to persist,
    # so the generation sweep can retry safely. NULL = not template-generated.
    recurring_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recurring_invoice_templates.id"), index=True
    )
    recurring_period_key: Mapped[str | None] = mapped_column(String(40))
    # Inter-company routing (multi-entity). Set when this invoice is a charge
    # between two subsidiaries of the SAME tenant: `counterparty_entity_id` names
    # the OTHER `entities` row (the entity that owes / is owed), and
    # `intercompany_mirror_id` self-references the paired Invoice — origin ↔
    # generated mirror, set on both. `services.intercompany.route_intercompany_invoice`
    # generates the mirror payable under the counterparty entity, using
    # `intercompany_mirror_id` as the idempotency guard (a set value means the
    # mirror already exists — never create a second). NULL on ordinary invoices.
    # See backend/docs/inter-company.md.
    counterparty_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entities.id")
    )
    intercompany_mirror_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id")
    )

    __table_args__ = (
        # Idempotency backstop for the supplier-portal PO flip
        # (POST /api/portal/purchase-orders/{id}/flip). The marker
        # `po-flip:<po_id>` is unique per tenant, so this partial unique index
        # makes a concurrent double-flip of the same PO impossible to persist —
        # the second INSERT raises IntegrityError, which the handler catches and
        # turns into the idempotent short-circuit response. The app-level
        # existing-invoice check stays as the fast path. Partial predicate keeps
        # it from ever constraining ordinary invoices' `reference_number`.
        Index(
            "uq_invoice_po_flip_ref",
            "reference_number",
            unique=True,
            postgresql_where=text("reference_number LIKE 'po-flip:%'"),
        ),
        # Idempotency for the recurring-invoice generation sweep: at most one
        # invoice per (template, period). A concurrent / retried double-fire of
        # the same period raises IntegrityError on the second INSERT, which the
        # sweep catches and turns into a no-op. Partial predicate keeps it from
        # ever constraining ordinary (non-recurring) invoices.
        Index(
            "uq_invoice_recurring_period",
            "recurring_template_id",
            "recurring_period_key",
            unique=True,
            postgresql_where=text("recurring_template_id IS NOT NULL"),
        ),
        # Idempotency backstop for inter-company routing
        # (POST /api/invoices/{id}/route-intercompany). The origin ↔ mirror link
        # is a 1:1 pairing, so an invoice may be named as the mirror-partner of
        # at most ONE other invoice. Two concurrent routing calls on the same
        # origin would each INSERT a mirror carrying
        # `intercompany_mirror_id = <origin id>`; this index makes the second
        # one impossible to persist (IntegrityError → clean 409) so a duplicate
        # live payable — a double liability — can never reach the books. The
        # app-level FOR UPDATE row lock in the routing endpoint stays as the
        # fast path. Partial predicate keeps it from ever constraining the
        # (overwhelming majority of) ordinary invoices, whose column is NULL.
        Index(
            "uq_invoice_intercompany_mirror",
            "intercompany_mirror_id",
            unique=True,
            postgresql_where=text("intercompany_mirror_id IS NOT NULL"),
        ),
        # invoice_warnings.refresh_warnings runs on EVERY invoice save and issues
        # several vendor-scoped "last N approved invoices" lookups (bank-change
        # detection, stat-anomaly history, price-variance history) filtered by
        # vendor_id + a status IN (...) list, ordered by created_at DESC LIMIT N.
        # Without this index each lookup was a full-table Parallel Seq Scan (a
        # ~300ms scan of ~30k buffers at 1.2M rows) — status stays a cheap
        # in-index Filter since vendor_id already narrows the scan to one
        # vendor's rows and the created_at order lets Postgres stop at LIMIT.
        Index("ix_invoices_vendor_id_created_at", "vendor_id", "created_at"),
        # Same duplicate-detection gate's invoice_number equality check
        # (`func.lower(func.trim(Invoice.invoice_number)) == ...`) was also a
        # full-table seq scan — a plain btree index can't be used against a
        # wrapped column, so this mirrors the exact expression as a functional
        # index.
        Index(
            "ix_invoices_invoice_number_norm",
            text("lower(trim(invoice_number))"),
        ),
        # `GET /api/invoices`'s own default order (`created_at DESC, id DESC`).
        # Without it page 1 of the list was a whole-table Parallel Seq Scan plus
        # a top-N heapsort — 15.8 ms / 4 952 buffers at 200k invoices, growing
        # with the table while the page stays at 20 rows. See migration 0092.
        Index("ix_invoices_created_at_id", text("created_at DESC"), text("id DESC")),
        # The same order under a status chip. Not redundant with the index
        # above: for a RARE, scattered status Postgres otherwise walks the plain
        # ordering index discarding non-matching rows until it has 20 (18 928
        # discarded for one page at 200k), and that count grows with the table.
        Index(
            "ix_invoices_status_created_at_id",
            "status",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )

    vendor_rel: Mapped["Vendor | None"] = relationship(back_populates="invoices")  # noqa: F821
    line_items: Mapped[list["InvoiceLineItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    extraction_results: Mapped[list["InvoiceExtractionResult"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class InvoiceLineItem(Base, TimestampMixin):
    __tablename__ = "invoice_line_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False, index=True
    )
    line_number: Mapped[int | None] = mapped_column(Integer)
    item_code: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    tax: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    gl_account: Mapped[str | None] = mapped_column(String(100))

    invoice: Mapped[Invoice] = relationship(back_populates="line_items")


class InvoiceExtractionResult(Base, TimestampMixin):
    __tablename__ = "invoice_extraction_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False, index=True
    )
    method: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    raw_result: Mapped[dict | None] = mapped_column(JSONB)
    # Tracks what the priors pipeline did for this extraction so the UI can
    # show the reviewer which past invoices / vendor-cache entries shaped
    # the output. Shape:
    #   {
    #     "vendor_cache_applied": ["currency", "tax_rate"],
    #     "rag_neighbors": [{"invoice_id": "...", "similarity": 0.87, ...}]
    #   }
    priors_metadata: Mapped[dict | None] = mapped_column(JSONB)

    invoice: Mapped[Invoice] = relationship(back_populates="extraction_results")
