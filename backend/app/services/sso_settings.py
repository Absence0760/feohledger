"""The `Organization.settings.sso` block as an admin writes it — its single owner.

`PUT /api/organization/sso` is the only sanctioned way for a tenant to change
this block (`PATCH /api/organization` refuses the key). Before it existed the
generic PATCH merged per top-level key, so a `{"sso": {"client_secret": …}}`
body replaced the whole block: it dropped `enabled`, `sso_only`, the rest of the
IdP config and the SCIM group state, and nothing recorded that it happened.

Four rules, kept here so the router does not have to re-derive them:

1. **The client secret is write-only.** :func:`sso_status` reports whether one
   is stored and never what it is. A save that omits it, or sends it blank,
   keeps the stored one ("leave blank to keep"); clearing it is an explicit
   flag, never a side effect of an empty field.
2. **Keys the admin does not edit here are carried across.** The SCIM token
   digest and the SCIM group state are written by the SCIM machinery
   (`POST /organization/sso/scim-token`, `/scim/v2/Groups`), and the group →
   role map is carried unless the request names one. A config save can never
   un-provision a tenant's groups.
3. **Every other key is replaced, not merged.** The request states the whole
   IdP configuration; a key it does not send is removed. That is what makes the
   stored block exactly what the admin saw on the form, with nothing left over
   from a configuration they replaced.
4. **What leaves this module for the audit trail is key names.**
   :func:`changed_keys` names what a save changed; the values — one of which is
   the client secret — never reach the trail.

Pure: no DB, no network, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.sso import (
    SAML_PROTOCOL,
    SSOConfigError,
    check_sso_idp_config,
    is_sso_only,
    redirect_uri,
    saml_acs_url,
    saml_sp_entity_id,
)

CLIENT_SECRET_KEY = "client_secret"
SCIM_BEARER_HASH_KEY = "scim_bearer_hash"
SCIM_GROUPS_KEY = "scim_groups"
SCIM_GROUP_ROLE_MAP_KEY = "scim_group_role_map"

# Written by the SCIM machinery, never by this endpoint: always carried across.
CARRIED_KEYS = (SCIM_BEARER_HASH_KEY, SCIM_GROUPS_KEY)

PROTOCOLS = ("oidc", SAML_PROTOCOL)

# The free-text configuration keys a save replaces. A blank value removes the key.
TEXT_KEYS = (
    "provider",
    "discovery_url",
    "client_id",
    "idp_entity_id",
    "idp_sso_url",
    "idp_x509_cert",
    "sp_entity_id",
    "idp_slo_url",
)

# A defensive bound so a pasted blob cannot bloat the settings JSONB. A PEM
# certificate is the longest legitimate value (a few KB).
MAX_TEXT = 16_384
MAX_LABEL = 64


class SSOSettingsError(ValueError):
    """A submitted SSO setting is not acceptable.

    ``field`` names the key. The message never carries a value: the block holds
    the client secret, and an HTTP error body is routinely captured by proxies
    and APM.
    """

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


def stored_sso_block(org_settings: Mapping[str, Any] | None) -> dict:
    """``org_settings["sso"]`` as a fresh dict, or ``{}`` when it is not one."""
    if not isinstance(org_settings, Mapping):
        return {}
    raw = org_settings.get("sso")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _text(value: object, *, field: str, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SSOSettingsError(f"sso.{field} must be text.", field=field)
    value = value.strip()
    if len(value) > limit:
        raise SSOSettingsError(f"sso.{field} is too long.", field=field)
    return value or None


def _email_domains(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SSOSettingsError(
            "sso.allowed_email_domains must be a list of domains.", field="allowed_email_domains"
        )
    out: list[str] = []
    for raw in value:
        domain = _text(raw, field="allowed_email_domains", limit=253)
        if domain is None:
            continue
        domain = domain.lower().lstrip("@")
        if not domain or any(ch.isspace() or ch in "@/:" for ch in domain) or "." not in domain:
            raise SSOSettingsError(
                "sso.allowed_email_domains must hold bare domains, such as example.com.",
                field="allowed_email_domains",
            )
        if domain not in out:
            out.append(domain)
    return out


def _cert_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SSOSettingsError(
            "sso.idp_x509_cert_multi must be a list of certificates.",
            field="idp_x509_cert_multi",
        )
    return [c for c in (_text(v, field="idp_x509_cert_multi") for v in value) if c]


def _role_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise SSOSettingsError(
            "sso.scim_group_role_map must map group names to role names.",
            field=SCIM_GROUP_ROLE_MAP_KEY,
        )
    out: dict[str, str] = {}
    for group, role in value.items():
        group_name = _text(group, field=SCIM_GROUP_ROLE_MAP_KEY, limit=256)
        role_name = _text(role, field=SCIM_GROUP_ROLE_MAP_KEY, limit=MAX_LABEL)
        if not group_name or not role_name:
            raise SSOSettingsError(
                "sso.scim_group_role_map entries need a group name and a role name.",
                field=SCIM_GROUP_ROLE_MAP_KEY,
            )
        out[group_name] = role_name
    return out


def build_sso_block(
    stored: Mapping[str, Any],
    *,
    enabled: object,
    sso_only: object,
    protocol: object,
    allowed_email_domains: object = None,
    idp_x509_cert_multi: object = None,
    text_values: Mapping[str, object] | None = None,
    client_secret: object = None,
    clear_client_secret: bool = False,
    scim_group_role_map: object = None,
) -> dict:
    """The block a save writes: the request's configuration plus what it carries.

    ``enabled`` and ``sso_only`` must be stated. A body that leaves them out is
    refused rather than read as ``false``: an omitted flag silently switching
    SSO off is the defect this endpoint replaces.

    Raises :class:`SSOSettingsError` naming the offending key, never its value.
    """
    for name, flag in (("enabled", enabled), ("sso_only", sso_only)):
        if not isinstance(flag, bool):
            raise SSOSettingsError(f"sso.{name} is required and must be true or false.", field=name)

    proto = _text(protocol, field="protocol") or "oidc"
    proto = proto.lower()
    if proto not in PROTOCOLS:
        raise SSOSettingsError("sso.protocol must be oidc or saml.", field="protocol")

    block: dict[str, Any] = {"enabled": enabled, "sso_only": sso_only, "protocol": proto}
    values = text_values or {}
    for key in TEXT_KEYS:
        limit = MAX_LABEL if key == "provider" else MAX_TEXT
        value = _text(values.get(key), field=key, limit=limit)
        if value is not None:
            block[key] = value

    domains = _email_domains(allowed_email_domains)
    if domains:
        block["allowed_email_domains"] = domains
    certs = _cert_list(idp_x509_cert_multi)
    if certs:
        block["idp_x509_cert_multi"] = certs

    # The secret: replaced only by a non-blank value, removed only on request.
    if client_secret is not None and not isinstance(client_secret, str):
        raise SSOSettingsError("sso.client_secret must be text.", field=CLIENT_SECRET_KEY)
    new_secret = (client_secret or "").strip()
    if new_secret and clear_client_secret:
        raise SSOSettingsError(
            "Send a new client_secret or clear_client_secret, not both.", field=CLIENT_SECRET_KEY
        )
    if len(new_secret) > MAX_TEXT:
        raise SSOSettingsError("sso.client_secret is too long.", field=CLIENT_SECRET_KEY)
    if new_secret:
        block[CLIENT_SECRET_KEY] = new_secret
    elif not clear_client_secret and stored.get(CLIENT_SECRET_KEY):
        block[CLIENT_SECRET_KEY] = stored[CLIENT_SECRET_KEY]

    if scim_group_role_map is None:
        if SCIM_GROUP_ROLE_MAP_KEY in stored:
            block[SCIM_GROUP_ROLE_MAP_KEY] = stored[SCIM_GROUP_ROLE_MAP_KEY]
    else:
        role_map = _role_map(scim_group_role_map)
        if role_map:
            block[SCIM_GROUP_ROLE_MAP_KEY] = role_map

    for key in CARRIED_KEYS:
        if key in stored:
            block[key] = stored[key]
    return block


def _comparable(block: Mapping[str, Any]) -> dict:
    """``block`` with the spellings that mean the same thing folded together.

    An absent key, an empty list and a blank string all mean "unset", and an
    absent protocol means OIDC, so a first save through this endpoint does not
    report keys it only re-spelled.
    """
    out = {k: v for k, v in block.items() if v not in (None, "", [], {})}
    out.setdefault("protocol", "oidc")
    out["enabled"] = bool(block.get("enabled"))
    out["sso_only"] = bool(block.get("sso_only"))
    return out


def changed_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """The names of the keys a save added, removed or changed — never a value."""
    a, b = _comparable(before), _comparable(after)
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def idp_config_missing(org_settings: Mapping[str, Any] | None) -> list[str]:
    """The IdP keys the selected protocol still lacks, as if SSO were on.

    Lets the admin panel say what stands between this block and a working SSO
    button, using the same check sign-in uses. Key names only.
    """
    block = stored_sso_block(org_settings)
    try:
        check_sso_idp_config({"sso": {**block, "enabled": True}})
    except SSOConfigError as exc:
        return list(exc.fields) or ["sso"]
    return []


def sso_status(org_settings: Mapping[str, Any] | None, *, tenant_slug: str) -> dict:
    """The secret-free view of the block, for the admin panel.

    The client secret is reported as ``client_secret_configured`` only, and the
    SCIM token as ``scim_token_configured``. The three ``*_uri`` / ``*_url``
    values are what the admin registers at the IdP, computed here so the panel
    cannot drift from what the handlers send.
    """
    block = stored_sso_block(org_settings)
    domains = block.get("allowed_email_domains")
    certs = block.get("idp_x509_cert_multi")
    role_map = block.get(SCIM_GROUP_ROLE_MAP_KEY)
    raw_protocol = block.get("protocol")
    protocol = raw_protocol.lower() if isinstance(raw_protocol, str) and raw_protocol else "oidc"

    def text(key: str) -> str | None:
        value = block.get(key)
        return value if isinstance(value, str) and value else None

    return {
        "enabled": bool(block.get("enabled")),
        "sso_only": bool(block.get("sso_only")),
        "protocol": protocol if protocol in PROTOCOLS else "oidc",
        "provider": text("provider"),
        "allowed_email_domains": [d for d in domains if isinstance(d, str)]
        if isinstance(domains, list)
        else [],
        "discovery_url": text("discovery_url"),
        "client_id": text("client_id"),
        "client_secret_configured": bool(text(CLIENT_SECRET_KEY)),
        "idp_entity_id": text("idp_entity_id"),
        "idp_sso_url": text("idp_sso_url"),
        "idp_x509_cert": text("idp_x509_cert"),
        "idp_x509_cert_multi": [c for c in certs if isinstance(c, str)]
        if isinstance(certs, list)
        else [],
        "sp_entity_id": text("sp_entity_id"),
        "idp_slo_url": text("idp_slo_url"),
        "scim_group_role_map": {str(k): str(v) for k, v in role_map.items() if isinstance(v, str)}
        if isinstance(role_map, Mapping)
        else {},
        "scim_token_configured": bool(block.get(SCIM_BEARER_HASH_KEY)),
        "password_sign_in_closed": is_sso_only(org_settings),
        "idp_config_missing": idp_config_missing(org_settings),
        "oidc_redirect_uri": redirect_uri(tenant_slug, org_settings),
        "saml_acs_url": saml_acs_url(),
        "saml_sp_entity_id": text("sp_entity_id") or saml_sp_entity_id(tenant_slug),
    }
