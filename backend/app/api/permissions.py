"""Granular permission catalog + system-role default-permission map.

This is the data layer behind ``require_permission`` (in ``deps.py``). It exists
to let an org *split* fraud-sensitive duties that are conflated inside a single
system role today — most importantly, that one ``ap_manager`` can both approve a
vendor bank-detail change (where the money goes) AND execute a payment run (the
money moving). A textbook segregation-of-duties violation that no amount of
whole-role bundling can fix; only a permission can.

Design (additive, backward-compatible):

* The catalog is **deliberately small** — only the sensitive, *splittable* set,
  not an exhaustive enumeration of every action. Everything not listed here
  stays on ``require_roles`` and behaves exactly as before.
* ``ROLE_DEFAULT_PERMISSIONS`` reproduces today's RBAC matrix for the four
  system roles *exactly*, so they resolve identically with zero behaviour
  change. System ``Role`` rows leave ``Role.permissions`` NULL and resolve via
  this map; a custom role stores an explicit list on the column.
* Effective permissions for a user = the union over all their roles (system via
  this map, custom via the stored list). Computed once in ``get_current_user``.

Keep the constants as the single source of truth — endpoints import these names,
never bare strings, so a typo is an ``ImportError`` not a silent always-deny.
"""

from __future__ import annotations

from app.api.deps import ROLE_ADMIN, ROLE_AP_CLERK, ROLE_AP_MANAGER, ROLE_CFO

# ---------------------------------------------------------------------------
# Permission catalog — the sensitive, splittable set.
#
# Naming: dotted ``domain.action`` strings. These are the durable identifiers
# stored in ``roles.permissions`` and shipped to the frontend, so renaming one
# is a breaking change (it would orphan any custom role that stored the old
# name). Add new permissions; don't rename existing ones.
# ---------------------------------------------------------------------------

# Invoice approval (the approval queue decision). Splittable from the money
# movement below so an org can let a role approve invoices but never pay them.
PERM_INVOICE_APPROVE = "invoice.approve"

# Money movement — the two ends of the payment SoD split.
PERM_PAYMENT_EXECUTE = "payment.execute"  # execute a draft payment run (sends money)
PERM_PAYMENT_VOID = "payment.void"  # reverse / void a payment
PERM_PAYMENT_RUN_APPROVE = "payment_run.approve"  # approve a payment run before execution
# Record a payment the customer made OUTSIDE FeohLedger (cheque, bank portal, a
# bank file it uploaded itself) — the no-rail pilot's path to `paid`
# (`services/external_payment.py`, decisions §251). Its own permission rather
# than `payment.execute`: recording moves no money, so an org that keeps
# sending payments with a few people can still let others book what they paid.
PERM_PAYMENT_RECORD_EXTERNAL = "payment.record_external"

# Vendor master-data control — the BEC / bank-redirect fraud surface.
PERM_VENDOR_BANK_CHANGE_APPROVE = "vendor.bank_change.approve"  # approve a staged bank/tax change
PERM_VENDOR_BLOCK = "vendor.block"  # block / unblock payments to a vendor
PERM_VENDOR_MANAGE = "vendor.manage"  # create / edit / verify / reject vendors

# User & access administration.
PERM_USER_MANAGE = "user.manage"  # create / edit / delete users, assign roles


# The full catalog — the set a custom role may be granted, and the set the
# /admin/roles UI renders checkboxes for. Order is display order.
ALL_PERMISSIONS: tuple[str, ...] = (
    PERM_INVOICE_APPROVE,
    PERM_PAYMENT_RUN_APPROVE,
    PERM_PAYMENT_EXECUTE,
    PERM_PAYMENT_VOID,
    PERM_PAYMENT_RECORD_EXTERNAL,
    PERM_VENDOR_BANK_CHANGE_APPROVE,
    PERM_VENDOR_BLOCK,
    PERM_VENDOR_MANAGE,
    PERM_USER_MANAGE,
)

_ALL_PERMISSIONS_SET = frozenset(ALL_PERMISSIONS)

# Human-readable labels — surfaced verbatim by the frontend role editor. Kept
# here so the catalog (names + labels) has one home.
PERMISSION_LABELS: dict[str, str] = {
    PERM_INVOICE_APPROVE: "Approve invoices",
    PERM_PAYMENT_RUN_APPROVE: "Approve payment runs",
    PERM_PAYMENT_EXECUTE: "Execute payment runs (move money)",
    PERM_PAYMENT_VOID: "Void / reverse payments",
    PERM_PAYMENT_RECORD_EXTERNAL: "Record payments made outside FeohLedger",
    PERM_VENDOR_BANK_CHANGE_APPROVE: "Approve vendor bank / tax changes",
    PERM_VENDOR_BLOCK: "Block / unblock vendor payments",
    PERM_VENDOR_MANAGE: "Manage vendors",
    PERM_USER_MANAGE: "Manage users & roles",
}


