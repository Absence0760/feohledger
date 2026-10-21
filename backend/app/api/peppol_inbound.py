"""PEPPOL AS4 inbound receive webhook (the receiver corner, C4).

``POST /api/peppol/inbound/{organization_id}`` — PUBLIC, no JWT.

Called by the receiver's Access Point (C3) when it delivers an inbound BIS
Billing 3.0 document to us. Security is the provider's HMAC signature over the
raw body (verified via :func:`peppol_receive.verify_inbound_signature` +
``webhook_security.extract_signature_header``). The tenant is encoded in the URL
path — each tenant configures its own inbound URL with the Access Point, so a
leaked URL only affects that one tenant (mirrors the payment webhook).

**The path names the organisation's id, never its slug** (decisions §306). The
signing key is platform-wide, so the path is the only thing binding a delivery to
a tenant, and a slug can change hands: once a closed workspace's slug has rested
(`utils/slug.CLOSED_SLUG_COOLDOWN`) a new workspace may take it, and an Access
Point still configured with the old URL would deliver the closed tenant's
supplier invoices to the new owner. An organisation id is never reused, so a
URL for a closed tenant resolves to nothing, for ever.

Every rejection path returns ``204`` silently with a PII-free ``logger.warning``
naming only a reason code — never a 4xx (which would enumerate which tenant
slugs / signing secrets / payload shapes are accepted) and never the supplier's
participant value / tax id / payload in the log or body.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_control_db
from app.models.organization import Organization
from app.services.peppol_adapters import UnknownPeppolProviderError, get_peppol_adapter
from app.services.peppol_receive import receive_peppol_message, verify_inbound_signature
from app.services.webhook_security import extract_signature_header

logger = logging.getLogger(__name__)

public_router = APIRouter(prefix="/peppol", tags=["peppol"])


@public_router.post("/inbound/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
async def peppol_inbound_webhook(
    organization_id: str,
    request: Request,
    ctrl_db: AsyncSession = Depends(get_control_db),
) -> Response:
    """Receive an inbound PEPPOL document from the Access Point.

    PUBLIC-BY-DESIGN, no JWT — the HMAC signature is the gate. ``get_control_db``
    is a plain DB session, not an auth dependency (the route stays in
    ``ALTERNATE_AUTH``); the tenant is taken from the URL path, never a header.
    Returns 204 on every path — success AND every rejection — so the response
    can't be used to enumerate tenants or probe for the signing secret.
    """
    # 1. Master switch (mirrors email-intake's domain gate). Closed by default.
    if not settings.peppol_inbound_enabled:
        logger.warning("PEPPOL inbound disabled")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # 1b. Bound the body BEFORE buffering it. A signed-but-oversized POST would
    #     otherwise be read fully into memory then handed to lxml (memory-
    #     exhaustion on a public route). Reject on the declared Content-Length
    #     when present, and re-check the actual read in case the header lied /
    #     was absent (chunked). PEPPOL UBL is tens of KB; cap defaults to a few MB.
    max_bytes = settings.peppol_inbound_max_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                logger.warning("PEPPOL inbound rejected: body exceeds size cap")
                return Response(status_code=status.HTTP_204_NO_CONTENT)
        except ValueError:
            logger.warning("PEPPOL inbound rejected: invalid content-length")
            return Response(status_code=status.HTTP_204_NO_CONTENT)

    body = await request.body()
    if len(body) > max_bytes:
        logger.warning("PEPPOL inbound rejected: body exceeds size cap")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    headers = dict(request.headers)

    # 2. Verify the gateway HMAC over the raw bytes. The signature is the gate
    #    on this public route (auth-before-everything for a public-by-design
    #    webhook). Reuse the multi-candidate header lookup from webhook_security.
    signature = extract_signature_header(
        headers, "X-Peppol-Signature", "X-Signature", "X-Webhook-Signature"
    )
    if not verify_inbound_signature(body, signature):
        logger.warning("PEPPOL inbound signature rejected")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # 3. Resolve the tenant from the URL path — its id, which no later tenant
    #    can ever hold. A malformed id is the same silent 204 as an unknown one
    #    (a 422 would tell a prober the shape is wrong).
    try:
        org_uuid = uuid.UUID(organization_id)
    except ValueError:
        logger.warning("PEPPOL inbound: unknown tenant")
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    org = (
        await ctrl_db.execute(select(Organization).where(Organization.id == org_uuid))
    ).scalar_one_or_none()
    if org is None:
        logger.warning("PEPPOL inbound: unknown tenant")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # 4. Parse the inbound metadata + payload via the tenant's configured
    #    adapter. None = unparseable / missing message id → can't dedupe →
    #    refuse (mirrors email-intake's parse-None drop).
    #    A provider we have no adapter for is refused rather than resolved to
    #    `mock`, whose `parse_inbound` reads a permissive dev envelope
    #    (`decisions.md` §29). That is OUR failure, not a decision about this
    #    message (§37): the document is still unprocessed work and a redelivery
    #    after the admin fixes the setting would succeed, so ask the Access
    #    Point to retry with a bodyless 503 instead of acking a document we
    #    dropped. Bodyless, so it still carries no detail and no tenant, and it
    #    narrows the enumeration surface to "while this tenant is already
    #    misconfigured" — the same trade §37 accepted for email intake.
    try:
        adapter = get_peppol_adapter((org.settings or {}).get("peppol"))
    except UnknownPeppolProviderError as exc:
        logger.warning(
            "PEPPOL inbound: provider %r has no registered adapter — asking for redelivery",
            exc.provider,
        )
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    message = adapter.parse_inbound(headers, body)
    if message is None or not message.message_id:
        logger.warning("PEPPOL inbound: unparseable delivery or missing message id")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # 5. Hand off to the receive service. 204 regardless of the result —
    #    accepted, duplicate, and every soft-reject are all silent 204.
    result = await receive_peppol_message(ctrl_db, organization_id=org.id, message=message)

    if not result.accepted:
        logger.warning("PEPPOL inbound not accepted: %s", result.reason or "unknown")

    return Response(status_code=status.HTTP_204_NO_CONTENT)
