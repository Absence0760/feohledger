"""80% / 100% / spending-cap notices for the AI-read-invoice allowance.

Decisions §253: paid tiers bill overage instead of stopping, "with 80% and 100%
notices so the bill is never a surprise"; Free pauses AI reading at 100%, and a
paid org pauses at its own spending cap. Each of those moments gets ONE in-app
notification + email to the org's admins, per threshold, per calendar month.

Idempotency — a claim, not a send-then-mark
-------------------------------------------
``Organization.settings.billing.ai_usage_notices[period]`` lists the
thresholds already announced. The check runs after every billable read and on
every reconciliation tick, so two of them can race to the same threshold. The
threshold is therefore CLAIMED first, under ``lock_organization``: the claimer
that adds the key sends, the other finds it present and does not. If the send
then reaches nobody (notifications off, no admin, every admin opted out —
``notify_event``'s count), the claim is released so a later check retries,
the same contract ``cash_flow_alerts`` keeps for its marker.

The trade this makes: a crash between the claim and the send loses that notice
(at most once), where ``cash_flow_alerts`` sends first and risks a duplicate.
Here a duplicate is the likelier failure — the post-read check runs on every
extraction, several at once — and a billing email that arrives twice reads as
a billing error.

Several thresholds due at once (the first check of the month after an outage,
or a cap set below what is already used) send ONE notice, for the most severe,
and claim the milder ones with it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.database import control_session_factory
from app.models.notification import EVENT_AI_INVOICE_USAGE
from app.models.organization import Organization
from app.services.billing.ai_invoice_meter import (
    BILLING_SETTINGS_KEY,
    AiAllowance,
    allowance_for_plan,
    cap_units,
    count_ai_invoices,
    overage_units,
    parse_spend_cap,
    period_of,
)
from app.services.billing.entitlements import get_active_subscription
from app.services.billing.plan_catalog import CATALOG_CURRENCY
from app.services.notification_templates import (
    AI_NOTICE_80,
    AI_NOTICE_100,
    AI_NOTICE_CAP,
    render_ai_invoice_usage,
)
from app.tenant import lock_organization

logger = logging.getLogger(__name__)

#: ``settings.billing`` key: ``{period: [notice keys already sent]}``.
NOTICES_KEY = "ai_usage_notices"

#: Most severe first — the one a batch of due thresholds is announced as.
_SEVERITY = (AI_NOTICE_CAP, AI_NOTICE_100, AI_NOTICE_80)

#: Who hears about the plan's usage: the people who can change the plan or the
#: cap (`/api/billing/spending-cap` and the plan picker are admin surfaces).
NOTICE_ROLE = "admin"

_PERIODS_KEPT = 3


def due_notices(used: int, allowance: AiAllowance, cap) -> list[str]:
    """Pure: the thresholds ``used`` has reached this month, mildest first.

    80% is integer arithmetic (``used * 5 >= included * 4``) so 80% of 101 is
    reached at 81, never via a float. An allowance of 0 has no 80% to warn at;
    its 100% is reached immediately. The cap notice is due when this month's
    overage has used every unit the cap pays for.
    """
    if not allowance.metered:
        return []
    assert allowance.included is not None
    included = allowance.included
    due: list[str] = []
    if included > 0 and used * 5 >= included * 4:
        due.append(AI_NOTICE_80)
    if used >= included:
        due.append(AI_NOTICE_100)
    limit = cap_units(allowance, cap)
    if limit is not None and used >= included and overage_units(used, allowance) >= limit:
        due.append(AI_NOTICE_CAP)
    return due


def sent_notices(org_settings: dict | None, period: str) -> set[str]:
    billing = (org_settings or {}).get(BILLING_SETTINGS_KEY) or {}
    raw = (billing.get(NOTICES_KEY) or {}).get(period) or []
    return {str(k) for k in raw} if isinstance(raw, list) else set()


def _oldest_kept(period: str) -> str:
    year, month = (int(p) for p in period.split("-"))
    month -= _PERIODS_KEPT - 1
    while month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}"


async def _mutate_claims(organization_id, period: str, *, add: list[str], remove: list[str]):
    """Add / remove notice keys for ``period`` under the org lock. Returns the
    keys THIS call added (already-present keys are someone else's claim)."""
    async with control_session_factory() as ctrl:
        org = await ctrl.get(Organization, organization_id)
        if org is None:
            return []
        org = await lock_organization(ctrl, org)
        settings_dict = dict(org.settings or {})
        billing = dict(settings_dict.get(BILLING_SETTINGS_KEY) or {})
        keep_from = _oldest_kept(period)
        notices = {
            p: list(v)
            for p, v in (billing.get(NOTICES_KEY) or {}).items()
            if isinstance(p, str) and p >= keep_from and isinstance(v, list)
        }
        current = notices.get(period, [])
        claimed = [k for k in add if k not in current]
        current = [k for k in current if k not in remove] + claimed
        notices[period] = current
        billing[NOTICES_KEY] = notices
        settings_dict[BILLING_SETTINGS_KEY] = billing
        org.settings = settings_dict
        flag_modified(org, "settings")
        await ctrl.commit()
        return claimed


async def send_due_ai_usage_notices(
    tenant_db: AsyncSession, *, organization_id, now: datetime | None = None
) -> list[str]:
    """Announce any newly reached threshold for the current month.

    ``tenant_db`` is the org's tenant session, NOT mid-transaction: the in-app
    rows are committed on it here. Returns the notice keys claimed and
    delivered (empty when nothing was due or nobody could be told).
    """
    moment = now or datetime.now(UTC)
    period = period_of(moment)

    async with control_session_factory() as ctrl:
        org = await ctrl.get(Organization, organization_id)
        if org is None:
            return []
        active = await get_active_subscription(ctrl, organization_id)
        org_settings = dict(org.settings or {})
    plan = active[1] if active else None
    allowance = allowance_for_plan(plan)
    if not allowance.metered:
        return []
    # The unit price and the cap are both in the plan's currency; a negotiated
    # non-USD plan must not read "USD" in its notice.
    currency = plan.currency if plan is not None else CATALOG_CURRENCY
    cap = parse_spend_cap(org_settings)

    used = await count_ai_invoices(tenant_db, organization_id=organization_id, period=period)
    already = sent_notices(org_settings, period)
    pending = [k for k in due_notices(used, allowance, cap) if k not in already]
    if not pending:
        return []

    claimed = await _mutate_claims(organization_id, period, add=pending, remove=[])
    if not claimed:
        return []

    from app.services.notification_dispatch import notify_event, resolve_role_user_ids

    headline = next(k for k in _SEVERITY if k in claimed)
    notified = 0
    try:
        recipients = await resolve_role_user_ids(organization_id, NOTICE_ROLE)
        if recipients:
            assert allowance.included is not None
            rendered = render_ai_invoice_usage(
                notice=headline,
                period=period,
                used=used,
                included=allowance.included,
                overage_unit_price=allowance.overage_unit_price,
                spend_cap=cap,
                currency=currency,
            )
            notified = await notify_event(
                tenant_db,
                correlation_id=uuid.uuid4(),
                organization_id=organization_id,
                event_type=EVENT_AI_INVOICE_USAGE,
                entity_id=None,
                recipient_user_ids=recipients,
                rendered=rendered,
                entity_type="billing",
            )
            await tenant_db.commit()
    finally:
        if not notified:
            # Reached nobody (or raised): release the claim so a later check
            # retries instead of the threshold going unannounced all month.
            await _mutate_claims(organization_id, period, add=[], remove=claimed)
            logger.warning(
                "[ai-usage-notices] org=%s: %s notice reached no recipient; will retry",
                organization_id,
                headline,
            )
    return claimed if notified else []
