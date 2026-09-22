import 'package:feohledger_mobile/models/payment.dart' show unconvertedCountOf;
import 'package:feohledger_mobile/models/payment_queue.dart' show moneyToDisplay;

/// Cash-flow forecast + cash-position data for the CFO screen, from
/// `GET /api/analytics/cashflow_forecast` and `GET /api/analytics/cash_position`.
///
/// Money arrives as an **exact decimal string** — the backend never floats
/// currency across the API boundary. [moneyToDisplay] passes a string through
/// verbatim and still stringifies a JSON number, so a build running against a
/// pre-migration backend keeps rendering; either way the device NEVER does
/// float arithmetic on currency — every total the screen shows is
/// server-computed (forecast `totals`, cash-position `opening`/`closing`,
/// breach `shortfall`). This mirrors the payment-queue "money as string, never
/// client float math" invariant.

/// One bucketed forecast period from `cashflow_forecast.periods[]`.
class CashFlowForecastPeriod {
  final String period;
  final String scheduledAmountDisplay;
  final String committedAmountDisplay;
  final String pendingAmountDisplay;
  final String discountEligibleAmountDisplay;
  final int count;

  CashFlowForecastPeriod({
    required this.period,
    required this.scheduledAmountDisplay,
    required this.committedAmountDisplay,
    required this.pendingAmountDisplay,
    required this.discountEligibleAmountDisplay,
    required this.count,
  });

  factory CashFlowForecastPeriod.fromJson(Map<String, dynamic> json) {
    return CashFlowForecastPeriod(
      period: json['period'] as String? ?? '',
      scheduledAmountDisplay: moneyToDisplay(json['scheduled_amount']),
      committedAmountDisplay: moneyToDisplay(json['committed_amount']),
      pendingAmountDisplay: moneyToDisplay(json['pending_amount']),
      discountEligibleAmountDisplay:
          moneyToDisplay(json['discount_eligible_amount']),
      count: (json['count'] as num?)?.toInt() ?? 0,
    );
  }
}

/// `cashflow_forecast.totals` — the horizon-wide rollup, server-computed.
class CashFlowForecastTotals {
  final String scheduledAmountDisplay;
  final String committedAmountDisplay;
  final String pendingAmountDisplay;
  final String discountEligibleAmountDisplay;
  final int count;

  /// Commitments in the horizon with no exchange rate into the reporting
  /// currency, added to every total above (and to their periods) at FACE
  /// value — `totals.unconverted_count`. Non-zero means those totals mix
  /// currencies by that many rows.
  final int unconvertedCount;

  CashFlowForecastTotals({
    required this.scheduledAmountDisplay,
    required this.committedAmountDisplay,
    required this.pendingAmountDisplay,
    required this.discountEligibleAmountDisplay,
    required this.count,
    this.unconvertedCount = 0,
  });

  factory CashFlowForecastTotals.fromJson(Map<String, dynamic> json) {
    return CashFlowForecastTotals(
      scheduledAmountDisplay: moneyToDisplay(json['scheduled_amount']),
      committedAmountDisplay: moneyToDisplay(json['committed_amount']),
      pendingAmountDisplay: moneyToDisplay(json['pending_amount']),
      discountEligibleAmountDisplay:
          moneyToDisplay(json['discount_eligible_amount']),
      count: (json['count'] as num?)?.toInt() ?? 0,
      unconvertedCount: unconvertedCountOf(json['unconverted_count']),
    );
  }
}

/// One running-balance period from `cash_position.periods[]`.
class CashPositionPeriod {
  final String period;
  final String openingDisplay;
  final String outflowDisplay;
  final String closingDisplay;
  final bool belowThreshold;

  CashPositionPeriod({
    required this.period,
    required this.openingDisplay,
    required this.outflowDisplay,
    required this.closingDisplay,
    required this.belowThreshold,
  });

  factory CashPositionPeriod.fromJson(Map<String, dynamic> json) {
    return CashPositionPeriod(
      period: json['period'] as String? ?? '',
      openingDisplay: moneyToDisplay(json['opening']),
      outflowDisplay: moneyToDisplay(json['outflow']),
      closingDisplay: moneyToDisplay(json['closing']),
      belowThreshold: json['below_threshold'] as bool? ?? false,
    );
  }
}

/// One low-balance breach from `cash_position.breaches[]`.
class CashPositionBreach {
  final String period;
  final String closingDisplay;
  final String shortfallDisplay;

