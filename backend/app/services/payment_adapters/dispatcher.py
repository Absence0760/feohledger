"""Payment adapter dispatcher — selects the right processor from org config."""

from __future__ import annotations

from app.services.payment_adapters.base import PaymentAdapter

_ADAPTER_REGISTRY: dict[str, type[PaymentAdapter]] = {}

# How much of an admin-supplied provider name may be echoed back in an error.
# The column it comes from is `String(50)`; bounding it keeps an absurd
# settings value out of a log line or an HTTP body.
_PROVIDER_NAME_ECHO_LIMIT = 50


class UnknownPaymentProviderError(ValueError):
    """`settings.payments.provider` names a processor we have no adapter for.

    Raised instead of silently substituting `mock`, which reports every
    payment `completed` without moving money (see `get_payment_adapter`).
    Callers on the money path turn this into a refusal; best-effort callers
    (balance fetch, upstream void) degrade and record the reason.
    """

    def __init__(self, provider: str):
        # `provider` is admin-supplied config, not PII — but bound it anyway
        # so an oversized value can't bloat a log line or a response body.
        self.provider = str(provider)[:_PROVIDER_NAME_ECHO_LIMIT]
        super().__init__(
            f"No payment adapter registered for provider '{self.provider}'. "
            f"Registered providers: {', '.join(list_available_providers())}."
        )


def register_payment_adapter(provider: str):
    """Decorator to register a payment adapter class."""

    def wrapper(cls: type[PaymentAdapter]):
        _ADAPTER_REGISTRY[provider] = cls
        return cls

    return wrapper


def get_payment_adapter(payment_config: dict | None) -> PaymentAdapter:
    """Build the adapter for the configured provider.

    Config shape (lives in `Organization.settings.payments`). This function
    reads exactly one key — `provider` — and hands the whole dict to the
    adapter, so the remaining keys are per-adapter and only that adapter's
    module defines them:

        {
            "provider": "modern_treasury" | "column" | ... | "mock",
            ...                          # whatever that adapter's __init__ reads
        }

    For Modern Treasury that is `org_id`, `api_key`, `originating_account_id`
    and `webhook_secret` (`modern_treasury.py`); `backend/docs/payments.md`
    carries the per-provider tables. Note `program_type` and `sandbox` are
    NOT read anywhere on the payments path — they are card/extraction keys,
    and this docstring listed them until 2026-09-17, which is how they
    reached the founder runbook as though they did something here.

    **No config → `mock`** (unchanged): that is the local-first default, and
    an org that has never configured a processor is a normal state.

    **A configured provider we have no adapter for → `UnknownPaymentProviderError`.**
    This used to fall back to `mock` too, on the reasoning that a
    misconfigured org shouldn't 500 the payments domain. That trade was
    wrong in this family, because `mock` is not an inert stub: its
    `create_payment` returns `success=True, status=completed` immediately,
    so a single typo in an admin-supplied settings value (`modern-treasury`
    for `modern_treasury`) made every payment in every run report as settled
    while no money moved, and flipped the invoices to `paid`. Its
    `parse_webhook` also performs NO signature verification, so the same
    typo routed the public webhook route — whose `provider == "mock"`
    early-return exists precisely to prevent that — to an unverified parser
    under a different name.

    Failing closed is the same call `decisions.md` §26 made for extraction,
    where an unrecognised provider name reached a *fixture* adapter. There
    the fix was a boot-time allowlist, because the name came from an env
    var; here it comes from per-org DB settings, so the refusal has to live
    at the dispatcher and each caller decides what to do with it.
    """
    config = payment_config or {}
    provider = config.get("provider") or "mock"
    adapter_cls = _ADAPTER_REGISTRY.get(provider)
    if adapter_cls is None:
        raise UnknownPaymentProviderError(provider)
    return adapter_cls(config)


def list_available_providers() -> list[str]:
    return sorted(_ADAPTER_REGISTRY.keys())
