"""Base payment adapter interface.

Adapters are responsible for the *external* leg of paying a vendor: handing
the payment off to a bank or processor and returning a tracking ID. The
internal Payment row is owned by `app.api.payments` — adapters don't touch
the DB.

Status lifecycle:

    pending → submitted → completed
                       ↘ failed
                       ↘ cancelled

`pending` means we've created the row locally but haven't called the
processor yet (a transient state during execute_payment_run). `submitted`
means the processor accepted the request — money is in flight but not yet
settled. `completed` is the terminal success state. `failed` and
`cancelled` are terminal too. Webhooks drive the submitted → completed
transition; we don't poll.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.utils.currency_code import iso_currency_or_none


class PaymentStatus(enum.StrEnum):
    pending = "pending"
    submitted = "submitted"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


# Subset that adapters use to report initial submission outcome. Webhooks
# move payments from `submitted` → `completed` / `failed` later.
TERMINAL_STATUSES: frozenset[PaymentStatus] = frozenset(
    {PaymentStatus.completed, PaymentStatus.failed, PaymentStatus.cancelled}
)


# --- Routing-number selection -------------------------------------------------
#
# A US vendor's `bank_details` may carry TWO ABA routing numbers, because larger
# US banks publish a different one for incoming wires than for ACH:
#
#   * ``routing_number``       — the ACH / domestic (paper & electronic) ABA.
#     This is the ORIGINAL, generic key. Every row already stored under it means
#     the ACH number, so it keeps that meaning: no backfill, no reinterpretation
#     of data somebody else wrote.
#   * ``wire_routing_number``  — the Fedwire ABA, recorded only when the bank
#     actually publishes a separate one.
#
# `WIRE_ROUTING_METHODS` is the rail set that must use the wire ABA. Everything
# else (ach, international_ach, rtp, check, and the non-US rails, which don't use
# an ABA at all) reads the ACH field.
WIRE_ROUTING_METHODS: frozenset[str] = frozenset({"wire", "international_wire"})

ACH_ROUTING_FIELD = "routing_number"
WIRE_ROUTING_FIELD = "wire_routing_number"


@dataclass(frozen=True)
class RoutingSelection:
    """Which routing number a rail should use, and where it came from.

    `source` is a PII-free label (`"wire"` / `"ach"` / `"none"`) safe to put in
    a log line, an audit row or a provider `raw_response`; `number` is banking
    data and must never leave the outbound request to the processor.
    """

    number: str | None
    source: str  # "wire" | "ach" | "none"


def resolve_routing_number(vendor_bank: dict | None, method: str) -> RoutingSelection:
    """Pick the routing number the given rail must be instructed with.

    Wire-family rails prefer ``wire_routing_number`` and **fall back** to
    ``routing_number`` when the vendor recorded only one — a bank that publishes
    a single ABA uses it for both, so refusing the payment there would break
    every vendor banking at a smaller institution.

    ACH-family rails read ``routing_number`` and deliberately do **not** fall
    back to the wire number. The fallback is asymmetric on purpose: a bank with
    two ABAs will not accept an ACH file addressed to its Fedwire number, so
    "borrowing" it turns a missing-data problem into a returned item days later
    at the vendor's expense. Missing means missing.

    Pure — no IO, no DB. Callers decide what an empty selection means for their
    rail (most processors identify the payee by a counterparty token instead and
    never need this at all).
    """
    bank = vendor_bank or {}
    if method in WIRE_ROUTING_METHODS:
        wire = (str(bank.get(WIRE_ROUTING_FIELD) or "")).strip()
        if wire:
            return RoutingSelection(number=wire, source="wire")
        ach = (str(bank.get(ACH_ROUTING_FIELD) or "")).strip()
        return RoutingSelection(number=ach or None, source="ach" if ach else "none")
    ach = (str(bank.get(ACH_ROUTING_FIELD) or "")).strip()
    return RoutingSelection(number=ach or None, source="ach" if ach else "none")


@dataclass
class PaymentPayload:
    """Everything an adapter needs to submit one payment.

    `correlation_id` is sent as the processor's idempotency key so a retry
    doesn't double-pay. `vendor_bank` is intentionally a free-form dict —
    each processor models bank accounts differently (Modern Treasury uses
    `counterparty_id`; Increase uses an external account ID; mock ignores it).
    The orchestrator looks up the right shape per processor. It carries the
    vendor's whole `bank_details` JSONB, which may hold BOTH a `routing_number`
    (ACH) and a `wire_routing_number` (Fedwire) — read the right one for this
    rail through the `routing` property, never by key.

    International fields are populated by
    `services.international_payments.prepare_international_payment` when
    the corridor needs an FX leg or a foreign rail. Adapters that don't
    support international rails ignore them; ones that do (Wise, future
    Tipalti) use them to set the FX quote ID + destination corridor on
    the outbound request.
    """

    correlation_id: str
    invoice_id: str
    invoice_number: str
    vendor_name: str
    amount: Decimal
    currency: str
    method: str  # "ach" | "wire" | "rtp" | "check" | "sepa" | "international_wire"
    description: str | None = None
    vendor_bank: dict | None = None
    metadata: dict | None = None
    # International leg — None on domestic same-currency payments.
    source_currency: str | None = None
    source_amount: Decimal | None = None
    fx_rate: Decimal | None = None
    target_country: str | None = None

    @property
    def routing(self) -> RoutingSelection:
        """The routing number THIS rail must be instructed with.

        Single accessor so no adapter has to re-derive "is this a wire?" from
        `self.method` — see `resolve_routing_number`. Most adapters never touch
        it (their processor identifies the payee by a counterparty token); the
        ones that hand a bank raw coordinates read it here rather than reaching
        into `vendor_bank["routing_number"]`, which is the ACH number and would
        misroute a wire at any bank publishing a separate Fedwire ABA.
        """
        return resolve_routing_number(self.vendor_bank, self.method)


@dataclass
class PaymentResult:
    """Outcome of `create_payment`. Reference is whatever the processor
    returns to identify the transaction (Modern Treasury Payment ID, ACH
    trace number, check number, etc.). Status reflects the *immediate*
    response — webhooks drive subsequent transitions."""

    success: bool
    status: PaymentStatus
    provider_payment_id: str | None = None
    reference: str | None = None
    failure_reason: str | None = None
    raw_response: dict | None = None


@dataclass
class CorridorQuote:
    """Per-provider price quote for one payment.

    Returned by `quote_payment` and aggregated by
    `services.corridor_quotes.compare_quotes` to pick the
    cheapest / fastest route across N enabled processors.

    `flat_fee` and `pct_fee` together describe the fee structure:
    total = flat_fee + amount * pct_fee. The aggregator computes the
    realized total for a given amount and ranks. `eta_business_days`
    is the processor's stated settlement SLA; the aggregator uses it
    for the "fastest" tiebreaker.

    `available` is the load-bearing field: an adapter that doesn't
    support the requested (method, currency_pair, country) corridor
    returns `available=False` so the aggregator skips it. The
    `unavailable_reason` carries the why for debugging.
    """

    provider: str
    method: str
    available: bool
    flat_fee: Decimal = Decimal("0")
    pct_fee: Decimal = Decimal("0")
    eta_business_days: int = 0
    fx_rate: Decimal | None = None
    unavailable_reason: str | None = None

    def total_cost(self, amount: Decimal) -> Decimal:
        """Realised cost in source currency for the given amount.

        Unavailable corridors return Decimal("Infinity") so they
        never win a min() comparison. We use the unbounded value
        instead of None so callers don't have to filter twice."""
        if not self.available:
            return Decimal("Infinity")
        return self.flat_fee + (amount * self.pct_fee)


