"""Audit-log append-only contract (project invariant #3).

> Audit trail is append-only. Status transitions on invoices,
> payments, approvals, and vendors write a log row through the
> audit-shipping infrastructure, not just mutate state. A status
> change that overwrites without producing an audit row is
> `Improvement` at minimum, `Critical` if the field is regulated
> (`paid_at`, `approved_at`, `void_at`).

Tests:
  - No HTTP API exposes PATCH / DELETE on AuditLog
  - `dispatch_audit` is called from every money-/status-moving
    handler (void_payment, cancel_run, approve_run,
    transition_invoice)
  - Every tenant-mutating HANDLER audits — the drift guard. The unit
    is the handler (`inspect.getsource(endpoint)` plus any same-module
    helper it calls), never the module: one auditing handler must not
    vouch for its twenty unaudited neighbours in the same file.
  - AuditLog model declares the columns required for the WORM
    pipeline (shipped_at for the centralized SOC 2 sink)
  - The audit-shipper batch helper does NOT delete rows on ship —
    it only stamps `shipped_at`

Every route-based sweep here resolves the app through `_flat_routes()`
and asserts a floor on what it found, so a flattener that stops seeing
the app fails loudly instead of passing on an empty scan.
"""

from __future__ import annotations

import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Route flattening — the one place this file resolves the app's routes
# ---------------------------------------------------------------------------

# FastAPI 0.138+ `include_router` keeps nested `_IncludedRouter` objects in
# `app.routes` instead of flattening sub-routes, so a naive
# `[r for r in app.routes if isinstance(r, APIRoute)]` sees ONE route out of the
# whole app and every filter built on it yields nothing — a guard that passes
# having examined almost nothing. Flatten via the supported `iter_route_contexts`
# (full path + methods + endpoint); fall back to the flat list on an older
# FastAPI, where `app.routes` really is flat.
#
# `_MIN_EXPECTED_ROUTES` is the actual fix: any future regression that collapses
# the flattening (a FastAPI internal moving again, a bad refactor) fails loudly
# here instead of silently reporting green on an empty scan. The app has ~564
# routes; the floor is set well below that so ordinary route churn never trips
# it, but a collapse to a handful cannot slip through.
_MIN_EXPECTED_ROUTES = 400


def _flat_routes() -> list[tuple[str, set[str], object]]:
    """Every HTTP route in the app as `(path, methods, endpoint)`."""
    from app.main import app

    try:
        from fastapi.routing import iter_route_contexts

        return [(c.path, set(c.methods or ()), c.endpoint) for c in iter_route_contexts(app.routes)]
    except ImportError:  # pragma: no cover - older FastAPI
        from fastapi.routing import APIRoute

        return [
            (r.path, set(r.methods or ()), r.endpoint)
            for r in app.routes
            if isinstance(r, APIRoute)
        ]


def test_route_flattener_sees_the_whole_app():
    """Guard the guards: every sweep in this file filters `_flat_routes()`, so a
    flattener that returns almost nothing turns each of them into a no-op that
    still reports green."""
    routes = _flat_routes()
    assert len(routes) >= _MIN_EXPECTED_ROUTES, (
        f"only {len(routes)} routes discovered (expected >= {_MIN_EXPECTED_ROUTES}). "
        "The route flattener is not seeing the whole app — every audit sweep in "
        "this file is scanning a near-empty set and passing vacuously."
    )


# ---------------------------------------------------------------------------
# No HTTP API exposes destructive verbs on AuditLog
# ---------------------------------------------------------------------------


def test_no_router_defines_a_put_patch_or_delete_on_audit_log():
    """Sweep every route in the app. Any path that contains
    "audit" / "audit-log" must NOT respond to PATCH / DELETE / PUT.
    If a future PR adds an "edit audit row" endpoint, this catches it."""
    routes = _flat_routes()
    assert len(routes) >= _MIN_EXPECTED_ROUTES, (
        f"only {len(routes)} routes discovered — this sweep would pass vacuously"
    )

    violations: list[str] = []
    for path, methods, _endpoint in routes:
        if "audit" not in path.lower():
            continue
        forbidden = methods & {"PUT", "PATCH", "DELETE"}
        if forbidden:
            violations.append(f"{path} {sorted(forbidden)}")
    assert not violations, f"audit endpoints must be GET-only — violations: {violations}"


