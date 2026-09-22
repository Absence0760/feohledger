"""Unit coverage for the shared `api/sorting.py` primitives.

`resolve_order_by` is the one function standing between a client's `sort=`
query param and a real SQL `ORDER BY` — it's what makes an out-of-allowlist
column a 422 instead of either a silent no-op or (if a caller ever
mis-implemented it) raw string interpolation. These tests are DB-free: they
assert on the SQLAlchemy `ColumnElement` objects the function returns/raises,
not on query results (the list-endpoint sort behavior itself is covered by
the real-DB tests in `test_list_sorting.py`).
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, Integer, MetaData, String, Table

from app.api.sorting import SortParams, resolve_order_by, sort_params

_metadata = MetaData()
_widgets = Table(
    "widgets",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String),
    Column("amount", Integer),
)

ALLOWLIST = {"name": _widgets.c.name, "amount": _widgets.c.amount}


def test_no_sort_field_returns_default_unchanged():
    default = [_widgets.c.id.desc()]
    params = SortParams(field=None, order="desc")
    result = resolve_order_by(params, ALLOWLIST, id_column=_widgets.c.id, default=default)
    assert result is default


def test_allowlisted_field_ascending_appends_id_tiebreak():
    params = SortParams(field="name", order="asc")
    result = resolve_order_by(
        params, ALLOWLIST, id_column=_widgets.c.id, default=[_widgets.c.id.desc()]
    )
    assert len(result) == 2
    # Both the chosen column and the id tie-break follow the caller's
    # `order` — a mismatched tie-break direction would let ties reorder
    # across pages even though the primary column agrees.
    assert "name ASC" in str(result[0])
    assert "id ASC" in str(result[1])


def test_allowlisted_field_descending_appends_id_tiebreak():
    params = SortParams(field="amount", order="desc")
    result = resolve_order_by(
        params, ALLOWLIST, id_column=_widgets.c.id, default=[_widgets.c.id.desc()]
    )
    assert "amount DESC" in str(result[0])
    assert "id DESC" in str(result[1])


def test_unknown_field_is_422_not_silent_fallback():
    """A `sort=` value outside the endpoint's allowlist must be refused, not
    silently applied to whatever the default order was — a caller that
    thinks it sorted by X and gets Y back is worse than an explicit error."""
    params = SortParams(field="secret_internal_column", order="asc")
    with pytest.raises(HTTPException) as exc_info:
        resolve_order_by(params, ALLOWLIST, id_column=_widgets.c.id, default=[_widgets.c.id.desc()])
    assert exc_info.value.status_code == 422
    # The 422 names the accepted keys so the caller can self-correct.
    assert "amount" in exc_info.value.detail
    assert "name" in exc_info.value.detail
    assert "secret_internal_column" in exc_info.value.detail


def test_sort_params_dependency_defaults():
    params = sort_params(sort=None, order="desc")
    assert params.field is None
    assert params.order == "desc"


def test_sort_params_dependency_carries_field():
    params = sort_params(sort="amount", order="asc")
    assert params.field == "amount"
    assert params.order == "asc"


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_nulls_last_key_trails_nulls_in_both_directions(order):
    """A key named in `nulls_last` is one whose NULL means "no value on this
    axis". Postgres ranks NULL above every value, so a plain `DESC` would lead
    with those rows; the key sorts them last whichever way the caller asks."""
    params = SortParams(field="amount", order=order)
    result = resolve_order_by(
        params,
        ALLOWLIST,
        id_column=_widgets.c.id,
        default=[_widgets.c.id.desc()],
        nulls_last=frozenset({"amount"}),
    )
    assert f"amount {order.upper()} NULLS LAST" in str(result[0])
    # The tie-break is the primary key — never NULL — so it takes no clause.
    assert "NULLS" not in str(result[1])


def test_keys_outside_nulls_last_keep_the_database_default():
    params = SortParams(field="name", order="desc")
    result = resolve_order_by(
        params,
        ALLOWLIST,
        id_column=_widgets.c.id,
        default=[_widgets.c.id.desc()],
        nulls_last=frozenset({"amount"}),
    )
    assert "NULLS" not in str(result[0])
