import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/l10n/gen/app_localizations_de.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_en.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations_ja.dart';
import 'package:feohledger_mobile/l10n/gl_chart_refusal_messages.dart';

// The GL-chart refusal arrives STRUCTURED (`backend/app/services/gl_chart.py`
// `ChartRefusal.body`): a stable code, the refused codes by reason, and the
// English `message`. This is the mobile half that states it in the reader's
// language — and returns null for anything it does not recognise, so the
// caller falls back to the server's sentence. Mirrors the web
// `frontend/src/lib/api/glChartRefusal.test.ts`.
void main() {
  final en = AppLocalizationsEn();
  final de = AppLocalizationsDe();
  final ja = AppLocalizationsJa();

  Map<String, dynamic> body({
    List<Object?> foreign = const [],
    List<Object?> retired = const [],
    List<Object?> unknown = const [],
    bool onLines = false,
  }) => {
    'code': glCodesOutsideChart,
    'on_lines': onLines,
    'foreign': foreign,
    'retired': retired,
    'unknown': unknown,
    'message': 'server english',
  };

  test('names each code under its reason and asks for a code', () {
    expect(
      localizeGlChartRefusal(en, body(foreign: ['6000'])),
      "GL account '6000' belongs to another entity's chart of accounts, not "
      "this invoice's. Choose a code from the invoice's own chart — the shared "
      "accounts plus its entity's own.",
    );
  });

  test(
    'pluralizes and asks for an ACTIVE code when one was retired/unknown',
    () {
      expect(
        localizeGlChartRefusal(
          en,
          body(retired: ['6800', '6900'], unknown: ['9999']),
        ),
        "GL accounts '6800', '6900' are retired in this invoice's chart. "
        "GL account '9999' is not in this invoice's chart of accounts. "
        "Choose an active code from the invoice's own chart — the shared "
        "accounts plus its entity's own.",
      );
    },
  );

  test('marks line-item codes', () {
    expect(
      localizeGlChartRefusal(en, body(unknown: ['9998'], onLines: true)),
      startsWith('Line items: '),
    );
  });

  test("states it in the reader's language, not the server's English", () {
    final out = localizeGlChartRefusal(de, body(unknown: ['9999']))!;
    expect(out, contains("Das Sachkonto '9999' ist nicht im Kontenplan"));
    expect(out, isNot(contains('server english')));
    // Japanese has no grammatical plural: the `other` arm serves every count.
    expect(
      localizeGlChartRefusal(ja, body(retired: ['6800', '6900'])),
      contains("勘定科目 '6800', '6900' はこの請求書の勘定科目表で廃止されています。"),
    );
  });

  group('anything else falls back to the server sentence (null)', () {
    final cases = <String, Object?>{
      'a string detail': 'GL account refused',
      'a validation list': [
        {
          'loc': ['body'],
          'msg': 'bad',
        },
      ],
      'another code': {
        ...body(foreign: ['6000']),
        'code': 'something_else',
      },
      'a missing bucket': {
        'code': glCodesOutsideChart,
        'foreign': ['6000'],
        'message': 'x',
      },
      'a non-string code': body(retired: [6800]),
      'every bucket empty': body(),
      'null': null,
    };
    cases.forEach((label, detail) {
      test(label, () => expect(localizeGlChartRefusal(en, detail), isNull));
    });
  });
}
