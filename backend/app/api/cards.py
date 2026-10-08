"""Virtual card endpoints — generate, list, cancel, webhook."""

import logging
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import case, extract, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    get_org_id,
    require_permission,
    require_roles,
)
from app.api.pagination import PaginationParams, pagination_params
from app.api.permissions import PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID
from app.config import settings
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.user import User
from app.models.vendor import Vendor
from app.models.virtual_card import CardRebate, VirtualCard
from app.schemas.virtual_card import (
    CardDashboardResponse,
    CardDetailsResponse,
    CardListResponse,
    CardResponse,
    GenerateCardsRequest,
    RebateListResponse,
    RebateResponse,
    RebateStatusBreakdown,
)
from app.services.currency_conversion import (
    card_currency_sql,
    resolve_reporting_currency,
)
from app.services.payment_adapters.base import minor_units_to_decimal
from app.tenant import (
    apply_entity_scope,
    ensure_in_entity_scope,
    get_entity_id,
    get_tenant,
    get_tenant_db,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cards", tags=["cards"])

# Platform default rebate rate when the org has no negotiated rate on file.
_DEFAULT_REBATE_RATE = Decimal("0.0100")  # 1%


def _resolve_rebate_rate(card_config: dict) -> Decimal:
    """The org's negotiated rebate rate from `settings.cards.rebate_rate`,
    falling back to the 1% platform default. Parsed defensively (a malformed
    or out-of-range value must never break payment settlement) and clamped to
    a sane 0–10% band."""
    raw = (card_config or {}).get("rebate_rate")
    if raw is None:
        return _DEFAULT_REBATE_RATE
    try:
        rate = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return _DEFAULT_REBATE_RATE
    if rate < 0 or rate > Decimal("0.10"):
        return _DEFAULT_REBATE_RATE
    return rate


def _classify_card_event(event_type: str) -> tuple[bool, bool]:
    """Classify a card webhook `event_type` into (is_charge_auth, is_settlement).

    Provider event names are matched by substring, but the *non-charging*
    variants are excluded FIRST. Names like `authorization.decline`,
    `authorization.reversal`, or `transaction.voided` all contain
    "auth"/"transaction", so a naive substring match would treat a declined or
    reversed authorization as a real charge — flipping the card to `charged` on
    money that never moved (and later minting a rebate on it). A decline /
    reversal / void / refund / return / cancel / expiry is neither a charge nor
    a settlement: both flags are False and the handler leaves the card untouched.
    """
    et = (event_type or "").lower()
    is_decline_or_reversal = any(
        kw in et
        for kw in (
            "decline",
            "declined",
            "reversal",
            "reversed",
            "void",
            "return",
            "refund",
            "cancel",
            "expire",
        )
    )
    if is_decline_or_reversal:
        return False, False
    is_auth = "authorization" in et or "auth" in et
    is_settled = "transaction" in et or "settlement" in et
    return is_auth, is_settled


def _normalize_charge_amount(
    provider: str, amount, fallback: Decimal | None, currency: str | None = None
) -> Decimal | None:
    """Normalize a webhook charge `amount` to a major-unit Decimal.

    The unit differs by provider: Lithic webhook amounts are in MINOR units
    (cents — e.g. 150000 == $1,500.00), Nium in MAJOR units (e.g. 50.00 ==
    $50.00). Dividing both by 100 recorded 1/100th of every Nium charge (and a
    rebate on it).

    The minor-unit conversion goes through
    `payment_adapters.base.minor_units_to_decimal`, which is ISO-4217-exponent
    aware. A flat `/ 100` is right for the ~universal exponent of 2 and wrong
    in both directions elsewhere: ¥150000 is ¥150,000 (exponent 0), not
    ¥1,500, and 150000 fils is 150 KWD (exponent 3), not 1,500. Lithic is
    USD-only in practice today, so nothing in play is currently mispriced —
    which is exactly why this had to be routed through the one exponent table
    before a card provider or a non-USD card currency arrives, rather than
    after. `currency` is optional because a webhook body need not carry it;
    absent, the helper falls back to the common exponent of 2, i.e. the old
    behaviour.

    A falsy / unparseable amount returns `fallback` (the card's own limit).
    """
    if not amount:
        return fallback
    if provider != "lithic":
        try:
            return Decimal(str(amount))
        except (InvalidOperation, ValueError, TypeError):
            return fallback
    converted = minor_units_to_decimal(amount, currency)
    return fallback if converted is None else converted


def resolve_rebate_base(
    settled_amount: Decimal | None, authorized_amount: Decimal | None
) -> tuple[Decimal, str]:
    """The amount a rebate is earned on, and which figure it came from.

    A card network's SETTLEMENT routinely differs from the authorization it
    clears — partial capture, tips, fuel adjustments — and the processor pays
    rebate on what actually settled. The settlement branch used to read
    `card.amount_charged`, which is stamped by the AUTH event and never updated
    at settlement, so the rebate was computed on the authorized figure while the
    settlement event's own amount sat unused in scope.

    The fallback is the sharper half. It used to be `or card.amount_limit`: the
    card's authorization CEILING, not spend. A settlement webhook that arrived
    without a usable amount, on a card whose auth was also missing, rebated on
    the full limit — a $10,000 card that settled $100 earned a rebate on
    $10,000. With no evidence of what moved, the honest base is zero; the
    returned source says which figure was used so a reconciliation against the
    processor's own statement can tell them apart.
    """
    if settled_amount is not None:
        return settled_amount, "settled"
    if authorized_amount:
        return authorized_amount, "authorized"
    return Decimal("0"), "unknown"


def _resolve_card_config(org: Organization) -> dict:
    """Build card adapter config based on program type.

    - "platform": use platform-level keys from app settings (you earn rebates)
    - "byok": use customer's own keys from org settings (they earn rebates)
    """
    from app.services.card_issuance import _coerce_expiry_days

    org_cards = (org.settings or {}).get("cards", {})
    program_type = org_cards.get("program_type", "platform")
    region = org_cards.get("region", "US")
    expiry_days = _coerce_expiry_days(org_cards.get("default_expiry_days"))

    if program_type == "byok":
        # Customer provided their own keys
        return {
            "provider": org_cards.get("provider", ""),
            "region": region,
            "api_key": org_cards.get("api_key", ""),
            "client_id": org_cards.get("client_id", ""),
            "client_secret": org_cards.get("client_secret", ""),
            "customer_hash_id": org_cards.get("customer_hash_id", ""),
            "wallet_hash_id": org_cards.get("wallet_hash_id", ""),
            # BYOK sandbox is opt-IN: a customer who wires their own real
            # provider keys expects live rails. Defaulting to sandbox silently
            # routed their production key at the sandbox host (invoices paid
            # into a void). Sandbox must be an explicit `"sandbox": true`.
            "sandbox": org_cards.get("sandbox", False),
            "default_expiry_days": expiry_days,
        }
    else:
        # Platform keys. An explicit admin-set `provider` override wins;
        # auto-select by region only when unset. Without this, `platform`
        # mode (the default for every fresh clone's seeded tenants) could
        # never be pointed at `mock` — issuance would always resolve to
        # lithic/nium and, with no platform credential configured locally,
        # silently fail with a live outbound call to the real sandbox host.
        from app.services.card_adapters.dispatcher import get_default_provider

        provider = org_cards.get("provider") or get_default_provider(region)

        if provider == "lithic":
            return {
                "provider": "lithic",
                "region": region,
                "api_key": settings.lithic_api_key,
                "sandbox": settings.lithic_sandbox,
                "default_expiry_days": expiry_days,
            }
        elif provider == "nium":
            return {
                "provider": "nium",
                "region": region,
                "client_id": settings.nium_client_id,
                "client_secret": settings.nium_client_secret,
                "customer_hash_id": settings.nium_customer_hash_id,
                "wallet_hash_id": settings.nium_wallet_hash_id,
                "sandbox": settings.nium_sandbox,
                "default_expiry_days": expiry_days,
            }
        else:
            # e.g. "mock" for local-first testing — no live credentials
            # needed. Any other/unrecognized value is REFUSED by
            # `get_card_adapter` (`UnknownCardProviderError`) rather than
            # resolving to the fixture adapter — see `decisions.md` §29.
            return {
                "provider": provider,
                "region": region,
                "default_expiry_days": expiry_days,
            }


def _require_card_adapter(org: Organization):
    """Resolve the org's card adapter or 409 naming the unregistered provider.

    `get_card_adapter` refuses a NAMED provider it has no adapter for rather
    than substituting `mock`, whose `create_card` reports success with a fixture
    PAN and whose `get_card_details` returns `4242424242424242`
    (`decisions.md` §29). Every route here that reaches a provider funnels
    through this so the refusal is a clean, actionable 409 instead of a 500 — and
    so a settings typo can't quietly mint or reveal fixture cards.

    409 (not 400/422): the request is well-formed; the org's card configuration
    is in a state that cannot service it. The provider name is admin-supplied
    config, not PII, and the exception bounds it to 50 characters.
    """
    import app.services.card_adapters.lithic  # noqa: F401
    import app.services.card_adapters.mock_adapter  # noqa: F401
    import app.services.card_adapters.nium  # noqa: F401
    from app.services.card_adapters import UnknownCardProviderError, get_card_adapter
    from app.services.card_adapters.dispatcher import list_available_providers

    card_config = _resolve_card_config(org)
    try:
        return get_card_adapter(card_config)
    except UnknownCardProviderError as exc:
        logger.warning(
            "[cards] card provider %r has no registered adapter for org %s",
            exc.provider,
            org.id,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{exc.provider}' is not a supported card provider "
                f"(one of: {', '.join(list_available_providers())}). "
                "Fix it in Organization Settings and retry."
            ),
        ) from None


