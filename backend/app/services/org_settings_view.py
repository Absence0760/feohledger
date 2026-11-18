"""What `GET /api/organization` may serve out of `Organization.settings`.

That endpoint is gated only by `get_current_user` — no role check — and returned
the raw settings JSONB, so **every** authenticated role, `ap_clerk` included,
could read the tenant's third-party credentials: `erp.client_secret` /
`erp.api_key` / `erp.webhook_signing_secret`, `payments.credentials` +
`payments.webhook_secret`, `cards.api_key` + `cards.webhook_signing_secret`,
`extraction.api_key`, `sso.client_secret` + `sso.scim_bearer_hash`, and
`chat_notifications.webhook_url`. Any one of those is enough to act as the
tenant against a third party; the chat webhook URL alone lets its holder post
into the channel where payments are approved.

Two rules, and they answer different questions:

**`NON_ADMIN_SETTINGS` — an allow-list, not a deny-list.** A deny-list of
secret-looking key names is the wrong shape for a free-form JSONB blob that
keeps growing: the day someone adds a provider block, the leak is the default.
Here, exposure requires a deliberate edit to this file, and each entry has to
name a real non-admin consumer.

**`ALWAYS_REDACTED` — dropped for every role, admin included.** Two values are
write-only by design rather than by privilege: the chat incoming-webhook URL is
a bearer capability whose only management path is the audited
`/api/organization/chat-notifications/webhook`, and the OIDC client secret's
only writer is the audited `PUT /api/organization/sso`, which keeps the stored
value when the field is left blank. Leaving either readable here would make "no
endpoint ever returns it" false and give the settings page a silent, unaudited
second way to see it.

**Provider credentials are not in the JSONB at all any more.** The secrets of
the `erp`, `payments`, `cards`, `commerce` and `extraction` blocks live sealed
in `provider_credentials`
(`services/provider_credentials`), written only by the audited
`PUT /api/organization/credentials/{block}` and reported by
`GET /api/organization/credentials` as names-only "is set" flags. The settings
page uses "leave blank to keep" for them, so nothing needs them back. Every
secret-named key in those blocks is still stripped here for every role, admin
included, as a second line: a value that reached the JSONB by some other route
(a hand edit, a pre-0110 backup restored) must not reappear on this response.
`settings.erp.oauth` — the OAuth consent metadata `services/erp_oauth` keeps
beside its sealed tokens — reads as `{"connected": bool}` for the same reason:
its `connection_id` is the capability the token refresher checks
(`erp_adapters/catalog.public_erp_config`).

The OIDC client secret is sealed there too (block `sso`, written only by
`PUT /api/organization/sso`); the block keeps a `client_secret_configured`
marker, and `ALWAYS_REDACTED` still drops a stray `client_secret` as the same
second line.

Admins otherwise still get the settings verbatim.

Pure: no DB, no request, no I/O.
"""

from __future__ import annotations

from app.services.erp_adapters.catalog import public_erp_config
from app.services.provider_credentials import strip_all_blocks
from app.utils.bank_masking import mask_secret

# Top-level settings blocks a NON-ADMIN may read.
#
# * `None` → the whole block passes through.
# * a set  → only those sub-keys pass.
#
# Each entry earns its place by naming a real consumer; a block with no
# non-admin reader stays out, because "it looks harmless" is how the credential
# blocks were reachable in the first place.
NON_ADMIN_SETTINGS: dict[str, set[str] | None] = {
    # Tenant company profile — the mobile org-settings screen renders it for any
    # authed user (`mobile/lib/stores/org_settings_store.dart`).
    "company": None,
    # Currency / terms / GL defaults — the web `orgCurrency` store reads
    # `invoice_defaults.currency` to format every aggregate figure, for every
    # role (`frontend/src/lib/stores/orgSettings.svelte.ts`).
    "invoice_defaults": None,
    # The org's REPORTING (base) currency — a bare top-level string, not a
    # block. It is the first candidate `currency_conversion.resolve_reporting_
    # currency` reads, and every cross-currency rollup the API serves
    # (`/payments/summary`, the CFO forecast + cash position, the dashboard's
    # `reporting` block) is denominated in the code it resolves. The web store
    # had only `invoice_defaults.currency` to go on, so an org reporting in GBP
    # while invoicing defaults to USD had its converted GBP totals rendered
    # with a `$`. PII-free and credential-free — a three-letter ISO code.
    "reporting_currency": None,
    # ONLY the home currency. Second in that same resolution order. The rest of
    # the payments block is the processor credential set and stays admin-only.
    "payments": {"home_currency"},
    # White-label brand. Already readable by any authed role through
    # `GET /api/organization/branding`, and PII-free by construction.
    "brand": None,
    # ONLY the routing mode. The workflow builder shows a different ERP hint for
    # merge_dev vs direct (`frontend/src/routes/workflows/[id]/+page.svelte`).
    # Every credential in this block stays behind the admin gate.
    "erp": {"integration_method"},
}

