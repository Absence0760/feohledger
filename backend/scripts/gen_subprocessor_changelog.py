"""Generate the published sub-processor change log from the backend's data.

Issue #427 / `docs/decisions.md` §321. `app/services/subprocessor_changelog.py`
is the change log as data; the same entries drive the notices emailed to
customers (`services/subprocessor_notices.py`). This writes the table
`/legal/sub-processors` § 10 renders, so the row a customer reads and the notice
a customer receives come from one entry and cannot disagree.

    python scripts/gen_subprocessor_changelog.py            # write the module
    python scripts/gen_subprocessor_changelog.py --check     # fail if stale

``--check`` is the drift guard CI runs (Backend lint job). Generation refuses a
change log that breaks its own rules (`subprocessor_changelog.validate`) — a
notice entry with less than 30 days and no stated reason never reaches the page.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported — see
# `gen_pricing_catalog.py` for why.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.subprocessor_changelog import (  # noqa: E402
    CHANGELOG,
    NOTICE_PERIOD_DAYS,
    OBJECTION_CONTACT,
    ChangelogError,
    long_date,
    validate,
)

#: Relative to the repo root so a worktree writes into its own frontend.
OUTPUT_PATH = Path("frontend/src/lib/legal/subprocessorChangelog.generated.ts")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _ts(value: object) -> str:
    """A TypeScript literal in the frontend's style (single-quoted strings)."""
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if value is None:
        return "null"
    return json.dumps(value)


def render() -> str:
    problems = validate(CHANGELOG)
    if problems:
        raise ChangelogError("; ".join(problems))
    lines = [
        "// GENERATED FILE — do not edit by hand.",
        "//",
        "// Source of truth: backend/app/services/subprocessor_changelog.py",
        "// Regenerate:      pnpm gen:subprocessor-changelog",
        "// Drift check:     pnpm check:subprocessor-changelog  (runs in CI)",
        "//",
        "// /legal/sub-processors § 10 renders these rows. The same entries drive",
        "// the notice emailed to every customer organisation's administrators",
        "// (backend/app/services/subprocessor_notices.py, docs/decisions.md §321),",
        "// so a row here and the notice a customer received cannot disagree.",
        "// Add a row in the Python module, never here.",
        "",
        "/** The advance notice the DPA promises, in days. */",
        f"export const NOTICE_PERIOD_DAYS = {NOTICE_PERIOD_DAYS};",
        "",
        "/** The objection address the notice email names. Must equal",
        " *  `CONTACT.privacy` (operator.ts) — a vitest holds the two together. */",
        f"export const NOTICE_OBJECTION_CONTACT = {_ts(OBJECTION_CONTACT)};",
        "",
        "export interface SubprocessorChange {",
        "\t/** Stable entry id — the key the notice ledger is written under. */",
        "\treadonly id: string;",
        "\t/** ISO date the row was published. */",
        "\treadonly publishedOn: string;",
        "\t/** `publishedOn` as the page prints it (e.g. 15 September 2026). */",
        "\treadonly publishedLabel: string;",
        "\treadonly change: string;",
        "\t/** Whether the change needed, and was given, the advance notice. */",
        "\treadonly notice: boolean;",
        "\t/** When a notice change takes effect, as the page prints it; null otherwise. */",
        "\treadonly effectiveLabel: string | null;",
        "\t/** Why a notice was shorter than NOTICE_PERIOD_DAYS, if it was. */",
        "\treadonly urgentReason: string | null;",
        "}",
        "",
        "/** Oldest first, exactly as the backend change log lists them. */",
        "export const SUBPROCESSOR_CHANGES = [",
    ]
    for entry in CHANGELOG:
        lines += [
            "\t{",
            f"\t\tid: {_ts(entry.id)},",
            f"\t\tpublishedOn: {_ts(entry.published_on.isoformat())},",
            f"\t\tpublishedLabel: {_ts(long_date(entry.published_on))},",
            f"\t\tchange: {_ts(entry.change)},",
            f"\t\tnotice: {_ts(entry.notice)},",
            "\t\teffectiveLabel: "
            + _ts(long_date(entry.effective_on) if entry.effective_on else None)
            + ",",
            f"\t\turgentReason: {_ts(entry.urgent_reason)},",
            "\t},",
        ]
    lines += ["] as const satisfies readonly SubprocessorChange[];", ""]
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
    try:
        rendered = render()
    except ChangelogError as exc:
        print(f"The sub-processor change log is invalid: {exc}", file=sys.stderr)
        return 1

    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == rendered:
            print(f"{OUTPUT_PATH}: in sync with the sub-processor change log")
            return 0
        print(
            f"{OUTPUT_PATH} is STALE — backend/app/services/subprocessor_changelog.py "
            "lists different changes than /legal/sub-processors publishes.\n"
            "Run `pnpm gen:subprocessor-changelog`.",
            file=sys.stderr,
        )
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
