# Invoice warnings — the payload, the code catalogue, and the generator

`Invoice.warnings` is a JSONB array of findings a reviewer reads on the
`/invoices` row icon and in `InvoiceModal`. Every entry looks like this:

```json
{
  "type": "fraud_round_amount",
  "severity": "info",
  "message": "Round amount: 5000.00 ZAR",
  "code": "round_amount",
  "params": { "amount": "5000.00", "currency": "ZAR" }
}
```

Four fields, and each carries its own job:

| Field | What it is |
|-------|------------|
| `type` | The CATEGORY. Several codes share one, and code paths key on this (`UPSTREAM_WARNING_TYPES`, the modal's `missing_field` / `po_mismatch` filter, `EntityMixin`-free row filters). A new code never needs a new type. |
| `severity` | `error` \| `warning` \| `info`. Chosen at the call site, not by the catalogue — the same code can be `info` or `warning` depending on which threshold it crossed (`price_variance_*`). |
| `message` | The backend's rendered English. It is the **fallback**, not the thing to render. |
| `code` | Stable identity for the SENTENCE. This is what a client localizes on. |
| `params` | The figures the sentence embeds, typed so a client can format them. |

Some entries carry extra keys beside these (`line_total_mismatch` merges the
reconciliation payload, `duplicate_similar` a `related_invoices` array,
`gl_account_invalid` the raw `codes` list, `extraction_self_correction` its
`check` name). Those are machine fields the UI reads structurally; they are
unaffected by the catalogue.

## One code per SENTENCE, not per type

`po_mismatch` is five different sentences (no PO, an amount variance, a partial
receipt, an over-receipt, an unquantified over-receipt) and `quality_hold` is
five more. A label keyed on `type` could only ever name one of them, which is
why `decisions.md` §155 filed this as needing a *parameterized* catalogue
rather than a label map.

`app/services/invoice_warning_catalog.py` is that catalogue. It declares, per
code: the `type` it belongs to, the English template, and each placeholder's
kind. `warning(code, severity, **params)` is the ONLY way a warning is built —
it renders `message` from the template, so the English and the parameters
cannot disagree about what the finding says.

## Composing a warning

```python
from app.services.invoice_warning_catalog import warning

warnings.append(
    warning("round_amount", "info", amount=invoice.amount, currency=invoice.currency)
)
```

Rules that hold at every call site:

- **Never build the dict by hand.** `tests/test_invoice_warning_catalog.py`
  AST-scans every producing module and fails on a literal carrying both `type`
  and `message` — a hand-rolled warning has no code, so it silently reaches the
  browser as server English and nothing else notices.
- **A word in the sentence selects the CODE, never a param.** `over` / `under`,
  `terminated` / `cancelled`, `(not-to-exceed)` — a client cannot splice an
  English word into a German sentence, so each variant is its own code
  (`price_variance_over` / `price_variance_under`,
  `contract_spend_limit_exceeded{,_not_to_exceed}`).
- **A count is a `count` param and the template uses an ICU plural.** The
  catalogue resolves those with English rules for the fallback; the client
  resolves the same block through `Intl.PluralRules`. `=0 {}` is how an
  optional trailing clause collapses (the `duplicate_similar` cross-entity
  tail) instead of doubling the code count.
- **Money is exact.** A `money` param is the `Decimal`'s own digits as a string,
  widened to two places when it has fewer. Never a float, never a symbol.
- **Nothing new becomes a param.** The params carry exactly what the composed
  sentence already carried. Bank details, tax ids and addresses are not
  warning parameters.

### Parameter kinds

| Kind | Emitted as | Rendered by the client as |
|------|-----------|---------------------------|
| `money` | exact decimal digits (`"5000.00"`) | `formatMoney` with the warning's `currency` |
| `currency` | ISO 4217 code, `USD` when absent | printed only by the English fallback |
| `percent` | the caller's own formatted figure (`"+20.0"`, `"98"`) | `Intl.NumberFormat` percent — precision and sign read off the digits |
| `number` | the caller's own formatted figure | `Intl.NumberFormat` decimal, same precision rule |
| `count` | an `int` | a number, so ICU plural can select on it |
| `date` | ISO `YYYY-MM-DD` | `formatDate` in the reader's locale |
| `text` | verbatim | verbatim |

`percent` and `number` keep the CALLER's precision because only the caller
knows what it measured — a duplicate similarity is whole percent, a PO variance
is one decimal place.

## The generated frontend catalogue

`pnpm gen:warning-messages` writes
`frontend/src/lib/api/invoiceWarningMessages.generated.ts` — a code → message-key
map plus the param-kind map — from the Python catalogue.
`pnpm check:warning-messages` is its drift guard and runs in CI's **Backend
lint** job, exactly as `check:einvoice-messages` does for the e-invoice rule
codes (`decisions.md` §95 established the shape).

Three guards in a chain, each catching the step after the one before it:

1. `--check` goes red when the catalogue gains or reworders a code;
2. once regenerated, `pnpm check` goes red because `en.ts` has no such key (the
   generated map is `satisfies Record<string, MessageKey>`);
3. once English exists, the locale-parity test demands the other five.

`frontend/src/lib/api/invoiceWarnings.ts` is the reader. It returns `null` —
and the caller renders `message` — for an unknown code **and** for a known code
whose params are incomplete, because `interpolate.ts` leaves an unfilled
`{placeholder}` intact and braces at a reviewer are worse than English prose.

## Old rows carry no code, and nothing backfills them

`refresh_warnings` rebuilds every category it owns from scratch on each invoice
write, so a row re-derives its codes the next time anything touches it. Until
then it has only `message`, and the fallback renders it. That is the normal
state of an untouched historical invoice, not an error — which is also why
`message` stays on the payload permanently rather than being removed once the
catalogue covers every code.

## What is NOT localized by this

- **`Invoice.po_match.issues`** — the matcher's own composed sentences, rendered
  verbatim in the modal's PO-match panel. A separate surface with a separate
  vocabulary.
- **`Exception.description`** — `_ensure_exception` is passed the same English
  prose (sometimes the warning's own `message`). The exception queue renders it
  raw.
- **The mobile app.** `InvoiceWarningsPanel` renders `message`, so mobile is
  unchanged — English, exactly as before, via the fallback that exists for it.
