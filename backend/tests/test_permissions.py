"""Unit tests for the granular-permission layer (`app/api/permissions.py`).

Covers:
* the system-role default map reproduces today's RBAC matrix exactly,
* the effective-permissions union (system default map ∪ custom stored list),
* `sanitize_permissions` drops unknown / malformed entries,
* `require_permission` enforces any-of semantics + rejects typos at import time.

Pure, no DB — the resolver takes scalars / lightweight stand-ins.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

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
    effective_permissions,
    permissions_for_role,
    sanitize_permissions,
)


def _system_role(name: str):
    return SimpleNamespace(name=name, organization_id=None, permissions=None)


def _custom_role(permissions):
    return SimpleNamespace(name="Custom", organization_id=uuid.uuid4(), permissions=permissions)


# ---------- system-role default map = today's matrix --------------------------


def test_admin_has_every_permission():
    """admin keeps the whole catalog — unchanged from `require_roles(ADMIN)`."""
    assert ROLE_DEFAULT_PERMISSIONS[ROLE_ADMIN] == frozenset(ALL_PERMISSIONS)


def test_ap_manager_default_matches_prior_role_matrix():
    """ap_manager held: invoice approve, run create+execute, vendor bank-change
    approve, vendor block, vendor manage — but NOT payment void (admin/cfo only).
    `payment.record_external` (issue #517) has no prior route; it goes to the
    same holders as execute."""
    assert ROLE_DEFAULT_PERMISSIONS[ROLE_AP_MANAGER] == frozenset(
        {
            PERM_INVOICE_APPROVE,
            PERM_PAYMENT_RUN_APPROVE,
            PERM_PAYMENT_EXECUTE,
            PERM_PAYMENT_RECORD_EXTERNAL,
            PERM_VENDOR_BANK_CHANGE_APPROVE,
            PERM_VENDOR_BLOCK,
            PERM_VENDOR_MANAGE,
        }
    )
    assert PERM_PAYMENT_VOID not in ROLE_DEFAULT_PERMISSIONS[ROLE_AP_MANAGER]
    assert PERM_USER_MANAGE not in ROLE_DEFAULT_PERMISSIONS[ROLE_AP_MANAGER]


def test_cfo_default_matches_prior_role_matrix():
    """cfo held: invoice approve, run create+execute, payment void — but not
    vendor master-data / user management."""
    assert ROLE_DEFAULT_PERMISSIONS[ROLE_CFO] == frozenset(
        {
            PERM_INVOICE_APPROVE,
            PERM_PAYMENT_RUN_APPROVE,
            PERM_PAYMENT_EXECUTE,
            PERM_PAYMENT_VOID,
            PERM_PAYMENT_RECORD_EXTERNAL,
        }
    )
    assert PERM_VENDOR_BANK_CHANGE_APPROVE not in ROLE_DEFAULT_PERMISSIONS[ROLE_CFO]
    assert PERM_USER_MANAGE not in ROLE_DEFAULT_PERMISSIONS[ROLE_CFO]


def test_ap_clerk_default_is_empty():
    """A clerk holds none of the sensitive permissions — unchanged."""
    assert ROLE_DEFAULT_PERMISSIONS[ROLE_AP_CLERK] == frozenset()


def test_user_manage_default_is_admin_only():
    holders = {r for r, perms in ROLE_DEFAULT_PERMISSIONS.items() if PERM_USER_MANAGE in perms}
    assert holders == {ROLE_ADMIN}


def test_payment_void_default_is_admin_and_cfo_only():
    holders = {r for r, perms in ROLE_DEFAULT_PERMISSIONS.items() if PERM_PAYMENT_VOID in perms}
    assert holders == {ROLE_ADMIN, ROLE_CFO}


# ---------- permissions_for_role + effective_permissions union ----------------


def test_system_role_ignores_stored_permissions_column():
    """A system row's `permissions` column is NULL and irrelevant — even if it
    somehow carried a value, the static map wins (can't widen a system role)."""
    rogue = SimpleNamespace(
        name=ROLE_AP_CLERK, organization_id=None, permissions=[PERM_PAYMENT_EXECUTE]
    )
    assert (
        permissions_for_role(
            name=rogue.name, organization_id=rogue.organization_id, permissions=rogue.permissions
        )
        == frozenset()
    )


def test_custom_role_resolves_from_stored_list():
    role = _custom_role([PERM_INVOICE_APPROVE])
    assert permissions_for_role(
        name=role.name, organization_id=role.organization_id, permissions=role.permissions
    ) == frozenset({PERM_INVOICE_APPROVE})


def test_custom_role_with_none_or_empty_grants_nothing():
    assert (
        permissions_for_role(name="C", organization_id=uuid.uuid4(), permissions=None)
        == frozenset()
    )
    assert (
        permissions_for_role(name="C", organization_id=uuid.uuid4(), permissions=[]) == frozenset()
    )


def test_effective_permissions_unions_across_roles():
    """A clerk (no perms) + a custom role granting only invoice.approve → the
    union is exactly invoice.approve. This is the SoD split in action."""
    roles = [_system_role(ROLE_AP_CLERK), _custom_role([PERM_INVOICE_APPROVE])]
    assert effective_permissions(roles) == frozenset({PERM_INVOICE_APPROVE})


def test_effective_permissions_system_plus_custom():
    roles = [_system_role(ROLE_CFO), _custom_role([PERM_VENDOR_MANAGE])]
    out = effective_permissions(roles)
    assert PERM_PAYMENT_VOID in out  # from cfo
    assert PERM_VENDOR_MANAGE in out  # from custom
    assert PERM_USER_MANAGE not in out  # neither grants it


def test_effective_permissions_empty_for_no_roles():
    assert effective_permissions([]) == frozenset()
    assert effective_permissions(None) == frozenset()


# ---------- sanitize_permissions ---------------------------------------------


def test_sanitize_drops_unknown_and_dedupes_in_catalog_order():
    raw = [PERM_PAYMENT_EXECUTE, "bogus", PERM_INVOICE_APPROVE, PERM_INVOICE_APPROVE]
    out = sanitize_permissions(raw)
    # de-duped, only-known, and ordered by catalog order (approve before execute)
    assert out == [PERM_INVOICE_APPROVE, PERM_PAYMENT_EXECUTE]


def test_sanitize_handles_non_list_inputs():
    assert sanitize_permissions(None) == []
    assert sanitize_permissions("invoice.approve") == []
    assert sanitize_permissions(42) == []


# ---------- require_permission dependency -------------------------------------


def _fake_user_with_perms(*perms: str) -> MagicMock:
    user = MagicMock(spec=["id", "organization_id", "effective_permissions"])
    user.id = uuid.uuid4()
    user.organization_id = uuid.uuid4()
    user.effective_permissions = frozenset(perms)
    return user


def _fake_request() -> MagicMock:
    req = MagicMock()
    req.method = "POST"
    req.url.path = "/api/payments/runs/x/execute"
    return req


@pytest.mark.asyncio
async def test_require_permission_accepts_holder():
    checker = require_permission(PERM_PAYMENT_EXECUTE)
    user = _fake_user_with_perms(PERM_PAYMENT_EXECUTE)
    assert (await checker(request=_fake_request(), user=user)) is user


@pytest.mark.asyncio
async def test_require_permission_any_of_semantics():
    checker = require_permission(PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID)
    user = _fake_user_with_perms(PERM_PAYMENT_VOID)
    assert (await checker(request=_fake_request(), user=user)) is user


@pytest.mark.asyncio
async def test_require_permission_rejects_non_holder():
    """A custom role granted only invoice.approve is refused payment execution —
    the e2e SoD guarantee, asserted at the dependency level."""
    checker = require_permission(PERM_PAYMENT_EXECUTE)
    user = _fake_user_with_perms(PERM_INVOICE_APPROVE)
    with pytest.raises(HTTPException) as exc:
        await checker(request=_fake_request(), user=user)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_permission_rejects_no_permissions():
    checker = require_permission(PERM_PAYMENT_EXECUTE)
    user = _fake_user_with_perms()
    with pytest.raises(HTTPException) as exc:
        await checker(request=_fake_request(), user=user)
    assert exc.value.status_code == 403


def test_require_permission_rejects_empty_at_import_time():
    with pytest.raises(ValueError):
        require_permission()


def test_require_permission_rejects_typo_at_import_time():
    with pytest.raises(ValueError):
        require_permission("payment.exceute")


@pytest.mark.asyncio
async def test_require_permission_logs_denials(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="app.api.deps")
    checker = require_permission(PERM_PAYMENT_EXECUTE)
    user = _fake_user_with_perms(PERM_INVOICE_APPROVE)
    with pytest.raises(HTTPException):
        await checker(request=_fake_request(), user=user)
    assert any("RBAC denied (permission)" in rec.message for rec in caplog.records)


def test_every_label_covers_catalog():
    """No permission ships without a human label for the role editor."""
    from app.api.permissions import PERMISSION_LABELS

    assert set(PERMISSION_LABELS) == set(ALL_PERMISSIONS)


# ---------- POST /runs/{id}/approve — deliberately NOT on this permission ---


@pytest.mark.asyncio
async def test_approve_payment_run_route_checks_the_cfo_role_not_the_permission():
    """`POST /runs/{id}/approve` (the CFO sign-off above the org's dollar
    threshold) must stay on `require_roles(ROLE_CFO)`, NOT
    `require_permission(PERM_PAYMENT_RUN_APPROVE)` — that permission's
    default holders (admin, ap_manager) also cover `POST /runs` (create), and
    granting them the sign-off too defeats the control a genuine CFO
    signature exists to provide. A prior round migrated this on a
    false-consistency reading of the two routes, letting a non-CFO admin/
    ap_manager sign off — caught by `tests-e2e/payments/cfo-approval.spec.ts`
    and `tests-e2e/auth/rbac-api.spec.ts`.

    Proves the *behavior*, not which factory built the checker: a user
    holding the ROLE name "cfo" but none of the granular permissions passes,
    and a user holding the PERMISSION but no cfo role name is refused — only
    `require_roles` semantics produce that combination.
    """
    import inspect

    from app.api.payments import approve_payment_run

    checker = inspect.signature(approve_payment_run).parameters["user"].default.dependency

    cfo_role_only = MagicMock(spec=["id", "organization_id", "roles", "effective_permissions"])
    cfo_role_only.id = uuid.uuid4()
    cfo_role_only.organization_id = uuid.uuid4()
    cfo_role_only.roles = [SimpleNamespace(name="cfo")]
    cfo_role_only.effective_permissions = frozenset()
    assert (await checker(request=_fake_request(), user=cfo_role_only)) is cfo_role_only

    permission_only = MagicMock(spec=["id", "organization_id", "roles", "effective_permissions"])
    permission_only.id = uuid.uuid4()
    permission_only.organization_id = uuid.uuid4()
    permission_only.roles = [SimpleNamespace(name="admin")]
    permission_only.effective_permissions = frozenset({PERM_PAYMENT_RUN_APPROVE})
    with pytest.raises(HTTPException) as exc:
        await checker(request=_fake_request(), user=permission_only)
    assert exc.value.status_code == 403


# ---------- payment.execute / payment.void — custom-role-only end-to-end ------
#
# Proves the finding this migration closes: a custom role holding ONLY the
# granular permission (no admin/ap_manager/cfo system role at all) can reach
# the money-moving endpoint AND its supporting read endpoints — the queue/run
# list, the run detail, the payment list/detail — that the web UI needs to
# even present the Execute/Void button. Same checker-introspection pattern as
# `test_approve_payment_run_route_checks_the_cfo_role_not_the_permission`
# above: pull the real dependency off the route function so a future edit to
# the route can't drift from what this test exercises.


def _permission_only_user(*perms: str) -> MagicMock:
    """A user with NO system role name at all — only the effective permission
    set a custom role would confer. If this passes, the permission alone
    (not an implicit role membership) is what let the request through."""
    user = MagicMock(spec=["id", "organization_id", "roles", "effective_permissions"])
    user.id = uuid.uuid4()
    user.organization_id = uuid.uuid4()
    user.roles = [SimpleNamespace(name="Custom AP Payments Role")]
    user.effective_permissions = frozenset(perms)
    return user


def _checker_for(func):
    import inspect

    return inspect.signature(func).parameters["user"].default.dependency


@pytest.mark.asyncio
async def test_custom_role_with_only_payment_execute_can_execute_a_run():
    from app.api.payments import execute_payment_run

    checker = _checker_for(execute_payment_run)
    holder = _permission_only_user(PERM_PAYMENT_EXECUTE)
    assert (await checker(request=_fake_request(), user=holder)) is holder

    non_holder = _permission_only_user(PERM_PAYMENT_VOID)
    with pytest.raises(HTTPException) as exc:
        await checker(request=_fake_request(), user=non_holder)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_custom_role_with_only_payment_void_can_void_a_payment():
    from app.api.payments import void_payment

    checker = _checker_for(void_payment)
    holder = _permission_only_user(PERM_PAYMENT_VOID)
    assert (await checker(request=_fake_request(), user=holder)) is holder

    non_holder = _permission_only_user(PERM_PAYMENT_EXECUTE)
    with pytest.raises(HTTPException) as exc:
        await checker(request=_fake_request(), user=non_holder)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_custom_role_with_either_permission_reaches_the_payment_list_and_detail():
    """`GET /api/payments` and `GET /api/payments/{id}` are the History-tab
    reads a `payment.void`-only holder needs to find the row to void — and,
    symmetrically, what a `payment.execute`-only holder needs to check what a
    run already paid. Any-of over both permissions, matching the exact prior
    `require_roles(ADMIN, AP_MANAGER, CFO)` footprint (see the migration
    comment on `list_payments` in `app/api/payments.py`)."""
    from app.api.payments import get_payment, list_payments

    for route in (list_payments, get_payment):
        checker = _checker_for(route)
        execute_holder = _permission_only_user(PERM_PAYMENT_EXECUTE)
        assert (await checker(request=_fake_request(), user=execute_holder)) is execute_holder

        void_holder = _permission_only_user(PERM_PAYMENT_VOID)
        assert (await checker(request=_fake_request(), user=void_holder)) is void_holder

        neither = _permission_only_user(PERM_INVOICE_APPROVE)
        with pytest.raises(HTTPException) as exc:
            await checker(request=_fake_request(), user=neither)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_custom_role_with_only_payment_execute_reaches_the_runs_list_and_detail():
    """`GET /api/payments/runs/` and `GET /api/payments/runs/{id}` are what
    `RunDetailModal.svelte` loads to put the Execute button on screen at all —
    without these a `payment.execute`-only holder could call the execute
    endpoint directly but never reach it through the app."""
    from app.api.payments import get_payment_run, list_payment_runs

    for route in (list_payment_runs, get_payment_run):
        checker = _checker_for(route)
        holder = _permission_only_user(PERM_PAYMENT_EXECUTE)
        assert (await checker(request=_fake_request(), user=holder)) is holder

        non_holder = _permission_only_user(PERM_PAYMENT_VOID)
        with pytest.raises(HTTPException) as exc:
            await checker(request=_fake_request(), user=non_holder)
        assert exc.value.status_code == 403
