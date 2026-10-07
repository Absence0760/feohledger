"""The AI-read-invoice meter — the ONE owner of "how many has this org used".

Decisions §253 makes an **AI-read invoice** the platform's single metered unit:
a distinct invoice whose extraction ran on the *platform's* model key and
succeeded, counted per calendar month in UTC. Everything that needs that number
— the extraction gate that pauses AI reading, the ``ai_invoices`` usage meter,
the overage reporter that bills Stripe, the 80% / 100% notices and the billing
page — reads it through :func:`count_ai_invoices` here, so there is exactly one
definition of the unit and no second query to drift from it.

The counting rule
-----------------
``COUNT(DISTINCT invoice_id)`` over ``extraction_usage`` rows (a TENANT table —
decisions §57) with:

* ``program_type = 'platform'`` — BYOK runs on the customer's own key, so it is
  their model bill and never ours to resell;
* ``success = true`` — a failed read is our retry, not the customer's usage
  (§253 rejects "charging for our own retries");
* ``provider`` in :data:`BILLABLE_EXTRACTION_PROVIDERS` — see below;
* ``period`` = the UTC ``YYYY-MM`` the row was written in. ``run_extraction``
  stamps ``datetime.now(UTC).strftime("%Y-%m")``, so the month boundary is UTC
  by construction and this module never re-derives it from ``created_at``.

DISTINCT is what makes a re-read of the same invoice in the same month count
once; the period key is what makes the same invoice read again *next* month
count again (it was read again, on our key, in a new allowance window).

Which providers are billable
----------------------------
The question is "did this read cost the platform a per-document model or OCR
charge?" — that marginal cost is the whole reason the unit exists.

* ``claude_vision``, ``openai_vision`` — a paid vendor API call per document.
* ``aws_textract`` — AWS bills per page. An operator can point platform mode at
  it (``FEOH_EXTRACTION_PROVIDER``), and then it is our bill.
* ``mock`` — NOT billable. It is the keyless local-dev / e2e reader and returns
  a fixture; counting it would trip the Free limit on every e2e tenant (they are
  all on ``free`` and extract through ``mock``) and bill for no read at all.
* ``ollama`` — NOT billable. Self-hosted: there is no per-call charge to pass
  on, and pricing a self-hosted model per invoice would be inventing a cost.
* ``einvoice`` — NOT billable. Structured e-invoices (UBL / CII / Factur-X) are
  parsed deterministically; ``run_extraction`` routes them to this adapter
  BEFORE any model is chosen, but it still writes a ``platform`` usage row, so
  the exclusion has to live here.

Both sets are declared, and ``tests/test_ai_invoice_meter.py`` asserts every
registered extraction adapter sits in exactly one of them — a new adapter
forces an explicit billing decision instead of silently defaulting. At read
time an unlisted provider is NOT counted: undercounting costs us a few cents,
overcounting bills a customer for something nobody decided was billable.

Allowance + spending cap
------------------------
The allowance comes from the live plan's ``usage_components[METER_AI_INVOICES]``
(``{"included": int, "overage_unit_price": decimal-string | null}``). The
customer's optional monthly overage cap lives on
``Organization.settings.billing.monthly_spend_cap`` (a USD decimal string), and
:func:`decide` is the pure function that turns (used, already-counted, allowance,
cap) into "read" or "pause, because …".

Money is ``Decimal`` throughout; nothing here moves money.
"""

from __future__ import annotations

import calendar
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usage import ExtractionUsage
from app.services.billing.plan_catalog import DEFAULT_PLAN_CATALOG, METER_AI_INVOICES

logger = logging.getLogger(__name__)

#: Providers whose platform-mode read costs the platform a per-document charge.
BILLABLE_EXTRACTION_PROVIDERS: frozenset[str] = frozenset(
    {"claude_vision", "openai_vision", "aws_textract"}
)

#: Providers that never count, each for a reason in the module docstring.
NON_BILLABLE_EXTRACTION_PROVIDERS: frozenset[str] = frozenset({"mock", "ollama", "einvoice"})

#: The only program type that can count. BYOK is the customer's own model bill.
BILLABLE_PROGRAM_TYPE = "platform"

#: The Free tier's allowance, used for an org with NO live subscription (one
#: provisioned before `ensure_subscription` existed). `free` is the plan every
#: new tenant lands on, so "no plan" is read as the default plan rather than as
#: unlimited AI reading on our key.
_FREE_COMPONENT: dict = next(p for p in DEFAULT_PLAN_CATALOG if p["code"] == "free")[
    "usage_components"
][METER_AI_INVOICES]

