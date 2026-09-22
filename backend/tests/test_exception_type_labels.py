"""Guard: every exception type the platform raises has a friendly label.

`app/api/exceptions.py::EXCEPTION_TYPE_LABELS` maps `Exception.exception_type`
values to the human-readable strings the exception-queue UI shows (`type_label`
in `_exception_dict`). A missing entry isn't a crash — `.get()` falls back to
the raw string — so it silently renders as e.g. `line_total_mismatch` instead of
"Line Total Mismatch" (issue #151).

**Why this file scans the source instead of listing the types.** The first
version of this guard carried a hand-maintained set of the "real" types. That
set is itself the thing that goes stale: `line_total_mismatch`,
`payment_compliance_hold` and `price_variance` were all raised by the platform
without ever being added here, and the guard stayed green while the label map
drifted — including for `line_total_mismatch`, which BLOCKS a payment run and so
is one of the rows an AP manager most needs to read as a control, not as debug
output.

So the population is now *derived*: `app/services/exception_lifecycle.py`
declares the canonical `EXCEPTION_TYPES` roster, and an AST walk over `app/`
collects the type strings the code actually uses. A new type raised anywhere
fails this file until it joins the roster AND gets a label — there is no
second list to remember.

Pure-Python, no DB, no imports of the scanned modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.api.exceptions import EXCEPTION_TYPE_LABELS
from app.services.exception_lifecycle import (
    EXCEPTION_SEVERITY_RANK,
    EXCEPTION_TYPES,
    LEGACY_EXCEPTION_TYPES,
)

APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: Call sites that pass an exception type as a bare positional string. Keyed by
#: callee name → the 0-based index of that argument. Every other raise site in
#: the tree uses the `exception_type=` keyword, which needs no entry here — but
#: a NEW helper that takes the type positionally must be registered, or its
#: raise sites are invisible to this scan.
_POSITIONAL_TYPE_ARGS = {"_ensure_exception": 2}


def _string_constants(node: ast.AST) -> set[str]:
    """String constants in a literal expression (a bare string, or a tuple/list
    of them). Anything non-literal is ignored — it can't be checked statically."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List)):
        found: set[str] = set()
        for element in node.elts:
            found |= _string_constants(element)
        return found
    return set()


def _callee_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _types_used_in_source() -> dict[str, set[str]]:
    """``{exception_type: {"relative/path.py:line", …}}`` for every type string
    the backend statically hands to the exception machinery.

    Three shapes are recognised, which together cover every raise site:

    * ``exception_type="…"`` keyword (``create_exception`` and friends),
    * the positional type argument of a known helper (``_ensure_exception``),
    * a module constant named ``*_EXCEPTION_TYPE`` / ``*_EXCEPTION_TYPES``
      (``PAYMENT_BLOCKING_EXCEPTION_TYPES``, ``COMPLIANCE_EXCEPTION_TYPE``, …).
    """
    used: dict[str, set[str]] = {}

    def record(values: set[str], path: Path, lineno: int) -> None:
        where = f"{path.relative_to(APP_DIR.parent)}:{lineno}"
        for value in values:
            used.setdefault(value, set()).add(where)

    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "exception_type":
                        record(_string_constants(keyword.value), path, node.lineno)
                index = _POSITIONAL_TYPE_ARGS.get(_callee_name(node.func) or "")
                if index is not None and len(node.args) > index:
                    record(_string_constants(node.args[index]), path, node.lineno)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                if any(n.endswith(("_EXCEPTION_TYPE", "_EXCEPTION_TYPES")) for n in names):
                    if node.value is not None:
                        record(_string_constants(node.value), path, node.lineno)
    return used


def test_scan_finds_the_known_raise_sites():
    """Sanity-check the scanner itself — a guard that silently matches nothing
    would pass forever."""
    used = _types_used_in_source()
    # One of each recognised shape: keyword, positional helper arg, constant.
    assert "review_rejected" in used  # exception_type="…" in services/review.py
    assert "quality_hold" in used  # _ensure_exception(db, invoice, "quality_hold", …)
    assert "erp_reconciliation" in used  # ERP_RECONCILIATION_EXCEPTION_TYPE
    assert len(used) >= 10


def test_every_raised_type_is_on_the_canonical_roster():
    used = _types_used_in_source()
    unknown = {t: sorted(where) for t, where in used.items() if t not in EXCEPTION_TYPES}
    assert not unknown, (
        "exception types raised in app/ but missing from "
        f"exception_lifecycle.EXCEPTION_TYPES: {unknown}"
    )


