"""Drift guard: every ``create_exception(...)`` site states who raised the flag.

``exception_lifecycle.segregation_refusal`` returns ``None`` — no refusal — when
``Exception.raised_by_user_id`` is NULL. That branch reads fail-open on a fraud
control, and it is sound only because of an invariant that lives nowhere in the
type system: **the column names the actor whose own act the exception exists to
have a second person look at, and nothing else**. So NULL provably means "no
such actor", not "we forgot".

Both failure directions are real, and this file guards both.

*Omission* is the one that shipped on the sibling control (``services/csv_import``
never passed ``uploaded_by_id``, so an importer could approve what they had just
imported — ``docs/decisions.md`` §131). It reads as an oversight and behaves as
an exemption.

*Over-stamping* is the one specific to this column, and it is worse. Nine of the
eleven raise sites are detectors or sweeps; several run from a door that DOES
have a user in scope who did not cause the finding — ``refresh_warnings`` is
re-run by whoever next PATCHes the invoice, and a Positive Pay return is
imported by an operator who did not alter the cheque. Stamping those actors
would bar a bystander from clearing a flag they had no hand in *and* absolve
whoever really caused it, which is the same manufactured refusal / absolution
§141 and §152 refused to produce by backfilling. So a literal ``None`` is not
merely tolerated here: at ten of the eleven sites it is the correct answer, and
it has to be argued for rather than inferred.

``create_exception`` is the ONE construction site for an ``Exception`` row
(``app/services/exception_service.py``; nothing else calls the model
constructor), which is what makes a scan of its callers exhaustive.
"""

from __future__ import annotations

import ast
import tokenize
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

CALLEE = "create_exception"

# Raise sites with no actor whose act this exception asks a second person to
# review. Keyed by module path relative to `backend/`; the value is why.
_NO_RAISING_ACTOR: dict[str, str] = {
    "app/services/invoice_warnings.py": (
        "every finding is a detector's, attributable to the invoice's own contents; "
        "`refresh_warnings` takes no actor and is reached from fifteen doors "
        "including the extraction worker, the QMS sweep and two agent resolvers"
    ),
    "app/services/extraction.py": (
        "the extraction worker (a pool thread or a Lambda) raised it — no request, "
        "no user; a provider/parse failure and a semantic-duplicate hit are nobody's act"
    ),
    "app/services/payment_erp_sync.py": (
        "the ERP sync-back leg failed under a sweep / post-commit hook; the ERP did "
        "not answer and nobody acted"
    ),
    "app/services/payment_reconciler.py": (
        "the reconciler sweep aged the payment out; no human is in scope"
    ),
    "app/services/payment_settlement_record.py": (
        "the rail settled for an amount we did not book — the discrepancy is the "
        "processor's, arriving on a webhook"
    ),
    "app/api/erp_webhook.py": (
        "inbound ERP webhook — the caller is the tenant's ERP, authenticated by HMAC, "
        "not a control-plane user"
    ),
    "app/api/payments.py": (
        "the sanctions/KYC screening verdict raised the hold, not the operator who "
        "dispatched the payment; two of the four call sites are unattended retry paths"
    ),
    "app/api/positive_pay.py": (
        "the BANK raised it — the return file says a cheque was altered, stale-dated "
        "or never issued; the operator importing the file is not the actor in question"
    ),
    "app/services/review.py": (
        "the rejecter IS in scope and is deliberately not stamped: the row notifies AP "
        "of a decision they already made and audited, not a second look at their act"
    ),
}

# The sites that must pass a real VALUE. Currently exactly one: approving a
# vendor bank-detail change is what re-points the money, and the flag exists to
# get a second pair of eyes on that act.
_MUST_STAMP_AN_ACTOR: dict[str, str] = {
    "app/api/vendors.py": (
        "the bank-change approver — the one raise site where the signed-in actor is "
        "genuinely what the flag asks someone else to check, and the axis the "
        "payable's own implicated-actor set cannot reach"
    ),
}


