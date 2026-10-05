/// Hardcoded-literal guard for a localized surface.
///
/// The ARB parity test proves every key has a translation; it cannot prove a
/// widget *reads* the key. A literal like `Text('Export')` compiles, passes
/// every English assertion, and ships English to a `de` / `ja` reader. The
/// test this supports renders the same surface twice — once under `en`, once
/// under `ja` — and treats any user-facing string that comes out identical in
/// both as a literal nobody localized. Japanese is the comparison locale on
/// purpose: it shares no ordinary vocabulary with English, so a cognate like
/// the German / Portuguese "Status" cannot pass for a literal and the only
/// legitimate overlap is fixture data and language-invariant tokens (file-
/// format names), which the caller lists explicitly.
///
/// "User-facing" is what a sighted or assistive-tech reader can receive: every
/// [Text]'s content, every [Tooltip] / [IconButton] tooltip, and every
/// [Semantics] label. Strings with no Latin letter (amounts, counts, icon
/// glyphs) can't be English and are ignored.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Every user-facing string currently in the tree, skipping the subtrees of
/// any widget whose type is in [skipSubtreesOf] — for content localized in a
/// separate turn (a list row's own badge, say) that the surface under test
/// merely hosts.
Set<String> renderedStrings(
  WidgetTester tester, {
  Set<Type> skipSubtreesOf = const {},
}) {
  final out = <String>{};
  void add(String? s) {
    if (s == null) return;
    final t = s.trim();
    if (t.isNotEmpty && RegExp('[A-Za-z]').hasMatch(t)) out.add(t);
  }

  void visit(Element element) {
    final widget = element.widget;
    if (skipSubtreesOf.contains(widget.runtimeType)) return;
    switch (widget) {
      case Text(:final data, :final textSpan):
        add(data ?? textSpan?.toPlainText());
      case Tooltip(:final message):
        add(message);
      case IconButton(:final tooltip):
        add(tooltip);
      case Semantics(:final properties):
        add(properties.label);
    }
    element.visitChildren(visit);
  }

  visit(tester.binding.rootElement!);
  return out;
}

/// Fails when a string rendered under both locales is not in [allowed], or
/// when a Japanese string carries an English word.
///
/// The second check catches the literal the first cannot: an English fragment
/// glued onto a localized one (`'${l.invoicesBulkDeleted(n)} ($m skipped)'`)
/// makes a string that differs between the runs as a whole. Japanese copy
/// writes no lowercase Latin words — the acronyms it borrows (ERP, CSV) are
/// capitals — so any word with two lowercase letters in a `ja` string is
/// English that bypassed the catalogue.
void expectNoUntranslatedStrings({
  required Set<String> english,
  required Set<String> other,
  Set<String> allowed = const {},
  String surface = 'surface',
}) {
  final leaked = english.intersection(other).difference(allowed).toList()
    ..sort();
  expect(
    leaked,
    isEmpty,
    reason: '$surface renders these identically in English and Japanese — '
        'a hardcoded literal, not an ARB key read through AppLocalizations: '
        '$leaked',
  );
  final englishWord = RegExp('[A-Za-z]*[a-z]{2,}[A-Za-z]*');
  final fragments = other
      .difference(allowed)
      .where((s) => englishWord.hasMatch(s))
      .toList()
    ..sort();
  expect(
    fragments,
    isEmpty,
    reason: '$surface renders English words inside these Japanese strings — '
        'a literal fragment composed around a localized one: $fragments',
  );
  // Guard the guard: an empty capture would pass vacuously.
  expect(english, isNotEmpty, reason: '$surface captured no English strings');
}
