"""Endpoint SoD wiring — the splittable, money/approval-sensitive routes gate on
`require_permission(...)`, not bare `require_roles(...)`.

Why this matters: the granular-permission layer exists so an org can SPLIT
fraud-sensitive duties (approve invoices vs. move money) onto custom roles. A
route still on `require_roles` silently ignores that split — a custom role that
had `payment.execute` stripped could still move money, and a custom role granted
`invoice.approve` could not actually approve. These tests pin the wiring so a
future refactor can't quietly drop a route back onto `require_roles`.

We resolve each route's `require_permission` checker out of its FastAPI
dependency tree and exercise it directly with a user holding / lacking the
permission — so the test covers BOTH "this route is permission-gated" and "it
gates on the RIGHT permission set," with no DB. The resolution itself lives in
`tests/permission_gates.py` and identifies the gate by its **code object**:
`require_permission` builds a fresh closure per call but they all share one
`__code__`, so a rename of the real checker cannot make this scan quietly stop
finding it. This file used to match `__qualname__` instead — not a bypass (the
qualname pins the enclosing factory), but strictly weaker, and a second copy of
the question is how the two files drifted apart at all.

**The pin is inverted, not hand-maintained.** `CASES` used to be a hand-written
list, and a hand-written list omits things: the eight routes that actually move
money (`execute`, `void`, `settlement/accept`, `compliance/release`,
`compliance/dismiss`, `resume`, `sync-erp`, `retry-failed`) were all missing from
it, on the one class of route where the SoD layer existing at all is the point,
and two of the eight catalogue permissions were never imported here.
`test_every_permission_gated_route_is_pinned` closes that for good: it derives the
set of permission-gated routes from the running app and fails if any of them is
absent from `CASES`. Adding a `require_permission` route now REQUIRES adding it
here, with the exact permission set it gates on.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.deps import (
    ROLE_ADMIN,
    ROLE_AP_CLERK,
    ROLE_AP_MANAGER,
    ROLE_CFO,
    require_permission,
)
from app.api.permissions import (
    ALL_PERMISSIONS,
    PERM_INVOICE_APPROVE,
    PERM_PAYMENT_EXECUTE,
    PERM_PAYMENT_RECORD_EXTERNAL,
    PERM_PAYMENT_RUN_APPROVE,
    PERM_PAYMENT_VOID,
    PERM_USER_MANAGE,
    PERM_VENDOR_BANK_CHANGE_APPROVE,
    PERM_VENDOR_BLOCK,
    PERM_VENDOR_MANAGE,
    ROLE_DEFAULT_PERMISSIONS,
)
from app.main import app
from tests.permission_gates import permission_checkers, permission_gate_sets

# `require_permission(...)` builds a fresh closure per call, but every one of
# them shares a single code object — comparing `__code__` identifies the factory
# exactly, where `__qualname__.endswith("require_permission.<locals>.checker")`
# only guesses at it (an unrelated closure named `checker` would satisfy the
# string, and renaming the real one would break it). Mirrors
# `test_rbac.py::_REQUIRE_PERMISSION_CODE`.
_REQUIRE_PERMISSION_CODE = require_permission("user.manage").__code__


def _is_require_permission_checker(call) -> bool:
    return getattr(call, "__code__", None) is _REQUIRE_PERMISSION_CODE


def _iter_app_routes():
    """Yield (path, methods, route) for every route in the app.

    FastAPI 0.138 changed `include_router` to keep nested `_IncludedRouter`
    objects in `app.routes` instead of flattening sub-routes into top-level
    `APIRoute`s, so the old flat scan no longer sees included routes. Flatten
    via the supported `iter_route_contexts` helper (full path + the underlying
    `APIRoute`, which still carries `.dependant`); fall back to the flat list on
    a FastAPI that predates the helper.
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:
        for route in app.routes:
            yield getattr(route, "path", None), getattr(route, "methods", set()) or set(), route
        return
    for ctx in iter_route_contexts(app.routes):
        yield ctx.path, ctx.methods or set(), ctx.route


