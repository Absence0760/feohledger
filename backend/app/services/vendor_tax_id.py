"""The one place that decides what re-keying a vendor's tax ID does to the row.

A TIN verification (`Vendor.tin_verified_at`) is a statement about a specific
number: the IRS matched *this* TIN to *this* name. Writing a different number
over it leaves the stamp asserting a match nobody made, and the 1099 dashboard
reads the stamp as "verified" — so the vendor drops off the pre-filing chase
list. Five paths write `tax_id` (vendor PATCH, the W-9 PATCH, the TIN-verify
override, an approved change request, ERP sync) and only the change-request
approval voided it; this function is now how all of them write the field.

Re-screening is deliberately NOT done here: it needs the org's sanctions config,
an actor and a savepoint, and the ERP sync has none of the first two. Callers
that re-key on a person's say-so re-screen themselves when this returns True
(`vendor_screening.screen_best_effort`).
"""

from __future__ import annotations

from app.models.vendor import Vendor


def rekey_tax_id(vendor: Vendor, new_tax_id: str | None) -> bool:
    """Write ``new_tax_id`` onto ``vendor``; return whether the value changed.

    A changed number voids any prior TIN verification. Re-sending the stored
    value is a no-op, so an edit form that round-trips the unchanged TIN does
    not discard a real IRS match.
    """
    if new_tax_id == vendor.tax_id:
        return False
    vendor.tax_id = new_tax_id
    vendor.tin_verified_at = None
    return True