#: Pause reasons. Each maps to an invoice-warning code
#: (`invoice_warning_catalog`) the browser localizes.
PAUSE_ALLOWANCE_REACHED = "allowance_reached"
PAUSE_SPEND_CAP_REACHED = "spend_cap_reached"

#: Where the customer's cap lives inside `Organization.settings`.
BILLING_SETTINGS_KEY = "billing"
SPEND_CAP_KEY = "monthly_spend_cap"


def period_of(when: datetime | None = None) -> str:
    """The UTC calendar month (``YYYY-MM``) ``when`` falls in — the meter key.

    Same expression ``run_extraction`` stamps onto ``ExtractionUsage.period``;
    a naive datetime is taken as UTC.
    """
    moment = when or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m")


def is_billable_read(*, program_type: str | None, provider: str | None) -> bool:
    """Would a successful extraction with this config count as an AI read?"""
    return program_type == BILLABLE_PROGRAM_TYPE and provider in BILLABLE_EXTRACTION_PROVIDERS


def _billable_filters(organization_id, period: str) -> tuple:
    return (
        ExtractionUsage.organization_id == organization_id,
        ExtractionUsage.period == period,
        ExtractionUsage.program_type == BILLABLE_PROGRAM_TYPE,
        ExtractionUsage.success.is_(True),
        ExtractionUsage.provider.in_(sorted(BILLABLE_EXTRACTION_PROVIDERS)),
    )


async def count_ai_invoices(db: AsyncSession, *, organization_id, period: str) -> int:
    """AI-read invoices for the org in ``period``. ``db`` is a TENANT session."""
    count = (
        await db.execute(
            select(func.count(func.distinct(ExtractionUsage.invoice_id))).where(
                *_billable_filters(organization_id, period)
            )
        )
    ).scalar_one()
    return int(count or 0)


async def is_invoice_counted(
    db: AsyncSession, *, organization_id, period: str, invoice_id: uuid.UUID
) -> bool:
    """Has ``invoice_id`` already been counted this period? A re-read of a
    counted invoice is free, so the gate must never refuse it."""
    row = (
        await db.execute(
            select(ExtractionUsage.id)
            .where(*_billable_filters(organization_id, period))
            .where(ExtractionUsage.invoice_id == invoice_id)
            .limit(1)
        )
    ).first()
    return row is not None


# ---------------------------------------------------------------------------
# Allowance + cap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AiAllowance:
    """The org's AI-read allowance this month.

    ``included is None`` means the plan does not meter AI reads at all (an
    operator-configured negotiated plan with no component) — reading is never
    paused and nothing is billed as overage. ``overage_unit_price is None``
    means "pause at the limit" (Free).
    """

    included: int | None
    overage_unit_price: Decimal | None
    plan_code: str | None = None

    @property
    def metered(self) -> bool:
        return self.included is not None

    @property
    def bills_overage(self) -> bool:
        return self.included is not None and self.overage_unit_price is not None


UNMETERED = AiAllowance(included=None, overage_unit_price=None)


def allowance_from_component(component, *, plan_code: str | None = None) -> AiAllowance:
    """Parse a ``usage_components[METER_AI_INVOICES]`` value.

    A missing component means the plan does not meter AI reads (unmetered). A
    MALFORMED one is logged and also read as unmetered: a typo in an operator's
    plan row must not pause a paying customer's AI reading or bill them an
    overage nobody priced. The error log is the signal to fix the row.
    """
    if component is None:
        return AiAllowance(included=None, overage_unit_price=None, plan_code=plan_code)
    try:
        included = component["included"]
        if isinstance(included, bool) or not isinstance(included, int) or included < 0:
            raise ValueError("included must be a non-negative int")
        raw_price = component.get("overage_unit_price")
        price = None if raw_price is None else Decimal(str(raw_price))
        if price is not None and (not price.is_finite() or price <= 0):
            raise ValueError("overage_unit_price must be a positive decimal")
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        logger.error(
            "[ai-meter] plan %s has a malformed %s usage component; treating it as unmetered",
            plan_code,
            METER_AI_INVOICES,
        )
        return AiAllowance(included=None, overage_unit_price=None, plan_code=plan_code)
    return AiAllowance(included=included, overage_unit_price=price, plan_code=plan_code)


