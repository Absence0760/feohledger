"""`GET /api/organization` must not hand third-party credentials to a clerk.

The endpoint is gated only by `get_current_user` and returned the raw
`Organization.settings` JSONB, so every authenticated role could read the
tenant's ERP client secret, payment-processor API key, card-issuing key,
extraction key, SSO client secret / SCIM bearer hash, and the Slack/Teams
incoming-webhook URL. Any one of those is enough to act as the tenant against a
third party.

Covers the pure projection (`services/org_settings_view`) and the endpoint
wired to it. Admins keep the verbatim settings — the `/organization` page reads
saved credentials back into its form fields, so redacting for them would blank a
live config on the next save.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.organization import Organization
from app.services.org_settings_view import (
    ALWAYS_REDACTED,
    NON_ADMIN_SETTINGS,
    settings_for_response,
)

SECRETS = {
    "erp": {
        "type": "netsuite",
        "integration_method": "direct",
        "client_secret": "erp-client-secret",
        "api_key": "erp-api-key",
        "webhook_signing_secret": "erp-webhook-secret",
    },
    "payments": {"provider": "modern_treasury", "webhook_secret": "pay-webhook-secret"},
    "cards": {"provider": "lithic", "api_key": "card-api-key"},
    "extraction": {"program_type": "byok", "api_key": "extract-api-key"},
    "sso": {"client_secret": "sso-client-secret", "scim_bearer_hash": "deadbeef"},
    "chat_notifications": {
        "enabled": True,
        "provider": "slack",
        "webhook_url": "https://hooks.example.invalid/services/T/B/zzTOPSECRETzz",
    },
    "company": {"address": "1 Main St", "tax_id": "12-3456789"},
    "invoice_defaults": {"currency": "EUR", "payment_terms": "Net 45"},
}

SECRET_VALUES = [
    "erp-client-secret",
    "erp-api-key",
    "erp-webhook-secret",
    "pay-webhook-secret",
    "card-api-key",
    "extract-api-key",
    "sso-client-secret",
    "deadbeef",
    "zzTOPSECRETzz",
]


# ---------- the pure projection ---------------------------------------------


def test_admin_keeps_every_credential_except_the_write_only_one():
    """The admin settings page round-trips saved credentials through its form
    fields, so an admin still sees them — all except the chat webhook URL,
    whose only sanctioned management path is the audited endpoint."""
    projected = settings_for_response(SECRETS, is_admin=True)
    assert projected["erp"]["client_secret"] == "erp-client-secret"
    assert projected["payments"]["webhook_secret"] == "pay-webhook-secret"
    assert projected["sso"]["scim_bearer_hash"] == "deadbeef"
    # …but never this one, for any role.
    assert "webhook_url" not in projected["chat_notifications"]
    assert projected["chat_notifications"]["provider"] == "slack"
    assert "zzTOPSECRETzz" not in str(projected)


def test_admin_projection_does_not_mutate_the_live_settings():
    """The admin path copies only the redacted branch, so removing the key must
    not reach through into the ORM object it was handed."""
    live = {"chat_notifications": {"provider": "slack", "webhook_url": "https://x.invalid/t"}}
    settings_for_response(live, is_admin=True)
    assert live["chat_notifications"]["webhook_url"] == "https://x.invalid/t"


def test_admin_projection_is_identity_when_nothing_is_redacted():
    """No needless copying on the common path."""
    plain = {"company": {"address": "1 Main St"}}
    assert settings_for_response(plain, is_admin=True) is plain


def test_non_admin_gets_no_credential():
    projected = settings_for_response(SECRETS, is_admin=False)
    blob = str(projected)
    for secret in SECRET_VALUES:
        assert secret not in blob, f"{secret} leaked into the non-admin projection"


def test_non_admin_keeps_what_real_consumers_read():
    projected = settings_for_response(SECRETS, is_admin=False)
    # The web `orgCurrency` store formats every aggregate figure from this.
    assert projected["invoice_defaults"]["currency"] == "EUR"
    # The mobile org-settings screen renders the company profile for any role.
    assert projected["company"]["address"] == "1 Main St"
    # The workflow builder branches its ERP hint on the routing mode only.
    assert projected["erp"] == {"integration_method": "direct"}


def test_projection_is_an_allow_list_not_a_deny_list():
    """A block nobody listed is invisible — so adding a new provider block to
    the JSONB can't leak by default."""
    projected = settings_for_response(
        {**SECRETS, "brand_new_provider": {"api_key": "future-secret"}},
        is_admin=False,
    )
    assert "brand_new_provider" not in projected
    assert "future-secret" not in str(projected)


