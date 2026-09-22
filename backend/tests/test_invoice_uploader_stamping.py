"""Drift guard: every ``Invoice(...)`` site states who created the row.

``approval_chain.violates_segregation`` returns False — no breach — when
``Invoice.uploaded_by_id`` is NULL. That branch reads fail-open on a fraud
control, and it is only sound because of an invariant that lives nowhere in the
type system: **an invoice created on behalf of a signed-in employee always
records that employee**, so NULL provably means "nobody who could approve this
made it".

``services/csv_import`` violated that invariant silently for the whole life of
the CSV importer — the route had the user, the audit row used it, and the
``Invoice(...)`` constructor simply didn't pass it — so an AP manager could
import a payable at ``new`` and immediately approve it, while the same person
doing the same thing through ``POST /api/invoices`` got a 403.

Failing CLOSED on NULL instead would take out email intake, inbound PEPPOL and
the supplier portal, none of which have a control-plane user to record. So the
enforcement is here: a new construction site must pass ``uploaded_by_id``
explicitly, and passing a literal ``None`` must be declared below with the
reason there is no employee actor.

The recurring sweep was the fourth path on that list until migration 0096, and
it was the one that did not belong: nobody *ran* the sweep, but an employee
*authored* the template, and that is the person segregation has to exclude.
``recurring_invoice_templates.created_by_user_id`` now records the author and
``generate_one`` stamps ``actor_id or template.created_by_user_id``, so the
sweep's invoices name a creator. That is why ``recurring_invoices.py`` is
asserted below to pass a *value* rather than being excused here.

Migration 0097 widened the rule from that one column to a **set**:
``Invoice.segregation_actor_ids`` carries every other actor the payable's terms
are attributable to, because a recurring template can be shaped by its author
*and* by whoever later repointed its vendor and amount. That column is **not**
required at every construction site — unlike ``uploaded_by_id`` it has only two
possible sources, and every other path has no second actor to name, so
demanding the kwarg there would be noise with no safety in it. The two sources
are pinned below, as a declared list in ``_SEGREGATION_SET_WRITERS``: a
recurring template behind the invoice (``recurring_invoices.py``), and the
source payable behind an inter-company mirror (``intercompany.py``, whose
terms are the source's, copied verbatim — so everyone implicated in the source
is implicated in the mirror). At either site, omitting the kwarg *would*
silently drop a real implicated actor. A third site that starts writing the
set has to join the list, so the claim "two writers" cannot quietly go stale.
"""

from __future__ import annotations

import ast
import tokenize
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# Construction sites that legitimately have no control-plane ``User`` to record.
# Keyed by module path relative to `backend/`; the value is why.
_NO_EMPLOYEE_ACTOR: dict[str, str] = {
    "app/api/portal.py": (
        "supplier portal — the actor is a tenant-scoped VendorUser, who holds no "
        "employee JWT and can never reach an approval endpoint"
    ),
    "app/services/email_intake.py": ("inbound email — system ingestion, no human uploader"),
    "app/services/peppol_receive.py": ("inbound PEPPOL AS4 — system ingestion, no human uploader"),
}


