"""ERP webhook endpoints — status callbacks from ERPs, Merge.dev, QuickBooks and Xero."""

import json
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.invoice import Invoice, InvoiceStatus
from app.models.organization import Organization
from app.models.payment import Payment
from app.services.credential_crypto import CredentialCryptoError
from app.services.erp_adapters.dispatcher import MERGE_DEV_ADAPTER_KEY, resolve_adapter_key
from app.services.erp_status_sync import (
    DOUBLE_PAY_REFUSALS,
    ERP_PAID_RECORDABLE,
    ERP_RECONCILIATION_EXCEPTION_TYPE,
    ERP_REPORT_SETTLED_SHORT,
    ERP_REPORT_VOIDED_AFTER_POST,
    erp_finding,
    reported_paid_on,
    reported_payment_method,
    withdraw_erp_paid_reports,
)
from app.services.erp_status_sync import (
    open_erp_reconciliation_exception as _open_erp_reconciliation_exception,
)
from app.services.erp_status_sync import (
    record_erp_reported_payment as _record_erp_reported_payment,
)
from app.services.payment_settlement import settlement_coverage
from app.services.provider_credentials import provider_config
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

# `ERP_RECONCILIATION_EXCEPTION_TYPE`, `ERP_PAID_RECORDABLE`,
# `DOUBLE_PAY_REFUSALS` and the two helpers aliased above live in
# `services/erp_status_sync`, which QuickBooks' status sync-back shares; they
# are re-exported here, under their old names, for this module and its tests.
__all__ = [
    "DOUBLE_PAY_REFUSALS",
    "ERP_PAID_RECORDABLE",
    "ERP_RECONCILIATION_EXCEPTION_TYPE",
    "erp_webhook",
    "merge_webhook",
    "quickbooks_webhook",
    "xero_webhook",
]


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


# ---------------------------------------------------------------------------
# QuickBooks Online — CloudEvents webhooks (decisions §295)
# ---------------------------------------------------------------------------


@router.post("/webhook/quickbooks", status_code=status.HTTP_204_NO_CONTENT)
async def quickbooks_webhook(request: Request):
    """Intuit's CloudEvents webhook for every connected QuickBooks company.

    Declared before ``/webhook/{erp_type}`` so it is matched first. Intuit
    sends neither a tenant nor a per-tenant secret, so this route differs from
    the generic one in exactly those two places:

    * the delivery is signed with the Intuit app's ONE verifier token
      (``FEOH_ERP_QBO_WEBHOOK_VERIFIER_TOKEN``): ``intuit-signature`` is the
      base64 HMAC-SHA256 of the RAW body, checked before anything is parsed.
      An empty token means webhooks are off, and every delivery is refused;
    * each event names its company (``intuitaccountid``), which the
      control-plane realm index maps to the one tenant connected to it.

    Every path answers a bodyless 204 — a bad signature, a malformed body, an
    unknown company, a duplicate — so nothing enumerates tenants. A verified
    delivery's events are claimed (dedupe by event id) and applied after the
    response (``erp_status_sync.dispatch_qbo_events``): Intuit wants an answer
    within seconds, an event costs reads of the company, and the
    change-data-capture sweep is the backstop for anything lost.
    """
    from app.services.erp_status_sync import (
        claim_qbo_events,
        dispatch_qbo_events,
        parse_qbo_events,
    )
    from app.services.webhook_security import verify_hmac_sha256_base64

    token = settings.erp_qbo_webhook_verifier_token
    if not token:
        logger.debug("QuickBooks webhook refused: no verifier token configured")
        return
    max_bytes = settings.erp_webhook_max_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                return
        except ValueError:
            return
    raw_body = await request.body()
    if len(raw_body) > max_bytes:
        return
    signature = extract_signature_header(dict(request.headers), "intuit-signature")
    if not verify_hmac_sha256_base64(token, raw_body, signature):
        return
    try:
        body = json.loads(raw_body)
    except ValueError:
        return
    # Dedupe by event id before answering: a replay is dropped here, and a
    # dedup store we cannot reach is our failure, answered 503 so Intuit
    # redelivers rather than 2xx for work nobody claimed.
    try:
        claimed = await claim_qbo_events(parse_qbo_events(body))
    except Exception as exc:  # noqa: BLE001 — Redis unreachable
        logger.warning("QuickBooks webhook: dedup unavailable (%s)", exc.__class__.__name__)
        return _retry_please()
    dispatch_qbo_events(claimed)


# ---------------------------------------------------------------------------
# Xero — invoice webhooks (decisions §392)
# ---------------------------------------------------------------------------


