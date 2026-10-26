"""The ERP provider catalogue: what Organization → ERP offers and asks for.

The single source of truth for the ERP setup form. ``GET
/api/organization/erp/providers`` serves it, and the page renders one dropdown
entry and one set of inputs per entry. Before it, the page carried its own
hardcoded ``ERP_TYPES`` list of mostly enterprise ERPs, most of which had no
direct adapter, so choosing one fell silently into a generic form or Merge.dev.

It also names **which ERP fields are secrets** (:data:`SECRET_KEYS`). None of
them is stored in ``settings.erp``: they are sealed in the control-plane
``provider_credentials`` table (``services/provider_credentials``, decisions
§266), written only through the audited ``PUT
/api/organization/credentials/erp`` and never read back. What this module
decides about them is *when they stop applying*: a save that points the block
at a different ERP or a different destination (:func:`destination_keys`) drops
them (:func:`secrets_to_drop`). ``settings.erp.oauth`` — the consent metadata
``services/erp_oauth`` writes beside its sealed tokens — is never writable
through the settings API, and reads see only ``{"connected": bool}``.

Pure: no DB, no request, no I/O. ``tests/test_erp_catalog.py`` keeps it in step
with the adapter registry and with ``provider_credentials.SECRET_FIELDS``.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

#: The OAuth consent metadata block. Its only writer is ``services/erp_oauth``;
#: its tokens are sealed in ``provider_credentials``, not stored here.
OAUTH_KEY = "oauth"
#: ``settings.erp`` key: the ERP company every pre-0115 link is assumed to
#: belong to (``services/erp_link_realm``). Written only by migration 0115.
LEGACY_REALM_KEY = "legacy_link_realm"
#: Token keys that must never sit in the JSONB OAuth block.
_OAUTH_TOKEN_KEYS = frozenset({"access_token", "refresh_token"})

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


#: ``settings.erp`` keys that say WHERE the stored credentials are sent: the
#: host itself (``base_url``), or a value an adapter builds the host or the
#: target books from (NetSuite's ``account_id`` is a hostname label; Business
#: Central's ``tenant_id`` / ``environment`` / ``company_id`` are URL path
#: segments; SYSPRO and Intacct log in to ``company_id``). A save that changes
#: any of them is a NEW connection: the sealed secrets are dropped and have to
#: be typed again
#: (:func:`secrets_to_drop`; the inbound webhook key, :data:`EXTRA_SECRET_KEYS`,
#: is never sent outbound and so survives), and a connection test against the
#: changed form gets none of them. Without this, ``{"type": "syspro",
#: "base_url": "https://attacker.tld"}`` from an admin (or a stolen admin
#: token) sent the stored password to attacker.tld through
#: ``POST /organization/test-erp`` or a settings PATCH. A catalogue field that
#: names a host belongs here: ``tests/test_erp_catalog.py`` fails on any
#: ``*_url`` / ``*host*`` field left out.
#:
#: This is the DEFAULT, by name, for every provider — including for a key the
#: provider's form does not render, since an adapter may still read it. A
#: provider narrows it only by declaring one of its own fields
#: ``destination=False`` (:func:`destination_keys`), and only with evidence that
#: no value of that field moves the credentials off the vendor's own hosts.
#: QuickBooks Online's ``environment`` is the one such field: it picks between
#: two Intuit hosts and nothing else (decisions §316).
#: ``tests/test_erp_catalog.py`` pins the opt-outs, so a new one is a reviewed
#: change.
DESTINATION_KEYS: frozenset[str] = frozenset(
    {"base_url", "tenant_id", "account_id", "company_id", "environment"}
)


def _field(
    name: str,
    *,
    secret: bool = False,
    required: bool = True,
    placeholder: str | None = None,
    help_key: str | None = None,
    label_key: str | None = None,
    options: list[str] | None = None,
    destination: bool | None = None,
    show_when: dict[str, Any] | None = None,
    stored_default: str | None = None,
) -> dict[str, Any]:
    """One form field. ``label_key`` is a frontend i18n ``MessageKey``.

    ``destination`` defaults to membership of :data:`DESTINATION_KEYS`; pass it
    only to override that for this provider (see :func:`destination_keys`).

    ``show_when`` (``{"field": name, "values": [...]}``) makes the field apply
    only while another field of the same provider holds one of ``values``: the
    form hides it, and does not require it, otherwise. ``stored_default`` is
    what a SAVED block without this key means (the adapter's reading of the
    absent key), which the form shows instead of the first option; a new
    connection still starts on the first option.
    """
    out: dict[str, Any] = {
        "name": name,
        "label_key": label_key or _EXISTING_LABEL_KEYS.get(name, f"org.erp.field.{name}"),
        "secret": secret,
        "required": required,
        # Changing a destination field starts a new connection: the form then
        # asks for the secrets again rather than claiming they are kept. The
        # backend's own rule (`destination_keys`) reads this same flag.
        "destination": (name in DESTINATION_KEYS) if destination is None else destination,
    }
    if placeholder is not None:
        out["placeholder"] = placeholder
    if help_key is not None:
        out["help_key"] = help_key
    if options is not None:
        out["options"] = options
    if show_when is not None:
        out["show_when"] = show_when
    if stored_default is not None:
        out["stored_default"] = stored_default
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

#: NetSuite's two sign-in methods (``erp_adapters/netsuite.AUTH_METHODS``):
#: each one's fields apply only while it is the chosen ``auth_method``.
_NETSUITE_OAUTH2: dict[str, Any] = {"field": "auth_method", "values": ["oauth2_m2m"]}
_NETSUITE_TBA: dict[str, Any] = {"field": "auth_method", "values": ["tba"]}

#: Every direct ERP, in dropdown order within its region group.
ERP_PROVIDERS: list[dict[str, Any]] = [
    {
        "key": "quickbooks_online",
        "label": "QuickBooks Online",
        "regions": ["US"],
        "auth": "oauth",
        "fields": [
            *_byo_app_fields(),
            # Not a destination: it only picks between Intuit's sandbox and
            # production API hosts (the adapter maps any other value to
            # production), and the OAuth endpoints are Intuit constants, so no
            # value sends a credential anywhere but Intuit. Treated as one, it
            # dropped a bring-your-own-app client_secret on every switch, which
            # made a sandbox connection impossible to save (decisions §316).
            _field(
                "environment",
                options=["production", "sandbox"],
                placeholder="production",
                destination=False,
            ),
            # The GL codes (our chart) of the accounts a FeohLedger payment is
            # written back from as a QuickBooks BillPayment (decisions §295).
            # Not required: without one, write-back refuses
            # `payment_account_not_mapped` and the bill stays unpaid there.
            _field(
                "bill_payment_bank_account",
                required=False,
                help_key="org.erp.help.billPaymentBankAccount",
            ),
            _field(
                "bill_payment_card_account",
                required=False,
                help_key="org.erp.help.billPaymentCardAccount",
            ),
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
        # Sage's v3.1 API: the UK, Ireland, Canada and the EU countries it lists.
        # Not the US, where Sage retired the product on 2024-12-31, and not
        # South Africa (a separate product, below). Outside both region groups,
        # so the picker lists it under "Other".
        "key": "sage_accounting",
        "label": "Sage Business Cloud Accounting",
        "regions": ["GB"],
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
            _field("company_currency", required=False, placeholder="ZAR"),
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
            # OAuth 2.0 client credentials first: from NetSuite 2027.1 it is the
            # only method a NEW integration record can use. A block saved
            # before this choice existed is TBA (the adapter reads an absent
            # key that way, and so does the form). Not a destination: either
            # method signs in to the same account's hosts.
            _field(
                "auth_method",
                options=["oauth2_m2m", "tba"],
                stored_default="tba",
                help_key="org.erp.help.netsuiteAuthMethod",
            ),
            _field("client_id", show_when=_NETSUITE_OAUTH2),
            _field(
                "certificate_id",
                show_when=_NETSUITE_OAUTH2,
                help_key="org.erp.help.netsuiteCertificateId",
            ),
            _field(
                "private_key",
                secret=True,
                show_when=_NETSUITE_OAUTH2,
                placeholder="-----BEGIN PRIVATE KEY-----",
                help_key="org.erp.help.netsuitePrivateKey",
            ),
            _field("consumer_key", show_when=_NETSUITE_TBA),
            _field("consumer_secret", secret=True, show_when=_NETSUITE_TBA),
            _field("token_id", show_when=_NETSUITE_TBA),
            _field("token_secret", secret=True, show_when=_NETSUITE_TBA),
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


#: Every catalogue field marked ``secret``, plus :data:`EXTRA_SECRET_KEYS`. A
#: key name is a secret for every provider once ANY provider marks it secret:
#: the dispatcher reads whichever it needs, so treating it by name is the
#: fail-closed direction. Each one is sealed in ``provider_credentials``
#: (``services/provider_credentials.SECRET_FIELDS["erp"]`` must contain every
#: name here — ``tests/test_erp_catalog.py``), never stored in ``settings.erp``.
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
    """Does the stored config hold a completed OAuth consent?

    Read from the block's metadata: the consent callback writes the
    ``connection_id`` together with the sealed tokens, and disconnect removes
    both, so the tokens themselves never have to be opened to answer.
    """
    if not isinstance(erp_config, dict):
        return False
    block = erp_config.get(OAUTH_KEY)
    return isinstance(block, dict) and bool(block.get("connection_id"))


def public_erp_config(erp_config: Any) -> Any:
    """``settings.erp`` as a settings read shows it: the OAuth block hidden.

    The secrets are not in the block at all (they are sealed, and
    ``provider_credentials.strip_secrets`` removes any copy). What is left to
    hide is the OAuth metadata — its ``connection_id`` is the capability the
    refresher checks (``services/erp_oauth``), so no endpoint returns it — which
    reads as ``{"connected": bool}``. Returns a new dict.
    """
    if not isinstance(erp_config, dict):
        return erp_config
    out = {k: deepcopy(v) for k, v in erp_config.items() if k != OAUTH_KEY}
    if OAUTH_KEY in erp_config:
        out[OAUTH_KEY] = {"connected": oauth_connected(erp_config)}
    return out


def _destination_value(value: Any) -> Any:
    """A destination key's value for comparison: blank and absent are the same."""
    if isinstance(value, str):
        return value.strip() or None
    return value


