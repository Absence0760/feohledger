import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, TimestampMixin


class PaymentRun(Base, EntityMixin, TimestampMixin):
    __tablename__ = "payment_runs"

    # AI Cash-Flow Copilot Phase 3 idempotency anchor: at most one run per
    # non-NULL `plan_id`. Retrying `POST /api/cash-flow/plans/{plan_id}/
    # draft-run` for the SAME deterministic plan id
    # (`services/cash_flow_plan.compute_plan_id`) must return the existing
    # draft run, never stage a second one. NULL on every run created through
    # the ordinary `POST /api/payments/runs` flow, so ordinary manual runs
    # never contend with each other or this index. Mirrors migration 0075's
    # partial-unique-index style; declared here so fresh tenants built via
    # create_all in tenant_provisioning get it too, not only migrated ones.
    __table_args__ = (
        Index(
            "uq_payment_runs_plan_id",
            "plan_id",
            unique=True,
            postgresql_where=text("plan_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Set at creation time when total_amount > org's cfo_approval_above
    # threshold. While True, /execute returns 409 unless the actor holds
    # the CFO role and `cfo_approved_at` is also set.
    requires_cfo_approval: Mapped[bool] = mapped_column(default=False, nullable=False)
    cfo_approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cfo_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # See the partial unique index above. NULL for every manually-created run.
    plan_id: Mapped[str | None] = mapped_column(String(64))
    # NACHA files exported for this run (`GET /api/payments/runs/{id}/nacha`,
    # migration 0106). Drives the file-ID modifier — A for the first, B for a
    # regenerated second — so the customer's bank can tell them apart.
    nacha_export_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )


class PaymentSchedule(Base, TimestampMixin):
    __tablename__ = "payment_schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False, index=True
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    discount_date: Mapped[date | None] = mapped_column(Date)
    discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    payment_terms: Mapped[str | None] = mapped_column(String(100))


class Payment(Base, EntityMixin, TimestampMixin):
    __tablename__ = "payments"

    # One LIVE payment per invoice: partial unique index covering every payment
    # that isn't terminal (`voided`/`failed`/`cancelled`). This is the DB-level
    # idempotency backstop for the money invariant — a retried / double-clicked /
    # concurrent `POST /api/payments` can no longer book a second full-amount
    # payment for the same invoice. Terminal states are excluded so a void
    # (which hands the invoice back to `approved` to be re-paid) or a failed
    # attempt still lets a fresh payment be booked. Mirrors migration 0074;
    # declared here so fresh tenants built via create_all in tenant_provisioning
    # get it too, not only migrated ones.
    __table_args__ = (
        Index(
            "uq_payments_one_live_per_invoice",
            "invoice_id",
            unique=True,
            postgresql_where=text("status NOT IN ('voided', 'failed', 'cancelled')"),
        ),
        # `GET /api/payments`'s default order + its status chips. Same shape and
        # same reasoning as `ix_invoices_created_at_id` — see migration 0092.
        Index("ix_payments_created_at_id", text("created_at DESC"), text("id DESC")),
        Index(
            "ix_payments_status_created_at_id",
            "status",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # Corridor analytics / the international-rail reads. Partial because a
        # domestic payment leaves `corridor` NULL and those are the majority.
        # Migration 0017's; declared here so `create_all` builds it too.
        Index(
            "ix_payments_corridor",
            "corridor",
            postgresql_where=text("corridor IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False, index=True
    )
    # Indexed: every payment-run surface reads a run's payments by this key
    # (list rollup, detail, approve / execute, Positive Pay, ERP sync-back).
    # Without it each was a full scan of `payments` (migration 0101).
    payment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_runs.id"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    method: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    reference: Mapped[str | None] = mapped_column(String(255))
    # Which adapter handled this payment (`mock`, `modern_treasury`, ...).
    # Populated when the row is submitted to a processor. Null for pre-
    # adapter rows backfilled from the legacy fake-execute path.
    provider: Mapped[str | None] = mapped_column(String(50))
    # Processor's identifier — used to look up the row when a webhook lands.
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), index=True)
    # Set on `failed` / `cancelled`. Free-form so we can preserve the
    # processor's exact error message for debugging.
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # Attempt chain. Set on a payment booked by `POST /runs/{id}/retry-failed`
    # and points at the FAILED attempt it replaces. The retry never re-arms the
    # old row in place — that row is the immutable record of a failure that
    # really happened, and its `correlation_id` (the processor's idempotency
    # key), `provider_payment_id` and regulated timestamps are the only handles
    # anyone has for reconciling what attempt #1 actually did. This column is
    # what lets the run rollup count the LATEST attempt per invoice instead of
    # every row ever (see `services/payment_runs.active_run_payments`).
    retry_of_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id"), index=True
    )
    # The accepted early-payment discount this payment TAKES (migration 0104).
    # Booked when the run was built (`payment_runs.payable_amounts`): the
    # invoice-scoped `DiscountOffer` whose deadline the payment date met, and
    # the exact savings deducted, so `amount` is the invoice net of applied
    # credits MINUS `discount_amount`. Both NULL when no discount applies.
    #
    # Stored rather than re-derived on read because it is a fact about what was
    # AUTHORIZED: settlement verification, the capture on completion
    # (`discount_capture.capture_offer_for_settled_payment`) and the void
    # reversal all key on it, and none of them may depend on the offer's tiers
    # or the calendar still saying what they said at booking.
    discount_offer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("discount_offers.id", ondelete="SET NULL"),
        index=True,
    )
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    # Lifecycle timestamps. `submitted_at` = sent to processor; `completed_at`
    # = terminal status reported. Lets us compute settlement latency for
    # ops dashboards without parsing audit logs.
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # What the PROCESSOR said it actually settled, as reported on its webhook
    # (or re-fetched via `PaymentAdapter.fetch_settlement` for a rail whose
    # event body carries no amount). `amount` above is the AUTHORIZATION — what
    # AP instructed; this is the settlement — what the rail says it moved. The
    # two are compared by `services/payment_settlement.verify_settlement`.
    #
    # NULL is meaningful and is NOT zero: no processor ever reported a figure
    # for this payment (an amount-free rail, or a row predating migration
    # 0083). `settlement_coverage` treats NULL as "nothing indicates a
    # shortfall" and fails OPEN — the same posture the verifier takes toward an
    # absent amount — which is what stops an amount-free rail from holding
    # every invoice it settles.
    settled_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    settled_currency: Mapped[str | None] = mapped_column(String(3))

    # The rail reported a figure that does NOT fit `settled_amount`'s
    # NUMERIC(15, 2) — more than 13 integer digits (migration 0085). Distinct
    # from NULL above, and that distinction is the whole point: NULL means
    # nothing was ever reported and fails OPEN, whereas this means we were told
    # something we cannot represent, which must NOT read as "nothing
    # contradicts this invoice being settled". `settlement_coverage` returns
    # `uncertain` for it and the invoice holds at `payment_scheduled`.
    #
    # No legitimate settlement is 14 integer digits, so this is a corrupt or
    # hostile report, not a large payment — which is why the column is a flag
    # rather than a wider NUMERIC. The figure itself is preserved verbatim on
    # the append-only audit row (`SettlementVerification.as_details`, JSONB).
    settled_amount_unstorable: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    # International / multi-currency. NULL on domestic same-currency
    # payments; populated by `services.international_payments` when
    # the corridor needs an FX leg. See migration 0017.
    source_currency: Mapped[str | None] = mapped_column(String(3))
    source_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    fx_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    corridor: Mapped[str | None] = mapped_column(String(40))
    target_country: Mapped[str | None] = mapped_column(String(2))