def _card_response(
    card: VirtualCard, invoice: Invoice | None = None, vendor: Vendor | None = None
) -> CardResponse:
    return CardResponse(
        id=str(card.id),
        invoice_id=str(card.invoice_id),
        vendor_id=str(card.vendor_id) if card.vendor_id else None,
        card_provider=card.card_provider,
        last_four=card.last_four,
        amount_limit=card.amount_limit,
        amount_charged=card.amount_charged,
        currency=card.currency,
        status=card.status,
        expires_at=card.expires_at.isoformat() if card.expires_at else None,
        sent_at=card.sent_at.isoformat() if card.sent_at else None,
        charged_at=card.charged_at.isoformat() if card.charged_at else None,
        merchant_name=card.merchant_name,
        decline_reason=card.decline_reason,
        created_at=card.created_at.isoformat() if card.created_at else "",
        vendor_name=invoice.vendor_name if invoice else (vendor.name if vendor else None),
        invoice_number=invoice.invoice_number if invoice else None,
    )


@router.get("", response_model=CardListResponse)
async def list_cards(
    status_filter: str | None = Query(None, alias="status"),
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    query = apply_entity_scope(
        select(VirtualCard, Invoice).outerjoin(Invoice, VirtualCard.invoice_id == Invoice.id),
        VirtualCard,
        entity_id,
    )
    if status_filter:
        statuses = [s.strip() for s in status_filter.split(",")]
        query = query.where(VirtualCard.status.in_(statuses))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0

    query = (
        query.order_by(VirtualCard.created_at.desc(), VirtualCard.id.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    result = await db.execute(query)
    rows = result.all()

    return CardListResponse(
        items=[_card_response(card, inv) for card, inv in rows],
        total=int(total),
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/dashboard", response_model=CardDashboardResponse)
async def card_dashboard(
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    now = datetime.now(UTC)

    # Every money figure in this response is reported under ONE currency code,
    # so each aggregate below counts only rows denominated in it. These were
    # bare cross-currency SUMs presented as a single headline: a programme
    # running USD and EUR cards added the two together, which is not a quantity
    # in either currency and moves silently as the mix changes.
    #
    # `CardRebate` carries no currency column of its own — a rebate's currency
    # is only knowable through the card that earned it — so the rebate rollups
    # join `VirtualCard` rather than guessing.
    #
    # Filtered rather than converted, for the same reason as the discounting
    # rollups: these are historical realised figures and an FX fetch on a
    # dashboard read would make them non-deterministic.
    reporting_currency = resolve_reporting_currency(org.settings)
    _card_ccy = card_currency_sql(reporting_currency)

    # Every aggregate below is entity-scoped, rebates included — `CardRebate` is
    # tenant-scoped (decisions §57) and reaches its entity through the
    # `VirtualCard` join the currency predicate already needs.
    active_q = apply_entity_scope(
        select(func.count(), func.coalesce(func.sum(VirtualCard.amount_limit), 0)).where(
            VirtualCard.status.in_(["created", "sent", "active"]),
            _card_ccy == reporting_currency,
        ),
        VirtualCard,
        entity_id,
    )
    active_result = await db.execute(active_q)
    active_count, active_value = active_result.one()

    excluded_card_count = (
        await db.execute(
            apply_entity_scope(
                select(func.count()).where(
                    VirtualCard.status.in_(["created", "sent", "active"]),
                    _card_ccy != reporting_currency,
                ),
                VirtualCard,
                entity_id,
            )
        )
    ).scalar() or 0

    # Spend this month
    spend_q = apply_entity_scope(
        select(func.coalesce(func.sum(VirtualCard.amount_charged), 0)).where(
            VirtualCard.status.in_(["charged", "completed"]),
            extract("month", VirtualCard.charged_at) == now.month,
            extract("year", VirtualCard.charged_at) == now.year,
            _card_ccy == reporting_currency,
        ),
        VirtualCard,
        entity_id,
    )
    spend_result = await db.execute(spend_q)
    spend_this_month = spend_result.scalar() or 0

    # Rebates this month — split by lifecycle status in one query rather than
    # a blind SUM. A `pending` `CardRebate` is the processor's own ESTIMATE:
    # not yet confirmed by the processor's out-of-band settlement (`POST
    # /rebates/{id}/confirm`) and further still from an actual payout
    # (`/mark-paid`). Blending all three into one "Rebates Earned" figure let
    # 100% of a displayed "earned" total be entirely unconfirmed money that
    # may never materialize. Only `confirmed` + `paid_out` is REALIZED.
    _pending_amt = case((CardRebate.status == "pending", CardRebate.amount), else_=0)
    _confirmed_amt = case((CardRebate.status == "confirmed", CardRebate.amount), else_=0)
    _paid_out_amt = case((CardRebate.status == "paid_out", CardRebate.amount), else_=0)

    # Entity-scoped through `VirtualCard`, the same way `GET /rebates` scopes
    # its list: `CardRebate` carries no `entity_id` of its own, and the join
    # this rollup already needs for the currency predicate is what makes the
    # subsidiary reachable. Without it the dashboard reported entity-scoped
    # card figures beside org-wide rebate figures, so the rebate total could
    # not be reconciled against the rebate list the operator drills into.
    rebate_month_q = apply_entity_scope(
        select(
            func.coalesce(func.sum(_pending_amt), 0),
            func.coalesce(func.sum(_confirmed_amt), 0),
            func.coalesce(func.sum(_paid_out_amt), 0),
        )
        .join(VirtualCard, VirtualCard.id == CardRebate.virtual_card_id)
        .where(
            CardRebate.period == now.strftime("%Y-%m"),
            _card_ccy == reporting_currency,
        ),
        VirtualCard,
        entity_id,
    )
    month_pending, month_confirmed, month_paid_out = (await db.execute(rebate_month_q)).one()
    month_pending = Decimal(str(month_pending or 0))
    month_confirmed = Decimal(str(month_confirmed or 0))
    month_paid_out = Decimal(str(month_paid_out or 0))
    rebate_month_realized = month_confirmed + month_paid_out

    # Rebates YTD. BOUNDED at both ends: `period` is a `YYYY-MM` string, so a
    # bare `>= "{year}-01"` also matches every FUTURE year ("2027-03" sorts
    # above "2026-01"), letting a forward-dated row leak into a
    # year-to-date figure — and `projected_annual` divides that figure by
    # months elapsed, so one such row inflates the projection too.
    rebate_ytd_q = apply_entity_scope(
        select(
            func.coalesce(func.sum(_pending_amt), 0),
            func.coalesce(func.sum(_confirmed_amt), 0),
            func.coalesce(func.sum(_paid_out_amt), 0),
        )
        .join(VirtualCard, VirtualCard.id == CardRebate.virtual_card_id)
        .where(
            CardRebate.period >= f"{now.year}-01",
            CardRebate.period <= now.strftime("%Y-%m"),
            _card_ccy == reporting_currency,
        ),
        VirtualCard,
        entity_id,
    )
    ytd_pending, ytd_confirmed, ytd_paid_out = (await db.execute(rebate_ytd_q)).one()
    ytd_pending = Decimal(str(ytd_pending or 0))
    ytd_confirmed = Decimal(str(ytd_confirmed or 0))
    ytd_paid_out = Decimal(str(ytd_paid_out or 0))
    excluded_rebate_count = (
        await db.execute(
            apply_entity_scope(
                select(func.count())
                .select_from(CardRebate)
                .join(VirtualCard, VirtualCard.id == CardRebate.virtual_card_id)
                .where(
                    CardRebate.period >= f"{now.year}-01",
                    CardRebate.period <= now.strftime("%Y-%m"),
                    _card_ccy != reporting_currency,
                ),
                VirtualCard,
                entity_id,
            )
        )
    ).scalar() or 0
    rebate_ytd_realized = ytd_confirmed + ytd_paid_out

    # Projected annual: (REALIZED YTD / months elapsed) × 12 — never the
    # pending amount, which may be revised or never confirmed at all. In
    # January where realized YTD is short, the per-day rate is too noisy, so
    # we fall back to this month's realized total × 12 if nothing has been
    # confirmed/paid yet. Decimal throughout (money is exact); quantize the
    # projection to cents rather than hopping through float.
    months_elapsed = now.month
    if rebate_ytd_realized:
        projected_annual = rebate_ytd_realized / months_elapsed * 12
    else:
        projected_annual = rebate_month_realized * 12
    projected_annual = projected_annual.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return CardDashboardResponse(
        active_cards=active_count or 0,
        active_cards_value=active_value,
        spend_this_month=spend_this_month,
        rebates_this_month=rebate_month_realized,
        rebates_ytd=rebate_ytd_realized,
        projected_annual_rebates=projected_annual,
        rebates_this_month_by_status=RebateStatusBreakdown(
            pending_total=month_pending,
            confirmed_total=month_confirmed,
            paid_out_total=month_paid_out,
        ),
        rebates_ytd_by_status=RebateStatusBreakdown(
            pending_total=ytd_pending,
            confirmed_total=ytd_confirmed,
            paid_out_total=ytd_paid_out,
        ),
        currency=reporting_currency,
        excluded_card_count=excluded_card_count,
        excluded_rebate_count=excluded_rebate_count,
    )


@router.post("/generate", response_model=CardListResponse, status_code=status.HTTP_201_CREATED)
async def generate_cards(
    body: GenerateCardsRequest,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # `payment.execute`, not a role list: minting a card funds the invoice the
    # same as executing a run does, so an org that withholds the duty from a
    # custom role must be able to withhold this door too. Every system role the
    # role list admitted (admin, ap_manager, cfo) holds it by default.
    user: User = Depends(require_permission(PERM_PAYMENT_EXECUTE)),
    org_id: uuid.UUID = Depends(get_org_id),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Mint a virtual card directly for one or more invoices (outside a
    payment run).

    Scoped to the selected entity (`X-Entity-ID`) like every other money
    route: an invoice outside it is skipped, never minted.

    This is a second entry point into card issuance — the other is the
    ``virtual_card`` leg of ``execute_payment_run`` in ``api/payments.py``.
    Both MUST enforce the same gates a card mint moves real money, so this
    handler reuses the same building blocks the payment-run executor uses
    rather than re-implementing them:

      - ``PAYABLE_INVOICE_STATUSES`` (from ``api/payments``) — the invoice
        must have cleared AP approval. An invoice still in
        new/pending/ready_for_review/rejected/failed is filtered out before
        any card is minted.
      - ``payment_runs.blocked_invoice_ids`` and the vendor's status — an
        unresolved payment-blocking exception, or a vendor that is not
        ``active`` (unverified / inactive / rejected), skips the invoice exactly
        as a run refuses it; so does an applied credit that no longer pairs with
        the invoice, or one covering all of it.
      - ``card_issuance.lock_invoices_for_mint``: each invoice and its vendor
        are row-locked before any of these checks, and the mint commits per
        invoice. Two concurrent requests, or a request and a payment dispatch,
        therefore cannot both pass the checks. The loser waits, then skips an
        invoice that now has a live card or a live payment. An invoice whose
        lock wait passes ``payment_invoice_lock_timeout_ms`` is skipped having
        minted nothing; if nothing at all was minted, the answer is 409.
      - The card is minted for the invoice net of applied credit memos, never
        the gross, because the card is spendable up to its limit. An accepted
        early-payment discount is NOT taken here — only a booked payment takes
        one (``payment_runs.payable_amounts``).
      - ``check_payment_compliance`` — sanctions/KYC/AML screening. Card
        issuance moves money just like an ACH/wire, so a blocked or
        sanctioned vendor must not receive a card; a hold/refuse verdict
        skips the invoice (mirrors the payment-run leg).
      - ``issue_card_for_invoice`` — the single adapter-dispatch + VirtualCard
        construction routine, so provider selection / expiry / entity
        propagation can't drift between the two entry points.
      - ``dispatch_audit`` — every other card-lifecycle event in this module
        (cancel, PAN reveal, webhook charge/settle) writes an append-only
        audit row; a direct mint must too (SOX trail).
    """
    org_cards = (org.settings or {}).get("cards", {})
    if not org_cards.get("enabled"):
        raise HTTPException(
            status_code=400,
            detail="Virtual cards are not enabled. Configure in Organization Settings.",
        )
    # A minted card is spendable — issuing one moves money exactly as a run's
    # card leg does, and that leg is already refused on a record-only tenant
    # (`api/payments._require_payment_adapter`). This door must refuse too, or
    # "FeohLedger moves no money" (issue #517, decisions §251) has an exception.
    from app.api.payments import refuse_record_only

    refuse_record_only(org)

    from app.api.payments import PAYABLE_INVOICE_STATUSES
    from app.config import settings as app_settings

    # Refuse the whole batch up front when the configured provider names no
    # registered adapter. `issue_card_for_invoice` would refuse each invoice
    # individually anyway, but the loop's per-invoice `continue` reports that as
    # "0 cards generated" — indistinguishable from "nothing was eligible", which
    # is how a settings typo stayed invisible. Naming the bad value here is the
    # same call `/organization/test-erp` makes (`decisions.md` §29); it is admin
    # config, never a credential.
    _require_card_adapter(org)

    # Only invoices that have cleared AP approval are eligible.
    # PAYABLE_INVOICE_STATUSES is the single source of truth shared with the
    # payment queue / run builder, so a card can't be minted against an
    # unapproved invoice on any path.
    #
    # Each invoice is locked, checked, minted and COMMITTED on its own. The lock
    # (its vendor FOR SHARE, then the invoice FOR NO KEY UPDATE) comes before any
    # check reads the row. Without it, two concurrent generates both saw no live
    # card; the loser minted a second real card at the provider under a fresh
    # idempotency key, and the unique index refused to record it. A payment
    # dispatch holding the invoice could also pay it while this minted a card.
    # Every check after the lock is a fresh statement, so a request that waited
    # sees what the winner committed. The wait is bounded (decisions §233) and
    # comes before any provider call, so a timeout mints nothing. Committing per
    # invoice holds the locks for one provider call, not the whole batch, and
    # makes each card durable as soon as the provider has issued it. See
    # `card_issuance.lock_invoices_for_mint`, decisions §265.
    from app.api.payments import INVOICE_LOCKED_DETAIL
    from app.services.card_issuance import lock_invoices_for_mint
    from app.utils.db_locks import LockWaitTimeout

    cards: list[VirtualCard] = []
    lock_refused = 0
    for invoice_id in sorted({uuid.UUID(i) for i in body.invoice_ids}):
        try:
            locked = await lock_invoices_for_mint(
                db,
                [invoice_id],
                payable_statuses=PAYABLE_INVOICE_STATUSES,
                timeout_ms=app_settings.payment_invoice_lock_timeout_ms,
                entity_id=entity_id,
            )
        except LockWaitTimeout:
            lock_refused += 1
            continue
        if not locked:
            await db.commit()  # not payable, out of scope, or vendor moved
            continue
        card = await _mint_one_locked(
            db,
            locked[0],
            org=org,
            org_id=org_id,
            user=user,
            app_settings=app_settings,
        )
        # Releases this invoice's locks before the next one is taken, and makes
        # a card the provider already issued durable before anything else runs.
        await db.commit()
        if card is not None:
            cards.append(card)

    # Nothing minted, and at least one invoice could not be locked in time: say
    # so, rather than report "nothing was eligible". Retry-safe (no provider
    # call was made for it). When some cards WERE minted they are committed and
    # returned; a locked invoice is then absent like any other skip, and a retry
    # picks it up.
    if not cards and lock_refused:
        raise HTTPException(status_code=409, detail=INVOICE_LOCKED_DETAIL)

    return CardListResponse(
        items=[_card_response(c) for c in cards],
        total=len(cards),
    )


async def _mint_one_locked(
    db: AsyncSession,
    inv: Invoice,
    *,
    org: Organization,
    org_id: uuid.UUID,
    user: User,
    app_settings,
) -> VirtualCard | None:
    """Run every gate on one invoice the caller holds locked, then mint.

    Returns the persisted card, or ``None`` when the invoice is skipped. The
    gates are the ones a payment run enforces, through the same shared
    predicates, so the two entry points can't drift. Imports are lazy, as in
    ``generate_cards``, so a test that patches a collaborator at its source
    module is honoured.
    """
    from app.services.applied_credit_integrity import applied_credit_conflicts
    from app.services.audit_dispatch import dispatch_audit
    from app.services.card_issuance import (
        issue_card_for_invoice,
        live_card_invoice_ids,
        persist_card,
    )
    from app.services.compliance import check_payment_compliance
    from app.services.payment_runs import (
        blocked_invoice_ids,
        inactive_vendor_statuses,
        live_payment_invoices,
        net_payable_amounts,
    )

    # Idempotency: an invoice with a LIVE (non-cancelled) card is skipped, so a
    # retried request (network timeout, double-click) or a concurrent one that
    # waited on the lock doesn't mint a second provider card. The partial unique
    # index uq_virtual_cards_one_live_per_invoice stays the backstop for a writer
    # that does not take the lock.
    if inv.id in await live_card_invoice_ids(db, [inv.id]):
        return None
    # An unresolved payment-blocking exception, or a vendor that is not verified
    # and active: a minted card is spendable the moment it exists, so it must
    # not slip past what `POST /api/payments/runs` refuses.
    if inv.id in await blocked_invoice_ids(db, [inv.id]):
        return None
    if inv.id in await inactive_vendor_statuses(db, [inv]):
        return None
    # An invoice already being paid on another rail. `payment_scheduled` is a
    # payable status, so an invoice whose ACH has settled but is not yet `paid`
    # still passes the status filter. Without this check a card was minted on
    # top of the wire, and a generate that waited out a dispatch's lock would
    # have minted onto the payment it just watched land. The run builder refuses
    # a live payment the same way (`uq_payments_one_live_per_invoice`).
    if inv.id in await live_payment_invoices(db, [inv.id]):
        return None
    # The card is minted for the invoice net of applied credit memos: a card is
    # spendable up to its limit, so one minted at the gross overpays the credit.
    # It deliberately does NOT take an accepted early-payment discount: a card
    # minted here is not a booked payment, so nothing would record the discount
    # (no `discount_offer.applied` row, no capture) and the card would outlive
    # the offer's deadline. Discounts flow only through booked payments; a run
    # that books one refuses to converge on this card
    # (`card_issuance.card_settlement_block` → `card_limit_exceeds_payment`).
    # A credit that no longer pairs with the invoice makes the net meaningless
    # (decisions §214); a fully-credited invoice owes nothing.
    if inv.id in await applied_credit_conflicts(db, [inv]):
        return None
    net = (await net_payable_amounts(db, [inv]))[inv.id]
    if net <= 0:
        return None

    # Compliance gate: mirrors execute_payment_run's virtual_card leg. No
    # screenable vendor (no vendor_id, or the row was deleted) → skip rather
    # than mint unscreened; a refuse/hold verdict also skips.
    if not inv.vendor_id:
        return None
    vendor = (
        await db.execute(select(Vendor).where(Vendor.id == inv.vendor_id))
    ).scalar_one_or_none()
    if vendor is None:
        return None
    decision = await check_payment_compliance(
        db,
        vendor=vendor,
        # In the invoice's own currency; the KYC threshold is a home-currency
        # figure, so hand the gate the currency and let it fail closed when the
        # two can't be compared.
        payment_amount=net,
        payment_currency=inv.currency,
        payment_method="virtual_card",
        org_settings=org.settings or {},
        organization_id=org_id,
        correlation_id=inv.correlation_id,
    )
    if decision.verdict != "allow":
        return None  # blocked/held vendor: skip, don't block the batch

    issue = await issue_card_for_invoice(
        db=db,
        invoice=inv,
        organization_id=org_id,
        org_settings=org.settings or {},
        app_settings=app_settings,
        amount=net,
    )
    if not issue.success or issue.card is None:
        return None  # skip failed cards, don't block the batch

    card = issue.card
    # Savepoint-guarded flush (shared with the payment-run card leg), so a
    # duplicate caught by the partial unique index skips just this card instead
    # of poisoning the transaction.
    if not await persist_card(db, card):
        return None

    # SOX trail: every other card-lifecycle event in this module audits
    # (cancel, PAN reveal, webhook charge/settle), so a direct mint must too.
    await dispatch_audit(
        db,
        correlation_id=inv.correlation_id or uuid.uuid4(),
        organization_id=org_id,
        actor_id=user.id,
        action="card.generated",
        entity_type="virtual_card",
        entity_id=card.id,
        details={
            "invoice_id": str(inv.id),
            "last_four": card.last_four,
            "amount_limit": str(card.amount_limit),
        },
    )
    return card


@router.get("/{card_id}/details", response_model=CardDetailsResponse)
async def get_card_details(
    card_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Retrieve full card details. Restricted to admin/ap_manager/cfo. Audit-logged."""
    await ensure_in_entity_scope(db, VirtualCard, card_id, entity_id, detail="Card not found")
    result = await db.execute(select(VirtualCard).where(VirtualCard.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    # Audit log the access
    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=card.correlation_id or uuid.uuid4(),
        organization_id=card.organization_id,
        actor_id=user.id,
        action="card.details_viewed",
        entity_type="virtual_card",
        entity_id=card.id,
        details={"last_four": card.last_four},
    )

    # Refuses (409) when the org names an unregistered provider — otherwise the
    # mock adapter would hand this route the fixture PAN 4242424242424242 and
    # the caller would have no way to tell it apart from the real thing.
    adapter = _require_card_adapter(org)
    details = await adapter.get_card_details(card.provider_card_id)

    return CardDetailsResponse(
        card_number=details.card_number,
        exp_month=details.exp_month,
        exp_year=details.exp_year,
        cvv=details.cvv,
    )


@router.post("/{card_id}/cancel")
async def cancel_card(
    card_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    # The gate of the operation this DUPLICATES, not the card router's own.
    # See the docstring — this is the one migrated route that deliberately does
    # NOT reproduce the prior system-role matrix.
    user: User = Depends(require_permission(PERM_PAYMENT_VOID)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """Cancel an unused virtual card at the provider, then in our DB.

    **`payment.void`, and this NARROWS `ap_manager` on purpose.** Every other
    route migrated from `require_roles` to `require_permission` reproduces the
    prior admin/ap_manager/cfo matrix exactly for the four system roles. This one
    cannot, because the mismatch IS the defect: the gate was
    `require_roles(ADMIN, AP_MANAGER, CFO)` while the two operations that also
    close a card — `POST /api/payments/{id}/void` and its completion
    `POST /api/payments/{id}/void/retry-card-cancel` — both gate on
    `payment.void`, which the default `ap_manager` does not hold. So an
    `ap_manager` could not reverse a card payment but could kill the card behind
    it here, through the wider of two doors onto the same effect. `docs/decisions.md`
    §132 already recorded the rule for the sibling route ("not the card router's
    bare roles, which would hand an `ap_manager` in a duty-split org the half of
    the reversal the org withheld"); this applies it to the door that was left
    open. Net effect on the four system roles: `admin` and `cfo` keep it,
    `ap_manager` loses it, `ap_clerk` never had it.

    Nothing in the UI regresses — the route is deliberately unwired
    (`docs/decisions.md` §96): a standalone Cancel is reachable on a LIVE payment,
    where it would kill the card while that payment and its invoice still claim
    money is in flight. The supported remedy sits ON the void.
    """
    await ensure_in_entity_scope(db, VirtualCard, card_id, entity_id, detail="Card not found")
    result = await db.execute(select(VirtualCard).where(VirtualCard.id == card_id))
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if card.status in ("charged", "completed", "cancelled"):
        raise HTTPException(status_code=409, detail=f"Cannot cancel card in '{card.status}' status")

    # Refuses (409) when the org names an unregistered provider — the mock's
    # `cancel_card` returns True unconditionally, so the row would be marked
    # cancelled while the real card stayed live and chargeable.
    adapter = _require_card_adapter(org)
    # Cancel at the provider FIRST, then reflect it in the DB — never the other
    # way round. The fail-safe direction is "dead at the provider, maybe stale
    # in the DB"; the dangerous direction is a card the AP team believes is
    # cancelled while it is still chargeable at the provider. So we only mark
    # the row cancelled once the provider CONFIRMS the close.
    try:
        cancelled_ok = await adapter.cancel_card(card.provider_card_id)
    except Exception as exc:  # noqa: BLE001
        # Provider unreachable — its state is unknown. Don't record an
        # unverified cancel; let the client retry. (PII guard: type only.)
        import logging

        logging.getLogger(__name__).warning(
            "[cards] provider cancel raised for card %s: %s", card.id, exc.__class__.__name__
        )
        raise HTTPException(
            status_code=502, detail="Card provider is unavailable; please retry."
        ) from None
    if not cancelled_ok:
        raise HTTPException(
            status_code=502,
            detail="Card provider did not confirm cancellation; please retry.",
        )
    prior_status = card.status
    card.status = "cancelled"

    # Cancelling voids an issued card before it can be charged — a card
    # lifecycle state change must leave an append-only audit row (project
    # invariant: status transitions write audit). PII-free: only the
    # last_four + the from/to status, never the PAN.
    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=card.correlation_id or uuid.uuid4(),
        organization_id=card.organization_id,
        actor_id=user.id,
        action="card.cancelled",
        entity_type="virtual_card",
        entity_id=card.id,
        details={"last_four": card.last_four, "from": prior_status, "to": "cancelled"},
    )

    await db.commit()

    return {"success": True, "message": "Card cancelled"}


@router.post("/webhook/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def card_webhook(provider: str, request: Request):
    """Handle charge/settlement webhooks from card providers.

    Authenticated by HMAC over the raw body. The signing secret is
    looked up off the tenant whose card the event references — we
    can't pick the right secret without first identifying the
    tenant, so the flow is:

      1. Parse the body for `card_token` + `event_id`
      2. Find the owning tenant by `card_token`
      3. Verify HMAC against that tenant's `cards.webhook_signing_secret`
      4. Dedupe by event id (cross-tenant key) so re-delivery is a
         silent no-op
      5. Apply the state change inside a row-locked transaction

    Bad signatures, unknown card tokens, missing event ids — every
    failure returns 204 silently. Leaking the difference would help
    an attacker probe for valid tokens.
    """
    # Bound the body BEFORE buffering it. The HMAC check below can't run
    # until the owning tenant is identified from the parsed body, so an
    # unauthenticated attacker could otherwise POST an arbitrarily large
    # payload and have it read fully into memory before anything rejects it
    # (memory-exhaustion DoS on a public route). Reject on the declared
    # Content-Length when present, and re-check the actual read in case the
    # header lied / was absent (chunked). Provider settlement payloads are a
    # few KB; cap defaults to a few MB.
    max_bytes = settings.card_webhook_max_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                logger.warning("Card webhook rejected: body exceeds size cap")
                return
        except ValueError:
            logger.warning("Card webhook rejected: invalid content-length")
            return

    raw_body = await request.body()
    if len(raw_body) > max_bytes:
        logger.warning("Card webhook rejected: body exceeds size cap")
        return
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return  # malformed JSON → silent 204

    # Normalize by provider
    if provider == "lithic":
        card_token = body.get("card_token") or body.get("card", {}).get("token")
        event_type = body.get("type", "")
        event_id = body.get("event_id") or body.get("token") or body.get("id")
        amount = body.get("amount", 0)
        merchant = body.get("merchant", {}).get("descriptor")
    elif provider == "nium":
        card_token = body.get("cardHashId")
        event_type = body.get("eventType", "")
        event_id = body.get("webhookId") or body.get("eventId") or body.get("id")
        amount = body.get("amount", 0)
        merchant = body.get("merchantName")
    else:
        return

    if not card_token:
        return

    # Find the card across tenant DBs. The lookup is fail-soft —
    # if the card isn't anywhere, we return 204 silently rather than
    # confirming "no such card" (enumeration vector).
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import control_session_factory, get_tenant_engine
    from app.services.webhook_security import (
        extract_signature_header,
        is_event_already_processed,
        release_event_claim,
        verify_hmac_sha256,
    )

    async with control_session_factory() as ctrl_db:
        orgs_result = await ctrl_db.execute(select(Organization))
        orgs = orgs_result.scalars().all()

    for org_obj in orgs:
        engine = get_tenant_engine(org_obj.db_name)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            claimed_event: str | None = None
            try:
                # Row-lock the card so two concurrent deliveries of events for
                # the SAME card serialize at the DB layer (one waits for the
                # other's commit/rollback) — the Redis dedup alone can't order
                # them if both slip through the NX gap under load.
                # `card_provider` is filtered too — defense-in-depth. The two
                # providers' opaque `provider_card_id` values are independently
                # generated so a real cross-provider collision is negligible,
                # but the URL's {provider} segment is otherwise used only to
                # pick the field-normalization branch below, never as part of
                # the lookup itself.
                result = await db.execute(
                    select(VirtualCard)
                    .where(
                        VirtualCard.provider_card_id == card_token,
                        VirtualCard.card_provider == provider,
                    )
                    .with_for_update()
                )
                card = result.scalar_one_or_none()
                if not card:
                    continue

                # We've identified the owning tenant. Verify HMAC
                # against that tenant's signing secret before doing
                # anything else.
                card_config = (org_obj.settings or {}).get("cards") or {}
                signing_secret = card_config.get("webhook_signing_secret", "")
                provided_sig = extract_signature_header(
                    dict(request.headers),
                    "Webhook-Signature",
                    "X-Webhook-Signature",
                    "X-Signature",
                )
                if not verify_hmac_sha256(signing_secret, raw_body, provided_sig):
                    return  # silent 204 on bad / missing signature

                # Dedup: if this event id has been processed already
                # (across any tenant), short-circuit. Same response
                # as first delivery so the provider doesn't retry.
                if await is_event_already_processed(provider, str(event_id or "")):
                    return
                # Track the claim so we can release it if the commit below
                # fails — the Redis claim is only durable AFTER the DB change
                # commits; otherwise a rolled-back txn would strand the event
                # id as "processed" and the retry would be deduped away.
                claimed_event = str(event_id) if event_id else None

                # Update card based on event (declines / reversals excluded).
                is_auth, is_settled = _classify_card_event(event_type)

                from app.services.audit_dispatch import dispatch_audit

                if is_auth and card.status in ("created", "sent", "active"):
                    prior_status = card.status
                    card.status = "charged"
                    card.amount_charged = _normalize_charge_amount(
                        provider, amount, card.amount_limit, card.currency
                    )
                    card.charged_at = datetime.now(UTC)
                    card.merchant_name = merchant
                    # A charge is a money-state transition — leave an
                    # append-only audit row. Amount serialises as a string
                    # Decimal (never float); no PAN ever enters the trail.
                    await dispatch_audit(
                        db,
                        correlation_id=card.correlation_id or uuid.uuid4(),
                        organization_id=card.organization_id,
                        actor_id=None,
                        action="card.charged",
                        entity_type="virtual_card",
                        entity_id=card.id,
                        details={
                            "last_four": card.last_four,
                            "from": prior_status,
                            "to": "charged",
                            "amount_charged": str(card.amount_charged),
                        },
                    )
                elif is_settled and card.status == "charged":
                    card.status = "completed"
                    # The SETTLED figure is the rebate base, not the authorized
                    # one. A card network's settlement routinely differs from the
                    # authorization it clears (partial capture, tips, fuel
                    # adjustments), and the processor pays rebate on what
                    # actually settled — so basing it on the auth over- or
                    # under-states what we are owed. This branch read
                    # `card.amount_charged`, stamped by the AUTH event above and
                    # never updated at settlement, while the settlement event's
                    # own `amount` sat unused in scope.
                    settled_amount = _normalize_charge_amount(provider, amount, None, card.currency)
                    if settled_amount is not None:
                        # Persist it: `amount_charged` is what the card detail,
                        # the spend rollups and the corporate-card feed all read,
                        # and after settlement the settled figure is the true one.
                        card.amount_charged = settled_amount

                    # Create rebate at the org's negotiated rate (not a hardcoded
                    # 1%), quantized to cents — the rate field was documented on
                    # settings.cards but never read, so every org earned 1%.
                    rebate_rate = _resolve_rebate_rate(card_config)
                    # Never the card's limit — see `resolve_rebate_base`.
                    _rebate_base, rebate_base_source = resolve_rebate_base(
                        settled_amount, card.amount_charged
                    )
                    rebate_amount = (_rebate_base * rebate_rate).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    rebate = CardRebate(
                        virtual_card_id=card.id,
                        amount=rebate_amount,
                        rate=rebate_rate,
                        status="pending",
                        period=datetime.now(UTC).strftime("%Y-%m"),
                        organization_id=card.organization_id,
                    )
                    # One rebate per card is enforced by the unique index
                    # uq_card_rebates_virtual_card (migration 0069). Under a race
                    # / Redis-outage a second settlement could reach here; insert
                    # inside a savepoint so a duplicate is silently skipped WITHOUT
                    # aborting the card completion + audit row (which must still
                    # land — the money-state transition is the point of the event).
                    rebate_created = True
                    try:
                        async with db.begin_nested():
                            db.add(rebate)
                            await db.flush()
                    except IntegrityError:
                        rebate_created = False
                    await dispatch_audit(
                        db,
                        correlation_id=card.correlation_id or uuid.uuid4(),
                        organization_id=card.organization_id,
                        actor_id=None,
                        action="card.settled",
                        entity_type="virtual_card",
                        entity_id=card.id,
                        details={
                            "last_four": card.last_four,
                            "from": "charged",
                            "to": "completed",
                            "rebate_amount": str(rebate_amount),
                            "rebate_rate": str(rebate_rate),
                            "rebate_created": rebate_created,
                            # Which figure the rebate was computed on, so a
                            # reconciliation against the processor's own
                            # statement can tell a settled base from an
                            # authorized one without re-deriving it.
                            "rebate_base": str(_rebate_base),
                            "rebate_base_source": rebate_base_source,
                        },
                    )

                await db.commit()
                return

            except Exception:
                await db.rollback()
                # The dedup claim guards a side effect that just rolled back —
                # release it so the provider's retry can reprocess (otherwise
                # the rebate / charge is dropped for the full TTL window).
                if claimed_event is not None:
                    await release_event_claim(provider, claimed_event)
                continue

    return


def _rebate_list_filters(query, *, period: str | None, entity_id: uuid.UUID | None):
    """The predicates BOTH the rebate page and its whole-set rollup apply.

    One builder, two callers — the shape `tests/test_whole_set_kpi_rollups.py`
    pins on every other list-plus-KPI surface — so the total under the table can
    never describe a different set than the rows in it once the table is only
    ever showing a page of them (`docs/decisions.md` §79, §82).

    `CardRebate` carries no `entity_id` of its own, so the entity scope reaches
    it through the `VirtualCard` join every caller already needs for the
    currency. The join itself is the caller's, not this builder's, because the
    count/rollup/page queries each select from a different shape.
    """
    query = apply_entity_scope(query, VirtualCard, entity_id)
    if period:
        query = query.where(CardRebate.period == period)
    return query


@router.get("/rebates", response_model=RebateListResponse)
async def list_rebates(
    period: str | None = None,
    pagination: PaginationParams = Depends(pagination_params),
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
    entity_id: uuid.UUID | None = Depends(get_entity_id),
):
    """A page of the tenant's card rebates, newest first, plus the whole-set
    money total under them.

    Paginated on the canonical `page` / `page_size` contract (`api/pagination`)
    like every other list here: the table this backs grows monotonically —
    one rebate per settled card, forever — and returning all of them made the
    response size a function of how long the tenant had been running.

    `total` is therefore the ROW COUNT of the whole filtered set (what the
    Load-More control counts against); `total_amount` is the summed money, also
    over the whole filtered set and never over the loaded page.
    """
    # Denominated like the dashboard's rebate figures: `card_rebates` carries no
    # currency column, so the join that scopes the entity also supplies the
    # currency (`card_currency_sql` owns that expression). It denominates the
    # total AND — since this change — each row, so a mixed-currency programme
    # renders every amount under its own code instead of bare.
    reporting_currency = resolve_reporting_currency(org.settings)
    _rebate_ccy = card_currency_sql(reporting_currency)

    def _joined(selectable):
        return _rebate_list_filters(
            selectable.join(VirtualCard, CardRebate.virtual_card_id == VirtualCard.id),
            period=period,
            entity_id=entity_id,
        )

    total = (
        await db.execute(
            select(func.count()).select_from(_joined(select(CardRebate.id)).subquery())
        )
    ).scalar() or 0

    # `created_at` alone is not a total order — two rebates minted by the same
    # settlement batch share a timestamp, and a tie split across a page boundary
    # drops one row and repeats another. `id` is the tiebreak.
    rows = (
        await db.execute(
            _joined(select(CardRebate, _rebate_ccy.label("card_currency")))
            .order_by(CardRebate.created_at.desc(), CardRebate.id.desc())
            .offset(pagination.offset)
            .limit(pagination.limit)
        )
    ).all()

    total_q = _joined(
        select(
            func.coalesce(
                func.sum(case((_rebate_ccy == reporting_currency, CardRebate.amount))), 0
            ),
            func.count(case((_rebate_ccy != reporting_currency, CardRebate.id))),
        ).select_from(CardRebate)
    )
    total_amount, excluded_rebate_count = (await db.execute(total_q)).one()

    return RebateListResponse(
        items=[_rebate_response(r, currency) for r, currency in rows],
        total=int(total),
        page=pagination.page,
        page_size=pagination.page_size,
        total_amount=total_amount or 0,
        currency=reporting_currency,
        excluded_rebate_count=int(excluded_rebate_count or 0),
    )


async def _get_org_rebate(
    db: AsyncSession, rebate_id: uuid.UUID, reporting_currency: str
) -> tuple[CardRebate, str]:
    """A rebate plus the currency it is denominated in, scoped to the caller's
    tenant DB (implicit — `db` is already the tenant session). No entity filter:
    a rebate confirmation is an org-level bookkeeping action, not a
    per-subsidiary read.

    The `VirtualCard` join is not optional garnish — `card_rebates` has no
    currency column, so it is the only way the response can state one, and
    `RebateResponse.currency` is required. `virtual_card_id` is a NOT NULL FK,
    so the join never loses a row that exists."""
    result = await db.execute(
        select(CardRebate, card_currency_sql(reporting_currency))
        .join(VirtualCard, CardRebate.virtual_card_id == VirtualCard.id)
        .where(CardRebate.id == rebate_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Rebate not found")
    return row[0], row[1]


def _rebate_response(r: CardRebate, currency: str) -> RebateResponse:
    return RebateResponse(
        id=str(r.id),
        virtual_card_id=str(r.virtual_card_id),
        amount=r.amount,
        rate=r.rate,
        currency=currency,
        status=r.status,
        period=r.period,
        created_at=r.created_at.isoformat() if r.created_at else "",
    )


@router.post("/rebates/{rebate_id}/confirm", response_model=RebateResponse)
async def confirm_rebate(
    rebate_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    """Advance a rebate `pending` → `confirmed` — the processor's monthly
    statement (or equivalent manual reconciliation) confirmed the rebate
    actually accrued. `CardRebate.status` never transitioned automatically —
    real rebate reporting from Lithic/Nium arrives out-of-band (a periodic
    statement, not a webhook event we already ingest), so this is a
    human-driven confirmation, not something the card webhook can do for us."""
    reporting_currency = resolve_reporting_currency(org.settings)
    rebate, currency = await _get_org_rebate(db, rebate_id, reporting_currency)
    if rebate.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"Cannot confirm a rebate in '{rebate.status}' status"
        )
    rebate.status = "confirmed"

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="card_rebate.confirmed",
        entity_type="card_rebate",
        entity_id=rebate.id,
        details={"amount": str(rebate.amount), "from": "pending", "to": "confirmed"},
    )
    await db.commit()
    await db.refresh(rebate)
    return _rebate_response(rebate, currency)


@router.post("/rebates/{rebate_id}/mark-paid", response_model=RebateResponse)
async def mark_rebate_paid(
    rebate_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    org: Organization = Depends(get_tenant),
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER, ROLE_CFO)),
):
    """Advance a rebate `confirmed` → `paid_out` once the processor's payout
    actually lands (e.g. as a line on the org's own bank statement). Requires
    `confirmed` first — a rebate can't be recorded paid before it was
    confirmed to exist."""
    reporting_currency = resolve_reporting_currency(org.settings)
    rebate, currency = await _get_org_rebate(db, rebate_id, reporting_currency)
    if rebate.status != "confirmed":
        raise HTTPException(
            status_code=409, detail=f"Cannot mark paid a rebate in '{rebate.status}' status"
        )
    rebate.status = "paid_out"

    from app.services.audit_dispatch import dispatch_audit

    await dispatch_audit(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=org.id,
        actor_id=user.id,
        action="card_rebate.paid_out",
        entity_type="card_rebate",
        entity_id=rebate.id,
        details={"amount": str(rebate.amount), "from": "confirmed", "to": "paid_out"},
    )
    await db.commit()
    await db.refresh(rebate)
    return _rebate_response(rebate, currency)