def same_erp(stored: Any, incoming: Any) -> bool:
    """Do two blocks select the same catalogue entry and the same ERP type?"""
    if not isinstance(stored, dict) or not isinstance(incoming, dict):
        return False
    return catalog_key(stored) == catalog_key(incoming) and stored.get("type") == incoming.get(
        "type"
    )


def destination_keys(erp_config: Any) -> frozenset[str]:
    """The ``settings.erp`` keys that are a destination for this block's ERP.

    :data:`DESTINATION_KEYS`, plus any field the provider's entry marks
    ``destination``, minus any field it explicitly marks not one. The by-name
    default still covers every key the provider does not declare, rendered or
    not: narrowing is opt-in per field, never inferred from what a form shows.
    """
    entry = provider(catalog_key(erp_config)) if isinstance(erp_config, dict) else None
    fields = entry["fields"] if entry else []
    declared = frozenset(f["name"] for f in fields if f["destination"])
    excluded = frozenset(f["name"] for f in fields if not f["destination"])
    return (DESTINATION_KEYS | declared) - excluded


def same_connection(stored: Any, incoming: Any) -> bool:
    """Does ``incoming`` name the same ERP, at the same destination, as ``stored``?

    The one condition under which the sealed secrets stay with the block across
    a save (:func:`secrets_to_drop`), and under which a connection test may use
    them (``provider_credentials.config_for_connection_test``). Which keys are
    the destination is per ERP (:func:`destination_keys`); both blocks select
    the same ERP by this point, so either one names it.
    """
    if not same_erp(stored, incoming):
        return False
    return all(
        _destination_value(stored.get(key)) == _destination_value(incoming.get(key))
        for key in destination_keys(stored)
    )


