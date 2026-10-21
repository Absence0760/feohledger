"""Payment-rail classification — one source of truth for what a
``Payment.method`` value *means* to downstream regulatory reporting **and to
the international money path**.

Two orthogonal questions are answered here, both keyed on the same rail string,
because a rail added for one and forgotten by the other is the failure mode:

1. **Is this rail reported by us on a 1099, or by the card settlement entity on
   a 1099-K?** (``CARD_PAYMENT_METHODS`` / ``IRS_1099_REPORTABLE_METHODS``)
2. **Is this rail international?** (``INTERNATIONAL_PAYMENT_METHODS`` /
   ``DOMESTIC_PAYMENT_METHODS``) — which decides whether an FX rate is locked,
   whether a corridor override is trustworthy, and whether KYC is required.

The rail matters most for **IRS 1099 reporting**. Payments settled over a
payment card are reported by the card settlement entity on Form **1099-K**,
and the payer must therefore leave them OFF Form 1099-NEC / 1099-MISC:

    "Payments made with a credit card or payment card and certain other
    types of payments, including third-party network transactions, must be
    reported on Form 1099-K by the payment settlement entity ... and are not
    subject to reporting on Form 1099-NEC or Form 1099-MISC."
    — IRS, General Instructions for Certain Information Returns
      (see also the Form 1099-NEC/MISC instructions, "Exceptions").

Including a card payment in the payer's 1099 box amount therefore
**over-reports** the vendor's income and double-counts the same dollar
against the processor's 1099-K. Under-reporting is equally wrong, so the
classification is an explicit, exhaustive registry rather than a guess.

Rail-by-rail:

===================== ============ ===============================================
``Payment.method``    1099 treated Why
===================== ============ ===============================================
``ach``               reportable   Bank ACH credit — a direct payment by the payer.
``wire``              reportable   Domestic bank wire.
``rtp``               reportable   RTP / instant bank rail (The Clearing House).
``check``             reportable   Paper cheque drawn on the payer's account.
``sepa``              reportable   SEPA Credit Transfer — a bank rail.
``international_ach`` reportable   NACHA Global ACH / IAT — a bank rail.
``international_wire``reportable   SWIFT wire.
``bacs``              reportable   UK BACS — a batched domestic bank credit.
``faster_payments``  reportable   UK Faster Payments — instant domestic bank credit.
``chaps``             reportable   UK CHAPS — same-day high-value domestic bank credit.
``virtual_card``      EXCLUDED     Payment card. The card network / issuer is the
                                   settlement entity and files the 1099-K.
``card``              EXCLUDED     A company payment card used outside FeohLedger
                                   (an ERP-reported or recorded payment); same
                                   1099-K reasoning. Never dispatched.
===================== ============ ===============================================

An unknown or ``NULL`` rail counts as **reportable**. That is deliberate:
NULL is what the manual / legacy payment paths write (``POST /api/payments``
leaves ``method`` unset when the caller omits it), those are overwhelmingly
bank payments, and the safe default for an unclassified rail is to keep it on
the 1099 rather than silently drop money out of a filed figure. The drift
guard below is what stops "unknown" from becoming a hiding place.

**Drift guard:** ``tests/test_payment_methods.py`` fails if a rail appears in
the ``PaymentMethod`` schema enum, in any registered payment adapter's
``supported_methods``, or in the corridor selector's method table without
being classified here — on **both** axes. Add the rail to exactly one of the
1099 frozensets *and* one of the geography frozensets and the guard goes green
again; there is no way to add a rail and leave either treatment undecided.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, func

# Card rails: excluded from 1099-NEC/1099-MISC (the settlement entity files
# a 1099-K for these).
CARD_PAYMENT_METHODS: frozenset[str] = frozenset({"virtual_card", "card"})

# Non-card rails: the payer reports these on the 1099.
IRS_1099_REPORTABLE_METHODS: frozenset[str] = frozenset(
    {
        "ach",
        "check",
        "wire",
        "rtp",
        "sepa",
        "international_ach",
        "international_wire",
        # UK domestic bank rails — a direct payment by the payer, same as ACH.
        "bacs",
        "faster_payments",
        "chaps",
    }
)

KNOWN_PAYMENT_METHODS: frozenset[str] = CARD_PAYMENT_METHODS | IRS_1099_REPORTABLE_METHODS

# ---------------------------------------------------------------------------
# Geography: which rails cross a border / a currency by construction.
# ---------------------------------------------------------------------------
# These three exist ONLY as an international routing decision — nothing
# defaults to them. ``payment_runs.create_payment_run`` stamps every line item
# with a plain domestic method regardless of the invoice's real currency or
# country, so a truthy ``ach``/``wire`` is a default, while a truthy ``sepa``
# means a human (or a vendor preference) actually chose that corridor. Three
# live decisions read this set:
#
#   * ``payment_corridor.pick_corridor`` — whether to honour a caller's
#     ``requested_method`` as a real override or fall through to auto-selection.
#   * ``compliance._kyc_required_for`` — the default high-risk corridor set
#     above which a vendor must be KYC-verified (an org may override it via
#     ``settings.compliance.high_risk_corridor_methods``).
#   * ``api/payments`` — whether to run the international leg at all, i.e.
#     whether an FX rate gets locked onto the row.
#
# Before this set existed each of those hand-rolled its own literal, so adding
# a fourth international rail meant remembering all three.
INTERNATIONAL_PAYMENT_METHODS: frozenset[str] = frozenset(
    {
        "sepa",
        "international_ach",
        "international_wire",
    }
)

# Rails that stay inside the payer's own banking system. ``virtual_card`` is
# here because the corridor selector never routes a cross-border payment onto
# a card — card issuance is its own path (``services/card_issuance``) and does
# not participate in FX locking or corridor selection.
DOMESTIC_PAYMENT_METHODS: frozenset[str] = frozenset(
    {
        "ach",
        "check",
        "wire",
        "rtp",
        "virtual_card",
        # A company card used outside FeohLedger (recorded only, decisions
        # §295): never routed by the corridor selector.
        "card",
        # UK domestic rails — money stays inside the UK banking system, no FX,
        # no KYC-corridor risk. `pick_corridor` routes a same-currency GBP/GB
        # payment onto `faster_payments`; `bacs` / `chaps` are override-only.
        "bacs",
        "faster_payments",
        "chaps",
    }
)


def normalize_payment_method(value: str | None) -> str:
    """Canonical form of a stored rail: trimmed + lower-cased. ``None`` → ``""``."""
    return (value or "").strip().lower()


def is_card_payment_method(value: str | None) -> bool:
    """True when the rail is a payment card (1099-K territory, not ours)."""
    return normalize_payment_method(value) in CARD_PAYMENT_METHODS


def is_1099_reportable_method(value: str | None) -> bool:
    """True when the payer reports this rail on a 1099-NEC / 1099-MISC.

    Everything that is not a known card rail is reportable — including an
    unknown or ``NULL`` method. See the module docstring for why.
    """
    return not is_card_payment_method(value)


def is_international_payment_method(value: str | None) -> bool:
    """True when the rail is one of the international corridors.

    An unknown or ``NULL`` rail is **not** international. That is the correct
    default at all three call sites — an unrecognised ``requested_method`` must
    not be honoured as a deliberate international override, and the FX leg is
    additionally reached whenever the invoice currency differs from the org's
    home currency or the vendor carries IBAN/SWIFT fields, so the predicate is
    never the only gate.

    It IS the only gate for the KYC threshold, where "unknown ⇒ not high risk"
    would be fail-open — which is precisely why the drift guard in
    ``tests/test_payment_methods.py`` requires every rail the codebase can
    produce to appear in ``INTERNATIONAL_PAYMENT_METHODS`` or
    ``DOMESTIC_PAYMENT_METHODS``. "Unknown" is unreachable for a real rail, so
    it can't become a hiding place.
    """
    return normalize_payment_method(value) in INTERNATIONAL_PAYMENT_METHODS


def card_payment_method_clause(column: ColumnElement[str | None]) -> ColumnElement[bool]:
    """SQL predicate: the rail in ``column`` is a card rail.

    ``COALESCE`` keeps this two-valued — a ``NULL`` method evaluates to
    ``FALSE`` (not card ⇒ reportable) instead of ``NULL``, so the negation
    ``~card_payment_method_clause(...)`` is safe to use as a filter without
    re-handling NULLs at every call site.
    """
    return func.lower(func.btrim(func.coalesce(column, ""))).in_(sorted(CARD_PAYMENT_METHODS))
