"""Generate the public pricing page's plan module from the plan catalogue.

Issue #426 was the pricing page selling a product the billing code did not
model: a hand-typed grid ($29 per seat, an annual toggle, a 5-seat minimum)
beside a catalogue of flat monthly plans. `docs/decisions.md` §253 settled the
tiers; this makes the page READ them, so the two cannot drift apart again.
`app/services/billing/plan_catalog.py::DEFAULT_PLAN_CATALOG` is the source;
`frontend/src/lib/marketing/plans.generated.ts` is what this writes.

Two modes, one behaviour (the `gen:einvoice-messages` shape):

    python scripts/gen_pricing_catalog.py            # write the file
    python scripts/gen_pricing_catalog.py --check     # fail if stale

``--check`` is the drift guard CI runs. Change a price, an allowance, an
overage rate or a feature grant in the catalogue and the committed module
differs, so CI goes red until it is regenerated. A NEW feature key then fails
`pnpm check` too: the page's label map is typed
``Record<PlanFeature, MessageKey>`` over the generated union, so a feature with
no label is a compile error rather than a silently missing bullet.

Money leaves here as decimal STRINGS, never floats: `Decimal` → `str` is exact,
and the page formats it through `formatMoney` like every other amount.

Only what a plan can actually state is emitted. An entitlement key outside
`ALL_FEATURES`, a usage component other than the AI-read meter, or a malformed
overage price fails generation outright — the page has no honest way to
describe something it does not know, and a silent omission is how the old grid
ended up describing a different product.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Anchor THIS checkout's `backend/` on sys.path before `app` is imported — the
# same two lines, and the same reason, as `gen_einvoice_rule_messages.py`: a
# worktree reusing the primary checkout's `.venv` would otherwise read the
# OTHER checkout's catalogue through the editable install.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))

from app.services.billing.plan_catalog import (  # noqa: E402
    ALL_FEATURES,
    CATALOG_CURRENCY,
    DEFAULT_PLAN_CATALOG,
    METER_AI_INVOICES,
)

#: Relative to the repo root so a worktree writes into its own frontend.
OUTPUT_PATH = Path("frontend/src/lib/marketing/plans.generated.ts")


class CatalogError(ValueError):
    """The catalogue holds something the pricing page cannot describe."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _decimal_string(value: object, *, where: str) -> str:
    """An exact, non-negative decimal string, or a `CatalogError`."""
    if isinstance(value, float):
        # A float has already lost the exact value; refusing it is the point.
        raise CatalogError(f"{where} is a float ({value!r}); use Decimal or a string")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise CatalogError(f"{where} is not a decimal: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise CatalogError(f"{where} must be a finite, non-negative amount: {value!r}")
    return str(value)


def plan_rows() -> list[dict]:
    """The catalogue, reduced to exactly what the pricing page renders."""
    rows: list[dict] = []
    for spec in DEFAULT_PLAN_CATALOG:
        code = spec["code"]

        unknown = set(spec["entitlements"]) - set(ALL_FEATURES)
        if unknown:
            raise CatalogError(
                f"plan {code!r} grants {sorted(unknown)}, which are not in ALL_FEATURES"
            )
        # Catalogue order, not dict order, so reordering a plan's literal is
        # not a diff and every tier lists features in the same sequence.
        features = [f for f in ALL_FEATURES if spec["entitlements"].get(f)]

        components = spec["usage_components"]
        if set(components) != {METER_AI_INVOICES}:
            raise CatalogError(
                f"plan {code!r} meters {sorted(components)}; the page only knows "
                f"{METER_AI_INVOICES!r}"
            )
        meter = components[METER_AI_INVOICES]
        included = meter["included"]
        if not isinstance(included, int) or isinstance(included, bool) or included < 0:
            raise CatalogError(f"plan {code!r} has a bad included allowance: {included!r}")
        overage = meter["overage_unit_price"]
        if overage is not None:
            overage = _decimal_string(overage, where=f"plan {code!r} overage_unit_price")

        rows.append(
            {
                "code": code,
                "name": spec["name"],
                "monthlyPrice": _decimal_string(
                    spec["monthly_price"], where=f"plan {code!r} monthly_price"
                ),
                "includedAiInvoices": included,
                "overageUnitPrice": overage,
                "trialDays": spec["trial_days"],
                "features": features,
            }
        )
    return rows


def _ts(value: object) -> str:
    """A TypeScript literal in the frontend's style (single-quoted strings)."""
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return json.dumps(value)


def render() -> str:
    lines: list[str] = [
        "// GENERATED FILE — do not edit by hand.",
        "//",
        "// Source of truth: backend/app/services/billing/plan_catalog.py",
        "// Regenerate:      pnpm gen:pricing",
        "// Drift check:     pnpm check:pricing  (runs in CI)",
        "//",
        "// The public pricing page renders every price, allowance, overage rate",
        "// and feature from this module (docs/decisions.md §253, issue #426), so",
        "// a catalogue change is a red CI check until this file is regenerated.",
        "// Money is an exact decimal STRING — format it with formatMoney, never",
        "// parse it into arithmetic.",
        "",
        "/** The ISO 4217 code every catalog plan is priced in. */",
        f"export const PLAN_CURRENCY = {_ts(CATALOG_CURRENCY)};",
        "",
        "/** Every gated feature, in catalogue order (`ALL_FEATURES`). */",
        "export const PLAN_FEATURES = [",
    ]
    lines += [f"\t{_ts(f)}," for f in ALL_FEATURES]
    lines += [
        "] as const;",
        "",
        "export type PlanFeature = (typeof PLAN_FEATURES)[number];",
        "",
        "export interface CatalogPlan {",
        "\t/** Stable machine code (`Plan.code`). */",
        "\treadonly code: string;",
        "\t/** Display name. A proper noun; not translated. */",
        "\treadonly name: string;",
        "\t/** Flat price per organisation per month, as an exact decimal string. */",
        "\treadonly monthlyPrice: string;",
        "\t/** AI-read invoices the monthly price covers. */",
        "\treadonly includedAiInvoices: number;",
        "\t/** Price per AI-read invoice past the allowance, or `null`: AI reading",
        "\t *  pauses at the limit and nothing else stops. */",
        "\treadonly overageUnitPrice: string | null;",
        "\t/** `Plan.trial_days`. Carried for completeness — nothing grants a trial",
        "\t *  yet, so the page must not promise one. */",
        "\treadonly trialDays: number;",
        "\t/** Features the plan grants, in catalogue order. */",
        "\treadonly features: readonly PlanFeature[];",
        "}",
        "",
        "/** `DEFAULT_PLAN_CATALOG`, in catalogue order. Enterprise is not here: it is a",
        " *  negotiated contract, shown on the page as contact-sales. */",
        "export const PLANS = [",
    ]
    for row in plan_rows():
        lines.append("\t{")
        for key in (
            "code",
            "name",
            "monthlyPrice",
            "includedAiInvoices",
            "overageUnitPrice",
            "trialDays",
        ):
            lines.append(f"\t\t{key}: {_ts(row[key])},")
        if row["features"]:
            lines.append("\t\tfeatures: [")
            lines += [f"\t\t\t{_ts(f)}," for f in row["features"]]
            lines.append("\t\t],")
        else:
            lines.append("\t\tfeatures: [],")
        lines.append("\t},")
    lines += ["] as const satisfies readonly CatalogPlan[];", ""]
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
            print(f"{OUTPUT_PATH}: in sync with the plan catalogue")
            return 0
        print(
            f"{OUTPUT_PATH} is STALE — backend/app/services/billing/plan_catalog.py "
            "describes different plans than the public pricing page renders.\n"
            "Run `pnpm gen:pricing`. A new feature key also needs a label in "
            "frontend/src/lib/marketing/pricing.ts (all six locales).",
            file=sys.stderr,
        )
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
