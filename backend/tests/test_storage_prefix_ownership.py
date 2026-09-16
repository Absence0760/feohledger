"""Every object key begins with the owning organisation's id.

This is not a tidiness rule. Two things rest on it:

* **Deleting a tenant reaches all of its documents.**
  ``services/storage.delete_prefix`` sweeps ``{org_id}/`` and is the only
  traversal in the app — it is how ``tenant_deletion`` keeps the deletion
  clause in ``/legal/dpa`` § 13 ("the object-storage key prefix holding your
  uploaded invoices, receipts, contracts and tax forms"). A key written
  outside that prefix survives the deletion silently, because nothing
  enumerates it and no row points at it any more.
* **One tenant's crafted filename cannot land under another's prefix.**
  ``_safe_filename`` strips path separators for exactly this reason, and says
  so. That guard only means anything while the org id is the FIRST segment.

So the invariant is asserted at the shape of every key-construction site rather
than by reading them once. A new upload surface that writes
``f"uploads/{org_id}/..."`` — plausible, tidy-looking, and wrong — fails here.
"""

from __future__ import annotations

import ast
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

# The expressions allowed to open a key. Both spellings of the same thing: the
# id passed as an argument, and the id read off a loaded Organization.
OWNER_EXPRESSIONS = {"org_id", "org.id", "organization_id", "organization.id"}

# Every site found when this guard was written. A floor, not a target: the test
# asserting "no violations" is vacuous if the scan stops finding anything, which
# is what would happen if the assignment shape changed and this file silently
# started scanning nothing.
KNOWN_SITE_COUNT = 10


def _key_sites() -> list[tuple[str, int, ast.JoinedStr]]:
    """Every `file_key = f"..."` assignment under `app/`."""
    sites: list[tuple[str, int, ast.JoinedStr]] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.JoinedStr):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "file_key":
                    sites.append((str(path.relative_to(APP_DIR.parent)), node.lineno, node.value))
    return sites


def test_every_object_key_starts_with_the_org_id():
    offenders: list[str] = []
    for path, lineno, template in _key_sites():
        first = template.values[0] if template.values else None
        if not isinstance(first, ast.FormattedValue):
            offenders.append(f"{path}:{lineno} — key starts with a literal, not the org id")
            continue
        expression = ast.unparse(first.value)
        if expression not in OWNER_EXPRESSIONS:
            offenders.append(f"{path}:{lineno} — key starts with {expression!r}")

    assert offenders == [], (
        "every S3 key must begin with the owning org's id, or `storage.delete_prefix` "
        "cannot reach it when the tenant is deleted (see /legal/dpa § 13): " + "; ".join(offenders)
    )


def test_the_scan_still_finds_the_key_sites_it_is_guarding():
    """A guard that finds nothing passes forever."""
    found = len(_key_sites())
    assert found >= KNOWN_SITE_COUNT, (
        f"only {found} key-construction sites found, expected at least "
        f"{KNOWN_SITE_COUNT} — the scan has stopped seeing them, so the "
        "ownership assertion above is passing vacuously. Fix the scan, or "
        "lower KNOWN_SITE_COUNT deliberately if a surface was genuinely removed."
    )
