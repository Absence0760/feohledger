"""Outbound-webhook management — subscription CRUD + delivery log + redelivery.

Admin-facing endpoints, gated by the normal JWT session + ``require_roles(
ROLE_ADMIN)`` (NOT the X-API-Key path — outbound-webhook config is a
control-plane admin action). They operate on the control-plane
``webhook_subscriptions`` / ``webhook_deliveries`` tables.

Create returns the signing secret EXACTLY ONCE (like an API-key mint); list/get
responses carry only ``secret_prefix`` — never the full secret. Every mutation
writes an append-only, PII-free audit row.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, ensure_entitlement, require_entitlement, require_roles
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.models.webhook import (
    DELIVERY_DEAD,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    WEBHOOK_EVENT_TYPES,
    WebhookDelivery,
    WebhookSubscription,
)
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.billing.plan_catalog import FEATURE_PUBLIC_API
from app.services.webhooks.rotation import (
    DEFAULT_OVERLAP_MINUTES,
    MAX_OVERLAP_MINUTES,
    MIN_OVERLAP_MINUTES,
    rotate_secret,
)
from app.services.webhooks.signing import generate_signing_secret
from app.services.webhooks.url_guard import (
    REJECT_DETAIL,
    WebhookTargetNotAllowed,
    ensure_public_webhook_target,
)
from app.tenant import get_tenant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _require_public_target(target_url: str) -> None:
    """SSRF gate on the supplied target URL (resolves the host; rejects
    loopback / private / link-local / metadata / other non-public addresses).
    One generic, non-enumerating 422 for every rejection reason. The same
    guard runs again immediately before each dispatch (DNS-rebinding TOCTOU)."""
    try:
        await ensure_public_webhook_target(target_url)
    except WebhookTargetNotAllowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=REJECT_DETAIL
        ) from None


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateSubscriptionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    target_url: str = Field(..., min_length=1, max_length=2048)
    event_types: list[str] = Field(..., min_length=1)

    @field_validator("target_url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("target_url must be an http(s) URL")
        return v

    @field_validator("event_types")
    @classmethod
    def _validate_events(cls, v: list[str]) -> list[str]:
        unknown = [e for e in v if e not in WEBHOOK_EVENT_TYPES]
        if unknown:
            raise ValueError(
                f"unknown event type(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(WEBHOOK_EVENT_TYPES)}"
            )
        # de-dupe, preserve order
        return list(dict.fromkeys(v))


class UpdateSubscriptionRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    target_url: str | None = Field(None, min_length=1, max_length=2048)
    event_types: list[str] | None = None
    active: bool | None = None

    @field_validator("target_url")
    @classmethod
    def _validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("target_url must be an http(s) URL")
        return v

    @field_validator("event_types")
    @classmethod
    def _validate_events(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if not v:
            raise ValueError("event_types cannot be empty")
        unknown = [e for e in v if e not in WEBHOOK_EVENT_TYPES]
        if unknown:
            raise ValueError(f"unknown event type(s): {', '.join(unknown)}")
        return list(dict.fromkeys(v))


class SubscriptionResponse(BaseModel):
    """Subscription metadata — never carries the full signing secret."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    target_url: str
    event_types: list[str]
    secret_prefix: str
    active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # When a rotation's overlap window closes — i.e. when the retiring secret
    # stops signing `X-Webhook-Signature-Previous`. NULL when no rotation is in
    # flight, which is the ordinary state.
    #
    # The EXPIRY only, never `previous_signing_secret`: this response is the
    # list/get surface, and the whole point of the create/rotate contract is
    # that a secret is shown exactly once. Surfacing the timestamp lets an admin
    # see a rotation is mid-flight after a page reload — without it the UI can
    # only remember the window for the life of one page view, which is precisely
    # when someone is away pasting the new secret into another system.
    previous_secret_expires_at: datetime | None = None