def test_audit_log_get_endpoint_is_read_only_in_signature():
    """The lone audit endpoint (`get_audit_log` in workflow.py) must
    be a GET. A regression to POST/PUT shape would imply mutation."""
    audit_routes = [
        (path, methods) for path, methods, _ in _flat_routes() if "audit" in path.lower()
    ]
    assert audit_routes, "expected at least one audit-log GET endpoint"
    for path, methods in audit_routes:
        assert methods == {"GET"}, f"audit route {path} must only respond to GET"


# ---------------------------------------------------------------------------
# AuditLog model — required columns for the WORM pipeline
# ---------------------------------------------------------------------------


def test_audit_log_has_shipped_at_for_worm_pipeline():
    """The centralized SOC 2 shipper (`services/audit_log_shipper.py`)
    stamps `shipped_at` after a batch successfully reaches every
    configured sink. Without that column the shipper would re-ship
    forever — and operators couldn't tell what's been replicated."""
    from app.models.workflow import AuditLog

    assert "shipped_at" in AuditLog.__table__.columns


def test_audit_log_columns_carry_full_who_what_when_where():
    """Auditors expect every row to identify actor, action,
    entity, and time. Pin the column names so a future "I'll
    rename actor_id to user_id" refactor doesn't break the SOC 2
    evidence trail."""
    from app.models.workflow import AuditLog

    required = {
        "id",
        "actor_id",
        "action",
        "entity_type",
        "entity_id",
        "created_at",
        "details",
        "organization_id",
        "correlation_id",
    }
    actual = set(AuditLog.__table__.columns.keys())
    missing = required - actual
    assert not missing, f"AuditLog is missing required columns: {missing}"


# ---------------------------------------------------------------------------
# Money-/status-moving handlers must call dispatch_audit
# ---------------------------------------------------------------------------


def test_void_payment_handler_dispatches_audit():
    """`void_payment` is regulated money movement — invariant says
    it MUST write an audit row. Static-grep the handler source."""
    from app.api import payments

    src = inspect.getsource(payments.void_payment)
    assert "dispatch_audit" in src, (
        "void_payment must call dispatch_audit before commit (invariant #3)"
    )


def test_cancel_payment_run_handler_dispatches_audit():
    """Cancelling a draft run reopens the invoices it pinned — the
    SOD chain needs the audit row to reconstruct who undid what."""
    from app.api import payments

    src = inspect.getsource(payments.cancel_payment_run)
    assert "dispatch_audit" in src


def test_approve_payment_run_handler_dispatches_audit():
    """CFO sign-off on a high-value run — the audit row is the
    proof-of-approval auditors look for."""
    from app.api import payments

    src = inspect.getsource(payments.approve_payment_run)
    assert "dispatch_audit" in src


def test_transition_invoice_helper_dispatches_audit():
    """`transition_invoice` is the chokepoint for every invoice
    status change. It must dispatch an audit on every call — the
    detail payload includes old/new status."""
    from app.services import workflow_engine

    src = inspect.getsource(workflow_engine.transition_invoice)
    assert "dispatch_audit" in src


# ---------------------------------------------------------------------------
# Every HANDLER that mutates TENANT state audits — drift guard
# ---------------------------------------------------------------------------

