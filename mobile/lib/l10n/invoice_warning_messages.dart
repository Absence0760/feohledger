/// Turning an `InvoiceWarning` into a sentence in the reader's language — the
/// mobile half of `backend/app/services/invoice_warning_catalog.py`, and the
/// mirror of the web `frontend/src/lib/api/invoiceWarnings.ts`.
///
/// The backend composes each finding from the row's own data ("PO 4412 not
/// found", a variance, an amount) and emits a stable `code` plus typed
/// `params` beside the English `message`. This maps that pair onto an ARB key
/// and a set of locale-formatted parameters.
///
/// Three properties make it safe to render, and they are the web module's
/// three:
///
/// * **Unknown or absent code → `null`**, and the caller renders
///   `warning.message`, the backend's own sentence. Every warning persisted
///   before the catalogue existed carries no code at all (`refresh_warnings`
///   re-derives one on the invoice's next write, and nothing backfills), so
///   the fallback is the NORMAL path for an untouched row, not an edge case.
/// * **A known code with a MISSING parameter → `null` too.** The sentence
///   would render a bare `{poNumber}` at a reviewer; the server's complete
///   English one is strictly better than a half-filled translated one.
/// * **A parameter that is present but not the shape its kind expects renders
///   VERBATIM**, inside the localized sentence — it is not a fallback trigger.
///   The value is the server's own characters either way, so the choice is
///   only which language surrounds them, and the fallback path would print the
///   identical fragment inside the English sentence. It is the same rule
///   `formatMoneyString` and `utils/numbers.dart` already follow for a figure
///   they cannot format: the server's own text beats a blank.
///   **The one exception is a plural selector**, which must be an integer
///   because it chooses an ARM of the message rather than filling a slot —
///   `_count` returns `null` and the whole finding falls back.
///
/// **Nothing that names a code is hand-written.** The code → sentence
/// dispatch and the parameter-kind map live in
/// `invoice_warning_messages.generated.dart`, a `part` of this library that
/// `pnpm gen:warning-messages` writes from
/// `backend/app/services/invoice_warning_catalog.py` — in the same run, from
/// the same catalogue, as the web's
/// `frontend/src/lib/api/invoiceWarningMessages.generated.ts` — so the two
/// surfaces cannot hold different codes or different parameter kinds.
/// `pnpm check:warning-messages` (CI's Backend lint job) fails when either file
/// is stale, and a generated arm that calls a message `app_en.arb` does not
/// declare fails `flutter analyze`. What stays here is what the generator
/// cannot know: how each parameter KIND is formatted, and the rules above.
library;

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/utils/dates.dart';
import 'package:feohledger_mobile/utils/money.dart';
import 'package:feohledger_mobile/utils/numbers.dart';

part 'invoice_warning_messages.generated.dart';

/// The sentence to show for one warning: localized when the code is known,
/// otherwise the backend's own English `message`.
///
/// Every render site goes through this, so none of them can end up showing a
/// finding in a different language from its neighbour.
String invoiceWarningText(AppLocalizations l, InvoiceWarning w) =>
    localizeInvoiceWarning(l, w) ?? w.message;

/// The localized sentence, or `null` when this build cannot state the finding
/// — see the module note. Split out so a test can tell "fell back" apart from
/// "localized to something that happens to equal the English".
String? localizeInvoiceWarning(AppLocalizations l, InvoiceWarning w) {
  final p = w.params;
  // A money parameter is denominated in the invoice's own currency, which the
  // backend puts in the params beside it. Absent means the figure renders
  // bare — never a substituted default (`docs/decisions.md` §79/§82).
  return _localizeWarningCode(l, w.code, p, p['currency']);
}

// Each formatter takes the same (raw, currency) pair so the generated arms can
// be written one way per kind; only `_money` has anything to do with the
// currency.

/// A plural selector: an integer, because it drives ICU plural selection.
int? _count(String? raw) => raw == null ? null : int.tryParse(raw.trim());

String? _text(String? raw, String? currency) => raw;

/// Exact decimal digits off the wire, in the invoice's own currency.
String? _money(String? raw, String? currency) =>
    raw == null ? null : formatMoneyString(raw, currency: currency);

String? _percent(String? raw, String? currency) =>
    raw == null ? null : formatPercentString(raw);

String? _number(String? raw, String? currency) =>
    raw == null ? null : formatDecimalString(raw);

String? _date(String? raw, String? currency) {
  if (raw == null) return null;
  final parsed = DateTime.tryParse(raw);
  // Not a date this build can parse: the server's own string beats a blank.
  return parsed == null ? raw : formatDate(parsed);
}