def _xero_unverified() -> Response:
    """Bodyless ``401``: the delivery could not be verified.

    Xero's protocol, not a choice: its intent-to-receive check sends one
    correctly and one incorrectly signed delivery, and enables the webhook
    only when the first answers 200 and the second 401 (anything else fails
    it), and it keeps sending such checks. The signature is the platform
    app's ONE key, so a 401 says only "not signed by Xero" and names no
    tenant: nothing enumerates. Every unverifiable delivery answers it (no
    key configured, a body too large to read, a bad or missing signature)."""
    return Response(status_code=status.HTTP_401_UNAUTHORIZED)


def _xero_ack() -> Response:
    """Bodyless ``200`` for every verified delivery, whatever it decided.

    Xero wants exactly 200 with no body for a delivery it signed (a 204 is a
    failed delivery to it), so a malformed body, an unknown organisation, a
    duplicate and an applied event all answer the same."""
    return Response(status_code=status.HTTP_200_OK)


@router.post("/webhook/xero", status_code=status.HTTP_200_OK)
async def xero_webhook(request: Request) -> Response:
    """Xero's invoice webhook for every connected organisation.

    Declared before ``/webhook/{erp_type}`` so it is matched first. Like the
    QuickBooks route, it carries no tenant and no per-tenant secret:

    * the delivery is signed with the platform app's ONE webhook key
      (``FEOH_ERP_XERO_WEBHOOK_KEY``): ``x-xero-signature`` is the base64
      HMAC-SHA256 of the RAW body, checked before anything is parsed. An
      empty key means webhooks are off, and every delivery is refused;
    * each event names its organisation (``tenantId``), which the
      control-plane connection index maps to the one tenant connected to it.

    The answers are Xero's: 401 when the delivery cannot be verified, which
    is what its intent-to-receive check requires, and a bodyless 200 for
    every verified one, whatever it decided (a malformed body, an unknown
    organisation, a duplicate), so nothing enumerates. A verified delivery's
    INVOICE events are claimed (deduped) before the answer and applied after
    it (``erp_xero_webhook.dispatch_xero_events``), each through the sweep's
    per-bill path; our own failure (the dedup store down) is a bodyless 503
    so Xero redelivers.
    """
    from app.services import erp_xero_webhook as xero_events
    from app.services.webhook_security import verify_hmac_sha256_base64

    key = settings.erp_xero_webhook_key
    if not key:
        logger.debug("Xero webhook refused: no webhook key configured")
        return _xero_unverified()
    max_bytes = settings.erp_webhook_max_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                return _xero_unverified()
        except ValueError:
            return _xero_unverified()
    raw_body = await request.body()
    if len(raw_body) > max_bytes:
        return _xero_unverified()
    signature = extract_signature_header(dict(request.headers), "x-xero-signature")
    if not verify_hmac_sha256_base64(key, raw_body, signature):
        return _xero_unverified()
    try:
        body = json.loads(raw_body)
    except ValueError:
        return _xero_ack()
    try:
        claimed = await xero_events.claim_xero_events(xero_events.parse_xero_events(body))
    except Exception as exc:  # noqa: BLE001 — Redis unreachable
        logger.warning("Xero webhook: dedup unavailable (%s)", exc.__class__.__name__)
        return _retry_please()
    xero_events.dispatch_xero_events(claimed)
    return _xero_ack()


# ---------------------------------------------------------------------------
# Merge.dev — per-tenant Invoice webhooks (decisions §399)
# ---------------------------------------------------------------------------


