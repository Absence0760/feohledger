"""Tenant database engines are built only from a resolved org row, and stay that way.

Tenant isolation is the project's highest-blast-radius invariant, and the piece
of it asserted elsewhere is narrow: `tests/test_tenant_isolation.py` proves
`app.tenant.get_tenant` cross-checks the JWT `org` claim against the slug in
`X-Tenant-Slug`. That proves the chokepoint is *correct*. Nothing proved it is
*used*.

The gap it leaves has a real shape. A tenant session is only ever as isolated as
the URL its engine was built from, and a URL is a string: any module that does

    create_async_engine(f"{base}/feoh_{slug}")     # slug straight off the request

reaches a tenant database without the org row, without the JWT cross-check, and
without any test noticing — every guard in `get_tenant` sits upstream of a call
that never happens.

So this module pins the construction discipline rather than the string:

* `app/tenant_url.py` owns the construction itself — one dependency-free
  `make_tenant_url(base_url, db_name)`. `app/database.py` binds it to
  `settings.database_url` as `_make_tenant_url(db_name)`; the three AWS Lambda
  handlers, which cannot import `app.database` (it reaches `app.config`, and
  backend/CLAUDE.md forbids dotenv-reaching imports on a Lambda path), import the
  primitive directly. Nobody has a copy.
* Everywhere else under `app/`, `create_async_engine(...)` must be passed a
  tenant-URL builder call (tenant) or the control-plane URL (control) —
  directly, or through a local assigned from one of them.
* Nothing re-implements the builder's body. The three handlers used to inline it
  and were held to *mirroring* it, which is a guard over a duplication rather
  than a guard against one; now the duplication itself is refused.
* A `feoh_`-prefixed database name never appears as a literal outside
  `app/config.py`, which owns the prefix.

What AST cannot prove is that a `db_name` came from a *resolved row* rather than
from the request — no static rule can. What it can prove is that every call site
goes through the one helper whose only caller-visible input is a `db_name`, which
turns reviewing a new site into a one-line question instead of an audit. Same
shape as `tests/test_pdf_render_offloaded.py` and
`tests/test_sqs_dispatch_nonblocking.py`: pure AST, no import, no database.
"""

from __future__ import annotations

import ast
import pathlib
import re

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"

#: The module that owns engine construction. Unrestricted here — this is where
#: `_make_tenant_url` and the control-plane engine live.
CHOKEPOINT = APP_DIR / "database.py"

#: The approved ways to turn a tenant DB name into a URL. `make_tenant_url` is
#: the primitive in `app/tenant_url.py`; `_make_tenant_url` is `app/database.py`'s
#: binding of it to `settings.database_url`, which ~70 in-app call sites spell.
#: Both end at the same one function — that is the point.
TENANT_URL_BUILDERS = frozenset({"_make_tenant_url", "make_tenant_url"})

#: The module that owns the construction, and the only place its body may appear.
URL_BUILDER_MODULE = APP_DIR / "tenant_url.py"

#: The approved way to read the control-plane URL on a path that cannot import
#: `app.config` — i.e. the Lambda handlers. Named here so the scan classifies
#: their control-plane engine the same way it classifies `settings.database_url`.
CONTROL_URL_FROM_ENV = "control_url_from_env"

#: The SQLAlchemy engine constructors. `create_engine` is the synchronous twin —
#: the app has none today, and naming it here means reaching for it is not a way
#: around this scan.
ENGINE_CONSTRUCTORS = frozenset({"create_async_engine", "create_engine"})

#: `app/config.py` owns the `feoh_` tenant-DB prefix.
CONFIG_MODULE = APP_DIR / "config.py"

#: A whole tenant-DB-name literal (`feoh_acme`) or the bare prefix. Anchored, so
#: prose that merely mentions `feoh_<slug>` does not match.
TENANT_DB_LITERAL = re.compile(r"^feoh_[a-z0-9_]*$")

#: Literals that start `feoh_` but name no database.
LITERAL_EXEMPTIONS = {
    # The public-API key brand (`feoh_live_<43 chars>`). Shares the product
    # prefix with the DB names by coincidence of branding.
    ("app/services/api_keys.py", "feoh_live"),
}

#: Guard the guard. If a refactor moves engine construction somewhere this scan
#: does not look, the count collapses and the suite says so instead of passing
#: on an empty set. Floored well below the 31 sites that exist today, so
#: consolidating a few call sites is not a spurious failure — only losing sight
#: of most of them is.
MIN_ENGINE_SITES = 25


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(BACKEND_DIR))


