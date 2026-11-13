"""Invoices an employee created before §131 get the uploader their audit row names, and a
cleared exception records who cleared it (tenant).

The hole
--------
Before decisions §131 (2026-09-09) two employee paths built the ``Invoice``
without ``uploaded_by_id``: CSV import (``POST /api/invoices/import-csv``) and
recurring ``POST /api/recurring/{id}/generate-now``. Each still wrote the acting
employee into ``actor_id`` on an audit row keyed to the invoice —
``invoice.imported_csv`` for the import (written since 2026-09-08, #384; an
import before that left no row at all), and ``invoice.created`` with
``details.source = "recurring_template"`` for generate-now (written since the
feature shipped; the sweep's copy of that row has ``actor_id`` NULL).
``approval_chain.violates_segregation`` reads a NULL uploader as "no employee
created this", so on any such invoice not yet ``done`` the importer or generator
could still approve it, or clear a payment-blocking exception on it
(``exception_lifecycle.segregation_refusal`` reads the same set, §169).

The other pre-§131 employee paths always stamped the uploader (manual create and
file upload), and the inter-company mirror was closed by 0100 (§198), so these
two are the whole remainder.

Unlike §141's and §152's no-backfill cases, the person is *recorded* here: the
audit row is written by the only code that creates such an invoice, in the same
transaction, and ``audit_log`` is append-only at the database level (0022). So
this copies evidence rather than inventing it (decisions §367).

Which rows
----------
An invoice is stamped only when ALL of these hold:

* ``uploaded_by_id`` is NULL — a stamped uploader is never overwritten;
* its status has a successor in ``workflow_engine.VALID_TRANSITIONS`` (today
  every status but ``done``), on 0100's reasoning: the uploader also gates
  clearing a payment-blocking exception after approval, and ``paid`` can be
  voided back to ``approved``;
* its **creation records** — every audit row keyed to the invoice whose action
  is one a creation path writes (:data:`CREATION_ACTIONS`; the routing row
  counts only with ``role = 'mirror'``, since the origin side of a routing is not
  a creation) — are all **evidence rows**, and there is at least one;
* every evidence row names the same employee.

An evidence row is exactly one of two shapes, each cross-checked against the
invoice's own columns so a row cannot vouch for an invoice it does not describe:

* ``invoice.imported_csv`` with ``details.source = 'csv_import'``, on an invoice
  whose ``meta.imported.source`` is ``'csv_import'`` (the importer stamps both in
  one transaction; an operator-asserted provenance marker is a different source
  and does not qualify);
* ``invoice.created`` with ``details.source = 'recurring_template'`` and
  ``details.recurring_template_id`` equal to the invoice's own
  ``recurring_template_id``.

In both, the row's ``actor_id`` must be present, must not be the nil-UUID system
sentinel email intake and inbound PEPPOL use, and must belong to the invoice's
organization.

So nothing is guessed, and the cases that are not certain stay NULL:

* **system channels** — the recurring sweep's row (actor NULL), email intake,
  inbound PEPPOL, the supplier portal's submit and PO flip (actor NULL, or the
  system sentinel, and in every case not an evidence action);
* **conflicting evidence** — two evidence rows naming different employees, or an
  evidence row beside a creation record of another channel or one with no
  actor. The invoice keeps the legacy NULL reading; a human can decide.

Each invoice changed gets an ``invoice.segregation_backfilled`` audit row filed
under the invoice's ``correlation_id``, so it lands on that invoice's own SOX
trail — ``actor_id`` NULL, because nobody acted (§141's rule) — with the old and
new uploader and the ids of the audit rows that named them, so an approval given
before this ran stays legible as the one the rules then allowed.

Who cleared an exception: ``exceptions.decided_by_user_id``
-----------------------------------------------------------
Stamping the uploader refuses them the NEXT clearance, but not the one they
already gave. Before this revision the importer could dismiss a ``duplicate`` or
``fraud_flag`` on their own invoice (the NULL uploader let the decision through),
and that dismissal kept its §286 fingerprint, which suppresses the finding on
every refresh while the invoice is unchanged. So the suppression now re-asks
segregation of the person who decided (``exception_lifecycle.clearance_stands``),
and that needs the person on the row: the audit log cannot be read at refresh
time, because under ``FEOH_AUDIT_MODE=lambda`` the decision's row is not local
yet (0105's reason).

* ``exceptions.decided_by_user_id`` — ``uuid``, nullable, no FK (a control-plane
  id, as ``raised_by_user_id``). ``record_decision`` writes it on every resolve or
  dismiss from now on: the person, or NULL for a detector or an agent.
* Backfilled on rows already ``resolved`` / ``dismissed`` from their
  ``exception.resolved`` / ``exception.dismissed`` audit rows, only when every
  such row on the exception is a person's decision (an actor that is not the
  system sentinel, no ``via``, the row's ``new_status`` equal to the exception's,
  the same organization) and all name one person. Anything else stays NULL, and a
  NULL decider on a payment-blocking row never suppresses: fail closed, one more
  review. No audit row for this part: the column restates, verbatim, the audit
  row it was read from, and changes nobody's rights.

It runs before the uploader backfill, though the two are independent: neither
statement reads what the other writes.

Revision ID: 0126_uploader_audit_backfill
Revises: 0125_vendor_erp_links
Create Date: 2026-11-13

28 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

TENANT DB ONLY: gated on ``exceptions`` / ``invoices`` and ``audit_log``
existing, so a no-op on the control plane; fanned out by
``scripts/migrate_all_tenants.py``. One schema change, ``ADD COLUMN IF NOT
EXISTS``, mirrored on ``models.exception.Exception`` so a fresh tenant's
``create_all`` builds the same column; a fresh tenant has no legacy row.
Idempotent: a second run finds every invoice it stamped carrying an uploader and
every exception it filled carrying a decider, matches nothing and writes no
audit row. ``downgrade`` drops the column and leaves the data — see its
docstring.
"""