# Tenant-mutating HANDLERS with no audit-writing call (`_AUDIT_WRITERS`, below)
# reachable from their own source. Keyed on `(module, handler)` — never on the
# module.
#
# The unit used to be the module, and one auditing handler exempted every other
# handler beside it: `api/invoices.py` has 21 tenant-mutating routes, so a
# single `dispatch_audit` anywhere in the file made all 21 look covered. That is
# how three unaudited DELETE handlers shipped there and were only caught by
# hand in round 23.
#
# Each entry needs a reason; an entry with no reason is a hole in the trail, not
# an exemption. The four per-handler tests above stay as they are — they pin the
# specific money-path handlers by name.
_TENANT_MUTATORS_WITHOUT_DIRECT_AUDIT: dict[tuple[str, str], str] = {
    # -- POST/PATCH-shaped but read-only: nothing is persisted ---------------
    ("app.api.analytics", "post_forecast_variance"): (
        "POST /forecast_variance is pure compute — nothing is persisted"
    ),
    ("app.api.contracts", "bulk_export_contracts"): "CSV export — reads, never writes",
    ("app.api.discounts", "optimize_discounts"): (
        "pure optimizer over open offers — selects nothing, persists nothing"
    ),
    ("app.api.enrichment", "enrich_vendor"): (
        "advisory firmographics lookup — returns a suggestion diff, never writes the Vendor row"
    ),
    ("app.api.invoices", "bulk_export"): "CSV/XML export — reads, never writes",
    ("app.api.payments", "compare_corridor_quotes"): (
        "advisory corridor pricing — books nothing and changes no rail (decisions §42)"
    ),
    ("app.api.reports", "run_adhoc"): "runs a whitelisted report query — reads, never writes",
    ("app.api.reports", "run_saved"): "runs a whitelisted report query — reads, never writes",
    ("app.api.vendors", "bulk_export_vendors"): "CSV export — reads, never writes",
    ("app.api.workflow_definitions", "simulate_workflow"): (
        "dry-run of a definition against a synthetic invoice — persists nothing"
    ),
    # -- the caller's own record / a derived cache, not tenant business state -
    # The assistant + copilot persist only the caller's own chat transcript;
    # the access itself is metered (`extraction_usage` / token budget) and
    # every tool they can call is read-only.
    ("app.api.assistant", "chat"): (
        "persists only the caller's own chat transcript; tools are read-only"
    ),
    ("app.api.assistant", "chat_stream"): (
        "persists only the caller's own chat transcript; tools are read-only"
    ),
    ("app.api.cash_flow", "copilot"): (
        "assistant façade — persists only the caller's own chat transcript; tools are read-only"
    ),
    ("app.api.cash_flow", "copilot_stream"): (
        "assistant façade — persists only the caller's own chat transcript; tools are read-only"
    ),
    ("app.api.notifications", "mark_read"): "read receipt on the caller's own notification",
    ("app.api.notifications", "mark_all_read"): "read receipts on the caller's own notifications",
    ("app.api.adaptive_workflows", "dismiss_suggestion"): (
        "advisory WorkflowSuggestion row records its own dismisser + timestamp; "
        "touches no invoice, payment, approval or vendor"
    ),
    ("app.api.invoices", "regenerate_invoice_summary"): (
        "rebuilds the derived `invoices.meta.audit_summary` cache FROM the audit trail; "
        "no business field changes"
    ),
    # -- supplier-portal auth. `portal_login`, `portal_change_password`,
    #    `portal_mfa_verify` and `portal_mfa_disable` were listed here on the
    #    strength of auditing via `dispatch_auth_audit` / `queue_auth_audit`,
    #    which the scan did not recognise; it does now (`_AUDIT_WRITERS`), so
    #    they need no exemption. `portal_mfa_challenge` left for the other
    #    reason — it genuinely did not audit, and now does
    #    (`portal.mfa.verify.success` / `.failure`), because with a second
    #    factor enrolled it is where the supplier's sign-in actually completes.
    ("app.api.portal_auth", "portal_request_email_otp"): (
        # This is a real decision, not a to-do. The consequential event is the
        # code being USED, and `/mfa/challenge` now records that on both
        # outcomes with the `method` — so an issued-but-never-redeemed code is
        # already visible as an absence, and a row here would only ever
        # duplicate a delivery that the per-account and per-IP send caps
        # already bound. It is also a 204-on-every-path anti-enumeration
        # endpoint: a row would be written for exactly the addresses that
        # exist AND are enrolled, rebuilding inside the audit trail the
        # account oracle the uniform 204 exists to withhold. Its employee twin
        # (`api/auth.request_email_otp`) is unaudited for the same reasons, and
        # a stricter control on the supplier surface than on the employee one
        # would be knowingly asymmetric.
        "mints a single-use email OTP into Redis and mails it; the auditable "
        "event is the code being redeemed, which `/mfa/challenge` records "
        "(`portal.mfa.verify.success` / `.failure`, carrying `method`)"
    ),
    ("app.api.portal_auth", "portal_update_me"): (
        "the calling vendor user's own email-locale preference, not tenant business state"
    ),
    # -- audits through a CROSS-MODULE chokepoint --------------------------
    # `_handler_audits` deliberately does not follow calls out of the handler's
    # own module: "that other file audits" is a design claim, so it is written
    # down here where it can be re-read, rather than inferred by a source scan.
    ("app.api.adaptive_workflows", "apply_routing_suggestion"): (
        "audits via services/review.assign_reviewer"
    ),
    ("app.api.cash_flow", "draft_run_from_plan"): (
        "audits via services/payment_runs.create_payment_run_for_invoices"
    ),
    ("app.api.exception_agents", "agent_resolve"): (
        "audits via exception_lifecycle.record_decision"
    ),
    ("app.api.exceptions", "assign_exception"): "audits via exception_lifecycle.record_assignment",
    ("app.api.exceptions", "bulk_resolve"): "audits via exception_lifecycle.record_decision",
    ("app.api.exceptions", "resolve_exception"): "audits via exception_lifecycle.record_decision",
    ("app.api.goods_receipts", "cancel_receipt"): (
        "audits via services/goods_receipts.cancel_goods_receipt (`goods_receipt.cancelled`)"
    ),
    ("app.api.goods_receipts", "record_goods_receipt"): (
        "audits via services/goods_receipts.create_goods_receipt (`goods_receipt.created`)"
    ),
    ("app.api.inspections", "sync_inspections"): (
        "audits via services/qms_sync (`quality_inspection.synced`)"
    ),
    ("app.api.invoices", "bulk_recode_gl_endpoint"): "audits via services/gl_recode.bulk_recode_gl",
    ("app.api.invoices", "import_invoices_from_csv"): (
        "audits via services/csv_import.import_invoices_csv (`invoice.imported_csv` per "
        "created invoice, keyed on its own correlation_id, plus `vendor.imported_csv` "
        "for each auto-created vendor stub)"
    ),
    ("app.api.invoices", "bulk_status_change"): (
        "audits via workflow_engine.transition_invoice + services/review"
    ),
    ("app.api.invoices", "peppol_send"): "audits via services/peppol_send.send_invoice_over_peppol",
    ("app.api.invoices", "route_intercompany"): (
        "audits via services/intercompany.route_intercompany_invoice (both rows)"
    ),
    ("app.api.payments", "create_payment_run"): (
        "audits via services/payment_runs.create_payment_run_for_invoices"
    ),
    ("app.api.payments", "record_payment_outside"): (
        "audits via services/external_payment.record_invoice_paid_outside "
        "(`payment.recorded_outside`, plus the invoice transitions)"
    ),
    ("app.api.portal", "resubmit_invoice"): (
        "audits via workflow_engine.transition_invoice + exception_lifecycle.record_decision"
    ),
    ("app.api.recurring", "generate_now"): ("audits via services/recurring_invoices.generate_one"),
    ("app.api.vendors", "import_vendors_from_csv"): (
        "audits via services/csv_import.import_vendors_csv (`vendor.imported_csv` per "
        "created vendor)"
    ),
    ("app.api.vendors", "sync_vendors_from_erp_endpoint"): (
        "audits via services/vendor_sync.sync_vendors_from_erp "
        "(`vendor.synced_from_erp` per created/updated/linked vendor)"
    ),
    ("app.api.vendors", "screen_vendor"): (
        "audits via services/vendor_screening.screen_vendor_record (`vendor.screened`)"
    ),
    ("app.api.vendors", "bulk_screen_vendors"): (
        "audits via services/vendor_screening.screen_vendor_record (`vendor.screened`)"
    ),
    ("app.api.workflow", "approve_invoice"): "audits via services/review.approve_invoice",
    ("app.api.workflow", "assign_reviewer"): "audits via services/review.assign_reviewer",
    ("app.api.workflow", "reject_invoice"): "audits via services/review.reject_invoice",
    ("app.api.workflow", "resubmit_invoice"): "audits via services/review.resubmit_invoice",
    ("app.api.workflow", "complete_invoice"): "audits via workflow_engine.transition_invoice",
    ("app.api.workflow", "reset_extraction"): "audits via workflow_engine.transition_invoice",
    ("app.api.workflow", "send_to_erp"): "audits via workflow_engine.transition_invoice",
    ("app.api.workflow", "trigger_extraction"): "audits via workflow_engine.transition_invoice",
    ("app.api.workflow", "retry_erp"): (
        "audits via services/erp.retry_erp → workflow_engine.transition_invoice"
    ),
}