def allowance_for_plan(plan) -> AiAllowance:
    """The allowance a ``Plan`` grants; the Free default when ``plan is None``."""
    if plan is None:
        return allowance_from_component(_FREE_COMPONENT, plan_code=None)
    components = plan.usage_components or {}
    return allowance_from_component(components.get(METER_AI_INVOICES), plan_code=plan.code)


def parse_spend_cap(org_settings: dict | None) -> Decimal | None:
    """The customer's monthly overage cap (USD), or ``None`` for "no cap".

    Stored as an exact decimal string. Anything unreadable is treated as no cap
    and logged — never as a zero cap, which would pause a paid org's reading on
    a corrupted value nobody set.
    """
    billing = (org_settings or {}).get(BILLING_SETTINGS_KEY) or {}
    raw = billing.get(SPEND_CAP_KEY)
    if raw is None or raw == "":
        return None
    try:
        cap = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        logger.error("[ai-meter] unreadable %s in org settings; ignoring it", SPEND_CAP_KEY)
        return None
    if not cap.is_finite() or cap < 0:
        logger.error("[ai-meter] out-of-range %s in org settings; ignoring it", SPEND_CAP_KEY)
        return None
    return cap


def cap_units(allowance: AiAllowance, cap: Decimal | None) -> int | None:
    """How many overage invoices the cap pays for, or ``None`` when uncapped.

    Floor division: a cap of $1.05 at $0.10 buys 10 overage reads, not 11 —
    the 11th would put the bill at $1.10, past what the customer allowed.
    """
    if cap is None or not allowance.bills_overage:
        return None
    assert allowance.overage_unit_price is not None
    return int((cap / allowance.overage_unit_price).to_integral_value(rounding=ROUND_FLOOR))


def overage_units(used: int, allowance: AiAllowance) -> int:
    """AI-read invoices past the allowance this period (0 when unmetered)."""
    if allowance.included is None:
        return 0
    return max(0, used - allowance.included)


def billable_overage_units(used: int, allowance: AiAllowance, cap: Decimal | None) -> int:
    """Overage units the customer is actually billed for.

    The cap is applied HERE as well as at the gate, so the bill can never pass
    it even when two concurrent extractions both slipped under the gate at the
    boundary (decisions — the gate's overshoot is ours to absorb, never the
    customer's). Free (no overage price) bills nothing.
    """
    if not allowance.bills_overage:
        return 0
    units = overage_units(used, allowance)
    limit = cap_units(allowance, cap)
    return units if limit is None else min(units, limit)


def overage_amount(units: int, allowance: AiAllowance) -> Decimal:
    """Exact money for ``units`` overage reads, quantized to the cent."""
    if not allowance.bills_overage or units <= 0:
        return Decimal("0.00")
    assert allowance.overage_unit_price is not None
    return (allowance.overage_unit_price * units).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class AiReadDecision:
    """Whether the next AI read may run, and why not if it may not."""

    allowed: bool
    reason: str | None = None  # PAUSE_* when not allowed
    used: int = 0
    allowance: AiAllowance = UNMETERED
    cap: Decimal | None = None


def decide(
    *,
    used: int,
    already_counted: bool,
    allowance: AiAllowance,
    cap: Decimal | None,
) -> AiReadDecision:
    """Pure: may one more AI read run for this org this month?

    * a re-read of an invoice already counted this month costs nothing more, so
      it is always allowed — whatever the counters say;
    * an unmetered plan never pauses;
    * within the allowance → allowed;
    * past it with no overage price (Free) → pause, ``allowance_reached``;
    * past it on a paid tier → allowed, unless the read would take this
      month's overage past the customer's cap → pause, ``spend_cap_reached``.
    """
    base = {"used": used, "allowance": allowance, "cap": cap}
    if already_counted or not allowance.metered:
        return AiReadDecision(allowed=True, **base)
    assert allowance.included is not None
    next_count = used + 1
    if next_count <= allowance.included:
        return AiReadDecision(allowed=True, **base)
    if allowance.overage_unit_price is None:
        return AiReadDecision(allowed=False, reason=PAUSE_ALLOWANCE_REACHED, **base)
    limit = cap_units(allowance, cap)
    if limit is not None and next_count - allowance.included > limit:
        return AiReadDecision(allowed=False, reason=PAUSE_SPEND_CAP_REACHED, **base)
    return AiReadDecision(allowed=True, **base)


