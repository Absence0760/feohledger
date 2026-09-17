"""The single definition of what counts as a banking secret, and how it is masked.

Every surface in this product that shows a payee's bank coordinates shows a
last-4 and nothing more: the audit trail, the dual-control change queue, the UI,
the logs, the error bodies. That rule only holds if there is ONE answer to "which
keys inside ``Vendor.bank_details`` are the secret?", so it lives here rather
than in whichever router happened to need it first (it started in
``api/vendors.py``, which is why the router still re-exports these names under
their old private aliases).

Two reducers, with deliberately different failure directions:

* :func:`bank_details_audit_summary` describes a CHANGE for the audit trail. It
  is a **denylist** — the named secrets are reduced to a last-4, anything else
  records its literal old/new value — because an audit row's job is to say
  exactly what moved, and the keys it can see are the ones an AP user just sent.
* :func:`mask_bank_details` reduces a bank blob for DISCLOSURE (the DSAR
  bundle). It is an **allowlist**: only the known non-secret display keys pass
  through verbatim; the named secrets AND any key this module has never heard of
  are masked. A new display key added upstream therefore shows up as ``****`` in
  an export until someone lists it here — visible, harmless, and the correct
  direction for a file that leaves the system. The reverse default (unknown ⇒
  verbatim) is exactly the bug the DSAR bundle shipped with.
"""

from __future__ import annotations

# Keys inside ``bank_details`` JSONB that hold raw banking secrets and must
# NEVER appear in an audit row or an unflagged export. We record only THAT they
# changed plus a last-4 (PII-out-of-logs invariant).
#
# NOTE ``wire_routing_number`` is here for the same reason ``routing_number``
# is: this project treats a payee's routing coordinates as banking data that
# stays out of the trail.
BANK_SECRET_KEYS = frozenset({"account_number", "routing_number", "wire_routing_number", "iban"})

# Non-secret display metadata — safe to render verbatim wherever the blob is
# shown. Everything NOT listed here is masked by :func:`mask_bank_details`,
# including keys this module has never seen.
BANK_DISPLAY_KEYS = frozenset(
    {
        "account_last4",
        "routing_last4",
        "wire_routing_last4",
        "iban_last4",
        "account_type",
        "bank_name",
        "bic",
        "swift_bic",
        "country",
        "counterparty_id",
        "currency",
        "mailing_address",
    }
)

# What a masked secret renders as. Four dots so the shape is obviously a mask
# and not a truncated value, then the last four characters the rest of the
# product already shows.
_MASK_PREFIX = "****"


def last4(value: object) -> str | None:
    """The last four characters of a value, or ``None`` when it is too short."""
    s = str(value or "")
    return s[-4:] if len(s) >= 4 else None


def mask_secret(value: object) -> str | None:
    """``"021000021"`` → ``"****0021"``. ``None``/blank stays ``None``.

    A value shorter than four characters masks to ``"****"`` with no suffix —
    revealing three of three digits would not be a mask.
    """
    if value is None or str(value) == "":
        return None
    tail = last4(value)
    return f"{_MASK_PREFIX}{tail}" if tail else _MASK_PREFIX


def mask_bank_details(details: dict | None) -> dict | None:
    """Reduce a ``bank_details`` blob to its disclosable form.

    Display keys pass through; secrets and unknown keys become ``"****1234"``.
    The result carries ``_masked``/``_masked_keys`` so a reader of the bundle can
    tell a masked export from one that was never populated — a bare ``None``
    would be indistinguishable from "this vendor has no bank details on file".
    """
    if not details:
        return None
    out: dict = {}
    masked_keys: list[str] = []
    for key, value in details.items():
        if key in BANK_DISPLAY_KEYS:
            out[key] = value
            continue
        out[key] = mask_secret(value) if not isinstance(value, (dict, list)) else None
        masked_keys.append(key)
    out["_masked"] = True
    out["_masked_keys"] = sorted(masked_keys)
    return out


# Keys inside ``beneficial_owner_data`` that may be disclosed verbatim. The blob
# is ``{"owners": [{...}, ...]}`` (see ``services/vendor_screening._beneficial_owners``)
# and each owner is a DIFFERENT natural person from the DSAR subject — a
# vendor's ultimate beneficial owner did not ask for this export. So the owner
# records are reduced to what identifies the ownership relationship, and every
# identity-document field an onboarding form might have collected (passport
# number, national id, date of birth) is dropped rather than masked: a last-4 of
# someone else's passport number is still someone else's passport number.
BENEFICIAL_OWNER_DISPLAY_KEYS = frozenset(
    {"name", "role", "title", "ownership_percentage", "percentage", "country", "is_pep"}
)


def mask_beneficial_owner_data(data: dict | None) -> dict | None:
    """Reduce ``beneficial_owner_data`` to the disclosable ownership summary."""
    if not data:
        return None
    owners = data.get("owners") if isinstance(data, dict) else None
    if not isinstance(owners, list):
        # An unrecognised shape is not walked — we cannot tell which of its keys
        # are third-party identity documents, so none of it is disclosed.
        return {"_masked": True, "_masked_note": "unrecognised shape; withheld"}
    reduced = []
    withheld: set[str] = set()
    for owner in owners:
        if not isinstance(owner, dict):
            continue
        kept = {k: v for k, v in owner.items() if k in BENEFICIAL_OWNER_DISPLAY_KEYS}
        withheld |= {k for k in owner if k not in BENEFICIAL_OWNER_DISPLAY_KEYS}
        reduced.append(kept)
    return {
        "owners": reduced,
        "_masked": True,
        "_withheld_keys": sorted(withheld),
    }


def bank_details_audit_summary(before: dict | None, after: dict | None) -> dict | None:
    """PII-safe description of a ``bank_details`` change for the audit trail.

    Records the SET of keys that changed and, for the raw banking secrets
    (account/routing number, IBAN), only a masked last-4 of the old/new
    value — never the full number (PII / banking data must stay out of the
    audit trail). Non-secret display keys (counterparty_id, *_last4,
    bank_name, swift_bic, country) record their literal old/new values.
    Returns ``None`` when nothing changed.
    """
    before = before or {}
    after = after or {}
    all_keys = before.keys() | after.keys()
    changed_keys = sorted(k for k in all_keys if before.get(k) != after.get(k))
    if not changed_keys:
        return None

    field_changes: dict[str, dict] = {}
    for k in changed_keys:
        if k in BANK_SECRET_KEYS:
            field_changes[k] = {
                "old_last4": last4(before.get(k)),
                "new_last4": last4(after.get(k)),
            }
        else:
            field_changes[k] = {"old": before.get(k), "new": after.get(k)}
    return {"changed_fields": changed_keys, "fields": field_changes}
