"""The ``audit_log`` sweeps and every list endpoint's default sort order are
indexed — migration 0092.

Two query shapes had no index at all before 0092, and both ran against tables
that only grow:

1. ``audit_log``, the largest table in a tenant, had nothing on ``created_at``
   or ``action`` — so the SOX signature sweep, the auditor's date-range export,
   the dashboard's approval-timing leg, the adaptive feedback reads and the
   retention sweep were each a full sequential scan.

   The shipper's own ``WHERE shipped_at IS NULL ORDER BY created_at ASC LIMIT
   n`` — **per tenant, every 60 s, returning nothing on a healthy platform** —
   was a full scan only on tenants provisioned by ``create_all``. Migration
   ``0010_audit_log_shipping`` built ``ix_audit_log_shipped_at_null`` for it, but
   nothing declared it on the model, and fresh tenants never run Alembic. That
   split is exactly what this file now makes impossible.
2. Seven list endpoints ordered by a column with no index, so page 1 of every
   list view was a whole-table read plus a top-N heapsort.

Measured before landing 0092 (local Postgres 16, warm cache, third run of each
query; the full table is in the migration's docstring):

    1.2 M audit rows   shipper, caught up *      39.740 ms → 0.040 ms
                       verify-signatures 30 d    41.010 ms → 4.198 ms
                       audit export 7 d          64.427 ms → 1.381 ms
    200 k invoices     list page 1               15.780 ms → 0.031 ms
                       list page 1 + status      13.068 ms → 0.053 ms

    * what a `create_all`-provisioned tenant was paying; a migrated one already
      had migration 0010's index.

What this file pins:

1. **Every index the migration creates is also declared on its model.** Tenants
   are provisioned two ways — ``tenant_provisioning._create_tenant_tables``
   (``create_all``, fresh tenants) and Alembic (existing tenants) — so an index
   that lives only in the migration leaves the two schemas permanently
   different. This is what fails if a later migration drops one.
2. **The two spellings produce the SAME index**, column-for-column and
   predicate-for-predicate, checked against what Postgres actually built.
3. **The revision is tenant-gated**, so it no-ops on the control plane (which
   has none of these eight tables) while still advancing its head.
4. **The DDL applies, is idempotent, and reverses.**
5. **Each index is usable for the predicate it was added for**, asserted under
   ``enable_seqscan = off`` — that tests the SHAPE of the index against the
   shape of the query, which is deterministic, rather than "would the planner
   pick it", which at the harness's handful of rows would be a fiction (the
   real-volume choice is the measurement above).

`ix_audit_log_shipped_at_null` turned out to be one instance of a class of 20.
The rest are closed by migration 0093, and
`tests/test_migration_model_index_parity.py` is the systemic guard that now
covers EVERY `CREATE INDEX` in EVERY revision — opt-out, so a new migration-only
index fails on the day it lands. This file stays the per-index guard for 0092's
own sixteen (including that each actually serves its caller's query, which the
systemic one does not assert).

Real-Postgres harness (`realdb`).
"""

from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models.base import Base
from app.services.tenant_provisioning import CONTROL_TABLES

TENANT = "a"

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0092_list_and_audit_indexes.py"
)


