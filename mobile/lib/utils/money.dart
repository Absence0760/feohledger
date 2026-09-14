import 'package:intl/intl.dart';

/// Money formatting for the whole app.
///
/// **A figure is formatted with the currency its own payload names.** Nine
/// screens and widgets each declared their own
/// `NumberFormat.currency(symbol: '\$')` at module level, so every amount on
/// them read as dollars regardless of what the org — or the row — was actually
/// denominated in. The invoice and contract detail screens rendered the proof
/// of it themselves: an `Amount` line wearing a `$` directly above a
/// `Currency` line reading `EUR`.
///
/// The rule this file exists to enforce, and the reason it takes [currency]
/// as a *parameter* rather than resolving one itself:
///
/// * **Per-row figures** (an invoice, a contract, a payment, a queue row) are
///   in the row's OWN currency. The org's reporting currency is not what they
///   are in — a GBP-reporting tenant holds USD invoices — so passing the org
///   code there would be a new wrong answer, not a fix.
/// * **Aggregates the server denominates in the reporting currency** get that
///   code: from the payload when it names one (`/payments/summary`'s
///   `currency`, `cash_position`'s `opening_balance_currency`, the dashboard's
///   `reporting.reporting_currency`), and otherwise from
///   `stores/org_currency_store.dart`, which resolves the same three settings
///   rungs the backend did when it denominated them.
/// * **When neither exists the figure renders BARE** — grouped digits, no
///   symbol. That is not a fallback, it is the answer: `PaymentResponse`'s own
///   contract says a `None` currency "is NOT a licence to substitute a
///   default … Render the bare figure rather than a code that cannot be
///   proven" (`docs/decisions.md` §79/§82). A missing symbol is a visible
///   gap; a wrong one is a wrong number that looks right.
///
/// Two entry points because money arrives in two shapes. [formatMoney] takes
/// the `num` the older dict endpoints already floated server-side;
/// [formatMoneyString] takes the exact decimal string the payment-queue,
/// cash-flow and adaptive payloads send and never lets a digit of it go
/// missing — an amount too large to survive a `double` is passed through
/// verbatim rather than rounded.

/// What a money slot reads when there is no figure at all — distinct from a
/// figure whose currency is unknown, which still renders its digits.
///
/// The same em dash the web's `utils/kpiValue.ts::KPI_NO_FIGURE` uses, for the
/// same reason: coercing an absent amount to `0` states a fact the server
/// never sent.
const String moneyNoFigure = '—';

/// A usable ISO 4217 code, or `null` when the value cannot be proven to be one.
///
/// The direct mirror of `frontend/src/lib/utils/reportingCurrency.ts`'s
/// `usableCode`, length check included: a 2- or 4-character string in a
/// currency field is a misconfiguration, and formatting against it would put
/// an invented symbol on a real figure.
String? normalizeCurrencyCode(String? value) {
  final code = (value ?? '').trim();
  return code.length == 3 ? code.toUpperCase() : null;
}

/// Format a numeric amount denominated in [currency].
///
/// [currency] `null` (or unusable) renders the grouped digits with no symbol.
/// [amount] `null` renders [placeholder] — there is no figure to show.
String formatMoney(
  num? amount, {
  String? currency,
  String? locale,
  String placeholder = moneyNoFigure,
}) {
  if (amount == null) return placeholder;
  final code = normalizeCurrencyCode(currency);
  if (code == null) {
    return NumberFormat.decimalPatternDigits(locale: locale, decimalDigits: 2)
        .format(amount);
  }
  return _currencyFormat(code, locale).format(amount);
}

/// [formatMoney] in the compact form the dashboard KPI tiles use — `$1.2M`
/// instead of `$1,200,000`, because the tile is one line wide.
String formatMoneyCompact(
  num? amount, {
  String? currency,
  String? locale,
  String placeholder = moneyNoFigure,
}) {
  if (amount == null) return placeholder;
  final code = normalizeCurrencyCode(currency);
  if (code == null) return NumberFormat.compact(locale: locale).format(amount);
  return NumberFormat.compactSimpleCurrency(locale: locale, name: code)
      .format(amount);
}

/// Format an **exact decimal money string** — the shape the payment-queue,
/// cash-flow and adaptive payloads send — denominated in [currency].
///
/// The string is the authority on its own digits, so it is returned verbatim
/// in every case where formatting it would risk changing them:
///
/// * no usable [currency] — there is no symbol to add, and the server's own
///   digit count is more informative than a re-grouped guess at its precision;
/// * not a number (a wire-format change, a pre-migration backend);
/// * more than [_maxExactDigits] significant digits, which cannot round-trip
///   through the `double` `NumberFormat` takes. A silently rounded cent is
///   worse than an unformatted figure.
String formatMoneyString(
  String? amount, {
  String? currency,
  String? locale,
  String placeholder = moneyNoFigure,
}) {
  final raw = (amount ?? '').trim();
  if (raw.isEmpty) return placeholder;
  final code = normalizeCurrencyCode(currency);
  if (code == null) return raw;
  if (_significantDigits(raw) > _maxExactDigits) return raw;
  final n = num.tryParse(raw);
  if (n == null) return raw;
  return _currencyFormat(code, locale).format(n);
}

/// `simpleCurrency` rather than `currency`: it resolves the short symbol for
/// the code (`€`, `¥`, `R`) and — the part a hand-rolled formatter gets wrong
/// — the currency's own minor-unit count, so JPY renders `¥1,235` with no
/// decimal places while USD renders `$1,234.56`. An unlisted code degrades to
/// itself as the symbol (`XYZ1,234.56`), which still names what the figure is
/// in.
NumberFormat _currencyFormat(String code, String? locale) =>
    NumberFormat.simpleCurrency(locale: locale, name: code);

/// A `double` carries 15 decimal significant digits losslessly. Past that a
/// money string cannot be formatted without changing it.
const int _maxExactDigits = 15;

int _significantDigits(String raw) {
  var seenNonZero = false;
  var count = 0;
  for (final unit in raw.codeUnits) {
    if (unit >= 0x30 && unit <= 0x39) {
      if (unit != 0x30) seenNonZero = true;
      if (seenNonZero) count++;
    }
  }
  return count;
}