@dataclass(frozen=True)
class OrgBillingContext:
    """The control-plane facts the meter needs, read in ONE short session."""

    allowance: AiAllowance
    cap: Decimal | None
    org_settings: dict


async def load_org_billing_context(organization_id) -> OrgBillingContext:
    """Read the org's live plan allowance + spend cap from the control plane.

    Opens and closes its own control session (``control_session_factory`` —
    the dispatcher-scoped one inside an extraction worker), so a caller holding
    a tenant session never also holds a second control connection open: the
    worker's control pool is a single connection.
    """
    from app.database import control_session_factory
    from app.models.organization import Organization
    from app.services.billing.entitlements import get_active_subscription

    async with control_session_factory() as ctrl:
        org = await ctrl.get(Organization, organization_id)
        active = await get_active_subscription(ctrl, organization_id)
        org_settings = dict((org.settings if org else None) or {})
    plan = active[1] if active else None
    return OrgBillingContext(
        allowance=allowance_for_plan(plan),
        cap=parse_spend_cap(org_settings),
        org_settings=org_settings,
    )


async def check_ai_read(
    db: AsyncSession,
    *,
    organization_id,
    invoice_id: uuid.UUID,
    program_type: str | None,
    provider: str | None,
    now: datetime | None = None,
) -> AiReadDecision:
    """The extraction gate: may this invoice be read on the platform's model?

    Non-billable reads (BYOK, ``mock``, ``ollama``, a structured e-invoice) are
    allowed without touching the control plane at all — that is why e2e and
    local dev can never trip the Free limit.

    **Race window, deliberately not locked.** Two extractions for one org can
    both read ``used = included - 1`` and both proceed, so the count can end a
    few over the allowance. Serialising every extraction of an org behind a
    lock held across a 5–30 s model call would cap a tenant at one read at a
    time to protect a few cents; a reservation row would need a schema change.
    The overshoot is bounded by the number of concurrent workers, it is OUR
    cost (a few extra free reads), and it never reaches the customer's bill:
    the overage reporter bills ``billable_overage_units``, which clamps to the
    cap independently of the gate.
    """
    if not is_billable_read(program_type=program_type, provider=provider):
        return AiReadDecision(allowed=True)
    period = period_of(now)
    if await is_invoice_counted(
        db, organization_id=organization_id, period=period, invoice_id=invoice_id
    ):
        return AiReadDecision(allowed=True)
    context = await load_org_billing_context(organization_id)
    used = await count_ai_invoices(db, organization_id=organization_id, period=period)
    return decide(used=used, already_counted=False, allowance=context.allowance, cap=context.cap)


# ---------------------------------------------------------------------------
# The read model the billing page shows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AiUsageSummary:
    """This month's AI-read usage, priced. Every amount is an exact Decimal."""

    period: str
    used: int
    allowance: AiAllowance
    cap: Decimal | None
    overage_units: int
    overage_amount: Decimal
    #: Overage at the month's current daily pace, clamped to the cap.
    projected_overage_amount: Decimal
    paused: bool
    pause_reason: str | None


def project_month_end(used: int, *, now: datetime) -> int:
    """Linear month-end projection of the AI-read count at today's pace.

    Pace is per elapsed day (fractional), so day 1 at noon projects off half a
    day. Integer arithmetic on seconds, so no float ever reaches a money value:
    the result is a count, floored.
    """

    moment = now.astimezone(UTC)
    days_in_month = calendar.monthrange(moment.year, moment.month)[1]
    month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((moment - month_start).total_seconds())
    if used <= 0 or elapsed <= 0:
        return used
    total = days_in_month * 86400
    return max(used, (used * total) // elapsed)


def summarize(
    *, used: int, allowance: AiAllowance, cap: Decimal | None, now: datetime
) -> AiUsageSummary:
    """Pure: price this month's usage for display."""
    units = billable_overage_units(used, allowance, cap)
    projected_units = billable_overage_units(project_month_end(used, now=now), allowance, cap)
    next_read = decide(used=used, already_counted=False, allowance=allowance, cap=cap)
    return AiUsageSummary(
        period=period_of(now),
        used=used,
        allowance=allowance,
        cap=cap,
        overage_units=units,
        overage_amount=overage_amount(units, allowance),
        projected_overage_amount=overage_amount(projected_units, allowance),
        paused=not next_read.allowed,
        pause_reason=next_read.reason,
    )