def _invoice_binding_names(tree: ast.AST) -> set[str]:
    """Every local name in this module bound to the ``Invoice`` model.

    Deliberately matches on the imported NAME, not the module it came from:
    ``app/models/__init__.py`` re-exports ``Invoice``, so both
    ``from app.models.invoice import Invoice`` and ``from app.models import
    Invoice`` are available spellings, and a module-path filter silently skips
    the second. ``import ... as`` is followed too, so an alias cannot make the
    scan quietly stop finding a construction site.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "Invoice":
                    names.add(alias.asname or alias.name)
    return names


def _is_invoice_constructor(func: ast.expr, bindings: set[str]) -> bool:
    """``Invoice(...)`` / ``InvoiceModel(...)`` — or any ``<mod>.Invoice(...)``.

    The attribute form is matched on the attribute NAME alone, without resolving
    the module: `from app.models import invoice` then `invoice.Invoice(...)`
    binds nothing this scan can follow, and a scan that silently skipped such a
    file would be worse than one that occasionally flags an unrelated
    `something.Invoice(...)` — there is no such class in this codebase, and a
    false positive is loud while a false negative is exactly the hole being
    closed.
    """
    if isinstance(func, ast.Name):
        return func.id in bindings
    return isinstance(func, ast.Attribute) and func.attr == "Invoice"


def _construction_sites() -> list[tuple[str, int, ast.Call]]:
    sites: list[tuple[str, int, ast.Call]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        bindings = _invoice_binding_names(tree)
        rel = path.relative_to(APP_ROOT.parent).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_invoice_constructor(node.func, bindings):
                sites.append((rel, node.lineno, node))
    return sites


def _uploader_kwarg(call: ast.Call) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == "uploaded_by_id":
            return kw.value
    return None


def _is_literal_none(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value is None


def _token_level_invoice_calls() -> set[tuple[str, int]]:
    """Every ``Invoice(`` in the source, found by TOKENS rather than by AST.

    The AST scan has to decide what counts as the model — which import bound the
    name, whether an attribute access is the right one — and every such decision
    is a way to miss a site. That is what this second, dumber pass is for: it
    knows only "the name `Invoice`, immediately called", so it cannot be fooled
    by a spelling the resolver hasn't been taught.

    Tokens, not a regex over lines: `payment_runs.py` has seven error strings
    reading `"Invoice(s) already have…"`, and a text search cannot tell those
    from code. `V1Invoice` / `ProviderInvoice` / `LedgerInvoice` tokenize as
    their own NAME and never match; `class Invoice(Base)` is excluded by the
    preceding keyword.
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
            if tok.string != "Invoice" or tok.type != tokenize.NAME:
                continue
            following = toks[i + 1] if i + 1 < len(toks) else None
            if following is None or following.string != "(":
                continue
            if i and toks[i - 1].string == "class":
                continue
            hits.add((rel, tok.start[0]))
    return hits


def test_no_invoice_construction_is_invisible_to_the_ast_scan():
    """Cross-check the AST scan against a plain text search.

    `from app.models import Invoice` (the `__init__` re-export) is as valid a
    spelling as `from app.models.invoice import Invoice`, and an earlier version
    of this guard recognised only the second — a site written the other way was
    skipped entirely and every test here still passed. Any future divergence
    between what the source says and what the scan sees now fails here instead.
    """
    seen = {(rel, lineno) for rel, lineno, _ in _construction_sites()}
    invisible = sorted(_token_level_invoice_calls() - seen)
    assert not invisible, (
        "source constructs an Invoice at "
        + ", ".join(f"{rel}:{lineno}" for rel, lineno in invisible)
        + " but the AST scan below does not see it — the uploader guard would "
        "silently skip that site. Teach `_invoice_binding_names` / "
        "`_is_invoice_constructor` about the spelling used there."
    )


def test_the_scan_finds_the_known_construction_sites():
    """A guard that silently matches nothing passes forever. Pin the paths the
    scan is supposed to reach, so a broken matcher fails loudly."""
    found = {rel for rel, _, _ in _construction_sites()}
    assert {
        "app/api/invoices.py",
        "app/api/portal.py",
        "app/api/workflow.py",
        "app/services/csv_import.py",
        "app/services/email_intake.py",
        "app/services/intercompany.py",
        "app/services/peppol_receive.py",
        "app/services/recurring_invoices.py",
    } <= found, f"scan lost a known Invoice construction site; found {sorted(found)}"


def test_every_invoice_construction_site_states_its_uploader():
    """Omitting the kwarg is the failure mode that shipped: it reads as an
    oversight and behaves as an SoD exemption. Every site must answer."""
    missing = [
        f"{rel}:{lineno}"
        for rel, lineno, call in _construction_sites()
        if not _uploader_kwarg(call)
    ]
    assert not missing, (
        "Invoice(...) built without `uploaded_by_id` at "
        + ", ".join(missing)
        + " — pass the acting user (segregation of duties keys on this column), "
        "or pass None explicitly and declare the path in _NO_EMPLOYEE_ACTOR."
    )


def test_null_uploader_sites_are_declared():
    """A literal ``None`` is a deliberate SoD exemption for that path. It has to
    be argued for here, not left to a reader of the constructor to infer."""
    undeclared = [
        f"{rel}:{lineno}"
        for rel, lineno, call in _construction_sites()
        if (value := _uploader_kwarg(call)) is not None
        and _is_literal_none(value)
        and rel not in _NO_EMPLOYEE_ACTOR
    ]
    assert not undeclared, (
        "Invoice(...) hardcodes `uploaded_by_id=None` at "
        + ", ".join(undeclared)
        + " — that exempts the row from segregation of duties. Thread the acting "
        "user through, or add the path to _NO_EMPLOYEE_ACTOR with the reason."
    )


def test_no_stale_null_uploader_declarations():
    """An allowlist outlives what it excused. Drop an entry once the path stops
    hardcoding None, so the exemption can't be reused by a later edit."""
    declared_in_source = {
        rel
        for rel, _, call in _construction_sites()
        if (value := _uploader_kwarg(call)) is not None and _is_literal_none(value)
    }
    stale = sorted(set(_NO_EMPLOYEE_ACTOR) - declared_in_source)
    assert not stale, f"_NO_EMPLOYEE_ACTOR excuses paths that no longer need it: {stale}"


@pytest.mark.parametrize(
    "module",
    [
        "app/api/invoices.py",
        "app/api/workflow.py",
        "app/services/csv_import.py",
        "app/services/intercompany.py",
        "app/services/recurring_invoices.py",
    ],
)
def test_employee_paths_stamp_a_real_actor(module: str):
    """The five paths a signed-in employee can reach must pass a *value*, never
    a literal None — this is what makes the NULL branch of
    ``violates_segregation`` mean "no employee creator" rather than "unknown".

    ``recurring_invoices.py`` qualifies even though the background sweep passes
    ``actor_id=None``: the expression it stamps is ``actor_id or
    template.created_by_user_id``, so a sweep-generated invoice carries the
    employee who authored the template. It resolves to NULL only for a template
    created before migration 0096 added that column — deliberately never
    backfilled, because there is no honest author to recover and inventing one
    would manufacture either a refusal or an absolution."""
    sites = [(rel, ln, call) for rel, ln, call in _construction_sites() if rel == module]
    assert sites, f"no Invoice construction site found in {module}"
    for rel, lineno, call in sites:
        value = _uploader_kwarg(call)
        assert value is not None, f"{rel}:{lineno} does not pass uploaded_by_id"
        assert not _is_literal_none(value), (
            f"{rel}:{lineno} hardcodes uploaded_by_id=None on a path an employee "
            "reaches — the creator would be able to approve their own invoice"
        )


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


#: The construction sites that write ``Invoice.segregation_actor_ids``, and the
#: second actors each one has to name. Keyed by module path relative to
#: `backend/`.
_SEGREGATION_SET_WRITERS: dict[str, str] = {
    "app/services/recurring_invoices.py": (
        "a recurring template's author and every material editor — "
        "`implicated_actor_ids(template, uploader_id=...)`"
    ),
    "app/services/intercompany.py": (
        "the source payable's whole implicated set (its uploader and its own "
        "`segregation_actor_ids`), because the mirror's terms are the source's — "
        "`approval_chain.implicated_actors(invoice)`"
    ),
}


@pytest.mark.parametrize("module", sorted(_SEGREGATION_SET_WRITERS))
def test_each_declared_writer_stamps_the_implicated_actor_set(module: str):
    """Segregation of duties keys on the uploader **plus**
    ``Invoice.segregation_actor_ids`` (``approval_chain.violates_segregation``).
    At these sites the set is how a second person who shaped the payable is kept
    from approving it: a recurring template's material editor, or — across the
    entity boundary — whoever uploaded or shaped the source of an inter-company
    mirror. Drop the kwarg (or hardcode ``None``) and nothing fails loudly: the
    column reads NULL, the predicate reads "nobody beyond the uploader", and
    that person can approve again — exactly the silent regression the uploader
    guard above exists to prevent.
    """
    sites = [(rel, ln, call) for rel, ln, call in _construction_sites() if rel == module]
    assert sites, f"no Invoice construction site found in {module}"
    for rel, lineno, call in sites:
        value = _kwarg(call, "segregation_actor_ids")
        assert value is not None, (
            f"{rel}:{lineno} does not pass `segregation_actor_ids` — it must name "
            f"{_SEGREGATION_SET_WRITERS[module]}"
        )
        assert not _is_literal_none(value), (
            f"{rel}:{lineno} hardcodes `segregation_actor_ids=None`, which discards "
            f"{_SEGREGATION_SET_WRITERS[module]}"
        )


def test_no_undeclared_site_writes_the_implicated_actor_set():
    """The other direction: every site that passes a real value is declared
    above, and every declared writer still passes one. A new writer is a new
    claim about who shaped a payable, so it has to be argued for in
    ``_SEGREGATION_SET_WRITERS`` rather than appear silently — and a writer
    that stops writing must leave the list, so the docs' "two writers" cannot
    outlive the code."""
    writers = {
        rel
        for rel, _, call in _construction_sites()
        if (value := _kwarg(call, "segregation_actor_ids")) is not None
        and not _is_literal_none(value)
    }
    assert writers == set(_SEGREGATION_SET_WRITERS), (
        f"undeclared writers: {sorted(writers - set(_SEGREGATION_SET_WRITERS))}; "
        f"declared but not writing: {sorted(set(_SEGREGATION_SET_WRITERS) - writers)}"
    )