@router.post("/webhook/merge_dev/{tenant_slug}", status_code=status.HTTP_204_NO_CONTENT)
async def merge_webhook(tenant_slug: str, request: Request):
    """Merge.dev's ``Invoice`` webhooks for ONE tenant.

    Declared before ``/webhook/{erp_type}``. Merge's delivery differs from the
    generic shape in each place that decides whose books it may touch:

    * it names no tenant of ours, only Merge's own ``linked_account``, and each
      tenant connects its OWN Merge organisation (its own API key and webhook
      signature key). So the tenant is the URL it registered in its Merge
      dashboard, and the delivery must verify under THAT tenant's key: a
      delivery signed for one tenant posted to another's URL fails;
    * ``X-Merge-Webhook-Signature`` is the HMAC-SHA256 of the raw body,
      **base64url**-encoded, under the tenant's Merge signature key (stored as
      the ERP block's ``webhook_signing_secret``), checked before the body is
      parsed;
    * ``hook.id`` is the subscription, the same on every delivery, so the
      dedupe key is the tenant, the bill and its ``modified_at``
      (``erp_merge_webhook.event_key``).

    Only the bill's id is taken from the body: the bill is read back from Merge
    and applied by the status sync's reconciler
    (``erp_merge_webhook.apply_merge_event``). Every decision — unknown tenant,
    a tenant not on Merge, no key, a bad signature, a malformed body, another
    event, a duplicate, a bill no invoice was pushed as — is a silent 204.
    Our own failure (the credential store, the dedup store, the bill read or
    the tenant DB) is a bodyless 503 with the claim released, so Merge's
    redelivery is applied. The credential-store 503 comes before the
    signature can be checked (the key is what it could not open), as on the
    generic route; it says only that a slug's credentials are unreadable.

    Not gated on ``FEOH_ERP_STATUS_SYNC_ENABLED``, unlike the QuickBooks and
    Xero webhooks (§392): that switch decides whether a push leaves the
    invoice payable, which never applies to Merge (its pushes close at
    ``done``); a tenant opts in by storing its key (decisions §399).
    """
    from app.services import erp_merge_webhook as merge_events
    from app.services.webhook_security import verify_hmac_sha256_base64url

    max_bytes = settings.erp_webhook_max_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                return
        except ValueError:
            return
    raw_body = await request.body()
    if len(raw_body) > max_bytes:
        return

    async with control_session_factory() as ctrl_db:
        org = (
            await ctrl_db.execute(select(Organization).where(Organization.slug == tenant_slug))
        ).scalar_one_or_none()
        if org is None:
            return
        try:
            erp_config = await provider_config(org, "erp", db=ctrl_db) or {}
        except CredentialCryptoError:
            logger.error("Merge webhook: the tenant's ERP credentials could not be opened")
            return _retry_please()

    if not erp_config or resolve_adapter_key(erp_config) != merge_events.MERGE_PROVIDER:
        return  # this tenant is not connected through Merge
    signing_secret = erp_config.get("webhook_signing_secret") or ""
    if not signing_secret:
        logger.warning(
            "Merge webhook dropped: no webhook_signing_secret configured for tenant '%s'",
            tenant_slug,
        )
        return
    signature = extract_signature_header(dict(request.headers), "X-Merge-Webhook-Signature")
    if not verify_hmac_sha256_base64url(signing_secret, raw_body, signature):
        return

    try:
        body = json.loads(raw_body)
    except ValueError:
        return
    event = merge_events.parse_merge_delivery(body, org.id)
    if event is None:
        return
    try:
        if await is_event_already_processed(merge_events.DEDUP_PROVIDER, event.event_key):
            return
    except Exception as exc:  # noqa: BLE001 — Redis unreachable
        logger.warning("Merge webhook: dedup unavailable (%s)", exc.__class__.__name__)
        return _retry_please()
    try:
        await merge_events.apply_merge_event(org, erp_config, event)
    except Exception as exc:  # noqa: BLE001 — our failure, never a decision
        await release_event_claim(merge_events.DEDUP_PROVIDER, event.event_key)
        logger.warning("Merge webhook: applying the bill failed (%s)", exc.__class__.__name__)
        return _retry_please()