class SubscriptionCreatedResponse(BaseModel):
    """The create response — the ONLY place the signing secret is returned."""

    subscription: SubscriptionResponse
    signing_secret: str


class DeliveryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    subscription_id: uuid.UUID
    event_id: str
    event_type: str
    status: str
    attempt_count: int
    response_code: int | None = None
    next_attempt_at: datetime | None = None
    last_attempt_at: datetime | None = None
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Subscription CRUD
# ---------------------------------------------------------------------------


@router.post("", response_model=SubscriptionCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_subscription(
    body: CreateSubscriptionRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    _entitled: User = Depends(require_entitlement(FEATURE_PUBLIC_API)),
    db: AsyncSession = Depends(get_control_db),
) -> SubscriptionCreatedResponse:
    """Create a webhook subscription. Returns the signing secret ONCE."""
    await _require_public_target(body.target_url)
    secret, prefix = generate_signing_secret()
    row = WebhookSubscription(
        id=uuid.uuid4(),
        organization_id=org.id,
        name=body.name,
        target_url=body.target_url,
        event_types=body.event_types,
        signing_secret=secret,
        secret_prefix=prefix,
        active=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="webhook_subscription.created",
        entity_id=row.id,
        details={
            "name": row.name,
            "event_types": row.event_types,
            "secret_prefix": row.secret_prefix,
        },
    )
    return SubscriptionCreatedResponse(
        subscription=SubscriptionResponse.model_validate(row),
        signing_secret=secret,
    )


@router.get("", response_model=list[SubscriptionResponse])
async def list_subscriptions(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
) -> list[SubscriptionResponse]:
    rows = (
        (
            await db.execute(
                select(WebhookSubscription)
                .where(WebhookSubscription.organization_id == org.id)
                .order_by(WebhookSubscription.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [SubscriptionResponse.model_validate(r) for r in rows]


async def _get_owned_subscription(
    db: AsyncSession, sub_id: uuid.UUID, org_id: uuid.UUID, *, for_update: bool = False
) -> WebhookSubscription:
    stmt = (
        select(WebhookSubscription)
        .where(
            WebhookSubscription.id == sub_id,
            WebhookSubscription.organization_id == org_id,
        )
        .execution_options(populate_existing=True)
    )
    # Lock the row for a mutation whose new state is DERIVED from the current
    # one — rotation carries the existing `signing_secret` into
    # `previous_signing_secret`. Two concurrent rotations (a double-click, or
    # two admins racing during incident response to a suspected leak — exactly
    # when this endpoint gets used) would otherwise both read the same current
    # secret, mint different replacements, and the second commit would silently
    # overwrite the first. The losing admin was handed a secret in their
    # response that was never persisted, so their receiver gets configured with
    # a key that will never verify and nothing surfaces the error.
    #
    # Serialized, the loser's secret becomes the PREVIOUS one instead — still
    # signing the overlap header, so their receiver keeps working through the
    # window rather than breaking silently.
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        # Same 404 for wrong-org and missing so we don't enumerate another
        # tenant's subscription ids.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Webhook subscription not found"
        )
    return row


@router.patch("/{sub_id}", response_model=SubscriptionResponse)
async def update_subscription(
    sub_id: uuid.UUID,
    body: UpdateSubscriptionRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
) -> SubscriptionResponse:
    # Switching a subscription OFF is open on every plan, so a downgraded
    # tenant can always quiet an integration it no longer pays for; any other
    # edit configures the outbound-webhook feature and needs the plan (§258).
    if body.active is not False:
        await ensure_entitlement(db, org.id, FEATURE_PUBLIC_API)
    if body.target_url is not None:
        await _require_public_target(body.target_url)
    row = await _get_owned_subscription(db, sub_id, org.id)
    changed: dict = {}
    if body.name is not None:
        row.name = body.name
        changed["name"] = body.name
    if body.target_url is not None:
        row.target_url = body.target_url
        changed["target_url_changed"] = True  # never log the URL value (may carry a token)
    if body.event_types is not None:
        row.event_types = body.event_types
        changed["event_types"] = body.event_types
    if body.active is not None:
        row.active = body.active
        changed["active"] = body.active
    if changed:
        await db.commit()
        await db.refresh(row)
        await dispatch_auth_audit(
            organization_id=org.id,
            actor_id=user.id,
            action="webhook_subscription.updated",
            entity_id=row.id,
            details=changed,
        )
    return SubscriptionResponse.model_validate(row)


class RotateSecretRequest(BaseModel):
    """How long the retiring secret keeps signing the secondary header.

    `0` is a deliberate, documented choice — a hard cutover for a
    known-compromised secret, where the old key must stop working immediately
    and a few rejected deliveries are the point rather than a cost.
    """

    overlap_minutes: int = Field(
        default=DEFAULT_OVERLAP_MINUTES,
        ge=MIN_OVERLAP_MINUTES,
        le=MAX_OVERLAP_MINUTES,
    )


class SecretRotatedResponse(BaseModel):
    """The rotate response — the only other place a signing secret is returned."""

    subscription: SubscriptionResponse
    signing_secret: str
    #: When the retiring secret stops signing. `None` on a hard cutover — it
    #: already has.
    previous_secret_expires_at: datetime | None = None


@router.post("/{sub_id}/rotate-secret", response_model=SecretRotatedResponse)
async def rotate_subscription_secret(
    sub_id: uuid.UUID,
    body: RotateSecretRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
) -> SecretRotatedResponse:
    """Replace a subscription's signing secret, keeping its id and history.

    Open on every plan, like switching a subscription off (decisions §258): a
    rotation is incident remediation, and a downgraded tenant with a leaked
    secret must not have to choose between the 402 and deleting the log.

    The secret is the customer's verification key, and anyone holding it can
    forge a signed `invoice.approved` / `payment.settled` payload into their
    receiver. Before this the only remedy on a leak was DELETE + re-create,
    which changes the subscription id and CASCADE-deletes the entire delivery
    log — so recovering from a leak meant destroying the evidence of what had
    been delivered.

    Returns the new secret EXACTLY ONCE, mirroring the create-time contract.

    By default the retiring secret keeps signing a second
    `X-Webhook-Signature-Previous` header for `overlap_minutes`, so a receiver
    that accepts either header rotates with no dropped deliveries. Pass
    `overlap_minutes: 0` for a hard cutover when the old secret is known
    compromised. See `backend/docs/public-api.md` § Rotating a signing secret
    for the receiver-side procedure.
    """
    row = await _get_owned_subscription(db, sub_id, org.id, for_update=True)
    result = rotate_secret(
        current_secret=row.signing_secret,
        now=datetime.now(UTC),
        overlap_minutes=body.overlap_minutes,
    )
    row.signing_secret = result.plaintext_secret
    row.secret_prefix = result.secret_prefix
    row.previous_signing_secret = result.previous_secret
    row.previous_secret_expires_at = result.previous_expires_at
    await db.commit()
    await db.refresh(row)

    # PII-free AND secret-free: the prefix is the non-secret label, and the
    # overlap is recorded so an auditor can see how long the retired key stayed
    # valid. Neither the old nor the new secret ever enters the trail.
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="webhook_subscription.secret_rotated",
        entity_id=row.id,
        details={
            "secret_prefix": row.secret_prefix,
            "overlap_minutes": body.overlap_minutes,
            "previous_secret_expires_at": (
                result.previous_expires_at.isoformat() if result.previous_expires_at else None
            ),
        },
    )
    return SecretRotatedResponse(
        subscription=SubscriptionResponse.model_validate(row),
        signing_secret=result.plaintext_secret,
        previous_secret_expires_at=result.previous_expires_at,
    )


@router.delete("/{sub_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subscription(
    sub_id: uuid.UUID,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
) -> None:
    row = await _get_owned_subscription(db, sub_id, org.id)
    await db.delete(row)  # CASCADE removes its deliveries
    await db.commit()
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="webhook_subscription.deleted",
        entity_id=sub_id,
        details={"name": row.name},
    )


# ---------------------------------------------------------------------------
# Delivery log + redelivery
# ---------------------------------------------------------------------------


@router.get("/deliveries", response_model=list[DeliveryResponse])
async def list_deliveries(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
    subscription_id: uuid.UUID | None = None,
    status_filter: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> list[DeliveryResponse]:
    """List this org's webhook deliveries (newest first), optionally filtered."""
    stmt = select(WebhookDelivery).where(WebhookDelivery.organization_id == org.id)
    if subscription_id is not None:
        stmt = stmt.where(WebhookDelivery.subscription_id == subscription_id)
    if status_filter is not None:
        stmt = stmt.where(WebhookDelivery.status == status_filter)
    stmt = (
        stmt.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [DeliveryResponse.model_validate(r) for r in rows]


@router.post("/deliveries/{delivery_id}/redeliver", response_model=DeliveryResponse)
async def redeliver(
    delivery_id: uuid.UUID,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    _entitled: User = Depends(require_entitlement(FEATURE_PUBLIC_API)),
    db: AsyncSession = Depends(get_control_db),
) -> DeliveryResponse:
    """Re-enqueue a failed / dead delivery and attempt it immediately.

    Only ``failed`` / ``dead`` deliveries can be redelivered — a re-send of an
    already-``delivered`` row would double-fire a side effect at the receiver.
    Resets the attempt counter and re-processes inline so the response reflects
    the new outcome.

    **The row is CLAIMED for the whole redelivery** (``FOR UPDATE``, held from
    the read until ``process_delivery`` commits), which is the same claim
    ``deliver_due`` and the emit path's immediate attempt take. This endpoint
    used to read the row unlocked, commit it back to ``pending`` with
    ``next_attempt_at = now()``, and only THEN POST — leaving a window in which
    the row was due, unclaimed and in flight, so a sweep tick landing there
    picked it up and POSTed the same delivery a second time, with both commits
    racing on ``attempt_count``. That is exactly the duplicate the claim was
    introduced to close on the other two paths; this one was left open.

    Holding the claim also serialises two admins hitting Redeliver at once: the
    second waits for the first to finish and then re-reads the row, instead of
    both passing the status guard on the same snapshot and both sending. What it
    sees is the first attempt's real outcome — ``delivered`` (or ``dead``) gives
    it the 409 the guard promises, while a first attempt that merely failed
    again leaves the row ``failed``, so the second click is a genuine second
    retry rather than a duplicate of an in-flight one.

    The trade-off is deliberate: because the requeue is no longer committed
    before the send, a failure in the send path's own commit rolls the row back
    to its pre-request state rather than leaving it queued for the sweep. That
    is the honest outcome — the admin sees the error and retries — and it is
    strictly better than a silent double-send.
    """
    row = (
        await db.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.id == delivery_id,
                WebhookDelivery.organization_id == org.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Webhook delivery not found"
        )
    if row.status not in (DELIVERY_FAILED, DELIVERY_DEAD):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed or dead deliveries can be redelivered.",
        )

    row.status = DELIVERY_PENDING
    row.attempt_count = 0
    row.next_attempt_at = datetime.now(UTC)

    # Opens its own short-lived tenant session and is fail-soft, so it neither
    # conflicts with the claim held here nor can break the redelivery.
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="webhook_delivery.redelivered",
        entity_id=row.id,
        details={"event_type": row.event_type, "event_id": row.event_id},
    )

    # Attempt inline so the caller sees the result. `process_delivery` commits,
    # which is what releases the claim taken above.
    from app.services.webhooks.delivery import process_delivery

    await process_delivery(db, row)
    await db.refresh(row)
    return DeliveryResponse.model_validate(row)