@dataclass
class BalanceResult:
    """Current available balance on the org's funding/operating account at the
    processor, used to auto-seed the cash-position dashboard's opening balance.

    `available` is the load-bearing field (mirrors `CorridorQuote`): an adapter
    that can't report a balance — no bank link, no balance endpoint, transport
    failure — returns ``BalanceResult(available=False, ...)`` and the caller
    falls back to the org's bring-your-own opening balance. `amount` is exact
    (`Decimal`, never float) and only meaningful when `available` is True.
    """

    available: bool
    amount: Decimal = Decimal("0")
    currency: str = "USD"
    account_ref: str | None = None  # opaque account label for the UI — never a full account number
    unavailable_reason: str | None = None


#: ISO-4217 currencies whose minor unit is NOT 1/100. Everything absent here
#: uses the near-universal exponent of 2, so this table only carries the
#: exceptions — a full currency list would be dead weight that drifts.
#:
#: Exponent 0: the major unit IS the minor unit. ¥100 is sent as `100`, not
#: `10000`. Exponent 3: a thousandth (Kuwaiti fils, Bahraini fils, Omani
#: baisa) — 1 KWD is sent as `1000`.
_MINOR_UNIT_EXPONENTS: dict[str, int] = {
    # exponent 0
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "ISK": 0,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "PYG": 0,
    "RWF": 0,
    "UGX": 0,
    "UYI": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
    # exponent 3
    "BHD": 3,
    "IQD": 3,
    "JOD": 3,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "TND": 3,
}