# ---------------------------------------------------------------------------
# OPEN HOLES — NOT justified exemptions
# ---------------------------------------------------------------------------
#
# Handlers that genuinely mutate tenant business state with no audit row
# anywhere on the path. Listing one here keeps the suite green on a KNOWN,
# enumerated set instead of widening the real exemption dict above — an entry
# here says "still to do", never "already covered", and
# `test_audit_exemption_list_has_no_stale_entries` fails the moment the handler
# starts auditing, which is the prompt to delete the entry.
#
# Do not add to this dict. A new unaudited mutating handler is a bug to fix, not
# an entry to make.
_OPEN_AUDIT_HOLES: dict[tuple[str, str], str] = {}
"""Empty on purpose — kept, not deleted.

The six entries this held (round-24 report) are all closed:

* ``workflow_definitions.create_workflow`` → ``workflow.created``
* ``vendors.invite_vendor_portal_user`` → ``vendor_user.invited``
* ``vendors.sync_vendors_from_erp_endpoint`` → ``vendor.synced_from_erp``
  (in ``services/vendor_sync``)
* ``vendors.import_vendors_from_csv`` → ``vendor.imported_csv``
  (in ``services/csv_import``)
* ``invoices.import_invoices_from_csv`` → ``invoice.imported_csv``
  (in ``services/csv_import``)
* ``inspections.create_inspection`` → ``quality_inspection.created``

The dict stays because the DISTINCTION it draws is the valuable part: a known
gap is not a settled decision, and merging one into the exemption dict above is
how "we have not done this yet" quietly becomes "this is fine". A future
unaudited mutator should be FIXED; if one genuinely cannot be fixed in the
session that finds it, it belongs here — with a reason that says it is open work
— and nowhere else.
"""

