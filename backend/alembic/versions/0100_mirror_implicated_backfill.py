"""Inter-company mirrors routed before §192 inherit their source's implicated set (tenant).

The hole
--------
Since decisions §192, ``services/intercompany.route_intercompany_invoice`` stamps
the mirror payable with everyone implicated in its source —
``approval_chain.implicated_actors(source)``, the source's uploader ∪ its own
``segregation_actor_ids`` — minus the routing actor, who is the mirror's
uploader. A mirror routed before that carries no set, so the employee who
uploaded or shaped the source payable can still approve its mirror under the
counterparty entity, and — after approval — clear a payment-blocking exception
on it (``exception_lifecycle.segregation_refusal`` reads the same set, §169).
A mirror routed before §131 (2026-09-09) carries no uploader either, so its
router is free as well.

Unlike the no-backfill cases in 0096–0098, every input here is *observed*: the
source's two columns are on the source row, and the router is named on the
routing's own ``invoice.intercompany_routed`` audit row. So this copies
evidence rather than inventing it, and writes exactly what routing writes
today: ``intercompany.inherited_actor_ids(source, uploader_id=<the mirror's
uploader>)``, with the uploader filled from that audit row only where it is
NULL.

Which rows (decisions §198)
---------------------------
* **Which side is the mirror is read from the routing's audit row, not
  inferred.** ``intercompany_mirror_id`` is set on BOTH rows, each pointing at
  the other, and the entity columns are symmetric too (the mirror's
  ``entity_id`` is the origin's ``counterparty_entity_id`` and vice versa), so
  neither can say which is which. ``invoice_number = 'IC-' || <origin's>`` can,
  but it is editable through ``PATCH /api/invoices/{id}``. The
  ``invoice.intercompany_routed`` row with ``details.role = 'mirror'`` is
  written by the only code that creates a mirror, in the same transaction,
  since the feature shipped; it names ``origin_invoice_id`` and the router
  (``actor_id``), and ``audit_log`` is append-only at the database level
  (0022). The mutual FK is still required, so a pair is only touched while
  both rows still point at each other.
* **Every mirror not in a terminal status**, derived from
  ``workflow_engine.VALID_TRANSITIONS`` (a status with no successor — today
  only ``done``). Not "not yet past approval": the set also gates clearing a
  payment-blocking exception, which only matters AFTER approval
  (``api/payments.PAYABLE_INVOICE_STATUSES``), and ``paid`` can be voided back
  to ``approved``.
* **Only a mirror that carries no set.** SQL NULL (routed before 0097 added
  the column), JSON ``null`` (the column's ``JSONB`` type persists a Python
  ``None`` as JSON ``null``, so every mirror routed between 0097 and §192 has
  that, not SQL NULL) or ``[]``. A non-empty set is never touched.

Each mirror changed gets an ``invoice.segregation_backfilled`` audit row on its
own correlation id — ``actor_id`` NULL, because nobody acted (§141's rule), and
the old and new values of each column that moved. Without it, a mirror
approved by its source's uploader before this ran would read, from its columns
alone, as an approval that breached segregation; the row dates the change so
the historical decision stays re-derivable (§169's auditor argument).

Revision ID: 0100_mirror_implicated_backfill
Revises: 0099_purchase_order_currency
Create Date: 2026-09-22

The revision id is 31 characters. ``alembic_version.version_num`` is
``VARCHAR(32)`` (0086 shipped the overflow); ``tests/test_alembic_revision_ids.py``
is the guard, and the filename matches the id (the trap 0094 set).

TENANT DB ONLY: neither ``invoices`` nor ``audit_log`` is in
``tenant_provisioning.CONTROL_TABLES``, so the upgrade is gated on both existing
and no-ops on the control DB; it fans out via ``scripts/migrate_all_tenants.py``.
A freshly provisioned tenant has no legacy mirror, so ``create_all`` needs
nothing.

Idempotent: a second run finds every mirror it changed already carrying a set
or an uploader, matches nothing, and writes no audit row. ``downgrade`` is a
no-op — see its docstring.
"""

from sqlalchemy import text

from alembic import op

revision = "0100_mirror_implicated_backfill"
down_revision = "0099_purchase_order_currency"
branch_labels = None
depends_on = None

#: Statuses with no successor in ``workflow_engine.VALID_TRANSITIONS``: a mirror
#: in one has no approval and no payment ahead of it, so no decision the set
#: could gate. A literal, because a migration must not import app code; the test
#: re-derives it from the state machine so the two cannot disagree.
TERMINAL_STATUSES = ("done",)

#: The routing's own record of which side of a pair is the mirror.
ROUTED_ACTION = "invoice.intercompany_routed"
#: What this revision writes on each mirror it changes.
BACKFILL_ACTION = "invoice.segregation_backfilled"