def _find_route(path: str, method: str):
    for route_path, methods, route in _iter_app_routes():
        if route_path == path and method in methods:
            return route
    raise AssertionError(f"route not found: {method} {path}")


def _permission_checkers(route):
    """Every `require_permission(...)` gate on a route, as the permission set it
    captured in its closure. Code-object identity — see `permission_gates`."""
    return permission_gate_sets(route)


def _permission_gated_routes() -> dict[tuple[str, str], list[frozenset[str]]]:
    """Every (path, method) in the app that goes through `require_permission`.

    Derived from the live app — this is the ground truth `CASES` is checked
    against, so a new permission-gated route cannot escape the pin.
    """
    out: dict[tuple[str, str], list[frozenset[str]]] = {}
    for path, methods, route in _iter_app_routes():
        if not hasattr(route, "dependant"):
            continue
        captured = _permission_checkers(route)
        if not captured:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            out[(path, method)] = captured
    return out


def _user_with(perms: set[str]):
    return SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        effective_permissions=frozenset(perms),
        roles=[],
    )


async def _call_checker(route, user):
    """Invoke the route's permission checker as FastAPI would."""
    checkers = permission_checkers(route)
    assert checkers, "no require_permission checker on route"
    fake_request = SimpleNamespace(
        client=None, headers={}, method="POST", url=SimpleNamespace(path="/test")
    )
    return await checkers[0](request=fake_request, user=user)


# --- which permission set each splittable route must gate on -------------------
#
# The third element is the EXACT any-of set the route's `require_permission(...)`
# was constructed with — not merely "one permission it must include". Pinning the
# exact set catches a widening (someone quietly adding `payment.void` to the
# `/execute` gate) as well as a narrowing, and both are SoD failures.
#
# Every entry here is checked against the live app by
# `test_every_permission_gated_route_is_pinned`, in both directions.

_EXECUTE = frozenset({PERM_PAYMENT_EXECUTE})
_VOID = frozenset({PERM_PAYMENT_VOID})
# The payments page's reads also admit `payment.record_external` (issue #517):
# a role that only records payments made outside FeohLedger still has to reach
# the queue it records them from.
_EXECUTE_OR_VOID = frozenset(
    {PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID, PERM_PAYMENT_RECORD_EXTERNAL}
)
_RECORD_EXTERNAL = frozenset({PERM_PAYMENT_RECORD_EXTERNAL})
_EXECUTE_OR_RECORD = frozenset({PERM_PAYMENT_EXECUTE, PERM_PAYMENT_RECORD_EXTERNAL})
_RUN_APPROVE = frozenset({PERM_PAYMENT_RUN_APPROVE})
_INVOICE_APPROVE = frozenset({PERM_INVOICE_APPROVE})
_VENDOR_MANAGE = frozenset({PERM_VENDOR_MANAGE})
_VENDOR_BLOCK = frozenset({PERM_VENDOR_BLOCK})
_BANK_CHANGE_APPROVE = frozenset({PERM_VENDOR_BANK_CHANGE_APPROVE})
_USER_MANAGE = frozenset({PERM_USER_MANAGE})

