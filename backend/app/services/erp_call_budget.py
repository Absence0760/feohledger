"""A per-org budget against an ERP API quota that several tenants share.

Blackbaud's SKY API meters calls **per subscription key**: a rate limit (SKY
answers 429) and a daily quota that resets at 00:00 UTC (SKY answers 403 with
``Retry-After``), both with ``Retry-After`` saying how long to wait. One
platform key (``FEOH_ERP_BLACKBAUD_SUBSCRIPTION_KEY``) serves every tenant that
did not store its own, so without a budget one tenant's bulk send or vendor
sync spends the day for all of them. Decisions §322.

What this module keeps, per subscription key (digested, never the key itself)
and per UTC day, in Redis:

* the calls made on the key (``total``) and by each org on it;
* a **cool-down** set from a 429 / quota 403 and its ``Retry-After``, which
  every tenant on that key honours, so nobody keeps calling into a refusal.

:meth:`CallBudget.check` answers whether an org may spend ``reserve`` more
calls. An org on the shared platform key may spend
``FEOH_ERP_BLACKBAUD_TENANT_DAILY_CALL_BUDGET`` a day; any org may spend only
what is left of ``FEOH_ERP_BLACKBAUD_DAILY_CALL_QUOTA`` on its key. A refusal
is coded (``base.ERP_CALL_BUDGET_EXHAUSTED``, reason + retry time) and is made
**before** a call, so it never leaves anything half done in the ERP.

Why Redis: the counts are transient (a day), must be shared by every worker,
the dispatch task and the Lambda path, and have to be incremented atomically
on every call — the ``services/rate_limit`` shape, not a row in a tenant
database written on every HTTP request. Losing them (a Redis restart) costs at
most a day's count, and SKY's own 403 remains the backstop. A Redis error
**fails open** (the call goes ahead, counted nowhere): this is a courtesy brake
on a quota the provider enforces itself, and failing closed would stop every
Blackbaud post on a Redis blip.

The org's admins hear about it through the ordinary notification centre
(``erp_call_quota``): once a day each when the org passes 80% of its share,
when its share is used, and when the subscription's quota is.
"""

from __future__ import annotations

import enum
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from app.redis import get_redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "erpcalls:"

#: Counters and notice claims outlive their day by one, so a call at 23:59 UTC
#: and a check at 00:00 never race the expiry.
_DAY_TTL_SECONDS = 2 * 86_400

#: A ``Retry-After`` is honoured up to a day (the quota's own period); a
#: missing or unreadable one on a 429 waits this long.
_MAX_COOLDOWN_SECONDS = 86_400
_DEFAULT_COOLDOWN_SECONDS = 1

#: The share of its budget at which an org is warned (80%, as the AI-read
#: allowance warns, decisions §253).
WARN_NUMERATOR, WARN_DENOMINATOR = 4, 5


class BudgetReason(enum.StrEnum):
    #: This org has used its daily share of the shared platform key.
    tenant_budget = "tenant_budget"
    #: The subscription's daily quota is used — by our own count, or SKY said
    #: so with a 403 + ``Retry-After``.
    subscription_quota = "subscription_quota"
    #: SKY answered 429: too many calls a second. Over in seconds.
    rate_limited = "rate_limited"


class QuotaNotice(enum.StrEnum):
    """The notices an org's admins get, once per UTC day each."""

    warning = "warning"
    tenant_budget = "tenant_budget"
    subscription_quota = "subscription_quota"


@dataclass(frozen=True)
class BudgetRefusal:
    reason: BudgetReason
    retry_after: datetime

    @property
    def retryable(self) -> bool:
        """Whether ``services/erp``'s seconds-long backoff could outlast it."""
        return self.reason is BudgetReason.rate_limited

    def params(self) -> dict[str, str]:
        return {"reason": self.reason.value, "retry_after": self.retry_after.isoformat()}


class UsageScope(enum.StrEnum):
    """Which limit :class:`CallUsage` measures ``used`` against."""

    #: This org's daily share of the shared platform key
    #: (``FEOH_ERP_BLACKBAUD_TENANT_DAILY_CALL_BUDGET``): ``used`` is the org's
    #: own calls. The key's total is never reported — it is every tenant's.
    org_share = "org_share"
    #: The org's own subscription key: ``used`` is every call on that key,
    #: against its daily quota (``FEOH_ERP_BLACKBAUD_DAILY_CALL_QUOTA``).
    subscription = "subscription"


@dataclass(frozen=True)
class CallUsage:
    """Today's (UTC) calls, as the org may see them (decisions §324)."""

    scope: UsageScope
    used: int
    #: None when no limit applies to this scope (a share of 0, or a quota of 0).
    limit: int | None
    resets_at: datetime
    #: Set while the org's calls are refused: a cool-down on the key from
    #: SKY's 429 / quota 403, or a share or quota used up by our own count.
    paused: BudgetRefusal | None