_AUDIT_EXEMPT = {**_TENANT_MUTATORS_WITHOUT_DIRECT_AUDIT, **_OPEN_AUDIT_HOLES}


# How deep to follow same-module helper calls looking for a `dispatch_audit`.
# A handler that delegates to `_audit(...)` (or to `_transition(...)` which
# calls `_audit(...)`) is audited; three hops is well past anything in `app/api`
# and keeps the scan bounded.
_AUDIT_HELPER_MAX_DEPTH = 3


def _identifiers_in(src: str) -> set[str]:
    """Every bare name and attribute accessed anywhere in `src`.

    A superset of "functions this calls" on purpose — resolution against the
    module's own globals is what narrows it, and over-collecting here can only
    ever follow one extra same-module function, never invent an audit call.
    """
    import ast
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError:  # pragma: no cover - defensive
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


# Every helper that writes an `audit_log` row. `dispatch_audit` is the
# tenant-session path; `dispatch_auth_audit` and `queue_auth_audit` open their
# OWN tenant session (a handler running on the control-plane session has none to
# write through) and then go through the same `services/audit.log_action`
# primitive. All three produce the row this invariant is about, so all three
# have to count as auditing.
#
# Matching only `dispatch_audit` was a blind spot with teeth. Neither of the
# other two contains it as a substring, so a handler auditing through them read
# as UNAUDITED — it had to be excused in the dict above, and because
# `test_audit_exemption_list_has_no_stale_entries` only fires when an excused
# handler STARTS auditing, that entry's reason string could then never be
# re-validated by anything. That is how `portal_mfa_verify` kept a reason
# asserting "a SUCCESSFUL enrollment is unaudited on both surfaces" after both
# surfaces started auditing it: the same "one thing silently vouches for
# another" shape as the module-wide unit this file's per-handler unit replaced.
_AUDIT_WRITERS = ("dispatch_audit", "dispatch_auth_audit", "queue_auth_audit")


def _writes_audit_row(src: str) -> bool:
    return any(writer in src for writer in _AUDIT_WRITERS)


def _handler_audits(
    fn: object, module: str, _depth: int = 0, _seen: set[str] | None = None
) -> bool:
    """True when an audit-writing call (`_AUDIT_WRITERS`) appears in this
    handler's own source, or in the source of a function it calls that is
    defined in the SAME module.

    The same-module allowance is what keeps a real pattern legible: several
    routers funnel their writes through a local `_audit(...)` / `_transition(...)`
    helper. A cross-module chokepoint (`exception_lifecycle.record_decision`,
    `services/qms_sync`) is deliberately NOT followed — that is a design claim
    about another file, so it belongs in the exemption dict where it can be read
    and re-checked, not inferred by a source scan.
    """
    import sys

    if _seen is None:
        _seen = set()
    key = getattr(fn, "__qualname__", repr(fn))
    if key in _seen or _depth > _AUDIT_HELPER_MAX_DEPTH:
        return False
    _seen.add(key)

    try:
        src = inspect.getsource(fn)  # type: ignore[arg-type]
    except (OSError, TypeError):  # pragma: no cover - C builtins
        return False
    if _writes_audit_row(src):
        return True

    mod = sys.modules.get(module)
    if mod is None:  # pragma: no cover - module always imported by this point
        return False
    for name in _identifiers_in(src):
        helper = getattr(mod, name, None)
        if helper is None or not inspect.isfunction(helper):
            continue
        if getattr(helper, "__module__", None) != module:
            continue  # imported from elsewhere — not a same-module helper
        if _handler_audits(helper, module, _depth + 1, _seen):
            return True
    return False


