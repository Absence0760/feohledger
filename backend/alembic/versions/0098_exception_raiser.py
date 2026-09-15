"""Segregation of duties on the exception queue: who raised the flag (tenant).

The hole
--------
``POST /api/exceptions/{id}/resolve`` and ``/bulk/resolve`` gated on
``require_roles(ADMIN, AP_MANAGER)`` and nothing else. Clearing one of the
payment-BLOCKING types (``api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES`` —
``duplicate`` / ``fraud_flag`` / ``line_total_mismatch`` /
``payment_reconciliation``) is the human sign-off that lets a payment run pay
the invoice, and invoice approval gates on none of them. So the one control
standing between a flagged payable and money leaving could be stood down by the
very actor it was raised against.

``services/exception_lifecycle.segregation_refusal`` now refuses a clearing
verb on two axes. Only the second needs a column:

* **the payable's implicated actors** — ``Invoice.uploaded_by_id`` ∪
  ``Invoice.segregation_actor_ids``, already on the invoice since migrations
  0096/0097 and already refused the *approval* by
  ``approval_chain.violates_segregation``. No new storage; the exception's
  invoice is the subject.
* **the raiser** — this column. The set above cannot reach the one case that
  matters most: an ``ap_manager`` who approves a vendor bank-detail change is
  not the uploader of the invoices that change re-points, so
  ``api/vendors._flag_payable_invoices_for_bank_change`` raises a ``fraud_flag``
  on each of them and — until now — that same actor could clear every one and
  execute the run. ``docs/authentication.md`` recorded exactly that: "the
  compensating ``fraud_flag`` ... is not a second control against the same
  actor: exception resolution has no segregation check either."

What the column means
---------------------
``raised_by_user_id`` is the control-plane user **whose own act this exception
exists to have a second person look at**. It is emphatically *not* "who was
signed in". Almost every raise site is a detector or a sweep — the warnings
recompute, semantic duplicate detection, extraction failure, the payment
reconciler, the settlement-mismatch check, the ERP webhook, a Positive Pay bank
return — and several of those run from a door that *does* have a user in scope
who did not cause the finding at all (an AP manager PATCHing a field re-runs
``refresh_warnings``; an operator importing a bank-return file did not alter the
cheque). Recording those actors would manufacture a refusal against a bystander
and an absolution for whoever really caused the flag, which is the same error
§141/§152 refused to make by backfilling. Those sites store NULL, explicitly and
with a stated reason, enforced by ``tests/test_exception_raiser_stamping.py``.

NULL is therefore permissive, exactly as a NULL ``uploaded_by_id`` is: a
system-raised exception stays resolvable. Failing closed would make every
sweep-raised and ingestion-raised exception permanently unclearable — an outage
on the queue, not a control (decisions §131's reasoning, second application).

``uuid``, no FK: ``users`` is control-plane and ``exceptions`` is tenant-scoped,
so the reference cannot be enforced in this database. Same placement and same
reason as ``exceptions.assigned_to_user_id`` (which this column sits beside) and
as migrations 0095, 0096 and 0097.

No backfill
-----------
``exceptions`` records ``created_at`` and nothing about who caused the finding,
and the ``exception.raised`` audit row it writes carries ``actor_id = NULL`` on
every single historical row precisely *because* a detector opened it. There is
no honest raiser to recover, and every available proxy — the invoice's uploader,
the last actor on the invoice's trail, the assignee — manufactures either a
refusal (a real resolver newly barred on evidence nobody produced) or an
absolution (a fabricated name standing in a fraud control's input). Rows created
before this migration therefore implicate nobody through this axis. They are
still covered by the implicated-actor axis, which reads the invoice columns and
needs no new data.

Revision ID: 0098_exception_raiser
Revises: 0097_segregation_actor_set
Create Date: 2026-09-14

The revision id is 21 characters. ``alembic_version.version_num`` is
``VARCHAR(32)`` and a longer id aborts ``alembic upgrade head`` before applying
anything — migration 0086 shipped that bug. ``tests/test_alembic_revision_ids.py``
is the guard; the filename matches the revision id so the two cannot drift (the
trap 0094 set).

TENANT DB ONLY: ``exceptions`` is not in ``tenant_provisioning.CONTROL_TABLES``,
so it does not exist on the control-plane DB. The statement is gated on the
table existing, so the revision no-ops there and fans out to every tenant DB via
``scripts/migrate_all_tenants.py`` (or ``FEOH_MIGRATE_TENANT=feoh_<slug> alembic
upgrade head`` for one). Fresh tenants get the shape from ``create_all`` in
``tenant_provisioning`` (the column is on the model) — this revision only
backfills the shape into existing tenant DBs.

Idempotent + reversible: ``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS``.
"""

from sqlalchemy import text

from alembic import op

revision = "0098_exception_raiser"
down_revision = "0097_segregation_actor_set"
branch_labels = None
depends_on = None


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
    if _has_table("exceptions"):
        op.execute("ALTER TABLE exceptions ADD COLUMN IF NOT EXISTS raised_by_user_id uuid")


def downgrade() -> None:
    if _has_table("exceptions"):
        op.execute("ALTER TABLE exceptions DROP COLUMN IF EXISTS raised_by_user_id")