CASES = [
    ("/api/invoices/{invoice_id}/approve", "POST", _INVOICE_APPROVE),
    # Reject is the other half of the approve duty: an approver must be able to
    # reject, and a custom role from which `invoice.approve` was stripped must
    # not be able to reject through the back door. Same permission as approve.
    ("/api/invoices/{invoice_id}/reject", "POST", _INVOICE_APPROVE),
    # --- payment.execute: the routes that actually move money, or that decide
    # an already-moved payment's fate in AP's favour. These are the reason the
    # SoD layer exists at all, so they are pinned individually rather than left
    # to the payments router's read surface. ---
    #
    # `POST /payments` books a single ad-hoc payment; `runs/{id}/execute` funds
    # a whole draft run. Both call the payment adapter — real money leaves.
    ("/api/payments", "POST", _EXECUTE),
    ("/api/payments/runs/{run_id}/execute", "POST", _EXECUTE),
    # `resume` picks an interrupted run back up and keeps dispatching its
    # remaining payments — the same adapter call as `execute`, so the same gate.
    ("/api/payments/runs/{run_id}/resume", "POST", _EXECUTE),
    # `retry-failed` books a NEW attempt row per failed payment and dispatches
    # it. A retry is a fresh outbound payment, not a bookkeeping correction.
    ("/api/payments/runs/{run_id}/retry-failed", "POST", _EXECUTE),
    # `compliance/release` re-runs the SAME compliance-then-adapter path on a
    # payment the sanctions/KYC gate held, so a release that clears dispatches
    # the money. `payment.execute`, never `payment.void` — see below.
    ("/api/payments/{payment_id}/compliance/release", "POST", _EXECUTE),
    # `settlement/accept` declares an under-settlement final and releases the
    # invoice to `paid`. It moves no new money, but it is the affirmative
    # "this payment stands" call — the execute side of the split, not the
    # give-up side; `/void` is the give-up exit and gates on `payment.void`.
    ("/api/payments/{payment_id}/settlement/accept", "POST", _EXECUTE),
    # `sync-erp` re-runs the ERP sync-back pass for a run whose legs failed.
    # It moves no money (that already happened) but it is the exit that
    # transitions invoices to `paid`, so it sits with the execute duty.
    ("/api/payments/runs/{run_id}/sync-erp", "POST", _EXECUTE),
    # --- payment.void: giving up on / reversing money. Deliberately the OTHER
    # half of the split — by default `cfo` holds both but `ap_manager` holds
    # only `payment.execute`, so an org that splits them keeps reversal away
    # from whoever initiates. ---
    ("/api/payments/{payment_id}/void", "POST", _VOID),
    # `void/retry-card-cancel` re-attempts the card close for an ALREADY-voided
    # card payment — the other half of a reversal, so the same gate as the void
    # it completes. Deliberately NOT the card router's bare
    # `require_roles(ADMIN, AP_MANAGER, CFO)`: an org that split the duties and
    # withheld `payment.void` from `ap_manager` must not find this reachable.
    ("/api/payments/{payment_id}/void/retry-card-cancel", "POST", _VOID),
    # `POST /api/cards/{id}/cancel` is the third door onto "close this card", and
    # it was the widest: `require_roles(ADMIN, AP_MANAGER, CFO)` while both of the
    # routes above gate the same effect on `payment.void`. It is the ONE migrated
    # route that deliberately does NOT reproduce its prior system-role matrix —
    # `ap_manager` loses it, which is the fix, not a regression. Pinned by
    # `test_card_cancel_narrows_ap_manager_by_design` below so the narrowing is a
    # stated fact rather than a side effect nobody wrote down.
    ("/api/cards/{card_id}/cancel", "POST", _VOID),
    # `compliance/dismiss` gives up on a held payment and flips it to `failed`.
    # Its sibling `/release` gates on `payment.execute`: the two halves of the
    # compliance-hold exit are deliberately on opposite sides of the split.
    ("/api/payments/{payment_id}/compliance/dismiss", "POST", _VOID),
    # --- payment_run.approve: staging a draft run (choosing which invoices get
    # paid) is a separate duty from funding it. NOT the CFO sign-off route
    # `POST /runs/{id}/approve`, which stays on `require_roles(ROLE_CFO)` — see
    # `app/api/permissions.py` and that route's own docstring. ---
    ("/api/payments/runs", "POST", _RUN_APPROVE),
    # --- Supporting reads a `payment.execute`/`payment.void` custom-role holder
    # needs to REACH the money-moving action through the app, not just call it
    # directly. The any-of set reproduces the prior `require_roles(ADMIN,
    # AP_MANAGER, CFO)` exactly for the four system roles. ---
    ("/api/payments", "GET", _EXECUTE_OR_VOID),
    ("/api/payments/counts", "GET", _EXECUTE_OR_VOID),
    ("/api/payments/{payment_id}", "GET", _EXECUTE_OR_VOID),
    # The three reads the `/payments` PAGE fires on mount, unconditionally and
    # before any tab is chosen. They stayed on `require_roles(ADMIN, AP_MANAGER,
    # CFO)` while their siblings above migrated, so `nav.ts` — which OR's in
    # `payment.execute` / `payment.void` precisely so a permission-only custom
    # role can reach this page — showed that role a sidebar row whose first
    # paint 403'd three times. A nav entry that admits a role the page's first
    # load refuses is worse than a hidden page: the SoD split the permission
    # layer exists for was unusable through the UI.
    ("/api/payments/summary", "GET", _EXECUTE_OR_VOID),
    ("/api/payments/queue", "GET", _EXECUTE_OR_VOID),
    ("/api/payments/queue/ids", "GET", _EXECUTE_OR_VOID),
    # The run reads gate on `payment.execute` (or `payment.record_external`,
    # which records a whole run paid outside): a void-only role acts on
    # individual payments, not on runs.
    ("/api/payments/runs/", "GET", _EXECUTE_OR_RECORD),
    ("/api/payments/runs/{run_id}", "GET", _EXECUTE_OR_RECORD),
    # --- payment.record_external: the no-rail pilot (issue #517). Recording a
    # payment the customer made itself, a whole run it paid through its bank,
    # The mode read is display-only. ---
    ("/api/payments/execution-mode", "GET", _EXECUTE_OR_VOID),
    ("/api/payments/record-outside", "POST", _RECORD_EXTERNAL),
    ("/api/payments/runs/{run_id}/record-outside", "POST", _RECORD_EXTERNAL),
    # The NACHA export is the counterpart of `/execute` — uploading the file is
    # the money moving — so it gates on `payment.execute`, not on recording.
    ("/api/payments/runs/{run_id}/nacha", "GET", _EXECUTE),
    ("/api/payments/runs/{run_id}/nacha/void", "POST", _EXECUTE),
    # --- vendor.manage: create / edit / verify / reject / delete a vendor, and
    # the bulk-import paths that do the same thing at volume. ---
    ("/api/vendors", "POST", _VENDOR_MANAGE),
    ("/api/vendors/{vendor_id}", "PATCH", _VENDOR_MANAGE),
    ("/api/vendors/{vendor_id}", "DELETE", _VENDOR_MANAGE),
    ("/api/vendors/{vendor_id}/bank-change", "POST", _VENDOR_MANAGE),
    ("/api/vendors/{vendor_id}/verify", "POST", _VENDOR_MANAGE),
    ("/api/vendors/{vendor_id}/reject", "POST", _VENDOR_MANAGE),
    ("/api/vendors/{vendor_id}/screen", "POST", _VENDOR_MANAGE),
    ("/api/vendors/sync-erp", "POST", _VENDOR_MANAGE),
    ("/api/vendors/import-csv", "POST", _VENDOR_MANAGE),
    # Bulk verify/reject is the single-row verify/reject at volume — same gate,
    # or the bulk endpoint becomes the way around it.
    ("/api/vendors/bulk/status", "POST", _VENDOR_MANAGE),
    ("/api/enrichment/vendors/consolidation/merge", "POST", _VENDOR_MANAGE),
    # --- vendor.block: sticky payment block/unblock. ---
    ("/api/vendors/{vendor_id}/block", "POST", _VENDOR_BLOCK),
    ("/api/vendors/{vendor_id}/unblock", "POST", _VENDOR_BLOCK),
    # --- vendor.bank_change.approve: the BEC / bank-redirect dual-control gate.
    # Its sibling `POST /change-requests/{id}/reject` is deliberately NOT here —
    # reject never touches the vendor row (it can't redirect a payment), so it
    # stays on `require_roles(ADMIN, AP_MANAGER)` rather than sharing the
    # money-authorizing permission. See the route's own comment in
    # app/api/vendors.py and docs/authentication.md § "Approve and reject are
    # not always the same role set".
    ("/api/vendors/change-requests/{request_id}/approve", "POST", _BANK_CHANGE_APPROVE),
    # --- user.manage: who exists and what they can do. The role CRUD routes
    # (`POST/PATCH/DELETE /admin/roles`) deliberately stay on
    # `require_roles(ROLE_ADMIN)` and are therefore absent here — role CRUD is
    # the meta-layer that DEFINES permissions, so gating it on a permission a
    # custom role could itself be granted would let that role mint itself
    # everything. ---
    ("/api/admin/users", "GET", _USER_MANAGE),
    ("/api/admin/users", "POST", _USER_MANAGE),
    ("/api/admin/users/{user_id}", "PATCH", _USER_MANAGE),
    ("/api/admin/users/{user_id}", "DELETE", _USER_MANAGE),
    ("/api/admin/users/bulk-delete", "POST", _USER_MANAGE),
    ("/api/admin/users/{user_id}/revoke-sessions", "POST", _USER_MANAGE),
    # The role list + permission catalog are the role editor's data source —
    # readable by whoever may assign roles, i.e. the same duty.
    ("/api/admin/roles", "GET", _USER_MANAGE),
    ("/api/admin/permissions", "GET", _USER_MANAGE),
]

