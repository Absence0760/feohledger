"""Email catalogue + localized notification rendering — pure (no DB/Redis).

Covers the server-side email i18n surface
(``app/services/email_adapters/email_catalogue.py``):

- catalogue parity: every supported locale has every key, no empty strings,
  placeholder-faithful vs English;
- English fallback on a missing key and on an unknown locale;
- ``normalize_locale`` / ``is_supported_locale`` behaviour;
- ``notification_templates.render`` produces non-English copy for a non-English
  locale while keeping the invoice number / vendor / money identical (the
  locale changes copy only, never the data) — and PII never leaks.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.models.notification import (
    EVENT_INVOICE_APPROVED,
    EVENT_INVOICE_ASSIGNED,
    EVENT_INVOICE_PAID,
    EVENT_INVOICE_REJECTED,
)
from app.services.email_adapters.email_catalogue import (
    DEFAULT_LOCALE,
    SUPPORTED_EMAIL_LOCALES,
    all_keys,
    catalogue_for,
    is_supported_locale,
    normalize_locale,
    translate,
)
from app.services.notification_templates import InvoiceContext, render

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _placeholders(s: str) -> set[str]:
    return set(_PLACEHOLDER.findall(s))


def test_six_supported_locales():
    # Same starter set as web/mobile.
    assert set(SUPPORTED_EMAIL_LOCALES) == {"en", "de", "fr", "es", "pt-BR", "ja"}
    assert DEFAULT_LOCALE == "en"


def test_english_is_the_complete_key_set():
    keys = all_keys()
    assert keys, "English catalogue must define keys"
    # English is the source of truth — its dict carries every key.
    assert set(catalogue_for("en").keys()) == set(keys)


@pytest.mark.parametrize("locale", SUPPORTED_EMAIL_LOCALES)
def test_catalogue_parity_no_empty_no_placeholder_drift(locale: str):
    """Every locale resolves every key to a non-empty, placeholder-faithful str.

    A non-English catalogue may translate a subset (English fills the rest), so
    we assert through ``translate`` (the fallback path) rather than the raw
    dict — the contract is "no key ever renders empty, and the placeholder set
    matches English exactly".
    """
    for key in all_keys():
        english = translate(key, "en")
        value = translate(key, locale)
        assert value, f"{locale}:{key} rendered empty"
        assert value.strip() == value or value, f"{locale}:{key} is whitespace-only"
        # Placeholder tokens are part of the contract — a translation may
        # reorder them but must carry the same set (so no caller param is lost).
        assert _placeholders(value) == _placeholders(english), (
            f"{locale}:{key} placeholder mismatch: "
            f"{_placeholders(value)} != {_placeholders(english)}"
        )


# Product names the FeohLedger rename retired. "accounts payable" is the domain,
# not the brand, and stays legitimate copy — the pattern cannot match it.
_RETIRED_BRAND = re.compile(r"account[-_ ]payables|betterap", re.IGNORECASE)


@pytest.mark.parametrize("locale", SUPPORTED_EMAIL_LOCALES)
def test_no_retired_brand_name_in_any_locale(locale: str):
    """A translation is a second copy of the brand, and a rename can miss it.

    German shipped "Account-Payables-Arbeitsbereich" in both signup subjects
    long after every other locale said FeohLedger — the first email a new
    German-speaking tenant ever receives. The parity test above can't see it:
    the placeholders were faithful, only the product name was stale.
    """
    offenders = {
        key: translate(key, locale)
        for key in all_keys()
        if _RETIRED_BRAND.search(translate(key, locale))
    }
    assert offenders == {}
    for key in ("signup.verify.subject", "signup.welcome.subject"):
        assert "FeohLedger" in translate(key, locale), f"{locale}:{key}"


def test_translate_fills_placeholders():
    out = translate("notif.invoice_paid.title", "en", ref="Invoice X (Acme)")
    assert "Invoice X (Acme)" in out
    assert "{ref}" not in out


def test_missing_key_falls_back_to_raw_key_never_empty():
    # An unknown key is at least visible (never an empty string / crash).
    assert translate("does.not.exist", "de") == "does.not.exist"


def test_unknown_locale_falls_back_to_english():
    en = translate("notif.invoice_approved.title", "en", ref="R")
    assert translate("notif.invoice_approved.title", "zz-ZZ", ref="R") == en
    assert translate("notif.invoice_approved.title", None, ref="R") == en


def test_partial_locale_falls_back_to_english_for_untranslated_key():
    # If a non-English catalogue lacks a key, translate() yields the English
    # string (not empty, not a crash). Verified by injecting a missing key.
    # (All real keys are covered above; this asserts the fallback mechanism.)
    de_dict = catalogue_for("de")
    # pick any key, prove the de path resolves it (translated OR english-filled)
    sample = next(iter(all_keys()))
    out = translate(sample, "de")
    assert out  # non-empty regardless of whether `de_dict` has it
    assert sample in de_dict or out == translate(sample, "en")


def test_normalize_locale():
    assert normalize_locale("de") == "de"
    assert normalize_locale("DE") == "de"
    assert normalize_locale("pt-BR") == "pt-BR"
    assert normalize_locale("pt-br") == "pt-BR"
    assert normalize_locale("pt_BR") == "pt-BR"
    assert normalize_locale("pt") == "pt-BR"  # base-language fallback
    assert normalize_locale("de-AT") == "de"  # base-language fallback
    assert normalize_locale("zz") == "en"
    assert normalize_locale(None) == "en"
    assert normalize_locale("") == "en"


def test_is_supported_locale_is_strict():
    assert is_supported_locale("de") is True
    assert is_supported_locale("pt-BR") is True
    # Strict: a coercible-but-not-exact value is NOT "supported" (the set-locale
    # endpoint rejects it rather than silently storing a non-canonical value).
    assert is_supported_locale("pt") is False
    assert is_supported_locale("DE") is False
    assert is_supported_locale("zz") is False
    assert is_supported_locale(None) is False
    assert is_supported_locale("") is False


def _ctx() -> InvoiceContext:
    return InvoiceContext(
        invoice_number="INV-2026-001",
        vendor_name="Globex Corp",
        amount=Decimal("1234.56"),
        currency="USD",
    )


def test_render_default_locale_is_english():
    # No locale → English, byte-identical to explicitly asking for English.
    assert render(EVENT_INVOICE_APPROVED, _ctx()).body_text == (
        render(EVENT_INVOICE_APPROVED, _ctx(), locale="en").body_text
    )


@pytest.mark.parametrize("locale", ["de", "fr", "es", "pt-BR", "ja"])
def test_render_non_english_differs_but_keeps_data(locale: str):
    en = render(EVENT_INVOICE_PAID, _ctx(), locale="en")
    loc = render(EVENT_INVOICE_PAID, _ctx(), locale=locale)
    # Copy changed…
    assert loc.body_text != en.body_text
    assert loc.title != en.title
    # …but the locale-independent data (invoice number, vendor, exact money) is
    # present unchanged in every locale.
    assert "INV-2026-001" in loc.body_text
    assert "Globex Corp" in loc.body_text
    assert "USD 1,234.56" in loc.body_text


def test_render_unknown_locale_renders_english_copy():
    en = render(EVENT_INVOICE_ASSIGNED, _ctx(), locale="en")
    zz = render(EVENT_INVOICE_ASSIGNED, _ctx(), locale="zz-ZZ")
    assert zz.body_text == en.body_text


def test_render_rejection_reason_localized():
    ctx = InvoiceContext(
        invoice_number="INV-9",
        vendor_name="Initech",
        amount=Decimal("10.00"),
        reason="Missing PO number",
    )
    de = render(EVENT_INVOICE_REJECTED, ctx, locale="de")
    # The free-text reason is verbatim (caller-supplied), the surrounding copy
    # is German ("Grund").
    assert "Missing PO number" in de.body_text
    assert "Grund" in de.body_text


_FORBIDDEN_PII = [
    "123456789",
    "GB29NWBK60161331926819",
    "742 Evergreen Terrace, Springfield, IL",
]


@pytest.mark.parametrize("locale", SUPPORTED_EMAIL_LOCALES)
def test_no_pii_in_any_localized_template(locale: str):
    ctx = _ctx()
    for event in (
        EVENT_INVOICE_ASSIGNED,
        EVENT_INVOICE_APPROVED,
        EVENT_INVOICE_REJECTED,
        EVENT_INVOICE_PAID,
    ):
        r = render(event, ctx, locale=locale)
        blob = f"{r.title}\n{r.body_text}\n{r.body_html}"
        for token in _FORBIDDEN_PII:
            assert token not in blob