DEFAULT_MINOR_UNIT_EXPONENT = 2


def exponent_for(currency: str | None) -> int:
    """How many minor units make one major unit of `currency`, as a power of 10.

    Unknown or absent currency falls back to 2 — the overwhelmingly common
    case, and the behaviour every caller had before this table existed.
    """
    if not currency:
        return DEFAULT_MINOR_UNIT_EXPONENT
    return _MINOR_UNIT_EXPONENTS.get(
        iso_currency_or_none(currency) or "", DEFAULT_MINOR_UNIT_EXPONENT
    )


def _scale(exponent: int) -> Decimal:
    return Decimal(10) ** exponent


def to_minor_units(amount: Decimal, currency: str | None) -> int:
    """Scale a major-unit amount into the processor's minor units.

    The submit-side half of the pair. Both halves MUST resolve the exponent
    the same way: they were `* 100` and `/ 100` unconditionally, which is
    symmetric (so it could never raise a phantom settlement mismatch) but
    symmetrically WRONG for a currency whose exponent isn't 2 — ¥5,000 went
    out as 500,000 minor units, a 100x overpayment, and 5 KWD went out as 500
    fils instead of 5,000, a 10x underpayment. Changing one side alone would
    have turned that into a real mispricing, which is why the two moved
    together.
    """
    scaled = amount * _scale(exponent_for(currency))
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def minor_units_to_decimal(raw: object, currency: str | None = None) -> Decimal | None:
    """Convert a processor's minor-unit amount to an exact `Decimal`.

    The exact inverse of `to_minor_units` for the same currency, so the
    round-trip is symmetric and a clean settlement can never read as a
    mismatch. `currency` is optional only so a caller that genuinely doesn't
    know it (a webhook body that omits the field) still parses under the
    common exponent of 2 rather than failing; pass it whenever the payload
    carries it.

    Returns None for anything unparseable (a missing key, `null`, a string
    the provider didn't promise): the settlement verifier treats "no reported
    amount" as `unverified`, never as evidence of a discrepancy, so a
    tolerant parse here is the fail-open branch and not a swallowed error.
    """
    if raw is None or isinstance(raw, bool):
        return None
    exponent = exponent_for(currency)
    try:
        return (Decimal(str(raw)) / _scale(exponent)).quantize(Decimal(1).scaleb(-exponent))
    except (ArithmeticError, ValueError):
        return None