def test_projection_does_not_mutate_the_source():
    before = {"erp": dict(SECRETS["erp"])}
    settings_for_response(before, is_admin=False)
    assert before["erp"]["client_secret"] == "erp-client-secret"


def test_sub_key_filtered_block_fails_closed_when_malformed():
    """A block declared with a sub-key allow-list can't be passed through whole
    just because it isn't a dict."""
    assert settings_for_response({"erp": "not-a-dict"}, is_admin=False) == {}


@pytest.mark.parametrize("settings", [None, {}])
def test_projection_handles_empty(settings):
    assert settings_for_response(settings, is_admin=False) == {}


def test_non_admin_can_resolve_the_reporting_currency():
    """The web `orgCurrency` store mirrors `resolve_reporting_currency`, so it
    needs the two candidates that outrank `invoice_defaults.currency`.

    Without them a non-admin's aggregate figures were labelled with the invoice
    default while the API had denominated them in the reporting currency — a
    `$` on a converted GBP total.
    """
    projected = settings_for_response(
        {
            **SECRETS,
            "reporting_currency": "GBP",
            "payments": {**SECRETS["payments"], "home_currency": "EUR"},
        },
        is_admin=False,
    )
    assert projected["reporting_currency"] == "GBP"
    assert projected["payments"] == {"home_currency": "EUR"}
    # …and nothing else from the payments block came with it.
    assert "pay-webhook-secret" not in str(projected)
    assert "modern_treasury" not in str(projected)


def test_payments_block_is_dropped_entirely_when_it_holds_no_home_currency():
    """`if subset:` — an all-credential payments block projects to nothing, not
    to an empty dict the client would have to special-case."""
    projected = settings_for_response(SECRETS, is_admin=False)
    assert "payments" not in projected


def test_erp_allow_list_holds_only_the_routing_mode():
    """A drift guard: widening this set is how a credential gets back out."""
    assert NON_ADMIN_SETTINGS["erp"] == {"integration_method"}


def test_payments_allow_list_holds_only_the_home_currency():
    """The same drift guard on the block that carries the processor
    credentials — every other key in it is enough to move money as the tenant."""
    assert NON_ADMIN_SETTINGS["payments"] == {"home_currency"}


def test_always_redacted_covers_the_chat_webhook():
    """The write-only property of the chat credential is asserted here, not
    just at the router that manages it."""
    assert ("chat_notifications", "webhook_url") in ALWAYS_REDACTED


# ---------- the endpoint -----------------------------------------------------


async def _seed_settings(realdb, key: str = "a") -> None:
    cmk = realdb.control_sessionmaker()
    async with cmk() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info(key).org_id))
        ).scalar_one()
        org.settings = {**(org.settings or {}), **SECRETS}
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["ap_clerk", "ap_manager", "cfo"])
async def test_get_organization_redacts_credentials_for_non_admins(realdb, role):
    await _seed_settings(realdb)
    async with realdb.client(key="a", role=role) as c:
        resp = await c.get("/api/organization")
    assert resp.status_code == 200
    for secret in SECRET_VALUES:
        assert secret not in resp.text, f"{secret} served to {role}"
    body = resp.json()
    assert body["settings"]["invoice_defaults"]["currency"] == "EUR"
    assert "payments" not in body["settings"]


@pytest.mark.asyncio
async def test_get_organization_keeps_admin_access_intact(realdb):
    """The settings page reads saved credentials back into its form fields, so
    an admin must still get them — all but the write-only chat webhook URL."""
    await _seed_settings(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.get("/api/organization")
    assert resp.status_code == 200
    settings = resp.json()["settings"]
    assert settings["erp"]["client_secret"] == "erp-client-secret"
    assert settings["payments"]["webhook_secret"] == "pay-webhook-secret"
    assert "webhook_url" not in settings["chat_notifications"]
    assert "zzTOPSECRETzz" not in resp.text


@pytest.mark.asyncio
async def test_patch_organization_refuses_chat_notifications(realdb):
    """The generic settings merge would otherwise be a second, UNAUDITED writer
    of the chat webhook credential — and its shallow `update()` would replace
    the whole block, dropping the URL while "saving the provider"."""
    await _seed_settings(realdb)
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={
                "settings": {
                    "chat_notifications": {
                        "enabled": True,
                        "provider": "slack",
                        "webhook_url": "https://evil.invalid/hook",
                    }
                }
            },
        )
    assert resp.status_code == 422
    assert "/api/organization/chat-notifications" in resp.json()["detail"]

    cmk = realdb.control_sessionmaker()
    async with cmk() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
    # The refused write landed nowhere.
    assert org.settings["chat_notifications"]["webhook_url"].endswith("zzTOPSECRETzz")


