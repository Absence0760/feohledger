"""Settlement-amount verification — did the processor move what we authorized?

The processor's webhook is the settlement moment for every real rail: ACH,
wire, SEPA and cheque all sit ``submitted``/``processing`` until the provider
calls back, and that callback is what stamps the regulated ``completed_at``.
Until now the handler took the provider's word for the *status* and never
looked at the *amount* — a webhook saying "settled" flipped the payment to
``completed``, captured any accepted early-pay discount and told the ERP the
invoice was paid, without one comparison against the figure AP actually
authorized.

That is the same hole the other two reconcilers already close, one and two
steps further downstream:

* ``positive_pay.classify_presented_items`` — a cheque found by number whose
  presented amount differs beyond a cent is ``amount_mismatch`` (an ALTERED
  cheque), never ``matched_ok``.
* ``bank_reconciliation.classify_discrepancy`` — a bank line matched on our
  own trace reference whose amount differs is ``amount_mismatch``, linked but
  excluded from ``matched_count``.

Identity is not reconciliation. A ``provider_payment_id`` proves *which*
payment the event is about; it does not prove the processor moved the amount
on the instruction. This module supplies the missing comparison at the
earliest, most authoritative point — days before a bank statement is uploaded,
and for rails (ACH, SEPA, wire) where no cheque ever exists to present.

Pure: no DB, no clock, no I/O. ``app/api/payments.py::payment_webhook`` owns
what to *do* with a discrepancy (audit row + a payment-blocking ``fraud_flag``
exception + suppressing the discount capture); this module only decides
whether there is one.

Two legs are authorized, not one. A cross-currency payment debits
``Payment.source_amount`` in the org's home currency and credits
``Payment.amount`` in the invoice's currency (see
``services/international_payments.prepare_international_payment``), and
different processors report different sides of that. Reporting *either*
authorized leg is a match; reporting a third number is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.services.invoice_warning_catalog import exception_finding
from app.services.numeric_bounds import fits_numeric

# One cent — the same band `positive_pay.DEFAULT_AMOUNT_TOLERANCE` uses for its
# altered-cheque call and `bank_reconciliation.AMOUNT_MATCH_TOLERANCE` uses for
# its statement match. Kept identical on purpose: three reconcilers disagreeing
# about what "the same amount" means is how a discrepancy hides in the gap.
SETTLEMENT_AMOUNT_TOLERANCE = Decimal("0.01")

_CENTS = Decimal("0.01")

OUTCOME_MATCHED = "matched"
OUTCOME_AMOUNT_MISMATCH = "amount_mismatch"
OUTCOME_CURRENCY_MISMATCH = "currency_mismatch"
OUTCOME_UNVERIFIED = "unverified"

#: Outcomes that mean the settlement did NOT reconcile against an authorized
#: leg. ``unverified`` is deliberately absent — "the provider told us nothing"
#: is not evidence of a discrepancy, and treating it as one would open a
#: ``fraud_flag`` on every payment from an adapter that omits the field.
DISCREPANCY_OUTCOMES = frozenset({OUTCOME_AMOUNT_MISMATCH, OUTCOME_CURRENCY_MISMATCH})

REASON_NO_REPORTED_AMOUNT = "provider_reported_no_amount"
REASON_CURRENCY_NOT_AUTHORIZED = "settled_currency_not_authorized"
REASON_AMOUNT_DIFFERS = "settled_amount_differs_from_authorization"

#: The processor reported something that is not a usable number — a JSON `NaN`
#: or `Infinity` (Python's own `json` module emits and accepts both by default,
#: so an adapter parsing a hostile or broken payload really can hand one over),
#: or a magnitude so large that quantizing it exceeds the decimal context.
#:
#: Named apart from `REASON_NO_REPORTED_AMOUNT` on purpose: "the rail said
#: nothing" and "the rail said something unusable" reach the same
#: `unverified` OUTCOME — neither is evidence of a discrepancy, and inventing
#: a `fraud_flag` out of garbage would flag every payment on a broken
#: integration — but they must stay distinguishable on the audit row, and they
#: diverge downstream: `persistable_settled_amount` records the unusable figure
#: as `settled_amount_unstorable`, so `settlement_coverage` returns `uncertain`
#: and the invoice HOLDS, while a genuinely absent figure fails open.
REASON_REPORTED_AMOUNT_UNUSABLE = "provider_reported_unusable_amount"


@dataclass(frozen=True)
class AuthorizedLeg:
    """One side of what AP authorized: an exact amount and its currency.

    ``currency`` is ``None`` when the caller genuinely cannot establish it
    (an invoice row that has since been deleted). A ``None`` currency is a
    wildcard for the currency check — we never manufacture a currency
    mismatch out of missing data — while the amount comparison still runs.
    """

    amount: Decimal
    currency: str | None
    leg: str  # "target" (invoice currency) | "source" (org home currency)


@dataclass(frozen=True)
class SettlementVerification:
    """Verdict on one reported settlement.

    ``variance`` is signed and 2dp: ``reported - authorized``, so **positive
    means the processor moved MORE than we authorized** — the same sign
    convention ``bank_reconciliation.match_variance`` uses for a bank debit.
    It is ``None`` whenever no numeric comparison was possible (nothing
    reported, or the reported currency matched no authorized leg).
    """

    outcome: str
    reason: str | None = None
    variance: Decimal | None = None
    settled_amount: Decimal | None = None
    settled_currency: str | None = None
    authorized_amount: Decimal | None = None
    authorized_currency: str | None = None
    authorized_leg: str | None = None

    @property
    def is_discrepancy(self) -> bool:
        return self.outcome in DISCREPANCY_OUTCOMES

    def as_details(self) -> dict:
        """PII-free dict for an ``audit_log.details`` payload.

        Money serialises as an exact decimal STRING, never a float (project
        invariant). Nothing here names a vendor, a bank account or a tax id —
        only amounts, currency codes and the verdict.
        """
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "settled_amount": (None if self.settled_amount is None else str(self.settled_amount)),
            "settled_currency": self.settled_currency,
            "authorized_amount": (
                None if self.authorized_amount is None else str(self.authorized_amount)
            ),
            "authorized_currency": self.authorized_currency,
            "authorized_leg": self.authorized_leg,
            "variance": None if self.variance is None else str(self.variance),
        }


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(_CENTS, rounding=ROUND_HALF_UP)


def usable_reported_amount(value) -> Decimal | None:
    """The 2 dp form of a reported settlement figure, or ``None`` if unusable.

    ``_q`` raises ``InvalidOperation`` on a non-finite ``Decimal`` (``NaN`` /
    ``Infinity``) and on a magnitude past the decimal context's precision. That
    raise used to happen INSIDE ``verify_settlement``, i.e. before the figure
    ever reached ``persistable_settled_amount`` — which handles exactly these
    values, deliberately and explicitly, via ``fits_numeric``. So the one class
    of report the recording layer was designed to quarantine instead took the
    whole webhook transaction down: the handler 5xx'd, the processor retried
    into the identical failure, and nothing about the money movement was
    recorded. A retry storm, not a wrong figure — but the payment was left in
    flight forever either way.

    The NaN-aware guard therefore runs FIRST, and the verdict for such a report
    is an explicit named one (``unverified`` /
    ``REASON_REPORTED_AMOUNT_UNUSABLE``), never a 500 and never a silent
    ``matched``.
    """
    candidate = coerce_reported_amount(value)
    if candidate is None or not candidate.is_finite():
        return None
    try:
        return _q(candidate)
    except InvalidOperation:
        return None


def coerce_reported_amount(value) -> Decimal | None:
    """The reported figure as a ``Decimal``, or ``None`` if it is not a number.

    Separate from :func:`usable_reported_amount` because ``NaN`` IS a Decimal:
    it is unusable for arithmetic but must still be carried onto the verdict so
    the recording layer can quarantine it. Only a value that is not a number at
    all (a caller-contract violation) yields ``None`` here.
    """
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _same_currency(a: str | None, b: str | None) -> bool:
    """A ``None`` on either side is a wildcard — see ``AuthorizedLeg``."""
    if a is None or b is None:
        return True
    return a.strip().upper() == b.strip().upper()


def build_authorized_legs(
    *,
    target_amount: Decimal,
    target_currency: str | None,
    source_amount: Decimal | None = None,
    source_currency: str | None = None,
) -> list[AuthorizedLeg]:
    """The set of amounts a processor may legitimately report for a payment.

    The target leg (``Payment.amount`` in the invoice's currency) is always
    present. The source leg (``Payment.source_amount`` in the org's home
    currency) is added only for a payment that actually carries one — i.e. one
    that went through the international/FX path. When the two legs are the
    same amount in the same currency (a domestic payment where
    ``prepare_international_payment`` still stamped the home currency) the
    duplicate is dropped so the verdict names one leg, not an arbitrary one
    of two identical ones.
    """
    legs = [AuthorizedLeg(amount=_q(target_amount), currency=target_currency, leg="target")]
    if source_amount is not None:
        source = AuthorizedLeg(amount=_q(source_amount), currency=source_currency, leg="source")
        if not (
            source.amount == legs[0].amount and _same_currency(source.currency, legs[0].currency)
        ):
            legs.append(source)
    return legs


def verify_settlement(
    *,
    reported_amount: Decimal | None,
    reported_currency: str | None,
    target_amount: Decimal,
    target_currency: str | None,
    source_amount: Decimal | None = None,
    source_currency: str | None = None,
    tolerance: Decimal = SETTLEMENT_AMOUNT_TOLERANCE,
) -> SettlementVerification:
    """Compare what the processor says it settled against what AP authorized.

    Outcomes:

    ``matched``
        The reported amount is within ``tolerance`` of an authorized leg
        whose currency the report is compatible with. The ordinary path.

    ``amount_mismatch``
        A currency-compatible leg exists but no leg's amount is within
        tolerance. ``authorized_*`` names the CLOSEST such leg and
        ``variance`` is the signed gap against it.

    ``currency_mismatch``
        The processor reported a currency that matches no authorized leg.
        No numeric comparison is meaningful across currencies, so
        ``variance`` stays ``None`` and ``authorized_*`` reports the target
        leg for context.

    ``unverified``
        The provider's webhook carried no amount at all (an adapter whose
        payload genuinely omits it), or carried one that is not a usable
        number (``NaN`` / ``Infinity`` / a magnitude past the decimal
        context). Fails OPEN as a VERDICT by design: neither is evidence, and
        inventing a discrepancy from either would flag every payment on that
        rail. The two are distinguished by ``reason``
        (``REASON_NO_REPORTED_AMOUNT`` vs ``REASON_REPORTED_AMOUNT_UNUSABLE``)
        and they diverge downstream — an unusable figure is persisted as
        ``settled_amount_unstorable``, so ``settlement_coverage`` returns
        ``uncertain`` and the invoice holds. The caller records the outcome on
        the audit row so the blind spot is visible rather than silent, and bank
        reconciliation remains the downstream net.
    """
    legs = build_authorized_legs(
        target_amount=target_amount,
        target_currency=target_currency,
        source_amount=source_amount,
        source_currency=source_currency,
    )
    target_leg = legs[0]

    if reported_amount is None:
        return SettlementVerification(
            outcome=OUTCOME_UNVERIFIED,
            reason=REASON_NO_REPORTED_AMOUNT,
            settled_currency=(reported_currency or None),
            authorized_amount=target_leg.amount,
            authorized_currency=target_leg.currency,
            authorized_leg=target_leg.leg,
        )

    usable = usable_reported_amount(reported_amount)
    if usable is None:
        # A visible blind spot, never a silent pass. The unusable figure itself
        # is carried through on `settled_amount` so it reaches
        # `persistable_settled_amount`, which flags it unstorable — that is
        # what makes `settlement_coverage` return `uncertain` and HOLD the
        # invoice, rather than laundering garbage into the fails-open NULL that
        # means "no rail ever reported a figure".
        return SettlementVerification(
            outcome=OUTCOME_UNVERIFIED,
            reason=REASON_REPORTED_AMOUNT_UNUSABLE,
            settled_amount=coerce_reported_amount(reported_amount),
            settled_currency=reported_currency,
            authorized_amount=target_leg.amount,
            authorized_currency=target_leg.currency,
            authorized_leg=target_leg.leg,
        )

    settled = usable
    candidates = [leg for leg in legs if _same_currency(reported_currency, leg.currency)]

    if not candidates:
        return SettlementVerification(
            outcome=OUTCOME_CURRENCY_MISMATCH,
            reason=REASON_CURRENCY_NOT_AUTHORIZED,
            settled_amount=settled,
            settled_currency=reported_currency,
            authorized_amount=target_leg.amount,
            authorized_currency=target_leg.currency,
            authorized_leg=target_leg.leg,
        )

    for leg in candidates:
        if abs(settled - leg.amount) <= tolerance:
            return SettlementVerification(
                outcome=OUTCOME_MATCHED,
                variance=_q(settled - leg.amount),
                settled_amount=settled,
                settled_currency=reported_currency,
                authorized_amount=leg.amount,
                authorized_currency=leg.currency,
                authorized_leg=leg.leg,
            )

    closest = min(candidates, key=lambda leg: abs(settled - leg.amount))
    return SettlementVerification(
        outcome=OUTCOME_AMOUNT_MISMATCH,
        reason=REASON_AMOUNT_DIFFERS,
        variance=_q(settled - closest.amount),
        settled_amount=settled,
        settled_currency=reported_currency,
        authorized_amount=closest.amount,
        authorized_currency=closest.currency,
        authorized_leg=closest.leg,
    )


#: Coverage states — "does the settlement on record discharge the invoice?"
#: Distinct from the verification OUTCOME above, which answers "did the rail
#: report what we authorized?". The two differ on over-settlement: a processor
#: that moved MORE than instructed is an ``amount_mismatch`` worth flagging, but
#: the vendor is not short and the invoice IS discharged.
COVERAGE_COVERED = "covered"
COVERAGE_SHORT = "short"
COVERAGE_UNCERTAIN = "uncertain"

REASON_NO_SETTLED_AMOUNT = "no_settled_amount_on_record"
REASON_SETTLED_SHORT = "settled_below_every_authorized_leg"
REASON_COVERAGE_CURRENCY_UNKNOWN = "settled_currency_matches_no_authorized_leg"
REASON_SETTLED_AMOUNT_UNSTORABLE = "settled_amount_exceeded_the_column"

#: The shape of ``payments.settled_amount``. Kept beside the code that decides
#: what may be written into it so the two cannot drift.
SETTLED_AMOUNT_NUMERIC = (15, 2)


def persistable_settled_amount(amount: Decimal | None) -> tuple[Decimal | None, bool]:
    """Split a reported figure into ``(what to store, was it unstorable)``.

    ``payments.settled_amount`` is ``NUMERIC(15, 2)``. A processor reporting
    more than 13 integer digits used to parse, verify, and then raise
    ``NumericValueOutOfRangeError`` at the flush — taking the whole webhook
    transaction with it, including the ``fraud_flag`` the verdict had already
    decided on and the record that the payment completed at all. The handler
    5xx'd and the processor retried into the identical failure, so the most
    suspicious settlement a rail can report was the one nothing was recorded
    about.

    The unstorable case returns ``(None, True)`` rather than ``(None, False)``
    on purpose. A bare NULL already means "no rail ever reported a figure",
    which coverage deliberately fails OPEN on; collapsing a garbage report into
    that would mark the invoice paid on the strength of a number we know is
    wrong. The flag is what keeps the two distinguishable.

    Both call sites — the webhook and the reconciler backstop — go through
    here so they cannot disagree about what is storable.
    """
    if amount is None:
        return None, False
    # The same NaN-aware front guard the verifier runs. `fits_numeric` already
    # rejects a non-finite Decimal explicitly, but it reaches for `.is_finite()`
    # first, so anything that is not a Decimal at all would AttributeError here
    # — on the money path, for a report we have already decided is garbage.
    if usable_reported_amount(amount) is None:
        return None, True
    if fits_numeric(amount, *SETTLED_AMOUNT_NUMERIC):
        return amount, False
    return None, True


@dataclass(frozen=True)
class SettlementCoverage:
    """Whether the recorded settlement discharges the invoice.

    ``shortfall`` is positive and 2dp — how far the settlement fell below the
    most generous authorized leg — and is ``None`` unless the state is
    ``short``.
    """

    state: str
    reason: str | None = None
    shortfall: Decimal | None = None

    @property
    def completes_invoice(self) -> bool:
        """True when nothing on record contradicts the invoice being settled.

        Deliberately phrased as the absence of contradiction rather than
        positive proof: ``covered`` is also what an unreported settlement
        returns. See ``settlement_coverage``.
        """
        return self.state == COVERAGE_COVERED


def settlement_coverage(
    *,
    settled_amount: Decimal | None,
    settled_currency: str | None,
    target_amount: Decimal,
    target_currency: str | None,
    source_amount: Decimal | None = None,
    source_currency: str | None = None,
    settled_amount_unstorable: bool = False,
    tolerance: Decimal = SETTLEMENT_AMOUNT_TOLERANCE,
) -> SettlementCoverage:
    """Decide whether a recorded settlement discharges the invoice.

    This is what lets ``payment_erp_sync`` hold an under-settled invoice short
    of ``paid`` instead of closing it out as settled in full. It reads the
    figure PERSISTED on the payment row (``Payment.settled_amount``), not a
    live webhook event, so the condition is a durable fact about the payment
    rather than the transient state of an exception a human is expected to
    clear. That distinction is the whole reason the earlier attempt at this
    hold had to be reverted: keyed on a resolvable flag, clearing the flag —
    the correct response to an over-settlement — stranded the invoice
    permanently, because nothing re-invokes the sweep that would then have
    marked it paid.

    States:

    ``covered``
        Some authorized leg is fully covered by what settled, **or** nothing
        was ever reported. Fails OPEN on the absent case on purpose: NULL
        means an amount-free rail (Dwolla's bare envelope) or a row predating
        migration 0083, and treating "we don't know" as a shortfall would hold
        every invoice those rails settle. Absence is not evidence — the same
        posture ``verify_settlement`` takes with ``unverified``.

    ``short``
        A figure was reported and it falls below EVERY currency-compatible
        authorized leg by more than ``tolerance``. The vendor is short; the
        invoice must not read as settled in full.

    ``uncertain``
        A figure was reported that we cannot evaluate. Two ways in: it came in
        a currency matching no authorized leg (comparing across currencies
        without a rate would be inventing an answer), or it did not fit
        ``payments.settled_amount`` at all and only the
        ``settled_amount_unstorable`` flag survives (migration 0085). Either
        way it holds like a shortfall and a human reconciles.

        The unstorable case is checked FIRST and deliberately does not fall
        through to the NULL branch below: ``settled_amount`` is NULL in both,
        but NULL alone means "nothing was ever reported" and fails OPEN. A
        garbage report is not an absent one, and must not be laundered into
        "nothing contradicts this invoice being settled".

    Over-settlement is ``covered``: the vendor received at least what was
    authorized, so the invoice is discharged. It is still flagged by
    ``verify_settlement`` as an ``amount_mismatch`` — recording that too much
    moved is a separate concern from whether the payable is satisfied.
    """
    if settled_amount_unstorable:
        return SettlementCoverage(state=COVERAGE_UNCERTAIN, reason=REASON_SETTLED_AMOUNT_UNSTORABLE)

    if settled_amount is None:
        return SettlementCoverage(state=COVERAGE_COVERED, reason=REASON_NO_SETTLED_AMOUNT)

    legs = build_authorized_legs(
        target_amount=target_amount,
        target_currency=target_currency,
        source_amount=source_amount,
        source_currency=source_currency,
    )
    candidates = [leg for leg in legs if _same_currency(settled_currency, leg.currency)]
    if not candidates:
        return SettlementCoverage(state=COVERAGE_UNCERTAIN, reason=REASON_COVERAGE_CURRENCY_UNKNOWN)

    settled = _q(settled_amount)
    # Covering ANY authorized leg discharges the invoice — a cross-currency
    # payment legitimately settles on either side, and the processor chooses
    # which one it reports.
    if any(settled >= leg.amount - tolerance for leg in candidates):
        return SettlementCoverage(state=COVERAGE_COVERED)

    # Short against every leg. Measure the gap against the SMALLEST one: that
    # is the most generous reading of what was owed, so the shortfall reported
    # is the least we can claim was missed.
    smallest = min(leg.amount for leg in candidates)
    return SettlementCoverage(
        state=COVERAGE_SHORT,
        reason=REASON_SETTLED_SHORT,
        shortfall=_q(smallest - settled),
    )


def discrepancy_finding(verification: SettlementVerification) -> dict:
    """The catalogue finding an ``Exception`` description carries for a
    settlement discrepancy (decisions §358).

    Names amounts, currencies and the verdict — never a vendor, a bank
    account or an invoice number (the exception row already carries the
    invoice FK). The figures are bare ``number`` params beside their own
    currency codes, because the two sides of a currency mismatch are in two
    currencies; a code the processor did not report renders as ``?``.
    Raises for a verdict that is not a discrepancy: both discrepancy outcomes
    always carry a reported and an authorized figure, and nothing else opens
    a settlement exception.
    """
    if not verification.is_discrepancy:
        raise ValueError(f"not a settlement discrepancy: {verification.outcome}")
    figures = {
        "settledAmount": str(verification.settled_amount),
        "settledCurrency": verification.settled_currency or "?",
        "authorizedAmount": str(verification.authorized_amount),
        "authorizedCurrency": verification.authorized_currency or "?",
    }
    if verification.outcome == OUTCOME_CURRENCY_MISMATCH:
        return exception_finding("settlement_currency_mismatch", **figures)
    variance = verification.variance or Decimal("0")
    if variance > 0:
        return exception_finding("settlement_amount_over", variance=str(variance), **figures)
    return exception_finding("settlement_amount_under", variance=str(variance), **figures)


def describe_discrepancy(verification: SettlementVerification) -> str:
    """The English of :func:`discrepancy_finding` — a one-line, PII-free summary."""
    return discrepancy_finding(verification)["message"]
