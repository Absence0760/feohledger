# security-patterns.sh opt-outs (noqa: webhook-no-hmac) (noqa: webhook-no-dedup) — see docstring
"""Chat-notification settings + webhook-credential rotation.

The two `webhook-*` opt-outs on line 1 are deliberate and scoped to this module.
The `security-patterns.sh` rules that fire on a route path containing "webhook"
exist for **inbound** receivers, which are public-by-design and therefore need
an HMAC check and an event-id dedupe. Nothing here receives anything: these are
JWT + `require_roles(ROLE_ADMIN)` management endpoints for an **outbound**
destination we POST to. There is no provider signature to verify and no event
to dedupe. (`api/webhooks.py`, the outbound-subscription CRUD, is the same
shape and only escapes the rule because its paths don't spell the word.)

Mounted at `/api/organization/chat-notifications`, mirroring how
`api/email_intake.py` hangs its admin surface off `/api/organization/...`.

What lives here
---------------
`Organization.settings.chat_notifications` holds the org's Slack / Teams
fan-out config **and** `webhook_url`, which is the credential for both real
providers — a bearer capability that lets whoever holds it post arbitrary
content into the customer's approval channel. Before this module the URL was
settable only by overwriting the settings JSON through the generic
`PATCH /api/organization`, which meant recovering from a leak was an untracked
hand-edit that nothing audited.

The shape rules (write-only, preserved across a config save, never logged
beyond its hostname) live in the pure `services/chat_notifications_config`;
this module does persistence, RBAC and the audit write. See
`backend/docs/notifications.md` § Rotating the webhook URL and
`docs/secrets-rotation.md`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api.deps import ROLE_ADMIN, require_roles
from app.database import get_control_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.organization import (
    ChatNotificationStatus,
    SetChatWebhookRequest,
    UpdateChatNotificationsRequest,
)
from app.services.audit_dispatch import dispatch_auth_audit
from app.services.chat_notifications_config import (
    WEBHOOK_URL_KEY,
    ChatConfigError,
    apply_config,
    apply_webhook_url,
    coerce_chat_config,
    is_webhook_configured,
    normalize_events,
    normalize_provider,
    safe_status,
    webhook_host,
)
from app.tenant import get_tenant, lock_organization

# No module logger on purpose: every value this router handles is either the
# webhook credential or derived from it, and the safe derivation (its hostname)
# already goes to the audit trail. A logger here would be an invitation to log
# the one thing that must not be logged — see `notification_dispatch`, where a
# `logger.exception` on the send path put exactly this URL into the log.

router = APIRouter(prefix="/organization/chat-notifications", tags=["chat-notifications"])

# Defensive cap so a giant string can't bloat the settings JSONB. Matches the
# outbound-webhook `target_url` bound in `api/webhooks.py`.
MAX_WEBHOOK_URL = 2048

# ONE generic refusal for every bad-URL path (wrong scheme, over length, no
# host, non-public address). It never names the value: an HTTP error body is
# routinely captured by proxies and APM, and this value is a credential.
REJECT_DETAIL = (
    "webhook_url must be a publicly routable http(s) URL "
    "(the incoming-webhook address your chat provider issued)."
)


def _chat_registry() -> tuple[list[str], tuple[str, ...]]:
    """The live adapter registry + the chat-notifiable event vocabulary.

    Imported lazily so importing this router doesn't drag the adapter package
    (and its httpx clients) in at module-import time.
    """
    from app.services.chat_notification_adapters import (
        CHAT_EVENT_TYPES,
        list_available_providers,
    )

    return list_available_providers(), CHAT_EVENT_TYPES


def _resolve_chat_config(org: Organization) -> dict:
    """Read `settings.chat_notifications`, tolerating a missing/malformed block."""
    return coerce_chat_config((org.settings or {}).get("chat_notifications"))


def _status(config: dict) -> ChatNotificationStatus:
    """Build the credential-free response.

    Goes through `safe_status`, which is the only projection of the block
    allowed to leave the backend — do not assemble this from the raw dict.
    """
    providers, events = _chat_registry()
    return ChatNotificationStatus(
        **safe_status(config),
        supported_providers=providers,
        supported_events=list(events),
    )


def _persist(org: Organization, config: dict) -> None:
    """Write the block back onto `org.settings` and mark the JSONB dirty."""
    existing = dict(org.settings or {})
    existing["chat_notifications"] = config
    org.settings = existing
    # Mutating a nested dict in-place doesn't mark JSONB dirty on its own.
    flag_modified(org, "settings")


async def _assert_public_webhook(url: str) -> None:
    """SSRF gate on an admin-supplied chat webhook URL.

    Runs the **same** `is_public_url` rule the Slack / Teams adapters apply at
    send time, so "saved" implies "the sender will not silently skip it" — a
    write-path guard with different rules from the send-path guard would let an
    admin store a URL that quietly never posts. The DNS lookup goes through the
    awaitable form of the guard, so a slow resolver suspends this request
    instead of stalling the worker.
    """
    from app.utils.url_safety import UnsafeUrlError, assert_public_url_async

    try:
        await assert_public_url_async(url)
    except UnsafeUrlError:
        raise HTTPException(status_code=422, detail=REJECT_DETAIL) from None


async def _audit_webhook_change(
    *,
    org: Organization,
    actor_id: uuid.UUID,
    config: dict,
    previous_configured: bool,
    previous_host: str | None,
    removed: bool,
) -> None:
    """Write the PII-free `organization.chat_webhook_rotated` row.

    **The URL never enters the trail.** The audit log is shipped to CloudWatch
    and an S3 Object Lock WORM bucket, so a credential written here would be
    both replicated and undeletable. What IS recorded is the pair of bare
    hostnames — not a credential (Slack's is the constant `hooks.slack.com`;
    the token lives in the path) and the thing that makes the row worth having,
    because it answers "when did our approval channel start posting somewhere
    else?", which is the question an incident actually asks. Do not "improve"
    this by adding the URL.
    """
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=actor_id,
        action="organization.chat_webhook_rotated",
        entity_id=org.id,
        details={
            "provider": config.get("provider"),
            "removed": removed,
            "previous_configured": previous_configured,
            "previous_host": previous_host,
            "new_host": webhook_host(config.get(WEBHOOK_URL_KEY)),
        },
    )


@router.get("", response_model=ChatNotificationStatus)
async def get_chat_notifications(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
):
    """Read this tenant's outbound chat-notification config. Admin only.

    Admin-only on the READ too, unlike branding / data-residency: nothing in
    the app renders from this, and the response carries the webhook's hostname,
    which is closer to the credential than anything a clerk needs.

    The webhook URL itself is never returned — by this endpoint or any other.
    `GET /api/organization` serves the settings JSONB and used to hand it back
    in full to every authenticated role; `services/org_settings_view` now drops
    it there for **every** role, admin included, so this module's write-only
    property holds system-wide rather than just locally.
    """
    return _status(_resolve_chat_config(org))


@router.put("", response_model=ChatNotificationStatus)
async def update_chat_notifications(
    body: UpdateChatNotificationsRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Update the non-credential chat settings. Admin only; audited.

    The provider is validated against the live adapter registry and every event
    key against the chat-notifiable vocabulary — an unknown value is refused
    (422) rather than persisted as a toggle that reads as configured in the UI
    and silently does nothing.

    **The webhook URL is preserved**, not replaced: it is managed by the
    endpoints below, on a different cadence, and a whole-block replace here
    would silently drop the credential — precisely the bug the branding
    endpoint once hit with `custom_domains`.
    """
    providers, event_types = _chat_registry()
    try:
        provider = normalize_provider(body.provider, supported=providers)
        events = normalize_events(body.events, supported=event_types)
    except ChatConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    # Serialise with every other settings writer (`lock_organization`).
    org = await lock_organization(db, org)
    before = _resolve_chat_config(org)
    updated = apply_config(before, enabled=body.enabled, provider=provider, events=events)
    _persist(org, updated)
    await db.commit()

    # PII-free AND credential-free: booleans + the provider key only.
    await dispatch_auth_audit(
        organization_id=org.id,
        actor_id=user.id,
        action="organization.chat_notifications_updated",
        entity_id=org.id,
        details={
            "enabled": {"old": bool(before.get("enabled")), "new": bool(body.enabled)},
            "provider": {"old": before.get("provider"), "new": provider},
            "events": events,
        },
    )
    return _status(updated)


