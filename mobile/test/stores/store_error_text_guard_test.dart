// Source-scan guard: no store under lib/stores turns a caught error into text
// with `toString()` or string interpolation. Either produces
// `ApiException(403): …` / `ClientException: …`, which screens then show
// inside a localized sentence. Stores describe errors with
// `describeApiError` (lib/api/api_client.dart), which yields the server's
// own sentence or a localized description; the behaviour is pinned in
// `store_error_text_test.dart`.

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// The names a `catch` clause in lib/stores binds its error to.
final _caught = RegExp(r'catch\s*\(\s*(\w+)');

void main() {
  test('no store stringifies a caught error', () {
    final offenders = <String>[];
    final files = Directory('lib/stores')
        .listSync()
        .whereType<File>()
        .where((f) => f.path.endsWith('.dart'));
    for (final file in files) {
      final source = file.readAsStringSync();
      final names = {
        for (final m in _caught.allMatches(source)) m.group(1)!,
      }..remove('_');
      final lines = source.split('\n');
      for (var i = 0; i < lines.length; i++) {
        final line = lines[i];
        if (line.trimLeft().startsWith('//')) continue;
        for (final name in names) {
          final stringified = RegExp(
            '\\b$name\\.toString\\(\\)'
            '|\\\$$name\\b'
            '|\\\$\\{$name\\b',
          );
          if (stringified.hasMatch(line)) {
            offenders.add('${file.path}:${i + 1}: ${line.trim()}');
          }
        }
      }
    }
    expect(
      offenders,
      isEmpty,
      reason: 'Describe the error with describeApiError(e) instead.',
    );
  });

  test('the guard sees the patterns it bans', () {
    const sample = '''
    } catch (e) {
      _error = e.toString();
      _error = 'Connection failed: \$e';
      _error = 'x \${e.runtimeType}';
    }''';
    final names = {
      for (final m in _caught.allMatches(sample)) m.group(1)!,
    };
    expect(names, {'e'});
    final hits = sample.split('\n').where(
          (l) => RegExp(r'\be\.toString\(\)|\$e\b|\$\{e\b').hasMatch(l),
        );
    expect(hits, hasLength(3));
  });
}