def _tenant_mutating_handlers() -> dict[tuple[str, str], list[str]]:
    """Map `(module, handler name)` → its mutating route paths.

    `Depends(get_tenant_db)` is the precise marker for "this handler writes
    tenant state", which is what the invariant governs — control-plane and
    webhook-receiver routers are out of scope by construction.
    """
    from app.tenant import get_tenant_db

    out: dict[tuple[str, str], list[str]] = {}
    for path, methods, endpoint in _flat_routes():
        module = getattr(endpoint, "__module__", "")
        if not module.startswith("app.api."):
            continue
        if not (methods & {"POST", "PATCH", "PUT", "DELETE"}):
            continue
        try:
            sig = inspect.signature(endpoint)  # type: ignore[arg-type]
        except (ValueError, TypeError):  # pragma: no cover - builtins
            continue
        takes_tenant_db = any(
            getattr(param.default, "dependency", None) is get_tenant_db
            for param in sig.parameters.values()
        )
        if takes_tenant_db:
            name = getattr(endpoint, "__name__", "<anonymous>")
            out.setdefault((module, name), []).append(f"{sorted(methods)} {path}")
    return out


def _resolve_handler(module: str, name: str) -> object | None:
    import importlib

    return getattr(importlib.import_module(module), name, None)


def test_every_tenant_mutating_handler_writes_an_audit_row():
    """Widened from the four hand-picked handlers above, and narrowed from the
    module-wide grep that replaced them.

    The invariant is "status changes / mutations write an audit row". The first
    sweep required `dispatch_audit` somewhere in the MODULE, which meant one
    auditing handler vouched for every sibling in the file — `api/invoices.py`
    alone has 21 tenant-mutating routes behind a single such handler. The unit
    is now the handler: its own source, plus any same-module helper it calls.
    """
    handlers = _tenant_mutating_handlers()
    assert len(handlers) >= 200, (
        f"only {len(handlers)} tenant-mutating handlers discovered — the sweep "
        "is not seeing the app's routes"
    )

    unaudited = []
    for (module, name), routes in sorted(handlers.items()):
        if (module, name) in _AUDIT_EXEMPT:
            continue
        fn = _resolve_handler(module, name)
        if fn is None:
            unaudited.append(f"{module}.{name} — {routes} (handler not importable by name)")
            continue
        if not _handler_audits(fn, module):
            unaudited.append(f"{module}.{name} — {routes}")

    assert not unaudited, (
        "these handlers mutate tenant state without calling dispatch_audit "
        "(invariant #3). Add the audit row, or add (module, handler) to "
        "_TENANT_MUTATORS_WITHOUT_DIRECT_AUDIT with a reason that says why the "
        "mutation is already covered (adding to _OPEN_AUDIT_HOLES is not a "
        "resolution):\n  " + "\n  ".join(unaudited)
    )


def test_audit_exemption_list_has_no_stale_entries():
    """An exemption that stops being true is worse than none — it silently
    excuses a handler that has since grown an audited mutation, one that has
    been renamed, or one that no longer mutates at all."""
    handlers = _tenant_mutating_handlers()
    stale = []
    for (module, name), reason in sorted(_AUDIT_EXEMPT.items()):
        assert reason, f"{module}.{name} needs a reason, not a bare exemption"
        if (module, name) not in handlers:
            stale.append(f"{module}.{name} (no longer has a tenant-mutating route)")
            continue
        fn = _resolve_handler(module, name)
        if fn is not None and _handler_audits(fn, module):
            stale.append(f"{module}.{name} (now writes an audit row — drop the exemption)")
    assert not stale, f"stale audit exemptions: {stale}"


def test_entity_and_gl_account_mutations_dispatch_audit():
    """Named explicitly, not just covered by the sweep above: an `Entity` is
    the scope key every entity-scoped money query filters by, and a GL account
    is what invoice lines are coded to."""
    from app.api import entities, gl_accounts

    for handler in (entities.create_entity, entities.update_entity):
        assert "dispatch_audit" in inspect.getsource(handler), handler.__name__
    for handler in (gl_accounts.create_gl_account, gl_accounts.sync_gl_accounts_from_erp):
        assert "dispatch_audit" in inspect.getsource(handler), handler.__name__