PINNED = {(path, method) for path, method, _ in CASES}


@pytest.mark.parametrize("path,method,expected", CASES)
def test_route_gates_on_expected_permission(path, method, expected):
    route = _find_route(path, method)
    captured = _permission_checkers(route)
    assert captured, f"{method} {path} is not permission-gated (still on require_roles?)"
    assert captured == [expected], (
        f"{method} {path} must gate on exactly {sorted(expected)}; "
        f"captured {[sorted(s) for s in captured]}"
    )


@pytest.mark.parametrize("path,method,expected", CASES)
@pytest.mark.asyncio
async def test_holder_allowed_nonholder_denied(path, method, expected):
    route = _find_route(path, method)

    # Every leg of the any-of set independently grants access — so an org that
    # splits the duties doesn't lose the route for one half of the split.
    for perm in sorted(expected):
        allowed = await _call_checker(route, _user_with({perm}))
        assert allowed is not None, f"{method} {path} denied a holder of {perm}"

    # A user with no permissions (e.g. a custom role with the duty stripped) is
    # rejected with 403 — the back door is closed.
    with pytest.raises(HTTPException) as exc:
        await _call_checker(route, _user_with(set()))
    assert exc.value.status_code == 403


# --- the one deliberate departure from the prior system-role matrix ------------

