"""Dynamic discounting — supplier-offered early-payment discount offers.

A :class:`DiscountOffer` is a *dynamic*, time-boxed early-payment proposal:
"pay within N days for X% off", optionally as a sliding scale of tiers
(5 days → 3%, 10 days → 2%, 15 days → 1%). Offers are scoped either to a
single invoice or to a vendor (a bulk "pay all open invoices from Vendor X
early for 2%" negotiation).

This is distinct from ``PaymentSchedule.discount_percent`` — that is the
*static* term captured at invoice creation ("2/10 net 30"). A
``DiscountOffer`` is negotiable, has its own accept / decline / expire
lifecycle, and is optimized against cash on hand (see
``services/discount_optimizer.py``) and auto-captured when its annualized
ROI clears the org threshold (see ``services/discount_auto_trigger.py``).

Money is ``Numeric`` and tier percents are stored as Decimal-strings inside
the ``tiers`` JSONB to preserve exactness (never float). See
``backend/docs/dynamic-discounting.md``.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, EntityMixin, TimestampMixin

# Lifecycle states.
OFFER_STATUS_OFFERED = "offered"
OFFER_STATUS_ACCEPTED = "accepted"
OFFER_STATUS_CAPTURED = "captured"
OFFER_STATUS_DECLINED = "declined"
OFFER_STATUS_EXPIRED = "expired"

# Who originated the offer.
OFFER_SOURCE_SUPPLIER = "supplier"  # supplier proposed it (portal / negotiation)
OFFER_SOURCE_SYSTEM = "system"  # platform derived it from static terms
OFFER_SOURCE_FINANCING = "financing"  # a supply-chain-finance marketplace funded it

# Scope.
OFFER_SCOPE_INVOICE = "invoice"
OFFER_SCOPE_VENDOR = "vendor"  # bulk negotiation across a vendor's open invoices


class DiscountOffer(Base, EntityMixin, TimestampMixin):
    __tablename__ = "discount_offers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )

    # Scope — exactly one of invoice_id / vendor_id is set per `scope`.
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default=OFFER_SCOPE_INVOICE)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), index=True
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id"), index=True
    )

    source: Mapped[str] = mapped_column(String(20), nullable=False, default=OFFER_SOURCE_SUPPLIER)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OFFER_STATUS_OFFERED, index=True
    )

    # Sliding scale: list of {"days": int, "percent": "3.00"}. Percent is a
    # Decimal-string (JSON has no Decimal) — parse with Decimal on read.
    tiers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Amount the discount applies to (invoice amount, or the summed open
    # balance for a vendor-scoped bulk offer). Currency for display/rollup.
    base_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")

    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)

    # Set when accepted: the chosen tier {"days", "percent"} and who/when.
    accepted_tier: Mapped[dict | None] = mapped_column(JSONB)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Realized discount once captured (status → captured). Lets analytics sum
    # actual savings without recomputing against the tier table.
    captured_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The settled payment that realized the capture (migration 0104). Lets a
    # void reverse exactly the capture its own payment made instead of
    # attributing it by elimination; NULL on a capture made before the column
    # existed, which the void still handles by the old elimination rule
    # (`discount_capture.reverse_captures_for_voided_payment`). `use_alter`
    # because `payments.discount_offer_id` points back the other way.
    captured_by_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "payments.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_discount_offers_captured_by_payment_id",
        ),
        index=True,
    )

    # Populated when source == "financing" — which marketplace funded the
    # early payment (see services/financing_adapters/).
    financing_provider: Mapped[str | None] = mapped_column(String(50))

    notes: Mapped[str | None] = mapped_column(String(500))
    meta: Mapped[dict | None] = mapped_column(JSONB)