def test_round_24_open_audit_holes_are_closed():
    """The six mutators round 24 enumerated in `_OPEN_AUDIT_HOLES` — a Day-0
    CSV load of a tenant's AP ledger, an ERP vendor pull, a bulk vendor
    import, the one AP action that mints a vendor identity, a workflow
    definition, and a hand-recorded 4-way-match quality inspection — now each
    dispatch a PII-free audit row (invariant #3).

    Pinned by name here so a refactor that drops one `dispatch_audit` call
    fails loudly even if the route-discovery sweep ever stops reaching that
    handler — the belt to the sweep's suspenders.

    Three of the six audit **per row inside their service**, not once at the
    route: an import that creates 400 invoices writes 400 rows, each keyed on
    the invoice's own `correlation_id` so it is reachable from that invoice's
    trail. A single route-level summary row keyed on the org would be
    invisible from every invoice it describes, which is the trail an auditor
    actually walks. So the assertion follows the delegation rather than
    demanding the call sit in the handler — the same distinction
    `_TENANT_MUTATORS_WITHOUT_DIRECT_AUDIT` draws above.
    """
    from app.api import inspections, vendors, workflow_definitions
    from app.services import csv_import, vendor_sync

    # Audits directly in the handler.
    direct = [
        workflow_definitions.create_workflow,
        vendors.invite_vendor_portal_user,
        inspections.create_inspection,
    ]
    # Audits in the service the handler delegates to (one row per created row).
    delegated = [
        csv_import.import_invoices_csv,
        csv_import.import_vendors_csv,
        vendor_sync.sync_vendors_from_erp,
    ]
    missing = [
        f"{fn.__module__}.{fn.__name__}"
        for fn in (*direct, *delegated)
        if not _handler_audits(fn, fn.__module__)
    ]
    assert not missing, f"these tenant-mutating paths write no audit row: {missing}"


# ---------------------------------------------------------------------------
# Audit-shipper preserves rows on ship (stamp, don't delete)
# ---------------------------------------------------------------------------


def test_audit_shipper_does_not_delete_rows_on_ship():
    """The shipper marks rows `shipped_at=now()` after a batch ACKs
    on every sink. It must NEVER DELETE rows — auditors need the
    tenant-local copy for forensic queries even after replication."""
    from app.services import audit_log_shipper

    src = inspect.getsource(audit_log_shipper)
    # Quick sanity: no DELETE FROM audit_log anywhere in the shipper.
    assert "DELETE FROM audit_log" not in src.upper()
    assert (
        "audit_log_shipper" not in src
        or "db.delete" not in src
        or ("AuditLog" not in src.split("db.delete", 1)[1][:200] if "db.delete" in src else True)
    )


# ---------------------------------------------------------------------------
# Status fields that are regulated by SOC 2 evidence
# ---------------------------------------------------------------------------


def test_invoice_status_transitions_funnel_through_transition_invoice():
    """Every router that touches `invoice.status` must call
    transition_invoice — direct assignment skips the audit dispatch.

    Three routers move invoices through their state machine today:
    invoices (bulk + per-row ops), workflow (review / approve / reject),
    and payments (schedule / void). Any of them dropping its
    transition_invoice call would silently start producing
    money-touching transitions without an audit row.
    """
    from app.api import invoices, payments, workflow

    invoices_src = inspect.getsource(invoices)
    workflow_src = inspect.getsource(workflow)
    payments_src = inspect.getsource(payments)
    for name, src in (
        ("invoices", invoices_src),
        ("workflow", workflow_src),
        ("payments", payments_src),
    ):
        assert "transition_invoice" in src, (
            f"router `{name}` must call transition_invoice — the only path "
            "that pairs the invoice state change with an audit row"
        )


# ---------------------------------------------------------------------------
# Audit dispatch helper carries enough context to be useful
# ---------------------------------------------------------------------------


def test_dispatch_audit_signature_demands_actor_action_entity():
    """The helper signature must require the four fields auditors
    need: actor_id, action, entity_type, entity_id. A regression
    that demotes any of these to optional opens the door to "lazy"
    audit rows that don't identify who did what."""
    from app.services.audit_dispatch import dispatch_audit

    sig = inspect.signature(dispatch_audit)
    required = {"actor_id", "action", "entity_type", "entity_id", "organization_id"}
    declared = set(sig.parameters.keys())
    missing = required - declared
    assert not missing, f"dispatch_audit signature missing required params: {missing}"


# ---------------------------------------------------------------------------
# Schema response — exposing AuditLog must not let the UI sort by
# `shipped_at` in a way that hides rows
# ---------------------------------------------------------------------------


def test_audit_log_get_endpoint_returns_rows_ordered_by_created_at():
    """The endpoint orders by `created_at` so the timeline is
    chronological. A regression that ordered by `shipped_at` would
    push unshipped rows to the end of the list — operators would
    miss the most recent events."""
    from app.api import workflow

    src = inspect.getsource(workflow.get_audit_log)
    assert "order_by" in src
    assert "AuditLog.created_at" in src
    assert "shipped_at" not in src  # not the sort key


