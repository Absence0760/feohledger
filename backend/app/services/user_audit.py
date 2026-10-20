"""The payloads of the `user.*` audit rows a user-account change writes.

Two surfaces change a user account — the admin API (`api/admin.py`) and the
IdP over SCIM (`api/scim.py`) — and both build these rows here, so an auditor
filtering on the action sees one shape whichever wrote it. A SCIM row carries
`via: "scim"` and a null `actor_id` (the IdP's bearer token is not a user),
the convention the SCIM group-role rows already follow.

What is recorded is what an auditor needs to answer "who was removed, and what
could they do": each changed field as before AND after, and on deletion the
address and roles the account held, since the row it pointed at is gone. The
address is recorded the way the `auth.*` rows already record it; `full_name`
is named in `changed_fields` but its value stays out, the rule `user.updated`
has always followed. `docs/decisions.md` §290.
"""

from __future__ import annotations


def user_updated_details(
    changed_fields: list[str],
    *,
    previous_email: str,
    email: str,
    previous_role_names: list[str],
    role_names: list[str],
    previous_is_active: bool,
    is_active: bool,
    via: str | None = None,
    **extra: object,
) -> dict:
    """Values appear only for the fields that changed."""
    roles_changed = "role_names" in changed_fields
    active_changed = "is_active" in changed_fields
    details: dict = {
        "changed_fields": changed_fields,
        "role_names": sorted(role_names) if roles_changed else None,
        "previous_role_names": sorted(previous_role_names) if roles_changed else None,
        "is_active": is_active if active_changed else None,
        "previous_is_active": previous_is_active if active_changed else None,
    }
    if "email" in changed_fields:
        details["email"] = email
        details["previous_email"] = previous_email
    if via is not None:
        details["via"] = via
    details.update(extra)
    return details


def user_deleted_details(*, email: str, role_names: list[str], was_active: bool) -> dict:
    """Who the deleted account was, captured before the row goes."""
    return {"email": email, "role_names": sorted(role_names), "was_active": was_active}
