"""Canonical column sorting for list endpoints.

Every sortable list endpoint shares ONE contract, mirroring
``api/pagination.py``:

- query params ``sort`` (a client-facing column key) and ``order``
  (``asc`` | ``desc``, default ``desc``)
- the column a ``sort`` key maps to comes from a per-endpoint allowlist —
  the raw query param is NEVER interpolated into SQL, and a key outside the
  allowlist is a 422 naming the ones that are accepted (not a silent
  fallback to the default order, which would leave a caller believing it
  sorted by something it didn't)
- the row's own primary key is ALWAYS appended as the final sort key,
  regardless of which column the caller picked — the same reasoning behind
  the existing `.id` tie-breaker on every list endpoint's default order
  (`created_at` alone isn't unique enough to make OFFSET/LIMIT pagination
  deterministic; neither is any other sortable column here). Without it, two
  rows tied on the chosen column could be reordered between pages by
  Postgres, duplicating one row onto two pages or skipping another.

Use :func:`sort_params` as the FastAPI dependency and :func:`resolve_order_by`
to turn the validated input into an ``ORDER BY`` clause list.
"""

from dataclasses import dataclass

from fastapi import HTTPException, Query
from sqlalchemy import ColumnElement

DEFAULT_ORDER = "desc"


@dataclass(frozen=True)
class SortParams:
    """Resolved, unvalidated sort inputs for a list request.

    ``field`` is unvalidated against any particular endpoint's allowlist here
    — validation happens in :func:`resolve_order_by`, which knows which
    columns THIS endpoint accepts. Keeping the two separate lets one
    dependency serve every list endpoint.
    """

    field: str | None
    order: str


def sort_params(
    sort: str | None = Query(None, description="Column to sort by (endpoint-specific allowlist)"),
    order: str = Query(DEFAULT_ORDER, pattern="^(asc|desc)$"),
) -> SortParams:
    return SortParams(field=sort, order=order)


def resolve_order_by(
    params: SortParams,
    allowlist: dict[str, ColumnElement],
    *,
    id_column: ColumnElement,
    default: list[ColumnElement],
    nulls_last: frozenset[str] = frozenset(),
) -> list[ColumnElement]:
    """Build an ``ORDER BY`` clause list from validated, allowlisted input.

    ``allowlist`` maps the client-facing ``sort`` key to a real SQLAlchemy
    column/expression for THIS endpoint — the only thing standing between the
    query string and a `sort=` value that isn't a real, intended-sortable
    column. An unrecognised key is refused with a 422 naming the accepted
    ones; a caller isn't left thinking it sorted by something it didn't.

    When ``params.field`` is ``None`` (no ``sort=`` supplied), returns
    ``default`` unchanged — the endpoint's pre-existing order, so a bare
    `GET` behaves exactly as it did before this module existed.

    The resolved column's tie-break direction, and the appended `.id`
    tie-break, both follow ``params.order`` — so switching a column between
    ascending and descending doesn't leave stale ties ordered the old way.

    ``nulls_last`` names the allowlisted keys whose NULL means "this row has no
    value on the axis", not "smallest" or "largest". Postgres sorts NULL as
    larger than every value, so without it a descending sort puts every such
    row FIRST — on the exception queue's `due_at`, a descending "Due" click
    showed every exception with no SLA above the ones actually running out of
    time. Those keys sort their NULLs last in BOTH directions. A key left out
    keeps Postgres' default, which is what every endpoint had before this
    parameter existed.
    """
    if params.field is None:
        return default
    column = allowlist.get(params.field)
    if column is None:
        allowed = ", ".join(sorted(allowlist))
        raise HTTPException(
            status_code=422,
            detail=f"Cannot sort by '{params.field}'. Allowed values: {allowed}.",
        )
    ascending = params.order == "asc"
    primary = column.asc() if ascending else column.desc()
    if params.field in nulls_last:
        primary = primary.nulls_last()
    tiebreak = id_column.asc() if ascending else id_column.desc()
    return [primary, tiebreak]
