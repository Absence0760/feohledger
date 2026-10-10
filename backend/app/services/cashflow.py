"""Cash-position helpers — bank-balance auto-sync + persisted alert thresholds.

Three pieces backing the cash-position dashboard (`GET /api/analytics/cash_position`
and the threshold GET/PUT) and the cash-flow copilot, kept out of
`api/analytics.py` so the route file stays SQL-and-shaping only:

1. ``fetch_provider_balance`` — best-effort read of the org's funding-account
   balance from its configured payment adapter's optional ``get_balance``
   capability. Lets the CFO skip typing an opening balance by hand. Falls back
   (returns ``None``) when no provider supports it or the fetch fails — it NEVER
   raises, so a bank-link outage can't 500 the dashboard. The local-first `mock`
   adapter returns a deterministic figure, so `pnpm dev` needs no real bank
   credential.

2. ``resolve_opening_balance`` — the whole resolution CHAIN (explicit → provider
   auto-sync → persisted settings → zero) plus its provenance, in one place so
   every consumer resolves the same number the same way and can say where it
   came from.

3. ``resolve_cash_thresholds`` / ``store_cash_thresholds`` — read/normalise the
   per-org alert thresholds persisted on ``Organization.settings.cashflow``
   (JSON — no migration). The cash-position endpoint reads the persisted
   ``min_balance_threshold`` when the request doesn't override it.

Money is `Decimal` end-to-end; nothing here logs an account number (the adapter
returns only an opaque ``account_ref`` label, never a full PAN/account number).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)


@dataclass
class ProviderBalance:
    """A funding-account balance resolved from the configured payment provider.

    `account_ref` is the adapter's opaque account label (e.g. "mock-operating"),
    safe to show in the UI — never a full account/routing number."""

    amount: Decimal
    currency: str
    provider: str
    account_ref: str | None = None


async def fetch_provider_balance(payment_config: dict | None) -> ProviderBalance | None:
    """Best-effort current funding-account balance from the org's payment adapter.

    Returns ``None`` (caller falls back to the manual opening balance) when:
      - the adapter doesn't implement the optional ``get_balance`` capability
        (its `BalanceResult.available` is False — the base-class default), or
      - the adapter raised (transport / credential failure).

    Never raises and never logs the balance figure or any account number — only
    the provider name + a coarse reason, so a bank-link outage degrades to the
    BYO opening balance instead of breaking the cash-position view.
    """
    # Import here (not module top) so this service has no import-time dependency
    # on the adapter registry; matches the lazy-import posture elsewhere.
    from app.services.payment_adapters import get_payment_adapter, list_available_providers

    try:
        adapter = get_payment_adapter(payment_config)
        result = await adapter.get_balance()
    except Exception:  # noqa: BLE001 — a provider failure must not break the dashboard
        # `payment_config` carries the unsealed credentials (decisions §266), so
        # nothing read from it reaches the log: the name logged is the
        # registry's own string, matched against the configured one.
        configured = (payment_config or {}).get("provider") or "mock"
        provider = next((n for n in list_available_providers() if n == configured), "unregistered")
        logger.warning("cash-position balance fetch failed for provider %s", provider)
        return None

    if not result.available:
        return None
    return ProviderBalance(
        amount=Decimal(str(result.amount)),
        currency=result.currency,
        provider=adapter.provider_name,
        account_ref=result.account_ref,
    )


# ---------------------------------------------------------------------------
# Opening-balance resolution + provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpeningBalance:
    """The opening balance a cash-position curve starts from, *plus where it
    came from*.

    A projected shortfall is only actionable if the reader can tell whether the
    starting figure is a live bank balance, a number someone typed into
    settings months ago, or the ``0`` we assume when neither exists — so the
    provenance travels with the amount rather than being reconstructed by each
    caller.

    ``currency`` is the org's reporting currency, i.e. the currency the whole
    curve (and every outflow subtracted from it) is denominated in — see
    ``resolve_opening_balance`` for why a provider balance in any OTHER
    currency is refused rather than mixed in.
    """

    amount: Decimal
    source: str  # "explicit" | "provider" | "settings" | "none"
    currency: str
    provider: str | None = None  # adapter name, when source == "provider"
    account_ref: str | None = None  # opaque account label — never an account number
    # Set to "currency_mismatch" when a live provider balance WAS available but
    # was refused because its account is denominated in another currency. The
    # amount then comes from the next link in the chain; this field is what
    # stops that fallback from looking like "no bank is connected".
    provider_skipped: str | None = None


def _normalize_currency(code: str | None) -> str | None:
    if not isinstance(code, str):
        return None
    normalized = code.strip().upper()
    return normalized or None


async def resolve_opening_balance(
    *,
    org_settings: dict | None,
    reporting_currency: str,
    explicit_opening: Decimal | None = None,
    use_provider: bool = True,
    org_id: uuid.UUID | None,
) -> OpeningBalance:
    """Resolve the cash-position opening balance, first hit wins:

    1. ``explicit_opening`` — a bring-your-own figure from the caller.
    2. The org's payment provider's live funding-account balance, when the org
       has a payments provider configured and its adapter supports the optional
       ``get_balance`` capability (``use_provider=False`` skips this).
    3. ``Organization.settings.cashflow.opening_balance`` — a persisted BYO figure.
    4. ``0``.

    **A provider balance denominated in a currency other than the org's
    reporting currency is refused** (falling through to 3/4 with
    ``provider_skipped="currency_mismatch"``). Every outflow subtracted from
    the opening balance is expressed in the reporting currency, so seeding the
    curve from, say, a EUR operating account while the org reports in USD
    produces a running balance that is silently a mixture of two currencies —
    and the shortfall alerts / plan proposals priced off it would be wrong by
    the exchange rate. Converting is not an option either: an FX rate fetched
    on a read would make the curve non-deterministic and unreproducible
    (docs/decisions.md §18), and the fix an operator actually needs is to set
    the reporting currency or a BYO opening balance, not to have us guess.

    Never raises: a malformed persisted value degrades to the next link, and
    ``fetch_provider_balance`` already swallows a bank-link outage.

    ``org_id`` lets the provider link authenticate: the processor's API key is
    sealed (`services/provider_credentials`) and is merged into the payments
    block only through that module's accessor. It is REQUIRED (no default) so a
    caller cannot silently drop the provider link — pass ``None`` only where
    there is genuinely no org (a pure test). A credential that cannot be opened
    degrades like a bank-link outage.
    """
    settings_dict = org_settings or {}
    currency = _normalize_currency(reporting_currency) or "USD"

    if explicit_opening is not None:
        return OpeningBalance(amount=explicit_opening, source="explicit", currency=currency)

    provider_skipped: str | None = None
    payments_config = settings_dict.get("payments")
    if use_provider and payments_config and org_id is not None:
        from app.services.provider_credentials import resolve_block

        try:
            payments_config = await resolve_block(org_id, settings_dict, "payments")
        except Exception:  # noqa: BLE001 — never raises; degrade to the next link
            logger.warning("cash-position: the payments credentials could not be opened")
            payments_config = None
    if use_provider and payments_config:
        provider_balance = await fetch_provider_balance(payments_config)
        if provider_balance is not None:
            account_currency = _normalize_currency(provider_balance.currency)
            if account_currency == currency:
                return OpeningBalance(
                    amount=provider_balance.amount,
                    source="provider",
                    currency=currency,
                    provider=provider_balance.provider,
                    account_ref=provider_balance.account_ref,
                )
            provider_skipped = "currency_mismatch"
            # Currency codes are not PII; the balance figure and the account
            # reference deliberately stay out of the log line.
            logger.warning(
                "cash-position: ignoring provider opening balance — funding account is "
                "denominated in %s but the org reports in %s",
                account_currency or "unknown",
                currency,
            )

    stored = (settings_dict.get("cashflow") or {}).get("opening_balance")
    stored_amount = _coerce_decimal(stored, field="opening_balance")
    if stored_amount is not None:
        return OpeningBalance(
            amount=stored_amount,
            source="settings",
            currency=currency,
            provider_skipped=provider_skipped,
        )

    return OpeningBalance(
        amount=Decimal("0"),
        source="none",
        currency=currency,
        provider_skipped=provider_skipped,
    )


# ---------------------------------------------------------------------------
# Persisted cash-position alert thresholds (Organization.settings.cashflow)
# ---------------------------------------------------------------------------


@dataclass
class CashThresholds:
    """Per-org persisted cash-position alert thresholds.

    ``min_balance_threshold`` — the low-balance warning level a projected
    period's closing balance must stay at or above; periods below it are flagged
    and collected as breaches. ``None`` means "no persisted threshold" (the
    dashboard simply doesn't flag breaches unless the request passes one)."""

    min_balance_threshold: Decimal | None = None


def _coerce_decimal(raw, *, field: str = "cashflow value") -> Decimal | None:
    """Parse a persisted/JSON money value into Decimal, tolerating a malformed
    stored value by returning ``None`` (a corrupt settings blob must never break
    the read) — but never SILENTLY.

    Returning ``None`` for ``min_balance_threshold`` is not a neutral fallback:
    that threshold IS the per-org opt-in to shortfall alerting
    (``cash_flow_alerts._project_tenant`` returns early on ``None``), so a
    single malformed character in the settings JSON un-subscribes the org from
    the alert it configured, forever, with nothing anywhere saying so. A log
    line naming the FIELD (never the value — a settings blob is operator data)
    is what makes that recoverable.

    ``Decimal("NaN")`` is the same failure wearing a valid-parse costume:
    ``Decimal(str("nan"))`` succeeds, and every subsequent comparison against
    it (``closing < threshold``) is False, so the org is opted out of alerting
    just as completely while looking configured. Infinities likewise — an
    infinite threshold breaches on every period, or never. Only a finite
    number is a threshold.
    """
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning("[cashflow] ignoring malformed persisted %s in organization settings", field)
        return None
    if not value.is_finite():
        logger.warning(
            "[cashflow] ignoring non-finite persisted %s in organization settings", field
        )
        return None
    return value


def resolve_cash_thresholds(settings: dict | None) -> CashThresholds:
    """Read the persisted thresholds off ``Organization.settings.cashflow``.

    Tolerant: a missing/malformed ``cashflow`` block or a non-numeric stored
    value yields an all-``None`` ``CashThresholds`` (no thresholds) rather than
    raising."""
    cashflow = (settings or {}).get("cashflow")
    if not isinstance(cashflow, dict):
        return CashThresholds()
    return CashThresholds(
        min_balance_threshold=_coerce_decimal(
            cashflow.get("min_balance_threshold"), field="min_balance_threshold"
        ),
    )


def store_cash_thresholds(settings: dict | None, thresholds: CashThresholds) -> dict:
    """Return a NEW settings dict with the thresholds written under ``cashflow``.

    Preserves any other keys already on the ``cashflow`` block (e.g. a manually
    set ``opening_balance``) and stores the threshold as a string so the money
    value round-trips through JSON without going through a float. A ``None``
    threshold clears the persisted key."""
    new_settings = dict(settings or {})
    cashflow = dict(new_settings.get("cashflow") or {})
    if thresholds.min_balance_threshold is None:
        cashflow.pop("min_balance_threshold", None)
    else:
        cashflow["min_balance_threshold"] = str(thresholds.min_balance_threshold)
    new_settings["cashflow"] = cashflow
    return new_settings


# ---------------------------------------------------------------------------
# Projected-shortfall alert marker (Organization.settings.cashflow)
# ---------------------------------------------------------------------------
#
# The `cash_flow_alerts` sweep records WHICH forecast period it last alerted an
# org about, so a standing shortfall is announced once instead of every tick.
# Same JSON block + same "preserve the other keys" discipline as the thresholds
# above — the shape of `settings.cashflow` has exactly one owner, this module.


def resolve_shortfall_alert_period(settings: dict | None) -> str | None:
    """The forecast period key the shortfall sweep last alerted this org about.

    ``None`` = never alerted, or the shortfall previously cleared. Tolerant of a
    missing/malformed block (a corrupt settings blob must not stop the sweep)."""
    cashflow = (settings or {}).get("cashflow")
    if not isinstance(cashflow, dict):
        return None
    marker = cashflow.get("shortfall_alert")
    if not isinstance(marker, dict):
        return None
    period = marker.get("period")
    return period if isinstance(period, str) and period else None


def store_shortfall_alert_period(
    settings: dict | None, *, period: str | None, sent_on: str | None = None
) -> dict:
    """Return a NEW settings dict recording the alerted period under
    ``cashflow.shortfall_alert``.

    ``period=None`` clears the marker — the projected shortfall resolved, so the
    alert re-arms and the org is told again if it comes back."""
    new_settings = dict(settings or {})
    cashflow = dict(new_settings.get("cashflow") or {})
    if period is None:
        cashflow.pop("shortfall_alert", None)
    else:
        marker: dict[str, str] = {"period": period}
        if sent_on:
            marker["sent_on"] = sent_on
        cashflow["shortfall_alert"] = marker
    new_settings["cashflow"] = cashflow
    return new_settings
