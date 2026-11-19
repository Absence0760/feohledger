// Source-scan guard: a screen states a failed load through the shared
// `LoadErrorState` and a localized sentence, never a hand-built
// icon-and-text column or an exception's `toString()`.
//
// Two shapes are banned:
//
// - `Icons.error_outline, size: …` outside `lib/widgets/load_error_state.dart`
//   — the signature of a hand-built full-screen error state. Each copy drifted
//   on its own (icon size, red shade, whether Retry was a `FilledButton` or a
//   `FilledButton.icon`, whether the text could scroll at 200% scale).
// - A caught error rendered raw in `lib/screens/`: `e.toString()`, `'$e'` /
//   `'${e}'` on a `catch` variable, or `error.toString()` on a held error.
//   Those print `ApiException(403): …` or `SocketException: …` — a log line
//   that names neither what happened nor what to do next. Word it through
//   `describeLoadError` (`lib/l10n/load_error_messages.dart`) or
//   `describeApiError` instead.
//
// [_allowlist] names the screens other work still owns, per rule. It fails
// when an entry no longer matches, so a fixed screen leaves the list in the
// same change rather than keeping an exemption it no longer needs.
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

const _iconRule = 'hand-built error icon';
const _rawErrorRule = 'raw caught error';

/// `path` → the rules it is still exempt from.
const Map<String, Set<String>> _allowlist = {
  'lib/screens/approvals_screen.dart': {_iconRule},
  'lib/screens/capture_screen.dart': {_rawErrorRule},
  'lib/screens/cash_flow_screen.dart': {_rawErrorRule},
  'lib/screens/invoices_screen.dart': {_rawErrorRule},
};

final _errorIcon = RegExp(r'Icons\.error_outline\s*,\s*size\s*:');
final _catchVar = RegExp(r'catch\s*\(\s*(\w+)');
final _heldErrorToString = RegExp(r'\berror\??\.toString\(\)');

/// The rules [source] (at [path]) breaks.
Set<String> _violations(String path, String source) {
  final found = <String>{};
  if (path != 'lib/widgets/load_error_state.dart' &&
      _errorIcon.hasMatch(source)) {
    found.add(_iconRule);
  }
  if (path.startsWith('lib/screens/')) {
    final names = _catchVar
        .allMatches(source)
        .map((m) => m.group(1)!)
        .where((n) => n != '_')
        .toSet();
    final rawCaught = names.any((n) {
      final name = RegExp.escape(n);
      return RegExp('\\b$name\\.toString\\(\\)').hasMatch(source) ||
          RegExp('\\\$$name\\b').hasMatch(source) ||
          RegExp('\\\$\\{$name\\}').hasMatch(source);
    });
    if (rawCaught || _heldErrorToString.hasMatch(source)) {
      found.add(_rawErrorRule);
    }
  }
  return found;
}

void main() {
  test('the scan recognises each banned shape (not a vacuous guard)', () {
    const screen = 'lib/screens/x_screen.dart';
    expect(
      _violations(screen, 'Icon(Icons.error_outline,\n   size: 48)'),
      {_iconRule},
    );
    expect(
      _violations('lib/widgets/load_error_state.dart',
          'Icon(Icons.error_outline, size: 48)'),
      isEmpty,
    );
    expect(
      _violations(screen, 'try {} catch (e) { show(e.toString()); }'),
      {_rawErrorRule},
    );
    expect(
      _violations(screen, "try {} catch (err, st) { show('\$err'); }"),
      {_rawErrorRule},
    );
    expect(
      _violations(screen, "try {} catch (e) { show('\${e}'); }"),
      {_rawErrorRule},
    );
    expect(
      _violations(screen, 'Text(store.error.toString())'),
      {_rawErrorRule},
    );
    // The sanctioned wording, and an unrelated `.toString()`, pass.
    expect(
      _violations(
        screen,
        'try {} catch (e) { show(describeLoadError(l, e, l.x)); }\n'
        'final q = item.quantity.toString();',
      ),
      isEmpty,
    );
  });

  test('screens state a failed load through LoadErrorState, never raw text',
      () {
    final unexpected = <String>[];
    final stale = <String>[];
    final seen = <String>{};
    for (final entity in Directory('lib').listSync(recursive: true)) {
      if (entity is! File || !entity.path.endsWith('.dart')) continue;
      if (entity.path.contains('/l10n/gen/')) continue;
      final path = entity.path;
      seen.add(path);
      final found = _violations(path, entity.readAsStringSync());
      final allowed = _allowlist[path] ?? const <String>{};
      for (final rule in found.difference(allowed)) {
        unexpected.add('$path: $rule');
      }
      for (final rule in allowed.difference(found)) {
        stale.add('$path: $rule');
      }
    }
    for (final path in _allowlist.keys) {
      if (!seen.contains(path)) stale.add('$path: file no longer exists');
    }

    expect(
      unexpected,
      isEmpty,
      reason: 'use LoadErrorState (lib/widgets/load_error_state.dart) and '
          'describeLoadError / describeApiError, never an exception '
          'toString()',
    );
    expect(
      stale,
      isEmpty,
      reason: 'these allowlisted screens are clean now: remove them from '
          '_allowlist in this test',
    );
  });
}