@router.post("/webhook/{erp_type}", status_code=status.HTTP_204_NO_CONTENT)
async def erp_webhook(
    erp_type: str,
    request: Request,
):
    """Receive status updates from a tenant's directly connected ERP.

    Authenticated by HMAC over the raw body. The signing secret is
    looked up off the tenant named in the body — same pattern as the
    card webhook — and only for the ERP that tenant is connected to:
    ``erp_type`` must be the adapter its config resolves to, and never
    ``merge_dev`` (decisions §399). Bad signatures, unknown tenants, missing events all
    return 204 silently. Leaking the distinction would help an
    attacker enumerate tenant slugs or replay events.

    Expected body:
    {
        "tenant_slug": "acme",
        "correlation_id": "uuid" | null,
        "erp_document_id": "string" | null,
        "event_id": "string" | null,
        "status": "Open" | "posted" | "paid" | ...,
        "paid_on": "YYYY-MM-DD" | null,      // optional, for a paid report
        "payment_method": "check" | ... | null,  // optional, for a paid report
        "details": { ... }  // optional extra data
    }

    ``paid_on`` / ``payment_method`` date and classify a payment the ERP
    made (decisions §295): validated like the user's own record (a real
    date not in the future; a recordable rail, never ``virtual_card``), and
    an invalid or absent value falls back to the arrival date / NULL rather
    than refusing the report.

    Merge.dev's own webhooks arrive on :func:`merge_webhook` instead.
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

    if not isinstance(body, dict):
        return  # not the generic shape → silent 204
    # This route takes the tenant from the BODY, so the only thing standing
    # between a body and a tenant's books is the HMAC key that tenant's OWN
    # ERP integration signs with. It therefore answers only for the ERP the
    # tenant is actually connected to (`erp_type` must be the adapter its
    # config resolves to, checked below) and never for Merge.dev: a Merge
    # tenant's `webhook_signing_secret` is Merge's signature key, held by a
    # third party, and Merge's deliveries go to the per-tenant
    # `/webhook/merge_dev/{tenant_slug}` (`merge_webhook`, decisions §399),
    # which reads the bill back before believing it.
    if erp_type == MERGE_DEV_ADAPTER_KEY:
        return

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
    paid_on = reported_paid_on(body.get("paid_on"))
    payment_method = reported_payment_method(body.get("payment_method"))

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
        # The signing secret is sealed (`services/provider_credentials`). Not
        # being able to open it is OUR failure, not a decision about this
        # event, so the ERP is asked to retry rather than have it dropped.
        try:
            erp_config = await provider_config(org, "erp", db=ctrl_db) or {}
        except CredentialCryptoError:
            logger.error("ERP webhook: the tenant's ERP credentials could not be opened")
            return _retry_please()

    # Only the tenant's own adapter may report here (above). An empty block,
    # or one with no `integration_method`, resolves to Merge.dev and so is
    # refused too. A switch to another ERP drops every sealed secret, the
    # webhook key included (`erp_adapters/catalog.secrets_to_drop`), so a key
    # stored for one ERP never verifies another's route.
    if resolve_adapter_key(erp_config) != erp_type:
        return

    # Verify HMAC against the tenant's configured signing secret.
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
            # Any status but `Paid` for an invoice still payable here means the
            # ERP no longer shows the bill paid (a voided cheque reads `Open`):
            # a payment it reported earlier, which FeohLedger could not record,
            # must never be recorded now (decisions §319). Before the
            # transition screen, because `posted_in_erp → posted_in_erp` is a
            # silent no-op below.
            withdrawn = 0
            if target_status is not InvoiceStatus.paid and current in ERP_PAID_RECORDABLE:
                withdrawn = await withdraw_erp_paid_reports(
                    db, invoice, erp_type=erp_type, erp_state=erp_status
                )
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
                elif target_status is InvoiceStatus.paid and current in ERP_PAID_RECORDABLE:
                    # The ERP paid an invoice FeohLedger never paid — the
                    # ERP-led half of the no-rail model (issue #517). The state
                    # machine has no `posted_in_erp → paid` edge because `paid`
                    # needs a payment behind it; recording the ERP's payment
                    # supplies one, through the same service a user's "paid
                    # outside FeohLedger" goes through. Before this the report
                    # was dropped here, and a tenant with no rail had no
                    # automatic path to `paid` at all.
                    await _record_erp_reported_payment(
                        db,
                        invoice,
                        org=org,
                        erp_type=erp_type,
                        erp_document_id=erp_document_id,
                        event_id=event_id,
                        paid_on=paid_on,
                        method=payment_method,
                    )
                    await db.commit()
                elif withdrawn:
                    await db.commit()  # the withdrawal is the whole effect
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
                            finding=erp_finding("erp_settled_short", erp_type),
                            erp_report={
                                "kind": ERP_REPORT_SETTLED_SHORT,
                                "erp_type": erp_type,
                                "erp_document_id": erp_document_id,
                                "event_id": str(event_id) if event_id else None,
                                "payment_id": str(settling.id),
                                "coverage_state": coverage.state,
                                "shortfall": (
                                    str(coverage.shortfall)
                                    if coverage.shortfall is not None
                                    else None
                                ),
                            },
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
    auto-transition the invoice. (A payment the ERP reported earlier and
    FeohLedger could not record has already been withdrawn by the handler,
    which does so for every non-`Paid` status — decisions §319.)
    """
    await _open_erp_reconciliation_exception(
        db,
        invoice,
        org_id=org_id,
        finding=erp_finding("erp_voided_after_post", erp_type),
        erp_report={
            "kind": ERP_REPORT_VOIDED_AFTER_POST,
            "erp_type": erp_type,
            "erp_status": erp_status,
            "erp_document_id": erp_document_id,
            "event_id": str(event_id) if event_id else None,
            "invoice_status": invoice.status.value,
        },
    )
