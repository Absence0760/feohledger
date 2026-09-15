"""ERP webhook endpoint — receives status callbacks from ERPs and Merge.dev."""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.exception import Exception as APException
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.services.exception_service import create_exception
from app.services.payment_settlement import settlement_coverage
from app.services.webhook_security import (
    extract_signature_header,
    is_event_already_processed,
    release_event_claim,
    verify_hmac_sha256,
)
from app.services.workflow_engine import (
    VALID_TRANSITIONS,
    get_invoice_for_update,
    transition_invoice,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/erp", tags=["erp"])

# Exception type opened when the ERP reports an invoice void/cancel we can no
# longer safely auto-apply (the invoice already advanced past the point where
# ``→ failed`` is a legal transition). Free-form ``Exception.exception_type``
# string — no migration. See the § Exception types list in backend/CLAUDE.md.
ERP_RECONCILIATION_EXCEPTION_TYPE = "erp_reconciliation"


def _retry_please() -> Response:
    """Bodyless ``503`` — OUR failure, not a decision about the event.

    Every *decision* this handler reaches (unknown tenant, bad signature,
    duplicate, unknown status, no matching invoice, a transition the state
    machine forbids) stays a silent ``204``: it is a final answer, and varying
    the response would enumerate tenant slugs / invoice state.

    A failure of OURS — the tenant DB unreachable, a statement timeout, a
    concurrent transition racing the guard — is not an answer. The handler
    already releases its Redis dedup claim on those paths so "the ERP's retry
    can reprocess", but a ``204`` tells the ERP the event was delivered, so no
    retry ever came and the status transition was dropped permanently.
    ``api/billing_webhook.py`` already returns 5xx here; ``api/email_intake.py``
    now does too.

    Bodyless, so the response still carries no diagnostic detail.
    """
    return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)


# Map ERP status strings to our internal status transitions
ERP_STATUS_MAP = {
    # Merge.dev statuses
    "OPEN": InvoiceStatus.posted_in_erp,
    "SUBMITTED": InvoiceStatus.posted_in_erp,
    # Business Central
    "Open": InvoiceStatus.posted_in_erp,
    "Paid": InvoiceStatus.paid,
    # NetSuite
    "open": InvoiceStatus.posted_in_erp,
    "paidInFull": InvoiceStatus.paid,
    # Generic
    "posted": InvoiceStatus.posted_in_erp,
    "paid": InvoiceStatus.paid,
    "cancelled": InvoiceStatus.failed,
    "voided": InvoiceStatus.failed,
}