@router.put("/webhook", response_model=ChatNotificationStatus)
async def rotate_chat_webhook(
    body: SetChatWebhookRequest,
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Set or replace the chat incoming-webhook URL. Admin only; audited.

    This is the remedy for a **leaked** webhook URL. The URL is the credential
    for both real providers: anyone holding it can post arbitrary content into
    the customer's approval channel indefinitely, with no authentication.

    **There is deliberately no overlap window**, unlike
    `POST /api/webhooks/{id}/rotate-secret`. That rotates an HMAC *signing
    secret* — a verifier held by a counterparty, so an overlap window lets the
    receiver switch keys without dropping deliveries. This is a *destination*:
    we POST to exactly one URL and no counterparty holds the old value, so
    there is nothing to overlap. Keeping the retiring URL live would mean
    posting every approval event to the compromised channel as well — the
    overlap would *extend* the leak rather than smooth a cutover. The
    replacement is atomic.

    Nor is anything "returned exactly once": we do not mint this value. The
    customer creates the incoming webhook at Slack / Teams and pastes it here,
    so they already hold it. The response is the same credential-free status
    every other read returns.

    Revoking the old URL at the provider is the customer's step and we cannot
    do it for them — see `docs/secrets-rotation.md` § Chat-notification webhook.
    """
    url = (body.webhook_url or "").strip()
    # Every refusal below answers with the same value-free 422 (see REJECT_DETAIL).
    if not url or len(url) > MAX_WEBHOOK_URL:
        # Removal has its own verb (DELETE), so an empty string can't quietly
        # disable the channel because a form field failed to populate.
        raise HTTPException(status_code=422, detail=REJECT_DETAIL)
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=422, detail=REJECT_DETAIL)
    await _assert_public_webhook(url)

    # Serialise with every other settings writer (`lock_organization`).
    org = await lock_organization(db, org)
    before = _resolve_chat_config(org)
    previous_configured = is_webhook_configured(before)
    previous_host = webhook_host(before.get(WEBHOOK_URL_KEY))

    updated = apply_webhook_url(before, url)
    _persist(org, updated)
    await db.commit()

    await _audit_webhook_change(
        org=org,
        actor_id=user.id,
        config=updated,
        previous_configured=previous_configured,
        previous_host=previous_host,
        removed=False,
    )
    return _status(updated)


@router.delete("/webhook", response_model=ChatNotificationStatus)
async def revoke_chat_webhook(
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN)),
    db: AsyncSession = Depends(get_control_db),
):
    """Remove the chat incoming-webhook URL. Admin only; audited; idempotent.

    The fastest containment for a leak when the admin does not yet have a
    replacement to paste: the adapters already fail closed with no URL (a no-op
    plus a PII-free warning), so removing it stops the fan-out immediately
    without disturbing the rest of the org's config.

    It shares the `organization.chat_webhook_rotated` action with the
    set/replace path, flagged `removed: true` — a removal is the most
    aggressive rotation, and an auditor reconstructing an incident should be
    able to follow the credential's whole lifecycle with one grep.
    """
    # Serialise with every other settings writer (`lock_organization`).
    org = await lock_organization(db, org)
    before = _resolve_chat_config(org)
    previous_configured = is_webhook_configured(before)
    previous_host = webhook_host(before.get(WEBHOOK_URL_KEY))

    updated = apply_webhook_url(before, None)
    _persist(org, updated)
    await db.commit()

    await _audit_webhook_change(
        org=org,
        actor_id=user.id,
        config=updated,
        previous_configured=previous_configured,
        previous_host=previous_host,
        removed=True,
    )
    return _status(updated)