#: ``intercompany.inherited_actor_ids(source, uploader_id=mirror_uploader)`` in
#: SQL: ``approval_chain.implicated_actors(source)`` — the source's uploader ∪
#: the members of its ``segregation_actor_ids`` array, as text — minus the
#: mirror's uploader, sorted byte-wise like Python's ``sorted``; NULL when
#: nobody is left, as routing writes. Each placeholder is a column expression.
#: A constant so the test can evaluate it against the Python for the same
#: inputs.
INHERITED_SET_SQL = """(
    SELECT jsonb_agg(actor ORDER BY actor COLLATE "C")
    FROM (
        SELECT jsonb_array_elements_text(
            CASE WHEN jsonb_typeof({source_set}) = 'array'
                 THEN {source_set} ELSE '[]'::jsonb END
        ) AS actor
        UNION
        SELECT ({source_uploader})::text WHERE {source_uploader} IS NOT NULL
    ) implicated
    WHERE {mirror_uploader} IS NULL OR actor <> ({mirror_uploader})::text
)"""

_TERMINAL_SQL = ", ".join(f"'{status}'" for status in TERMINAL_STATUSES)
_NEW_SET_SQL = INHERITED_SET_SQL.format(
    source_set="c.source_set",
    source_uploader="c.source_uploader",
    mirror_uploader="c.new_uploader",
)

#: One statement: the UPDATE and its audit rows commit together or not at all.
BACKFILL_SQL = f"""
WITH routed AS (
    SELECT DISTINCT ON (r.entity_id)
        r.entity_id AS mirror_id,
        r.details ->> 'origin_invoice_id' AS origin_id,
        r.actor_id AS router_id
    FROM audit_log r
    WHERE r.action = '{ROUTED_ACTION}'
      AND r.entity_type = 'invoice'
      AND r.details ->> 'role' = 'mirror'
    ORDER BY r.entity_id, r.created_at, r.id
),
candidates AS (
    SELECT
        m.id,
        m.organization_id,
        m.correlation_id,
        o.id AS origin_id,
        m.segregation_actor_ids AS old_set,
        m.uploaded_by_id AS old_uploader,
        COALESCE(m.uploaded_by_id, routed.router_id) AS new_uploader,
        o.uploaded_by_id AS source_uploader,
        o.segregation_actor_ids AS source_set
    FROM invoices m
    JOIN routed ON routed.mirror_id = m.id
    JOIN invoices o
      ON o.id = m.intercompany_mirror_id
     AND o.intercompany_mirror_id = m.id
     AND o.id::text = routed.origin_id
    WHERE m.status NOT IN ({_TERMINAL_SQL})
      AND (m.segregation_actor_ids IS NULL
           OR m.segregation_actor_ids IN ('null'::jsonb, '[]'::jsonb))
),
computed AS (
    SELECT c.*, {_NEW_SET_SQL} AS new_set
    FROM candidates c
),
stamped AS (
    UPDATE invoices m
    SET uploaded_by_id = p.new_uploader,
        segregation_actor_ids = COALESCE(p.new_set, m.segregation_actor_ids)
    FROM computed p
    WHERE m.id = p.id
      AND (p.new_set IS NOT NULL OR p.old_uploader IS DISTINCT FROM p.new_uploader)
    RETURNING p.id, p.organization_id, p.correlation_id, p.origin_id,
              p.old_set, p.new_set, p.old_uploader, p.new_uploader
)
INSERT INTO audit_log
    (id, correlation_id, organization_id, actor_id, action, entity_type, entity_id, details)
SELECT
    gen_random_uuid(),
    s.correlation_id,
    s.organization_id,
    NULL,
    '{BACKFILL_ACTION}',
    'invoice',
    s.id,
    jsonb_build_object(
        'revision', '{revision}',
        'origin_invoice_id', s.origin_id::text,
        'changes', (
            CASE WHEN s.new_set IS NOT NULL
                 THEN jsonb_build_object(
                     'segregation_actor_ids',
                     jsonb_build_object('old', s.old_set, 'new', s.new_set))
                 ELSE '{{}}'::jsonb END
            ||
            CASE WHEN s.old_uploader IS DISTINCT FROM s.new_uploader
                 THEN jsonb_build_object(
                     'uploaded_by_id',
                     jsonb_build_object('old', s.old_uploader, 'new', s.new_uploader))
                 ELSE '{{}}'::jsonb END
        )
    )
FROM stamped s
"""


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": name},
        ).scalar()
        is not None
    )


def upgrade() -> None:
    if not (_has_table("invoices") and _has_table("audit_log")):
        return
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    """A no-op, on purpose.

    The revision changes data, not schema, so 0098's schema is already in place
    and the stamped values are exactly what 0098's code would write for a mirror
    routed today. Reversing them would re-open the segregation hole this closes —
    and the ``invoice.segregation_backfilled`` rows, which ``audit_log``'s
    append-only trigger keeps, would then describe a state the invoices no
    longer have.
    """
