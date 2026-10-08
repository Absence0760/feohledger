"""Notification dispatch — fan an invoice lifecycle event out to recipients.

Two effects per recipient, each gated by that recipient's per-user preference:

1. **in-app** — insert a `Notification` row into the *tenant* DB (the same
   session that owns the status transition, so it commits atomically with it).
2. **email** — build an `EmailMessage` and hand it to the configured email
   adapter (`console` by default — safe, no network, no secrets). This leg, and
   the chat post below it, run **after the caller commits** (see below).

**The outbound legs run POST-COMMIT.** Everything that talks to a third party —
every email, and the single Slack/Teams post — is queued onto the caller's
session via `services/post_commit.enqueue_post_commit` and fired from
SQLAlchemy's `after_commit`. Before this, `transition_invoice` awaited the whole
fan-out *inside* the caller's still-open transaction: `payment_erp_sync` holds
`SELECT … FOR UPDATE` on the invoice and `review.approve_invoice` on the
`WorkflowInstance` until after the transition returns, so a hung chat webhook
held a row lock on a live invoice for its full 10-second timeout and N
recipients multiplied the email leg linearly. Nothing else about the contract
moved: the in-app `Notification` rows are still added to the caller's session
and still ride its commit, because those are DB writes and *should*. A
transaction that rolls back sends nothing, which is the correct semantics — we
no longer email people about a status change that never happened.

Both are **best-effort**. A failure here must never roll back or abort the
caller's status transition / audit write, so the whole dispatch is wrapped in a
guard and logs without PII (event type + notification id only, never the
recipient's email address or any invoice banking field).

**The two OUTBOUND send guards log the exception's class name, not
``logger.exception``.** `.exception()` / `exc_info=True` append the traceback —
and with it the exception's own text — no matter what the format string says,
and both transports raise errors that carry exactly what this module promises
not to log: httpx's ``HTTPStatusError`` embeds the request URL, which for a
Slack/Teams incoming webhook IS the credential, and ``SMTPRecipientsRefused``
embeds the addresses it refused. The remaining ``logger.exception`` calls here
wrap DB reads and template renders, whose exceptions carry row ids and SQL
rather than a credential or an address — there the traceback is the diagnostic
value, so it stays.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.notification import NOTIFICATION_EVENT_TYPES, Notification
from app.services.notification_templates import InvoiceContext, RenderedNotification, render
from app.services.post_commit import enqueue_post_commit
from app.utils.tenant_urls import tenant_base_url

logger = logging.getLogger(__name__)

# Default channel state when a user hasn't set an explicit preference.
_DEFAULT_CHANNELS = {"email": True, "in_app": True}


def resolve_prefs(notification_prefs: dict | None, event_type: str) -> dict:
    """Resolve the effective {email, in_app} channels for an event type.

    Missing event or missing channel key falls back to "on" — opt-out, not
    opt-in, so a brand-new user gets notifications by default.
    """
    prefs = notification_prefs or {}
    event_prefs = prefs.get(event_type) or {}
    return {
        "email": bool(event_prefs.get("email", _DEFAULT_CHANNELS["email"])),
        "in_app": bool(event_prefs.get("in_app", _DEFAULT_CHANNELS["in_app"])),
    }


async def _load_recipients(
    recipient_user_ids: list[uuid.UUID],
    organization_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, object]:
    """Load control-plane User rows for the given ids, keyed by id.

    Uses its own control-plane session (the caller's `db` is tenant-scoped),
    mirroring `approval_chain.resolve_assignee`.

    **Scoped to ``organization_id``** — `users` is control-plane, so an
    unscoped ``WHERE id IN (…)`` resolves accounts in EVERY tenant. The
    recipient loop below already documents that a wrong-org recipient must be
    skipped; that only holds if the query never returns one. Any id that
    reaches here from a caller that failed to scope its own lookup (the
    invoice-assign route did) is simply not found, so the notification — an
    email carrying this tenant's invoice number, vendor and amount — is never
    addressed outside the tenant it belongs to. ``None`` (the default) keeps the
    old unscoped behaviour for callers with no org in hand; every production
    caller goes through ``notify_event``, which always has one.
    """
    from app.database import control_session_factory
    from app.models.user import User

    unique_ids = list({uid for uid in recipient_user_ids if uid is not None})
    if not unique_ids:
        return {}

    stmt = select(User).where(User.id.in_(unique_ids))
    if organization_id is not None:
        stmt = stmt.where(User.organization_id == organization_id)

    async with control_session_factory() as ctrl_db:
        result = await ctrl_db.execute(stmt)
        users = result.scalars().all()
    return {u.id: u for u in users}


async def _resolve_org_brand(organization_id: uuid.UUID):
    """Load an org's resolved white-label brand context (control plane).

    Returns the platform-default brand on any miss so the caller can always
    brand the email (the platform brand is a sensible default). Best-effort —
    never raises.
    """
    from app.database import control_session_factory
    from app.models.organization import Organization
    from app.services.branding import get_brand_context

    try:
        async with control_session_factory() as ctrl_db:
            result = await ctrl_db.execute(
                select(Organization.settings).where(Organization.id == organization_id)
            )
            org_settings = result.scalar_one_or_none()
    except Exception:  # noqa: BLE001 — brand is cosmetic; never break dispatch.
        return get_brand_context(None)
    return get_brand_context(org_settings)


async def _resolve_org_slug(organization_id: uuid.UUID) -> str | None:
    """Look up an org's tenant slug (control plane). Used only to build the
    per-recipient email-approval links — returns None on any miss so the caller
    simply omits the links."""
    from app.database import control_session_factory
    from app.models.organization import Organization

    async with control_session_factory() as ctrl_db:
        result = await ctrl_db.execute(
            select(Organization.slug).where(Organization.id == organization_id)
        )
        return result.scalar_one_or_none()


async def _resolve_org_chat_config(
    organization_id: uuid.UUID,
) -> tuple[dict, str | None, dict]:
    """Load an org's `settings.chat_notifications` config, its slug + its settings.

    Returns ``({}, None, {})`` on any miss so the caller can degrade to "no chat
    notification" without raising. The slug and the full settings blob together
    build the (PII-free) deep link into the tenant app — the blob because the
    tenant may override its own base URL under `settings.brand`, which is what
    keeps a white-label tenant's Slack/Teams approval link on its vanity host.
    """
    from app.database import control_session_factory
    from app.models.organization import Organization

    async with control_session_factory() as ctrl_db:
        result = await ctrl_db.execute(
            select(Organization.slug, Organization.settings).where(
                Organization.id == organization_id
            )
        )
        row = result.first()
    if row is None:
        return {}, None, {}
    slug, org_settings = row
    org_settings = org_settings if isinstance(org_settings, dict) else {}
    chat_config = org_settings.get("chat_notifications") or {}
    if not isinstance(chat_config, dict):
        return {}, slug, org_settings
    return chat_config, slug, org_settings


async def resolve_role_user_ids(organization_id: uuid.UUID, role_name: str) -> list[uuid.UUID]:
    """Return the ids of active users in an org holding a given role.

    Used to fan a `paid` notification out to every AP manager. Runs against the
    control plane (where Users + Roles live); the role membership is the
    `user_roles` junction. System roles have `organization_id IS NULL`, so we
    match the role by name and scope the *users* by org instead.
    """
    from app.database import control_session_factory
    from app.models.user import Role, User, UserRole

    async with control_session_factory() as ctrl_db:
        result = await ctrl_db.execute(
            select(User.id)
            .join(UserRole, UserRole.user_id == User.id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                User.organization_id == organization_id,
                User.is_active == True,  # noqa: E712
                Role.name == role_name,
            )
        )
        return [row[0] for row in result.all()]


async def notify_event(
    db: AsyncSession,
    *,
    correlation_id: uuid.UUID,
    organization_id: uuid.UUID,
    event_type: str,
    entity_id: uuid.UUID | None,
    recipient_user_ids: list[uuid.UUID],
    invoice_ctx: InvoiceContext | None = None,
    rendered: RenderedNotification | None = None,
    actor_id: uuid.UUID | None = None,
    entity_type: str = "invoice",
    action_facts: str | None = None,
) -> int:
    """Notify each recipient of `event_type`, gated by their preferences.

    ``action_facts`` is ``email_action_token.digest_of_invoice`` of the invoice
    an ``invoice_assigned`` message announces, computed by the caller from the
    row it holds. The message's Approve / Reject actions bind it, so they act
    only on the version the message describes; without it no action is offered
    (decisions §263).

    Pass either ``invoice_ctx`` (the dispatcher renders the invoice template)
    or a pre-``rendered`` notification (for non-invoice events like contract
    renewals, which carry a different context). Exactly one is required.

    Never raises — all failures are swallowed and logged (without PII) so the
    caller's transaction is unaffected. The in-app rows are *added* to `db`
    (the caller's tenant session) but not committed here; the caller's existing
    commit flushes them alongside the status change + audit row. The email and
    chat legs are queued to run AFTER that commit (see the module docstring), so
    no third party's latency is charged to an open transaction holding row
    locks.

    **Returns the number of recipients something was actioned for** — an in-app
    row added, or an email queued. Most callers ignore it: for a status
    transition, "nobody had this event turned on" is a normal outcome. It exists
    for the callers that write a **suppress-forever marker** afterwards
    (``cash_flow_alerts``'s alerted-period marker, ``contract_renewal``'s
    ``renewal_alert_sent_at``). Both already skip the marker when they resolve
    zero recipients, but that only covers one of the ways this can silently
    reach nobody: the master switch being off, an unknown event type, a template
    render that raised, the recipient load failing, or every resolved recipient
    being inactive / opted out. Each of those returned ``None`` indistinguishably
    from success, so the caller stamped its marker and the finance leaders were
    never told about that period's projected cash shortfall — or that contract's
    renewal — for the rest of its life, with nothing counted as a failure. A
    number the caller can test is the only honest signal.
    """
    if not settings.notifications_enabled:
        return 0

    if event_type not in NOTIFICATION_EVENT_TYPES:
        logger.warning("notify_event: unknown event_type=%s — skipping", event_type)
        return 0

    if rendered is None:
        if invoice_ctx is None:
            logger.warning("notify_event: no invoice_ctx or rendered for %s — skipping", event_type)
            return
        try:
            rendered = render(event_type, invoice_ctx)
        except Exception:  # noqa: BLE001 — never let a template bug break a transition
            logger.exception("notify_event: template render failed for event_type=%s", event_type)
            return 0

    try:
        users_by_id = await _load_recipients(recipient_user_ids, organization_id)
    except Exception:  # noqa: BLE001
        logger.exception("notify_event: failed loading recipients for event_type=%s", event_type)
        return 0

    from app.models.notification import EVENT_INVOICE_ASSIGNED

    # Split point. Everything from here to the end of the recipient loop is
    # IN-TRANSACTION work (the in-app `Notification` rows, which must ride the
    # caller's commit); the outbound sends are collected and handed to
    # `post_commit` instead of awaited here.
    email_targets: list[tuple[uuid.UUID, str, str | None]] = []
    #: Recipients something was actioned for — see this function's docstring.
    notified = 0

    # De-dup recipients so a user who is both uploader and AP-manager gets one.
    for recipient_id in {uid for uid in recipient_user_ids if uid is not None}:
        user = users_by_id.get(recipient_id)
        if user is None:
            # Recipient no longer exists / wrong org — skip silently.
            continue
        if not getattr(user, "is_active", True):
            continue

        channels = resolve_prefs(getattr(user, "notification_prefs", None), event_type)

        if channels["in_app"]:
            db.add(
                Notification(
                    correlation_id=correlation_id,
                    organization_id=organization_id,
                    recipient_user_id=recipient_id,
                    event_type=event_type,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    title=rendered.title,
                    body=rendered.body_text,
                )
            )

        if channels["email"] and getattr(user, "email", None):
            email_targets.append((recipient_id, user.email, getattr(user, "locale", None)))

        if channels["in_app"] or (channels["email"] and getattr(user, "email", None)):
            notified += 1

    wants_chat = entity_type == "invoice" and invoice_ctx is not None
    if not email_targets and not wants_chat:
        # In-app rows may still have been added above — `notified` counts them.
        return notified

    final_rendered = rendered

    async def _outbound() -> None:
        """The third-party legs — run after the caller's transaction commits."""
        # Resolve the tenant brand once for every email this dispatch sends (the
        # From display name + HTML header + support footer). Best-effort — falls
        # back to the platform brand on any miss.
        brand = await _resolve_org_brand(organization_id)

        # Email approval: when an invoice is assigned for review and the feature
        # is configured, the reviewer's email gets per-recipient Approve/Reject
        # links (the token binds to *that* reviewer). Resolve the tenant slug
        # once here; the per-recipient token is built inside the loop.
        # Best-effort — any miss just omits the links.
        tenant_slug: str | None = None
        if (
            event_type == EVENT_INVOICE_ASSIGNED
            and entity_type == "invoice"
            and settings.email_action_signing_key
            and entity_id is not None
            and action_facts is not None
        ):
            try:
                tenant_slug = await _resolve_org_slug(organization_id)
            except Exception:  # noqa: BLE001
                logger.exception("notify_event: failed resolving tenant slug for action links")
                tenant_slug = None

        for recipient_id, recipient_email, recipient_locale in email_targets:
            # Localize the email copy to the recipient's account-level locale
            # preference (DB `User.locale`). The in-app row above stays in the
            # default (English) `rendered` — the locale pref drives EMAIL only,
            # never in-app UI. We can only re-render per-locale when we have the
            # PII-free invoice context; a pre-`rendered` event (e.g. contract
            # renewal) keeps its English copy. Deep links / money / numbers are
            # placeholder-interpolated, so they're identical across locales.
            email_rendered = final_rendered
            if invoice_ctx is not None and recipient_locale:
                try:
                    email_rendered = render(event_type, invoice_ctx, locale=recipient_locale)
                except Exception:  # noqa: BLE001 — never let a locale render break the send
                    email_rendered = final_rendered
            email_subject = email_rendered.title
            email_text = email_rendered.body_text
            email_html = email_rendered.body_html
            if tenant_slug is not None:
                from app.services.email_action_token import build_email_action_links

                links = build_email_action_links(
                    api_base_url=settings.api_public_url,
                    tenant_slug=tenant_slug,
                    invoice_id=entity_id,
                    actor_id=recipient_id,
                    facts=action_facts,
                    signing_key=settings.email_action_signing_key,
                    ttl_hours=settings.email_action_ttl_hours,
                )
                if links:
                    text_block, html_block = links
                    email_text = f"{email_text}\n\n{text_block}"
                    email_html = f"{email_html or ''}{html_block}"
            await _send_email_best_effort(
                recipient_email,
                email_subject,
                email_text,
                email_html,
                event_type=event_type,
                brand=brand,
            )

        # Chat fan-out (Slack / Teams) — a single channel post per event, not
        # per-recipient. Approval-lifecycle events only; entirely best-effort
        # and self-guarded so a chat-send failure never breaks anything.
        if wants_chat:
            # `entity_id` is generic on `notify_event` (it keys whatever
            # `entity_type` names); inside this branch it is provably the
            # invoice PK, so it crosses the boundary under that name.
            await _send_chat_best_effort(
                organization_id=organization_id,
                event_type=event_type,
                invoice_ctx=invoice_ctx,
                invoice_id=entity_id,
                recipient_user_ids=recipient_user_ids,
                action_facts=action_facts,
            )

    enqueue_post_commit(db, _outbound, name=f"notify-{event_type}")
    return notified


async def _send_email_best_effort(
    to: str,
    subject: str,
    body_text: str,
    body_html: str | None,
    *,
    event_type: str,
    brand=None,
) -> None:
    """Send one email, swallowing + logging (PII-free) any failure."""
    from app.services.email_adapters import EmailMessage, get_email_adapter

    try:
        adapter = get_email_adapter()
        await adapter.send(
            EmailMessage(
                to=to,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                brand=brand,
            )
        )
    except Exception as exc:  # noqa: BLE001
        # Event type + exception CLASS only, for the same reason as the chat
        # send below: `logger.exception` attaches the traceback whatever the
        # format string says, and an SMTP failure carries the addresses it
        # refused — `smtplib.SMTPRecipientsRefused` stringifies as
        # `{'someone@customer.com': (550, b'…')}`. That is exactly the recipient
        # address this call site has always promised not to log.
        logger.warning(
            "notify_event: email send failed for event_type=%s err=%s",
            event_type,
            type(exc).__name__,
        )


def _chat_event_enabled(chat_config: dict, event_type: str) -> bool:
    """Whether the org wants chat notifications for this event.

    Master gate is `chat_config["enabled"]` (default off — chat is opt-in per
    org, unlike email/in-app which default on). The per-event `events` map is
    opt-out within that: a missing event key defaults to on once enabled.
    """
    if not chat_config.get("enabled"):
        return False
    events = chat_config.get("events")
    if not isinstance(events, dict):
        return True
    return bool(events.get(event_type, True))


def _build_chat_action_tokens(
    *,
    event_type: str,
    chat_config: dict,
    slug: str | None,
    invoice_id: uuid.UUID | None,
    recipient_user_ids: list[uuid.UUID] | None,
    facts: str | None,
) -> tuple[str | None, str | None]:
    """Build the (approve, reject) action tokens for the org's chat provider.

    Returns ``(None, None)`` — so the message stays non-interactive — unless ALL
    of: the event is ``invoice_assigned``, the chat provider has an interactive
    approval surface (Slack or Teams), the action signing key is set, the tenant
    slug + invoice id resolve, and there is exactly one intended approver to bind
    the token to. The single-approver guard matches how
    ``review.assign_reviewer`` fires the event (one reviewer per assignment);
    binding to one specific approver keeps the same no-privilege-escalation
    property as the per-recipient email link.

    The token is minted on the **provider's own channel** (``slack`` / ``teams``),
    so it is only redeemable at that provider's interactivity endpoint — a Slack
    token can never be replayed against the Teams route, or vice versa. A provider
    with no interactive surface (``mock``, or an unknown key) mints nothing.
    """
    from app.models.notification import EVENT_INVOICE_ASSIGNED
    from app.services.email_action_token import (
        build_slack_action_tokens,
        build_teams_action_tokens,
    )

    builders = {"slack": build_slack_action_tokens, "teams": build_teams_action_tokens}

    if event_type != EVENT_INVOICE_ASSIGNED:
        return None, None
    provider = chat_config.get("provider") or settings.chat_notification_provider
    builder = builders.get(provider)
    if builder is None:
        return None, None
    if not settings.email_action_signing_key or slug is None or invoice_id is None or not facts:
        return None, None

    approvers = [uid for uid in (recipient_user_ids or []) if uid is not None]
    if len(approvers) != 1:
        # Zero or many intended approvers — a single channel post can't bind a
        # token to a specific reviewer, so omit the buttons (link still works).
        return None, None

    tokens = builder(
        tenant_slug=slug,
        invoice_id=invoice_id,
        actor_id=approvers[0],
        facts=facts,
        signing_key=settings.email_action_signing_key,
        ttl_hours=settings.email_action_ttl_hours,
    )
    if tokens is None:
        return None, None
    return tokens


async def _send_chat_best_effort(
    *,
    organization_id: uuid.UUID,
    event_type: str,
    invoice_ctx,
    invoice_id: uuid.UUID | None,
    recipient_user_ids: list[uuid.UUID] | None = None,
    action_facts: str | None = None,
) -> None:
    """Post one approval event to the org's chat channel (Slack/Teams).

    ``invoice_id`` is the invoice PK, deliberately NOT named ``entity_id``:
    everywhere else in this codebase ``entity_id`` is the multi-entity
    subsidiary FK, so the old name read as a tenant-scoping bug on every review
    of this file. The caller narrows its generic ``entity_id`` to an invoice id
    before calling (the chat fan-out only fires for ``entity_type=="invoice"``).

    Best-effort + fully self-guarded: any failure (config load, adapter build,
    transport) is swallowed and logged PII-free so the caller's transaction is
    never affected. No-ops when the org hasn't enabled chat, the event isn't a
    chat-notifiable approval event, or the provider can't be resolved.

    For the "assigned for review" event, when the org's chat provider has an
    interactive approval surface (Slack or Teams) and the action-signing key is
    configured, the message gets Approve/Reject actions. Each carries a signed,
    single-use action token bound to the intended approver (the assigned
    reviewer) on that provider's own channel — the same primitive the
    email-approval link uses. The adapter has the final say on rendering: Teams
    also needs its interactivity secret, without which it emits a read-only card.
    """
    from app.services.chat_notification_adapters import (
        get_chat_notification_adapter,
        render_chat_message,
    )

    try:
        chat_config, slug, org_settings = await _resolve_org_chat_config(organization_id)
    except Exception:  # noqa: BLE001
        logger.exception("notify_event: failed loading chat config for event_type=%s", event_type)
        return

    if not _chat_event_enabled(chat_config, event_type):
        return

    # Deep link into the tenant app (no secrets / PII). Best-effort — omitted
    # when the slug or entity is missing.
    link: str | None = None
    if slug and invoice_id is not None:
        base = tenant_base_url(slug, org_settings)
        link = f"{base}/invoices/{invoice_id}" if base else None

    approve_token, reject_token = _build_chat_action_tokens(
        event_type=event_type,
        chat_config=chat_config,
        slug=slug,
        invoice_id=invoice_id,
        recipient_user_ids=recipient_user_ids,
        facts=action_facts,
    )

    message = render_chat_message(
        event_type,
        invoice_number=getattr(invoice_ctx, "invoice_number", "") or "",
        vendor_name=getattr(invoice_ctx, "vendor_name", "") or "",
        amount=getattr(invoice_ctx, "amount", None),
        currency=getattr(invoice_ctx, "currency", None) or "USD",
        link=link,
        approve_token=approve_token,
        reject_token=reject_token,
    )
    if message is None:
        # Not a chat-notifiable event (e.g. chat_message / contract_renewal).
        return

    try:
        adapter = get_chat_notification_adapter(chat_config)
        await adapter.send(message)
    except Exception as exc:  # noqa: BLE001
        # Event type + exception CLASS only — deliberately not `logger.exception`
        # / `exc_info`, which append the traceback (and the exception's own text)
        # regardless of the format string. The adapters end in
        # `response.raise_for_status()`, and httpx's `HTTPStatusError` message
        # embeds the request URL verbatim — which here IS the credential:
        # "Client error '404 Not Found' for url 'https://hooks.slack.com/services/…/<token>'".
        # So the first 4xx from a dead or rotated webhook used to write the org's
        # chat credential into the application log. Same shape as
        # `webhooks/delivery.process_delivery`, which already logs
        # `err=type(exc).__name__`. The provider key is left out too: it is
        # admin-supplied free text on a legacy row, so it is not ours to log.
        logger.warning(
            "notify_event: chat send failed for event_type=%s err=%s",
            event_type,
            type(exc).__name__,
        )
