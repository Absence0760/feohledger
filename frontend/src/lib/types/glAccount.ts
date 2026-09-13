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
 * second declaration of it, so the two cannot drift. The value bound by every
 * picker is the uuid `id` (it matches `Expense.gl_account_id` and the
 * `bulk-gl-code` body); `InvoiceLineItem.gl_account` stores the `code` string.
 */
export type GlAccountOption = Pick<GlAccount, 'id' | 'code' | 'name' | 'account_type'>;

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