_CARD_CANCEL = ("/api/cards/{card_id}/cancel", "POST")

# Every route that closes a virtual card. They must agree, because a card closed
# through the widest of them leaves the ledger in whatever state that door's
# caller was allowed to reach.
_CARD_CLOSING_ROUTES = [
    _CARD_CANCEL,
    ("/api/payments/{payment_id}/void", "POST"),
    ("/api/payments/{payment_id}/void/retry-card-cancel", "POST"),
]


def _system_role_user(role_name: str):
    """A user holding exactly one SYSTEM role, resolved through the real map."""
    return _user_with(set(ROLE_DEFAULT_PERMISSIONS[role_name]))


async def _allows(route, user) -> bool:
    try:
        await _call_checker(route, user)
    except HTTPException as exc:
        assert exc.status_code == 403
        return False
    return True


@pytest.mark.asyncio
async def test_card_cancel_narrows_ap_manager_by_design():
    """`POST /api/cards/{id}/cancel` no longer admits `ap_manager`, on purpose.

    This is the ONE route migrated off `require_roles` that does not reproduce
    its prior admin/ap_manager/cfo matrix — and the mismatch was the defect. The
    gate was wider than the `payment.void` gate on the two operations that close
    the same card (`/payments/{id}/void` and its `retry-card-cancel` completion),
    so an `ap_manager` who could not reverse a card payment could still kill the
    card behind a live one. `docs/decisions.md` §132 stated the rule for the
    sibling route; this applies it to the door that was left open.

    Asserted as an explicit four-role table rather than left implied, so a future
    "restore the old matrix" edit has to argue with a named expectation instead
    of quietly widening a money-adjacent control.
    """
    route = _find_route(*_CARD_CANCEL)

    assert await _allows(route, _system_role_user(ROLE_ADMIN))
    assert await _allows(route, _system_role_user(ROLE_CFO))
    # The deliberate change. `ap_manager` holds `payment.execute`, not
    # `payment.void` — initiating money and reversing it are the two halves of
    # the split, and closing a card sits on the reversal half.
    assert not await _allows(route, _system_role_user(ROLE_AP_MANAGER))
    # Unchanged: a clerk never held any sensitive permission.
    assert not await _allows(route, _system_role_user(ROLE_AP_CLERK))


