/// Stating the GL-chart refusal in the reader's language — the mobile half of
/// `backend/app/services/gl_chart.py::ChartRefusal.body`, and the mirror of
/// the web `frontend/src/lib/api/glChartRefusal.ts`.
///
/// Every invoice GL write refuses a code that is not an active account of the
/// invoice's own chart (`docs/decisions.md` §194/§199). The 422 `detail` is
/// structured — a stable `code`, the refused codes by reason (`foreign` /
/// `retired` / `unknown`), `on_lines`, and the English `message` — so a
/// non-English reader is told which code and why in their own language. On
/// mobile it reaches the reader through the invoice edit sheet's GL field,
/// which is free text.
///
/// **Anything this build does not recognise → `null`**, and the caller renders
/// the server's English sentence ([ApiException.message]): another code, a
/// bucket that is missing or not a list of strings, every bucket empty.
library;

import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';

/// The backend's `gl_chart.GL_CODES_OUTSIDE_CHART`.
const glCodesOutsideChart = 'gl_codes_outside_chart';

List<String>? _codes(Object? v) {
  if (v is! List) return null;
  if (!v.every((c) => c is String)) return null;
  return v.cast<String>();
}

/// The codes, each quoted as the server quotes them (`'6800'`). Joined with a
/// plain comma: they are identifiers the reader has to find in the chart, not
/// prose, so a locale's list conjunction would add nothing.
String _codeList(List<String> codes) => codes.map((c) => "'$c'").join(', ');

/// The localized refusal for [detail] (a decoded 422 `detail`), or `null` when
/// [detail] is not a GL-chart refusal this build can state.
String? localizeGlChartRefusal(AppLocalizations l, Object? detail) {
  if (detail is! Map) return null;
  if (detail['code'] != glCodesOutsideChart) return null;
  final foreign = _codes(detail['foreign']);
  final retired = _codes(detail['retired']);
  final unknown = _codes(detail['unknown']);
  if (foreign == null || retired == null || unknown == null) return null;
  if (foreign.isEmpty && retired.isEmpty && unknown.isEmpty) return null;

  final parts = <String>[
    if (foreign.isNotEmpty)
      l.glChartRefusalForeign(foreign.length, _codeList(foreign)),
    if (retired.isNotEmpty)
      l.glChartRefusalRetired(retired.length, _codeList(retired)),
    if (unknown.isNotEmpty)
      l.glChartRefusalUnknown(unknown.length, _codeList(unknown)),
    // Only a non-empty active chart refuses a retired or unknown code, so "an
    // active code" is always something the reader can pick — the backend's
    // own rule for the same sentence.
    if (retired.isNotEmpty || unknown.isNotEmpty)
      l.glChartRefusalPickActive
    else
      l.glChartRefusalPick,
  ];
  final sentence = parts.join(' ');
  return detail['on_lines'] == true
      ? l.glChartRefusalOnLines(sentence)
      : sentence;
}
