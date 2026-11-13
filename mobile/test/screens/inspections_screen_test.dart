import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/models/inspection.dart';
import 'package:feohledger_mobile/screens/inspection_detail_screen.dart';
import 'package:feohledger_mobile/screens/inspections_screen.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/inspection_store.dart';
import 'package:feohledger_mobile/widgets/inspection_list_tile.dart';

Widget _localized(Widget home) => MaterialApp(
  localizationsDelegates: appLocalizationsDelegates,
  supportedLocales: AppLocalizations.supportedLocales,
  home: home,
);

http.Response _json(Object body, [int status = 200]) => http.Response(
  jsonEncode(body),
  status,
  headers: {'content-type': 'application/json'},
);

http.Response _list(List<Map<String, dynamic>> items, {int? total}) =>
    _json({
      'items': items,
      'total': total ?? items.length,
      'page': 1,
      'page_size': 20,
    });

Map<String, dynamic> _me(List<String> roles) => {
  'id': 'u1',
  'email': 'demo@acme.com',
  'full_name': 'Demo User',
  'organization_id': 'org1',
  'roles': roles,
};

Map<String, dynamic> _inspectionJson(
  String id, {
  String number = 'QI-1',
  String result = 'pass',
  String? grNumber = 'GR-1',
  String? grId = 'gr1',
  String? poId = 'po1',
  String? notes,
}) => {
  'id': id,
  'inspection_number': number,
  'po_id': poId,
  'gr_id': grId,
  'gr_number': grNumber,
  'result': result,
  'inspected_date': '2026-03-04',
  'inspector': 'Dana',
  'accepted_quantity': 9.0,
  'rejected_quantity': 1.0,
  'deviation_notes': notes,
  'status': 'completed',
  'created_at': '2026-03-04T09:00:00',
};

Map<String, dynamic> _receiptJson(String id) => {
  'id': id,
  'gr_number': 'GR-$id',
  'po_id': 'po-$id',
  'po_number': 'PO-$id',
  'received_date': '2026-03-01',
  'status': 'received',
  'line_count': 1,
};

