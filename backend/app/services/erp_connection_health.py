"""OAuth ERP connection health: keep it alive, and say so before it dies (§295).

Phase 5 of the QuickBooks Online adapter, for every consent-based ERP
(``services/erp_oauth``). A refresh token is the only thing that lets
FeohLedger reach the customer's books, and it expires: Intuit reports the
expiry with every token response (``x_refresh_token_expires_in``, about 100
days, rolled forward on each refresh up to a hard lifetime cap). The refresher
records what the provider actually returned on every rotation
(``settings.erp.oauth.refresh_token_expires_at``), so this sweep reads the
CURRENT expiry, never the original issue date.

Every tick, for each org with an OAuth connection:

1. **Lost already** (``needs_reconnect``, set by the refresher when the
   provider refused the refresh token; ``reconnect_reason`` says expired or
   revoked) → notify the org's admins once per connection.
2. **Quiet and nearing expiry** — fewer than ``FEOH_ERP_CONNECTION_KEEPALIVE_DAYS``
   left → refresh it now (``get_access_token(force_refresh=True)``). A
   tenant that posts nothing for a quarter would otherwise lose its
   connection to inactivity alone. The refresh goes through the refresher's
   own lock and compare-and-swap, so it can never burn a token a concurrent
   caller rotated.
3. **Still inside a notice lead time after that** (the hard cap, or a
   provider that does not roll the expiry) → notify the admins, in-app and by
   email (``notify_event``, preference-gated), once per (expiry, lead):
   ``settings.erp.oauth.expiry_notice = {"expires_at", "lead_days"}``. A new
   expiry (a rotation, a reconnect) re-arms every lead; a more urgent lead
   sends again. The notification goes first and the marker after it, so a
   crash between them repeats a warning rather than losing one.

Never moves money, never touches a tenant's books. Disabled by default
(``FEOH_ERP_CONNECTION_HEALTH_ENABLED``), like every outbound sweep.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.database import control_session_factory, get_tenant_engine
from app.models.notification import EVENT_ERP_CONNECTION_EXPIRING
from app.models.organization import Organization
from app.services import erp_oauth
from app.services.notification_dispatch import notify_event, resolve_role_user_ids
from app.services.notification_templates import (
    ERP_NOTICE_EXPIRING,
    ERP_NOTICE_RECONNECT,
    render_erp_connection,
)

logger = logging.getLogger(__name__)

#: ``settings.erp.oauth`` keys this sweep owns (non-secret bookkeeping).
EXPIRY_NOTICE_KEY = "expiry_notice"
RECONNECT_NOTICE_KEY = "reconnect_notified"

ADMIN_ROLE = "admin"


def notice_leads() -> list[int]:
    """The configured lead times, most distant first, positive only."""
    return sorted({int(d) for d in settings.erp_connection_expiry_notice_days if int(d) > 0})[::-1]


def due_lead(days_left: float, already: dict | None, expires_at: str) -> int | None:
    """The lead time whose notice is due now, or None.

    The most urgent lead the connection is inside of, unless a notice for this
    same expiry at that lead or a more urgent one was already sent.
    """
    inside = [lead for lead in notice_leads() if days_left <= lead]
    if not inside:
        return None
    lead = min(inside)
    if isinstance(already, dict) and already.get("expires_at") == expires_at:
        sent = already.get("lead_days")
        if isinstance(sent, int) and sent <= lead:
            return None
    return lead


def expiring_soon(oauth: dict, *, now: datetime | None = None) -> bool:
    """Is the connection inside the most distant notice lead? (For the status
    endpoint: the settings page offers Reconnect from then on.)"""
    expires = erp_oauth.parse_timestamp(oauth.get("refresh_token_expires_at"))
    leads = notice_leads()
    if expires is None or not leads:
        return False
    return (expires - (now or datetime.now(UTC))).total_seconds() / 86400 <= leads[0]


@dataclass
class ConnectionHealthResult:
    connections_scanned: int = 0
    refreshed: int = 0
    expiry_notices: int = 0
    reconnect_notices: int = 0
    failures: int = 0


async def notify_org_admins(
    org: Organization, rendered, *, event_type: str = EVENT_ERP_CONNECTION_EXPIRING
) -> bool:
    """Tell the org's admins, in-app and by email (preference-gated). Whether
    anyone was actually notified. Also the delivery of the ERP call-quota
    notices (``services/erp_call_budget``, ``erp_call_quota``)."""
    recipients = await resolve_role_user_ids(org.id, ADMIN_ROLE)
    if not recipients:
        return False
    factory = async_sessionmaker(get_tenant_engine(org.db_name), expire_on_commit=False)
    async with factory() as db:
        notified = await notify_event(
            db,
            correlation_id=uuid.uuid4(),
            organization_id=org.id,
            event_type=event_type,
            entity_id=None,
            recipient_user_ids=recipients,
            rendered=rendered,
            entity_type="organization",
        )
        await db.commit()
    return bool(notified)


async def _resolved_erp(org: Organization) -> dict:
    from app.services.provider_credentials import provider_config

    async with control_session_factory() as ctrl:
        return await provider_config(org, "erp", db=ctrl) or {}


async def _reload_oauth(org_id: uuid.UUID) -> dict:
    async with control_session_factory() as ctrl:
        org_settings = (
            await ctrl.execute(select(Organization.settings).where(Organization.id == org_id))
        ).scalar_one_or_none()
    erp = (org_settings or {}).get("erp") if isinstance(org_settings, dict) else None
    oauth = erp.get("oauth") if isinstance(erp, dict) else None
    return dict(oauth) if isinstance(oauth, dict) else {}


async def check_connection(
    org: Organization, *, now: datetime, result: ConnectionHealthResult
) -> None:
    """One org's connection: notify a lost one, refresh a quiet one, warn an
    expiring one. Raises on our own failure (the caller counts it)."""
    oauth = ((org.settings or {}).get("erp") or {}).get("oauth")
    if not isinstance(oauth, dict):
        return
    spec = erp_oauth.load_providers().get(str(oauth.get("provider") or ""))
    connection_id = str(oauth.get("connection_id") or "")
    if spec is None or not connection_id:
        return
    result.connections_scanned += 1

    if oauth.get("needs_reconnect"):
        if oauth.get(RECONNECT_NOTICE_KEY) == connection_id:
            return
        reason = str(oauth.get("reconnect_reason") or erp_oauth.reconnect_reason(oauth, now=now))
        if await notify_org_admins(
            org,
            render_erp_connection(
                notice=ERP_NOTICE_RECONNECT, erp_name=spec.display_name, reason=reason
            ),
        ):
            await erp_oauth.update_connection_metadata(
                spec.key, org.id, connection_id, {RECONNECT_NOTICE_KEY: connection_id}
            )
            result.reconnect_notices += 1
        return

    expires = erp_oauth.parse_timestamp(oauth.get("refresh_token_expires_at"))
    if expires is None:
        return  # the provider reports no expiry: nothing to keep alive or warn about
    days_left = (expires - now).total_seconds() / 86400
    if days_left <= settings.erp_connection_keepalive_days:
        try:
            await erp_oauth.get_access_token(spec, await _resolved_erp(org), force_refresh=True)
            result.refreshed += 1
        except erp_oauth.ErpNotConnectedError:
            # Refused: the refresher marked it `needs_reconnect` (or the app is
            # gone). The next tick sends the reconnect notice.
            pass
        oauth = await _reload_oauth(org.id)
        if oauth.get("needs_reconnect") or str(oauth.get("connection_id") or "") != connection_id:
            return
        expires = erp_oauth.parse_timestamp(oauth.get("refresh_token_expires_at"))
        if expires is None:
            return
        days_left = (expires - now).total_seconds() / 86400

    expires_at = expires.isoformat()
    lead = due_lead(days_left, oauth.get(EXPIRY_NOTICE_KEY), expires_at)
    if lead is None:
        return
    rendered = render_erp_connection(
        notice=ERP_NOTICE_EXPIRING,
        erp_name=spec.display_name,
        expires_on=expires.date(),
        days_left=max(0, int(days_left)),
    )
    if await notify_org_admins(org, rendered):
        await erp_oauth.update_connection_metadata(
            spec.key,
            org.id,
            connection_id,
            {EXPIRY_NOTICE_KEY: {"expires_at": expires_at, "lead_days": lead}},
        )
        result.expiry_notices += 1


async def run_connection_health_once(*, now: datetime | None = None) -> ConnectionHealthResult:
    """One pass over every org with an OAuth ERP connection. One org's failure
    is counted (class only, never a message) and never stops the others."""
    result = ConnectionHealthResult()
    moment = now or datetime.now(UTC)
    oauth = Organization.settings["erp"]["oauth"]
    async with control_session_factory() as ctrl:
        orgs = (
            (await ctrl.execute(select(Organization).where(oauth["provider"].astext.isnot(None))))
            .scalars()
            .all()
        )
    for org in orgs:
        try:
            await check_connection(org, now=moment, result=result)
        except Exception as exc:  # noqa: BLE001 — one org must not halt the sweep
            result.failures += 1
            logger.warning(
                "[erp-connection-health] org %s failed: %s", org.id, exc.__class__.__name__
            )
    if result.refreshed or result.expiry_notices or result.reconnect_notices or result.failures:
        logger.info(
            "[erp-connection-health] %d connection(s): %d refreshed, %d expiry notice(s), "
            "%d reconnect notice(s), %d failed",
            result.connections_scanned,
            result.refreshed,
            result.expiry_notices,
            result.reconnect_notices,
            result.failures,
        )
    return result


async def run_connection_health_loop() -> None:
    """Long-lived loop started in ``main.lifespan``; body is the shared runner."""
    from app.services.sweep_health import SWEEP_ERP_CONNECTION_HEALTH, run_sweep_loop

    await run_sweep_loop(
        SWEEP_ERP_CONNECTION_HEALTH,
        lambda: run_connection_health_once(),
        interval_seconds=settings.erp_connection_health_interval_seconds,
        log=logger,
        log_prefix="[erp-connection-health]",
    )
