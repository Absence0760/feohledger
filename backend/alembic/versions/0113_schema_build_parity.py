"""Converge a migrated database onto the schema ``create_all`` builds (both planes).

Why
---
A database here is built one of two ways, and they must agree
(`docs/decisions.md` §274, `backend/docs/database.md` § Schema parity):

* a database that predates a release runs the Alembic chain;
* a NEW tenant (``tenant_provisioning._create_tenant_tables``) and a NEW control
  plane (``control_plane_bootstrap.initialise``) are built by ``create_all`` and
  never run a revision.

``tests/test_schema_build_parity.py`` (#575) builds one database each way — the
migrated one from the pre-Alembic schema — and compares the whole catalogue.
It found the two builds disagreeing in six ways. Production's control plane and
tenants were built by ``create_all`` at v0.1.0, so they already have the ORM's
shape — with one exception, which does reach production: a tenant ``create_all``
built is not stamped, so the first ``deploy.sh`` after provisioning replays the
whole chain over it, and 0029 / 0051 / 0082 guarded their foreign keys by their
OWN names only. Each re-added an FK the tenant already had under
``create_all``'s name, so a tenant deployed over since it was provisioned
carries 14 duplicate foreign keys, each checked again on every write. Those guards now accept either name, and
step 2 drops the duplicates where they landed. Everything else differs only on
databases that went through the chain — long-lived development databases, and
any database restored from a pre-v0.1.0 backup — and is a no-op elsewhere.

**The ORM is the head schema** (§274 builds a fresh control plane on exactly
that claim), so this revision moves the migrated side to it rather than the
other way round, and no model changes:

1. **Server defaults no model declares** (``_TENANT_DEFAULTS`` /
   ``_CONTROL_DEFAULTS``). Revisions added NOT NULL columns as
   ``ADD COLUMN … NOT NULL DEFAULT x`` so existing rows backfilled, and the
   model declared the default Python-side only (``default=``), which the ORM
   sends on every INSERT. Dropped here: a raw INSERT that leans on one of these
   defaults already fails on every production database, and now fails in
   development too instead of passing there alone.
2. **Foreign keys named by hand in a revision** (``_TENANT_FK_RENAMES`` /
   ``_CONTROL_FK_RENAMES``) — 0029's ``fk_<table>_entity`` family, 0051's
   inter-company pair, 0082's retry link, 0065's partner link — where
   ``create_all`` uses Postgres's own ``<table>_<column>_fkey``. Renamed, so a
   later revision that names one finds it on every database — or, where a
   replay left both, the revision's duplicate is dropped.
3. **Foreign keys with an ON DELETE action the model does not declare**
   (``_FK_ACTIONS``). The ORM deletes the children it owns itself
   (``BankStatement.transactions`` cascades; vendor delete removes its sanctions
   checks explicitly; cards and payments are never hard-deleted), which is what
   production already relies on.
4. **Uniqueness enforced by an index where the model declares a constraint**
   (``_TENANT_UNIQUE_CONSTRAINTS`` / ``_CONTROL_UNIQUE_CONSTRAINTS``). Promoted
   in place with ``ADD CONSTRAINT … USING INDEX`` — no rebuild, no window
   without the guarantee.
5. **What the chain never built**: ``card_reveal_tokens.updated_at`` and its
   ``organization_id`` index (0016 predates both on the model); the
   ``ix_payments_provider_payment_id`` the model declares over the whole column,
   where 0007 built it partial; ``email_verifications``' token uniqueness as the
   model spells it; and the control plane's ``assistant_usage``, which 0032
   creates only on a database without ``invoices`` — never on a pre-Alembic
   control plane, which ``seed.py`` had built with every table.
6. **Nullability and width the model states differently**
   (``_TENANT_NOT_NULL``, ``invoice_embeddings.model``): two columns the model
   makes NOT NULL that their revisions left nullable (any NULL is backfilled
   with the model's default first), and one VARCHAR(100) the model declares
   unbounded (widening a varchar rewrites nothing).

Revision ID: 0113_schema_build_parity
Revises: 0112_free_plan_erp
Create Date: 2026-10-19

BOTH PLANES, decided by the ``users`` table (control plane only), each step
gated on the catalogue so it is a no-op where the database already matches.
Idempotent. ``downgrade()`` is a no-op: before this revision the two builds
disagreed, so there is no single prior schema to return to, and re-creating the
migrated-only shape would re-open the drift.
"""

