"""Generate the frontend's invoice-warning code → message-key catalogue.

`decisions.md` §155 shipped a localized FRAME around server-English warning
findings and named the durable fix: a parameterized warning-code → message-key
catalogue of the kind `gen_einvoice_rule_messages.py` already builds from the
e-invoice rule set. This is that generator;
`app/services/invoice_warning_catalog.py` is the enumeration it reads.

Two modes, one behaviour:

    python scripts/gen_invoice_warning_messages.py            # write the file
    python scripts/gen_invoice_warning_messages.py --check    # fail if stale

``--check`` is the drift guard, and it holds the same way the e-invoice one
does: add a `WarningSpec` and this file's output changes, so CI goes red until
the catalogue is regenerated — at which point the new key does not exist in
`en.ts` and `pnpm check` goes red in turn (the generated map is
`satisfies Record<string, MessageKey>`), and once it does the locale-parity
test demands the other five translations. Three guards, each catching the step
after the one before it.

The generated module carries TWO maps, because a label per code is not enough:
`po_mismatch` alone is five sentences, and each embeds a PO number, a money
figure or a variance. The second map says what each placeholder IS, so the
client formats `5000.00` as money in the reader's locale instead of rendering
en-US digits inside a translated sentence.
"""

from __future__ import annotations

import argparse
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

from app.services.invoice_warning_catalog import WARNING_SPECS  # noqa: E402

#: Where the generated module lands. Relative to the repo root so a worktree
#: writes into its own frontend, never the primary checkout's.
OUTPUT_PATH = Path("frontend/src/lib/api/invoiceWarningMessages.generated.ts")

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed file differs from what would be generated",
    )
    args = parser.parse_args(argv)

    target = _repo_root() / OUTPUT_PATH
    rendered = render()

    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == rendered:
            print(f"{OUTPUT_PATH}: in sync with the warning catalogue")
            return 0
        print(
            f"{OUTPUT_PATH} is STALE — `invoice_warning_catalog.py` declares a different "
            "set of warning codes (or different wording) than the committed catalogue.\n"
            "Run `pnpm gen:warning-messages`, then add the new message key(s) to "
            "frontend/src/lib/i18n/locales/*.ts (all six locales).",
            file=sys.stderr,
        )
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
