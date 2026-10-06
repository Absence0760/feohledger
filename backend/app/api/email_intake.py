"""Email-intake webhooks + org-admin token management.

Two classes of endpoint:

1. ``POST /api/email-intake/inbound/{provider}`` — PUBLIC, no JWT.
   Called by the email provider (SES via SNS, Mailgun, etc). Security
   is HMAC-signature verification of the body, plus the opaque
   per-tenant token embedded in the recipient address. Both layers are
   required in prod — see backend/docs/email-intake.md.

2. ``/api/organization/email-intake/*`` — JWT-authenticated, admin only.
   Shows the current intake address and rotates the token on demand
   (e.g. after a suspected leak).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ROLE_ADMIN, get_org_id, require_roles
from app.config import settings
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.services.email_intake import (
    intake_address_for,
    process_inbound_email,
    provision_intake_token,
    verify_signature,
)
from app.services.email_intake_adapters import get_parser
from app.tenant import lock_organization

logger = logging.getLogger(__name__)

public_router = APIRouter(prefix="/email-intake", tags=["email-intake"])
admin_router = APIRouter(prefix="/organization/email-intake", tags=["email-intake"])


def _ack() -> JSONResponse:
    """Opaque 200 ack — the SAME body on every *decision* the handler reaches
    once the request has passed signature verification: unknown/disabled intake
    token, duplicate delivery, no usable attachment, or genuine success.

    Anyone holding the platform-wide ``FEOH_EMAIL_INTAKE_SIGNING_SECRET`` (shared
    across all tenants, since the provider doesn't know tenants) could
    otherwise grind per-tenant intake tokens by watching for ``tenant_slug`` to
    populate in the response body. Per-request detail is logged server-side
    only. Mirrors ``slack_approvals.py``'s ``_ack`` helper.

    Deliberately NOT used for OUR OWN failures — see :func:`_retry_please`.
    """
    return JSONResponse({"status": "received"})


def _retry_please() -> Response:
    """Bodyless ``503`` — the ONE outcome that is not a decision.

    A decision (unknown token, duplicate, no usable attachment, success) is a
    final answer about *this* message and gets the uniform ack above. A failure
    of OURS — S3 unreachable, the tenant DB down, Redis flapping — is not an
    answer at all: the message is still the vendor's unprocessed invoice.

    ``email_intake.process_inbound_email`` already releases its Redis dedup
    claim and re-raises on exactly those failures, commenting that this "lets
    the NEXT delivery of the same message_id actually retry the work". Acking
    ``200`` told SES / Mailgun the message was delivered, so there *was* no next
    delivery: the release-on-failure code was preparing for a retry that could
    never come, and the invoice was gone with only a log line behind it.

    The trade-off this reopens is bounded and deliberate. The uniform ack exists
    because the intake signing secret is platform-wide, so a response that
    varies by outcome is an oracle for a per-tenant intake token. A 5xx on OUR
    failure narrows that oracle to "while the platform is already broken" — an
    attacker learns only that something inside us threw, never whose token they
    guessed, and only during an outage. Losing every invoice that arrives during
    a blip is the larger harm. ``api/billing_webhook.py`` faced the identical
    choice and went this way already; ``api/erp_webhook.py`` now matches.

    Bodyless so the response itself still carries nothing — no detail, no stack
    trace, no tenant.
    """
    return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Public webhook
# ---------------------------------------------------------------------------


@public_router.post("/inbound/{provider}", status_code=status.HTTP_200_OK)
async def inbound_webhook(
    provider: str,
    request: Request,
    ctrl_db: AsyncSession = Depends(get_control_db),
):
    """Webhook endpoint the email provider posts to on each inbound message.

    Signature header names differ per provider; we check a few common ones
    so founders can point any provider at this URL without rewriting it.
    """
    # Pre-signature rejections (unknown provider / bad signature / unparseable
    # body) return 204 silently so the response can't be used to enumerate
    # which providers / signing secrets / payload shapes the tenant accepts.
    # Distinct 4xx codes leaked that information. Once the signature verifies,
    # every *decision* — unknown/disabled intake token, duplicate delivery, no
    # usable attachments, or genuine success — returns the SAME opaque 200 ack
    # via `_ack()`, so the per-tenant intake token can't be enumerated either.
    # The one exception is a failure of OURS, which returns a bodyless 503 so
    # the provider redelivers instead of the invoice being lost (see
    # `_retry_please`).
    parser = get_parser(provider)
    if parser is None:
        logger.warning("Email intake: unknown provider %s", provider)
        return Response(status_code=204)

    # Bound the body BEFORE buffering it. A POST would otherwise be read fully
    # into memory before the signature check ever runs (memory-exhaustion DoS
    # on a public, unauthenticated route). Reject on the declared
    # Content-Length when present, and re-check the actual read in case the
    # header lied / was absent (chunked). Email payloads (incl. base64
    # attachments) can legitimately run larger than other webhooks; cap
    # defaults to a few MB.
    max_bytes = settings.email_intake_max_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                logger.warning("Email intake rejected: body exceeds size cap provider=%s", provider)
                return Response(status_code=204)
        except ValueError:
            logger.warning("Email intake rejected: invalid content-length provider=%s", provider)
            return Response(status_code=204)

    body = await request.body()
    if len(body) > max_bytes:
        logger.warning("Email intake rejected: body exceeds size cap provider=%s", provider)
        return Response(status_code=204)

    signature = (
        request.headers.get("X-Signature")
        or request.headers.get("X-Webhook-Signature")
        or request.headers.get("X-Mailgun-Signature-V2")
    )
    if not verify_signature(body, signature):
        logger.warning("Email intake signature rejected for provider=%s", provider)
        return Response(status_code=204)

    payload = parser(body, dict(request.headers))
    if payload is None:
        logger.warning("Email intake: could not parse payload for provider=%s", provider)
        return Response(status_code=204)

    try:
        result = await process_inbound_email(ctrl_db, payload)
    except Exception:  # noqa: BLE001 — never surface a stack trace on a public route
        # OUR failure, not a decision about the message. `process_inbound_email`
        # has already released its dedup claim precisely so the next delivery
        # can retry — ask for that delivery. Bodyless, so nothing leaks.
        logger.exception("Email intake: processing failed for provider=%s", provider)
        return _retry_please()

    # Log the real outcome server-side only; the response is a uniform ack
    # regardless of tenant/token resolution so it can't be used as an oracle.
    logger.info(
        "Email intake processed: provider=%s tenant=%s invoices_created=%d error=%s",
        provider,
        result.tenant_slug,
        len(result.invoices_created),
        result.error,
    )
    return _ack()


# ---------------------------------------------------------------------------
# Admin: show / rotate intake address
# ---------------------------------------------------------------------------


@admin_router.get("", status_code=status.HTTP_200_OK)
async def get_intake_address(
    ctrl_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    org = await _get_org_or_404(ctrl_db, org_id)
    # `address` is None for TWO different reasons — the platform has no
    # `FEOH_EMAIL_INTAKE_DOMAIN` (the committed default), or this org has no
    # token yet — and the two call for opposite things in the UI: "email intake
    # isn't available on this deployment" versus "click to create your address".
    # `domain_configured` separates them, so the admin panel can render the
    # unavailable state on its FIRST read instead of having to mint a throwaway
    # token to find out. It is an operator-config boolean, not tenant data, so
    # it is PII-free and needs no migration.
    return {
        "address": intake_address_for(org),
        "enabled": bool(((org.settings or {}).get("email_intake") or {}).get("enabled")),
        "domain_configured": bool(settings.email_intake_domain),
    }


@admin_router.post("/rotate-token", status_code=status.HTTP_200_OK)
async def rotate_intake_token(
    ctrl_db: AsyncSession = Depends(get_control_db),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    org_id: uuid.UUID = Depends(get_org_id),
):
    """Generate a new token; the old address stops accepting email immediately."""
    org = await _get_org_or_404(ctrl_db, org_id)
    # Serialise with every other settings writer (`lock_organization`).
    org = await lock_organization(ctrl_db, org)
    provision_intake_token(org)
    await ctrl_db.commit()
    await ctrl_db.refresh(org)
    return {"address": intake_address_for(org)}


async def _get_org_or_404(ctrl_db: AsyncSession, org_id: uuid.UUID) -> Organization:
    q = await ctrl_db.execute(select(Organization).where(Organization.id == org_id))
    org = q.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org
