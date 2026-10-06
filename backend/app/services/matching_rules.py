"""Resolve the effective PO-match rule for an invoice.

PO matching has two tunable knobs: ``require_inspection`` (does a matched PO
need a quality-inspection record before the invoice clears the 4-way leg) and
``tolerance_pct`` (the allowed amount variance vs. the PO). Historically both
came from a single per-org setting. This module lets an org configure either
knob *per vendor* or *per commodity type*, falling back to the org default.

"Commodity type" is the invoice's header GL account (``invoice.gl_account``, a
``String(100)`` column that already exists) — no new DB columns.

The settings shape (all keys optional, backward-compatible) lives under
``Organization.settings.matching``::

    {
      "matching": {
        "require_inspection": false,
        "tolerance_pct": 5.0,
        "vendor_rules":    { "<vendor_id_uuid_str>": { "require_inspection": true,
                                                       "tolerance_pct": 2.0 } },
        "commodity_rules": { "<gl_account_code>":     { "require_inspection": true,
                                                       "tolerance_pct": 1.0 } }
      }
    }

Precedence is **per-field** (not whole-rule): for each of ``require_inspection``
and ``tolerance_pct`` independently, take the first present value walking

    vendor_rules[str(vendor_id)]  →  commodity_rules[gl_account]  →
    matching.<field>  →  hardcoded default (require_inspection=False, tolerance_pct=5.0)

So a vendor rule that only sets ``require_inspection`` still lets
``tolerance_pct`` fall through to the commodity / org / default layers.

The resolver is pure (no DB, no I/O) and never raises: malformed config —
non-dict rules, missing keys, ``vendor_id``/``gl_account`` ``None``, non-numeric
tolerance — is silently ignored in favour of the next layer down.

``tolerance_pct`` accepts a number **or its exact decimal string** ("1.0"), the
representation this project already uses for money in JSONB and the one
``po_matching.match_invoice_to_po`` already declares in its signature. That
matters because falling through here does not fail closed: the walk ends at
``DEFAULT_TOLERANCE_PCT`` (5.0), which is looser than any tolerance an org
would configure, so an unparsed value silently WIDENS the gate it was meant to
tighten.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

DEFAULT_REQUIRE_INSPECTION = False
# Decimal, never float — this tolerance flows straight into the PO-match gate,
# which compares in exact Decimal. Keeping the whole path Decimal means a
# per-vendor tolerance like 2.5 never picks up a binary-float artefact.
DEFAULT_TOLERANCE_PCT = Decimal("5.0")


@dataclass(frozen=True)
class EffectiveMatchRule:
    """The resolved per-invoice match rule.

    ``source`` records where ``require_inspection`` was resolved from
    ("vendor" | "commodity" | "org" | "default") for debuggability / logging.
    """

    require_inspection: bool
    tolerance_pct: Decimal
    source: str


def _rule_for(rules: object, key: str | None) -> dict:
    """Return the rule dict for ``key`` within ``rules``, or ``{}`` if absent/malformed."""
    if key is None or not isinstance(rules, dict):
        return {}
    rule = rules.get(key)
    return rule if isinstance(rule, dict) else {}


def _coerce_inspection(rule: dict) -> bool | None:
    """Extract ``require_inspection`` from a rule dict, or ``None`` if absent."""
    if "require_inspection" not in rule:
        return None
    return bool(rule["require_inspection"])


def _coerce_tolerance(rule: dict) -> Decimal | None:
    """Extract a numeric ``tolerance_pct`` as exact Decimal, or ``None`` if
    absent/non-numeric. Floats bridge through ``str`` so ``2.5`` lands as
    ``Decimal('2.5')``, never the binary-float artefact ``Decimal(2.5)`` gives.

    **A numeric STRING is accepted**, and that matters more here than anywhere
    else in this module. These rules live in a hand-edited JSONB blob, where an
    exact decimal string is the project's own preferred representation for a
    money-ish number (`auto_approve_below` is stored that way in
    `steps_config`), and `po_matching.match_invoice_to_po` already types its
    `tolerance_pct` parameter `Decimal | float | int | str`. Rejecting `"1.0"`
    made the resolver disagree with its only consumer — and, because `None`
    means "fall through", the walk terminated at `DEFAULT_TOLERANCE_PCT` (5.0),
    which is LOOSER than any tolerance an org would bother configuring. A
    high-risk supplier tightened to 1% silently got 5%, so an invoice 4.5% over
    its PO read `within_tolerance: True` → `matched` → no `po_mismatch`
    exception → straight into the approval queue as clean. The resolver never
    raises and never logs, so nothing surfaced it.

    Bools are still rejected (a bool is an int subclass, and `true` would
    resolve to a 1% tolerance nobody asked for), as are non-finite values —
    both fall through to the next layer rather than becoming a rule.
    """
    if "tolerance_pct" not in rule:
        return None
    value = rule["tolerance_pct"]
    # Reject bools (a bool is an int subclass) and anything that isn't a number
    # or a string spelling of one.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    # `Decimal("NaN")` / `Decimal("Infinity")` parse but can't be compared
    # meaningfully against a variance — treat them as absent config.
    return parsed if parsed.is_finite() else None


def resolve_match_rule(
    org_settings: dict | None,
    *,
    vendor_id: uuid.UUID | str | None,
    gl_account: str | None,
) -> EffectiveMatchRule:
    """Resolve the effective match rule for one invoice.

    Per-field precedence: vendor rule → commodity rule → org default →
    hardcoded default. ``vendor_id`` may be a ``uuid.UUID``, ``str``, or
    ``None``; commodity is the invoice's header GL account (``gl_account``).
    Never raises — malformed config falls through to the next layer.
    """
    matching = (org_settings or {}).get("matching")
    if not isinstance(matching, dict):
        matching = {}

    vendor_key = str(vendor_id) if vendor_id is not None else None
    vendor_rule = _rule_for(matching.get("vendor_rules"), vendor_key)
    commodity_rule = _rule_for(matching.get("commodity_rules"), gl_account)
    org_inspection = _coerce_inspection(matching)
    org_tolerance = _coerce_tolerance(matching)

    # require_inspection — per-field walk, tracking the source layer.
    require_inspection = DEFAULT_REQUIRE_INSPECTION
    source = "default"
    if (value := _coerce_inspection(vendor_rule)) is not None:
        require_inspection, source = value, "vendor"
    elif (value := _coerce_inspection(commodity_rule)) is not None:
        require_inspection, source = value, "commodity"
    elif org_inspection is not None:
        require_inspection, source = org_inspection, "org"

    # tolerance_pct — independent per-field walk.
    tolerance_pct = DEFAULT_TOLERANCE_PCT
    if (value := _coerce_tolerance(vendor_rule)) is not None:
        tolerance_pct = value
    elif (value := _coerce_tolerance(commodity_rule)) is not None:
        tolerance_pct = value
    elif org_tolerance is not None:
        tolerance_pct = org_tolerance

    return EffectiveMatchRule(
        require_inspection=require_inspection,
        tolerance_pct=tolerance_pct,
        source=source,
    )


def strictest_rule_for_any_commodity(
    org_settings: dict | None, *, vendor_id: uuid.UUID | str | None
) -> EffectiveMatchRule:
    """The strictest rule this invoice could resolve to, whatever its GL code.

    ``resolve_match_rule`` lets the header GL account (the commodity) pick the
    tolerance and ``require_inspection``, and ``gl_account`` stays editable on an
    approved invoice. So a rule that clears a payment block AFTER approval must
    not depend on the GL code someone may just have changed: this resolves the
    rule under every configured commodity (and none) for the vendor, then takes
    the smallest tolerance and requires an inspection if any of them does. A
    vendor rule still outranks every commodity, exactly as in the resolver.
    """
    matching = (org_settings or {}).get("matching")
    commodities = matching.get("commodity_rules") if isinstance(matching, dict) else None
    keys: list[str | None] = [None]
    if isinstance(commodities, dict):
        keys.extend(k for k in commodities if isinstance(k, str))
    rules = [resolve_match_rule(org_settings, vendor_id=vendor_id, gl_account=k) for k in keys]
    return EffectiveMatchRule(
        require_inspection=any(r.require_inspection for r in rules),
        tolerance_pct=min(r.tolerance_pct for r in rules),
        source="strictest",
    )