from sqlalchemy import text

from alembic import op

revision = "0113_schema_build_parity"
down_revision = "0112_free_plan_erp"
branch_labels = None
depends_on = None


#: ``(table, column)``: a server default a revision set and the model does not
#: declare (the model's ``default=`` is Python-side). Tenant plane.
_TENANT_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("agent_decisions", "autonomy_level"),
    ("agent_decisions", "confidence"),
    ("assistant_conversations", "meta"),
    ("assistant_messages", "input_tokens"),
    ("assistant_messages", "output_tokens"),
    ("assistant_messages", "tool_calls"),
    ("bank_statements", "currency"),
    ("bank_statements", "id"),
    ("bank_statements", "matched_count"),
    ("bank_statements", "transaction_count"),
    ("bank_transactions", "currency"),
    ("bank_transactions", "id"),
    ("budgets", "currency"),
    ("budgets", "dimension"),
    ("card_reveal_tokens", "id"),
    ("cash_plans", "periods"),
    ("cash_plans", "selected_offer_ids"),
    ("cash_plans", "total_outlay_selected"),
    ("cash_plans", "total_savings_selected"),
    ("cash_plans", "unconverted_count"),
    ("cash_plans", "unretimed_offer_ids"),
    ("catalog_items", "currency"),
    ("catalog_items", "is_active"),
    ("catalogs", "catalog_type"),
    ("catalogs", "is_active"),
    ("catalogs", "is_preferred"),
    ("contracts", "auto_renew"),
    ("contracts", "contract_type"),
    ("contracts", "currency"),
    ("contracts", "not_to_exceed"),
    ("contracts", "renewal_notice_days"),
    ("contracts", "status"),
    ("corporate_card_transactions", "currency"),
    ("corporate_card_transactions", "reconciliation_status"),
    ("credit_memos", "currency"),
    ("credit_memos", "status"),
    ("data_subject_requests", "fields_redacted"),
    ("data_subject_requests", "status"),
    ("discount_offers", "currency"),
    ("discount_offers", "scope"),
    ("discount_offers", "source"),
    ("discount_offers", "status"),
    ("discount_offers", "tiers"),
    ("expense_policies", "active"),
    ("expense_policies", "per_diem_currency"),
    ("expense_preapprovals", "currency"),
    ("expense_preapprovals", "status"),
    ("expense_reports", "currency"),
    ("expense_reports", "status"),
    ("expense_reports", "total_amount"),
    ("expenses", "currency"),
    ("expenses", "payment_method"),
    ("expenses", "reimbursable"),
    ("expenses", "status"),
    ("intake_requests", "currency"),
    ("intake_requests", "request_type"),
    ("intake_requests", "status"),
    ("intl_tax_records", "id"),
    ("intl_tax_records", "reverse_charge"),
    ("invoice_embeddings", "corrected_fields"),
    ("invoice_embeddings", "model"),
    ("notifications", "entity_type"),
    ("payment_runs", "requires_cfo_approval"),
    ("peppol_transmissions", "direction"),
    ("peppol_transmissions", "status"),
    ("positive_pay_files", "item_count"),
    ("positive_pay_files", "status"),
    ("positive_pay_files", "total_amount"),
    ("punchout_sessions", "currency"),
    ("punchout_sessions", "status"),
    ("purchase_requisitions", "currency"),
    ("purchase_requisitions", "status"),
    ("purchase_requisitions", "total"),
    ("quality_inspections", "result"),
    ("quality_inspections", "status"),
    ("recurring_invoice_templates", "cadence"),
    ("recurring_invoice_templates", "currency"),
    ("recurring_invoice_templates", "day_of_period"),
    ("recurring_invoice_templates", "generated_count"),
    ("recurring_invoice_templates", "status"),
    ("report_definitions", "dimensions"),
    ("report_definitions", "filters"),
    ("report_definitions", "measures"),
    ("report_definitions", "sort"),
    ("sanctions_checks", "id"),
    ("scheduled_reports", "enabled"),
    ("scheduled_reports", "id"),
    ("scheduled_reports", "period_days"),
    ("supplier_chat_threads", "status"),
    ("tax_1099_filings", "accepted_count"),
    ("tax_1099_filings", "rejected_count"),
    ("tax_1099_filings", "submitted_count"),
    ("vendor_change_requests", "id"),
    ("vendor_change_requests", "status"),
    ("vendor_extraction_priors", "correction_count"),
    ("vendor_statement_recon_lines", "resolution_status"),
    ("vendor_statement_reconciliations", "amount_mismatch_count"),
    ("vendor_statement_reconciliations", "currency"),
    ("vendor_statement_reconciliations", "line_count"),
    ("vendor_statement_reconciliations", "matched_count"),
    ("vendor_statement_reconciliations", "missing_our_side_count"),
    ("vendor_statement_reconciliations", "missing_their_side_count"),
    ("vendor_statement_reconciliations", "source_format"),
    ("vendor_statement_reconciliations", "status"),
    ("vendor_users", "is_active"),
    ("vendor_users", "mfa_enabled"),
    ("vendor_users", "must_change_password"),
    ("vendors", "is_1099_eligible"),
    ("vendors", "kyc_status"),
    ("vendors", "payments_blocked"),
    ("vendors", "risk_level"),
    ("vendors", "screening_status"),
    ("workflow_experiments", "assignments"),
    ("workflow_experiments", "min_sample_per_variant"),
    ("workflow_experiments", "primary_metric"),
    ("workflow_experiments", "split_a_pct"),
    ("workflow_experiments", "status"),
    ("workflow_suggestions", "confidence_pct"),
    ("workflow_suggestions", "status"),
    ("workflow_suggestions", "vendor_name"),
)
#: The same, control plane.
_CONTROL_DEFAULTS: tuple[tuple[str, str], ...] = (
    # 0032 built these three with DEFAULT 0; the model has none.
    ("assistant_usage", "input_tokens"),
    ("assistant_usage", "output_tokens"),
    ("assistant_usage", "request_count"),
    ("api_key_usage", "request_count"),
    ("api_keys", "scopes"),
    ("email_verifications", "meta"),
    ("plans", "currency"),
    ("plans", "entitlements"),
    ("plans", "is_active"),
    ("plans", "monthly_price"),
    ("plans", "seat_component"),
    ("plans", "trial_days"),
    ("plans", "usage_components"),
    ("subscriptions", "status"),
    ("users", "mfa_enabled"),
    ("users", "must_change_password"),
    ("webauthn_credentials", "name"),
    ("webauthn_credentials", "sign_count"),
    ("webhook_deliveries", "attempt_count"),
    ("webhook_deliveries", "status"),
    ("webhook_subscriptions", "active"),
    ("webhook_subscriptions", "event_types"),
)

