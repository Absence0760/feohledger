"""Email-to-invoice intake.

Receives forwarded-PDF invoices from a per-tenant inbound email address
(e.g. ``invoices+a1b2c3d4@ap.feohledger.com``) and routes each
attachment through the normal extraction pipeline. The AP team tells
their vendors "send POs to this address" and the invoice appears in the
queue without anyone touching the UI.

Public API:
    - ``provision_intake_token(org)`` — generate+persist a token on the org
    - ``resolve_tenant_from_recipient(ctrl_db, to_address)`` — reverse-lookup
    - ``process_inbound_email(ctrl_db, payload)`` — the webhook entry point

The webhook endpoint is provider-agnostic. We accept a normalized
:class:`InboundEmail` payload; provider-specific parsers live in
``services/email_intake_adapters/`` (SES SNS JSON, Mailgun form-data, etc).
This keeps the core intake logic testable and the provider plumbing
isolated.

Security:
    - Token in the recipient address is the tenant bearer — treat it like
      a password (constant-time compare against the stored token).
      Leaked token = spam channel into that tenant's AP queue. Which is why
      the recipient address never reaches a log line: a live token lands in
      the unresolved branch every time an org toggles intake off, and the
      address is a third party's PII besides. The miss is logged by shape
      (was a ``+token`` present at all), not by value.
    - HMAC-SHA256 signature verification against
      ``settings.email_intake_signing_secret`` when the header is present.
    - Dedup by the provider's ``message_id`` (shared
      ``webhook_security.is_event_already_processed`` Redis guard) before
      any Invoice is created — a provider retry or duplicate delivery of
      the same message must not create a second invoice. If invoice
      creation then fails **before the tenant commit** (e.g. S3/tenant-DB
      outage), the claim is released via ``release_event_claim`` so the next
      redelivery can retry instead of the message being silently dropped for
      the TTL window (mirrors ``api/cards.py``'s webhook claim/release
      discipline). **Past that commit the claim stands**: the invoices are
      durable, so releasing would invite the redelivery to create a SECOND
      payable per attachment. A post-commit failure (the extraction dispatch,
      which in ``lambda`` mode is a real SQS round trip) is logged by class
      name and swallowed; the invoice sits at ``pending`` and
      ``extraction_reaper`` ages it out, from where a reviewer re-runs
      extraction.
    - We silently drop attachments that are not PDFs / images (avoid
      shipping .docx Trojans into the extraction pipeline).
    - Rate-limiting is the provider's job — point SES at a Lambda that
      drops duplicates before they reach us, or use Mailgun's built-in
      rate limits.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.services.webhook_security import (
    event_claim_exists,
    is_event_already_processed,
    release_event_claim,
)

logger = logging.getLogger(__name__)

# Nil UUID sentinel — email intake has no human actor, so every audit trail
# written during intake points at this ID. The UI can translate it to "system
# (email)" when rendering.
SYSTEM_ACTOR_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")

_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/tiff",
    # Structured e-invoices: UBL 2.1 / standalone CII arrive as XML
    # attachments; Factur-X / ZUGFeRD arrive as PDF (covered above).
    "application/xml",
    "text/xml",
}


# ---------------------------------------------------------------------------
# Normalized payload shape — what every provider adapter emits
# ---------------------------------------------------------------------------


@dataclass
class InboundAttachment:
    filename: str
    content_type: str
    content: bytes  # decoded bytes; adapters handle base64/MIME-decoding


@dataclass
class InboundEmail:
    to: str  # the recipient address that hit our MX
    sender: str
    subject: str = ""
    message_id: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    attachments: list[InboundAttachment] = field(default_factory=list)


@dataclass
class IntakeResult:
    tenant_slug: str | None = None
    invoices_created: list[uuid.UUID] = field(default_factory=list)
    skipped_attachments: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "tenant_slug": self.tenant_slug,
            "invoices_created": [str(x) for x in self.invoices_created],
            "skipped_attachments": self.skipped_attachments,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------


def generate_intake_token(length: int = 16) -> str:
    """Cryptographically-random, URL-safe token used in the recipient address."""
    return secrets.token_urlsafe(length)[:length]


def provision_intake_token(org: Organization) -> str:
    """Assign a fresh intake token to an org (caller persists via db.commit())."""
    token = generate_intake_token()
    settings_ = dict(org.settings or {})
    intake = dict(settings_.get("email_intake") or {})
    intake["token"] = token
    intake["enabled"] = True
    intake["rotated_at"] = datetime.now(UTC).isoformat()
    settings_["email_intake"] = intake
    org.settings = settings_
    return token


def intake_address_for(org: Organization) -> str | None:
    """Render the public intake address, or None if not provisioned / configured."""
    domain = settings.email_intake_domain
    if not domain:
        return None
    token = ((org.settings or {}).get("email_intake") or {}).get("token")
    if not token:
        return None
    return f"invoices+{token}@{domain}"


# ---------------------------------------------------------------------------
# Recipient → tenant resolution
# ---------------------------------------------------------------------------


_PLUS_TOKEN_RE = re.compile(r"invoices\+([A-Za-z0-9_-]+)@", re.IGNORECASE)


def extract_token(to_address: str) -> str | None:
    """Parse the first ``+<token>@`` piece out of the recipient address."""
    tokens = extract_tokens(to_address)
    return tokens[0] if tokens else None


def extract_tokens(to_address: str) -> list[str]:
    """Every distinct ``+<token>@`` in a recipient string, in order.

    One message can be addressed to several intake addresses at once — a vendor
    billing two customers who both run on this platform puts both in the
    envelope, and SES reports every recipient its receipt rule matched in ONE
    notification. Each is a separate tenant's delivery, so each is resolved.
    """
    seen: list[str] = []
    for token in _PLUS_TOKEN_RE.findall(to_address or ""):
        if token not in seen:
            seen.append(token)
    return seen


async def resolve_tenants_from_recipient(
    ctrl_db: AsyncSession,
    to_address: str,
) -> list[Organization]:
    """Every organization whose ENABLED intake token appears in the recipients.

    Ordered by first appearance in ``to_address``; never repeats an org.
    """
    tokens = extract_tokens(to_address)
    if not tokens:
        return []

    # Postgres JSONB containment — the index-friendly form is
    # `settings @> '{"email_intake":{"token":"..."}}'` but we avoid a raw
    # fragment here and filter in Python on the small org set. For 10k
    # orgs this is still a single round-trip; revisit if it matters.
    q = await ctrl_db.execute(select(Organization))
    orgs = q.scalars().all()
    resolved: list[Organization] = []
    for token in tokens:
        for org in orgs:
            intake = (org.settings or {}).get("email_intake") or {}
            stored_token = intake.get("token")
            if (
                intake.get("enabled")
                and isinstance(stored_token, str)
                and hmac.compare_digest(stored_token.encode(), token.encode())
            ):
                if org not in resolved:
                    resolved.append(org)
                break
    return resolved


async def resolve_tenant_from_recipient(
    ctrl_db: AsyncSession,
    to_address: str,
) -> Organization | None:
    """The first organization whose intake token matches the recipient address."""
    orgs = await resolve_tenants_from_recipient(ctrl_db, to_address)
    return orgs[0] if orgs else None


def dedup_event_id(org: Organization, message_id: str) -> str:
    """The Redis dedup key for one TENANT's delivery of one message.

    Scoped by org, because ``Message-ID`` names the email, not the delivery: the
    same message addressed to two tenants' intake addresses carries ONE
    Message-ID (Mailgun forwards it once per matched recipient; SES lists both
    recipients in one notification). An unscoped key let whichever tenant was
    processed first claim it, and the other tenant's invoice was then dropped as
    a "duplicate delivery" it had never received. An empty id stays empty so
    the shared helper still logs it as un-dedupable rather than keying on the
    org alone.
    """
    if not message_id:
        return ""
    return f"{org.id}:{message_id}"


# ---------------------------------------------------------------------------
# HMAC verification (optional — when the provider signs the body)
# ---------------------------------------------------------------------------


def verify_signature(body: bytes, signature: str | None) -> bool:
    """Verify an HMAC-SHA256 signature of the webhook body.

    Fails closed: returns ``False`` whenever the secret is empty unless
    ``FEOH_DEBUG`` is true (local dev convenience). The startup guard in
    :func:`main.lifespan` already refuses to boot a deployed env that has
    ``email_intake_domain`` set but ``email_intake_signing_secret`` empty,
    so this branch only fires for an explicitly debug-mode developer.
    """
    secret = settings.email_intake_signing_secret
    if not secret:
        return bool(settings.debug)
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------


async def process_inbound_email(
    ctrl_db: AsyncSession,
    payload: InboundEmail,
) -> IntakeResult:
    """Route an inbound email to the right tenant and create invoices.

    Returns :class:`IntakeResult`. The caller (webhook endpoint) logs the
    result server-side and returns an opaque, uniform ack to the email
    provider — never the result body verbatim, which would let a caller
    holding the platform-wide signing secret enumerate valid intake tokens
    by watching for ``tenant_slug`` to populate.

    A message addressed to several tenants' intake addresses is delivered to
    EACH of them, one tenant transaction and one dedup claim apiece. Tenants
    are processed in order; if one raises, the ones before it have committed
    and keep their claims, so the provider's redelivery (the route answers
    503) dedupes those and retries only the tenant that failed.
    """
    result = IntakeResult()

    orgs = await resolve_tenants_from_recipient(ctrl_db, payload.to)
    if not orgs:
        result.error = "Unknown or disabled intake address"
        # The recipient address is NOT loggable: its ``+<token>`` part IS the
        # tenant bearer credential (see this module's Security docstring —
        # "treat it like a password"), and this branch is reached with a LIVE,
        # correct token whenever an org has simply toggled intake off. It is
        # also a third party's email address. So log the *shape* of the miss,
        # which is what an operator actually diagnoses from — a bad MX / wrong
        # address (no plus-token at all) reads differently from a token nothing
        # matched — and never the address itself.
        logger.warning(
            "Email intake: recipient did not resolve to an enabled intake "
            "address (token_present=%s)",
            extract_token(payload.to) is not None,
        )
        return result

    result.tenant_slug = orgs[0].slug
    outcomes = []
    for org in orgs:
        outcome = await _process_for_org(org, payload)
        outcomes.append(outcome)
        result.invoices_created.extend(outcome.invoices_created)
        for skipped in outcome.skipped_attachments:
            if skipped not in result.skipped_attachments:
                result.skipped_attachments.append(skipped)
    # The message is "processed" if any tenant took it; otherwise report the
    # first tenant's reason (they share one payload, so it is the same reason).
    if not any(o.error is None for o in outcomes):
        result.error = outcomes[0].error
    return result


async def _legacy_claim_live(message_id: str) -> bool:
    """TRANSITIONAL — is a pre-per-tenant ``email_intake:<message_id>`` claim live?

    Before claims were scoped by org, the key was the bare Message-ID, and it
    does not record WHICH tenant that delivery reached. A provider redelivery
    of such a message arriving after the deploy would miss the new scoped key
    and create a second payable. The only rule that can never duplicate is the
    old one: a live legacy claim means "already processed" for every tenant.
    Its cost is that the old cross-tenant drop persists for messages first
    delivered before the deploy, and only until their claim expires
    (``webhook_security.DEFAULT_DEDUP_TTL_SECONDS``, 72h).

    Delete this check, and :func:`webhook_security.event_claim_exists` if it
    has no other caller, once that TTL has elapsed after the per-tenant change
    is deployed — tracked in ``docs/followups.md``.
    """
    return await event_claim_exists("email_intake", message_id)


async def _process_for_org(org: Organization, payload: InboundEmail) -> IntakeResult:
    """Deliver one inbound message to ONE resolved tenant."""
    result = IntakeResult(tenant_slug=org.slug)

    # Dedup by the provider's message id — a provider retry (SES/Mailgun
    # retry-on-timeout) or a duplicate delivery must not create a second
    # Invoice from the same attachment. Mirrors payment/card/ERP webhook
    # dedup via the shared Redis SET-NX helper. The claim is per TENANT
    # (`dedup_event_id`): one Message-ID addressed to two tenants is two
    # deliveries. A missing message id can't be deduped (logged by the
    # helper) — always processed, same as the other webhook handlers.
    dedup_id = dedup_event_id(org, payload.message_id)
    if await _legacy_claim_live(payload.message_id) or await is_event_already_processed(
        "email_intake", dedup_id
    ):
        result.error = "Duplicate delivery"
        logger.info(
            "Email intake: duplicate delivery for tenant=%s message_id=%s",
            org.slug,
            payload.message_id,
        )
        return result

    attachments = list(_usable_attachments(payload.attachments, result))
    if not attachments:
        result.error = "No usable PDF / image / XML attachments"
        return result

    # Open a short-lived tenant session to create the invoice rows.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import _make_tenant_url
    from app.services.extraction_dispatch import dispatch_extraction

    tenant_engine = None
    try:
        try:
            # Engine construction sits INSIDE the releasing try: the claim is
            # already made, so anything that raises from here to the commit
            # must hand it back, or the redelivery is deduped and the
            # message is lost.
            tenant_engine = create_async_engine(
                _make_tenant_url(org.db_name), pool_size=1, max_overflow=0
            )
            tenant_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)
            async with tenant_factory() as tenant_db:
                # Email intake has no entity selector — land invoices under the
                # tenant's default entity so they stay visible in entity-scoped
                # views (multi-entity Phase 2). Resolved once per batch.
                from app.models.entity import Entity

                entity_id = (
                    await tenant_db.execute(select(Entity.id).where(Entity.is_default))
                ).scalar_one_or_none()
                for att in attachments:
                    invoice_id = await _create_invoice_from_attachment(
                        tenant_db=tenant_db,
                        org_id=org.id,
                        entity_id=entity_id,
                        sender=payload.sender,
                        subject=payload.subject,
                        attachment=att,
                    )
                    result.invoices_created.append(invoice_id)
                await tenant_db.commit()
        except Exception:
            # ONLY this block may release the claim, and only because nothing
            # it guards is durable yet. The dedup claim guards the invoice rows;
            # if S3 or the tenant DB was briefly unreachable they did not land,
            # so releasing lets the provider's redelivery reprocess instead of
            # the invoice being dropped for the full TTL. Mirrors
            # api/cards.py's webhook claim/release discipline. Re-raise so the
            # route answers 503 and the provider actually redelivers.
            result.invoices_created.clear()
            await release_event_claim("email_intake", dedup_id)
            raise

        # PAST THE COMMIT the claim MUST stand. The invoices exist; a failure
        # from here on is not "this message went unprocessed", and releasing
        # would invite the redelivery to create a SECOND set of payables for
        # the same email — a duplicate invoice per attachment, which is a
        # money-path defect rather than a lost document. `dispatch_extraction`
        # is a real failure candidate in `lambda` mode (a boto3 SQS round trip),
        # which is exactly how this used to fire. Same split
        # api/payments.py's webhook already uses: its post-commit ERP sync sits
        # OUTSIDE the try that releases.
        #
        # The cost of swallowing is bounded and already covered: an invoice
        # whose extraction never started sits at `pending` and the
        # `extraction_reaper` sweep ages it to `failed`, from which a reviewer
        # re-runs extraction. No document is lost.
        for invoice_id in result.invoices_created:
            try:
                await dispatch_extraction(invoice_id, org.id, SYSTEM_ACTOR_ID)
            except Exception as exc:  # noqa: BLE001 — class name only (PII)
                logger.warning(
                    "Email intake: extraction dispatch failed for invoice %s: %s "
                    "(invoice created; extraction_reaper will age it out)",
                    invoice_id,
                    exc.__class__.__name__,
                )
    finally:
        if tenant_engine is not None:
            await tenant_engine.dispose()

    return result


def _usable_attachments(
    attachments: Iterable[InboundAttachment],
    result: IntakeResult,
) -> Iterable[InboundAttachment]:
    from app.services.storage import _safe_filename

    for att in attachments:
        # Sanitise the reported filename: it is echoed back to the email
        # provider in the debug skip-list, never used as an S3 key, but we
        # strip path separators / control chars so a crafted filename can't
        # smuggle anything into a log or response body.
        safe_name = _safe_filename(att.filename)
        ct = (att.content_type or "").lower()
        if ct not in _ALLOWED_CONTENT_TYPES:
            result.skipped_attachments.append(f"{safe_name} ({ct or 'unknown'})")
            continue
        if not att.content:
            result.skipped_attachments.append(f"{safe_name} (empty)")
            continue
        yield att


async def _create_invoice_from_attachment(
    *,
    tenant_db: AsyncSession,
    org_id: uuid.UUID,
    entity_id: uuid.UUID | None,
    sender: str,
    subject: str,
    attachment: InboundAttachment,
) -> uuid.UUID:
    invoice = Invoice(
        invoice_number="",
        vendor_name="",
        description=f"Received by email from {sender}" + (f" — {subject}" if subject else ""),
        amount=Decimal("0"),
        currency="USD",
        status=InvoiceStatus.pending,  # skip 'new' — intake = trigger extraction
        organization_id=org_id,
        entity_id=entity_id,
        uploaded_by_id=None,  # system — no human uploader
    )
    tenant_db.add(invoice)
    await tenant_db.flush()

    # Freeze the workflow snapshot at ingest, exactly as every other ingress
    # does (`POST /api/invoices/upload`, manual create, the supplier portal,
    # `recurring_invoices`, `intercompany`). Without it an email-intake invoice
    # has no `WorkflowInstance` at all: a later edit to the tenant's workflow
    # definition then retroactively governs it (breaking the per-invoice
    # frozen-snapshot invariant for exactly the unattended paths), it has no
    # `WorkflowStep` rows, so it is invisible to the step-based approval-queue
    # reads and to `GET /api/invoices/{id}/workflow`, and it is never assigned
    # an A/B experiment variant.
    from app.services.workflow_engine import create_workflow_instance

    await create_workflow_instance(tenant_db, invoice)

    from app.services.storage import _put_object, _safe_filename

    file_key = f"{org_id}/{invoice.id}/{_safe_filename(attachment.filename)}"
    # boto3 is blocking; `_put_object` hands it to a worker thread so this
    # public webhook path never parks the event loop on an S3 round trip.
    await _put_object(file_key, attachment.content, attachment.content_type)
    invoice.file_key = file_key
    invoice.file_url = f"{settings.s3_endpoint_url.rstrip('/')}/{settings.s3_bucket}/{file_key}"
    return invoice.id