def _migration_module():
    """Import the migration for its index declarations (it never runs ``op`` here)."""
    spec = importlib.util.spec_from_file_location("_mig_0092", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATION = _migration_module()
#: The 16 indexes 0092 OWNS — created on upgrade, dropped on downgrade.
INDEXES: list[tuple[str, str, str]] = MIGRATION._INDEXES
#: The one it ADOPTS: `ix_audit_log_shipped_at_null` belongs to migration 0010,
#: which has been creating it on migrated tenants since audit shipping shipped.
#: 0092 restates the CREATE (idempotent) and adds the model declaration that was
#: missing, so a `create_all`-provisioned tenant stops running the shipper's
#: 60-second sweep against no index — but it must never DROP it.
ADOPTED: list[tuple[str, str, str]] = MIGRATION._ADOPTED
ALL_INDEXES = ADOPTED + INDEXES
INDEX_NAMES = {name for _table, name, _body in INDEXES}
ADOPTED_NAMES = {name for _table, name, _body in ADOPTED}
ALL_NAMES = INDEX_NAMES | ADOPTED_NAMES
TABLES = {table for table, _name, _body in ALL_INDEXES}


def _squash(sql: str) -> str:
    """Normalise an index body so the migration's spelling and Postgres's
    canonical ``pg_get_indexdef`` output compare equal.

    Postgres re-renders ``ON t (a) WHERE p`` as ``USING btree (a) WHERE (p)``
    and schema-qualifies the table, so a literal string compare would fail on
    formatting rather than on meaning. Everything that carries meaning — the
    column list, each column's sort direction, the partial predicate — survives
    this squash; only punctuation and case do not.
    """
    sql = sql.replace("public.", "")
    sql = re.sub(r"\s+", "", sql)
    return sql.replace("(", "").replace(")", "").lower()


def _body_after_table(body: str, table: str) -> str:
    """``ON invoices (created_at DESC, id DESC)`` → ``(created_at DESC, id DESC)``."""
    prefix = f"ON {table} "
    assert body.startswith(prefix), body
    return body[len(prefix) :]


async def _index_defs(session, table: str) -> dict[str, str]:
    rows = await session.execute(
        text(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = :t"
        ),
        {"t": table},
    )
    return {name: definition for name, definition in rows.all()}


# ---------------------------------------------------------------------------
# 1 + 3. the declarations themselves — no database needed
# ---------------------------------------------------------------------------


def test_every_migration_index_is_declared_on_its_model():
    """An index that exists only in the migration is invisible to
    ``create_all``, so a freshly-provisioned tenant would silently lack it while
    a migrated one has it. This also fails if a later migration removes one from
    the models without removing it here."""
    for table, name, _body in ALL_INDEXES:
        declared = {index.name for index in Base.metadata.tables[table].indexes}
        assert name in declared, (
            f"migration 0092 creates {name} on {table}, but the model declares "
            f"only {sorted(declared)} — a create_all-provisioned tenant would "
            "not get it"
        )


def test_every_indexed_table_is_tenant_scoped():
    """All eight tables live in the tenant DBs. If one ever moved to the control
    plane the revision's table gate would silently skip it there."""
    for table in sorted(TABLES):
        assert table not in CONTROL_TABLES, (
            f"{table} is now control-plane; migration 0092 gates on the table "
            "existing and would quietly create nothing"
        )


def test_migration_is_table_gated_so_it_no_ops_on_the_control_plane():
    gate = inspect.getsource(MIGRATION._table_exists)
    assert "information_schema.tables" in gate
    runner = inspect.getsource(MIGRATION._run)
    assert "_table_exists(table)" in runner
    assert "TENANT DB ONLY" in MIGRATION.__doc__


def test_ddl_is_idempotent_and_every_create_has_a_matching_drop():
    """The project invariant: safe, reversible DDL."""
    creates = {}
    for table, statement in MIGRATION._UPGRADE:
        assert statement.startswith("CREATE INDEX IF NOT EXISTS "), statement
        creates[statement.split()[5]] = table
    drops = {}
    for table, statement in MIGRATION._DOWNGRADE:
        assert statement.startswith("DROP INDEX IF EXISTS "), statement
        drops[statement.split()[4]] = table
    assert set(creates) == ALL_NAMES
    # Everything this revision OWNS is dropped; the index it adopts is not.
    assert set(drops) == INDEX_NAMES
    # Reverse order, so the downgrade unwinds the upgrade.
    assert [n for _t, n, _b in reversed(INDEXES)] == [
        s.split()[4] for _t, s in MIGRATION._DOWNGRADE
    ]


def test_the_adopted_index_belongs_to_migration_0010_and_is_never_dropped_here():
    """`ix_audit_log_shipped_at_null` is revision 0010's.

    0092 restates its CREATE so an already-provisioned tenant picks it up, and
    adds the model declaration `create_all` needs — but downgrading 0092 must
    leave 0010's index in place, and 0092 must not create a SECOND index on the
    same predicate under a new name, which would be pure write overhead on
    every audit row for the rest of the platform's life.
    """
    owner = (_MIGRATION_PATH.parent / "0010_audit_log_shipping.py").read_text()
    dropped = " ".join(statement for _t, statement in MIGRATION._DOWNGRADE)
    for name in ADOPTED_NAMES:
        assert f"CREATE INDEX IF NOT EXISTS {name} " in owner, (
            f"{name} is listed as adopted but migration 0010 does not create it"
        )
        assert name not in dropped
    partials = {
        name
        for table, name, body in ALL_INDEXES
        if table == "audit_log" and "shipped_at IS NULL" in body
    }
    assert partials == ADOPTED_NAMES, (
        "a second partial index on the shipper's own predicate would duplicate "
        f"migration 0010's: {sorted(partials)}"
    )


def test_concurrently_is_not_used():
    """``CONCURRENTLY`` cannot run inside Alembic's transaction, and combined
    with ``IF NOT EXISTS`` it is a trap: a failed concurrent build leaves an
    INVALID index whose name then makes every later run skip it and report
    success. The trade-off is argued in the migration's docstring; this pins the
    decision so it is re-argued rather than drifted into."""
    for _table, statement in MIGRATION._UPGRADE + MIGRATION._DOWNGRADE:
        assert "CONCURRENTLY" not in statement.upper()


# ---------------------------------------------------------------------------
# 2 + 4. against a real tenant DB
# ---------------------------------------------------------------------------


async def test_provisioned_tenant_carries_every_index(realdb):
    """A tenant DB built by ``create_all`` — how fresh tenants are provisioned —
    must match a migrated one."""
    async with realdb.sessionmaker(TENANT)() as session:
        for table in sorted(TABLES):
            present = await _index_defs(session, table)
            expected = {name for t, name, _b in ALL_INDEXES if t == table}
            missing = expected - set(present)
            assert not missing, (
                f"freshly-provisioned tenant is missing {sorted(missing)} on {table}"
            )


async def test_create_all_and_the_migration_build_the_same_index(realdb):
    """Same name is not enough — the same COLUMNS, in the same order and
    direction, with the same partial predicate."""
    async with realdb.sessionmaker(TENANT)() as session:
        for table, name, body in ALL_INDEXES:
            defs = await _index_defs(session, table)
            actual = defs[name]
            _prefix, _sep, built = actual.partition("USING btree ")
            assert built, actual
            assert _squash(built) == _squash(_body_after_table(body, table)), (
                f"{name}: create_all built {built!r}, migration 0092 declares {body!r}"
            )


async def test_migration_statements_apply_are_idempotent_and_reverse(realdb):
    """Run the migration's own SQL against a real tenant DB: down, up, up."""
    mk = realdb.sessionmaker(TENANT)
    async with mk() as session:
        for _table, statement in MIGRATION._DOWNGRADE:
            await session.execute(text(statement))
        await session.commit()
        for table in sorted(TABLES):
            present = set(await _index_defs(session, table))
            assert not (present & INDEX_NAMES)
        # ...but revision 0010's index survives a 0092 downgrade.
        assert ADOPTED_NAMES <= set(await _index_defs(session, "audit_log"))

        for _ in range(2):  # idempotent: CREATE INDEX ... IF NOT EXISTS
            for _table, statement in MIGRATION._UPGRADE:
                await session.execute(text(statement))
            await session.commit()

        for table in sorted(TABLES):
            present = set(await _index_defs(session, table))
            expected = {name for t, name, _b in ALL_INDEXES if t == table}
            assert expected <= present


# ---------------------------------------------------------------------------
# 5. each index is shaped for the query it was added for
# ---------------------------------------------------------------------------

# (index name, the production query's own predicate + order). Each SQL string is
# the shape the named caller issues, not an approximation of it.
_PLAN_CASES = [
    pytest.param(
        "ix_audit_log_created_at",
        "SELECT id FROM audit_log WHERE created_at >= now() - interval '7 days' "
        "AND created_at < now() ORDER BY created_at",
        id="GET_/audit/export",
    ),
    pytest.param(
        "ix_invoices_created_at_id",
        "SELECT id FROM invoices ORDER BY created_at DESC, id DESC LIMIT 20",
        id="GET_/invoices",
    ),
    pytest.param(
        "ix_payments_created_at_id",
        "SELECT id FROM payments ORDER BY created_at DESC, id DESC LIMIT 20",
        id="GET_/payments",
    ),
    pytest.param(
        "ix_exceptions_created_at_id",
        "SELECT id FROM exceptions ORDER BY created_at DESC, id DESC LIMIT 20",
        id="GET_/exceptions",
    ),
    pytest.param(
        "ix_expenses_created_at_id",
        "SELECT id FROM expenses ORDER BY created_at DESC, id DESC LIMIT 20",
        id="GET_/expenses",
    ),
    pytest.param(
        "ix_purchase_orders_created_at_id",
        "SELECT id FROM purchase_orders ORDER BY created_at DESC, id DESC LIMIT 20",
        id="GET_/purchase-orders",
    ),
    pytest.param(
        "ix_contracts_created_at_id",
        "SELECT id FROM contracts ORDER BY created_at DESC, id DESC LIMIT 20",
        id="GET_/contracts",
    ),
    pytest.param(
        "ix_corp_card_txn_date_id",
        "SELECT id FROM corporate_card_transactions ORDER BY txn_date DESC, id DESC LIMIT 20",
        id="GET_/corporate-card-transactions",
    ),
]


@pytest.mark.parametrize(("index_name", "sql"), _PLAN_CASES)
async def test_index_serves_its_query(realdb, index_name, sql):
    """Postgres can answer the caller's own query from this index, ordered,
    without sorting.

    The three ``enable_*`` knobs are what make this deterministic instead of a
    coin-flip. At the harness's row counts every plan costs ~nothing, so with
    only ``enable_seqscan = off`` the planner picks an arbitrary narrow index and
    sorts on top — a real plan, but it answers "what is cheapest over ten rows",
    not "is this index shaped for this query". Ruling out the seq scan, the
    bitmap scan (which discards ordering) and the sort leaves exactly one way to
    produce the requested order: an ordered scan of an index that carries it.
    """
    async with realdb.sessionmaker(TENANT)() as session:
        for knob in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
            await session.execute(text(f"SET LOCAL {knob} = off"))
        plan = "\n".join((await session.execute(text(f"EXPLAIN {sql}"))).scalars().all())
    assert index_name in plan, f"plan did not use {index_name}:\n{plan}"
    assert "Sort" not in plan, f"{index_name} did not supply the order:\n{plan}"


# The shipper's query needs its own case for the same reason the signature
# sweep below does: two `audit_log` indexes can produce `ORDER BY created_at`
# for it — the partial one with `shipped_at IS NULL` implied by its predicate,
# the plain one as an ordered scan with the null test as a per-row Filter — so
# the knobs above cannot separate them. Which one wins is decided by the
# trail's statistics, not by the index's shape. When `audit_log` has been
# analyzed while every row is unshipped (any earlier test in the shard that
# writes audit rows, then autovacuum), the two scans visit exactly the same
# tuples and cost exactly the same, and the tie goes to whichever index the
# planner enumerates first — the plain one. That is the CI failure this case
# was split out to stop: shard composition decided the assertion. On a real
# trail the shipped majority makes the partial index the only sensible plan
# (0.040 ms caught up at 1.2 M rows, the migration's measurement). Dropping the
# plain timestamp indexes inside a transaction that rolls back asks the
# deterministic question instead: can the partial index serve the shipper's
# query, ordered, with its predicate doing the filtering?
_SHIPPER_SQL = "SELECT id FROM audit_log WHERE shipped_at IS NULL ORDER BY created_at ASC LIMIT 500"


async def test_shipper_tail_is_served_by_the_partial_index(realdb):
    """`audit_log_shipper._ship_tenant` — every tenant, every 60 s — reads the
    unshipped tail oldest-first; the partial index carries both the predicate
    and the order."""
    async with realdb.sessionmaker(TENANT)() as session:
        try:
            for knob in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
                await session.execute(text(f"SET LOCAL {knob} = off"))
            await session.execute(text("DROP INDEX ix_audit_log_created_at"))
            await session.execute(text("DROP INDEX ix_audit_log_action_created_at"))
            rows = await session.execute(text(f"EXPLAIN {_SHIPPER_SQL}"))
            plan = "\n".join(rows.scalars().all())
        finally:
            # Undoes both DROPs: nothing here is committed.
            await session.rollback()
    assert "ix_audit_log_shipped_at_null" in plan, plan
    assert "Sort" not in plan, plan
    # The predicate is the index's own, so Postgres never re-checks it per row.
    assert "Filter" not in plan, f"`shipped_at IS NULL` is re-checked per row:\n{plan}"


# The signature sweep needs its own case, because both `audit_log` timestamp
# indexes can produce `ORDER BY created_at` — the composite as an Index Cond on
# `action`, the plain one as an ordered scan with `action` as a Filter — so the
# knobs above cannot separate them. Over the harness's handful of rows the
# planner prefers the plain one and re-checks `action` per row, which is a
# perfectly good plan there and a full timestamp walk at 1.2 M. Dropping the
# plain index inside a transaction that rolls back is what makes the question
# answerable: it asks whether the composite CAN serve the predicate as an index
# condition, which is the property migration 0092 added it for.
_SIGNATURE_SWEEP_SQL = (
    "SELECT id FROM audit_log WHERE action = 'invoice.approved' "
    "AND created_at >= now() - interval '30 days' AND created_at < now() "
    "ORDER BY created_at"
)


async def test_signature_sweep_matches_action_as_an_index_condition(realdb):
    """`GET /api/audit/verify-signatures` — the SOX population-level
    non-repudiation test — filters `action` to equality and ranges on
    `created_at`, which is exactly the composite's column order."""
    async with realdb.sessionmaker(TENANT)() as session:
        try:
            for knob in ("enable_seqscan", "enable_bitmapscan", "enable_sort"):
                await session.execute(text(f"SET LOCAL {knob} = off"))
            await session.execute(text("DROP INDEX ix_audit_log_created_at"))
            rows = await session.execute(text(f"EXPLAIN {_SIGNATURE_SWEEP_SQL}"))
            plan = "\n".join(rows.scalars().all())
        finally:
            # Undoes the DROP: nothing here is committed.
            await session.rollback()
    assert "ix_audit_log_action_created_at" in plan, plan
    assert "Sort" not in plan, plan
    index_cond = next(line for line in plan.splitlines() if "Index Cond" in line)
    assert "action" in index_cond, (
        f"`action` is re-checked per row rather than bounding the scan:\n{plan}"
    )


# The status-leading composites are asserted structurally rather than by plan.
# At the harness's row counts the planner can legitimately serve a status filter
# from the plain ordering index — walking it and discarding non-matches is
# cheaper over a handful of rows — so a plan assertion here would pin the
# planner's small-table CHOICE, not the property the index exists for. What the
# composite buys is measured at volume in the migration's docstring (a rare,
# scattered status: 18 928 rows discarded per page with the plain index alone,
# 2.145 ms / 591 buffers → 0.036 ms / 4). What is deterministic, and what this
# asserts, is the shape: status leads, then the list's own sort order.
_COMPOSITES = [
    ("invoices", "ix_invoices_status_created_at_id", "status", "created_at"),
    ("payments", "ix_payments_status_created_at_id", "status", "created_at"),
    ("exceptions", "ix_exceptions_status_created_at_id", "status", "created_at"),
    ("expenses", "ix_expenses_status_created_at_id", "status", "created_at"),
    ("purchase_orders", "ix_purchase_orders_status_created_at_id", "status", "created_at"),
    ("contracts", "ix_contracts_status_created_at_id", "status", "created_at"),
    (
        "corporate_card_transactions",
        "ix_corp_card_recon_status_txn_date_id",
        "reconciliation_status",
        "txn_date",
    ),
]


@pytest.mark.parametrize(("table", "index_name", "filter_col", "order_col"), _COMPOSITES)
async def test_status_composite_leads_with_the_filtered_column(
    realdb, table, index_name, filter_col, order_col
):
    async with realdb.sessionmaker(TENANT)() as session:
        actual = (await _index_defs(session, table))[index_name]
    _prefix, _sep, built = actual.partition("USING btree ")
    columns = [c.strip() for c in built.strip("() ").split(",")]
    assert columns == [filter_col, f"{order_col} DESC", "id DESC"], (
        f"{index_name} is {columns}; the list filters on {filter_col} and orders "
        f"by {order_col} DESC, id DESC"
    )
