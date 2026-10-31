"""Stable codes + parameterized templates for every invoice warning.

`invoice.warnings` used to carry only `{type, severity, message}`, where
`message` was an f-string composed at the call site from the row's own data
(`PO 4412 not found`, `Round amount: 5000.00`, a variance percentage). The
browser rendered it verbatim, so `/invoices` and `InvoiceModal` framed
server English in a localized shell — see `decisions.md` §155, which named
this catalogue as the fix and the e-invoice rule catalogue as its shape.

Three things live here, and they are deliberately one module:

* **The code vocabulary.** One :class:`WarningSpec` per DISTINCT message, not
  per `type`: `po_mismatch` is four sentences and `quality_hold` five, so a
  label keyed on `type` alone could only ever name one of them. The code is
  what the frontend keys its message catalogue on.
* **The English template.** The same string the composition site used to build
  by hand, with `{camelCase}` placeholders instead of interpolated values. It
  is rendered here to produce the `message` field, which stays on the payload
  as the fallback for a code the reading client predates — a warning persisted
  before this module carries no `code` at all, and `refresh_warnings` only
  re-derives it on the invoice's next write.
* **The parameter kinds.** What each placeholder IS — money, a percentage, a
  count, a date, opaque text — so the client can format it for the reader's
  locale instead of receiving `5000.00` formatted for en-US. This is why the
  English fallback and the English *message key* are two different strings:
  the fallback spells the currency code and the `%` out (nothing downstream
  will format them), while the localized value leaves both to
  `utils/money.ts::formatMoney` and `Intl.NumberFormat`.

Composition goes through :func:`warning`, never a dict literal — that is what
makes the catalogue complete rather than aspirational, and
`tests/test_invoice_warning_catalog.py` scans the producing modules to keep it
that way (the objection `e_invoice/rule_catalog.py` raises against a registry —
"an author can add a table entry and forget to use it" — is answered by the
same test asserting every declared code has a call site).

**Money stays exact.** A `money` param is the `Decimal`'s own digits as a
string, never a float and never rounded down; the client formats the string.
**No PII joins the params** that was not already in the message: a vendor name
or an inspection note that the composed sentence carried still travels, and
nothing new (bank detail, tax id, address) is ever a parameter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

#: What a placeholder holds, which is what the client needs to format it.
#:
#: ``money``    exact decimal digits; formatted with the warning's ``currency``
#: ``currency`` an ISO 4217 code — printed by the fallback, read by the client
#: ``number``   a bare figure (a quantity, a sigma count) the CALLER formats
#: ``percent``  a percentage figure the caller formats, sign included
#: ``count``    an integer; drives ICU plural selection on both sides
#: ``date``     an ISO ``YYYY-MM-DD`` date
#: ``text``     opaque — an identifier, a name, a sentence we cannot key
ParamKind = Literal["text", "currency", "money", "number", "percent", "count", "date"]

#: ISO 4217 fallback, matching `frontend/src/lib/utils/money.ts::DEFAULT_CURRENCY`
#: and the `invoice.currency or "USD"` reads already in `invoice_warnings`.
DEFAULT_CURRENCY = "USD"


@dataclass(frozen=True)
class WarningSpec:
    """One distinct warning sentence, and how a client re-renders it."""

    #: Stable, snake_case, unique across the catalogue. The frontend's key.
    code: str
    #: The `InvoiceWarning.type` bucket this code belongs to. Several codes
    #: share a type; nothing keys behaviour on the code, so a new code never
    #: needs a matching new type.
    type: str
    #: English, with `{camelCase}` placeholders and optional ICU plural blocks.
    template: str
    #: Placeholder name → kind. Must match the template exactly, both ways.
    params: dict[str, ParamKind]


# Placeholders, whether plain (`{amount}`) or an ICU plural head
# (`{days, plural, …}`). Branch bodies carry only `#` and prose, so this does
# not need to skip them.
_PLACEHOLDER = re.compile(r"\{(\w+)(?=[,}])")


WARNING_SPECS: tuple[WarningSpec, ...] = (
    # ---- missing_field ---------------------------------------------------
    WarningSpec("missing_vendor_name", "missing_field", "Missing vendor name", {}),
    WarningSpec("missing_invoice_number", "missing_field", "Missing invoice number", {}),
    WarningSpec("missing_amount", "missing_field", "Missing or zero amount", {}),
    # ---- duplicate (exact) -----------------------------------------------
    WarningSpec(
        "duplicate_invoice_number",
        "duplicate",
        "Duplicate invoice number for this vendor",
        {},
    ),
    # ---- duplicate_similar (semantic) ------------------------------------
    #
    # Four base sentences, because the composed message has two independent
    # optional halves (is the match's invoice number known? its vendor name?)
    # and a client cannot concatenate localized clauses. The cross-entity tail
    # is NOT a fifth dimension: it is an ICU plural with a `=0` arm, which
    # collapses "no cross-entity siblings" into the same template.
    WarningSpec(
        "duplicate_similar",
        "duplicate_similar",
        "Potential duplicate: {similarity}% match to {invoiceNumber} from {vendorName}"
        "{crossEntityCount, plural, =0 {}"
        " one { (plus # near-identical invoice under another entity)}"
        " other { (plus # near-identical invoices under another entity)}}",
        {
            "similarity": "percent",
            "invoiceNumber": "text",
            "vendorName": "text",
            "crossEntityCount": "count",
        },
    ),
    WarningSpec(
        "duplicate_similar_unnamed_vendor",
        "duplicate_similar",
        "Potential duplicate: {similarity}% match to {invoiceNumber}"
        "{crossEntityCount, plural, =0 {}"
        " one { (plus # near-identical invoice under another entity)}"
        " other { (plus # near-identical invoices under another entity)}}",
        {"similarity": "percent", "invoiceNumber": "text", "crossEntityCount": "count"},
    ),
    WarningSpec(
        "duplicate_similar_unnumbered",
        "duplicate_similar",
        "Potential duplicate: {similarity}% match to another invoice from {vendorName}"
        "{crossEntityCount, plural, =0 {}"
        " one { (plus # near-identical invoice under another entity)}"
        " other { (plus # near-identical invoices under another entity)}}",
        {"similarity": "percent", "vendorName": "text", "crossEntityCount": "count"},
    ),
    WarningSpec(
        "duplicate_similar_unnumbered_unnamed_vendor",
        "duplicate_similar",
        "Potential duplicate: {similarity}% match to another invoice"
        "{crossEntityCount, plural, =0 {}"
        " one { (plus # near-identical invoice under another entity)}"
        " other { (plus # near-identical invoices under another entity)}}",
        {"similarity": "percent", "crossEntityCount": "count"},
    ),
    WarningSpec(
        "duplicate_similar_cross_entity",
        "duplicate_similar",
        "Potential duplicate: {similarity}% match to a near-identical invoice under another entity",
        {"similarity": "percent"},
    ),
    # ---- fraud rules -----------------------------------------------------
    WarningSpec(
        "round_amount",
        "fraud_round_amount",
        "Round amount: {amount} {currency}",
        {"amount": "money", "currency": "currency"},
    ),
    WarningSpec(
        "future_invoice_date",
        "fraud_future_date",
        "Invoice date is in the future",
        {},
    ),
    WarningSpec(
        "rush_payment",
        "fraud_rush_payment",
        "Rush payment: due within {days, plural, one {# day} other {# days}} of the invoice date",
        {"days": "count"},
    ),
    WarningSpec("past_due", "past_due", "Invoice is past due", {}),
    # ---- ai_reading_paused (decisions §253) -------------------------------
    #
    # The plan's monthly AI-read allowance (or the customer's spending cap) is
    # used, so `run_extraction` did NOT call the model: the invoice is back at
    # `new` for manual entry. Two codes because the remedy differs — upgrade the
    # plan vs raise the cap. No money param: the cap is in USD while a warning's
    # money kind formats in the INVOICE's currency.
    WarningSpec(
        "ai_allowance_reached",
        "ai_reading_paused",
        "AI reading is paused: this month's "
        "{included, plural, one {# AI-read invoice is} other {# AI-read invoices are}} "
        "used. Enter the details manually, or upgrade your plan under Settings → Subscription.",
        {"included": "count"},
    ),
    WarningSpec(
        "ai_spend_cap_reached",
        "ai_reading_paused",
        "AI reading is paused: this month's AI spending cap is reached. Enter the "
        "details manually, or raise the cap under Settings → Subscription.",
        {},
    ),
    WarningSpec("unverified_vendor", "unverified_vendor", "Vendor is unverified", {}),
    WarningSpec(
        "personal_email_domain",
        "fraud_personal_email",
        "Vendor email uses personal domain: {domain}",
        {"domain": "text"},
    ),
    WarningSpec(
        "new_vendor_large_amount",
        "fraud_new_vendor_large",
        "New vendor (created {days, plural, one {# day} other {# days}} ago) "
        "submitting large invoice {amount} {currency}",
        {"days": "count", "amount": "money", "currency": "currency"},
    ),
    WarningSpec(
        "remit_to_changed",
        "fraud_bank_change",
        "Remit-to address changed since the last approved invoice for this vendor",
        {},
    ),
    WarningSpec(
        "amount_above_vendor_mean",
        "fraud_stat_anomaly",
        "Amount {amount} {currency} is {sigma}σ above this vendor's historical "
        "mean ({mean} {currency})",
        {
            "amount": "money",
            "sigma": "number",
            "mean": "money",
            "currency": "currency",
        },
    ),
    # The LLM's own explanation is the one param we cannot key — it is generated
    # prose, not a sentence from a closed set. The FRAME is keyed, so a reader
    # at least learns what kind of finding it is in their own language.
    WarningSpec(
        "llm_anomaly",
        "fraud_llm_anomaly",
        "AI-flagged anomaly: {reason}",
        {"reason": "text"},
    ),
    # ---- line-total reconciliation ---------------------------------------
    WarningSpec(
        "line_total_mismatch",
        "line_total_mismatch",
        "Line items total {lineItemsTotal} {currency} but the invoice amount "
        "is {headerAmount} {currency}",
        {
            "lineItemsTotal": "money",
            "headerAmount": "money",
            "currency": "currency",
        },
    ),
    # ---- per-vendor line-item price variance -----------------------------
    #
    # Two codes, not one with a `direction` param: "over" / "under" is a word
    # inside the sentence, and a word cannot be a parameter — the client would
    # splice English into a German sentence, which is the defect this catalogue
    # exists to remove.
    WarningSpec(
        "price_variance_over",
        "price_variance",
        "Unit price {deltaPct}% over this vendor's baseline for {item} "
        "({unitPrice} {currency} vs {baselineUnitPrice} {currency})",
        {
            "deltaPct": "percent",
            "item": "text",
            "unitPrice": "money",
            "baselineUnitPrice": "money",
            "currency": "currency",
        },
    ),
    WarningSpec(
        "price_variance_under",
        "price_variance",
        "Unit price {deltaPct}% under this vendor's baseline for {item} "
        "({unitPrice} {currency} vs {baselineUnitPrice} {currency})",
        {
            "deltaPct": "percent",
            "item": "text",
            "unitPrice": "money",
            "baselineUnitPrice": "money",
            "currency": "currency",
        },
    ),
    # ---- PO matching -----------------------------------------------------
    WarningSpec(
        "po_not_found",
        "po_mismatch",
        "PO {poNumber} not found",
        {"poNumber": "text"},
    ),
    WarningSpec(
        "po_amount_variance",
        "po_mismatch",
        "Amount variance {variancePct}% vs PO {poNumber} "
        "(invoice {invoiceAmount} {currency} vs PO {poTotal} {currency})",
        {
            "variancePct": "percent",
            "poNumber": "text",
            "invoiceAmount": "money",
            "poTotal": "money",
            "currency": "currency",
        },
    ),
    # The PO records no currency (`po_matching.CURRENCY_UNKNOWN`), so the amounts
    # were compared at face value and the PO's figure is NOT labelled: a `money`
    # kind would format it with the invoice's code and assert the PO is in it.
    # `poTotal` is a `number` for the reason the self-correction figures below
    # are (decisions §197).
    WarningSpec(
        "po_amount_variance_po_currency_unknown",
        "po_mismatch",
        "Amount variance {variancePct}% vs PO {poNumber}, which records no currency "
        "(invoice {invoiceAmount} {currency} vs PO {poTotal})",
        {
            "variancePct": "percent",
            "poNumber": "text",
            "invoiceAmount": "money",
            "poTotal": "number",
            "currency": "currency",
        },
    ),
    # The two ARE in different currencies, so there is no variance to state —
    # the codes are the finding. Both are `text`: they are printed, not used to
    # format a figure.
    WarningSpec(
        "po_currency_mismatch",
        "po_mismatch",
        "Invoice is in {invoiceCurrency} but PO {poNumber} is in {poCurrency} — "
        "the amounts were not compared",
        {"invoiceCurrency": "text", "poNumber": "text", "poCurrency": "text"},
    ),
    # The PO was cancelled (or voided): it authorizes nothing, so the invoice
    # was not matched against it.
    WarningSpec(
        "po_cancelled",
        "po_mismatch",
        "PO {poNumber} is cancelled — the invoice was not matched against it",
        {"poNumber": "text"},
    ),
    # The PO's last ERP sync could not state its total exactly, so the total on
    # record is not compared against (decisions §323). No figure is printed:
    # the stale one is precisely what must not be presented as the PO's.
    WarningSpec(
        "po_total_unverifiable",
        "po_mismatch",
        "The total of PO {poNumber} could not be confirmed from the ERP on its last sync — "
        "the amounts were not compared",
        {"poNumber": "text"},
    ),
    WarningSpec(
        "po_partial_receipt",
        "po_mismatch",
        "Partial 3-way match — {matchType} match against PO {poNumber}, but "
        "only part of the ordered quantity has been received",
        {"matchType": "text", "poNumber": "text"},
    ),
    WarningSpec(
        "po_over_receipt",
        "po_mismatch",
        "Over-receipt: {receivedQuantity} received against {orderedQuantity} "
        "ordered (+{excessQuantity}) on PO {poNumber}",
        {
            "receivedQuantity": "number",
            "orderedQuantity": "number",
            "excessQuantity": "number",
            "poNumber": "text",
        },
    ),
    # The matcher only counts quantities when the PO has lines and a receipt
    # carries them, so `over_receipt` can be set with nothing to count.
    WarningSpec(
        "po_over_receipt_unquantified",
        "po_mismatch",
        "More goods received than ordered on PO {poNumber}",
        {"poNumber": "text"},
    ),
    # ---- 4-way: quality inspection ---------------------------------------
    WarningSpec(
        "quality_inspection_failed",
        "quality_hold",
        "Failed quality inspection for PO {poNumber}",
        {"poNumber": "text"},
    ),
    WarningSpec(
        "quality_inspection_failed_notes",
        "quality_hold",
        "Failed quality inspection for PO {poNumber}: {notes}",
        {"poNumber": "text", "notes": "text"},
    ),
    WarningSpec(
        "quality_inspection_missing",
        "quality_hold",
        "Quality inspection required but missing for PO {poNumber}",
        {"poNumber": "text"},
    ),
    WarningSpec(
        "quality_partial_acceptance",
        "quality_hold",
        "Partial acceptance: {acceptedQuantity} of the ordered quantity accepted on PO {poNumber}",
        {"acceptedQuantity": "number", "poNumber": "text"},
    ),
    WarningSpec(
        "quality_partial_acceptance_unquantified",
        "quality_hold",
        "Partial quality acceptance on inspection for PO {poNumber}",
        {"poNumber": "text"},
    ),
    # ---- recurring-template variance -------------------------------------
    WarningSpec(
        "recurring_variance_over",
        "recurring_variance",
        "Amount {amount} {currency} is {deltaPct}% over the recurring template "
        "“{templateName}” expected amount {expectedAmount} {currency}",
        {
            "amount": "money",
            "deltaPct": "percent",
            "templateName": "text",
            "expectedAmount": "money",
            "currency": "currency",
        },
    ),
    WarningSpec(
        "recurring_variance_under",
        "recurring_variance",
        "Amount {amount} {currency} is {deltaPct}% under the recurring template "
        "“{templateName}” expected amount {expectedAmount} {currency}",
        {
            "amount": "money",
            "deltaPct": "percent",
            "templateName": "text",
            "expectedAmount": "money",
            "currency": "currency",
        },
    ),
    # ---- contract compliance ---------------------------------------------
    WarningSpec(
        "contract_expired",
        "contract_noncompliant",
        "Invoice dated {invoiceDate} is after contract {contractNumber} expired ({endDate})",
        {"invoiceDate": "date", "contractNumber": "text", "endDate": "date"},
    ),
    WarningSpec(
        "contract_not_started",
        "contract_noncompliant",
        "Invoice dated {invoiceDate} predates contract {contractNumber} start ({startDate})",
        {"invoiceDate": "date", "contractNumber": "text", "startDate": "date"},
    ),
    # One code per status rather than a `{status}` param, same reason as the
    # price-variance direction: the status is a word in the sentence.
    WarningSpec(
        "contract_terminated",
        "contract_noncompliant",
        "Spend recorded against terminated contract {contractNumber}",
        {"contractNumber": "text"},
    ),
    WarningSpec(
        "contract_cancelled",
        "contract_noncompliant",
        "Spend recorded against cancelled contract {contractNumber}",
        {"contractNumber": "text"},
    ),
    WarningSpec(
        "contract_vendor_mismatch",
        "contract_noncompliant",
        "Invoice vendor does not match contract {contractNumber} vendor",
        {"contractNumber": "text"},
    ),
    WarningSpec(
        "contract_spend_limit_exceeded",
        "contract_noncompliant",
        "Cumulative spend {cumulativeSpend} {currency} exceeds contract "
        "{contractNumber} limit {spendLimit} {currency}",
        {
            "cumulativeSpend": "money",
            "contractNumber": "text",
            "spendLimit": "money",
            "currency": "currency",
        },
    ),
    WarningSpec(
        "contract_spend_limit_exceeded_not_to_exceed",
        "contract_noncompliant",
        "Cumulative spend {cumulativeSpend} {currency} exceeds contract "
        "{contractNumber} limit {spendLimit} {currency} (not-to-exceed)",
        {
            "cumulativeSpend": "money",
            "contractNumber": "text",
            "spendLimit": "money",
            "currency": "currency",
        },
    ),
    WarningSpec(
        "contract_gl_not_allowed",
        "contract_noncompliant",
        "GL account {glAccount} is outside contract {contractNumber} allowed accounts",
        {"glAccount": "text", "contractNumber": "text"},
    ),
    # ---- extraction self-correction --------------------------------------
    #
    # These figures are `number`, not `money`, on purpose: they are read off an
    # `ExtractionResult` whose `currency` is itself an extracted field that can
    # be absent, and a money kind with no currency would format as the default
    # `USD` — stamping dollars on a EUR invoice, a wrong figure rather than an
    # unformatted one. The composed sentence carried them bare too.
    WarningSpec(
        "self_correction_total_reconciliation",
        "extraction_self_correction",
        "Amounts don't add up: subtotal ({subtotal}) + tax ({tax}) + shipping "
        "({shipping}) − discount ({discount}) = {expected}, but total is {amount}.",
        {
            "subtotal": "number",
            "tax": "number",
            "shipping": "number",
            "discount": "number",
            "expected": "number",
            "amount": "number",
        },
    ),
    WarningSpec(
        "self_correction_date_ordering",
        "extraction_self_correction",
        "Due date ({dueDate}) is before invoice date ({invoiceDate}).",
        {"dueDate": "date", "invoiceDate": "date"},
    ),
    WarningSpec(
        "self_correction_line_items_sum",
        "extraction_self_correction",
        "Line items total ({lineItemsTotal}) doesn't match invoice amount ({amount}).",
        {"lineItemsTotal": "number", "amount": "number"},
    ),
    # The document contradicts itself about its currency, so `_apply_extraction`
    # left the header money unread (`decisions.md` §308). The codes are `text`,
    # not `currency`: a `currency` param only labels a `money` figure beside it
    # and never reaches the sentence, and here the codes ARE the sentence.
    WarningSpec(
        "self_correction_currency_conflict",
        "extraction_self_correction",
        "Amounts are printed in {printedCurrency}, but the invoice currency reads "
        "{fieldCurrency}. The amounts were left blank — enter them from the document.",
        {"printedCurrency": "text", "fieldCurrency": "text"},
    ),
    WarningSpec(
        "self_correction_currency_mixed",
        "extraction_self_correction",
        "Amounts are printed in more than one currency ({printedCurrencies}). "
        "The amounts were left blank — enter them from the document.",
        {"printedCurrencies": "text"},
    ),
    WarningSpec(
        "self_correction_line_item_math",
        "extraction_self_correction",
        "Line {lineNumber}: {quantity} × {unitPrice} = {expected}, but total is {total}.",
        {
            "lineNumber": "count",
            "quantity": "number",
            "unitPrice": "number",
            "expected": "number",
            "total": "number",
        },
    ),
    # ---- GL validation ---------------------------------------------------
    #
    # `codes` is joined on THIS side with a literal `", "`: GL codes are
    # identifiers, not prose, and `decisions.md` §148 keeps the ASCII separator
    # for exactly that case (the frontend's `formatList` is for prose).
    WarningSpec(
        "gl_codes_not_in_chart",
        "gl_account_invalid",
        "AI suggested GL code(s) not in active chart: {codes}",
        {"codes": "text"},
    ),
    WarningSpec(
        "gl_code_stale_prior",
        "gl_account_invalid",
        "Cached vendor GL code '{code}' is no longer in the active chart of accounts.",
        {"code": "text"},
    ),
)


# --------------------------------------------------------------------------- #
# Two more sentence families, one vocabulary
#
# The PO-match panel's issue list and the exception queue's description were
# the two surfaces `decisions.md` §155 / round 31 left as server English: one
# panel below the keyed warnings, and the queue an auditor reads. They join the
# catalogue rather than growing catalogues of their own, so one generator, one
# drift guard and one client reader cover all three — and a finding that is a
# warning AND an exception (a round amount, a missing PO) is stated in ONE
# wording on both surfaces, because the exception reuses the warning's code.
#
# Each family's wire code carries its namespace (`po_match.issue.…`,
# `exception.…`), which is also how the generator derives the message key: the
# code is the identity, so a family can never borrow another's wording.
# --------------------------------------------------------------------------- #

#: Wire-code prefix of a `MatchResult.issues` entry.
PO_MATCH_ISSUE_PREFIX = "po_match.issue."
#: Wire-code prefix of an exception-only description (a sentence no warning
#: states). Most exceptions reuse a warning code instead — see
#: `invoice_warnings._ensure_exception`.
EXCEPTION_PREFIX = "exception."

#: The `type` every PO-match issue spec carries. Issues are not warnings — they
#: never reach `invoice.warnings` — so the field only keeps the dataclass whole.
PO_MATCH_ISSUE_TYPE = "po_match_issue"

#: The matcher's own findings, one per sentence `po_matching` used to compose.
#: They read shorter than the `po_mismatch` warnings because they render INSIDE
#: the PO-match panel, which already names the PO.
PO_MATCH_ISSUE_SPECS: tuple[WarningSpec, ...] = (
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "po_not_found",
        PO_MATCH_ISSUE_TYPE,
        "PO {poNumber} not found",
        {"poNumber": "text"},
    ),
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "currency_mismatch",
        PO_MATCH_ISSUE_TYPE,
        "Currency mismatch: invoice in {invoiceCurrency}, PO in {poCurrency} "
        "— amounts not compared",
        {"invoiceCurrency": "text", "poCurrency": "text"},
    ),
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "po_cancelled",
        PO_MATCH_ISSUE_TYPE,
        "PO is cancelled — amounts not compared",
        {},
    ),
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "po_total_unverifiable",
        PO_MATCH_ISSUE_TYPE,
        "PO total not confirmed by the ERP on its last sync — amounts not compared",
        {},
    ),
    # Both sides in one known currency — both figures are money in it.
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "amount_mismatch",
        PO_MATCH_ISSUE_TYPE,
        "Amount mismatch: invoice {invoiceAmount} {currency} vs PO {poTotal} {currency} "
        "({variancePct}%)",
        {
            "invoiceAmount": "money",
            "poTotal": "money",
            "currency": "currency",
            "variancePct": "percent",
        },
    ),
    # The PO records no currency, so its figure is a bare `number`: labelling
    # it with the invoice's code would assert the PO is in it (decisions §197).
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "amount_mismatch_po_currency_unknown",
        PO_MATCH_ISSUE_TYPE,
        "Amount mismatch: invoice {invoiceAmount} {currency} vs PO {poTotal} ({variancePct}%)",
        {
            "invoiceAmount": "money",
            "poTotal": "number",
            "currency": "currency",
            "variancePct": "percent",
        },
    ),
    # The invoice's own code is not a valid ISO code either. Neither figure may
    # be labelled — a `currency` param would coerce the absent code to USD and
    # assert dollars.
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "amount_mismatch_currency_unknown",
        PO_MATCH_ISSUE_TYPE,
        "Amount mismatch: invoice {invoiceAmount} vs PO {poTotal} ({variancePct}%)",
        {"invoiceAmount": "number", "poTotal": "number", "variancePct": "percent"},
    ),
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "partial_receipt",
        PO_MATCH_ISSUE_TYPE,
        "Partial receipt: {receivedPct}% of ordered quantity received",
        {"receivedPct": "percent"},
    ),
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "over_receipt",
        PO_MATCH_ISSUE_TYPE,
        "Over-receipt: {receivedQuantity} received against {orderedQuantity} ordered "
        "(+{excessQuantity})",
        {"receivedQuantity": "number", "orderedQuantity": "number", "excessQuantity": "number"},
    ),
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "inspection_failed",
        PO_MATCH_ISSUE_TYPE,
        "Failed quality inspection",
        {},
    ),
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "inspection_failed_notes",
        PO_MATCH_ISSUE_TYPE,
        "Failed quality inspection: {notes}",
        {"notes": "text"},
    ),
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "partial_acceptance",
        PO_MATCH_ISSUE_TYPE,
        "Partial acceptance: {acceptedQuantity} of ordered quantity accepted",
        {"acceptedQuantity": "number"},
    ),
    # The matcher used to splice the English word "part" into the quantity
    # slot when the inspection recorded none — a word, so its own code.
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "partial_acceptance_unquantified",
        PO_MATCH_ISSUE_TYPE,
        "Partial acceptance: part of ordered quantity accepted",
        {},
    ),
    WarningSpec(
        PO_MATCH_ISSUE_PREFIX + "inspection_required_missing",
        PO_MATCH_ISSUE_TYPE,
        "Quality inspection required but missing",
        {},
    ),
)

#: The reserved `params` key a composite exception description carries its
#: findings under — a list of `{code, params, message}` entries, each itself a
#: catalogue finding. Never a template placeholder.
FINDINGS_PARAM = "findings"

#: The exception type ERP reconciliation sentences are bucketed under. A
#: ``paid_not_recorded`` row is raised as ``payment_reconciliation`` for a
#: double-pay refusal, but the bucket is only a label (nothing keys on it).
ERP_RECONCILIATION_TYPE = "erp_reconciliation"

#: Suffix of the twin of every sentence that names the ERP: the adapter has no
#: product name (`erp_status_sync.erp_product_name` is None), so the sentence
#: says "Your ERP" — a word, so its own code, never a spliced param.
ERP_UNNAMED_SUFFIX = "_unnamed"

#: What stopped FeohLedger recording the ERP's payment, and what the reader does
#: about it — one clause per ``external_payment`` refusal code (the code minus
#: its ``external_payment_`` prefix), ``other`` for any code not listed.
ERP_UNRECORDED_REASONS: dict[str, str] = {
    "blocking_exception": (
        "another exception on this invoice is blocking payment. Clear that exception "
        "and FeohLedger records the payment automatically."
    ),
    "in_run": (
        "the invoice is also in a FeohLedger payment run, so the supplier could be paid "
        "twice. Take it out of the run or cancel the run, then record the ERP's payment "
        "from this exception."
    ),
    "payment_live": (
        "FeohLedger has its own payment in progress for this invoice, so the supplier "
        "could be paid twice. Void that payment if the ERP's payment is the real one, "
        "then record the ERP's payment from this exception."
    ),
    "card_live": (
        "a virtual card is still live for this invoice, so the supplier could be paid "
        "twice. Cancel the card, then record the ERP's payment from this exception."
    ),
    "credit_conflict": (
        "an applied credit memo no longer matches this invoice's supplier or currency, "
        "so the amount owed can't be stated. Correct the credit memo, then record the "
        "ERP's payment from this exception."
    ),
    "nothing_to_pay": (
        "credit memos already cover the whole invoice, so there is no payment to record. "
        "Check the bill in the ERP, then resolve this exception."
    ),
    "not_payable": (
        "the invoice is not at a status that can be marked paid. Check the invoice, then "
        "record the ERP's payment from this exception or resolve it."
    ),
    "other": (
        "FeohLedger refused to record it. Check the invoice, then record the ERP's payment "
        "from this exception or resolve it."
    ),
}

#: Sentences that open with the ERP's product name: bare name → the rest of the
#: sentence after "<ERP> ". Each becomes two codes (named, ``_unnamed``).
_ERP_NAMED_SENTENCES: dict[str, str] = {
    **{
        f"erp_paid_unrecorded_{reason}": (
            f"reports this bill paid, but FeohLedger couldn't record that payment because {clause}"
        )
        for reason, clause in ERP_UNRECORDED_REASONS.items()
    },
    "erp_paid_withdrawn": (
        "reported this bill paid, but no longer shows it paid. FeohLedger did not record "
        "that payment. Check the bill in the ERP: if it is still owed, resolve this "
        "exception and pay it as usual."
    ),
    "erp_paid_retry_failed": (
        "reports this bill paid, but FeohLedger couldn't record that payment automatically. "
        "Check the details, then record the ERP's payment from this exception."
    ),
    "erp_settled_short": (
        "reports this bill paid, but FeohLedger's own payment for it settled for less than "
        "AP authorized, so the invoice is held at Payment scheduled. Open the payment and "
        "either accept the settled amount or void the payment."
    ),
    "erp_voided_after_post": (
        "reports this bill voided or cancelled, but FeohLedger had already sent it on for "
        "payment, so it was not cancelled here. Check whether a payment is on its way to "
        "the supplier, and stop or recover it if the bill is really void."
    ),
    # The payment write-back (`erp_payment_writeback`, decisions §295). The
    # refusal code, payment, run and bill are in `exceptions.erp_report`.
    "erp_payment_writeback_failed": (
        "did not record this invoice's payment, so the bill still shows as unpaid there "
        "although the money was paid. Resolve what stopped it, then use Retry ERP sync on "
        "the payment's run in Payments."
    ),
    "erp_payment_writeback_balance_open": (
        "recorded this invoice's payment, but the bill still shows {amount} {currency} open "
        "there (a discount or credit taken in FeohLedger). Close the difference in the ERP."
    ),
    "erp_payment_void_writeback_failed": (
        "still shows this bill paid by a payment that was voided in FeohLedger, because "
        "voiding it in the ERP failed. Void that payment in the ERP so the books agree."
    ),
}

#: Params a named sentence carries besides ``erp`` (both twins carry them).
_ERP_NAMED_PARAMS: dict[str, dict[str, ParamKind]] = {
    "erp_payment_writeback_balance_open": {"amount": "money", "currency": "currency"},
}

_DISPATCH_STOPPED_TAIL = (
    " It was stopped before any money moved. Confirm what is still owed before paying "
    "it; if a virtual card was already issued for this invoice, cancel it."
)
_UNLINKED_TAIL = (
    ", so it will not pay it. Confirm in the ERP whether it is still owed, then record "
    "it as paid outside FeohLedger or re-post it."
)

#: ERP sentences that do not name the product (QuickBooks status sync-back and
#: the pre-dispatch bill check, decisions §295). Their machine detail — the
#: bill, the event, the payment — is in ``exceptions.erp_report``.
_ERP_UNNAMED_ONLY: dict[str, str] = {
    "erp_credit_settlement": (
        "The ERP shows this bill settled, at least in part by a vendor credit applied "
        "there. FeohLedger will not pay it until someone confirms what is still owed."
    ),
    "erp_paid_while_scheduled": (
        "The ERP shows this bill paid by a payment FeohLedger did not make, while a "
        "FeohLedger payment is scheduled for it. Stop or void one of them before both "
        "reach the supplier."
    ),
    "erp_paid_twice": (
        "FeohLedger paid this invoice, and the ERP shows the bill also paid by a payment "
        "FeohLedger did not make — the supplier may have been paid twice. Recover one of "
        "the payments."
    ),
    "erp_partly_paid_outside": (
        "The ERP shows part of this bill paid or credited outside FeohLedger; a FeohLedger "
        "payment for the full amount would overpay it. Reconcile what is still owed "
        "before paying."
    ),
    "erp_recorded_payment_withdrawn": (
        "The ERP reported this bill paid, and FeohLedger recorded that payment, but the "
        "ERP no longer shows it paid (the payment was deleted or voided there). Confirm "
        "whether it is still owed."
    ),
    "erp_bill_deleted_blocking": (
        "The bill this invoice was posted as has been deleted or voided in the ERP; "
        "FeohLedger will not pay it until someone confirms it is still owed."
    ),
    "erp_bill_deleted": (
        "The bill this invoice was posted as has been deleted or voided in the ERP. "
        "Confirm the books still agree."
    ),
    "erp_bill_unlinked_no_document": (
        "FeohLedger cannot check this invoice's bill in the ERP, because the ERP did not "
        "say which bill it created when the invoice was posted" + _UNLINKED_TAIL
    ),
    "erp_bill_unlinked_no_company": (
        "FeohLedger cannot check this invoice's bill in the ERP, because the posting did "
        "not record which ERP company it went to" + _UNLINKED_TAIL
    ),
    "erp_bill_unlinked_other_company": (
        "FeohLedger cannot check this invoice's bill in the ERP, because it was posted to "
        "a different ERP company than the one now connected" + _UNLINKED_TAIL
    ),
    "erp_dispatch_stopped_paid": (
        "FeohLedger was about to pay this invoice, but the ERP shows its bill already paid."
        + _DISPATCH_STOPPED_TAIL
    ),
    "erp_dispatch_stopped_partially_paid": (
        "FeohLedger was about to pay this invoice, but the ERP shows its bill partly paid."
        + _DISPATCH_STOPPED_TAIL
    ),
    "erp_dispatch_stopped_deleted": (
        "FeohLedger was about to pay this invoice, but the ERP shows its bill deleted or "
        "voided." + _DISPATCH_STOPPED_TAIL
    ),
    "erp_dispatch_stopped_unconfirmed": (
        "FeohLedger was about to pay this invoice, but the ERP could not confirm its bill "
        "is still open and unpaid." + _DISPATCH_STOPPED_TAIL
    ),
    "erp_reported_payment_voided": (
        "A payment the ERP reported was voided in FeohLedger, but the ERP may still show "
        "the bill paid. FeohLedger will not pay it again until someone confirms the ERP's "
        "payment is undone."
    ),
    # `payment_erp_sync`'s failed leg. It need not be the ERP that failed (the
    # tenant may have none), so it never names one. The failure class, payment,
    # run and the invoice's status are in `exceptions.erp_report`.
    "erp_sync_back_failed": (
        "This invoice's payment settled, but FeohLedger could not finish recording it, so "
        "the invoice has not moved to Paid although the money has gone. Nothing retries "
        "this automatically: use Retry ERP sync on the payment's run in Payments."
    ),
}

#: ERP reconciliation descriptions (decisions §319): the sentence an AP user
#: reads on a row `erp_status_sync` or `api/erp_webhook` opens. Built through
#: `erp_status_sync.erp_finding` (the named / `_unnamed` pick) or
#: `exception_finding` for the sentences that never name the product.
ERP_RECONCILIATION_SPECS: tuple[WarningSpec, ...] = (
    *(
        spec
        for name, rest in _ERP_NAMED_SENTENCES.items()
        for spec in (
            WarningSpec(
                EXCEPTION_PREFIX + name,
                ERP_RECONCILIATION_TYPE,
                "{erp} " + rest,
                {"erp": "text", **_ERP_NAMED_PARAMS.get(name, {})},
            ),
            WarningSpec(
                EXCEPTION_PREFIX + name + ERP_UNNAMED_SUFFIX,
                ERP_RECONCILIATION_TYPE,
                "Your ERP " + rest,
                dict(_ERP_NAMED_PARAMS.get(name, {})),
            ),
        )
    ),
    *(
        WarningSpec(EXCEPTION_PREFIX + name, ERP_RECONCILIATION_TYPE, template, {})
        for name, template in _ERP_UNNAMED_ONLY.items()
    ),
)

#: Exception descriptions no warning states. `type` is the exception type the
#: description belongs to (`exception_lifecycle.EXCEPTION_TYPES`).
#:
#: The two `*_findings` codes are FRAMES: an exception that covers several
#: findings at once (one `price_variance` for every flagged line, one
#: `contract_noncompliant` for every breached term) used to join their English
#: with `"; "` on the server. It now carries the findings themselves under
#: `params.findings`, and the frame says how many there are; each client lists
#: them in the reader's language. A single finding needs no frame and is stored
#: as itself (see `exception_findings`).
EXCEPTION_DESCRIPTION_SPECS: tuple[WarningSpec, ...] = (
    WarningSpec(
        EXCEPTION_PREFIX + "missing_data_after_extraction",
        "missing_data",
        "Required fields missing after extraction",
        {},
    ),
    WarningSpec(
        EXCEPTION_PREFIX + "price_variance_findings",
        "price_variance",
        "Line-item price variance vs vendor history on "
        "{count, plural, one {# line} other {# lines}}",
        {"count": "count"},
    ),
    WarningSpec(
        EXCEPTION_PREFIX + "contract_noncompliant_findings",
        "contract_noncompliant",
        "{count, plural, one {# contract-compliance finding} "
        "other {# contract-compliance findings}}",
        {"count": "count"},
    ),
    *ERP_RECONCILIATION_SPECS,
)

#: Every sentence the catalogue declares, in generation order.
ALL_SPECS: tuple[WarningSpec, ...] = (
    *WARNING_SPECS,
    *PO_MATCH_ISSUE_SPECS,
    *EXCEPTION_DESCRIPTION_SPECS,
)

_WARNING_CODES = frozenset(s.code for s in WARNING_SPECS)
_PO_MATCH_ISSUE_CODES = frozenset(s.code for s in PO_MATCH_ISSUE_SPECS)
_EXCEPTION_CODES = frozenset(s.code for s in EXCEPTION_DESCRIPTION_SPECS)

_BY_CODE: dict[str, WarningSpec] = {}
for _spec in ALL_SPECS:
    if _spec.code in _BY_CODE:
        raise RuntimeError(f"duplicate invoice-warning code: {_spec.code}")
    _declared = set(_PLACEHOLDER.findall(_spec.template))
    if _declared != set(_spec.params):
        raise RuntimeError(
            f"invoice-warning code {_spec.code}: template placeholders {sorted(_declared)} "
            f"do not match declared params {sorted(_spec.params)}"
        )
    _BY_CODE[_spec.code] = _spec


def spec_for(code: str) -> WarningSpec:
    """The spec for ``code``. Raises ``KeyError`` for an undeclared code."""
    return _BY_CODE[code]


def codes() -> tuple[str, ...]:
    """Every declared code across all three families, in catalogue order."""
    return tuple(s.code for s in ALL_SPECS)


# --------------------------------------------------------------------------- #
# Rendering the English fallback
# --------------------------------------------------------------------------- #


def _matching_brace(s: str, open_at: int) -> int:
    """Index of the ``}`` closing the brace at ``open_at``, or -1."""
    depth = 0
    for i in range(open_at, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _parse_branches(body: str) -> dict[str, str]:
    """``one {…} other {…}`` → ``{"one": "…", "other": "…"}``."""
    branches: dict[str, str] = {}
    i = 0
    while i < len(body):
        while i < len(body) and body[i].isspace():
            i += 1
        label_start = i
        while i < len(body) and body[i] != "{":
            i += 1
        label = body[label_start:i].strip()
        if i >= len(body):
            break
        close = _matching_brace(body, i)
        if close < 0:
            break
        if label:
            branches[label] = body[i + 1 : close]
        i = close + 1
    return branches


def _resolve_plurals(template: str, values: dict[str, object]) -> str:
    """Resolve ICU plural blocks with ENGLISH rules (``one`` iff n == 1).

    The frontend resolves the same blocks through ``Intl.PluralRules`` for the
    reader's locale (`i18n/interpolate.ts`); this side only ever renders the
    English fallback, so the one-branch rule is the whole of CLDR ``en``.
    """
    out = template
    while True:
        match = re.search(r"\{(\w+),\s*plural,\s*", out)
        if match is None:
            return out
        close = _matching_brace(out, match.start())
        if close < 0:
            return out
        name = match.group(1)
        branches = _parse_branches(out[match.end() : close])
        raw = values.get(name)
        try:
            count = int(str(raw))
        except (TypeError, ValueError):
            count = 0
        chosen = branches.get(f"={count}")
        if chosen is None:
            chosen = branches.get("one" if count == 1 else "other", branches.get("other", ""))
        out = out[: match.start()] + chosen.replace("#", str(count)) + out[close + 1 :]


def render(template: str, values: dict[str, object]) -> str:
    """The English sentence for ``template`` with ``values`` substituted.

    Plural blocks resolve first, then plain ``{name}`` placeholders — the same
    order, and the same literal (non-regex) substitution, as the client's
    ``interpolate.ts``, so the two renderings cannot disagree about nesting.
    """
    out = _resolve_plurals(template, values)
    for name, value in values.items():
        out = out.replace("{" + name + "}", str(value))
    return out


# --------------------------------------------------------------------------- #
# Param coercion
# --------------------------------------------------------------------------- #

_TWO_PLACES = Decimal("0.01")


def _money(value: object) -> str:
    """Exact decimal digits for a money figure — never a float, never rounded.

    Widened to two decimal places when the value carries fewer (a `Decimal("5")`
    or a legacy JSON number), so the fallback reads as currency; a 4-decimal
    unit price keeps all four.
    """
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    if -dec.as_tuple().exponent < 2:
        dec = dec.quantize(_TWO_PLACES)
    return f"{dec:f}"


def _coerce(kind: ParamKind, value: object) -> str | int:
    if kind == "count":
        return int(value)  # type: ignore[arg-type]
    if kind == "money":
        return _money(value)
    if kind == "currency":
        code = str(value or "").strip().upper()
        return code if len(code) == 3 else DEFAULT_CURRENCY
    if kind == "date":
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)
    # `number` / `percent` / `text`: the caller owns the precision and the sign,
    # because only the caller knows whether one decimal place or none is the
    # figure it measured.
    return str(value)


def _finding(code: str, params: dict[str, object]) -> dict:
    """`{message, code, params}` for one declared sentence — the shared core."""
    spec = _BY_CODE[code]
    if set(params) != set(spec.params):
        raise ValueError(
            f"invoice-warning code {code}: got params {sorted(params)}, "
            f"expected {sorted(spec.params)}"
        )
    values: dict[str, str | int] = {
        name: _coerce(spec.params[name], value) for name, value in params.items()
    }
    return {"message": render(spec.template, values), "code": code, "params": values}


def po_match_issue(code: str, /, **params: object) -> dict:
    """One `MatchResult.issues` entry: `{code, params, message}`.

    ``code`` is the bare name (`"partial_receipt"`); the wire code gains the
    `po_match.issue.` namespace here, so a call site cannot put a warning code
    in the issue list by mistake. ``message`` is the English fallback — and it
    is the WHOLE of an issue persisted before this change, which is a bare
    string rather than this dict. Every reader accepts both.
    """
    full = PO_MATCH_ISSUE_PREFIX + code
    if full not in _PO_MATCH_ISSUE_CODES:
        raise KeyError(full)
    return _finding(full, params)


def exception_finding(code: str, /, **params: object) -> dict:
    """An exception-only description: a sentence no warning states.

    ``code`` is the bare name; the `exception.` namespace is added here. An
    exception that mirrors a warning passes that warning's own dict to
    `_ensure_exception` instead, so the queue and the invoice state one
    finding in one wording.
    """
    full = EXCEPTION_PREFIX + code
    if full not in _EXCEPTION_CODES:
        raise KeyError(full)
    return _finding(full, params)


def exception_findings(frame: str, findings: list[dict]) -> dict:
    """One exception description covering ``findings`` (warning dicts).

    A single finding IS the description — it needs no frame, and stating it in
    its own warning wording keeps the queue and the invoice agreeing. Two or
    more become the ``frame`` code (`price_variance_findings` /
    `contract_noncompliant_findings`) with the count as its parameter and the
    findings themselves under ``params.findings``: decomposed, so each client
    lists them in its reader's language rather than receiving one server-joined
    English string. ``message`` — the fallback, and what
    ``Exception.description`` stores — is the frame followed by each finding's
    own English.
    """
    if not findings:
        raise ValueError("exception_findings needs at least one finding")
    items = [
        {"code": f["code"], "params": dict(f["params"]), "message": f["message"]} for f in findings
    ]
    if len(items) == 1:
        return items[0]
    head = exception_finding(frame, count=len(items))
    head["params"][FINDINGS_PARAM] = items
    head["message"] = f"{head['message']}: " + "; ".join(i["message"] for i in items)
    return head


def warning(code: str, severity: str, /, **params: object) -> dict:
    """One `invoice.warnings` entry: `{type, severity, message, code, params}`.

    ``message`` is the rendered English and stays on the payload as the
    fallback — a client whose catalogue predates ``code`` renders it verbatim,
    which is also what happens for every warning persisted before this module
    existed. Raises for an undeclared code or a params mismatch: both are
    programming errors in a literal argument, so failing on the write path is
    the honest outcome rather than a silently unkeyed warning.
    """
    if code not in _WARNING_CODES:
        # An issue or exception-only code is not a warning; letting one into
        # `invoice.warnings` would key a finding under the wrong family.
        raise KeyError(code)
    spec = _BY_CODE[code]
    return {"type": spec.type, "severity": severity, **_finding(code, params)}