#: ``(table, column, backfill SQL)``: NOT NULL on the model (a non-Optional
#: ``Mapped[...]``), nullable as its revision built it. Any NULL a raw write
#: left behind is set to the model's own ``default=`` first.
_TENANT_NOT_NULL: tuple[tuple[str, str, str], ...] = (
    ("invoice_embeddings", "corrected_fields", "'{}'::jsonb"),
    ("quality_inspections", "status", "'completed'"),
)

#: ``(table, old name, new name)``: a revision's hand-picked FK name → the name
#: ``create_all`` gives the same constraint.
_TENANT_FK_RENAMES: tuple[tuple[str, str, str], ...] = (
    *(
        (t, f"fk_{t}_entity", f"{t}_entity_id_fkey")
        for t in (
            "credit_memos",
            "exceptions",
            "gl_accounts",
            "goods_receipts",
            "invoices",
            "payment_runs",
            "payments",
            "purchase_orders",
            "vendors",
            "virtual_cards",
            "workflow_definitions",
        )
    ),
    ("invoices", "fk_invoices_counterparty_entity", "invoices_counterparty_entity_id_fkey"),
    ("invoices", "fk_invoices_intercompany_mirror", "invoices_intercompany_mirror_id_fkey"),
    ("payments", "fk_payments_retry_of_payment_id", "payments_retry_of_payment_id_fkey"),
)
_CONTROL_FK_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("organizations", "fk_organizations_parent_org_id", "organizations_parent_org_id_fkey"),
)

