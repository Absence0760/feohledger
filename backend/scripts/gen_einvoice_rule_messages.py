"""Generate the frontend's e-invoice rule-code → message-key catalogue.

`decisions.md` §95 deleted the client's hand-written code→prose map when the 422
body became structured, and named the durable replacement: a map **generated
from the backend's own rule set**, so the catalogue cannot drift from the
validator. This is the generator; `app/services/e_invoice/rule_catalog.py` is
the enumeration it reads.

Two modes, one behaviour:

    python scripts/gen_einvoice_rule_messages.py            # write the file
    python scripts/gen_einvoice_rule_messages.py --check     # fail if stale

``--check`` is the drift guard, and it is what makes the whole arrangement
hold: add a rule to `en16931_rules.py` and this file's output changes, so CI
goes red until the catalogue is regenerated — at which point the new key does
not exist in `en.ts` and `pnpm check` goes red in turn (the generated map is
`satisfies Record<string, MessageKey>`), and once it does the locale-parity
test demands the other five translations. Three guards, each catching the step
after the one before it.

The generated map deliberately covers only the codes a client can IDENTIFY:
`error_payload` folds a rule id into `msg` but leaves a generic kind
(`missing` / `malformed` / …) unprefixed, and the app's shared `formatApiDetail`
keeps only `loc` + `msg`. The generic kinds are emitted as a separate exported
list rather than silently dropped, so a new one shows up as a diff here instead
of as an unmapped code nobody notices.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported below —
# same two lines, and the same reason, as `scripts/migrate_all_tenants.py`:
# invoked as `python scripts/gen_einvoice_rule_messages.py`, sys.path[0] is
# `scripts/`, so a worktree reusing the primary checkout's `.venv` would fall
# through to the editable install's finder and enumerate the OTHER checkout's
# rules. See `frontend/tests-e2e/README.md` § Running from a worktree.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.e_invoice.rule_catalog import (  # noqa: E402
    RuleCode,
    opaque_rule_codes,
    rule_codes,
)

#: Where the generated module lands. Relative to the repo root so a worktree
#: writes into its own frontend, never the primary checkout's.
OUTPUT_PATH = Path("frontend/src/lib/api/einvoiceRuleMessages.generated.ts")

#: Namespace the invoice modal's e-invoice copy already lives under.
_KEY_PREFIX = "invoices.modal.einvoice.rule."


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def message_key(rule: RuleCode) -> str:
    """The `MessageKey` a code renders through.

    The per-VAT-category families collapse onto ONE key each: `BR-Z-08` and
    `BR-S-08` are the same sentence about different categories, and the row
    already shows the rule id and the field path beside the wording, so
    spelling the category into the copy would add nothing and multiply the
    translation surface by nine.
    """
    if rule.family is not None:
        return f"{_KEY_PREFIX}vatCategory{rule.family}"
    parts = rule.code.split("-")
    return _KEY_PREFIX + parts[0].lower() + "".join(p.capitalize() for p in parts[1:])


def render() -> str:
    visible = [rc for rc in rule_codes() if rc.client_visible]
    opaque = opaque_rule_codes()

    # One reference comment per KEY, in first-appearance order, so a reworded
    # backend sentence surfaces here as a diff — the signal that the five
    # translations of that key are now stale.
    seen: dict[str, str] = {}
    for rc in visible:
        seen.setdefault(message_key(rc), rc.message)

    lines: list[str] = [
        "// GENERATED FILE — do not edit by hand.",
        "//",
        "// Source of truth: backend/app/services/e_invoice/rule_catalog.py",
        "// Regenerate:      pnpm gen:einvoice-messages",
        "// Drift check:     pnpm check:einvoice-messages  (runs in CI)",
        "//",
        "// Maps every EN 16931 / PEPPOL rule id the backend's validators can",
        "// emit to the message key that states it in the reader's language.",
        "// decisions.md §95 removed the hand-written version because it covered",
        "// four codes out of dozens and drifted; this one is derived, so it",
        "// cannot.",
        "import type { MessageKey } from '#lib/i18n/messages.ts';",
        "",
        "/** Rule id → the localized sentence that explains it. */",
        "export const E_INVOICE_RULE_MESSAGE_KEYS = {",
    ]

    emitted_keys: set[str] = set()
    for rc in visible:
        key = message_key(rc)
        if key not in emitted_keys:
            emitted_keys.add(key)
            lines.append(f"\t// {seen[key]}")
        lines.append(f"\t'{rc.code}': '{key}',")

    lines += [
        "} as const satisfies Record<string, MessageKey>;",
        "",
        "/**",
        " * The codes the 422 body does NOT fold into `msg`, so a client that",
        " * flattens the payload cannot identify them and no map can name them.",
        " * Their own sentence already spells the problem out in words. Listed",
        " * rather than dropped so a new generic kind shows up as a diff here.",
        " */",
        "export const E_INVOICE_OPAQUE_CODES = [",
    ]
    lines += [f"\t'{rc.code}'," for rc in opaque]
    lines += ["] as const;", ""]
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
            print(f"{OUTPUT_PATH}: in sync with the validators")
            return 0
        print(
            f"{OUTPUT_PATH} is STALE — the e-invoice validators emit a different set of "
            "rule codes (or different wording) than the committed catalogue.\n"
            "Run `pnpm gen:einvoice-messages`, then add the new message key(s) to "
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