def test_roster_has_no_undeclared_dead_entries():
    """A roster type nothing raises is a typo or a leftover — unless it is
    declared legacy (kept so historical rows keep their label)."""
    used = _types_used_in_source()
    dead = sorted(set(EXCEPTION_TYPES) - used.keys() - LEGACY_EXCEPTION_TYPES)
    assert not dead, (
        "EXCEPTION_TYPES lists types nothing in app/ raises; either delete them "
        f"or declare them in LEGACY_EXCEPTION_TYPES: {dead}"
    )


def test_legacy_types_are_on_the_roster_and_labelled():
    """A legacy type still reaches the queue from historical rows, so it must
    keep both its roster seat and its friendly label."""
    assert LEGACY_EXCEPTION_TYPES <= set(EXCEPTION_TYPES)
    assert LEGACY_EXCEPTION_TYPES <= EXCEPTION_TYPE_LABELS.keys()


def test_roster_has_no_duplicates():
    assert len(EXCEPTION_TYPES) == len(set(EXCEPTION_TYPES))


def test_every_exception_type_has_a_label():
    missing = sorted(set(EXCEPTION_TYPES) - EXCEPTION_TYPE_LABELS.keys())
    assert not missing, f"EXCEPTION_TYPE_LABELS is missing entries for: {missing}"


def test_label_map_has_no_entries_for_unknown_types():
    extra = sorted(EXCEPTION_TYPE_LABELS.keys() - set(EXCEPTION_TYPES))
    assert not extra, f"EXCEPTION_TYPE_LABELS labels types that don't exist: {extra}"


def test_payment_blocking_types_are_on_the_roster():
    """The three types that stop a payment run must be first-class members of
    the roster — they are the ones an AP manager reads under time pressure."""
    from app.api.payments import PAYMENT_BLOCKING_EXCEPTION_TYPES

    off_roster = sorted(set(PAYMENT_BLOCKING_EXCEPTION_TYPES) - set(EXCEPTION_TYPES))
    assert not off_roster, f"payment-blocking types missing from the roster: {off_roster}"


#: Positional severity arguments, same idea as `_POSITIONAL_TYPE_ARGS`.
_POSITIONAL_SEVERITY_ARGS = {"_ensure_exception": 3}


def _severities_used_in_source() -> dict[str, set[str]]:
    """``{severity: {"relative/path.py:line", …}}`` for every literal severity
    handed to the exception machinery — the ``severity="…"`` keyword and the
    positional argument of a known helper.

    Wider than exception raise sites on purpose: invoice-warning and enrichment
    findings carry a ``severity=`` keyword too, from the same three-value
    vocabulary. A literal outside it anywhere is worth a look, and the
    alternative — deciding per call site whether it reaches an ``Exception`` —
    is the hand-maintained list this file exists to avoid.
    """
    used: dict[str, set[str]] = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            values: set[str] = set()
            for keyword in node.keywords:
                if keyword.arg == "severity":
                    values |= _string_constants(keyword.value)
            index = _POSITIONAL_SEVERITY_ARGS.get(_callee_name(node.func) or "")
            if index is not None and len(node.args) > index:
                values |= _string_constants(node.args[index])
            for value in values:
                where = f"{path.relative_to(APP_DIR.parent)}:{node.lineno}"
                used.setdefault(value, set()).add(where)
    return used


def test_every_raised_severity_has_a_sort_rank():
    """The queue sorts severity by `EXCEPTION_SEVERITY_RANK`, and a severity
    missing from it ranks 0 — below `info`. A new `severity="critical"` raised
    without joining the map would therefore sort as the LEAST urgent row in
    the queue, silently."""
    used = _severities_used_in_source()
    # Sanity: the scanner matched both shapes.
    assert {"error", "warning", "info"} <= used.keys(), used.keys()
    unranked = {s: sorted(where) for s, where in used.items() if s not in EXCEPTION_SEVERITY_RANK}
    assert not unranked, f"severities raised in app/ with no sort rank: {unranked}"


def test_severity_rank_is_strictly_ordered_worst_first():
    """Declared worst-first (the chip order), with distinct ranks — two
    severities sharing a rank would sort as one."""
    ranks = list(EXCEPTION_SEVERITY_RANK.values())
    assert ranks == sorted(ranks, reverse=True)
    assert len(set(ranks)) == len(ranks)
    assert min(ranks) > 0, "0 is reserved for a severity the map does not know"


def test_labels_are_non_empty_human_readable_strings():
    for exc_type, label in EXCEPTION_TYPE_LABELS.items():
        assert isinstance(label, str) and label.strip(), (
            f"EXCEPTION_TYPE_LABELS[{exc_type!r}] must be a non-empty string"
        )
        # The whole point of the map is to not show the raw snake_case value.
        assert "_" not in label, (
            f"EXCEPTION_TYPE_LABELS[{exc_type!r}] = {label!r} looks like a raw "
            "enum value, not a friendly label"
        )
