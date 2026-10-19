"""Which ERP company a link to an ERP record was made under (docs/decisions.md §284).

``gl_accounts.erp_account_id`` and ``vendors.erp_vendor_id`` are the ERP's own
ids, and an id only means something inside the company that issued it.
QuickBooks numbers accounts AND vendors per company with small integers, so a
sandbox company and a production company share most of them: sandbox "7" is
Office Supplies while production "7" is Advertising. A link carried across a
reconnect to another company therefore still resolves, and posts the bill to
whatever the new company calls "7".

So every link records the **connection realm** it was made under
(``erp_link_realm``), and a link only counts while the tenant is connected to
that same realm:

* the chart and vendor syncs re-judge any link from another realm by evidence
  (``services/gl_account_sync``, ``services/vendor_sync``) and record what they
  changed;
* the push resolves only links of the current realm (``services/erp``,
  ``gl_chart.resolve_erp_account_ids``), so an unverified id is refused
  ``account_not_linked`` / ``vendor_not_linked`` rather than posted.

A realm is ``"<adapter key>:<company identity>"``. The identity is read from
NON-secret configuration only, so it can be stored on every row and in audit
rows: the OAuth consent's provider company id (QuickBooks ``realmId``, Xero
tenant, Sage business, Blackbaud environment) or the company fields a direct
adapter is configured with. Merge.dev's ids are Merge-assigned UUIDs, unique
across every linked account, so its identity is the adapter alone.

``None`` means "no company can be named": the ``mock`` adapter (its ids are
fixtures and it posts nowhere), or an OAuth ERP with no completed consent
(which cannot post at all: ``OAuthErpAdapter.external_tenant_id`` raises). Links
are not filtered under ``None`` — refusing them would only replace the
``not_connected`` refusal with a misleading ``vendor_not_linked``.

**Links made before migration 0115 carry no realm (NULL).** The migration
recorded, per organization, the realm its ERP config named at that moment
(``settings.erp.legacy_link_realm``). The push trusts a NULL-realm link only
while the current realm is still that one: a tenant that never changed company
keeps posting without a re-sync, and one that has since connected anywhere else
has its NULL links refused until a sync re-judges them. The syncs never trust a
NULL link on that basis — they re-judge it by evidence and stamp the realm.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import or_

from app.services.erp_adapters.catalog import LEGACY_REALM_KEY
from app.services.erp_adapters.dispatcher import MOCK_ADAPTER_KEY, resolve_adapter_key

__all__ = [
    "LEGACY_REALM_KEY",
    "REALM_MAX_LENGTH",
    "connection_realm",
    "legacy_links_trusted",
    "link_is_live",
    "live_link_clause",
]

#: Width of the ``erp_link_realm`` columns.
REALM_MAX_LENGTH = 255

#: Adapters connected through OAuth: the consent's provider company id.
_OAUTH_ADAPTERS = frozenset({"quickbooks_online", "xero", "sage_accounting", "blackbaud_fe_nxt"})

#: Direct adapters: the configured fields that name the company. Every field an
#: id is scoped by — a NetSuite account, a Business Central tenant +
#: environment + company, an Intacct company, a SYSPRO / Sage ZA host + company.
_DIRECT_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "netsuite": ("account_id",),
    "dynamics_365_bc": ("base_url", "tenant_id", "environment", "company_id"),
    "sage_intacct": ("company_id",),
    "syspro": ("base_url", "company_id"),
    "sage_accounting_za": ("base_url", "company_id"),
}

#: A field's value when the adapter itself applies a default for a blank one,
#: so saving the default explicitly does not read as another company.
_FIELD_DEFAULTS: dict[tuple[str, str], str] = {
    ("dynamics_365_bc", "environment"): "production",
}


def _norm(value: Any) -> str:
    return str(value or "").strip().rstrip("/").lower()


def connection_realm(erp_config: Any) -> str | None:
    """The realm ``erp_config`` (a resolved or stored ``settings.erp``) names."""
    if not isinstance(erp_config, dict) or not erp_config:
        return None
    key = resolve_adapter_key(erp_config)
    if key == MOCK_ADAPTER_KEY:
        return None
    if key in _OAUTH_ADAPTERS:
        oauth = erp_config.get("oauth")
        company = str((oauth or {}).get("external_tenant_id") or "").strip()
        if not company:
            return None
        identity = company
    elif key in _DIRECT_IDENTITY_FIELDS:
        identity = "|".join(
            _norm(erp_config.get(f) or _FIELD_DEFAULTS.get((key, f)))
            for f in _DIRECT_IDENTITY_FIELDS[key]
        )
    else:
        # Merge.dev (globally unique ids) and any adapter that has not named
        # its company fields: the adapter is the realm.
        identity = ""
    realm = f"{key}:{identity}"
    if len(realm) > REALM_MAX_LENGTH:
        realm = f"{key}:sha256:{hashlib.sha256(identity.encode()).hexdigest()}"
    return realm


def legacy_links_trusted(erp_config: Any, realm: str | None) -> bool:
    """May the push treat a NULL-realm (pre-0115) link as belonging to ``realm``?"""
    if realm is None or not isinstance(erp_config, dict):
        return False
    return erp_config.get(LEGACY_REALM_KEY) == realm


def link_is_live(link_realm: str | None, realm: str | None, *, trust_legacy: bool) -> bool:
    """Does a link stamped ``link_realm`` count under the current ``realm``?"""
    if realm is None:
        return True
    if link_realm is None:
        return trust_legacy
    return link_realm == realm


def live_link_clause(column, realm: str | None, *, trust_legacy: bool):
    """:func:`link_is_live` as a SQL filter on an ``erp_link_realm`` column, or
    None when nothing is filtered (``realm`` None)."""
    if realm is None:
        return None
    if trust_legacy:
        return or_(column == realm, column.is_(None))
    return column == realm