from sqlalchemy import text

from alembic import op

revision = "0126_uploader_audit_backfill"
down_revision = "0125_vendor_erp_links"
branch_labels = None
depends_on = None

#: Statuses with no successor in ``workflow_engine.VALID_TRANSITIONS``. A literal,
#: because a migration must not import app code; the test re-derives it from the
#: state machine so the two cannot disagree.
TERMINAL_STATUSES = ("done",)

#: ``email_intake.SYSTEM_ACTOR_ID`` / ``peppol_receive.SYSTEM_ACTOR_ID`` — the
#: nil UUID system ingestion writes into ``actor_id``. Never an employee.
SYSTEM_ACTOR_ID = "00000000-0000-0000-0000-000000000000"

#: The CSV importer's audit action and provenance (``csv_import.IMPORT_PROVENANCE_KEY``
#: / ``IMPORT_PROVENANCE_SOURCE``; the test pins them to the app's constants).
CSV_IMPORT_ACTION = "invoice.imported_csv"
IMPORT_PROVENANCE_KEY = "imported"
IMPORT_PROVENANCE_SOURCE = "csv_import"

#: ``recurring_invoices.generate_one``'s audit action and ``details.source``.
RECURRING_CREATED_ACTION = "invoice.created"
RECURRING_SOURCE = "recurring_template"

#: Inter-company routing's audit action; only its ``role = 'mirror'`` row is a
#: creation record (the mirror is created; the origin already existed).
ROUTED_ACTION = "invoice.intercompany_routed"

#: Every audit action a path that creates an invoice writes on it. An invoice
#: whose creation records include any row that is not an evidence row is left
#: alone: the records disagree about who, or what, created it.
CREATION_ACTIONS = (
    "invoice.created",  # manual entry (source=manual), recurring (source=recurring_template)
    "invoice.imported_csv",  # CSV import
    "invoice.uploaded",  # file upload
    "invoice.submitted_by_vendor",  # supplier portal submit
    "invoice.created_from_po",  # supplier portal PO flip
    "invoice.peppol_received",  # inbound PEPPOL
    ROUTED_ACTION,  # inter-company mirror (role = 'mirror' only)
)

#: What this revision writes on each invoice it changes.
BACKFILL_ACTION = "invoice.segregation_backfilled"

#: ``exception_lifecycle.ACTION_RESOLVED`` / ``ACTION_DISMISSED`` — the rows a
#: clearing decision writes (the test pins them).
DECISION_ACTIONS = ("exception.resolved", "exception.dismissed")
#: The statuses those decisions leave (``RESOLUTION_STATUSES``).
CLEARED_STATUSES = ("resolved", "dismissed")

_TERMINAL_SQL = ", ".join(f"'{status}'" for status in TERMINAL_STATUSES)
_CREATION_SQL = ", ".join(f"'{action}'" for action in CREATION_ACTIONS)

_DECISION_SQL = ", ".join(f"'{action}'" for action in DECISION_ACTIONS)
_CLEARED_SQL = ", ".join(f"'{status}'" for status in CLEARED_STATUSES)

ADD_DECIDER_SQL = "ALTER TABLE exceptions ADD COLUMN IF NOT EXISTS decided_by_user_id uuid"

