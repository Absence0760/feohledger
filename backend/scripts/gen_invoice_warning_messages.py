"""Generate the web AND mobile invoice-warning code → message catalogues.

`decisions.md` §155 shipped a localized FRAME around server-English warning
findings and named the durable fix: a parameterized warning-code → message-key
catalogue of the kind `gen_einvoice_rule_messages.py` already builds from the
e-invoice rule set. This is that generator;
`app/services/invoice_warning_catalog.py` is the enumeration it reads.

Two modes, one behaviour:

    python scripts/gen_invoice_warning_messages.py            # write both files
    python scripts/gen_invoice_warning_messages.py --check    # fail if either is stale

It writes two files from the one catalogue:

* **Web** — `frontend/src/lib/api/invoiceWarningMessages.generated.ts`, a
  code → message-key map plus a code → parameter-kind map. The client applies
  the kinds generically, so the map is all it needs.
* **Mobile** — `mobile/lib/l10n/invoice_warning_messages.generated.dart`, the
  same parameter-kind map plus the code → `AppLocalizations` dispatch. Dart has
  no string-keyed lookup into a gen-l10n class — each message is its own
  typed method — so where the web needs a key, mobile needs a `switch` arm per
  code that calls the right method with its arguments in the right order.
  That arm is exactly what round 26 transcribed by hand for 48 codes.

The mobile arm's argument ORDER and each argument's TYPE are read from the
English ARB (`mobile/lib/l10n/app_en.arb`), because that is what
`flutter gen-l10n` builds the method signature from: every placeholder of a
warning message is a `String` except a plural selector, which is an `int`, and
two `String` arguments passed in the wrong order would compile and render a
PO number where the amount belongs. Reading the order off the ARB — rather than
assuming the catalogue's own order matches it — is what makes that swap
impossible rather than merely unlikely. A code the ARB does not state yet falls
back to the catalogue's order, and the call then fails to compile until the
English ARB entry exists.

``--check`` is the drift guard, and it holds the same way the e-invoice one
does: add a `WarningSpec` and both outputs change, so CI goes red until they
are regenerated. The chain then continues per surface — on the web the new key
does not exist in `en.ts` and `pnpm check` goes red (the generated map is
`satisfies Record<string, MessageKey>`); on mobile the generated arm calls an
`AppLocalizations` method `app_en.arb` does not declare and `flutter analyze`
goes red. Once English exists, each surface's locale-parity test demands the
other translations. Three guards per surface, each catching the step after the
one before it.

The generated modules carry the parameter kinds because a label per code is not
enough: `po_mismatch` alone is five sentences, and each embeds a PO number, a
money figure or a variance. The kinds say what each placeholder IS, so the
client formats `5000.00` as money in the reader's locale instead of rendering
en-US digits inside a translated sentence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below —
# same two lines, and the same reason, as `scripts/migrate_all_tenants.py`:
# invoked as `python scripts/gen_invoice_warning_messages.py`, sys.path[0] is
# `scripts/`, so a worktree reusing the primary checkout's `.venv` would fall
# through to the editable install's finder and enumerate the OTHER checkout's
# catalogue. See `frontend/tests-e2e/README.md` § Running from a worktree.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.invoice_warning_catalog import WARNING_SPECS, WarningSpec  # noqa: E402

#: Where the generated modules land. Relative to the repo root so a worktree
#: writes into its own frontend and mobile trees, never the primary checkout's.
OUTPUT_PATH = Path("frontend/src/lib/api/invoiceWarningMessages.generated.ts")
MOBILE_OUTPUT_PATH = Path("mobile/lib/l10n/invoice_warning_messages.generated.dart")

#: The English ARB — the one whose placeholder metadata `flutter gen-l10n`
#: builds every `AppLocalizations` method signature from.
MOBILE_ARB_PATH = Path("mobile/lib/l10n/app_en.arb")

#: Namespace the invoice list + modal copy already lives under.
_KEY_PREFIX = "invoices.warning."


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def message_key(code: str) -> str:
    """`round_amount` → `invoices.warning.roundAmount`.

    Purely mechanical: the code IS the identity, so deriving the key from it
    means a renamed code cannot keep pointing at the old wording.
    """
    head, *rest = code.split("_")
    return _KEY_PREFIX + head + "".join(part.capitalize() for part in rest)


def render() -> str:
    lines: list[str] = [
        "// GENERATED FILE — do not edit by hand.",
        "//",
        "// Source of truth: backend/app/services/invoice_warning_catalog.py",
        "// Regenerate:      pnpm gen:warning-messages",
        "// Drift check:     pnpm check:warning-messages  (runs in CI)",
        "//",
        "// Maps every `InvoiceWarning.code` the backend can emit to the message",
        "// key that states it in the reader's language, plus what each of that",
        "// message's parameters IS so the client can format it for the locale.",
        "// decisions.md §155 shipped a localized frame around server-English",
        "// findings and named this as the fix: a label per `type` could not work,",
        "// because one type is up to five different sentences.",
        "import type { MessageKey } from '$lib/i18n/messages';",
        "",
        "/** What a warning parameter holds, and therefore how it renders. */",
        "export type WarningParamKind =",
        "\t| 'text'",
        "\t| 'currency'",
        "\t| 'money'",
        "\t| 'number'",
        "\t| 'percent'",
        "\t| 'count'",
        "\t| 'date';",
        "",
        "/** Warning code → the localized sentence that states it. */",
        "export const INVOICE_WARNING_MESSAGE_KEYS = {",
    ]

    # One reference comment per code carrying the backend's own English
    # template, so a reworded backend sentence surfaces here as a diff — the
    # signal that the six translations of that key are now stale.
    for spec in WARNING_SPECS:
        lines.append(f"\t// {spec.template}")
        lines.append(f"\t'{spec.code}': '{message_key(spec.code)}',")

    lines += [
        "} as const satisfies Record<string, MessageKey>;",
        "",
        "/**",
        " * Warning code → each of its parameters' kind.",
        " *",
        " * The English FALLBACK (`InvoiceWarning.message`, rendered by the backend)",
        " * spells the currency code and the `%` out, because nothing downstream will",
        " * format them. The localized value leaves both to the client formatters, so",
        " * the two English strings are deliberately not identical.",
        " */",
        "export const INVOICE_WARNING_PARAM_KINDS = {",
    ]
    for spec in WARNING_SPECS:
        if not spec.params:
            lines.append(f"\t'{spec.code}': {{}},")
            continue
        entries = ", ".join(f"{name}: '{kind}'" for name, kind in spec.params.items())
        lines.append(f"\t'{spec.code}': {{ {entries} }},")
    lines += [
        "} as const satisfies Record<",
        "\tkeyof typeof INVOICE_WARNING_MESSAGE_KEYS,",
        "\tRecord<string, WarningParamKind>",
        ">;",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Mobile (Dart)
# --------------------------------------------------------------------------- #

#: The `_localizeWarningCode` parameters. A placeholder with one of these names
#: would shadow the parameter it is read from, so the generator refuses it.
_DART_RESERVED = frozenset({"l", "warningCode", "p", "currency"})

#: Parameter kind → the formatter in `invoice_warning_messages.dart` that turns
#: the raw wire string into a `String` argument. A `count` reaching a `String`
#: slot rather than a plural selector is an ordinal (a line number), and renders
#: as the server's own digits.
_DART_STRING_FORMATTER = {
    "text": "_text",
    "money": "_money",
    "percent": "_percent",
    "number": "_number",
    "date": "_date",
    "count": "_text",
}


class ArbMismatch(Exception):
    """The English ARB states a warning message differently from the catalogue."""


def arb_method(code: str) -> str:
    """`round_amount` → `invoiceWarningRoundAmount`, the gen-l10n member name.

    The same mechanical derivation as :func:`message_key`, spelled the way an
    ARB key has to be (no dots), so the web key `invoices.warning.roundAmount`
    and this name can never name two different sentences.
    """
    camel = message_key(code).removeprefix(_KEY_PREFIX)
    return "invoiceWarning" + camel[0].upper() + camel[1:]


def _load_arb() -> dict:
    return json.loads((_repo_root() / MOBILE_ARB_PATH).read_text(encoding="utf-8"))


def _signature(spec: WarningSpec, arb: dict) -> list[tuple[str, str]]:
    """The generated call's `(placeholder, dart type)` list, in argument order.

    `currency` parameters never reach the sentence — they are how the client
    formats the money beside them — so they are not placeholders.
    """
    expected = [name for name, kind in spec.params.items() if kind != "currency"]
    for name in expected:
        if name in _DART_RESERVED:
            raise ArbMismatch(
                f"{spec.code}: placeholder `{name}` collides with a parameter of the "
                "generated dispatch; rename it in invoice_warning_catalog.py"
            )

    method = arb_method(spec.code)
    if method not in arb:
        # Not stated in English yet. Emit the call in catalogue order so the
        # file is still complete; it cannot compile until `app_en.arb` declares
        # the message, and regenerating then re-reads the order from there.
        return [(name, "int" if spec.params[name] == "count" else "String") for name in expected]

    placeholders = (arb.get(f"@{method}") or {}).get("placeholders") or {}
    if set(placeholders) != set(expected):
        raise ArbMismatch(
            f"{MOBILE_ARB_PATH}: `{method}` declares placeholders {sorted(placeholders)} "
            f"but `{spec.code}` has {sorted(expected)} (currency parameters excluded) — "
            "the ARB and the catalogue must describe the same sentence"
        )
    signature: list[tuple[str, str]] = []
    for name, meta in placeholders.items():
        dart_type = (meta or {}).get("type", "String")
        if dart_type not in ("int", "String"):
            raise ArbMismatch(
                f"{MOBILE_ARB_PATH}: `{method}.{name}` has type {dart_type!r}; a warning "
                "placeholder is a String, or an int plural selector"
            )
        if dart_type == "int" and spec.params[name] != "count":
            raise ArbMismatch(
                f"{MOBILE_ARB_PATH}: `{method}.{name}` is an int, but only a `count` "
                f"parameter can be one (`{spec.code}.{name}` is `{spec.params[name]}`)"
            )
        signature.append((name, dart_type))
    return signature


def _dart_call(method: str, args: list[str], indent: str) -> list[str]:
    """`return l.method(a, b);` on one line when it fits `dart format`'s 80
    columns, otherwise one argument per line with the trailing comma the
    `require_trailing_commas` lint asks for."""
    one_line = f"{indent}return l.{method}({', '.join(args)});"
    if len(one_line) <= 80:
        return [one_line]
    return [
        f"{indent}return l.{method}(",
        *(f"{indent}  {a}," for a in args),
        f"{indent});",
    ]


def render_dart(arb: dict | None = None) -> str:
    """The mobile catalogue: the parameter kinds plus the per-code dispatch."""
    arb = _load_arb() if arb is None else arb
    lines: list[str] = [
        "// GENERATED FILE — do not edit by hand.",
        "//",
        "// Source of truth: backend/app/services/invoice_warning_catalog.py",
        "// Signatures:      mobile/lib/l10n/app_en.arb (argument order and type)",
        "// Regenerate:      pnpm gen:warning-messages",
        "// Drift check:     pnpm check:warning-messages  (runs in CI)",
        "//",
        "// The mobile twin of frontend/src/lib/api/invoiceWarningMessages.generated.ts",
        "// — the same codes and parameter kinds, generated from the same catalogue,",
        "// plus the code → AppLocalizations dispatch Dart needs where the web can",
        "// look a message key up by name. The formatters each arm calls (`_money`,",
        "// `_percent`, …) and the fallback rules live in the library this is a part",
        "// of, `invoice_warning_messages.dart`.",
        "part of 'invoice_warning_messages.dart';",
        "",
        "/// What each warning parameter holds, and therefore how it renders.",
        "///",
        "/// The localized message says `{amount}` where the English fallback says",
        "/// `{amount} {currency}`, precisely so the client can format the figure for",
        "/// the reader instead of embedding en-US digits in a translated sentence.",
        "const Map<String, Map<String, String>> invoiceWarningParamKinds = {",
    ]
    for spec in WARNING_SPECS:
        entries = [f"'{name}': '{kind}'" for name, kind in spec.params.items()]
        one_line = f"  '{spec.code}': {{{', '.join(entries)}}},"
        if len(one_line) <= 80:
            lines.append(one_line)
        else:
            lines.append(f"  '{spec.code}': {{")
            lines.extend(f"    {e}," for e in entries)
            lines.append("  },")
    lines += [
        "};",
        "",
        "/// The localized sentence for [warningCode], or `null` when this build does",
        "/// not know the code or a parameter the sentence needs is missing — see",
        "/// `localizeInvoiceWarning`, the only caller.",
        "String? _localizeWarningCode(",
        "  AppLocalizations l,",
        "  String? warningCode,",
        "  Map<String, String> p,",
        "  String? currency,",
        ") {",
        "  switch (warningCode) {",
    ]
    for spec in WARNING_SPECS:
        method = arb_method(spec.code)
        signature = _signature(spec, arb)
        # The backend's own English, so a reworded sentence surfaces here as a
        # diff — the signal that the ARB translations of it are now stale.
        lines.append(f"    // {spec.template}")
        lines.append(f"    case '{spec.code}':")
        if not signature:
            lines.append(f"      return l.{method};")
            continue
        for name, dart_type in signature:
            if dart_type == "int":
                expr = f"_count(p['{name}'])"
            else:
                formatter = _DART_STRING_FORMATTER[spec.params[name]]
                expr = f"{formatter}(p['{name}'], currency)"
            lines.append(f"      final {name} = {expr};")
            lines.append(f"      if ({name} == null) return null;")
        lines.extend(_dart_call(method, [n for n, _ in signature], "      "))
    lines += [
        "  }",
        "  return null;",
        "}",
        "",
    ]
    return "\n".join(lines)


#: What to do after regenerating each file, when `--check` finds it stale.
_REMEDY = {
    OUTPUT_PATH: "add any new message key to frontend/src/lib/i18n/locales/*.ts (all six)",
    MOBILE_OUTPUT_PATH: "add any new message to mobile/lib/l10n/app_*.arb (every locale), "
    "then `flutter gen-l10n`",
}


def _committed(path: Path) -> str | None:
    target = _repo_root() / path
    return target.read_text(encoding="utf-8") if target.exists() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if either committed file differs from what would be generated",
    )
    args = parser.parse_args(argv)

    try:
        outputs = {OUTPUT_PATH: render(), MOBILE_OUTPUT_PATH: render_dart()}
    except ArbMismatch as exc:
        print(f"cannot generate the mobile catalogue: {exc}", file=sys.stderr)
        return 1

    if args.check:
        stale = [path for path, rendered in outputs.items() if _committed(path) != rendered]
        for path in outputs:
            if path not in stale:
                print(f"{path}: in sync with the warning catalogue")
        for path in stale:
            print(
                f"{path} is STALE — `invoice_warning_catalog.py` declares a different set of "
                "warning codes (or different wording), or the English ARB a different "
                "placeholder order, than the committed file.\n"
                f"Run `pnpm gen:warning-messages`, then {_REMEDY[path]}.",
                file=sys.stderr,
            )
        return 1 if stale else 0

    for path, rendered in outputs.items():
        target = _repo_root() / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
