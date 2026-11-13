"""Sanctions-screening category taxonomy — the persisted half.

``ScreeningResult.categories`` (see ``sanctions_adapters/base.py``) enumerates
the *kinds* of hit a provider reported: a hard sanctions-list match, a PEP
flag, a high-risk jurisdiction, or an **adverse-media** (negative-news) hit —
press coverage of fraud / corruption that has not reached a formal list.

The adapters produce that taxonomy; this module is how it survives the write
and reaches the three surfaces that act on it:

* ``compliance.check_payment_compliance`` — an adverse-media hit adds its own
  reason to the ``ComplianceDecision``, so the AP team reviewing the hold sees
  *why* rather than a bare ``review_required``.
* ``vendor_screening.screen_vendor_record`` — the labels ride the persisted
  ``sanctions_checks`` row and the PII-free ``vendor.screened`` audit row.
* ``vendor_risk_scoring`` — reads them back off the persisted row (it is
  compute-on-read and never calls an adapter) to raise the vendor's sanctions
  sub-score and name the signal in ``Vendor.risk_factors``.

**Why the row's JSONB and not a new column.** ``sanctions_checks.raw_response``
and ``vendors.risk_factors`` are both already JSONB, so no migration is needed
to carry a taxonomy that is small, additive, and read by exactly one query
pattern (the latest row per vendor). A dedicated column would fan a schema
change out to every tenant DB to store what is effectively an enum list.

**The labels are PII-free by construction.** They are a fixed vocabulary — no
provider free text, no names, no dates of birth. That is what makes them safe
to put in an audit row, an API response and a UI badge, while
``raw_response``'s provider payload stays confined to the JSONB column
(invariant #7).
"""

from __future__ import annotations

from app.services.invoice_warning_catalog import exception_finding

# Taxonomy labels. Adapters normalise their provider's own vocabulary into
# these (e.g. World-Check's ``ADVERSE-MEDIA`` → ``adverse_media``).
CATEGORY_SANCTIONS = "sanctions"
CATEGORY_PEP = "pep"
CATEGORY_ADVERSE_MEDIA = "adverse_media"
CATEGORY_HIGH_RISK_COUNTRY = "high_risk_country"

# The reserved key under which the taxonomy is folded into the provider's own
# ``raw_response`` payload. Namespaced with a leading underscore so it cannot be
# confused with a provider field: World-Check, for one, uses ``categories``
# itself (nested under ``results[]``), and a future provider could use it at the
# top level.
RAW_RESPONSE_CATEGORIES_KEY = "_screening_categories"


def merge_categories_into_raw_response(
    raw_response: dict | None,
    categories: tuple[str, ...] | list[str] | None,
) -> dict | None:
    """Return the provider payload with the PII-free taxonomy folded in.

    Never mutates the input. The reserved key is written **only** when there is
    something to record, so a `clear` screen's stored payload stays exactly what
    the provider sent; on read, an absent key and an empty list mean the same
    thing ("no categories reported"), which is also what a row written before
    this existed means.

    If the provider's own payload happens to carry the reserved key, ours wins —
    the derived taxonomy is the authoritative one for every downstream reader.
    """
    labels = _normalize(categories)
    if not labels:
        return raw_response
    merged = dict(raw_response or {})
    merged[RAW_RESPONSE_CATEGORIES_KEY] = list(labels)
    return merged


def categories_from_raw_response(raw_response: dict | None) -> tuple[str, ...]:
    """Read the taxonomy back off a persisted ``sanctions_checks`` row.

    Tolerant by design — this reads a JSONB column that predates the key and
    that a provider payload also writes into. Anything that is not a list of
    non-empty strings reads as "no categories", never as an error: a screening
    trail row must not be able to 500 the risk endpoint.
    """
    if not isinstance(raw_response, dict):
        return ()
    return _normalize(raw_response.get(RAW_RESPONSE_CATEGORIES_KEY))


def has_adverse_media(categories: tuple[str, ...] | list[str] | None) -> bool:
    """True when the taxonomy includes a negative-news hit."""
    return CATEGORY_ADVERSE_MEDIA in _normalize(categories)


def adverse_media_finding(provider: str | None) -> dict:
    """The PII-free catalogue finding a compliance hold lists for this hit.

    Names the signal and the provider only — never the match detail, which can
    embed the article, the person, or a date of birth (invariant #7). A missing
    provider is its own code, never an English phrase spliced into the param.
    """
    if provider:
        return exception_finding("compliance_adverse_media", provider=provider)
    return exception_finding("compliance_adverse_media_unnamed_provider")


def adverse_media_reason(provider: str | None) -> str:
    """The English of :func:`adverse_media_finding`, for ``ComplianceDecision.reasons``."""
    return adverse_media_finding(provider)["message"]


def _normalize(categories: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """Trimmed, lower-cased, de-duplicated, order-stable labels."""
    if not isinstance(categories, list | tuple):
        return ()
    seen: list[str] = []
    for raw in categories:
        if not isinstance(raw, str):
            continue
        label = raw.strip().lower()
        if label and label not in seen:
            seen.append(label)
    return tuple(seen)
