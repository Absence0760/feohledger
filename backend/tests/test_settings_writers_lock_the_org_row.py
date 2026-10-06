"""Every writer of `Organization.settings` takes the org row lock first.

Each writer reads the JSONB, changes one block and writes the WHOLE dict back,
so two interleaving writers mean the one that commits second silently reverts
the other — a branding save undoing an SSO client-secret rotation, a sweep's
cursor write undoing a retention change. `app.tenant.lock_organization`
(`SELECT … FOR UPDATE` with a refresh) is the one fix, and it only works if
every writer takes it; a single unlocked writer reopens the race for all of
them. The 2026-10-05 SSO slice locked five writers and a review found eleven
more, which is the case for a guard rather than a convention.

Static on purpose: a race test per writer would need two sessions each, and
the property is "this function took the lock before writing", which is visible
in the source.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

# Helpers that write `settings` on an org their CALLER loaded; the lock is the
# caller's job, so each call site's enclosing function must take it instead.
CALLER_LOCKS = {"write_groups", "_persist", "provision_intake_token"}

LOCK_MARKERS = ("lock_organization(", "with_for_update(")


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _writes_settings(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign | ast.AugAssign)
            else []
        )
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr == "settings":
                return True
    return False


def _calls(fn: ast.AST, names: set[str]) -> set[str]:
    found = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = (
                f.id
                if isinstance(f, ast.Name)
                else f.attr
                if isinstance(f, ast.Attribute)
                else None
            )
            if name in names:
                found.add(name)
    return found


def _sources():
    for path in sorted(APP.rglob("*.py")):
        source = path.read_text()
        yield path, source, ast.parse(source)


def _locks(source: str, fn: ast.AST) -> bool:
    body = ast.get_source_segment(source, fn) or ""
    return any(marker in body for marker in LOCK_MARKERS)


def test_every_settings_writer_takes_the_org_row_lock():
    unlocked = []
    for path, source, tree in _sources():
        for fn in _functions(tree):
            if fn.name in CALLER_LOCKS or not _writes_settings(fn):
                continue
            if not _locks(source, fn):
                unlocked.append(f"{path.relative_to(APP.parent)}::{fn.name}")
    assert unlocked == [], (
        "these functions write Organization.settings without taking "
        "app.tenant.lock_organization first: " + ", ".join(unlocked)
    )


def test_every_caller_of_a_settings_helper_takes_the_lock():
    unlocked = []
    helpers_seen: set[str] = set()
    for path, source, tree in _sources():
        for fn in _functions(tree):
            if fn.name in CALLER_LOCKS and _writes_settings(fn):
                helpers_seen.add(fn.name)
                continue
            if _calls(fn, CALLER_LOCKS) and not _locks(source, fn):
                unlocked.append(f"{path.relative_to(APP.parent)}::{fn.name}")
    # A renamed helper would otherwise exempt its callers silently.
    assert helpers_seen == CALLER_LOCKS
    assert unlocked == [], (
        "these functions call a settings-writing helper without taking "
        "app.tenant.lock_organization first: " + ", ".join(unlocked)
    )