def parse_amount(raw: object) -> Decimal | None:
    """Parse a processor's major-unit amount (a decimal string or number).

    Same tolerant contract as `minor_units_to_decimal` — see its docstring
    for why an unparseable value is None rather than a raise.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return Decimal(str(raw)).quantize(Decimal("0.01"))
    except (ArithmeticError, ValueError):
        return None


@dataclass
class SettlementReport:
    """What the processor says it settled, fetched on demand.

    The pull counterpart to the push figure on ``WebhookEvent``. Two paths in
    the money system know a payment completed but never learn the amount:

    * a rail whose status webhook is a bare envelope (Dwolla's
      ``{id, topic, resourceId, _links}`` — the figure is only reachable by
      following ``_links.resource``, which the synchronous
      signature-verification path must not do); and
    * ``payment_reconciler``, the backstop for a webhook that never arrived,
      whose ``get_payment_status`` returns a bare status by design.

    Both therefore settle ``unverified``. This closes that by letting the
    caller ask.

    ``available`` is the load-bearing field, exactly as on ``BalanceResult``:
    an adapter with no way to report a settled figure returns
    ``available=False`` and the caller leaves the verdict ``unverified``
    rather than inventing one. Implementations catch transport failures and
    return ``available=False`` instead of raising — a settlement fetch must
    never break the webhook or the sweep that called it.
    """

    available: bool
    amount: Decimal | None = None
    currency: str | None = None
    unavailable_reason: str | None = None


@dataclass
class WebhookEvent:
    """Normalised representation of a webhook from the processor.

    Adapters parse provider-specific webhook bodies into this shape so the
    common handler in `app.api.payments` can route by `provider_payment_id`
    without knowing the processor's wire format.

    ``event_id`` is the processor's own event identifier (NOT the payment
    id) — webhooks retry on any non-2xx, so the handler dedupes by
    ``event_id`` against the Redis ``is_event_already_processed`` ledger
    before mutating state. Adapters that don't expose a stable event id
    should fall back to a composite key (e.g. ``f"{payment_id}:{status}"``)
    that's unique per state transition.

    ``amount`` / ``currency`` are what the processor says it actually
    SETTLED, in major units — the evidence
    `services.payment_settlement.verify_settlement` compares against the
    amount AP authorized before the handler treats a `completed` event as a
    clean settlement. Both stay ``None`` for a provider whose status webhook
    genuinely doesn't carry the figure (Dwolla's transfer events are a bare
    `{topic, resourceId}`); that reads as `unverified`, never as a
    discrepancy. Adapters converting from minor units should use
    ``minor_units_to_decimal`` so the round-trip stays symmetric with what
    they sent on submit.
    """

    provider_payment_id: str
    status: PaymentStatus
    event_id: str = ""
    reference: str | None = None
    failure_reason: str | None = None
    occurred_at: str | None = None  # ISO8601, processor's timestamp
    amount: Decimal | None = None  # settled amount, MAJOR units
    currency: str | None = None  # ISO-4217, as reported
    raw: dict = field(default_factory=dict)


class PaymentAdapter:
    """Base class for payment processor integrations.

    Adapters MUST be stateless — same instance is reused across requests.
    Anything tenant-specific is in `self.config`.
    """

    provider_name: str = "base"
    supported_methods: tuple[str, ...] = ()  # which `method` values this adapter accepts

    def __init__(self, config: dict):
        self.config = config

    async def create_payment(self, payload: PaymentPayload) -> PaymentResult:
        """Submit one payment to the processor. Idempotent by `correlation_id`."""
        raise NotImplementedError

    async def get_payment_status(self, provider_payment_id: str) -> PaymentStatus:
        """Re-fetch status from the processor. Used for reconciliation jobs
        when a webhook is suspected to be missing."""
        raise NotImplementedError

    def parse_webhook(self, headers: dict, body: bytes) -> WebhookEvent | None:
        """Parse + verify a webhook from the processor. Returns None if the
        signature doesn't match or the event isn't a payment status update
        we care about. Raises only on programmer error, never on bad input."""
        raise NotImplementedError

    async def test_connection(self) -> bool:
        """Hit a cheap read endpoint to verify credentials work."""
        raise NotImplementedError

    async def void_payment(self, provider_payment_id: str) -> bool:
        """Reverse / cancel an in-flight or settled payment upstream.

        Optional — adapters that don't support it should return False.
        Returning True means the processor accepted the void; the caller
        is responsible for the local bookkeeping (flipping the row,
        re-opening the invoice, audit log).

        Raises only on programmer error or transport failure; the caller
        treats exceptions as "void rejected" and records an adapter_error
        outcome on the audit row.
        """
        return False

    async def fetch_settlement(self, provider_payment_id: str) -> SettlementReport:
        """Re-fetch what the processor actually settled for one payment.

        OPTIONAL capability — the default returns
        ``SettlementReport(available=False, unavailable_reason="not_supported")``
        so adapters whose webhook already carries the figure (and any that
        genuinely can't report one) are unaffected, and the caller leaves the
        settlement ``unverified`` rather than inventing a verdict.

        Best-effort by contract, like ``get_balance``: implementations catch
        transport failures and return ``available=False`` rather than raise.
        Both call sites — the webhook handler's fallback and the reconciler
        backstop — additionally guard the call, so a settlement fetch can
        never break a webhook or halt a sweep.
        """
        return SettlementReport(available=False, unavailable_reason="not_supported")

    async def get_balance(self) -> BalanceResult:
        """Report the current available balance on the org's funding account.

        OPTIONAL capability — the default returns
        ``BalanceResult(available=False, unavailable_reason="not_supported")``
        so adapters that have no balance endpoint (or aren't bank-linked) are
        unaffected and the caller transparently falls back to the manual
        bring-your-own opening balance. Concrete adapters override this with a
        live read against the processor (mock returns a deterministic figure so
        local dev needs no real bank credential).

        Best-effort by contract: implementations should catch transport
        failures and return ``available=False`` rather than raise. The
        cash-position endpoint also guards the call, so a balance fetch never
        500s the dashboard.
        """
        return BalanceResult(available=False, unavailable_reason="not_supported")

    async def quote_payment(self, payload: PaymentPayload) -> CorridorQuote:
        """Return a price quote for this payment WITHOUT submitting it.

        OPTIONAL capability, and the default **fails closed** — an adapter that
        hasn't published a fee schedule reports `available=False`, exactly like
        `get_balance` and `fetch_settlement`.

        Used by `services.corridor_quotes.compare_quotes` to pick the cheapest
        (or fastest) of N configured processors. Concrete adapters override this
        with their real fee schedule + a live call to the processor's quote
        endpoint when one exists.

        **Why the default is not a permissive zero-fee quote.** It used to
        return `available=True` with `flat_fee=0`, `pct_fee=0` and
        `eta_business_days=0` for any supported method — a *fabricated* quote.
        `corridor_quotes._rank` ranks on realised cost then ETA, so an adapter
        inheriting that default beat every sibling publishing a real fee on BOTH
        `cheapest` and `fastest`, unconditionally, and `savings_vs_runner_up`
        reported an invented saving against it. Money would be routed on numbers
        nobody supplied. `modern_treasury` is the adapter that inherits it, and
        its docstring already told adapters that "genuinely can't quote MUST
        return `available=False`" — the default just didn't do what it asked.

        Failing closed means such a provider is skipped rather than chosen; if
        every configured provider is skipped, `compare_quotes` raises
        `NoEligibleCorridorError` and the caller fails the payment with a
        specific reason. That is the honest outcome of "we don't have this
        processor's pricing", and it is recoverable by adding the schedule.

        `tests/test_payment_adapter_capabilities.py` is the drift guard: a newly
        registered adapter must either implement this or be listed there as
        deliberately not implementing it.
        """
        if payload.method not in self.supported_methods:
            return CorridorQuote(
                provider=self.provider_name,
                method=payload.method,
                available=False,
                unavailable_reason=(
                    f"method '{payload.method}' not supported by {self.provider_name}"
                ),
            )
        return CorridorQuote(
            provider=self.provider_name,
            method=payload.method,
            available=False,
            unavailable_reason="no_quote_endpoint",
        )
