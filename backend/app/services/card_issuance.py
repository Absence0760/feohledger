"""Helpers for issuing virtual cards.

Used by both the explicit `/api/cards/generate` endpoint and the
payment-run executor when a row's method is `virtual_card`. Keeping the
provider-call shape in one place means a future card adapter only has to
change the inputs in one spot.

This module is sync-call-friendly: every helper either returns a row or
raises. Callers are responsible for awaiting `db.flush` / `db.commit`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invoice import Invoice
from app.models.vendor import Vendor
from app.models.virtual_card import VirtualCard

logger = logging.getLogger(__name__)

# Fixed namespace for card-issuance idempotency keys. Constant by design: the
# key must reproduce byte-for-byte across retries, processes and deploys, so it
# can never be a fresh uuid4 nor derive from anything transient.
CARD_IDEMPOTENCY_NAMESPACE = uuid.UUID("2b0a6d5e-1f43-4a7c-9d1e-6c4b8f2a3d90")


def build_card_idempotency_key(
    *,
    invoice_id,
    correlation_id=None,
    reissue_seq: int = 0,
) -> str:
    """Deterministic provider idempotency key for ONE logical card issuance.

    Card issuance mints a real, spendable card, so it needs the same
    idempotency discipline as a payment (project invariant: *idempotency on
    writes that move money*). The DB index
    ``uq_virtual_cards_one_live_per_invoice`` only catches duplicates that made
    it into OUR database — if httpx times out *after* Lithic/Nium provisioned
    the card, no row is written, and an unkeyed retry mints a SECOND live card
    while the first is orphaned and ungoverned. A stable key closes that hole:
    the provider replays the original response instead of issuing again.

    Stability inputs, all durable:

    - ``correlation_id`` (falling back to ``invoice_id`` when an invoice
      predates correlation ids) — anchors the key to the payable, not to the
      attempt. Never a fresh uuid4.
    - ``reissue_seq`` — how many ``VirtualCard`` rows the invoice already has.
      A timed-out attempt persists nothing, so a retry recomputes the SAME
      sequence and therefore the same key. A deliberate cancel-then-reissue
      *does* leave a row behind, so it advances the sequence and gets a fresh
      key — without this, the provider would replay the original (now closed)
      card inside its key-retention window and the vendor would receive a dead
      card.

    Returned as a UUID string because that is the strictest provider
    requirement (Lithic rejects a non-UUID ``Idempotency-Key``); Nium's
    ``x-request-id`` accepts it happily.
    """
    anchor = str(correlation_id) if correlation_id else str(invoice_id)
    return str(uuid.uuid5(CARD_IDEMPOTENCY_NAMESPACE, f"virtual_card:{anchor}:{reissue_seq}"))


async def _reissue_sequence(db: AsyncSession, invoice_id) -> int:
    """Count of cards already persisted for this invoice (incl. cancelled).

    See ``build_card_idempotency_key`` for why this is the right
    discriminator. Deliberately NOT wrapped in a swallow-and-continue: if this
    read fails the session is already broken, and minting a provider card on a
    transaction that cannot persist the resulting row is precisely the orphan
    this whole change exists to prevent. Fail before the money moves.
    """
    result = await db.execute(
        select(func.count()).select_from(VirtualCard).where(VirtualCard.invoice_id == invoice_id)
    )
    return int(result.scalar_one() or 0)


# Statuses meaning the card has been presented to the network — funds are
# committed or already moved. `POST /api/cards/{id}/cancel` refuses these (you
# cannot un-spend a card), so a spent card occupies its invoice's live-card slot
# permanently. Mirrors the spend split in `api/cards.py`'s dashboard queries.
CARD_SPENT_STATUSES = frozenset({"charged", "completed"})


async def find_live_card_for_invoice(db: AsyncSession, invoice_id) -> VirtualCard | None:
    """The card currently occupying this invoice's live-card slot, if any.

    The predicate is deliberately IDENTICAL to
    ``uq_virtual_cards_one_live_per_invoice`` (``status <> 'cancelled'``),
    because this is the pre-check FOR that index. Narrowing it — e.g. to exclude
    spent cards — would make the pre-check miss a row the index still counts, so
    the caller would go on to mint a real provider card that the index then
    refuses to persist: an orphaned, spendable card. Whether the occupying card
    is a valid settlement target is a SEPARATE question — see
    ``card_settlement_block``.

    Used as the cheap pre-check before the provider call, and again after a lost
    insert race to find the card the winner persisted.
    """
    result = await db.execute(
        select(VirtualCard)
        .where(VirtualCard.invoice_id == invoice_id, VirtualCard.status != "cancelled")
        .limit(1)
    )
    return result.scalar_one_or_none()


async def live_card_invoice_ids(db: AsyncSession, invoice_ids) -> set:
    """Which of ``invoice_ids`` currently hold a live card — the batch form of
    :func:`find_live_card_for_invoice`, and the same predicate
    (``status <> 'cancelled'``, i.e. ``uq_virtual_cards_one_live_per_invoice``).

    A live card is a **claim on its invoice**: it is bearer-spendable for the
    full amount. ``POST /api/cards/generate`` mints one without booking any
    ``Payment``, so nothing that keys on payment rows —
    ``uq_payments_one_live_per_invoice``, ``_live_payment_invoice_numbers``, the
    payment queue's `completed`-payment filter — could see that claim, and the
    invoice stayed fully payable by ACH. The vendor held a card for the face
    amount *and* received a wire.

    Lives here, beside the slot predicate it shares, so the money paths in
    ``services/payment_runs`` and ``api/payments`` cannot drift from the
    issuance path's idea of what occupies the slot.
    """
    ids = list(invoice_ids)
    if not ids:
        return set()
    rows = await db.execute(
        select(VirtualCard.invoice_id).where(
            VirtualCard.invoice_id.in_(ids),
            VirtualCard.status != "cancelled",
        )
    )
    return {i for i in rows.scalars().all() if i is not None}


async def lock_invoices_for_mint(
    db: AsyncSession,
    invoice_ids,
    *,
    payable_statuses,
    timeout_ms: int,
    entity_id: uuid.UUID | None = None,
) -> list[Invoice]:
    """Lock the vendors and the payable invoices a direct mint will check.

    What this serialises against: another mint, a payment dispatch, and any
    invoice status writer (all of which take a conflicting invoice lock); and
    any vendor UPDATE, which covers a status change, a block, a bank-change
    approval and a rescreen. What it does NOT serialise: inserts that point at
    the invoice, such as a newly raised payment-blocking exception, because an
    FK insert takes only ``FOR KEY SHARE``. Dispatch has the same exposure.

    ``POST /api/cards/generate`` reads the invoice's status, its live card, its
    live payment, its blocking exceptions and its vendor's status, then calls
    the card provider and inserts the card. Read unlocked, two things went wrong
    in that window:

    - **Two concurrent generates both minted.** Both saw no live card. The first
      committed; the second then read ``reissue_seq = 1`` — the winner's row was
      now visible — so it sent a FRESH provider idempotency key, the provider
      issued a second real card, and the unique index refused only our row: a
      live, spendable card at the provider that nothing in FeohLedger governs.
    - **A payment dispatch and a generate both paid.** Dispatch holds the
      invoice ``FOR NO KEY UPDATE`` and refuses an invoice with a live card, but
      a card INSERT takes only ``FOR KEY SHARE``, which that lock does not block.

    Lock modes and order:

    - Vendors ``FOR SHARE``, then invoices ``FOR NO KEY UPDATE`` — vendor
      before invoice, each in id order, the order ``vendor_merge`` takes them
      in, so no cycle with it. ``FOR SHARE`` lets two mints for one vendor run
      together but holds off a status change (deactivate / reject / block)
      until the card is committed, so the vendor-side check cannot go stale and
      whatever that change does about live cards sees this one.
    - ``FOR NO KEY UPDATE`` on the invoice is the lock dispatch takes
      (``api/payments._lock_payment_invoice``), so the two serialise, and like
      dispatch's it leaves the ``FOR KEY SHARE`` an FK insert takes alone.

    Every check the caller runs afterwards is a fresh statement under READ
    COMMITTED, so a loser that waited sees the winner's card and skips it.

    The wait is bounded by ``timeout_ms`` in a savepoint (``utils/db_locks``,
    ``docs/decisions.md`` §233) and raises ``LockWaitTimeout`` with the caller's
    transaction intact. It is taken before any provider call, so a timeout has
    minted nothing and is safe to retry.

    Returns the payable invoices in id order, narrowed to ``entity_id`` when one
    is selected (``tenant.apply_entity_scope``). An invoice whose vendor changed
    between the unlocked vendor read and the lock is left out, because its
    vendor was not locked and so it cannot be checked against a stable one.

    ``POST /api/cards/generate`` calls this once per invoice and commits after
    each one, so locks are held for one provider call, not the whole batch.
    """
    from app.tenant import apply_entity_scope
    from app.utils.db_locks import bounded_lock_wait

    ids = sorted(set(invoice_ids))
    if not ids:
        return []
    vendor_ids = sorted(
        set(
            (
                await db.execute(
                    select(Invoice.vendor_id).where(
                        Invoice.id.in_(ids), Invoice.vendor_id.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
    )
    async with bounded_lock_wait(db, timeout_ms):
        locked_vendors: set = set()
        if vendor_ids:
            locked_vendors = set(
                (
                    await db.execute(
                        select(Vendor.id)
                        .where(Vendor.id.in_(vendor_ids))
                        .order_by(Vendor.id)
                        .with_for_update(read=True)
                    )
                )
                .scalars()
                .all()
            )
        invoices = (
            (
                await db.execute(
                    apply_entity_scope(
                        select(Invoice).where(
                            Invoice.id.in_(ids), Invoice.status.in_(payable_statuses)
                        ),
                        Invoice,
                        entity_id,
                    )
                    .order_by(Invoice.id)
                    .with_for_update(key_share=True)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
    return [inv for inv in invoices if inv.vendor_id is None or inv.vendor_id in locked_vendors]


def card_settlement_block(card: VirtualCard, amount, *, now: datetime | None = None) -> str | None:
    """Why ``card`` cannot settle a payment of ``amount`` — ``None`` if it can.

    Only asked of a card this payment did NOT mint. Converging a payment onto a
    pre-existing card marks it ``completed``, i.e. asserts the money moved, so
    the assertion has to be true:

    - **Already spent.** ``amount_limit`` is the card's authorization ceiling and
      is NOT reduced by spend (a charge only sets ``amount_charged``), so a limit
      check alone happily "settles" a payment against a card whose funds already
      moved under a *different* payment. That is reachable without any race:
      mint → vendor redeems → AP voids that payment → the invoice returns to the
      payable pool → the next run rediscovers the same spent card.
    - **Expired.** An expired card cannot be charged at the network, so nothing
      can move against it and the ``completed`` assertion would be false. This
      is reached the same way as the spent case — a card minted for an earlier
      run, the payment voided, the invoice back in the pool weeks later — except
      the card aged out rather than being redeemed, so the status check above
      cannot see it. Without this, the run marked the payment `completed`, the
      invoice advanced to `paid`, and the vendor was never paid at all: worse
      than the double-spend the status check exists to prevent, because there is
      no charge anywhere to reconcile against.
    - **Limit too small.** A live, unspent card that cannot cover this payable is
      not what settles it.
    - **Limit too large.** A card is spendable up to its limit, and nothing on
      the card webhook compares a charge with ``Payment.amount``. Converging a
      payment for less than the limit — net of a credit memo, or of an accepted
      early-payment discount — records the smaller figure as paid (and the
      discount as captured) while the vendor can still charge the whole limit.
      Refused as ``card_limit_exceeds_payment`` before anything moves; cancel the
      card and the next run mints one for the booked amount.

    ``now`` is injectable so the expiry boundary is testable without freezing the
    clock; it defaults to the current UTC time. A card with no ``expires_at`` is
    treated as non-expiring — the column is nullable and several providers do not
    return one, and refusing on a missing value would block every such card.

    Returned strings are ``Payment.failure_reason`` values — operator-facing,
    PII-free (no PAN, no last four).
    """
    if card.status in CARD_SPENT_STATUSES:
        return "card_already_charged"
    if card.expires_at is not None:
        # Both sides normalised to aware UTC: `expires_at` is a timezone-aware
        # column, but a row built in a test or by an adapter that dropped the
        # tzinfo would otherwise raise on comparison inside the money path.
        expires_at = card.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= (now or datetime.now(UTC)):
            return "card_expired"
    if card.amount_limit is None or card.amount_limit < amount:
        return "card_already_issued_insufficient_limit"
    if card.amount_limit > amount:
        return "card_limit_exceeds_payment"
    return None


async def persist_card(db: AsyncSession, card: VirtualCard) -> bool:
    """Add + flush ONE freshly-minted card inside a SAVEPOINT.

    Returns True when the row landed, False when the invoice's live-card slot
    was claimed by a concurrent writer between the caller's pre-check and this
    flush (``uq_virtual_cards_one_live_per_invoice``).

    The savepoint is what makes the loser recoverable. A bare ``db.flush()``
    that trips the index leaves the *enclosing* transaction in a needs-rollback
    state, so every subsequent statement on that session — the caller's audit
    row, its ``commit()`` — raises ``PendingRollbackError`` instead. On the
    payment-run path that unwound the whole dispatch loop and stranded the run
    in ``executing``; on the batch path it discarded the sibling cards already
    minted at the provider. Rolling back to the savepoint keeps the outer
    transaction healthy and turns a benign duplicate into a value the caller
    can branch on.

    ``db.add`` MUST happen INSIDE the savepoint block, never before it.
    ``SessionTransaction._take_snapshot`` flushes the session when a
    ``begin_nested()`` boundary opens, so a row added first is written by
    ``begin_nested()`` itself — *before* the SAVEPOINT exists — and its
    IntegrityError escapes the very block meant to contain it. That ordering
    trap is the reason this lives in one helper instead of being inlined at
    each call site.

    No explicit ``expunge`` is needed on the losing row: rolling back to the
    savepoint runs ``SessionTransaction._restore_snapshot``, which expunges
    ``session._new`` to transient — so a later ``flush``/``commit`` on this
    session cannot re-attempt the insert that just failed. (Asserted by the
    regression tests, which commit after a lost race and check exactly one row
    survives.) ``recurring_invoices.generate_one`` relies on the same behaviour;
    the two savepoints are deliberately identical in shape.
    """
    try:
        async with db.begin_nested():
            db.add(card)
            await db.flush()
    except IntegrityError:
        return False
    return True


@dataclass
class CardIssueResult:
    """Returned to the payment-run executor so it can record provenance
    on the matching Payment row."""

    card: VirtualCard | None
    success: bool
    failure_reason: str | None = None


DEFAULT_CARD_EXPIRY_DAYS = 30


def _coerce_expiry_days(raw) -> int:
    """Coerce an org-supplied `default_expiry_days` into a sane int.

    Tenant settings come from a JSONB column, so the value could be a
    string like "14" pasted into an admin form, a float, or garbage.
    Anything that doesn't reduce to a positive int falls back to the
    platform default. Returning a bogus expiry here would either mint
    cards that expired in the past or cards that never expire.
    """
    if raw is None:
        return DEFAULT_CARD_EXPIRY_DAYS
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CARD_EXPIRY_DAYS
    if days <= 0:
        return DEFAULT_CARD_EXPIRY_DAYS
    return days


def _resolve_card_config(org_settings: dict, app_settings) -> dict | None:
    """Mirror of `app.api.cards._resolve_card_config` but pure — no
    Organization model, no FastAPI deps. Returns None if the org hasn't
    enabled cards."""
    org_cards = (org_settings or {}).get("cards") or {}
    if not org_cards.get("enabled"):
        return None

    program_type = org_cards.get("program_type", "platform")
    region = org_cards.get("region", "US")
    expiry_days = _coerce_expiry_days(org_cards.get("default_expiry_days"))

    if program_type == "byok":
        return {
            "provider": org_cards.get("provider", ""),
            "region": region,
            "api_key": org_cards.get("api_key", ""),
            "client_id": org_cards.get("client_id", ""),
            "client_secret": org_cards.get("client_secret", ""),
            "customer_hash_id": org_cards.get("customer_hash_id", ""),
            "wallet_hash_id": org_cards.get("wallet_hash_id", ""),
            # BYOK sandbox is opt-IN — a customer supplying their own real keys
            # expects live rails; defaulting to sandbox silently paid invoices
            # into the provider's sandbox. Mirror api.cards._resolve_card_config.
            "sandbox": org_cards.get("sandbox", False),
            "default_expiry_days": expiry_days,
        }

    from app.services.card_adapters.dispatcher import get_default_provider

    # An explicit admin-set `provider` override wins; auto-select by region
    # only when unset. Mirror api.cards._resolve_card_config — see its
    # comment for why platform mode must honour this (local-first: without
    # it, platform mode can never be pointed at `mock`).
    provider = org_cards.get("provider") or get_default_provider(region)
    if provider == "lithic":
        return {
            "provider": "lithic",
            "region": region,
            "api_key": app_settings.lithic_api_key,
            "sandbox": app_settings.lithic_sandbox,
            "default_expiry_days": expiry_days,
        }
    if provider == "nium":
        return {
            "provider": "nium",
            "region": region,
            "client_id": app_settings.nium_client_id,
            "client_secret": app_settings.nium_client_secret,
            "customer_hash_id": app_settings.nium_customer_hash_id,
            "wallet_hash_id": app_settings.nium_wallet_hash_id,
            "sandbox": app_settings.nium_sandbox,
            "default_expiry_days": expiry_days,
        }
    # e.g. "mock" for local-first testing — no live credentials needed. Any
    # other/unrecognized value is REFUSED by `get_card_adapter`
    # (`UnknownCardProviderError`) rather than resolving to the fixture adapter
    # — see `decisions.md` §29. Both card call sites below absorb that raise.
    return {
        "provider": provider,
        "region": region,
        "default_expiry_days": expiry_days,
    }


async def issue_card_for_invoice(
    *,
    db: AsyncSession,
    invoice: Invoice,
    organization_id,
    org_settings: dict,
    app_settings,
    payment_id=None,
    amount: Decimal | None = None,
) -> CardIssueResult:
    """Mint one card via the org's configured adapter and persist the
    VirtualCard row. Returns the row (uncommitted — caller flushes).

    Every provider call carries a deterministic idempotency key
    (`build_card_idempotency_key`), so a retry of the same logical issuance —
    after a client-side timeout, a double-click, a re-run of a payment run —
    resolves to the card the provider already made rather than minting a second
    live one. `db` is required for that: the key's re-issue discriminator is
    read from the invoice's existing card rows.

    Returns success=False with a populated failure_reason when:
      - The org hasn't enabled cards (`cards.enabled` falsy)
      - The org names a card provider we have no adapter for
        (`card_provider_not_configured` — never resolved to `mock`)
      - The adapter call fails

    Caller decides what to do on failure (skip the row, downgrade to
    ACH, etc.). This module never raises on a known business condition.
    """
    config = _resolve_card_config(org_settings, app_settings)
    if config is None:
        return CardIssueResult(card=None, success=False, failure_reason="cards_not_enabled")

    # Ensure the adapter classes register themselves with the dispatcher.
    import app.services.card_adapters.lithic  # noqa: F401
    import app.services.card_adapters.mock_adapter  # noqa: F401
    import app.services.card_adapters.nium  # noqa: F401
    from app.services.card_adapters import (
        UnknownCardProviderError,
        VirtualCardPayload,
        get_card_adapter,
    )

    try:
        adapter = get_card_adapter(config)
    except UnknownCardProviderError as exc:
        # The org named an issuer we have no adapter for. Refuse the issuance
        # rather than let it resolve to `mock`, which reports success with a
        # fixture PAN and marks the payment settled (`decisions.md` §29). The
        # refusal lands BEFORE any provider call, so no card exists anywhere —
        # the reason ends in `_not_configured`, which
        # `payment_runs.classify_payment_failure` already treats as RETRY_SAFE.
        # The AP-facing `failure_reason` names the condition only, never the
        # admin's raw settings value: every AP user reads it, only an admin
        # owns the setting (same split as `fx_provider_unsupported`).
        logger.warning(
            "[card_issuance] card provider %r has no registered adapter — refusing to issue",
            exc.provider,
        )
        return CardIssueResult(
            card=None, success=False, failure_reason="card_provider_not_configured"
        )
    expiry_days = config.get("default_expiry_days", DEFAULT_CARD_EXPIRY_DAYS)

    payload = VirtualCardPayload(
        correlation_id=str(invoice.correlation_id) if invoice.correlation_id else "",
        invoice_id=str(invoice.id),
        vendor_name=invoice.vendor_name,
        vendor_email=None,
        amount=amount or invoice.amount,
        currency=invoice.currency or "USD",
        description=invoice.description,
        expiry_days=expiry_days,
        idempotency_key=build_card_idempotency_key(
            invoice_id=invoice.id,
            correlation_id=invoice.correlation_id,
            reissue_seq=await _reissue_sequence(db, invoice.id),
        ),
    )

    try:
        result = await adapter.create_card(payload)
    except Exception as exc:  # noqa: BLE001
        # Log the exception type, never the message. A card-provider
        # adapter could surface a partial PAN / merchant token in its
        # error string; interpolating `exc` would push that into the
        # log sink (invariant #7).
        logger.warning(
            "[card_issuance] adapter %s raised on invoice %s: %s",
            adapter.provider_name,
            invoice.id,
            exc.__class__.__name__,
        )
        return CardIssueResult(
            card=None, success=False, failure_reason=f"adapter_error:{exc.__class__.__name__}"
        )

    if not result.success:
        return CardIssueResult(
            card=None,
            success=False,
            failure_reason=result.failure_reason or "adapter_returned_failure",
        )

    card = VirtualCard(
        invoice_id=invoice.id,
        payment_id=payment_id,
        vendor_id=invoice.vendor_id,
        correlation_id=invoice.correlation_id,
        card_provider=adapter.provider_name,
        provider_card_id=result.provider_card_id or "",
        last_four=result.last_four,
        amount_limit=amount or invoice.amount,
        currency=invoice.currency or "USD",
        status="created",
        expires_at=datetime.now(UTC) + timedelta(days=expiry_days),
        organization_id=organization_id,
        # Card follows the invoice it pays (multi-entity P2). `getattr` because
        # some lightweight test doubles construct an invoice-like object
        # without the attribute — a real ORM `Invoice` always has it via
        # `EntityMixin`.
        entity_id=getattr(invoice, "entity_id", None),
    )
    return CardIssueResult(card=card, success=True)


async def cancel_card_at_provider(
    *,
    card: VirtualCard,
    org_settings: dict,
    app_settings,
) -> str:
    """Close ``card`` at its provider. Returns an outcome tag; never raises.

    Provider-side only — the caller owns the DB row and the audit trail, and
    must only mark the row cancelled on ``"cancelled"``. That ordering is the
    fail-safe one: "dead at the provider, maybe stale in the DB" is recoverable,
    "cancelled in our DB but still chargeable at the provider" is not.

    Outcomes: ``cancelled`` | ``cards_not_configured`` |
    ``card_provider_not_configured`` | ``card_cancel_rejected`` |
    ``card_cancel_error:<ExceptionType>``.
    """
    config = _resolve_card_config(org_settings, app_settings)
    if config is None:
        return "cards_not_configured"

    import app.services.card_adapters.lithic  # noqa: F401
    import app.services.card_adapters.mock_adapter  # noqa: F401
    import app.services.card_adapters.nium  # noqa: F401
    from app.services.card_adapters import UnknownCardProviderError, get_card_adapter

    try:
        adapter = get_card_adapter(config)
    except UnknownCardProviderError as exc:
        # Distinct from `cards_not_configured` (cards switched off): here the
        # org names an issuer we cannot talk to, so the card is left LIVE at
        # whatever provider actually minted it. Record that; never claim a
        # cancel we did not obtain. Mirrors the payment rail's
        # `provider_not_supported` void outcome.
        logger.warning(
            "[card_issuance] card provider %r has no registered adapter — cannot cancel card %s",
            exc.provider,
            card.id,
        )
        return "card_provider_not_configured"

    try:
        confirmed = await adapter.cancel_card(card.provider_card_id)
    except Exception as exc:  # noqa: BLE001
        # PII guard: the exception TYPE only — a card-provider error string can
        # embed a partial PAN or a merchant token.
        logger.warning(
            "[card_issuance] provider cancel raised for card %s: %s",
            card.id,
            exc.__class__.__name__,
        )
        return f"card_cancel_error:{exc.__class__.__name__}"
    return "cancelled" if confirmed else "card_cancel_rejected"


# --- The verdict a void's card leg reports -----------------------------------
#
# `_cancel_card_for_void` (api/payments.py) returns a fine-grained outcome tag
# that rides the `payment.voided` audit row. The UI must not branch on that
# vocabulary — a client enumerating outcome strings mis-classifies every tag
# added later, and it fails in the dangerous direction ("unknown means fine").
# So the VERDICT is derived here, once, beside the function that produces the
# outcomes, and travels on the response as
# `PaymentResponse.void_card_disposition`:
#
#   closed                 the card is dead at the provider. Nothing to do.
#   no_card                this payment never had a card, or none is linked.
#   not_closed_final       the card was already CHARGED. It cannot be un-spent,
#                          so a retry can never help — the remedy is a vendor /
#                          chargeback conversation, and `card_settlement_block`
#                          is what stops a later run settling against it.
#   not_closed_retryable   the provider leg did not confirm a close (outage,
#                          refusal, cards switched off, unregistered provider).
#                          The card is LIVE and bearer-spendable; retrying the
#                          close is the remedy, and it lives on the void
#                          (`POST /api/payments/{id}/void/retry-card-cancel`).
#
# `None` means the leg never ran — a non-card payment, or a read that was not a
# void. Deliberately NOT folded into `closed`: "we never asked" is not "it is
# shut" (docs/decisions.md §34).
CardCancelDisposition = Literal["closed", "no_card", "not_closed_final", "not_closed_retryable"]

# `cancelled` is `cancel_card_at_provider`'s own success tag; `card_cancelled`
# is what the void path records for the same event. Both mean the same closed
# card, so both are listed rather than relying on the caller to translate.
_CARD_CLOSED_OUTCOMES = frozenset({"card_cancelled", "cancelled", "card_already_cancelled"})
_CARD_NO_CARD_OUTCOMES = frozenset({"no_card_linked"})
_CARD_FINAL_OUTCOMES = frozenset({"card_already_charged"})


def card_cancel_disposition(outcome: str | None) -> CardCancelDisposition | None:
    """Classify one card-cancel `outcome` tag into the verdict the UI renders.

    An unknown tag classifies as ``not_closed_retryable`` on purpose: only the
    tags enumerated as closing a card may claim one, so a new failure tag added
    to `cancel_card_at_provider` surfaces (and is offered a retry) rather than
    silently reading as success.
    """
    if outcome is None:
        return None
    if outcome in _CARD_CLOSED_OUTCOMES:
        return "closed"
    if outcome in _CARD_NO_CARD_OUTCOMES:
        return "no_card"
    if outcome in _CARD_FINAL_OUTCOMES:
        return "not_closed_final"
    return "not_closed_retryable"


async def notify_vendor_of_card(
    db: AsyncSession,
    *,
    card: VirtualCard,
    invoice: Invoice,
    org_name: str,
    public_url_template: str | None,
) -> bool:
    """Mint a single-use reveal token, send the vendor an email with
    the link. Returns True if the email was dispatched, False if we
    skipped (no vendor email, no base URL, or adapter error).

    `public_url_template` is the tenant's ALREADY-RESOLVED base URL (see
    `app/utils/tenant_urls.tenant_base_url`). It used to be the raw global
    template plus an `org_slug` this function substituted itself — one of the
    six inline `{slug}` substitutions that made a white-label tenant's links
    point back at the platform subdomain.

    Fail-soft: a missing vendor email or a flaky email adapter must not
    block the card issuance flow. The card is already minted; the email
    is a courtesy.
    """
    if not invoice.vendor_id:
        return False

    vendor_result = await db.execute(select(Vendor).where(Vendor.id == invoice.vendor_id))
    vendor = vendor_result.scalar_one_or_none()
    if vendor is None or not vendor.email:
        logger.info(
            "[card_issuance] skipping vendor email — vendor %s has no email on file",
            invoice.vendor_id,
        )
        return False

    from app.services.card_reveal import mint_reveal_token

    plaintext_token = await mint_reveal_token(db, card)

    # `public_url_template` arrives ALREADY RESOLVED for this tenant — the
    # caller runs `app/utils/tenant_urls.tenant_base_url`, which is the one
    # place `{slug}` is substituted and the one place a tenant's per-org vanity
    # host wins over the global template. Empty means nothing is configured, so
    # we skip the email rather than point the vendor at a fabricated host.
    if not public_url_template:
        return False
    link = f"{public_url_template.rstrip('/')}/portal/cards/{plaintext_token}"

    subject = f"{org_name} sent you a virtual card payment"
    body_text = (
        f"Hi {vendor.name},\n\n"
        f"{org_name} has issued a virtual card to pay invoice "
        f"{invoice.invoice_number or invoice.id} for "
        f"{invoice.currency or 'USD'} {(card.amount_limit):,.2f}.\n\n"
        f"View your one-time card details:\n  {link}\n\n"
        "This link will expire in 7 days and can only be used once. "
        "If you didn't expect this email, please reply to let us know.\n"
    )
    body_html = (
        f"<p>Hi {vendor.name},</p>"
        f"<p><strong>{org_name}</strong> has issued a virtual card to pay invoice "
        f"<code>{invoice.invoice_number or invoice.id}</code> for "
        f"<strong>{invoice.currency or 'USD'} {(card.amount_limit):,.2f}</strong>.</p>"
        f'<p><a href="{link}">View your one-time card details</a></p>'
        f"<p style='color:#888;font-size:12px'>This link expires in 7 days "
        "and can only be used once.</p>"
    )

    from app.services.email_adapters import EmailMessage, get_email_adapter

    try:
        adapter = get_email_adapter()
        await adapter.send(
            EmailMessage(
                to=vendor.email,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
            )
        )
        return True
    except Exception as exc:  # noqa: BLE001
        # PII guard: the raw exception can embed the vendor's email address
        # (SES/SMTP echo the recipient in the error). Log the type only.
        logger.warning(
            "[card_issuance] vendor email send failed for card %s: %s",
            card.id,
            exc.__class__.__name__,
        )
        return False
