"""Whether FeohLedger may move money for this tenant, or only record it.

The pilot's commercial model (issue #517, `docs/decisions.md` §251) is that
**FeohLedger does not move money**: a customer pays its suppliers from its own
bank or ERP, and FeohLedger records that the payment happened. Every money-
moving entry point — run execute / resume / retry-failed, the compliance
release, and the standalone `POST /api/payments` booking — asks this module
first, through `api/payments._require_payment_adapter` / `refuse_record_only`.

Two modes:

* ``processor`` — payments are dispatched to ``settings.payments.provider``.
* ``record_only`` — nothing is dispatched. A user records a payment made
  outside FeohLedger (`POST /api/payments/record-outside`), or the customer's
  ERP reports it paid (`api/erp_webhook`), and the ledger books it.

**Fail closed.** The hazard is the `mock` adapter: it reports every payment
`completed` without moving money, so an Execute click on a tenant that never
configured a processor would flip its invoices to `paid` with nothing sent. The
resolution therefore refuses to call `mock` a rail anywhere money is real:

* explicit ``mode: "record_only"`` → record-only, whatever the provider says;
* an unknown ``mode`` value → record-only (never guess towards moving money);
* a deployed environment (`app.config.Settings.is_deployed`) whose provider is
  absent or ``mock`` → record-only, **even with ``mode: "processor"``** — there
  is no setting that makes the mock a rail in production;
* otherwise (``processor`` or absent) → processor. Local dev and CI keep the
  `mock` default, which is guard rail 7 (local-first) — the mock is honest
  there because nobody's money is involved.

Pure: reads the org's settings dict and the deployment flag, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MODE_PROCESSOR = "processor"
MODE_RECORD_ONLY = "record_only"

PAYMENT_MODES: tuple[str, ...] = (MODE_PROCESSOR, MODE_RECORD_ONLY)

#: Why the mode resolved as it did. A fixed, PII-free vocabulary — these ride
#: the refusal body and the `GET /api/payments/execution-mode` response.
REASON_CONFIGURED = "configured"
REASON_UNKNOWN_MODE = "unknown_mode"
REASON_NO_PROCESSOR_DEPLOYED = "no_processor_in_deployed_environment"

ExecutionMode = Literal["processor", "record_only"]


@dataclass(frozen=True)
class PaymentExecutionMode:
    mode: ExecutionMode
    reason: str

    @property
    def record_only(self) -> bool:
        return self.mode == MODE_RECORD_ONLY


def _is_deployed() -> bool:
    from app.config import settings as app_settings

    return app_settings.is_deployed


def resolve_execution_mode(
    org_settings: dict | None, *, deployed: bool | None = None
) -> PaymentExecutionMode:
    """Resolve the tenant's payment execution mode. See the module docstring.

    ``deployed`` defaults to the running environment; tests pass it explicitly.
    """
    payments = (org_settings or {}).get("payments") or {}
    if not isinstance(payments, dict):
        payments = {}
    raw_mode = payments.get("mode")
    if deployed is None:
        deployed = _is_deployed()

    if raw_mode == MODE_RECORD_ONLY:
        return PaymentExecutionMode(MODE_RECORD_ONLY, REASON_CONFIGURED)
    if raw_mode is not None and raw_mode != MODE_PROCESSOR:
        return PaymentExecutionMode(MODE_RECORD_ONLY, REASON_UNKNOWN_MODE)

    provider = payments.get("provider") or "mock"
    if deployed and provider == "mock":
        return PaymentExecutionMode(MODE_RECORD_ONLY, REASON_NO_PROCESSOR_DEPLOYED)
    return PaymentExecutionMode(MODE_PROCESSOR, REASON_CONFIGURED)
