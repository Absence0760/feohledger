import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/adaptive_screen.dart';
import 'package:feohledger_mobile/stores/adaptive_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';

Widget _localized(Widget home) => MaterialApp(
  localizationsDelegates: AppLocalizations.localizationsDelegates,
  supportedLocales: AppLocalizations.supportedLocales,
  home: home,
);

http.Response _json(Object body, [int status = 200]) => http.Response(
  jsonEncode(body),
  status,
  headers: {'content-type': 'application/json'},
);

Map<String, dynamic> _me(List<String> roles) => {
  'id': 'u1',
  'email': 'demo@acme.com',
  'full_name': 'Demo User',
  'organization_id': 'org1',
  'roles': roles,
};

Map<String, dynamic> _suggestion(String id, {String status = 'open'}) => {
  'id': id,
  'kind': 'auto_approve_threshold',
  'vendor_id': 'v1',
  'vendor_name': 'Acme',
  'title': 'Vendor Acme: 14/14 invoices approved unmodified',
  'rationale': '14 invoices approved with no corrections and 0 rejections.',
  'payload': {'suggested_threshold': '2000.00'},
  'confidence_pct': '95.00',
  'status': status,
  'created_at': '2026-03-01T10:00:00',
  'dismissed_at': null,
};

Map<String, dynamic> _patterns({int unconverted = 0}) => {
  'generated_at': '2026-03-04T10:00:00',
  'lookback_days': 180,
  'entity_id': null,
  'approvers': [
    {
      'approver_id': 'u1',
      'approver_name': 'Ada Lovelace',
      'approved_count': 12,
      'rejected_count': 1,
      'approval_rate_pct': '92.31',
      'median_time_to_approve_days': '1.50',
      'avg_time_to_approve_days': '2.10',
      'sample_size': 13,
    },
  ],
  'vendors': [
    {
      'vendor_id': 'v1',
      'vendor_name': 'Acme Supplies',
      'approved_count': 14,
      'rejected_count': 0,
      'approval_rate_pct': '100.00',
      'unmodified_count': 14,
      'consistency_pct': '100.00',
      'avg_approved_amount': '1200.00',
      'median_approved_amount': '1150.00',
      'min_approved_amount': '400.00',
      'max_approved_amount': '1900.00',
      'sample_size': 14,
      'unconverted_count': unconverted,
    },
  ],
};

Map<String, dynamic> _anomalies({bool flagged = true}) => {
  'total_scanned': 7,
  'flagged': flagged
      ? [
          {
            'invoice_id': 'inv1',
            'vendor_id': 'v1',
            'vendor_name': 'Acme Supplies',
            'amount': '9000.00',
            'amount_currency': 'EUR',
            'insufficient_history': false,
            'flags': [
              {
                'code': 'amount_outlier',
                'severity': 'warning',
                'message': 'Amount is far above this vendor’s usual range.',
                'observed': '9000.00',
                'expected': '1200.00',
              },
            ],
          },
        ]
      : <Map<String, dynamic>>[],
};

/// Serves all three adaptive reads; POST is the dismiss.
MockClient _adaptiveClient({
  void Function(String path)? onDismiss,
  int dismissStatus = 200,
  Map<String, dynamic>? patterns,
  Map<String, dynamic>? anomalies,
  List<Map<String, dynamic>>? suggestions,
  // The reporting currency `OrgCurrencyStore` resolves out of
  // `GET /api/organization`. `null` serves settings declaring none, which is
  // what leaves the per-vendor figures bare and the section note on.
  String? reportingCurrency,
}) {
  return MockClient((req) async {
    final path = req.url.path;
    if (path.endsWith('/organization')) {
      return _json({
        'id': 'org1',
        'name': 'Acme Corp',
        'slug': 'acme',
        'plan': 'pro',
        'created_at': '2026-01-01T00:00:00',
        'settings': {'reporting_currency': ?reportingCurrency},
      });
    }
    if (req.method == 'POST' && path.endsWith('/dismiss')) {
      onDismiss?.call(path);
      if (dismissStatus != 200) {
        return _json({'detail': 'Your role does not permit this.'}, dismissStatus);
      }
      return _json({
        'suggestions': [_suggestion('s1', status: 'dismissed')],
      });
    }
    if (path.endsWith('/approval-patterns')) {
      return _json(patterns ?? _patterns());
    }
    if (path.endsWith('/anomalies')) {
      return _json(anomalies ?? _anomalies());
    }
    return _json({'suggestions': suggestions ?? [_suggestion('s1')]});
  });
}

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 30 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