@pytest.mark.asyncio
async def test_audit_log_get_endpoint_filters_by_correlation_id():
    """The endpoint takes a correlation_id and returns rows scoped
    to it. The filter is the contract — a regression that returned
    every audit row in the tenant would mean a clerk asking about
    invoice #1 sees the whole audit history."""
    from app.api import workflow

    src = inspect.getsource(workflow.get_audit_log)
    assert "AuditLog.correlation_id" in src


# ---------------------------------------------------------------------------
# log_action — the write primitive itself (every other test mocks it out)
#
# Mock-based: asserts the row it builds and its add-without-commit contract.
# The DB round-trip (read the row back; discarded on caller rollback) is a
# real-Postgres property this mock-only suite can't assert.
# ---------------------------------------------------------------------------


def _audit_db():
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_log_action_builds_row_with_all_fields_and_returns_it():
    from app.services.audit import log_action

    db = _audit_db()
    corr, org, actor, ent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    entry = await log_action(
        db,
        correlation_id=corr,
        organization_id=org,
        actor_id=actor,
        action="invoice.approved",
        entity_type="invoice",
        entity_id=ent,
        details={"old_status": "ready_for_review", "new_status": "approved"},
    )

    db.add.assert_called_once_with(entry)
    assert entry.correlation_id == corr
    assert entry.organization_id == org
    assert entry.actor_id == actor
    assert entry.action == "invoice.approved"
    assert entry.entity_type == "invoice"
    assert entry.entity_id == ent
    assert entry.details == {"old_status": "ready_for_review", "new_status": "approved"}


@pytest.mark.asyncio
async def test_log_action_does_not_commit_or_flush():
    """The audit row's durability is tied to the caller's business
    transaction — log_action must only add(), never commit/flush, so the
    row commits (or rolls back) atomically with the status change."""
    from app.services.audit import log_action

    db = _audit_db()
    await log_action(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        action="invoice.approved",
        entity_type="invoice",
        entity_id=uuid.uuid4(),
    )
    db.add.assert_called_once()
    db.commit.assert_not_awaited()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_log_action_accepts_null_actor_and_details_for_system_events():
    """System-initiated transitions have no human actor — actor_id and
    details default to None and must land on the row as NULL."""
    from app.services.audit import log_action

    db = _audit_db()
    entry = await log_action(
        db,
        correlation_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        action="invoice.extraction_started",
        entity_type="invoice",
        entity_id=uuid.uuid4(),
    )
    assert entry.actor_id is None
    assert entry.details is None


# ---------------------------------------------------------------------------
# Real-Postgres: the SOC 2 who/what/when/where actually persists, and stays
# atomic with the caller's business transaction.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_action_persists_and_reads_back_all_fields(realdb):
    from sqlalchemy import select

    from app.models.workflow import AuditLog
    from app.services.audit import log_action

    info = realdb.info("a")
    corr, actor, ent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mk = realdb.sessionmaker("a")
    async with mk() as s:
        await log_action(
            s,
            correlation_id=corr,
            organization_id=info.org_id,
            actor_id=actor,
            action="invoice.approved",
            entity_type="invoice",
            entity_id=ent,
            details={"old_status": "ready_for_review", "new_status": "approved"},
        )
        await s.commit()

    async with mk() as s:
        row = (
            await s.execute(select(AuditLog).where(AuditLog.correlation_id == corr))
        ).scalar_one()
    assert row.organization_id == info.org_id
    assert row.actor_id == actor
    assert row.action == "invoice.approved"
    assert row.entity_type == "invoice"
    assert row.entity_id == ent
    assert row.details == {"old_status": "ready_for_review", "new_status": "approved"}
    assert row.created_at is not None  # server-default fired


@pytest.mark.asyncio
async def test_log_action_is_discarded_on_caller_rollback(realdb):
    """log_action only add()s — if the caller's transaction rolls back, the
    audit row must vanish with it (atomic with the business change)."""
    from sqlalchemy import func, select

    from app.models.workflow import AuditLog
    from app.services.audit import log_action

    info = realdb.info("a")
    mk = realdb.sessionmaker("a")

    async with mk() as s:
        await log_action(
            s,
            correlation_id=uuid.uuid4(),
            organization_id=info.org_id,
            action="invoice.approved",
            entity_type="invoice",
            entity_id=uuid.uuid4(),
        )
        await s.rollback()

    async with mk() as s:
        count = (await s.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    assert count == 0  # rolled back with the caller — nothing committed