def secrets_to_drop(stored: Any, merged: Any) -> frozenset[str]:
    """The sealed ``erp`` secrets a save from ``stored`` to ``merged`` removes.

    * Same ERP, same destination — the secrets of a sign-in method the save
      switched away from (:func:`secrets_switched_off`): a NetSuite block moved
      from TBA to OAuth 2.0 must not keep TBA's token secret sealed and unused.
    * Same ERP, new destination — every secret but the inbound webhook's HMAC
      key (:data:`EXTRA_SECRET_KEYS`), which verifies what the ERP sends US and
      is never sent anywhere. Without this, ``{"type": "syspro", "base_url":
      "https://attacker.tld"}`` from an admin (or a stolen admin token) sent the
      stored ``operator_password`` to attacker.tld on the next push.
    * A different ERP, or the ERP cleared — every secret: one ERP's
      ``client_secret`` must not become another's app credentials.

    The OAuth tokens are never in this set. They are bound to their provider
    and ``connection_id`` (``services/erp_oauth`` refuses any other), are sent
    only to that provider's own hosts, and are removed by the OAuth disconnect,
    which revokes them first.

    A stored block that selects no ERP yet binds nothing, so nothing is dropped:
    secrets entered before the first configuration save (the credentials
    endpoint accepts them in either order) belong to the ERP that save names.
    """
    if not isinstance(stored, dict) or not (stored.get("type") or stored.get("integration_method")):
        return frozenset()
    if same_connection(stored, merged):
        return secrets_switched_off(stored, merged)
    if same_erp(stored, merged):
        return SECRET_KEYS - EXTRA_SECRET_KEYS
    return SECRET_KEYS