# ---------------------------------------------------------------------------
# System-role → default permissions.
#
# This MUST reproduce the existing `require_roles` matrix on the migrated
# endpoints exactly, so the four system roles behave identically before and
# after this layer lands. The mapping below is read off the current
# `require_roles(...)` declarations on each migrated endpoint:
#
#   payment execute   require_roles(ADMIN, AP_MANAGER, CFO)
#   payment void      require_roles(ADMIN, CFO)
#   payment-run approve (create draft) require_roles(ADMIN, AP_MANAGER, CFO)
#   payment-run CFO sign-off (`POST /runs/{id}/approve`) stays on
#     `require_roles(ROLE_CFO)`, NOT this permission — the two "approve"
#     names collide but the actions don't: sign-off is the mandatory human
#     CFO check above the org's dollar threshold, and granting it to
#     whoever holds the broader create-draft permission (admin/ap_manager
#     by default) defeats that control. See the route's own docstring.
#   vendor bank-change approve         require_roles(ADMIN, AP_MANAGER)
#   vendor block/unblock               require_roles(ADMIN, AP_MANAGER)
#   vendor manage (create/edit/verify) require_roles(ADMIN, AP_MANAGER)
#   user manage                        require_roles(ADMIN)
#   invoice approve                    require_roles(ADMIN, AP_MANAGER, CFO)  (review path)
#   payment record-outside             NEW (no prior route) — granted to the
#     same set as payment execute: the people who already close an invoice
#     out as paid. Its supporting reads (the payments list / queue / runs) OR
#     it in beside execute/void, the way they already OR the two of those.
#
# ap_clerk holds NONE of the sensitive permissions — exactly as today (a clerk
# can upload + enter data but cannot approve or pay).
#
# ONE ROUTE DELIBERATELY DOES NOT REPRODUCE ITS PRIOR MATRIX.
# `POST /api/cards/{id}/cancel` was `require_roles(ADMIN, AP_MANAGER, CFO)` and
# is now `require_permission(payment.void)`, so `ap_manager` LOSES it. That is
# the point rather than a regression: closing a card is the card half of a
# reversal, `POST /api/payments/{id}/void` and
# `POST /api/payments/{id}/void/retry-card-cancel` both gate it on
# `payment.void`, and the card router's wider role gate was the one door onto
# that effect an `ap_manager` denied `payment.void` could still walk through.
# Recorded here because "the map reproduces the matrix exactly" is otherwise
# read as unconditional; it holds for every route but this one, and the reason
# lives on the route's own docstring. See `docs/decisions.md` §96 / §132.
# ---------------------------------------------------------------------------

ROLE_DEFAULT_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset(ALL_PERMISSIONS),  # admin keeps everything
    ROLE_AP_MANAGER: frozenset(
        {
            PERM_INVOICE_APPROVE,
            PERM_PAYMENT_RUN_APPROVE,
            PERM_PAYMENT_EXECUTE,
            PERM_PAYMENT_RECORD_EXTERNAL,
            PERM_VENDOR_BANK_CHANGE_APPROVE,
            PERM_VENDOR_BLOCK,
            PERM_VENDOR_MANAGE,
        }
    ),
    ROLE_CFO: frozenset(
        {
            PERM_INVOICE_APPROVE,
            PERM_PAYMENT_RUN_APPROVE,
            PERM_PAYMENT_EXECUTE,
            PERM_PAYMENT_VOID,
            PERM_PAYMENT_RECORD_EXTERNAL,
        }
    ),
    ROLE_AP_CLERK: frozenset(),  # clerk holds no sensitive permission
}


def permissions_for_role(
    *, name: str, organization_id: object, permissions: object
) -> frozenset[str]:
    """Resolve the effective permissions a single role confers.

    * A SYSTEM role (``organization_id is None``) resolves from the static
      ``ROLE_DEFAULT_PERMISSIONS`` map — its stored ``permissions`` column is
      ignored entirely (it's always NULL for system rows). An unknown system
      name (shouldn't happen) confers nothing.
    * A CUSTOM role (``organization_id`` set) resolves from its stored list,
      sanitized to the known catalog. ``None`` / empty → grants nothing, which
      is the inert pre-this-feature default.

    Pure; takes scalars (not the ORM object) so it's trivial to unit-test.
    """
    if organization_id is None:
        return ROLE_DEFAULT_PERMISSIONS.get(name, frozenset())
    return frozenset(sanitize_permissions(permissions))


def effective_permissions(roles: object) -> frozenset[str]:
    """Union the permissions granted by every role a user holds.

    ``roles`` is an iterable of objects exposing ``.name`` /
    ``.organization_id`` / ``.permissions`` (the ORM ``Role``, or any stand-in
    with those attributes). The union is what ``require_permission`` checks and
    what ``GET /api/auth/me`` exposes to the SPA.
    """
    out: set[str] = set()
    for role in roles or ():
        out |= permissions_for_role(
            name=getattr(role, "name", ""),
            organization_id=getattr(role, "organization_id", None),
            permissions=getattr(role, "permissions", None),
        )
    return frozenset(out)


def sanitize_permissions(raw: object) -> list[str]:
    """Coerce a stored / submitted permission list to known catalog entries.

    Drops anything not in the catalog (a stale name from a removed permission,
    or a garbage value) and de-dupes while preserving catalog display order.
    Returns a plain ``list[str]`` suitable for JSONB storage. Never raises —
    a malformed ``roles.permissions`` must not break auth resolution.
    """
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    present = {p for p in raw if isinstance(p, str) and p in _ALL_PERMISSIONS_SET}
    return [p for p in ALL_PERMISSIONS if p in present]
