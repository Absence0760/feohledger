// Types for the chart of accounts — the `/api/gl-accounts` surface.
//
// This module is the single owner of the GL-account wire shape. Before it, the
// same payload was described four times (two `api/` modules and two invoice
// modals), each a narrower guess at the same endpoint; `GlAccountOption` below
// is now derived from the full row rather than restated beside it.
//
// No money lives here — a GL account is a code and a label, not an amount.

import type { MessageKey } from '$lib/i18n/messages';

/**
 * One row of `GET /api/gl-accounts`.
 *
 * `entity_id` is the field that makes the response readable, and it does NOT
 * mean what the same column means on every other business table. On `Invoice`,
 * `Vendor` or `Payment` a NULL is an unstamped legacy row; here NULL means the
 * account is **SHARED across every entity** (`backend/app/models/gl_account.py`).
 * So:
 *  - consolidated view (`X-Entity-ID` absent) → every entity's chart at once,
 *    and two subsidiaries may each legitimately hold their own `6000`;
 *  - an entity selected → `shared ∪ that entity's own`, where an entity row
 *    with the same code as a shared one is an override of it.
 * Both cases are pairs of otherwise-identical rows, which is why the column is
 * rendered rather than merely carried.
 */
export interface GlAccount {
	id: string;
	code: string;
	name: string;
	/** Free-form `String(50)` on the backend — see {@link GL_ACCOUNT_TYPES}. */
	account_type: string | null;
	parent_code: string | null;
	is_active: boolean;
	/** The id this account carries in the connected ERP, when it came from one. */
	erp_account_id: string | null;
	/** `null` = shared across every entity. See the note above. */
	entity_id: string | null;
}

/**
 * The narrow view a GL **picker** needs — a `Pick` of the row above, not a
 * second declaration of it, so the two cannot drift.
 *
 * **The bound value differs by picker, and that is the data model, not an
 * inconsistency.** `ExpenseModal`, `RequisitionModal` and `CatalogModal` bind
 * the uuid `id`, because `Expense.gl_account_id`, `RequisitionLineItem.
 * gl_account_id` and `CatalogItem.gl_account_id` are real
 * `ForeignKey("gl_accounts.id")` columns. `InvoiceModal` and
 * `CreateInvoiceModal` bind the `code`, because `Invoice.gl_account` and
 * `InvoiceLineItem.gl_account` are `String(100)` columns holding the code —
 * and a long list of backend readers treat that string AS the code: budget
 * dimension matching (`services/budget_service`), the ad-hoc report builder,
 * the PO-matching commodity resolver (`services/matching_rules`, keyed
 * `commodity_rules["<code>"]`), approval routing rules (`RoutingField`), the
 * 1099 box map (`services/tax_1099`, with glob patterns over the code), the
 * vendor GL priors behind bulk recode (`services/gl_recode`), and the AI
 * extraction catalog. Binding the uuid on the invoice side would write a uuid
 * into that column and silently break every one of them.
 *
 * `entity_id` is carried so a picker can say WHICH chart an option belongs to
 * — see {@link glAccountOptionLabel}.
 */
export type GlAccountOption = Pick<
	GlAccount,
	'id' | 'code' | 'name' | 'account_type' | 'entity_id'
>;

/**
 * The entity context a picker label needs — structurally satisfied by
 * `entityStore` (`$lib/stores/entity.svelte`), which is where every caller
 * gets it from. Declared structurally rather than importing the store so this
 * module stays a pure, unit-testable types module with no rune dependency.
 */
export interface GlAccountScope {
	/** `entityStore.multiEntity` — true once the tenant has more than one entity. */
	multiEntity: boolean;
	/** `entityStore.entities` — the names behind `entity_id`. */
	entities: readonly { id: string; name: string }[];
}

/**
 * The label one GL option renders with — the single owner of that decision,
 * rather than the same conditional copied into all five picker sites.
 *
 * On a **single-entity** tenant, and for any **shared** account (`entity_id`
 * NULL), the label is bare: the scope distinction has no consequence there,
 * and the `/gl-accounts` Scope column is gated on exactly the same condition.
 *
 * On a multi-entity tenant an **entity-scoped** account gets its owning
 * entity's name appended, because the consolidated view (`X-Entity-ID` absent)
 * returns every subsidiary's chart at once and two subsidiaries may each
 * legitimately hold their own `6000` — two options that read identically and
 * code to different accounts. Note the backend guarantees a code is unique
 * within one *effective* chart (`api/gl_accounts._code_in_effective_chart`:
 * shared ∪ the selected entity), so this only ever fires in the consolidated
 * view, which is precisely where the reader has no other signal.
 *
 * `unknownEntity` is passed in (the caller supplies
 * `m('glAccounts.scope.unknownEntity')`) rather than resolved here, for the
 * same reason `entities` is structural: no i18n store import in a types module.
 */
export function glAccountOptionLabel(
	account: GlAccountOption,
	scope: GlAccountScope,
	opts: { withName?: boolean; unknownEntity: string }
): string {
	const base = opts.withName ? `${account.code} — ${account.name}` : account.code;
	if (!scope.multiEntity || !account.entity_id) return base;
	const entityId = account.entity_id;
	const name = scope.entities.find((e) => e.id === entityId)?.name ?? opts.unknownEntity;
	return `${base} (${name})`;
}

/**
 * Whether the current view may correct or retire this row — the backend's
 * `PATCH /api/gl-accounts/{id}` rule, which follows CREATE rather than read:
 * the consolidated view (`selectedEntityId` null) may edit any row, while with
 * an entity selected only that entity's OWN rows are editable. A shared row is
 * visible in every entity's chart but belongs to all of them, so retiring it
 * from inside one subsidiary would pull it out of every other's too — the
 * backend 403s that, and `/gl-accounts` says where to go instead of offering a
 * button that can only fail.
 */
export function canEditGlAccount(
	account: Pick<GlAccount, 'entity_id'>,
	selectedEntityId: string | null
): boolean {
	return selectedEntityId === null || account.entity_id === selectedEntityId;
}

/**
 * The four account types the domain documents (`models/gl_account.py`), used
 * for the `/gl-accounts` filter chips and the create form's select.
 *
 * The column is a free-form `String(50)`, not an enum, because an ERP sync
 * writes whatever the ERP's chart says — so this is the *known* set, never an
 * exhaustive one. Treat an unrecognised value the way `/purchase-orders` treats
 * an unrecognised status: render it raw rather than dropping the cell. A type
 * outside this list is reachable through the All chip, and only through it.
 */
export const GL_ACCOUNT_TYPES = ['asset', 'liability', 'revenue', 'expense'] as const;

export type GlAccountType = (typeof GL_ACCOUNT_TYPES)[number];

/** The i18n key carrying each type's label — never the English string itself. */
export const GL_ACCOUNT_TYPE_LABEL_KEYS: Record<GlAccountType, MessageKey> = {
	asset: 'glAccounts.type.asset',
	liability: 'glAccounts.type.liability',
	revenue: 'glAccounts.type.revenue',
	expense: 'glAccounts.type.expense'
};

/**
 * The message key for an account type, or `null` for one this build has no
 * wording for. Tolerant on purpose — same rule as `exceptionTypeLabelKey`: the
 * caller renders the raw server value rather than an empty cell.
 */
export function glAccountTypeLabelKey(type: string | null): MessageKey | null {
	if (!type) return null;
	return GL_ACCOUNT_TYPE_LABEL_KEYS[type as GlAccountType] ?? null;
}