def secrets_switched_off(stored: Any, merged: Any) -> frozenset[str]:
    """The provider's secret fields that applied to ``stored`` and no longer
    apply to ``merged``, by their catalogue ``show_when``.

    Only a field the save turned OFF: a secret stored for a method the block
    was not yet on (the credentials endpoint takes secrets before or after the
    configuration save) is left for the save that selects it. An absent
    controlling key reads as its ``stored_default``, as the adapter reads it.
    """
    if not (isinstance(stored, dict) and isinstance(merged, dict)):
        return frozenset()
    entry = provider(catalog_key(stored))
    if entry is None:
        return frozenset()
    by_name = {f["name"]: f for f in entry["fields"]}

    def value(cfg: dict, name: str) -> Any:
        current = cfg.get(name)
        if current is None or (isinstance(current, str) and not current.strip()):
            return by_name.get(name, {}).get("stored_default")
        return current

    off = {
        f["name"]
        for f in entry["fields"]
        if f["secret"]
        and (cond := f.get("show_when")) is not None
        and value(stored, cond["field"]) in cond["values"]
        and value(merged, cond["field"]) not in cond["values"]
    }
    return frozenset(off) & SECRET_KEYS


def merge_erp_update(stored: Any, incoming: dict) -> dict:
    """The ``settings.erp`` CONFIGURATION a save of ``incoming`` produces.

    Secrets are not part of it: the caller has refused any non-blank secret in
    ``incoming`` (they are written through ``PUT
    /api/organization/credentials/erp``), and any blank one is dropped here.

    * Non-secret keys come from ``incoming``. For the same ERP, a stored key
      the form does not render (and so never sends) is kept when omitted.
    * ``legacy_link_realm`` (``services/erp_link_realm``) is never taken from
      ``incoming``; the stored value is kept for the same ERP only.
    * ``oauth`` is never taken from ``incoming``; the stored block is kept, even
      when the ERP is cleared or switched — only the OAuth disconnect removes it,
      after revoking at the provider.
    """
    stored = stored if isinstance(stored, dict) else {}
    merged: dict[str, Any] = {
        key: value
        for key, value in incoming.items()
        if key not in (OAUTH_KEY, LEGACY_REALM_KEY) and key not in SECRET_KEYS
    }
    if same_erp(stored, incoming) and LEGACY_REALM_KEY in stored:
        # Which company the pre-0115 ERP links belong to (services/
        # erp_link_realm): written by that migration only. Kept for the same
        # ERP, never taken from a save — and dropped with the block when the
        # ERP changes, since the old links belong to neither.
        merged[LEGACY_REALM_KEY] = stored[LEGACY_REALM_KEY]
    if same_erp(stored, incoming):
        # Keys the form never renders (an API-set Blackbaud
        # `transaction_code_values`, say) survive a save that omits them. A
        # rendered field is always sent, so leaving it out is not a request to
        # keep it; clearing it in the form still clears it.
        entry = provider(catalog_key(incoming))
        rendered = {f["name"] for f in entry["fields"]} if entry else set()
        for key, value in stored.items():
            if (
                key in incoming
                or key in rendered
                or key in SECRET_KEYS
                or key in (OAUTH_KEY, LEGACY_REALM_KEY)
            ):
                continue
            merged[key] = value
    if OAUTH_KEY in stored:
        # Minus any token copy a pre-store row or a hand edit left in it, so a
        # save never re-persists one (the tokens are sealed, not stored here).
        oauth = stored[OAUTH_KEY]
        if isinstance(oauth, dict):
            oauth = {k: v for k, v in oauth.items() if k not in _OAUTH_TOKEN_KEYS}
        merged[OAUTH_KEY] = oauth
    return merged
