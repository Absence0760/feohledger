"""How an ERP record's display name that does not fit our column is stored.

One rule for every sync that writes an ERP-supplied name — ``services/
vendor_sync`` (``vendors.name``) and ``services/gl_account_sync``
(``gl_accounts.name``), both ``String(255)`` — so the adapters never shorten a
name themselves (docs/decisions.md §353).

**A name is shortened and marked, never refused.** Neither name is a key: a
bill posts against the record's ERP id, a GL row is matched by id and code, and
the name only says to a person which record this is. Refusing the record would
leave a payee unlinked (every bill refused ``vendor_not_linked``) or a chart
account unpostable over a label. Writing it unchecked aborts the flush, and
with it the whole sync. So the stored name keeps as much of the ERP's as fits
and ends in :data:`SHORTENED_MARK`, which says on every screen that the full
name is in the ERP, and the sync reports the record (``names_shortened``).

This is the opposite of a CODE or tax id, which is an identifier: a cut one
names another thing, so those are dropped or refused, never shortened (§287).
The shortening is deterministic, so a re-sync of the same name writes the same
value and reports the record as unchanged.
"""

from __future__ import annotations

#: Ends a name the sync had to shorten to fit its column.
SHORTENED_MARK = "…"


def fit_name(name: str, max_length: int) -> tuple[str, bool]:
    """``name`` as stored in a column of ``max_length`` characters, and
    whether it had to be shortened. A name that fits is returned as is."""
    if len(name) <= max_length:
        return name, False
    return name[: max_length - len(SHORTENED_MARK)].rstrip() + SHORTENED_MARK, True