# (block, key) pairs stripped for EVERY role, admin included — see the module
# docstring. Keep this tiny: it is for values whose only sanctioned read is
# "is one set?", not for general credential hygiene.
ALWAYS_REDACTED: tuple[tuple[str, str], ...] = (
    ("chat_notifications", "webhook_url"),
    # The OIDC client secret. Sealed in `provider_credentials` (block `sso`) by
    # its one writer, `PUT /api/organization/sso`, so a value here is a stray
    # (a hand edit, a pre-0135 backup restored) and must not reappear;
    # `GET /api/organization/sso` reports `client_secret_configured` only.
    ("sso", "client_secret"),
)


def _without_always_redacted(settings: dict) -> dict:
    """Copy `settings` with every `ALWAYS_REDACTED` pair removed.

    Copies only the path it touches, so the caller's dict (and, for an admin,
    the live ORM `Organization.settings`) is never mutated.
    """
    if not any(block in settings for block, _ in ALWAYS_REDACTED):
        return settings
    out = dict(settings)
    for block, key in ALWAYS_REDACTED:
        value = out.get(block)
        if isinstance(value, dict) and key in value:
            out[block] = {k: v for k, v in value.items() if k != key}
    return out


def _with_public_erp(settings: dict) -> dict:
    """``settings`` with its ``erp`` block's OAuth metadata hidden (a new dict)."""
    if isinstance(settings.get("erp"), dict):
        return {**settings, "erp": public_erp_config(settings["erp"])}
    return settings


def _with_masked_nacha_offset(settings: dict) -> dict:
    """``settings`` with ``payments.nacha.offset_account`` reduced to a last-4.

    The NACHA offset account (decisions §432) is the customer's own bank
    account number — banking data, shown masked on every surface
    (`utils/bank_masking`). Its writer keeps the stored number when a save
    sends it blank or sends this mask back (`nacha.carry_offset_account`), so
    no page needs the full number. Copies only the path it touches.
    """
    payments = settings.get("payments")
    nacha = payments.get("nacha") if isinstance(payments, dict) else None
    if not isinstance(nacha, dict) or nacha.get("offset_account") in (None, ""):
        return settings
    masked = {**nacha, "offset_account": mask_secret(nacha["offset_account"])}
    return {**settings, "payments": {**payments, "nacha": masked}}


def settings_for_response(settings: dict | None, *, is_admin: bool) -> dict:
    """Return the settings a caller of this role may see.

    An admin gets everything except `ALWAYS_REDACTED`. Everyone else gets a NEW
    dict holding only `NON_ADMIN_SETTINGS`, so a block added to the JSONB later
    is invisible to non-admins until it is listed here on purpose.
    """
    raw = settings or {}
    if is_admin:
        return _with_masked_nacha_offset(
            _without_always_redacted(_with_public_erp(strip_all_blocks(raw)))
        )

    projected: dict = {}
    for block, allowed_keys in NON_ADMIN_SETTINGS.items():
        value = raw.get(block)
        if value is None:
            continue
        if allowed_keys is None:
            projected[block] = value
            continue
        if not isinstance(value, dict):
            # A block declared with a sub-key allow-list can't be filtered when
            # it isn't a mapping; drop it rather than pass it through whole.
            continue
        subset = {k: v for k, v in value.items() if k in allowed_keys}
        if subset:
            projected[block] = subset
    return _without_always_redacted(_with_public_erp(strip_all_blocks(projected)))
