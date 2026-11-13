"""Record who approved an invoice by id, so their later receipt cannot release it (tenant).

The hole
--------
Receiving and approving are separate duties (decisions §267): whoever
hand-recorded a live goods receipt on an invoice's PO is refused as its
approver. The reverse order was open. A manager approves the invoice first,
then records the receipt that lifts its "billed beyond receipt" ``po_mismatch``
hold, and the PO-match auto-close lets it through, because
``invoice_warnings.receipts_clear_hold`` measured the recorder against the
invoice's implicated actors only, and nothing recorded the approver by id:
``approved_by`` is a display name, and the audit log is not local under
``FEOH_AUDIT_MODE=lambda`` when the refresh runs. A chain's level approvals are
recorded by id in ``workflow_instances.state_data``; a single-level approval
was not.

* ``invoices.approved_by_user_id`` — ``uuid``, nullable, no FK (a control-plane
  id, like ``uploaded_by_id``). Stamped from now on at every final approval by
  ``approval_chain.stamp_approver`` (decisions §389).

Backfill, from evidence only
----------------------------
Like 0126, and unlike §141's no-backfill cases, the person is recorded: every
final approval writes an audit row keyed to the invoice in the same
transaction as the ``approved_by`` stamp — ``invoice.approved`` (the approval
door, whose ``actor_id`` is the approver) or ``invoice.auto_approved`` (the
``/complete`` amount floor, whose ``actor_id`` is the caller, and extraction's
auto-approve, whose ``actor_id`` is the reader or nobody) — and ``audit_log``
is append-only at the database level (0022). By the time a deploy runs this,
lambda-mode rows have landed too.

An invoice is stamped only when ALL of these hold:

* ``approved_by_user_id`` is NULL — a stamp is never overwritten;
* it was approved (``approval_date`` or a non-empty ``approved_by``, the app's
  ``was_ever_approved``);
* its status is not ``done`` (0126's scope: ``paid`` can be voided back);
* its **latest** final-approval row (the approval of record, which is what
  ``approved_by`` names: every approval overwrites it) is unique at its
  timestamp, and names a person: an actor present, not the nil-UUID system
  sentinel, in the invoice's organization;
* and, if its approval was ever recalled (``invoice.approval_recalled``), every
  person its approval rows name is that same person. An invoice is approved
  more than once only through a recall (§374), and under
  ``FEOH_AUDIT_MODE=lambda`` a row's ``created_at`` is when the worker inserted
  it, which an unordered queue need not keep in event order — so "latest" is
  trusted to pick between two approvers only when there is no second approver
  to pick. A recalled invoice nobody re-approved keeps its one approver, as
  the app keeps it across a recall.

So nothing is guessed. An approval of record with no actor (an unattended
auto-approve), two rows at one instant naming different actors, or an
invoice whose approval left no row at all stays NULL, which the receiving rule
reads as "no approver known" — the residue §389 records. No audit row is
written for the stamp: the column restates, verbatim, the audit row it was
read from (0126's ``decided_by_user_id`` reasoning).

Revision ID: 0129_invoice_approver_id
Revises: 0128_payee_and_merge_stamp
Create Date: 2026-11-13

24 characters (``alembic_version.version_num`` is ``VARCHAR(32)``); the filename
matches the id.

TENANT DB ONLY: gated on ``invoices`` existing (and on ``audit_log`` for the
backfill), so a no-op on the control plane; fanned out by
``scripts/migrate_all_tenants.py``. One schema change, ``ADD COLUMN IF NOT
EXISTS``, mirrored on ``models.invoice.Invoice`` so a fresh tenant's
``create_all`` builds the same column; a fresh tenant has no history.
Idempotent: a second run finds every invoice it stamped carrying an approver
and matches nothing.
"""

from sqlalchemy import text

from alembic import op

revision = "0129_invoice_approver_id"
down_revision = "0128_payee_and_merge_stamp"
branch_labels = None
depends_on = None

#: Statuses with no successor in ``workflow_engine.VALID_TRANSITIONS`` (the test
#: re-derives it from the state machine, as 0126's does).
TERMINAL_STATUSES = ("done",)

#: ``email_intake.SYSTEM_ACTOR_ID`` / ``peppol_receive.SYSTEM_ACTOR_ID``.
SYSTEM_ACTOR_ID = "00000000-0000-0000-0000-000000000000"

#: The audit actions a final approval writes (``review.approve_invoice`` and
#: the two automatic approvals; the test pins them to the app).
APPROVAL_ACTIONS = ("invoice.approved", "invoice.auto_approved")

#: ``workflow_engine.RECALL_ACTION`` (the test pins it).
RECALL_ACTION = "invoice.approval_recalled"

_TERMINAL_SQL = ", ".join(f"'{status}'" for status in TERMINAL_STATUSES)
_APPROVAL_SQL = ", ".join(f"'{action}'" for action in APPROVAL_ACTIONS)

ADD_COLUMN_SQL = "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS approved_by_user_id uuid"

BACKFILL_SQL = f"""
WITH approval AS (
    SELECT
        a.entity_id AS invoice_id,
        a.actor_id,
        (
            a.actor_id IS NOT NULL
            AND a.actor_id <> '{SYSTEM_ACTOR_ID}'::uuid
            AND a.organization_id = i.organization_id
        ) IS TRUE AS names_person,
        rank() OVER (PARTITION BY a.entity_id ORDER BY a.created_at DESC) AS recency
    FROM audit_log a
    JOIN invoices i ON i.id = a.entity_id
    WHERE a.entity_type = 'invoice'
      AND a.action IN ({_APPROVAL_SQL})
),
of_record AS (
    SELECT p.invoice_id, (array_agg(p.actor_id))[1] AS actor_id
    FROM approval p
    WHERE p.recency = 1
    GROUP BY p.invoice_id
    HAVING bool_and(p.names_person) AND count(DISTINCT p.actor_id) = 1 AND count(*) = 1
),
contested AS (
    SELECT p.invoice_id
    FROM approval p
    WHERE p.names_person
    GROUP BY p.invoice_id
    HAVING count(DISTINCT p.actor_id) > 1
)
UPDATE invoices i
SET approved_by_user_id = r.actor_id
FROM of_record r
WHERE i.id = r.invoice_id
  AND NOT (
      EXISTS (
          SELECT 1 FROM audit_log rc
          WHERE rc.entity_type = 'invoice'
            AND rc.entity_id = i.id
            AND rc.action = '{RECALL_ACTION}'
      )
      AND EXISTS (SELECT 1 FROM contested c WHERE c.invoice_id = i.id)
  )
  AND i.approved_by_user_id IS NULL
  AND i.status NOT IN ({_TERMINAL_SQL})
  AND (i.approval_date IS NOT NULL OR coalesce(i.approved_by, '') <> '')
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
    if not _has_table("invoices"):
        return
    op.execute(ADD_COLUMN_SQL)
    if _has_table("audit_log"):
        op.execute(BACKFILL_SQL)


def downgrade() -> None:
    """Drop the column. Nothing below this revision reads it, and the audit rows
    it was copied from are still there."""
    if _has_table("invoices"):
        op.execute("ALTER TABLE invoices DROP COLUMN IF EXISTS approved_by_user_id")