@router.post("/webhook/{erp_type}", status_code=status.HTTP_204_NO_CONTENT)
async def erp_webhook(
    erp_type: str,
    request: Request,
):
    """Receive status updates from ERPs or Merge.dev.

    Authenticated by HMAC over the raw body. The signing secret is
    looked up off the tenant named in the body — same pattern as the
    card webhook. Bad signatures, unknown tenants, missing events all
    return 204 silently. Leaking the distinction would help an
    attacker enumerate tenant slugs or replay events.

    Expected body:
    {
        "tenant_slug": "acme",
        "correlation_id": "uuid" | null,
        "erp_document_id": "string" | null,
        "event_id": "string" | null,
        "status": "Open" | "posted" | "paid" | ...,
        "details": { ... }  // optional extra data
    }

    For Merge.dev webhooks, the body structure may differ — we normalize it.
    """
    # Bound the body BEFORE buffering it. A POST would otherwise be read fully
    # into memory before the signature check ever runs (memory-exhaustion DoS
    # on a public, unauthenticated route). Reject on the declared
    # Content-Length when present, and re-check the actual read in case the
    # header lied / was absent (chunked). ERP status payloads are small JSON;
    # cap defaults to a few MB.
    max_bytes = settings.erp_webhook_max_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                logger.warning("ERP webhook rejected: body exceeds size cap")
                return
        except ValueError:
            logger.warning("ERP webhook rejected: invalid content-length")
            return

    raw_body = await request.body()
    if len(raw_body) > max_bytes:
        logger.warning("ERP webhook rejected: body exceeds size cap")
        return

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return  # malformed JSON → silent 204

    # Normalize — Merge.dev sends a different shape
    if erp_type == "merge_dev" and "data" in body:
        data = body["data"]
        body = {
            "tenant_slug": body.get("linked_account_id"),
            "correlation_id": data.get("integration_params", {}).get("correlation_id"),
            "erp_document_id": data.get("id"),
            "event_id": body.get("hook", {}).get("event") or body.get("event"),
            "status": data.get("status"),
            "details": data,
        }

    tenant_slug = body.get("tenant_slug")
    correlation_id = body.get("correlation_id")
    erp_document_id = body.get("erp_document_id")
    erp_status = body.get("status", "")
    # Deliberately NOT `body.get("event_id") or erp_document_id or
    # correlation_id`. Both fallbacks are constant for an invoice's WHOLE
    # lifecycle, so a direct integration that omits a per-delivery event_id
    # would have the first status event's dedup claim on that id silently
    # swallow every later distinct status event for the same invoice for the
    # rest of the dedup TTL (e.g. `posted_in_erp` claims it, the next day's
    # legitimate `paid` webhook never fires). `is_event_already_processed`
    # already has a real "missing event id -> always process" path — let a
    # genuinely absent event_id hit that instead of a fabricated one.
    event_id = body.get("event_id")

    if not tenant_slug:
        return  # silent — body didn't name a tenant
    if not correlation_id and not erp_document_id:
        return  # silent — nothing to look up

    # Look up the org. A missing slug → silent 204 (no enumeration).
    async with control_session_factory() as ctrl_db:
        result = await ctrl_db.execute(select(Organization).where(Organization.slug == tenant_slug))
        org = result.scalar_one_or_none()
        if not org:
            return

    # Verify HMAC against the tenant's configured signing secret.
    erp_config = (org.settings or {}).get("erp") or {}
    signing_secret = erp_config.get("webhook_signing_secret", "")
    if not signing_secret:
        # Fail closed (verify_hmac_sha256 would 204 on an empty secret anyway),
        # but surface a PII-free config error so an operator learns the ERP
        # integration is unconfigured rather than silently dropping every event.
        # tenant_slug / erp_type are non-PII identifiers; the secret is never logged.
        logger.warning(
            "ERP webhook dropped: no webhook_signing_secret configured for tenant '%s' (%s)",
            tenant_slug,
            erp_type,
        )
        return  # silent 204 to the caller (no enumeration)
    provided_sig = extract_signature_header(
        dict(request.headers),
        "X-Webhook-Signature",
        "X-Hub-Signature-256",
        "X-Merge-Webhook-Signature",
    )
    if not verify_hmac_sha256(signing_secret, raw_body, provided_sig):
        return  # silent 204 on bad / missing signature

    # Dedup by event id. Cross-tenant key because event ids should be
    # unique per provider; a duplicate within the TTL window is a
    # replay regardless of tenant.
    if await is_event_already_processed(f"erp:{erp_type}", str(event_id or "")):
        return
    # Track the claim so it can be released if the transition below rolls
    # back — the Redis claim is durable the instant it's written, but the
    # side effect it guards isn't durable until the DB commit. Without this,
    # a transient failure (bad correlation_id, DB hiccup) permanently drops
    # the ERP's retry for the full dedup TTL window (mirrors cards.py /
    # billing_webhook.py / email_intake.py, which all release on failure).
    claimed_event = str(event_id) if event_id else None

    # Map ERP status to our internal status
    target_status = ERP_STATUS_MAP.get(erp_status)
    if not target_status:
        return  # unknown status — silent ack

    # Open tenant DB and find the invoice
    engine = get_tenant_engine(org.db_name)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as db:
        try:
            if not correlation_id:
                return  # erp_document_id-only path not supported yet

            # Resolve the invoice id by correlation_id first (not unique
            # enough to lock on directly), then re-fetch WITH a row lock
            # before evaluating/applying the transition — the documented
            # convention for any status transition (get_invoice_for_update()).
            # Without the lock, a concurrent human approval racing this
            # webhook can silently overwrite the other, corrupting the audit
            # trail's from/to narrative.
            id_result = await db.execute(
                select(Invoice.id).where(Invoice.correlation_id == uuid.UUID(correlation_id))
            )
            invoice_id = id_result.scalar_one_or_none()
            if not invoice_id:
                return  # no matching invoice → silent ack
            invoice = await get_invoice_for_update(db, invoice_id)

            # Only transition if the AUTHORITATIVE state machine permits it.
            # We deliberately do NOT keep a second local transition map here: a
            # divergent copy that permitted an edge VALID_TRANSITIONS forbids
            # (e.g. posted_in_erp → paid, or an ERP void/cancel → failed from
            # posted_in_erp / payment_scheduled) would make transition_invoice
            # raise a 409 that then escaped this handler — breaking the
            # documented "every webhook rejection path returns 204 silently"
            # contract. Screening against the canonical set guarantees the
            # webhook can never claim a transition the engine will reject, and a
            # transition the machine legitimately forbids becomes a silent ack
            # (same as an unknown status / unknown invoice), not a 409.
            current = invoice.status
            if target_status not in VALID_TRANSITIONS.get(current, set()):
                # The state machine forbids this transition for the invoice's
                # current state. Almost always a silent no-op — a stale or
                # duplicate ERP status for an invoice that already moved on.
                # ONE forbidden case is a real reconciliation signal we must
                # NOT drop: the ERP reports the invoice VOIDED/CANCELLED
                # (→ failed) after we already advanced it (sent_to_erp /
                # posted_in_erp / payment_scheduled / paid). Money may already
                # be in flight, so we never auto-transition (auto → failed from
                # payment_scheduled/paid would collide with the money path);
                # instead we open an Exception for a human to reconcile. Every
                # OTHER forbidden transition stays a pure silent no-op — turning
                # them all into exceptions would be noise.
                if target_status is InvoiceStatus.failed:
                    await _raise_erp_reconciliation_exception(
                        db,
                        invoice,
                        org_id=org.id,
                        erp_type=erp_type,
                        erp_status=erp_status,
                        erp_document_id=erp_document_id,
                        event_id=event_id,
                    )
                    await db.commit()
                return  # silent 204 on every forbidden-transition path

            # `payment_scheduled → paid` is a legal edge, but it is the exact
            # transition the settlement-coverage hold governs. `payment_erp_sync`
            # (the normal, one-shot path to `paid`) refuses to close out an
            # invoice whose rail settled SHORT of what AP authorized — it holds
            # at `payment_scheduled` for a human, with `/settlement/accept` and
            # `/void` as the two exits. A validly-signed `{"status":"Paid"}`
            # from the tenant's own ERP (which legitimately fires once a bill
            # payment is applied there) must not walk around that: it would
            # flip a short-paid invoice to `paid`, fire the outbound
            # `payment.settled` webhook, email the supplier, and count the full
            # amount in aging / dashboard / 1099 YTD — and `/settlement/accept`
            # would then 409, so the documented exit is gone. Run the SAME check
            # off the SAME persisted figure (`Payment.settled_amount`) and take
            # the reconciliation-exception path on a non-covering verdict.
            if target_status is InvoiceStatus.paid and current == InvoiceStatus.payment_scheduled:
                settling = (
                    await db.execute(
                        select(Payment)
                        .where(
                            Payment.invoice_id == invoice.id,
                            Payment.status == "completed",
                        )
                        .order_by(Payment.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if settling is not None:
                    coverage = settlement_coverage(
                        settled_amount=settling.settled_amount,
                        settled_currency=settling.settled_currency,
                        target_amount=settling.amount,
                        target_currency=invoice.currency,
                        source_amount=settling.source_amount,
                        source_currency=settling.source_currency,
                        settled_amount_unstorable=settling.settled_amount_unstorable,
                    )
                    if not coverage.completes_invoice:
                        await _open_erp_reconciliation_exception(
                            db,
                            invoice,
                            org_id=org.id,
                            description=(
                                f"ERP reported 'Paid' via {erp_type} for an invoice "
                                f"whose payment settled {coverage.state} of what AP "
                                f"authorized (shortfall {coverage.shortfall or '-'}) — "
                                f"held at 'payment_scheduled'. Reconcile via "
                                f"POST /api/payments/{{id}}/settlement/accept or /void "
                                f"(erp_document_id={erp_document_id or '-'}, "
                                f"event={event_id or '-'})."
                            ),
                        )
                        await db.commit()
                        return  # silent 204 — held for human reconciliation

            await transition_invoice(
                db,
                invoice,
                target_status,
                action_name=f"invoice.erp_status_{target_status.value}",
                # PII guard: never splat the raw ERP `details` payload into the
                # append-only audit row — the ERP may include vendor bank/tax/address
                # fields. Whitelist only the safe, non-PII routing identifiers.
                details={
                    "erp_type": erp_type,
                    "erp_status": erp_status,
                    "erp_document_id": erp_document_id,
                    "raw_event_id": str(event_id) if event_id else None,
                },
            )
            await db.commit()
            return

        except HTTPException:
            # Defensive backstop: the VALID_TRANSITIONS guard above already
            # screens out every edge the state machine forbids, so
            # transition_invoice's validate_transition should never 409 here.
            # Reaching it means a concurrent status change slipped between the
            # guard and the transition — a race on OUR side, not a decision
            # about the event. A 409 must never escape (it would enumerate
            # invoice state), but the event is still unapplied, so ask for a
            # redelivery: the retry either applies cleanly or lands on the
            # forbidden-transition path and silently no-ops, which is correct
            # either way. Bodyless, so nothing leaks.
            await db.rollback()
            if claimed_event is not None:
                await release_event_claim(f"erp:{erp_type}", claimed_event)
            return _retry_please()
        except Exception:
            await db.rollback()
            # The dedup claim guards a side effect that just rolled back —
            # release it so the ERP's retry can reprocess (otherwise the
            # status transition is dropped for the full TTL window).
            if claimed_event is not None:
                await release_event_claim(f"erp:{erp_type}", claimed_event)
            # Same split as email_intake: this is OUR failure, not a decision,
            # so the release above is now paired with an actual request to
            # redeliver. Bodyless — no diagnostic detail in the response.
            logger.exception("ERP webhook: processing failed for erp_type=%s", erp_type)
            return _retry_please()


async def _open_erp_reconciliation_exception(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id,
    description: str,
) -> None:
    """Open ONE ``erp_reconciliation`` Exception for human review, PII-free.

    **Idempotent.** The webhook already dedupes redeliveries by event id, but
    two DISTINCT ERP events for the same invoice must not pile up duplicate
    reconciliation exceptions — so we skip if an OPEN ``erp_reconciliation``
    exception already exists for this invoice.

    ``description`` must carry only safe routing identifiers and status —
    never the raw ERP ``details`` payload (which may include vendor bank / tax
    / address fields).
    """
    existing = await db.execute(
        select(func.count()).where(
            APException.invoice_id == invoice.id,
            APException.exception_type == ERP_RECONCILIATION_EXCEPTION_TYPE,
            APException.status == "open",
        )
    )
    if (existing.scalar() or 0) > 0:
        return  # already flagged for this invoice — don't duplicate

    await create_exception(
        db,
        exception_type=ERP_RECONCILIATION_EXCEPTION_TYPE,
        severity="error",
        description=description,
        status="open",
        organization_id=org_id,
        invoice=invoice,
        # Raised from an inbound ERP webhook — the caller is the tenant's ERP,
        # authenticated by HMAC, not a control-plane user.
        raised_by_user_id=None,
    )


async def _raise_erp_reconciliation_exception(
    db: AsyncSession,
    invoice: Invoice,
    *,
    org_id,
    erp_type: str,
    erp_status: str,
    erp_document_id: str | None,
    event_id,
) -> None:
    """Open an ``erp_reconciliation`` Exception when the ERP reports an invoice
    VOIDED/CANCELLED (``→ failed``) that we've already advanced past the point
    where ``→ failed`` is a legal transition (``sent_to_erp`` /
    ``posted_in_erp`` / ``payment_scheduled`` / ``paid``). Money may already be
    in flight, so this is a review signal — we deliberately do NOT
    auto-transition the invoice.
    """
    await _open_erp_reconciliation_exception(
        db,
        invoice,
        org_id=org_id,
        description=(
            f"ERP reported '{erp_status}' (void/cancel) via {erp_type} for an "
            f"invoice already at '{invoice.status.value}' — money may be in flight. "
            f"Needs human reconciliation "
            f"(erp_document_id={erp_document_id or '-'}, event={event_id or '-'})."
        ),
    )