def _enclosing_scopes(tree: ast.AST) -> dict[int, ast.AST]:
    """Map id(node) → the nearest enclosing function (or the module)."""
    scopes: dict[int, ast.AST] = {}

    def _walk(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else scope
            scopes[id(child)] = inner
            _walk(child, inner)

    scopes[id(tree)] = tree
    _walk(tree, tree)
    return scopes


def _constructor_names(tree: ast.AST) -> set[str]:
    """The local names bound to an engine constructor in this module.

    `from sqlalchemy.ext.asyncio import create_async_engine as make_engine` renames
    the call, so matching only the canonical name would be a one-line bypass.
    """
    names = set(ENGINE_CONSTRUCTORS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                if alias.name in ENGINE_CONSTRUCTORS and alias.asname:
                    names.add(alias.asname)
    return names


def _constructs_an_engine(source: str) -> bool:
    """Cheap prefilter — skip the parse for a module that names no constructor."""
    return any(name in source for name in ENGINE_CONSTRUCTORS)


def _engine_sites(path: pathlib.Path) -> list[tuple[int, ast.expr, ast.AST]]:
    """Every engine construction in `path`, as (lineno, url expr, scope)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    scopes = _enclosing_scopes(tree)
    constructors = _constructor_names(tree)
    sites: list[tuple[int, ast.expr, ast.AST]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in constructors:
            continue
        url: ast.expr | None = node.args[0] if node.args else None
        for kw in node.keywords:
            # `**kwargs` (arg is None) hides the URL from this scan entirely, so
            # it counts as the URL and falls through to "unresolved".
            if kw.arg in ("url", None):
                url = kw.value
        assert url is not None, (
            f"{_rel(path)}:{node.lineno} engine constructed with no visible URL argument"
        )
        sites.append((node.lineno, url, scopes.get(id(node), tree)))

    return sites


def _resolve(expr: ast.expr, scope: ast.AST) -> list[ast.expr]:
    """Expand a local variable to the expressions assigned to it in `scope`.

    One hop, deliberately: every call site reads `tenant_url = _make_tenant_url(...)`
    a line or two above. Anything needing more indirection than that is exactly the
    kind of site a human should look at, and falls through as unresolved.
    """
    if not isinstance(expr, ast.Name):
        return [expr]

    assigned: list[ast.expr] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            # `url += slug` after a clean `url = _make_tenant_url(...)` would
            # otherwise resolve to the builder while the value it appends is
            # never looked at. Folding the operand in makes the site unresolved.
            targets = [node.target]
        else:
            continue
        if node.value is not None and any(
            isinstance(t, ast.Name) and t.id == expr.id for t in targets
        ):
            assigned.append(node.value)

    return assigned or [expr]


def _is_tenant_url_builder(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Call) and _call_name(expr) in TENANT_URL_BUILDERS


def _is_control_plane_url(expr: ast.expr) -> bool:
    """The control-plane URL, which names no tenant.

    Two spellings, because two kinds of module need it: `settings.database_url`
    in the app, and `control_url_from_env()` on the Lambda paths that cannot
    import `app.config` at all. Both are operator-supplied configuration and
    neither carries a tenant, so both are `control`. A bare
    `os.environ["DATABASE_URL"]` is deliberately NOT accepted — it is the shape
    the handlers used to spell inline, and routing it through the named helper is
    what keeps "which variable" a single fact.
    """
    if isinstance(expr, ast.Call) and _call_name(expr) == CONTROL_URL_FROM_ENV:
        return True
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == "database_url"
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "settings"
    )


def _classify(expr: ast.expr, scope: ast.AST) -> str:
    """`"tenant"` | `"control"` | `"unresolved"` for one engine's URL argument."""
    resolved = _resolve(expr, scope)
    if resolved and all(_is_tenant_url_builder(e) for e in resolved):
        return "tenant"
    if resolved and all(_is_control_plane_url(e) for e in resolved):
        return "control"
    return "unresolved"


def _interpolations(expr: ast.expr, scope: ast.AST) -> list[str]:
    """Interpolation nodes reachable from `expr` — f-string, `%`, `.format()`.

    Those three are the shapes that splice a caller-supplied value into a URL.
    Plain concatenation is deliberately absent: `tenant_db_url`'s own body is a
    concatenation, and it is the one place that construction lives.
    """
    found: list[str] = []
    for candidate in _resolve(expr, scope):
        for node in ast.walk(candidate):
            if isinstance(node, ast.JoinedStr):
                found.append("f-string")
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
                found.append("%-format")
            elif isinstance(node, ast.Call) and _call_name(node) == "format":
                found.append(".format()")
    return found


# --------------------------------------------------------------------------- #
# Rule 1 — every engine URL comes from `_make_tenant_url` or the control plane
# --------------------------------------------------------------------------- #


def test_every_engine_url_comes_from_make_tenant_url_or_the_control_plane():
    offenders: list[str] = []
    seen = 0

    for path in sorted(APP_DIR.rglob("*.py")):
        if path == CHOKEPOINT or not _constructs_an_engine(path.read_text()):
            continue
        rel = _rel(path)
        for lineno, url, scope in _engine_sites(path):
            seen += 1
            if _classify(url, scope) == "unresolved":
                offenders.append(f"{rel}:{lineno} engine URL = {ast.unparse(url)}")

    assert offenders == [], (
        "a database engine is being built from a URL that did not come through "
        f"one of {sorted(TENANT_URL_BUILDERS)} (tenant) or the control-plane URL "
        f"(`settings.database_url` / `{CONTROL_URL_FROM_ENV}()`) — a tenant DB "
        "reached that way has "
        "no resolved Organization row behind it, so the `get_tenant` JWT org-claim "
        "cross-check never runs: " + ", ".join(offenders)
    )
    assert seen >= MIN_ENGINE_SITES, (
        f"expected at least {MIN_ENGINE_SITES} engine construction sites outside "
        f"app/database.py, found {seen} — has construction moved somewhere this "
        "scan does not look?"
    )


def test_no_engine_constructor_is_referenced_outside_a_call():
    """A constructor handed around under another name disappears from the scan.

    Rule 1 classifies *calls*, so anything that stops the call being spelled with
    a constructor's name takes the site out of the enumeration entirely — not
    into the offender list, out of it:

        engine_factory = create_async_engine        # rebind
        engine_factory(f"{base}/feoh_{slug}")       # invisible to Rule 1

    An import alias is already followed (`_constructor_names`), but a rebind, a
    `staticmethod(create_async_engine)` wrapper, or passing the constructor as an
    argument are not — and cannot be, in general. So they are refused instead: a
    constructor name may only ever appear as the thing being called. Imports are
    `ast.alias`, not `ast.Name`, so they do not reach this check.
    """
    offenders: list[str] = []

    for path in sorted(APP_DIR.rglob("*.py")):
        source = path.read_text()
        if not _constructs_an_engine(source):
            continue
        tree = ast.parse(source, filename=str(path))
        constructors = _constructor_names(tree)
        called = {
            id(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node) in constructors
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Name) or node.id not in constructors:
                continue
            if isinstance(node.ctx, ast.Load) and id(node) in called:
                continue
            offenders.append(f"{_rel(path)}:{node.lineno} {node.id}")

    assert offenders == [], (
        "a SQLAlchemy engine constructor is bound to another name or passed as a "
        "value — every call through that name becomes invisible to this scan; "
        "call it directly: " + ", ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# Rule 2 — no engine URL is assembled by interpolation, anywhere
# --------------------------------------------------------------------------- #


def test_no_engine_url_is_built_by_interpolation():
    """Applies to every module under `app/`, the Lambda handlers included.

    `create_async_engine(f"{base}/feoh_{slug}")` is the specific shape that turns
    a request-supplied string into a tenant connection. It has no legitimate
    instance in this codebase: the chokepoint concatenates a `db_name` read off
    an Organization row, and everything else calls the chokepoint.
    """
    offenders: list[str] = []

    for path in sorted(APP_DIR.rglob("*.py")):
        if not _constructs_an_engine(path.read_text()):
            continue
        for lineno, url, scope in _engine_sites(path):
            for kind in _interpolations(url, scope):
                offenders.append(f"{_rel(path)}:{lineno} {kind} in {ast.unparse(url)}")

    assert offenders == [], (
        "a database URL is being assembled by string interpolation — build it with "
        "`make_tenant_url(base, db_name)` off a resolved Organization row instead: "
        + ", ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# Rule 3 — nobody re-implements the builder
# --------------------------------------------------------------------------- #


def _is_builder_body(expr: ast.expr) -> bool:
    """`<base>.rsplit("/", 1)[0] + "/" + <name>` — `make_tenant_url`'s body.

    Structural, not textual: base and name may be spelled however the module
    spells them, but the construction is the helper's.
    """
    if not (isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add)):
        return False

    left = expr.left
    if not (isinstance(left, ast.BinOp) and isinstance(left.op, ast.Add)):
        return False
    if not (isinstance(left.right, ast.Constant) and left.right.value == "/"):
        return False

    head = left.left
    return (
        isinstance(head, ast.Subscript)
        and isinstance(head.value, ast.Call)
        and _call_name(head.value) == "rsplit"
    )


def test_only_the_builder_module_builds_a_tenant_url():
    """The construction appears in exactly one file: `app/tenant_url.py`.

    This replaces the rule it grew out of. The three Lambda handlers used to
    inline `_make_tenant_url`'s body — they could not import `app.database`,
    which reaches `app.config` — and the guard held all three copies to
    *mirroring* the original. That is a guard over a duplication, and it accepts
    the duplication as permanent: every copy still had to be found, read and
    kept in step by hand, and the invariant "a tenant DB name becomes a URL in
    one place" was true only by convention.

    `app/tenant_url.py` is dependency-free, so the handlers import it like
    everyone else and the exemption is gone. What is left to assert is the
    stronger, simpler thing: the body exists once. A module that re-grows a copy
    fails here whether or not it is a Lambda handler, and whether or not the
    copy happens to be correct today.
    """
    offenders: list[str] = []

    # `alembic/` and `scripts/` too: alembic's env.py carried its own copy for
    # FEOH_MIGRATE_TENANT, outside the app-only scan, and that copy dropped the
    # control URL's query string (a TLS option) on every tenant migration.
    scanned = [
        *APP_DIR.rglob("*.py"),
        *(BACKEND_DIR / "alembic").glob("*.py"),
        *(BACKEND_DIR / "scripts").rglob("*.py"),
    ]
    for path in sorted(scanned):
        if path == URL_BUILDER_MODULE:
            continue
        source = path.read_text()
        if "rsplit" not in source:  # cheap prefilter
            continue
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.BinOp) and _is_builder_body(node):
                offenders.append(f"{_rel(path)}:{node.lineno} {ast.unparse(node)}")

    assert offenders == [], (
        "a module is re-implementing `app.tenant_url.make_tenant_url`'s body instead "
        "of calling it — import the helper (it has no dependencies, so it is safe "
        "even on a dotenv-free Lambda path): " + ", ".join(offenders)
    )


def test_the_builder_module_stays_dependency_free():
    """`app/tenant_url.py` imports nothing beyond `os`.

    That property is not decoration — it is the entire reason the three Lambda
    handlers can share the helper rather than copy it. One `from app.config
    import settings` here would put them back on their own copies, and the copies
    would be *justified*, so the rule above would have to be relaxed rather than
    enforced. `tests/test_tenant_url.py` proves the same thing dynamically, in a
    subprocess; this is the cheap static half that names the offending line.
    """
    tree = ast.parse(URL_BUILDER_MODULE.read_text(), filename=str(URL_BUILDER_MODULE))
    allowed = {"os", "__future__"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [
                f"{node.lineno} import {a.name}" for a in node.names if a.name not in allowed
            ]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in allowed:
                offenders.append(f"{node.lineno} from {node.module} import ...")

    assert offenders == [], (
        f"{_rel(URL_BUILDER_MODULE)} has grown an import — it must stay dependency-free "
        "so the AWS Lambda handlers can import it on a dotenv-free path: " + ", ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# Rule 4 — no tenant DB name is spelled out in code
# --------------------------------------------------------------------------- #


def _docstring_ids(tree: ast.AST) -> set[int]:
    """id()s of the Constant nodes that are docstrings — prose, not code."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        first = body[0] if body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def test_no_tenant_db_name_literal_outside_config():
    """`app/config.py` owns `tenant_db_prefix`; nowhere else names a tenant DB.

    A hardcoded `feoh_acme` is a query pointed at one specific tenant from code
    that has no business knowing which tenant it runs for — the failure mode
    "Don't hardcode tenant DB names" in the root CLAUDE.md names, and the one
    that survives every runtime guard because it never asks a question.
    """
    offenders: list[str] = []

    for path in sorted(APP_DIR.rglob("*.py")):
        source = path.read_text()
        if path == CONFIG_MODULE or "feoh_" not in source:
            continue
        rel = _rel(path)
        tree = ast.parse(source, filename=str(path))
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings or not TENANT_DB_LITERAL.match(node.value):
                continue
            if (rel, node.value) in LITERAL_EXEMPTIONS:
                continue
            offenders.append(f"{rel}:{node.lineno} {node.value!r}")

    assert offenders == [], (
        "a tenant database name is hardcoded — resolve it from the Organization "
        "row (`org.db_name`) instead: " + ", ".join(offenders)
    )