Future<void> _pumpUntil(WidgetTester tester, Finder finder) async {
  for (var i = 0; i < 30 && finder.evaluate().isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

/// The sheet's `partial` segment. The same label is also a filter chip behind
/// the sheet, so the finder is scoped to the segmented control.
final _partialSegment = find.descendant(
  of: find.byType(SegmentedButton<InspectionResult>),
  matching: find.text('Partial acceptance'),
);

Future<void> _pumpUntilTrue(WidgetTester tester, bool Function() done) async {
  for (var i = 0; i < 30 && !done(); i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    InspectionStore.instance.reset();
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure();
  });

  /// Put the given roles on the AuthStore via a one-shot login, then swap in
  /// [screenClient] for the screen under test.
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

  group('InspectionsScreen', () {
    testWidgets('renders one tile per inspection once loaded', (tester) async {
      await loginThen(
        ['ap_manager'],
        MockClient(
          (req) async => _list([
            _inspectionJson('1', number: 'QI-A'),
            _inspectionJson('2', number: 'QI-B', result: 'fail'),
          ]),
        ),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));

      expect(find.byType(InspectionListTile), findsNWidgets(2));
      expect(find.text('QI-A'), findsOneWidget);
      expect(find.text('QI-B'), findsOneWidget);
      // The outcome badge renders its localized label.
      expect(find.text('Pass'), findsWidgets);
      expect(find.text('Fail'), findsWidgets);
      expect(find.byType(CircularProgressIndicator), findsNothing);
    });

    testWidgets('shows a spinner while the first page is in flight',
        (tester) async {
      await loginThen(
        ['ap_clerk'],
        MockClient((req) async {
          await Future<void>.delayed(const Duration(milliseconds: 300));
          return _list([]);
        }),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 50));

      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      // Let the request finish so the test doesn't leave a pending timer.
      await _pumpUntil(tester, find.text('No quality inspections recorded.'));
    });

    testWidgets('shows the unfiltered empty state', (tester) async {
      await loginThen(['ap_manager'], MockClient((req) async => _list([])));

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.text('No quality inspections recorded.'));

      expect(find.text('No quality inspections recorded.'), findsOneWidget);
    });

    testWidgets('a filtered empty result says the FILTER is empty, not the set',
        (tester) async {
      await loginThen(['ap_manager'], MockClient((req) async => _list([])));

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.text('No quality inspections recorded.'));

      await tester.tap(find.widgetWithText(FilterChip, 'Fail'));
      await _pumpUntil(tester, find.text('No inspections with this outcome.'));

      expect(find.text('No inspections with this outcome.'), findsOneWidget);
      expect(find.text('No quality inspections recorded.'), findsNothing);
    });

    testWidgets('shows an error state with Retry, and Retry refetches',
        (tester) async {
      var calls = 0;
      await loginThen(
        ['ap_manager'],
        MockClient((req) async {
          calls++;
          return calls == 1
              ? http.Response('boom', 500)
              : _list([_inspectionJson('1')]);
        }),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.text('Could not load inspections'));

      expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
      await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
      await _pumpUntil(tester, find.byType(InspectionListTile));

      expect(find.byType(InspectionListTile), findsOneWidget);
    });

    testWidgets('the outcome chips send ?result= to the server', (tester) async {
      final results = <String?>[];
      await loginThen(
        ['ap_manager'],
        MockClient((req) async {
          results.add(req.url.queryParameters['result']);
          return _list([_inspectionJson('1')]);
        }),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));

      expect(find.widgetWithText(FilterChip, 'All'), findsOneWidget);
      expect(find.widgetWithText(FilterChip, 'Pass'), findsOneWidget);
      expect(find.widgetWithText(FilterChip, 'Fail'), findsOneWidget);
      expect(find.widgetWithText(FilterChip, 'Partial acceptance'), findsOneWidget);

      await tester.tap(find.widgetWithText(FilterChip, 'Partial acceptance'));
      await _pumpUntilTrue(tester, () => results.length >= 2);

      expect(results, [null, 'partial']);
    });

    testWidgets('a manager gets the record affordance', (tester) async {
      await loginThen(
        ['ap_manager'],
        MockClient((req) async => _list([_inspectionJson('1')])),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));

      expect(find.byType(FloatingActionButton), findsOneWidget);
    });

    testWidgets('a clerk sees the queue and NO record affordance',
        (tester) async {
      // The list is role-open on the backend (`get_current_user`) while the
      // create is admin/ap_manager — a clerk chasing a quality hold needs to
      // read a failed inspection, and must not be handed a guaranteed 403.
      await loginThen(
        ['ap_clerk'],
        MockClient((req) async => _list([_inspectionJson('1')])),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));

      expect(find.byType(InspectionListTile), findsOneWidget);
      expect(find.byType(FloatingActionButton), findsNothing);
    });

    testWidgets('recording posts the draft and refetches', (tester) async {
      Map<String, dynamic>? posted;
      var listCalls = 0;
      await loginThen(
        ['admin'],
        MockClient((req) async {
          if (req.method == 'POST' && req.url.path == '/api/inspections') {
            posted = jsonDecode(req.body) as Map<String, dynamic>;
            return _json(_inspectionJson('new', number: 'QI-GR-a'), 201);
          }
          if (req.url.path == '/api/goods-receipts') {
            return _list([_receiptJson('a')], total: 1);
          }
          listCalls++;
          return _list([_inspectionJson('1')]);
        }),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));

      await tester.tap(find.byType(FloatingActionButton));
      // The receipt page loads BEFORE the sheet opens — the form cannot be
      // submitted without the one thing it would otherwise still be fetching.
      await _pumpUntil(tester, find.text('Record Quality Inspection'));
      await tester.pumpAndSettle();

      // The number is suggested from the selected receipt.
      expect(find.text('QI-GR-a'), findsOneWidget);
      // A `pass` hides the quantity fields; switching to partial shows them.
      expect(find.text('Accepted quantity'), findsNothing);
      await tester.tap(_partialSegment);
      await tester.pumpAndSettle();
      expect(find.text('Accepted quantity'), findsOneWidget);

      await tester.enterText(
        find.widgetWithText(TextFormField, 'Accepted quantity'),
        '6.5',
      );
      // The form is taller than the test viewport (it is a scrolling sheet on a
      // phone too), so bring the submit button into view before tapping it.
      await tester.ensureVisible(
        find.widgetWithText(FilledButton, 'Record inspection'),
      );
      await tester.pumpAndSettle();
      await tester.tap(
        find.widgetWithText(FilledButton, 'Record inspection'),
      );
      await _pumpUntilTrue(tester, () => posted != null && listCalls >= 2);

      expect(posted!['gr_id'], 'a');
      expect(posted!['po_id'], 'po-a');
      expect(posted!['result'], 'partial');
      // String, not a JSON number — the digits reach Numeric(12,4) untouched.
      expect(posted!['accepted_quantity'], '6.5');
      expect(listCalls, greaterThanOrEqualTo(2));
    });

    testWidgets('a partial acceptance will not submit without a quantity',
        (tester) async {
      var posts = 0;
      await loginThen(
        ['admin'],
        MockClient((req) async {
          if (req.method == 'POST' && req.url.path == '/api/inspections') {
            posts++;
            return _json(_inspectionJson('new'), 201);
          }
          if (req.url.path == '/api/goods-receipts') {
            return _list([_receiptJson('a')], total: 1);
          }
          return _list([_inspectionJson('1')]);
        }),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));
      await tester.tap(find.byType(FloatingActionButton));
      await _pumpUntil(tester, find.text('Record Quality Inspection'));
      await tester.pumpAndSettle();

      await tester.tap(_partialSegment);
      await tester.pumpAndSettle();
      await tester.ensureVisible(
        find.widgetWithText(FilledButton, 'Record inspection'),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Record inspection'));
      await tester.pumpAndSettle();

      // The accepted quantity is what the matcher renders into its
      // partial-acceptance issue, so a partial without one is refused here.
      expect(find.text('Required for a partial acceptance'), findsOneWidget);
      expect(posts, 0);
      expect(find.text('Record Quality Inspection'), findsOneWidget);
    });

    testWidgets('the picker discloses that it holds one page, not every receipt',
        (tester) async {
      await loginThen(
        ['admin'],
        MockClient((req) async {
          if (req.url.path == '/api/goods-receipts') {
            return _list([_receiptJson('a'), _receiptJson('b')], total: 57);
          }
          return _list([_inspectionJson('1')]);
        }),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));
      await tester.tap(find.byType(FloatingActionButton));
      await _pumpUntil(tester, find.text('Record Quality Inspection'));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('Showing the 2 most recent of 57 receipts'),
        findsOneWidget,
      );
    });

    testWidgets('with no receipts the form says why there is nothing to record',
        (tester) async {
      await loginThen(
        ['admin'],
        MockClient((req) async {
          if (req.url.path == '/api/goods-receipts') return _list([], total: 0);
          return _list([_inspectionJson('1')]);
        }),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));
      await tester.tap(find.byType(FloatingActionButton));
      await _pumpUntil(tester, find.text('Record Quality Inspection'));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('No goods receipts yet'),
        findsOneWidget,
      );
      final submit = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Record inspection'),
      );
      expect(submit.onPressed, isNull);
    });

    testWidgets('a failed receipt load explains itself instead of opening',
        (tester) async {
      await loginThen(
        ['admin'],
        MockClient((req) async {
          if (req.url.path == '/api/goods-receipts') {
            return http.Response('boom', 500);
          }
          return _list([_inspectionJson('1')]);
        }),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));
      await tester.tap(find.byType(FloatingActionButton));
      await _pumpUntil(
        tester,
        find.textContaining('Could not load goods receipts'),
      );

      expect(find.text('Record Quality Inspection'), findsNothing);
      expect(
        find.textContaining('Could not load goods receipts'),
        findsOneWidget,
      );
    });

    testWidgets('tapping a row opens the detail screen', (tester) async {
      await loginThen(
        ['ap_clerk'],
        MockClient((req) async {
          if (req.url.path == '/api/inspections/1') {
            return _json(_inspectionJson('1', number: 'QI-A'));
          }
          return _list([_inspectionJson('1', number: 'QI-A')]);
        }),
      );

      await tester.pumpWidget(_localized(const InspectionsScreen()));
      await _pumpUntil(tester, find.byType(InspectionListTile));

      await tester.tap(find.byType(InspectionListTile));
      await _pumpUntil(tester, find.byType(InspectionDetailScreen));

      expect(find.byType(InspectionDetailScreen), findsOneWidget);
    });
  });

  group('InspectionDetailScreen', () {
    testWidgets('renders the fields and what the outcome does to the match',
        (tester) async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _json(
            _inspectionJson(
              'qi1',
              number: 'QI-FAIL',
              result: 'fail',
              notes: 'Two cartons crushed',
            ),
          ),
        ),
      );

      await tester.pumpWidget(
        _localized(const InspectionDetailScreen(inspectionId: 'qi1')),
      );
      await _pumpUntil(tester, find.text('QI-FAIL'));

      expect(find.text('QI-FAIL'), findsOneWidget);
      expect(find.text('GR-1'), findsOneWidget);
      expect(find.text('Dana'), findsOneWidget);
      expect(find.text('Two cartons crushed'), findsOneWidget);
      // The consequence is the reason the row exists, and it isn't guessable
      // from the word "Fail".
      expect(
        find.textContaining('a quality hold blocks payment'),
        findsOneWidget,
      );
    });

    testWidgets('flags a row that no match will ever read', (tester) async {
      ApiClient().debugConfigure(
        client: MockClient(
          (req) async => _json(
            _inspectionJson(
              'qi2',
              number: 'QI-ORPHAN',
              grId: null,
              poId: null,
              grNumber: null,
            ),
          ),
        ),
      );

      await tester.pumpWidget(
        _localized(const InspectionDetailScreen(inspectionId: 'qi2')),
      );
      await _pumpUntil(tester, find.text('QI-ORPHAN'));

      expect(find.text('Not linked'), findsOneWidget);
      expect(
        find.textContaining('PO matching will never read it'),
        findsOneWidget,
      );
    });

    testWidgets('a 404 renders the not-found state with Retry', (tester) async {
      ApiClient().debugConfigure(
        client: MockClient((req) async => _json({'detail': 'Not found'}, 404)),
      );

      await tester.pumpWidget(
        _localized(const InspectionDetailScreen(inspectionId: 'nope')),
      );
      await _pumpUntil(tester, find.textContaining('Not found'));

      expect(find.textContaining('Not found'), findsOneWidget);
      expect(find.widgetWithText(FilledButton, 'Retry'), findsOneWidget);
    });
  });
}