def next_reset(now: datetime) -> datetime:
    """The next 00:00 UTC, when SKY's daily quota resets."""
    day = now.astimezone(UTC).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def retry_after_seconds(resp: httpx.Response, now: datetime) -> int:
    """``Retry-After`` as seconds (delta or HTTP date), clamped to [1, a day]."""
    raw = (resp.headers.get("retry-after") or "").strip()
    seconds: float | None = None
    if raw.isdigit():
        seconds = float(raw)
    elif raw:
        try:
            seconds = (parsedate_to_datetime(raw) - now).total_seconds()
        except (TypeError, ValueError):
            seconds = None
    if seconds is None:
        seconds = _DEFAULT_COOLDOWN_SECONDS
    return int(min(max(seconds, 1), _MAX_COOLDOWN_SECONDS))


def throttle_reason(resp: httpx.Response) -> BudgetReason | None:
    """What a SKY answer says about our calls: 429 is the rate limit, 403 *with*
    ``Retry-After`` the quota (a bare 403 is a permission refusal)."""
    if resp.status_code == 429:
        return BudgetReason.rate_limited
    if resp.status_code == 403 and "retry-after" in resp.headers:
        return BudgetReason.subscription_quota
    return None


def subscription_digest(subscription_key: str) -> str:
    return hashlib.sha256(subscription_key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CallBudget:
    """One org's view of one subscription key's quota."""

    provider: str
    erp_name: str
    subscription_key: str
    org_id: str
    #: The platform key, shared by every tenant without its own: the
    #: per-tenant share applies. A tenant's own key is its own bucket.
    shared: bool
    daily_quota: int
    tenant_budget: int

    # -- keys -----------------------------------------------------------------

    def _base(self) -> str:
        return f"{KEY_PREFIX}{self.provider}:{subscription_digest(self.subscription_key)}"

    def _total_key(self, day: str) -> str:
        return f"{self._base()}:{day}:total"

    def _org_key(self, day: str) -> str:
        return f"{self._base()}:{day}:org:{self.org_id}"

    def _cooldown_key(self) -> str:
        return f"{self._base()}:cooldown"

    def _notice_key(self, day: str, notice: QuotaNotice) -> str:
        return f"{self._base()}:{day}:notice:{self.org_id}:{notice.value}"

    @property
    def _tenant_limit(self) -> int | None:
        return self.tenant_budget if self.shared and self.tenant_budget > 0 else None

    # -- the gate ---------------------------------------------------------------

    async def check(self, *, reserve: int = 1, now: datetime | None = None) -> BudgetRefusal | None:
        """None when this org may spend ``reserve`` more calls now, else why not.

        A push asks for the most calls it can make (its ``reserve``), so one it
        starts can finish without being cut off half way.
        """
        moment = now or datetime.now(UTC)
        day = moment.strftime("%Y%m%d")
        try:
            r = await get_redis()
            cooldown = await r.get(self._cooldown_key())
            total = int(await r.get(self._total_key(day)) or 0)
            used = int(await r.get(self._org_key(day)) or 0)
        except Exception as exc:  # noqa: BLE001 — fail open, see the module docstring
            logger.warning("[erp-call-budget] check skipped: %s", exc.__class__.__name__)
            return None
        if cooldown:
            refusal = _parse_cooldown(cooldown, moment)
            if refusal is not None:
                return refusal
        reset = next_reset(moment)
        if self.daily_quota > 0 and total + reserve > self.daily_quota:
            await self._notice(day, QuotaNotice.subscription_quota, used=total, moment=moment)
            return BudgetRefusal(BudgetReason.subscription_quota, reset)
        limit = self._tenant_limit
        if limit is not None and used + reserve > limit:
            await self._notice(day, QuotaNotice.tenant_budget, used=used, moment=moment)
            return BudgetRefusal(BudgetReason.tenant_budget, reset)
        return None

    async def usage(self, *, now: datetime | None = None) -> CallUsage | None:
        """Today's usage for this org, or None when Redis cannot say.

        A read only: unlike :meth:`check` it sends no notice and reserves
        nothing, so a settings page can call it as often as it renders. A
        Redis error is None (show nothing), never an error: the figure is a
        courtesy, as the budget itself is.
        """
        moment = now or datetime.now(UTC)
        day = moment.strftime("%Y%m%d")
        try:
            r = await get_redis()
            cooldown = await r.get(self._cooldown_key())
            total = int(await r.get(self._total_key(day)) or 0)
            used = int(await r.get(self._org_key(day)) or 0)
        except Exception as exc:  # noqa: BLE001 — fail open: no figure
            logger.warning("[erp-call-budget] usage unread: %s", exc.__class__.__name__)
            return None
        reset = next_reset(moment)
        quota = self.daily_quota if self.daily_quota > 0 else None
        paused = _parse_cooldown(cooldown, moment) if cooldown else None
        if paused is None and quota is not None and total >= quota:
            paused = BudgetRefusal(BudgetReason.subscription_quota, reset)
        if self.shared:
            limit = self._tenant_limit
            if paused is None and limit is not None and used >= limit:
                paused = BudgetRefusal(BudgetReason.tenant_budget, reset)
            return CallUsage(UsageScope.org_share, used, limit, reset, paused)
        return CallUsage(UsageScope.subscription, total, quota, reset, paused)

    # -- the counter --------------------------------------------------------------

    async def record_call(self, *, now: datetime | None = None) -> None:
        """Count one call against the key and the org (before it is sent: SKY
        counts a call it refuses, too)."""
        moment = now or datetime.now(UTC)
        day = moment.strftime("%Y%m%d")
        try:
            r = await get_redis()
            # One MULTI/EXEC: a crash between an INCR and its EXPIRE would
            # otherwise leave a day's counter that never expires.
            async with r.pipeline(transaction=True) as pipe:
                pipe.incr(self._total_key(day))
                pipe.expire(self._total_key(day), _DAY_TTL_SECONDS)
                pipe.incr(self._org_key(day))
                pipe.expire(self._org_key(day), _DAY_TTL_SECONDS)
                _, _, org_count, _ = await pipe.execute()
            used = int(org_count)
        except Exception as exc:  # noqa: BLE001 — fail open
            logger.warning("[erp-call-budget] count skipped: %s", exc.__class__.__name__)
            return
        limit = self._tenant_limit
        if limit is not None and used * WARN_DENOMINATOR >= limit * WARN_NUMERATOR:
            await self._notice(day, QuotaNotice.warning, used=used, moment=moment)

    async def record_response(self, resp: httpx.Response, *, now: datetime | None = None) -> None:
        """Honour a 429 / quota 403: every tenant on this key waits ``Retry-After``."""
        reason = throttle_reason(resp)
        if reason is None:
            return
        moment = now or datetime.now(UTC)
        seconds = retry_after_seconds(resp, moment)
        until = moment + timedelta(seconds=seconds)
        try:
            r = await get_redis()
            await r.set(self._cooldown_key(), f"{reason.value}|{until.isoformat()}", ex=seconds)
        except Exception as exc:  # noqa: BLE001 — fail open
            logger.warning("[erp-call-budget] cool-down not kept: %s", exc.__class__.__name__)
        if reason is BudgetReason.subscription_quota:
            day = moment.strftime("%Y%m%d")
            await self._notice(day, QuotaNotice.subscription_quota, used=None, moment=moment)

    # -- the notice ---------------------------------------------------------------

    async def _notice(
        self, day: str, notice: QuotaNotice, *, used: int | None, moment: datetime
    ) -> None:
        """Tell the org's admins, once per day per notice. Never raises.

        Claimed before sending (a SET NX), so concurrent pushes send one; a
        notice that reached nobody releases its claim for the next call.
        """
        key = self._notice_key(day, notice)
        try:
            r = await get_redis()
            if not await r.set(key, "1", nx=True, ex=_DAY_TTL_SECONDS):
                return
        except Exception as exc:  # noqa: BLE001 — no claim, no notice
            logger.warning("[erp-call-budget] notice skipped: %s", exc.__class__.__name__)
            return
        try:
            delivered = await deliver_quota_notice(
                self.org_id,
                notice=notice,
                erp_name=self.erp_name,
                used=used,
                budget=self.daily_quota
                if notice is QuotaNotice.subscription_quota
                else (self._tenant_limit),
                resets_at=next_reset(moment),
            )
        except Exception as exc:  # noqa: BLE001 — a notice never fails the call
            logger.warning("[erp-call-budget] notice failed: %s", exc.__class__.__name__)
            delivered = False
        if not delivered:
            try:
                await r.delete(key)
            except Exception:  # noqa: BLE001
                pass


def _parse_cooldown(raw: bytes | str, now: datetime) -> BudgetRefusal | None:
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    reason_text, _, until_text = text.partition("|")
    try:
        reason = BudgetReason(reason_text)
        until = datetime.fromisoformat(until_text)
    except ValueError:
        return None
    return BudgetRefusal(reason, until) if until > now else None


async def deliver_quota_notice(
    org_id: str,
    *,
    notice: QuotaNotice,
    erp_name: str,
    used: int | None,
    budget: int | None,
    resets_at: datetime,
) -> bool:
    """In-app + email to the org's admins (``erp_call_quota``). Whether anyone
    was told."""
    from app.database import control_session_factory
    from app.models.notification import EVENT_ERP_CALL_QUOTA
    from app.models.organization import Organization
    from app.services.erp_connection_health import notify_org_admins
    from app.services.notification_templates import render_erp_call_quota

    try:
        org_uuid = uuid.UUID(str(org_id))
    except ValueError:
        return False
    async with control_session_factory() as ctrl:
        org = await ctrl.get(Organization, org_uuid)
    if org is None:
        return False
    rendered = render_erp_call_quota(
        notice=notice.value, erp_name=erp_name, used=used, budget=budget, resets_at=resets_at
    )
    return await notify_org_admins(org, rendered, event_type=EVENT_ERP_CALL_QUOTA)