def _callee_binding_names(tree: ast.AST) -> set[str]:
    """Every local name in this module bound to ``create_exception``.

    Matches on the imported NAME rather than the module, and follows
    ``import ... as``, for the reason the sibling uploader guard gives: a
    module-path filter silently skips an equally valid spelling, and a guard
    with a spelling-shaped hole is worse than none. Most call sites here import
    it *inside* the function to dodge an import cycle, which `ast.walk` reaches
    just the same.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == CALLEE:
                    names.add(alias.asname or alias.name)
    return names


def _is_create_exception_call(func: ast.expr, bindings: set[str]) -> bool:
    if isinstance(func, ast.Name):
        return func.id in bindings
    return isinstance(func, ast.Attribute) and func.attr == CALLEE


def _call_sites() -> list[tuple[str, int, ast.Call]]:
    sites: list[tuple[str, int, ast.Call]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        bindings = _callee_binding_names(tree)
        rel = path.relative_to(APP_ROOT.parent).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_create_exception_call(node.func, bindings):
                sites.append((rel, node.lineno, node))
    return sites


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_literal_none(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value is None


def _token_level_calls() -> set[tuple[str, int]]:
    """Every ``create_exception(`` in the source, found by TOKENS not by AST.

    The AST scan has to decide which import bound the name; every such decision
    is a way to miss a site. This second, dumber pass knows only "the name,
    immediately called", so it cannot be fooled by a spelling the resolver has
    not been taught. Tokens rather than a regex because the name appears inside
    prose in several module docstrings, and a text search cannot tell those from
    code.
    """
    hits: set[tuple[str, int]] = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT.parent).as_posix()
        with path.open() as handle:
            toks = [
                t
                for t in tokenize.generate_tokens(handle.readline)
                if t.type in (tokenize.NAME, tokenize.OP)
            ]
        for i, tok in enumerate(toks):
            if tok.string != CALLEE or tok.type != tokenize.NAME:
                continue
            following = toks[i + 1] if i + 1 < len(toks) else None
            if following is None or following.string != "(":
                continue
            if i and toks[i - 1].string in ("def", "."):
                continue
            hits.add((rel, tok.start[0]))
    return hits


def test_the_model_constructor_has_exactly_one_caller():
    """The scan below is only exhaustive because ``create_exception`` is the one
    place an ``Exception`` row is built. A second constructor call would open a
    raise path this guard cannot see."""
    constructors: list[tuple[str, int]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT.parent).as_posix()
        tree = ast.parse(path.read_text())
        aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == "Exception" and (node.module or "").endswith("models.exception")
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in aliases
            ):
                constructors.append((rel, node.lineno))
    # Modules, not line numbers: a docstring edit must not fail this guard, but
    # a second construction site — in any module, including the chokepoint's own
    # — must.
    assert [rel for rel, _ in constructors] == ["app/services/exception_service.py"], (
        "an AP Exception row is constructed outside the create chokepoint (or more "
        f"than once inside it) at {constructors} — that raise path bypasses "
        "`raised_by_user_id`, the `exception.raised` audit row and the outbound "
        "webhook emit. Route it through `exception_service.create_exception`."
    )


def test_no_create_exception_call_is_invisible_to_the_ast_scan():
    seen = {(rel, lineno) for rel, lineno, _ in _call_sites()}
    invisible = sorted(_token_level_calls() - seen)
    assert not invisible, (
        "source calls create_exception at "
        + ", ".join(f"{rel}:{lineno}" for rel, lineno in invisible)
        + " but the AST scan does not see it — the raiser guard would silently "
        "skip that site. Teach `_callee_binding_names` / "
        "`_is_create_exception_call` about the spelling used there."
    )


def test_the_scan_finds_the_known_call_sites():
    """A guard that silently matches nothing passes forever."""
    found = {rel for rel, _, _ in _call_sites()}
    expected = set(_NO_RAISING_ACTOR) | set(_MUST_STAMP_AN_ACTOR)
    assert expected <= found, f"scan lost a known raise site; found {sorted(found)}"


def test_every_raise_site_states_its_raiser():
    """Omitting the kwarg reads as an oversight and behaves as an exemption from
    segregation of duties on the queue. Every site must answer."""
    missing = [
        f"{rel}:{lineno}"
        for rel, lineno, call in _call_sites()
        if _kwarg(call, "raised_by_user_id") is None
    ]
    assert not missing, (
        "create_exception(...) called without `raised_by_user_id` at "
        + ", ".join(missing)
        + " — pass the actor whose act this flag asks a second person to review "
        "(segregation of duties on the queue keys on it), or pass None "
        "explicitly and declare the path in _NO_RAISING_ACTOR."
    )


def test_null_raiser_sites_are_declared():
    undeclared = [
        f"{rel}:{lineno}"
        for rel, lineno, call in _call_sites()
        if (value := _kwarg(call, "raised_by_user_id")) is not None
        and _is_literal_none(value)
        and rel not in _NO_RAISING_ACTOR
    ]
    assert not undeclared, (
        "create_exception(...) hardcodes `raised_by_user_id=None` at "
        + ", ".join(undeclared)
        + " — that exempts the row from the raiser axis of segregation of duties. "
        "Thread the acting user through, or add the path to _NO_RAISING_ACTOR "
        "with the reason there is no such actor."
    )


def test_no_stale_null_raiser_declarations():
    """An allowlist outlives what it excused. Drop an entry once the path stops
    hardcoding None, so the exemption can't be reused by a later edit."""
    declared_in_source = {
        rel
        for rel, _, call in _call_sites()
        if (value := _kwarg(call, "raised_by_user_id")) is not None and _is_literal_none(value)
    }
    stale = sorted(set(_NO_RAISING_ACTOR) - declared_in_source)
    assert not stale, f"_NO_RAISING_ACTOR excuses paths that no longer need it: {stale}"


@pytest.mark.parametrize("module", sorted(_MUST_STAMP_AN_ACTOR))
def test_causal_paths_stamp_a_real_actor(module: str):
    """The sites where the signed-in actor IS what the flag asks about must pass
    a *value*, never a literal None — otherwise the actor who re-pointed a
    vendor's bank details can clear every flag that repointing raised and then
    execute the run, which is the open end of the BEC chain
    ``docs/authentication.md`` recorded."""
    sites = [(rel, ln, call) for rel, ln, call in _call_sites() if rel == module]
    assert sites, f"no create_exception call site found in {module}"
    for rel, lineno, call in sites:
        value = _kwarg(call, "raised_by_user_id")
        assert value is not None, f"{rel}:{lineno} does not pass raised_by_user_id"
        assert not _is_literal_none(value), (
            f"{rel}:{lineno} hardcodes raised_by_user_id=None on a path where the "
            f"actor IS the subject of the flag ({_MUST_STAMP_AN_ACTOR[module]})"
        )
