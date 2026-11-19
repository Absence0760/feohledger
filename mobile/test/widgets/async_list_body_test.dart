import 'package:material_ui/material_ui.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/widgets/async_list_body.dart';
import 'package:feohledger_mobile/widgets/load_error_state.dart';

const _savedCopy =
    "Showing a saved copy — couldn't reach the server. Pull down to try again.";
const _refreshFailed =
    "Couldn't refresh — showing the last list loaded. Pull down to try again.";

Widget _host({
  List<String> items = const [],
  bool loading = false,
  bool hasError = false,
  bool fromCache = false,
  Future<void> Function()? onRefresh,
}) => MaterialApp(
  localizationsDelegates: appLocalizationsDelegates,
  supportedLocales: AppLocalizations.supportedLocales,
  home: Scaffold(
    body: AsyncListBody<String>(
      items: items,
      loading: loading,
      hasError: hasError,
      fromCache: fromCache,
      errorMessage: 'Load failed sentence',
      onRefresh: onRefresh ?? () async {},
      empty: const Text('Nothing here'),
      itemBuilder: (context, item) => ListTile(title: Text(item)),
    ),
  ),
);

void main() {
  testWidgets('loading with no rows shows only a spinner', (tester) async {
    await tester.pumpWidget(_host(loading: true));

    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    expect(find.text('Nothing here'), findsNothing);
    expect(find.byType(RefreshIndicator), findsNothing);
  });

  testWidgets('a failed first load shows the error and Retry, not the empty '
      'copy', (tester) async {
    var retries = 0;
    await tester.pumpWidget(
      _host(hasError: true, onRefresh: () async => retries++),
    );

    expect(find.byType(LoadErrorState), findsOneWidget);
    expect(find.text('Load failed sentence'), findsOneWidget);
    expect(find.text('Nothing here'), findsNothing);

    await tester.tap(find.widgetWithText(FilledButton, 'Retry'));
    await tester.pump();
    expect(retries, 1);
  });

  testWidgets('a successful load with no rows shows the empty widget, with '
      'no banner', (tester) async {
    await tester.pumpWidget(_host());

    expect(find.text('Nothing here'), findsOneWidget);
    expect(find.byType(StaleDataBanner), findsNothing);
    expect(find.byType(LoadErrorState), findsNothing);
  });

  testWidgets('the empty state can be pulled to refresh', (tester) async {
    var refreshes = 0;
    await tester.pumpWidget(_host(onRefresh: () async => refreshes++));

    await tester.fling(find.text('Nothing here'), const Offset(0, 300), 1000);
    await tester.pumpAndSettle();

    expect(refreshes, 1);
  });

  testWidgets('rows render one per item, live, with no banner', (tester) async {
    await tester.pumpWidget(_host(items: ['a', 'b', 'c']));

    expect(find.byType(ListTile), findsNWidgets(3));
    expect(find.byType(Divider), findsNWidgets(2));
    expect(find.byType(StaleDataBanner), findsNothing);
  });

  testWidgets('rows can be pulled to refresh', (tester) async {
    var refreshes = 0;
    await tester.pumpWidget(
      _host(items: ['a'], onRefresh: () async => refreshes++),
    );

    await tester.fling(find.text('a'), const Offset(0, 300), 1000);
    await tester.pumpAndSettle();

    expect(refreshes, 1);
  });

  testWidgets('cached rows carry the saved-copy banner', (tester) async {
    await tester.pumpWidget(_host(items: ['a'], fromCache: true));

    expect(find.byType(StaleDataBanner), findsOneWidget);
    expect(find.text(_savedCopy), findsOneWidget);
    expect(find.text('a'), findsOneWidget);
  });

  testWidgets('a cached empty list carries the banner over the empty copy',
      (tester) async {
    await tester.pumpWidget(_host(fromCache: true));

    expect(find.text(_savedCopy), findsOneWidget);
    expect(find.text('Nothing here'), findsOneWidget);
  });

  testWidgets('a failed refresh keeps the rows and says so', (tester) async {
    await tester.pumpWidget(_host(items: ['a'], hasError: true));

    expect(find.text('a'), findsOneWidget);
    expect(find.text(_refreshFailed), findsOneWidget);
    expect(find.byType(LoadErrorState), findsNothing);
  });

  testWidgets('a refresh in flight keeps the rows on screen', (tester) async {
    await tester.pumpWidget(_host(items: ['a'], loading: true));

    expect(find.text('a'), findsOneWidget);
    expect(find.byType(CircularProgressIndicator), findsNothing);
  });

  testWidgets('the banner is a live region labelled with its message',
      (tester) async {
    final handle = tester.ensureSemantics();
    await tester.pumpWidget(_host(items: ['a'], fromCache: true));

    expect(
      tester.getSemantics(find.byType(StaleDataBanner)),
      containsSemantics(label: _savedCopy, isLiveRegion: true),
    );
    handle.dispose();
  });
}