Future<void> _pumpUntilTrue(WidgetTester tester, bool Function() done) async {
  for (var i = 0; i < 30 && !done(); i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    AdaptiveStore.instance.reset();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  Future<void> loginThen(List<String> roles, MockClient screenClient) async {
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        if (req.url.path == '/api/auth/login') {
          return _json({'access_token': 'tok'});
        }
        return _json(_me(roles));
      }),
    );
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
    ApiClient().debugConfigure(client: screenClient);
  }

  testWidgets('renders the three tabs', (tester) async {
    await loginThen(['ap_manager'], _adaptiveClient());

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.text('Suggestions'));

    expect(find.widgetWithText(Tab, 'Suggestions'), findsOneWidget);
    expect(find.widgetWithText(Tab, 'Approval patterns'), findsOneWidget);
    expect(find.widgetWithText(Tab, 'Anomalies'), findsOneWidget);
  });

  testWidgets('the suggestions tab renders the row and the advisory note',
      (tester) async {
    await loginThen(['ap_manager'], _adaptiveClient());

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(
      tester,
      find.text('Vendor Acme: 14/14 invoices approved unmodified'),
    );

    expect(
      find.text('Vendor Acme: 14/14 invoices approved unmodified'),
      findsOneWidget,
    );
    expect(find.text('Confidence 95.00%'), findsOneWidget);
    expect(find.text('Open'), findsWidgets);
    // Nothing here has changed a workflow, and the screen says so.
    expect(find.textContaining('Everything here is advisory'), findsOneWidget);
  });

  testWidgets('a manager can dismiss, via a confirm dialog', (tester) async {
    String? dismissedPath;
    await loginThen(
      ['ap_manager'],
      _adaptiveClient(onDismiss: (p) => dismissedPath = p),
    );

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.text('Dismiss'));

    await tester.tap(find.widgetWithText(TextButton, 'Dismiss'));
    await tester.pumpAndSettle();
    expect(find.text('Dismiss this suggestion?'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Dismiss'));
    await _pumpUntilTrue(tester, () => dismissedPath != null);
    await tester.pumpAndSettle();

    expect(dismissedPath, '/api/adaptive/suggestions/s1/dismiss');
    // Under the Open filter the dismissed row leaves the list.
    expect(
      find.text('Vendor Acme: 14/14 invoices approved unmodified'),
      findsNothing,
    );
    expect(find.text('Suggestion dismissed.'), findsWidgets);
  });

  testWidgets('cancelling the dialog dismisses nothing', (tester) async {
    String? dismissedPath;
    await loginThen(
      ['admin'],
      _adaptiveClient(onDismiss: (p) => dismissedPath = p),
    );

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.text('Dismiss'));

    await tester.tap(find.widgetWithText(TextButton, 'Dismiss'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
    await tester.pumpAndSettle();

    expect(dismissedPath, isNull);
    expect(
      find.text('Vendor Acme: 14/14 invoices approved unmodified'),
      findsOneWidget,
    );
  });

  testWidgets('a CFO reads the suggestions and is offered no dismiss',
      (tester) async {
    // `_WRITE_ROLES` on the backend is admin | ap_manager; a CFO would get a
    // 403, so the control is not rendered rather than rendered and refused.
    await loginThen(['cfo'], _adaptiveClient());

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(
      tester,
      find.text('Vendor Acme: 14/14 invoices approved unmodified'),
    );

    expect(find.text('Dismiss'), findsNothing);
  });

  testWidgets('a failed dismiss surfaces the server refusal', (tester) async {
    await loginThen(['admin'], _adaptiveClient(dismissStatus: 403));

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.text('Dismiss'));

    await tester.tap(find.widgetWithText(TextButton, 'Dismiss'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Dismiss'));
    await tester.pumpAndSettle();

    expect(
      find.textContaining('Your role does not permit this.'),
      findsOneWidget,
    );
    // The row stays — nothing was dismissed.
    expect(
      find.text('Vendor Acme: 14/14 invoices approved unmodified'),
      findsOneWidget,
    );
  });

  testWidgets('the show filter asks the server for all statuses',
      (tester) async {
    final statuses = <String?>[];
    await loginThen(
      ['ap_manager'],
      MockClient((req) async {
        final path = req.url.path;
        if (path.endsWith('/suggestions')) {
          statuses.add(req.url.queryParameters['status']);
          return _json({'suggestions': [_suggestion('s1')]});
        }
        if (path.endsWith('/approval-patterns')) return _json(_patterns());
        return _json(_anomalies());
      }),
    );

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.widgetWithText(FilterChip, 'All'));

    await tester.tap(find.widgetWithText(FilterChip, 'All'));
    await _pumpUntilTrue(tester, () => statuses.length >= 2);

    expect(statuses, ['open', 'all']);
  });

  testWidgets('the suggestions tab has its own empty state', (tester) async {
    await loginThen(['ap_manager'], _adaptiveClient(suggestions: []));

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.textContaining('No suggestions'));

    expect(find.textContaining('No suggestions'), findsOneWidget);
  });

  testWidgets('the suggestions tab has its own error state with Retry',
      (tester) async {
    await loginThen(
      ['ap_manager'],
      MockClient((req) async {
        if (req.url.path.endsWith('/suggestions')) {
          return http.Response('boom', 500);
        }
        if (req.url.path.endsWith('/approval-patterns')) {
          return _json(_patterns());
        }
        return _json(_anomalies());
      }),
    );

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.text('Could not load the suggestions.'));

    expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
  });

  testWidgets('the patterns tab renders both sections', (tester) async {
    await loginThen(['cfo'], _adaptiveClient(patterns: _patterns(unconverted: 2)));

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.text('Suggestions'));
    await tester.tap(find.widgetWithText(Tab, 'Approval patterns'));
    await tester.pumpAndSettle();

    expect(find.text('By approver'), findsOneWidget);
    expect(find.text('Ada Lovelace'), findsOneWidget);
    expect(find.text('12 approved · 1 rejected'), findsWidgets);
    expect(find.text('By vendor'), findsOneWidget);
    expect(find.text('Acme Supplies'), findsOneWidget);
    // This org declares no usable currency in its settings, so neither the
    // payload nor `OrgCurrencyStore` can name one: the figures render as the
    // exact strings the backend sent, and the section note is what says what
    // they are in.
    expect(find.text('Median 1150.00 · average 1200.00'), findsOneWidget);
    expect(
      find.textContaining('reporting currency'),
      findsWidgets,
    );
    // Excluded approvals are disclosed, because the sample still counts them.
    expect(
      find.textContaining('2 approvals could not be expressed'),
      findsOneWidget,
    );
  });

  testWidgets(
      'per-vendor amounts wear the org reporting currency once it resolves',
      (tester) async {
    // The patterns tab is the surface that had to opt out of money formatting:
    // its figures are in the org's reporting currency and `approval-patterns`
    // does not name it, so rather than stamp the `\$` the other four screens
    // asserted, it rendered raw strings under a note. `OrgCurrencyStore`
    // resolves the same settings rungs the server used to denominate them, so
    // the figures can now carry the real symbol — and the note, whose only job
    // was to cover for its absence, goes away.
    await loginThen(
      ['cfo'],
      _adaptiveClient(reportingCurrency: 'ZAR'),
    );

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.text('Suggestions'));
    await tester.tap(find.widgetWithText(Tab, 'Approval patterns'));
    await tester.pumpAndSettle();

    expect(find.text('Median R1,150.00 · average R1,200.00'), findsOneWidget);
    expect(find.textContaining('reporting currency.'), findsNothing);
    expect(find.textContaining(r'$'), findsNothing);
  });

  testWidgets('the anomalies tab renders the scan count and each flag',
      (tester) async {
    await loginThen(['ap_manager'], _adaptiveClient());

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.text('Suggestions'));
    await tester.tap(find.widgetWithText(Tab, 'Anomalies'));
    await tester.pumpAndSettle();

    // The scanned count is what makes an empty list mean "nothing stands out".
    expect(find.text('7 invoices in review scanned.'), findsOneWidget);
    expect(find.text('Acme Supplies'), findsOneWidget);
    // Labelled with the currency the API said the figure is in.
    expect(find.text('9000.00 EUR'), findsOneWidget);
    expect(
      find.text('Amount is far above this vendor’s usual range.'),
      findsOneWidget,
    );
  });

  testWidgets('an empty anomaly scan still reports what it looked at',
      (tester) async {
    await loginThen(
      ['ap_manager'],
      _adaptiveClient(anomalies: _anomalies(flagged: false)),
    );

    await tester.pumpWidget(_localized(const AdaptiveScreen()));
    await _pumpUntil(tester, find.text('Suggestions'));
    await tester.tap(find.widgetWithText(Tab, 'Anomalies'));
    await tester.pumpAndSettle();

    expect(find.text('7 invoices in review scanned.'), findsOneWidget);
    expect(
      find.textContaining('outside its supplier’s normal pattern'),
      findsOneWidget,
    );
  });
}
