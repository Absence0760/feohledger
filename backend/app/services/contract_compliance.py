"""Contract compliance monitoring — flag invoice spend that falls outside the
terms of its linked contract.

Pure-ish evaluator: given an invoice with a ``contract_id``, it loads the
contract and returns a list of finding dicts (``{type, severity, message}``).
Exception creation + persistence is the caller's job
(``invoice_warnings.refresh_warnings``), so this stays unit-testable in
isolation. Money math is exact — the cumulative-spend sum runs over the
``Numeric`` ``amount`` column and is compared as ``Decimal``.

Checks (all advisory unless the contract is ``not_to_exceed``):
  * spend recorded after the contract's ``end_date`` (expired) or before its
    ``start_date``
  * cumulative linked spend over the contract's ``spend_limit`` — ``error``
    severity when ``not_to_exceed``, else ``warning``. The cumulative sum is
    scoped to the contract's own ``currency`` (never add unlike face values —
    same rule as ``contract_spend`` / ``budget_service``).
  * an invoice in a different currency than a limited contract, or in none,
    cannot be summed against the limit and no rate is guessed to make it so:
    it says so instead (``contract_currency_mismatch`` /
    ``…_invoice_currency_unknown``), at the same severity the spend-limit
    finding would carry (decisions §472).
  * invoice vendor differs from the contract vendor
  * invoice GL account outside the contract's ``terms.allowed_gl_accounts``
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contract import Contract, ContractStatus
from app.models.invoice import Invoice, InvoiceStatus
from app.services.invoice_warning_catalog import warning
from app.utils.dates import utc_today

COMPLIANCE_EXCEPTION_TYPE = "contract_noncompliant"


async def evaluate_contract_compliance(
    db: AsyncSession,
    invoice: Invoice,
    *,
    today: date | None = None,
) -> list[dict]:
    """Return contract-compliance findings for ``invoice``.

    Empty list when the invoice isn't linked to a contract, or the link
    dangles (contract deleted) — a dangling link is not a compliance finding.
    """
    if not invoice.contract_id:
        return []

    contract = (
        await db.execute(select(Contract).where(Contract.id == invoice.contract_id))
    ).scalar_one_or_none()
    if contract is None:
        return []

    findings: list[dict] = []
    ref_date = invoice.invoice_date or today or utc_today()

    # --- term window -------------------------------------------------------
    if contract.end_date and ref_date > contract.end_date:
        findings.append(
            warning(
                "contract_expired",
                "warning",
                invoiceDate=ref_date,
                contractNumber=contract.contract_number,
                endDate=contract.end_date,
            )
        )
    if contract.start_date and ref_date < contract.start_date:
        findings.append(
            warning(
                "contract_not_started",
                "warning",
                invoiceDate=ref_date,
                contractNumber=contract.contract_number,
                startDate=contract.start_date,
            )
        )

    # --- terminated / cancelled contract -----------------------------------
    if contract.status in (ContractStatus.terminated, ContractStatus.cancelled):
        findings.append(
            warning(
                # The status is a word in the sentence, so it selects the code.
                "contract_terminated"
                if contract.status == ContractStatus.terminated
                else "contract_cancelled",
                "warning",
                contractNumber=contract.contract_number,
            )
        )

    # --- vendor mismatch ---------------------------------------------------
    if invoice.vendor_id and contract.vendor_id and invoice.vendor_id != contract.vendor_id:
        findings.append(
            warning(
                "contract_vendor_mismatch",
                "warning",
                contractNumber=contract.contract_number,
            )
        )

    # --- cumulative spend over limit --------------------------------------
    # Measured only when the invoice's own currency is the contract's: an
    # other-currency figure can't be added to a same-currency prior sum and
    # compared against the limit. An invoice recording no currency matches no
    # contract: reading its blank as USD measured a code-less figure against a
    # USD limit (decisions §466). Either case is reported below rather than
    # skipped in silence.
    from app.services.payment_runs import payable_currency_code

    invoice_currency = payable_currency_code(invoice.currency)
    contract_currency = payable_currency_code(contract.currency)
    limited = contract.spend_limit is not None and invoice.amount is not None
    # A not-to-exceed cap is an `error`, as its breach is, and stops every
    # unattended approval (`invoice_warnings.has_contract_cap_warning`): an amount
    # the cap cannot measure is one a person must read (decisions §472).
    cap_severity = "error" if contract.not_to_exceed else "warning"
    if limited and contract_currency is not None and invoice_currency != contract_currency:
        # Never converted: a guessed rate would decide a contractual cap.
        findings.append(
            warning(
                "contract_currency_mismatch",
                cap_severity,
                invoiceCurrency=invoice_currency,
                contractNumber=contract.contract_number,
                contractCurrency=contract_currency,
            )
            if invoice_currency is not None
            else warning(
                "contract_currency_mismatch_invoice_currency_unknown",
                cap_severity,
                contractNumber=contract.contract_number,
                contractCurrency=contract_currency,
            )
        )
    elif limited and invoice_currency is not None and invoice_currency == contract_currency:
        prior = (
            await db.execute(
                select(func.coalesce(func.sum(Invoice.amount), 0)).where(
                    Invoice.contract_id == contract.id,
                    Invoice.id != invoice.id,
                    Invoice.status != InvoiceStatus.rejected,
                    Invoice.currency == contract.currency,
                )
            )
        ).scalar()
        cumulative = Decimal(prior or 0) + invoice.amount
        if cumulative > contract.spend_limit:
            findings.append(
                warning(
                    "contract_spend_limit_exceeded_not_to_exceed"
                    if contract.not_to_exceed
                    else "contract_spend_limit_exceeded",
                    cap_severity,
                    cumulativeSpend=cumulative,
                    contractNumber=contract.contract_number,
                    spendLimit=contract.spend_limit,
                    currency=contract.currency,
                )
            )

    # --- GL outside contract terms ----------------------------------------
    allowed_gl = (contract.terms or {}).get("allowed_gl_accounts")
    if allowed_gl and invoice.gl_account and invoice.gl_account not in allowed_gl:
        findings.append(
            warning(
                "contract_gl_not_allowed",
                "warning",
                glAccount=invoice.gl_account,
                contractNumber=contract.contract_number,
            )
        )

    return findings