@pytest.mark.asyncio
async def test_patch_organization_still_merges_other_blocks(realdb):
    """The guard is scoped to one key — every other settings block still saves."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={"settings": {"invoice_defaults": {"currency": "GBP"}}},
        )
    assert resp.status_code == 200
    assert resp.json()["settings"]["invoice_defaults"]["currency"] == "GBP"


@pytest.mark.asyncio
async def test_vat_and_companies_house_numbers_round_trip(realdb):
    """A UK company has no use for the US-shaped `tax_id` field — these two
    are its own optional identifiers (`CompanyProfile.vat_registration_number`
    / `.companies_house_number`, `backend/app/schemas/organization.py`), and
    must save + read back through the same generic settings PATCH/GET every
    other company-profile field uses."""
    async with realdb.client(key="a", role="admin") as c:
        resp = await c.patch(
            "/api/organization",
            json={
                "settings": {
                    "company": {
                        "vat_registration_number": "GB123456789",
                        "companies_house_number": "12345678",
                    }
                }
            },
        )
        assert resp.status_code == 200
        company = resp.json()["settings"]["company"]
        assert company["vat_registration_number"] == "GB123456789"
        assert company["companies_house_number"] == "12345678"

        # And a fresh GET (not just the PATCH echo) reads the same values back.
        resp = await c.get("/api/organization")
    assert resp.status_code == 200
    company = resp.json()["settings"]["company"]
    assert company["vat_registration_number"] == "GB123456789"
    assert company["companies_house_number"] == "12345678"


# ---------- resolved_reporting_currency --------------------------------------
#
# docs/followups.md (c): the server KNOWS the reporting currency every rollup
# is denominated in — `currency_conversion.resolve_reporting_currency` — but
# used to serve only its raw ingredients, so a client whose only signal was
# the operator's `FEOH_REPORTING_CURRENCY_DEFAULT` (a setting no client can
# read) had nothing to resolve. `resolved_reporting_currency` is that answer,
# served as its own top-level field so it is never subject to the `settings`
# role projection above.


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "ap_clerk"])
async def test_get_organization_serves_resolved_reporting_currency(realdb, role):
    """Server-side truth, not projected settings — reaches every role
    identically, admin and non-admin alike."""
    await _seed_settings(realdb)  # sets invoice_defaults.currency = "EUR"
    async with realdb.client(key="a", role=role) as c:
        resp = await c.get("/api/organization")
    assert resp.status_code == 200
    assert resp.json()["resolved_reporting_currency"] == "EUR"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "ap_clerk"])
async def test_get_organization_resolved_reporting_currency_prefers_explicit_setting(realdb, role):
    """The first rung still wins when the org has set one, for every role."""
    cmk = realdb.control_sessionmaker()
    async with cmk() as s:
        org = (
            await s.execute(select(Organization).where(Organization.id == realdb.info("a").org_id))
        ).scalar_one()
        org.settings = {
            "reporting_currency": "GBP",
            "payments": {"home_currency": "EUR"},
            "invoice_defaults": {"currency": "USD"},
        }
        await s.commit()
    async with realdb.client(key="a", role=role) as c:
        resp = await c.get("/api/organization")
    assert resp.status_code == 200
    assert resp.json()["resolved_reporting_currency"] == "GBP"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "ap_clerk"])
async def test_get_organization_resolved_reporting_currency_falls_to_operator_default(
    realdb, monkeypatch, role
):
    """An org that has configured none of the three settings rungs (a fresh
    org — the harness resets `Organization.settings` to `{}` before every
    test) still gets a server-resolved answer: the operator's
    `FEOH_REPORTING_CURRENCY_DEFAULT`, which no client can read for itself
    (`docs/decisions.md` §119). Every role gets the same answer, because it is
    operator config, not tenant data."""
    monkeypatch.setattr(settings, "reporting_currency_default", "CHF")
    async with realdb.client(key="a", role=role) as c:
        resp = await c.get("/api/organization")
    assert resp.status_code == 200
    assert resp.json()["resolved_reporting_currency"] == "CHF"