#: ``(table, constraint, column, referenced table)``: FKs a revision gave an
#: ON DELETE action the model does not declare. Re-created as plain FKs.
_FK_ACTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("bank_transactions", "bank_transactions_statement_id_fkey", "statement_id", "bank_statements"),
    (
        "bank_transactions",
        "bank_transactions_matched_payment_id_fkey",
        "matched_payment_id",
        "payments",
    ),
    ("card_reveal_tokens", "card_reveal_tokens_card_id_fkey", "card_id", "virtual_cards"),
    ("sanctions_checks", "sanctions_checks_vendor_id_fkey", "vendor_id", "vendors"),
)

#: ``(table, name, columns)``: a model's ``UniqueConstraint`` that its revision
#: built as a bare ``CREATE UNIQUE INDEX`` of the same name and columns.
_TENANT_UNIQUE_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("tax_1099_filings", "uq_tax_1099_filing_idempotency", "organization_id, idempotency_key"),
    ("workflow_suggestions", "uq_workflow_suggestions_dedupe_key", "dedupe_key"),
)
_CONTROL_UNIQUE_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("api_key_usage", "uq_api_key_usage_key_day", "api_key_id, usage_date"),
    ("webhook_deliveries", "uq_webhook_delivery_sub_event", "subscription_id, event_id"),
)