@pytest.mark.asyncio
async def test_every_card_closing_route_gates_on_payment_void():
    """The three doors onto "close this card" gate identically.

    A capability with a second, divergent path is worse than one with none
    (`docs/decisions.md` §96). That argument is usually about *state*; it is just
    as true of the gate, and a wider third door is how the SoD split gets
    silently undone.
    """
    for path, method in _CARD_CLOSING_ROUTES:
        route = _find_route(path, method)
        assert _permission_checkers(route) == [_VOID], (
            f"{method} {path} closes a card but does not gate on payment.void"
        )


def test_every_permission_gated_route_is_pinned():
    """The pin is complete: `CASES` covers EVERY `require_permission` route.

    This is the guard that makes the hand-list safe. A hand-list is exactly what
    let the eight money-moving payment routes go unpinned; deriving the set from
    the app means a new `require_permission` route fails the suite until it is
    pinned here with the permission set it gates on.
    """
    live = set(_permission_gated_routes())

    unpinned = live - PINNED
    assert not unpinned, (
        "these routes go through require_permission but are not pinned in CASES — "
        "add each with the exact permission set it gates on: " + str(sorted(unpinned))
    )

    stale = PINNED - live
    assert not stale, (
        "these routes are pinned in CASES but no longer go through "
        "require_permission (moved, renamed, or downgraded to require_roles): " + str(sorted(stale))
    )


def test_every_catalogue_permission_is_exercised():
    """Every permission in the catalogue gates at least one pinned route.

    `payment.void` and `user.manage` were in the catalogue but never imported by
    this module, so nothing here proved either was wired to anything. A catalogue
    entry that gates no route is dead config — an org can grant it to a custom
    role and get nothing.
    """
    exercised: set[str] = set()
    for _path, _method, expected in CASES:
        exercised |= set(expected)

    missing = set(ALL_PERMISSIONS) - exercised
    assert not missing, (
        "these catalogue permissions gate no pinned route: "
        + str(sorted(missing))
        + " — either pin the route that uses one, or remove it from ALL_PERMISSIONS"
    )

    unknown = exercised - set(ALL_PERMISSIONS)
    assert not unknown, f"CASES names permissions outside the catalogue: {sorted(unknown)}"


# --- the resolution itself is by identity, not by name -------------------------


def test_gate_resolution_ignores_a_decoy_with_the_right_qualname():
    """A function merely *named* like the checker is not treated as a gate.

    This file used to resolve the gate with
    `__qualname__.endswith("require_permission.<locals>.checker")`. That is
    weaker in both directions, and this pins both:

      * a decoy carrying that qualname must NOT satisfy it — otherwise a helper
        that happens to be spelled that way could stand in for a real gate;
      * a renamed real checker must STILL satisfy it — otherwise the scan goes
        quiet, and `test_every_permission_gated_route_is_pinned` would report a
        route as un-gated (or worse, stop seeing it) after a pure rename.
    """
    from app.api.deps import require_permission
    from tests.permission_gates import is_permission_gate

    def decoy():  # pragma: no cover — never called
        pass

    decoy.__qualname__ = "require_permission.<locals>.checker"
    assert not is_permission_gate(decoy)

    real = require_permission(PERM_PAYMENT_EXECUTE)
    assert is_permission_gate(real)

    real.__qualname__ = "something_else_entirely"
    assert is_permission_gate(real), "renaming the checker must not hide it from the scan"


def test_gate_resolution_reads_the_real_permission_set():
    """The set comes out of the checker's own closure, not from a lookup table."""
    from app.api.deps import require_permission
    from tests.permission_gates import _captured_permissions

    checker = require_permission(PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID)
    assert _captured_permissions(checker) == frozenset({PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID})
