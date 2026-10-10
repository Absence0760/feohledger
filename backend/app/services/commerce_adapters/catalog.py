"""The commerce provider catalogue: what Organization → Commerce offers and asks for.

Each provider declares its own entry beside its adapter
(``base.CommerceProviderSpec``, passed to ``register_commerce_adapter``), so a
new provider edits no shared list beyond its secret names. This module gathers
those specs and owns how a save of ``settings.commerce`` treats configuration
and secrets — the rule ``settings.erp`` follows (``erp_adapters/catalog``,
decisions §271):

* ``settings.commerce`` is CONFIGURATION only. Every field a spec marks
  ``secret`` is sealed in ``provider_credentials`` (block ``commerce``, decisions
  §266), written only through ``PUT /api/organization/credentials/commerce`` and
  never read back; ``provider_credentials.SECRET_FIELDS["commerce"]`` must name
  every one (``tests/test_commerce_adapters.py``).
* A save that changes the provider or any destination key drops the sealed
  secrets (:func:`secrets_to_drop`): a stored token must never follow a changed
  shop domain to another host.

``settings.commerce`` shape::

    {
        "provider": "mock" | "shopify" | ...,
        ...the provider's non-secret fields,
        "location_id": "<provider location for receipts in any entity>",
        "entity_locations": {"<entity uuid>": "<provider location>"},
    }

The two location keys are written by ``PUT /api/commerce/locations`` and never
rendered by the connection form, so a connection save that omits them keeps
them — for the same provider only. A provider switch drops them: one platform's
location ids mean nothing to another.

Pure apart from the registry import.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.commerce_adapters.registry import registered_adapters

PROVIDER_KEY = "provider"
LOCATION_KEY = "location_id"
ENTITY_LOCATIONS_KEY = "entity_locations"

#: Destination keys every provider shares by name. A provider adds its own
#: through ``CommerceProviderSpec.destination_keys``; ``tests/test_commerce_catalog``
#: fails on a ``*_url`` / ``*domain*`` / ``*host*`` field left out of both.
COMMON_DESTINATION_KEYS: frozenset[str] = frozenset(
    {"base_url", "store_url", "shop_domain", "domain_prefix", "account_id", "environment"}
)

#: Field names with a label already in the frontend catalogues
#: (``org.commerce.field.<name>``). A field outside this set still works; its
#: provider PR adds the ``org.commerce.field.<name>`` key in every locale.
KNOWN_FIELD_LABELS: frozenset[str] = frozenset(
    {
        "access_token",
        "account_id",
        "api_key",
        "api_secret",
        "api_version",
        "application_key",
        "base_url",
        "client_id",
        "client_secret",
        "consumer_key",
        "consumer_secret",
        "domain_prefix",
        "environment",
        "shop_domain",
        "store_url",
    }
)


def field(
    name: str,
    *,
    secret: bool = False,
    required: bool = True,
    placeholder: str | None = None,
    help_key: str | None = None,
    label_key: str | None = None,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """One form field. ``label_key`` / ``help_key`` are frontend ``MessageKey``s."""
    out: dict[str, Any] = {
        "name": name,
        "label_key": label_key or f"org.commerce.field.{name}",
        "secret": secret,
        "required": required,
    }
    if placeholder is not None:
        out["placeholder"] = placeholder
    if help_key is not None:
        out["help_key"] = help_key
    if options is not None:
        out["options"] = list(options)
    return out


def all_providers() -> list[dict[str, Any]]:
    """Every registered provider's entry, ``mock`` last (it is the local default)."""
    specs = [cls.spec for cls in registered_adapters().values()]
    specs.sort(key=lambda s: (s.key == "mock", s.label.lower()))
    return [{**s.to_dict(), "available": s.selectable} for s in specs]


def provider(key: str | None) -> dict[str, Any] | None:
    for entry in all_providers():
        if entry["key"] == key:
            return entry
    return None


def secret_keys() -> frozenset[str]:
    """Every field name any registered provider marks secret.

    A name is a secret for every provider once ANY provider marks it secret —
    treating it by name is the fail-closed direction. Each must be in
    ``provider_credentials.SECRET_FIELDS["commerce"]``, the sealed store's
    allow-list (``tests/test_commerce_adapters.py``).
    """
    return frozenset(
        f["name"] for cls in registered_adapters().values() for f in cls.spec.fields if f["secret"]
    )