#: The control plane's ``assistant_usage`` as ``app.models.assistant.AssistantUsage``
#: declares it — 0032's own DDL, minus the three column defaults the model does
#: not have (``_CONTROL_DEFAULTS`` drops them where 0032 did run).
_ASSISTANT_USAGE = (
    """
    CREATE TABLE IF NOT EXISTS assistant_usage (
        id uuid PRIMARY KEY,
        organization_id uuid NOT NULL,
        period varchar(7) NOT NULL,
        input_tokens integer NOT NULL,
        output_tokens integer NOT NULL,
        request_count integer NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_assistant_usage_org_period UNIQUE (organization_id, period)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_assistant_usage_organization_id "
    "ON assistant_usage (organization_id)",
)


def _scalar(sql: str, **params):
    return op.get_bind().execute(text(sql), params).scalar()


def _has_table(name: str) -> bool:
    return bool(_scalar("SELECT to_regclass(:n) IS NOT NULL", n=f"public.{name}"))


def _has_constraint(table: str, name: str) -> bool:
    return bool(
        _scalar(
            "SELECT 1 FROM pg_constraint WHERE conname = :n AND conrelid = to_regclass(:t)",
            n=name,
            t=f"public.{table}",
        )
    )


def _drop_defaults(columns: tuple[tuple[str, str], ...]) -> None:
    for table, column in columns:
        has_default = _scalar(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c "
            "AND column_default IS NOT NULL",
            t=table,
            c=column,
        )
        if has_default:
            op.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT')


def _rename_fks(renames: tuple[tuple[str, str, str], ...]) -> None:
    for table, old, new in renames:
        if not _has_constraint(table, old):
            continue
        if _has_constraint(table, new):
            # Both: a `create_all` tenant whose first deploy replayed the
            # owning revision, whose guard knew only its own name — the same
            # FK twice, checked twice on every write. Keep the ORM's.
            op.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT "{old}"')
        else:
            op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"')


def _plain_fks() -> None:
    for table, name, column, ref in _FK_ACTIONS:
        action = _scalar(
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conname = :n AND conrelid = to_regclass(:t)",
            n=name,
            t=f"public.{table}",
        )
        # 'a' is NO ACTION, the default the model's plain ForeignKey builds.
        if action is not None and action != "a":
            op.execute(
                f'ALTER TABLE "{table}" DROP CONSTRAINT "{name}", '
                f'ADD CONSTRAINT "{name}" FOREIGN KEY ("{column}") REFERENCES "{ref}" (id)'
            )


def _unique_constraints(constraints: tuple[tuple[str, str, str], ...]) -> None:
    for table, name, columns in constraints:
        if not _has_table(table) or _has_constraint(table, name):
            continue
        if _scalar("SELECT to_regclass(:n) IS NOT NULL", n=f"public.{name}"):
            op.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" UNIQUE USING INDEX "{name}"')
        else:
            op.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" UNIQUE ({columns})')


def _not_null() -> None:
    for table, column, backfill in _TENANT_NOT_NULL:
        nullable = _scalar(
            "SELECT is_nullable = 'YES' FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c",
            t=table,
            c=column,
        )
        if nullable:
            op.execute(f'UPDATE "{table}" SET "{column}" = {backfill} WHERE "{column}" IS NULL')
            op.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET NOT NULL')


def _tenant() -> None:
    _drop_defaults(_TENANT_DEFAULTS)
    _not_null()
    if _scalar(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'invoice_embeddings' AND column_name = 'model' "
        "AND character_maximum_length IS NOT NULL"
    ):
        # The model is a bare `String` (unbounded); 0003 built VARCHAR(100).
        # Widening a varchar is a catalogue-only change — no table rewrite.
        op.execute("ALTER TABLE invoice_embeddings ALTER COLUMN model TYPE varchar")
    _rename_fks(_TENANT_FK_RENAMES)
    _plain_fks()
    _unique_constraints(_TENANT_UNIQUE_CONSTRAINTS)
    if _has_table("card_reveal_tokens"):
        op.execute(
            "ALTER TABLE card_reveal_tokens "
            "ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_card_reveal_tokens_organization_id "
            "ON card_reveal_tokens (organization_id)"
        )
    partial = _scalar(
        "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' "
        "AND indexname = 'ix_payments_provider_payment_id' AND indexdef LIKE '% WHERE %'"
    )
    if partial:
        op.execute("DROP INDEX ix_payments_provider_payment_id")
        op.execute("CREATE INDEX ix_payments_provider_payment_id ON payments (provider_payment_id)")


def _control() -> None:
    _drop_defaults(_CONTROL_DEFAULTS)
    _rename_fks(_CONTROL_FK_RENAMES)
    _unique_constraints(_CONTROL_UNIQUE_CONSTRAINTS)
    for stmt in _ASSISTANT_USAGE:
        op.execute(stmt)
    if _has_table("email_verifications"):
        # The model's `token` is `unique=True, index=True`: ONE unique index,
        # `ix_email_verifications_token`. 0001 built an inline UNIQUE
        # (`email_verifications_token_key`) beside a plain index of that name.
        plain = _scalar(
            "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexname = 'ix_email_verifications_token' "
            "AND indexdef NOT LIKE 'CREATE UNIQUE %'"
        )
        if plain:
            op.execute("DROP INDEX ix_email_verifications_token")
            op.execute(
                "CREATE UNIQUE INDEX ix_email_verifications_token ON email_verifications (token)"
            )
        if _has_constraint("email_verifications", "email_verifications_token_key"):
            op.execute(
                "ALTER TABLE email_verifications DROP CONSTRAINT email_verifications_token_key"
            )


def upgrade() -> None:
    if _has_table("users"):
        _control()
    elif _has_table("invoices"):
        _tenant()


def downgrade() -> None:
    # See the module docstring: there is no single prior schema to restore.
    pass