#: Who cleared each closed exception, copied from its decision rows, and only
#: when every one of them is a person's and all name the same person.
DECIDER_BACKFILL_SQL = f"""
WITH decision AS (
    SELECT
        e.id AS exception_id,
        a.actor_id,
        (
            a.actor_id IS NOT NULL
            AND a.actor_id <> '{SYSTEM_ACTOR_ID}'::uuid
            AND (a.details -> 'via') IS NULL
            AND a.details ->> 'new_status' = e.status
            AND a.organization_id = e.organization_id
        ) IS TRUE AS names_person
    FROM exceptions e
    JOIN audit_log a ON a.entity_type = 'exception' AND a.entity_id = e.id
    WHERE e.status IN ({_CLEARED_SQL})
      AND e.decided_by_user_id IS NULL
      AND a.action IN ({_DECISION_SQL})
),
named AS (
    SELECT d.exception_id, (array_agg(d.actor_id))[1] AS actor_id
    FROM decision d
    GROUP BY d.exception_id
    HAVING bool_and(d.names_person) AND count(DISTINCT d.actor_id) = 1
)
UPDATE exceptions e
SET decided_by_user_id = n.actor_id
FROM named n
WHERE e.id = n.exception_id
  AND e.decided_by_user_id IS NULL
"""

#: One statement: the UPDATE and its audit rows commit together or not at all.
BACKFILL_SQL = f"""
WITH creation AS (
    SELECT
        a.id AS audit_id,
        a.entity_id AS invoice_id,
        a.created_at,
        (
            (
                (a.action = '{CSV_IMPORT_ACTION}'
                 AND a.details ->> 'source' = '{IMPORT_PROVENANCE_SOURCE}'
                 AND i.meta -> '{IMPORT_PROVENANCE_KEY}' ->> 'source'
                     = '{IMPORT_PROVENANCE_SOURCE}')
                OR
                (a.action = '{RECURRING_CREATED_ACTION}'
                 AND a.details ->> 'source' = '{RECURRING_SOURCE}'
                 AND i.recurring_template_id IS NOT NULL
                 AND a.details ->> 'recurring_template_id' = i.recurring_template_id::text)
            )
            AND a.actor_id IS NOT NULL
            AND a.actor_id <> '{SYSTEM_ACTOR_ID}'::uuid
            AND a.organization_id = i.organization_id
        ) IS TRUE AS names_employee,
        a.actor_id
    FROM audit_log a
    JOIN invoices i ON i.id = a.entity_id
    WHERE a.entity_type = 'invoice'
      AND a.action IN ({_CREATION_SQL})
      AND (a.action <> '{ROUTED_ACTION}' OR a.details ->> 'role' = 'mirror')
),
named AS (
    SELECT
        c.invoice_id,
        (array_agg(c.actor_id))[1] AS actor_id,
        jsonb_agg(c.audit_id::text ORDER BY c.created_at, c.audit_id) AS evidence_ids
    FROM creation c
    GROUP BY c.invoice_id
    HAVING bool_and(c.names_employee) AND count(DISTINCT c.actor_id) = 1
),
stamped AS (
    UPDATE invoices i
    SET uploaded_by_id = n.actor_id
    FROM named n
    WHERE i.id = n.invoice_id
      AND i.uploaded_by_id IS NULL
      AND i.status NOT IN ({_TERMINAL_SQL})
    RETURNING i.id, i.organization_id, i.correlation_id, n.actor_id, n.evidence_ids
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
        'evidence_audit_ids', s.evidence_ids,
        'changes', jsonb_build_object(
            'uploaded_by_id', jsonb_build_object('old', NULL, 'new', s.actor_id::text)
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
    if not _has_table("audit_log"):
        return
    if _has_table("exceptions"):
        op.execute(ADD_DECIDER_SQL)
        op.execute(DECIDER_BACKFILL_SQL)
    if _has_table("invoices"):
        op.execute(BACKFILL_SQL)


def downgrade() -> None:
    """Drop ``decided_by_user_id``; leave the uploaders.

    The column is read by nothing below this revision, so dropping it loses only
    a copy of what the decision audit rows already say. The stamped uploaders
    stay, on purpose: each is exactly what the path would write today, clearing
    it would re-open the segregation hole this closes, and the
    ``invoice.segregation_backfilled`` rows, which ``audit_log``'s append-only
    trigger keeps, would then describe a state the invoices no longer have.
    """
    if _has_table("exceptions"):
        op.execute("ALTER TABLE exceptions DROP COLUMN IF EXISTS decided_by_user_id")
