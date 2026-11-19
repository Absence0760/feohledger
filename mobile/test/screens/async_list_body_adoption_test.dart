import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

// Adoption guard for `lib/widgets/async_list_body.dart`.
//
// A screen that hand-builds spinner → error → empty → RefreshIndicator has, in
// practice, got the order wrong: a failed load rendered as the empty state
// ("Queue clear" on a dropped connection), an empty state returned before the
// RefreshIndicator so it could not be pulled, and offline rows shown with no
// word that they were a saved copy. `AsyncListBody` owns that sequence, so a
// screen under lib/screens/ no longer builds its own `RefreshIndicator(`.
//
// The allowlist below is the screens not yet converted. It only shrinks: a new
// screen may not join it, and converting a listed screen fails this test until
// its entry is removed, so the list stays an honest to-do.

const _notYetConverted = <String>{
  'adaptive_screen.dart',
  'admin_users_screen.dart',
  'approvals_screen.dart',
  'cash_flow_screen.dart',
  'contract_detail_screen.dart',
  'dashboard_screen.dart',
  'exception_detail_screen.dart',
  'inspection_detail_screen.dart',
  'inspections_screen.dart',
  'invoice_detail_screen.dart',
  'invoices_screen.dart',
  'notifications_screen.dart',
  'payment_queue_screen.dart',
  'payments_screen.dart',
  'vendors_screen.dart',
  'workflow_detail_screen.dart',
  'workflows_screen.dart',
};

void main() {
  final screens = Directory('lib/screens')
      .listSync(recursive: true)
      .whereType<File>()
      .where((f) => f.path.endsWith('.dart'))
      .toList();

  String name(File f) => f.uri.pathSegments.last;
  bool usesRefreshIndicator(File f) =>
      f.readAsStringSync().contains('RefreshIndicator(');

  test('the screens directory is where this guard expects it', () {
    expect(screens, isNotEmpty);
  });

  test('no screen outside the allowlist builds its own RefreshIndicator', () {
    final offenders = screens
        .where((f) => !_notYetConverted.contains(name(f)))
        .where(usesRefreshIndicator)
        .map((f) => f.path)
        .toList();
    expect(
      offenders,
      isEmpty,
      reason:
          'Build list bodies with AsyncListBody (lib/widgets/async_list_body.dart) '
          'instead of a hand-rolled RefreshIndicator.',
    );
  });

  test('every allowlisted screen still exists and still needs its entry', () {
    final byName = {for (final f in screens) name(f): f};
    final stale = _notYetConverted.where((n) {
      final f = byName[n];
      return f == null || !usesRefreshIndicator(f);
    }).toList();
    expect(
      stale,
      isEmpty,
      reason:
          'These screens no longer use RefreshIndicator( (or are gone): '
          'remove them from _notYetConverted.',
    );
  });
}