  CashPositionBreach({
    required this.period,
    required this.closingDisplay,
    required this.shortfallDisplay,
  });

  factory CashPositionBreach.fromJson(Map<String, dynamic> json) {
    return CashPositionBreach(
      period: json['period'] as String? ?? '',
      closingDisplay: moneyToDisplay(json['closing']),
      shortfallDisplay: moneyToDisplay(json['shortfall']),
    );
  }
}

/// The combined payload the [CashFlowStore] builds from the two endpoints.
class CashFlowData {
  final int horizonDays;
  final String granularity;

  // Forecast leg.
  final List<CashFlowForecastPeriod> forecastPeriods;
  final CashFlowForecastTotals totals;

  // Cash-position leg.
  final String openingBalanceDisplay;
  final String openingBalanceSource;

  /// What EVERY figure on this screen is denominated in — the org's reporting
  /// currency, straight from `cash_position.opening_balance_currency`.
  ///
  /// The cash-position endpoint names it because the whole running curve has
  /// to be in one currency to mean anything (it REFUSES a provider balance in
  /// another, on exactly this ground), and `cashflow_forecast` resolves the
  /// same code for the same request without naming it — so this one field
  /// labels both legs. `null` when the position leg landed nothing, in which
  /// case the screen falls back to `OrgCurrencyStore`, which resolves the same
  /// settings rungs the server did.
  final String? openingBalanceCurrency;
  final String? thresholdDisplay;
  final List<CashPositionPeriod> positionPeriods;
  final List<CashPositionBreach> breaches;

  /// Commitments subtracted from the running balance at FACE value because no
  /// exchange rate bridged them into the reporting currency —
  /// `cash_position.unconverted_count`, the outflow half of the currency guard
  /// [openingBalanceCurrency] already applies to the opening balance.
  ///
  /// Non-zero means every closing balance from the first affected period on
  /// is a mixed-currency number: the curve carries the balance forward, so
  /// one unconvertible row poisons the tail, including
  /// [projectedEndBalanceDisplay].
  final int positionUnconvertedCount;

  CashFlowData({
    required this.horizonDays,
    required this.granularity,
    required this.forecastPeriods,
    required this.totals,
    required this.openingBalanceDisplay,
    required this.openingBalanceSource,
    this.openingBalanceCurrency,
    this.thresholdDisplay,
    required this.positionPeriods,
    required this.breaches,
    this.positionUnconvertedCount = 0,
  });

  /// The projected end balance is the closing balance of the LAST position
  /// period (server-computed); empty horizon falls back to the opening balance.
  String get projectedEndBalanceDisplay => positionPeriods.isEmpty
      ? openingBalanceDisplay
      : positionPeriods.last.closingDisplay;

  bool get hasBreach => breaches.isNotEmpty;

  factory CashFlowData.fromJson({
    required Map<String, dynamic> forecast,
    required Map<String, dynamic> position,
  }) {
    final forecastPeriods = (forecast['periods'] as List<dynamic>?)
            ?.map((p) =>
                CashFlowForecastPeriod.fromJson(p as Map<String, dynamic>))
            .toList() ??
        [];
    final positionPeriods = (position['periods'] as List<dynamic>?)
            ?.map(
                (p) => CashPositionPeriod.fromJson(p as Map<String, dynamic>))
            .toList() ??
        [];
    final breaches = (position['breaches'] as List<dynamic>?)
            ?.map(
                (b) => CashPositionBreach.fromJson(b as Map<String, dynamic>))
            .toList() ??
        [];

    return CashFlowData(
      horizonDays: (forecast['horizon_days'] as num?)?.toInt() ?? 0,
      granularity: forecast['granularity'] as String? ?? 'week',
      forecastPeriods: forecastPeriods,
      totals: CashFlowForecastTotals.fromJson(
        forecast['totals'] as Map<String, dynamic>? ?? {},
      ),
      openingBalanceDisplay: moneyToDisplay(position['opening_balance']),
      openingBalanceSource:
          position['opening_balance_source'] as String? ?? 'none',
      openingBalanceCurrency: position['opening_balance_currency'] as String?,
      thresholdDisplay: position['threshold'] == null
          ? null
          : moneyToDisplay(position['threshold']),
      positionPeriods: positionPeriods,
      breaches: breaches,
      positionUnconvertedCount: unconvertedCountOf(position['unconverted_count']),
    );
  }
}