def _all_secret_keys() -> frozenset[str]:
    from app.services.provider_credentials import SECRET_FIELDS

    return secret_keys() | SECRET_FIELDS.get("commerce", frozenset())


def destination_keys() -> frozenset[str]:
    return COMMON_DESTINATION_KEYS | frozenset(
        k for cls in registered_adapters().values() for k in cls.spec.destination_keys
    )


def _rendered_fields(config: dict) -> frozenset[str]:
    entry = provider(config.get(PROVIDER_KEY))
    return frozenset(f["name"] for f in entry["fields"]) if entry else frozenset()


def public_commerce_config(config: Any) -> Any:
    """``settings.commerce`` as a settings read shows it: no secret keys.

    The secrets are sealed and never in the block; a stray copy (a hand edit)
    is removed here too. Returns a new dict.
    """
    if not isinstance(config, dict):
        return config
    secrets = _all_secret_keys()
    return {k: deepcopy(v) for k, v in config.items() if k not in secrets}


def _destination_value(value: Any) -> Any:
    """A destination key's value for comparison: blank and absent are the same."""
    if isinstance(value, str):
        return value.strip() or None
    return value


def same_provider(stored: Any, incoming: Any) -> bool:
    if not isinstance(stored, dict) or not isinstance(incoming, dict):
        return False
    return bool(stored.get(PROVIDER_KEY)) and stored.get(PROVIDER_KEY) == incoming.get(PROVIDER_KEY)


def same_connection(stored: Any, incoming: Any) -> bool:
    """Does ``incoming`` name the same platform, at the same destination, as ``stored``?

    The one condition under which the sealed secrets stay with the block across
    a save (:func:`secrets_to_drop`), and under which a connection test may use
    them (``provider_credentials.config_for_connection_test``).
    """
    if not same_provider(stored, incoming):
        return False
    return all(
        _destination_value(stored.get(key)) == _destination_value(incoming.get(key))
        for key in destination_keys()
    )


def secrets_to_drop(stored: Any, merged: Any) -> frozenset[str]:
    """The sealed ``commerce`` secrets a save from ``stored`` to ``merged`` removes.

    None while it names the same platform at the same destination; every one
    otherwise — a new shop domain must not receive the old shop's token, and one
    platform's key must not become another's. A stored block that names no
    platform yet binds nothing: secrets entered before the first configuration
    save belong to the platform that save names.
    """
    if not isinstance(stored, dict) or not stored.get(PROVIDER_KEY):
        return frozenset()
    if same_connection(stored, merged):
        return frozenset()
    return _all_secret_keys()


def merge_commerce_update(stored: Any, incoming: dict) -> dict:
    """The ``settings.commerce`` CONFIGURATION a connection save of ``incoming`` produces.

    Secrets are not part of it: the caller has refused any non-blank secret in
    ``incoming``, and blank ones are dropped here. For the same platform, a
    stored key the form does not render (the location mapping) is kept when
    omitted; a rendered field left out is cleared.
    """
    stored = stored if isinstance(stored, dict) else {}
    secrets = _all_secret_keys()
    merged = {k: v for k, v in incoming.items() if k not in secrets}
    if same_provider(stored, incoming):
        rendered = _rendered_fields(incoming)
        for key, value in stored.items():
            if key in incoming or key in rendered or key in secrets:
                continue
            merged[key] = value
    return merged


def changed_keys(before: Any, after: Any) -> list[str]:
    """Names (never values) of the keys a save changed, for the audit row."""
    b = before if isinstance(before, dict) else {}
    a = after if isinstance(after, dict) else {}
    return sorted(k for k in set(b) | set(a) if b.get(k) != a.get(k))


def location_for(config: Any, entity_id: object) -> str | None:
    """The provider location a receipt in ``entity_id`` is pushed to.

    The entity's own mapping first (multi-entity: a subsidiary's warehouse),
    then the org-wide ``location_id``. ``None`` = unmapped; the sync records a
    skip rather than guessing a location.
    """
    if not isinstance(config, dict):
        return None
    per_entity = config.get(ENTITY_LOCATIONS_KEY)
    if entity_id is not None and isinstance(per_entity, dict):
        mapped = per_entity.get(str(entity_id))
        if isinstance(mapped, str) and mapped.strip():
            return mapped.strip()
    default = config.get(LOCATION_KEY)
    if isinstance(default, str) and default.strip():
        return default.strip()
    return None
