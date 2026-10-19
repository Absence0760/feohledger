"""Drift guard: every route that hands an AP actor a supplier password stamps it.

The vendor bank-change dual control refuses an approval when
``vendor_change_requests.requester_provisioned_by_user_id`` — frozen at staging
from ``VendorUser.provisioned_by_user_id`` — equals the approver. That column is
allowed to be NULL, and NULL is treated as *permissive*: a request from a portal
identity nobody in AP provisioned is approved normally.

That is only safe because of an **exhaustiveness claim**: an AP actor cannot come
to know a supplier's password without passing through a route that stamps the
column. Today the claim holds — ``POST /vendors/{id}/portal-users`` (invite) and
``POST .../portal-users/{id}/reset-password`` are the only two, and both stamp.

The claim is a property of the whole ``app/`` tree, not of any one file, so
nothing about adding a third one would fail. A new "resend credentials" or
"impersonate supplier" route that set ``hashed_password`` without stamping would
reopen the exact hole, silently, and every existing test would stay green. This
scans for that.

Companion to ``tests/test_vendor_bank_change_provisioner_sod.py``, which drives
the attack itself. See ``backend/docs/supplier-portal.md`` § Credential
provenance and the BEC dual control.
"""

from __future__ import annotations

import ast
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

PROVENANCE_COLUMN = "provisioned_by_user_id"

# Assignments to `<something>.hashed_password` that are NOT an AP actor learning a
# supplier's password. Keyed by "<module path relative to app/>::<function>", each
# with the reason it is exempt — a bare path is not enough, the reason is the
# review.
EXEMPT_PASSWORD_WRITES: dict[str, str] = {
    # The supplier setting their OWN password. Deliberately does not clear the
    # stamp: this route requires the CURRENT password, which the provisioner
    # has, so clearing here would be a one-request bypass of the whole control.
    "api/portal_auth.py::portal_change_password": (
        "the supplier's own change-password; must NOT clear the stamp"
    ),
    # GDPR erasure sets the hash to None — it destroys the credential rather
    # than handing one out, so nobody gains access.
    "services/privacy_erasure.py::erase_vendor_user": "erasure nulls the credential",
    "services/privacy_erasure.py::erase_vendor_contact": "erasure nulls the credential",
    # Employee (control-plane `User`) password writes. These never touch a
    # VendorUser, but the scan is deliberately name-based rather than
    # type-inferred — a `.hashed_password =` is caught wherever it is — so they
    # are named here instead of guessed at.
    "api/admin.py::reset_user_password": "control-plane User, not a VendorUser",
    "api/auth.py::reset_password": "control-plane User, not a VendorUser",
    "api/auth.py::_apply_new_password": "control-plane User, not a VendorUser",
    "services/privacy_erasure.py::erase_user": "control-plane User, not a VendorUser",
}


def _iter_app_modules():
    for path in sorted(APP_DIR.rglob("*.py")):
        yield path, ast.parse(path.read_text(), filename=str(path))


def _owner_of(tree: ast.AST, lineno: int) -> str:
    """Name of the innermost function containing `lineno` (`<module>` if none).

    Innermost by SPAN, not by walk order: a nested helper inside a route handler
    must resolve to the helper, or an exemption granted to the handler would
    silently cover it.
    """
    spans = [
        ((node.end_lineno or node.lineno) - node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.lineno <= lineno <= (node.end_lineno or node.lineno)
    ]
    return min(spans)[1] if spans else "<module>"


def test_every_vendoruser_construction_stamps_the_provisioner():
    """`VendorUser(..., hashed_password=...)` must also set the provisioner.

    A construction site that mints a password without recording who minted it
    creates a credential with a NULL provisioner — which the approve path reads
    as "no AP actor has ever held this", the one thing that must stay true.
    """
    offenders: list[str] = []
    for path, tree in _iter_app_modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "VendorUser":
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            if "hashed_password" not in kwargs:
                continue  # SSO-only / password-less construction: nothing handed out
            if PROVENANCE_COLUMN not in kwargs:
                rel = path.relative_to(APP_DIR)
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "VendorUser is constructed with a password but no "
        f"`{PROVENANCE_COLUMN}` at: {offenders}. An AP actor who mints a "
        "supplier credential must be recorded, or the vendor bank-change dual "
        "control silently fails open for that identity — see "
        "backend/docs/supplier-portal.md § Credential provenance."
    )


def test_every_supplier_password_write_stamps_or_is_exempt():
    """`vu.hashed_password = ...` must stamp the provisioner, or be exempt.

    The exemption list is the review: each entry names WHY that write does not
    hand an AP actor a working supplier credential. A new write fails here until
    it either stamps the column or earns an entry.
    """
    offenders: list[str] = []
    for path, tree in _iter_app_modules():
        rel = str(path.relative_to(APP_DIR))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Attribute) and t.attr == "hashed_password" for t in node.targets
            ):
                continue
            fn_name = _owner_of(tree, node.lineno)
            key = f"{rel}::{fn_name}"
            if key in EXEMPT_PASSWORD_WRITES:
                continue
            # Not exempt -> the same function must stamp the provenance column.
            stamps = any(
                isinstance(n, ast.Assign)
                and any(
                    isinstance(t, ast.Attribute) and t.attr == PROVENANCE_COLUMN for t in n.targets
                )
                and _owner_of(tree, n.lineno) == fn_name
                for n in ast.walk(tree)
            )
            if not stamps:
                offenders.append(f"{key} (line {node.lineno})")

    assert not offenders, (
        "these write a password hash without recording the AP actor who minted "
        f"it and without an EXEMPT_PASSWORD_WRITES entry: {offenders}. Either "
        f"set `{PROVENANCE_COLUMN}` in the same function, or add an entry "
        "stating why this write does not hand an AP actor a working supplier "
        "credential."
    )


def test_the_exempt_list_has_not_gone_stale():
    """Every exemption must still name a real write.

    A stale entry is worse than none: it reads as a reviewed decision about code
    that has since moved, and would silently cover a *different* write that
    later lands under the same name.
    """
    seen: set[str] = set()
    for path, tree in _iter_app_modules():
        rel = str(path.relative_to(APP_DIR))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "hashed_password" for t in node.targets
            ):
                seen.add(f"{rel}::{_owner_of(tree, node.lineno)}")

    stale = sorted(set(EXEMPT_PASSWORD_WRITES) - seen)
    assert not stale, f"EXEMPT_PASSWORD_WRITES names writes that no longer exist: {stale}"
