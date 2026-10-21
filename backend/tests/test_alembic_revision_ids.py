"""Alembic revision ids must fit the column that stores them.

``alembic_version.version_num`` is ``VARCHAR(32)``. A longer revision id is not
caught at authoring time, at import time, or by any linter — it surfaces as a
``StringDataRightTruncationError`` the first time ``alembic upgrade head`` runs,
which aborts the whole upgrade before applying anything.

That failure mode is worse than it sounds, because a deploy applies migrations
and then starts the app against the NEW model: the DB stays on the previous
revision while the code expects the new column, so every read of the affected
table 500s. Migration 0086 shipped a 33-character id and did exactly this to the
shared dev environment; its own docstring cited the trap and missed it by one
character.

These are cheap source-level checks, so they run without a database.
"""

import ast
import re
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"

# The width of alembic_version.version_num. Alembic's own default.
MAX_REVISION_LENGTH = 32

_REVISION_RE = re.compile(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)[\"']", re.M)
_DOWN_REVISION_RE = re.compile(r"^down_revision(?::\s*[^=]+)?\s*=\s*[\"']([^\"']+)[\"']", re.M)


def _revisions() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        match = _REVISION_RE.search(path.read_text())
        if match:
            found[match.group(1)] = path
    return found


def test_every_revision_id_fits_the_version_column() -> None:
    too_long = {
        rev: (len(rev), path.name)
        for rev, path in _revisions().items()
        if len(rev) > MAX_REVISION_LENGTH
    }
    assert not too_long, (
        "alembic_version.version_num is VARCHAR"
        f"({MAX_REVISION_LENGTH}); these revision ids do not fit, so "
        f"`alembic upgrade head` aborts before applying anything: {too_long}"
    )


def test_revision_ids_are_unique() -> None:
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        match = _REVISION_RE.search(path.read_text())
        if not match:
            continue
        rev = match.group(1)
        if rev in seen:
            duplicates.append(f"{rev}: {seen[rev]} and {path.name}")
        seen[rev] = path.name
    assert not duplicates, f"duplicate alembic revision ids: {duplicates}"


def test_every_down_revision_resolves() -> None:
    known = set(_revisions())
    dangling: list[str] = []
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        match = _DOWN_REVISION_RE.search(path.read_text())
        if not match:
            continue
        parent = match.group(1)
        if parent not in known:
            dangling.append(f"{path.name} -> {parent}")
    assert not dangling, (
        "these migrations name a down_revision that no file defines, so the "
        f"chain is broken: {dangling}"
    )


# ---------------------------------------------------------------------------
# One head, and one migration per number
# ---------------------------------------------------------------------------
#
# Two PRs written against the same `main` each add the next migration with the
# same `down_revision`. Each PR's CI is green, because each is tested alone; the
# second to merge leaves `main` with two heads, and the deploy's
# `alembic upgrade head` refuses ("Multiple head revisions are present"), so the
# release fails at its migration step. Branch protection does not require a PR
# to be up to date with `main` before merging, so a PR's green check can predate
# the merge that breaks it. That is why ci.yml also runs on every push to `main`:
# the post-merge run is where this fails if two such PRs land together.


def _down_revisions(path: Path) -> tuple[str, ...]:
    """Every parent `path` names. A merge migration names a tuple of them."""
    tree = ast.parse(path.read_text())
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "down_revision" for t in targets):
            continue
        parent = ast.literal_eval(value)
        if parent is None:
            return ()
        if isinstance(parent, str):
            return (parent,)
        return tuple(parent)
    return ()


def test_the_migration_chain_has_exactly_one_head() -> None:
    revisions = _revisions()
    parents = {parent for path in revisions.values() for parent in _down_revisions(path)}
    heads = sorted(rev for rev in revisions if rev not in parents)
    assert len(heads) == 1, (
        f"the migration chain has {len(heads)} heads: {heads}. Two migrations were "
        "written against the same parent (usually two PRs that each added the next "
        "number). Renumber the one that landed second so its down_revision is the "
        "other's revision, and rename its file to the next number, rather than "
        "adding a merge migration; a merge is only for branches that both already "
        "ran somewhere."
    )


#: Numbers that more than one migration already shares. History, frozen: four
#: parallel PRs each added a `0022_*` and `0023_merge_roadmap_fanout_*` joined
#: them after all four had run on databases. Never add to this.
_SHARED_NUMBERS_ALLOWED = frozenset({"0022"})


def test_no_two_migrations_share_a_number() -> None:
    by_number: dict[str, list[str]] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        number = path.name.split("_", 1)[0]
        if number.isdigit():
            by_number.setdefault(number, []).append(path.name)
    shared = {
        number: names
        for number, names in by_number.items()
        if len(names) > 1 and number not in _SHARED_NUMBERS_ALLOWED
    }
    assert not shared, (
        "these migration numbers are used more than once, which means two "
        "migrations were written against the same parent and the chain forks: "
        f"{shared}. Renumber the later one to the next free number and point its "
        "down_revision at the other."
    )
