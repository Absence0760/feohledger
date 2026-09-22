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

`po_mismatch` is seven different sentences (no PO, an amount variance, an amount
variance against a PO that records no currency, a currency mismatch, a partial
receipt, an over-receipt, an unquantified over-receipt) and `quality_hold` is
five more. The two currency codes (`po_amount_variance_po_currency_unknown`,
`po_currency_mismatch` — `docs/decisions.md` §197) show why a `money` kind is not
always right for a money figure: a warning has ONE `currency` param to format
its money with, so a PO total whose currency is unknown rides a `number` param
and renders unlabelled, and two codes that are the finding itself ride `text`. A label keyed on `type` could only ever name one of them, which is
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

## The generated client catalogues (web and mobile)

`pnpm gen:warning-messages` (`scripts/gen_invoice_warning_messages.py`) writes
**two** files from the Python catalogue, in one run:

| File | Contents | Reader |
|------|----------|--------|
| `frontend/src/lib/api/invoiceWarningMessages.generated.ts` | code → message-key map + param-kind map | `frontend/src/lib/api/invoiceWarnings.ts` |
| `mobile/lib/l10n/invoice_warning_messages.generated.dart` | the same param-kind map + a `switch` arm per code calling its `AppLocalizations` method | `mobile/lib/l10n/invoice_warning_messages.dart` (of which it is a `part`) |

They differ in shape because the clients do: the web looks a message up by its
key string, while a gen-l10n class exposes each message as its own typed method,
so Dart needs one call per code with its arguments in order. **That order, and
each argument's type, comes from `mobile/lib/l10n/app_en.arb`'s placeholder
metadata** — what `flutter gen-l10n` builds the signature from — not from the
catalogue's own parameter order: two `String` arguments passed swapped would
compile and render a PO number where the amount belongs. An ARB whose
placeholders don't match the catalogue (a missing or extra one, an `int` on a
non-`count` parameter) makes the generator refuse rather than write a half-right
file. `currency` parameters are not placeholders on either client: they are how
the `money` beside them is formatted.

`pnpm check:warning-messages` is the drift guard for **both** files and runs in
CI's **Backend lint** job, exactly as `check:einvoice-messages` does for the
e-invoice rule codes (`decisions.md` §95 established the shape). It names which
file is stale — including the mobile one alone when only the English ARB's
placeholder order moved.

Three guards in a chain per client, each catching the step after the one before
it:

1. `--check` goes red when the catalogue gains or rewords a code;
2. once regenerated, the web's `pnpm check` goes red because `en.ts` has no such
   key (the generated map is `satisfies Record<string, MessageKey>`), and
   mobile's `flutter analyze` goes red because the generated arm calls a method
   `app_en.arb` does not declare;
3. once English exists, each client's locale-parity test demands the other
   translations (`messages_parity` on the web, `test/l10n/arb_parity_test.dart`
   on mobile).

Round 26 hand-transcribed the 48 Dart arms and backstopped them with a flutter
test that parsed the TypeScript; generating both halves from one source is what
retired it (`decisions.md` §195). The generator's own tests (`tests/test_invoice_warning_catalog.py`)
pin both files in sync, the ARB-order read, and the refusal paths.

Both readers return `null` — and the caller renders `message` — for an unknown
code **and** for a known code whose params are incomplete: on the web
`interpolate.ts` leaves an unfilled `{placeholder}` intact, and braces at a
reviewer are worse than English prose.

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
