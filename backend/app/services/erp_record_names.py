"""The one rule for storing an ERP display name too long for its column.

A name is a label, not a key (bills post by ERP id, GL rows match by id and
code), so it is shortened and marked rather than refused, and adapters never
cut it themselves. Codes and tax ids are identifiers and are never shortened.
Deterministic, so a re-sync of the same name is unchanged (decisions §353).
"""

from __future__ import annotations

#: Ends a name the sync had to shorten to fit its column.
SHORTENED_MARK = "…"


def fit_name(name: str, max_length: int) -> tuple[str, bool]:
    """``name`` as stored in a column of ``max_length`` characters, and
    whether it had to be shortened. A name that fits is returned as is."""
    if max_length <= len(SHORTENED_MARK):
        raise ValueError("max_length must leave room for the shortened mark")
    if len(name) <= max_length:
        return name, False
    return name[: max_length - len(SHORTENED_MARK)].rstrip() + SHORTENED_MARK, True
